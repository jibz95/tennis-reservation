import os
import logging
from datetime import datetime, timedelta

import requests
import anthropic
from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler

from tennis_client import TennisClient

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Surveillance : liste des watches en mémoire
# Chaque watch : {"date": "JJ/MM/AAAA", "heure": "14", "notified": False}
# ------------------------------------------------------------------
_watches: list[dict] = []


def _get_client() -> TennisClient:
    client = TennisClient()
    client.login()
    return client


def _validate_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%d/%m/%Y")
        return True
    except ValueError:
        return False


def _send_ntfy(title: str, message: str):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        logger.warning("NTFY_TOPIC non defini, notification ignoree")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "tennis"},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Erreur ntfy: {e}")


def _check_watches():
    """Tâche planifiée : vérifie les créneaux surveillés."""
    if not _watches:
        return

    active = [w for w in _watches if not w["notified"]]
    if not active:
        return

    logger.info(f"Vérification de {len(active)} surveillance(s)...")
    try:
        client = _get_client()
    except Exception as e:
        logger.error(f"Échec login pour surveillance: {e}")
        return

    for watch in active:
        date_str = watch["date"]
        heure = watch["heure"]
        try:
            slots = client.get_creneaux(date_str)
            matches = [s for s in slots if s["heure"] == f"{heure}h"]
            if matches:
                courts = ", ".join(s["label"] for s in matches)
                logger.info(f"Créneau disponible! {courts}")
                _send_ntfy(
                    title=f"Tennis - Creneau dispo le {date_str} a {heure}h",
                    message=f"Disponible : {courts}",
                )
                watch["notified"] = True
            else:
                logger.info(f"Pas de creneau a {heure}h le {date_str}")
        except Exception as e:
            logger.error(f"Erreur surveillance {date_str} {heure}h: {e}")


# Démarrer le scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(_check_watches, "interval", minutes=5, id="watch_job")
scheduler.start()


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@app.route("/health")
def health():
    watches_actives = [w for w in _watches if not w["notified"]]
    return jsonify({"status": "ok", "surveillances_actives": len(watches_actives)})


@app.route("/creneaux")
def creneaux():
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "Parametre 'date' manquant (format: JJ/MM/AAAA)"}), 400
    if not _validate_date(date_str):
        return jsonify({"error": f"Format de date invalide: '{date_str}'"}), 400

    try:
        client = _get_client()
        slots = client.get_creneaux(date_str)
        return jsonify({"date": date_str, "creneaux": slots})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Erreur inattendue: {e}"}), 500


@app.route("/reserver", methods=["GET", "POST"])
def reserver():
    if request.method == "GET":
        slot_id = request.args.get("slot_id")
        date_str = request.args.get("date")
    else:
        body = request.get_json(silent=True) or {}
        slot_id = body.get("slot_id")
        date_str = body.get("date")

    if not slot_id:
        return jsonify({"error": "Champ 'slot_id' manquant"}), 400
    if not date_str:
        return jsonify({"error": "Champ 'date' manquant (format: JJ/MM/AAAA)"}), 400
    if not _validate_date(date_str):
        return jsonify({"error": f"Format de date invalide: '{date_str}'"}), 400

    try:
        client = _get_client()
        client.get_creneaux(date_str)
        message = client.reserver(slot_id, date_str)
        return jsonify({"status": "ok", "message": message})
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Erreur inattendue: {e}"}), 500


@app.route("/surveiller", methods=["GET", "POST"])
def surveiller():
    if request.method == "GET":
        date_str = request.args.get("date")
        heure = str(request.args.get("heure", "")).replace("h", "")
    else:
        body = request.get_json(silent=True) or {}
        date_str = body.get("date")
        heure = str(body.get("heure", "")).replace("h", "")

    if not date_str:
        return jsonify({"error": "Champ 'date' manquant (format: JJ/MM/AAAA)"}), 400
    if not heure.isdigit():
        return jsonify({"error": "Champ 'heure' manquant ou invalide (ex: 14)"}), 400
    if not _validate_date(date_str):
        return jsonify({"error": f"Format de date invalide: '{date_str}'"}), 400

    # Éviter les doublons
    for w in _watches:
        if w["date"] == date_str and w["heure"] == heure and not w["notified"]:
            return jsonify({"status": "ok", "message": f"Surveillance deja active pour {date_str} a {heure}h"})

    _watches.append({"date": date_str, "heure": heure, "notified": False})
    logger.info(f"Surveillance ajoutee: {date_str} a {heure}h")
    return jsonify({"status": "ok", "message": f"Surveillance activee pour {date_str} a {heure}h"})


@app.route("/surveiller", methods=["DELETE"])
def annuler_surveillance():
    body = request.get_json(silent=True) or {}
    date_str = body.get("date")
    heure = str(body.get("heure", "")).replace("h", "")

    removed = 0
    for w in _watches:
        if w["date"] == date_str and w["heure"] == heure:
            w["notified"] = True  # marquer comme terminé
            removed += 1

    return jsonify({"status": "ok", "message": f"{removed} surveillance(s) annulee(s)"})


@app.route("/surveillances")
def list_surveillances():
    actives = [w for w in _watches if not w["notified"]]
    return jsonify({"surveillances": actives})


@app.route("/chat")
def chat():
    question = request.args.get("q", "").strip()
    if not question:
        return jsonify({"error": "Parametre 'q' manquant"}), 400

    today = datetime.now()
    system_prompt = f"""Tu es un assistant qui gère les réservations de tennis pour JB au club.
Aujourd'hui nous sommes le {today.strftime('%A %d/%m/%Y')}.
Demain c'est le {(today + timedelta(days=1)).strftime('%d/%m/%Y')}.

Tu peux :
- Lister les créneaux disponibles (outil get_creneaux)
- Réserver un créneau (outil reserver)
- Surveiller un créneau pour être notifié s'il se libère (outil surveiller)

Réponds toujours en français, de façon concise et naturelle.
Pour les dates relatives comme "demain", "jeudi", etc., convertis-les en JJ/MM/AAAA.
Si plusieurs créneaux sont disponibles et que l'utilisateur n'a pas précisé le court, choisis le premier disponible à l'heure demandée."""

    tools = [
        {
            "name": "get_creneaux",
            "description": "Récupère les créneaux de tennis disponibles pour une date donnée.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date au format JJ/MM/AAAA"}
                },
                "required": ["date"]
            }
        },
        {
            "name": "reserver",
            "description": "Réserve un créneau de tennis.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "slot_id": {"type": "string", "description": "ID du créneau (ex: 9_0_4)"},
                    "date": {"type": "string", "description": "Date au format JJ/MM/AAAA"}
                },
                "required": ["slot_id", "date"]
            }
        },
        {
            "name": "surveiller",
            "description": "Surveille un créneau et envoie une notification quand il se libère.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date au format JJ/MM/AAAA"},
                    "heure": {"type": "integer", "description": "Heure souhaitée (ex: 14)"}
                },
                "required": ["date", "heure"]
            }
        }
    ]

    try:
        ai = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        messages = [{"role": "user", "content": question}]

        # Boucle agent : Claude appelle les outils jusqu'à avoir la réponse
        while True:
            response = ai.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )

            # Pas d'appel d'outil → réponse finale
            if response.stop_reason == "end_turn":
                final = next(b.text for b in response.content if hasattr(b, "text"))
                return jsonify({"reponse": final})

            # Traiter les appels d'outils
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                name = block.name
                args = block.input
                logger.info(f"Claude appelle {name}({args})")

                try:
                    if name == "get_creneaux":
                        client = _get_client()
                        slots = client.get_creneaux(args["date"])
                        result = {"date": args["date"], "creneaux": slots}
                    elif name == "reserver":
                        client = _get_client()
                        client.get_creneaux(args["date"])
                        msg = client.reserver(args["slot_id"], args["date"])
                        result = {"status": "ok", "message": msg}
                    elif name == "surveiller":
                        date_str = args["date"]
                        heure = str(args["heure"])
                        for w in _watches:
                            if w["date"] == date_str and w["heure"] == heure and not w["notified"]:
                                result = {"status": "ok", "message": f"Surveillance déjà active pour {date_str} à {heure}h"}
                                break
                        else:
                            _watches.append({"date": date_str, "heure": heure, "notified": False})
                            result = {"status": "ok", "message": f"Surveillance activée pour {date_str} à {heure}h"}
                    else:
                        result = {"error": f"Outil inconnu: {name}"}
                except Exception as e:
                    result = {"error": str(e)}

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result)
                })

            messages.append({"role": "user", "content": tool_results})

    except Exception as e:
        logger.error(f"Erreur /chat: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
