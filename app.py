import os
import logging
from datetime import datetime, timedelta

import re
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler

from tennis_client import TennisClient

app = Flask(__name__)
CORS(app)
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


def _notify(title: str, message: str):
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


def _send_whatsapp(title: str, message: str):
    phone = os.environ.get("WHATSAPP_PHONE")
    apikey = os.environ.get("WHATSAPP_APIKEY")
    if not phone or not apikey:
        return
    try:
        text = f"{title} — {message}"
        requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "text": text, "apikey": apikey},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Erreur WhatsApp: {e}")


def _notify(title: str, message: str):
    _notify(title, message)
    _send_whatsapp(title, message)


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
                chosen = matches[0]
                logger.info(f"Créneau disponible : {chosen['label']} — tentative de réservation automatique...")
                try:
                    client.reserver(chosen["slot_id"], date_str)
                    logger.info(f"Réservation automatique réussie : {chosen['label']}")
                    _notify(
                        title=f"Tennis - Réservé automatiquement le {date_str} à {heure}h",
                        message=f"Réservation confirmée : {chosen['label']}",
                    )
                except Exception as e_res:
                    logger.warning(f"Réservation automatique échouée ({e_res}) — notification simple")
                    _notify(
                        title=f"Tennis - Créneau dispo le {date_str} à {heure}h",
                        message=f"Disponible : {chosen['label']} (réservation manuelle nécessaire)",
                    )
                watch["notified"] = True
            else:
                logger.info(f"Pas de créneau à {heure}h le {date_str}")
        except Exception as e:
            logger.error(f"Erreur surveillance {date_str} {heure}h: {e}")


# Démarrer le scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(_check_watches, "interval", minutes=5, id="watch_job")
scheduler.start()


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@app.route("/")
def index():
    from flask import render_template
    return render_template("index.html")


@app.route("/health")
def health():
    watches_actives = [w for w in _watches if not w["notified"]]
    return jsonify({"status": "ok", "surveillances_actives": len(watches_actives)})


@app.route("/planning")
def planning():
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "Parametre 'date' manquant (format: JJ/MM/AAAA)"}), 400
    if not _validate_date(date_str):
        return jsonify({"error": f"Format de date invalide: '{date_str}'"}), 400
    try:
        client = _get_client()
        data = client.get_planning(date_str)
        return jsonify(data)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Erreur inattendue: {e}"}), 500


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


@app.route("/reserver_invitation", methods=["GET", "POST"])
def reserver_invitation():
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
        message = client.reserver_invitation(slot_id, date_str)
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
    return jsonify({"status": "ok", "message": f"Surveillance activee : reservation automatique des qu'un court se libere le {date_str} a {heure}h"})


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


@app.route("/reservations")
def reservations():
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "Parametre 'date' manquant (format: JJ/MM/AAAA)"}), 400
    if not _validate_date(date_str):
        return jsonify({"error": f"Format de date invalide: '{date_str}'"}), 400

    try:
        client = _get_client()
        client.get_creneaux(date_str)
        res = client.get_reservations(date_str)
        return jsonify({"date": date_str, "reservations": res})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Erreur inattendue: {e}"}), 500


@app.route("/annuler", methods=["GET", "POST"])
def annuler():
    if request.method == "GET":
        idres = request.args.get("idres")
        idpro = request.args.get("idpro")
        date_str = request.args.get("date")
    else:
        body = request.get_json(silent=True) or {}
        idres = body.get("idres")
        idpro = body.get("idpro")
        date_str = body.get("date")

    if not idres:
        return jsonify({"error": "Champ 'idres' manquant"}), 400
    if not idpro:
        return jsonify({"error": "Champ 'idpro' manquant"}), 400
    if not date_str:
        return jsonify({"error": "Champ 'date' manquant"}), 400
    if not _validate_date(date_str):
        return jsonify({"error": f"Format de date invalide: '{date_str}'"}), 400

    try:
        client = _get_client()
        client.get_creneaux(date_str)
        message = client.annuler(str(idres), str(idpro), date_str)
        return jsonify({"status": "ok", "message": message})
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Erreur inattendue: {e}"}), 500


@app.route("/surveillances")
def list_surveillances():
    actives = [w for w in _watches if not w["notified"]]
    return jsonify({"surveillances": actives})


JOURS_FR = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
    "vendredi": 4, "samedi": 5, "dimanche": 6,
}

def _parse_date(text: str) -> str | None:
    """Extrait une date du texte et retourne JJ/MM/AAAA."""
    text = text.lower()
    today = datetime.now()

    if "après-demain" in text or "apres-demain" in text or "apres demain" in text:
        return (today + timedelta(days=2)).strftime("%d/%m/%Y")
    if "demain" in text:
        return (today + timedelta(days=1)).strftime("%d/%m/%Y")
    if "aujourd" in text:
        return today.strftime("%d/%m/%Y")

    # Jour de la semaine (prochain)
    for jour, idx in JOURS_FR.items():
        if jour in text:
            diff = (idx - today.weekday()) % 7 or 7
            return (today + timedelta(days=diff)).strftime("%d/%m/%Y")

    # Format JJ/MM/AAAA ou JJ/MM
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{4}))?", text)
    if m:
        j, mo, an = m.group(1), m.group(2), m.group(3) or str(today.year)
        return f"{int(j):02d}/{int(mo):02d}/{an}"

    return None


def _parse_heure(text: str) -> int | None:
    """Extrait une heure du texte (entier)."""
    m = re.search(r"\b(\d{1,2})\s*h", text.lower())
    if m:
        return int(m.group(1))
    m = re.search(r"\bà\s+(\d{1,2})\b", text.lower())
    if m:
        return int(m.group(1))
    return None


def _parse_action(text: str) -> str:
    """Détecte l'intention : get_creneaux, reserver, surveiller ou annuler."""
    t = text.lower()
    if any(w in t for w in ["annule", "annuler", "supprime", "supprimer", "efface"]):
        return "annuler"
    if any(w in t for w in ["surveille", "surveiller", "alerte", "notifie", "préviens", "previens", "libère", "libere"]):
        return "surveiller"
    if any(w in t for w in ["réserve", "reserve", "réserver", "reserver", "book", "prend", "prends"]):
        return "reserver"
    return "get_creneaux"


@app.route("/chat")
def chat():
    question = request.args.get("q", "").strip()
    if not question:
        return jsonify({"error": "Parametre 'q' manquant"}), 400

    action = _parse_action(question)
    date_str = _parse_date(question)
    heure = _parse_heure(question)

    if not date_str:
        return jsonify({"reponse": "Je n'ai pas compris la date. Essaie : 'Réserve demain à 14h' ou 'Créneaux jeudi'."})

    try:
        if action == "get_creneaux":
            tennis = _get_client()
            slots = tennis.get_creneaux(date_str)
            if not slots:
                result_text = f"Aucun créneau disponible le {date_str}."
            elif heure:
                filtres = [s for s in slots if s["heure"] == f"{heure}h"]
                if filtres:
                    courts = ", ".join(s["label"] for s in filtres)
                    result_text = f"Disponible le {date_str} à {heure}h : {courts}"
                else:
                    result_text = f"Aucun créneau disponible le {date_str} à {heure}h."
            else:
                lines = [f"Créneaux disponibles le {date_str} :"]
                for s in slots:
                    lines.append(f"- {s['label']}")
                result_text = "\n".join(lines)

        elif action == "reserver":
            tennis = _get_client()
            slots = tennis.get_creneaux(date_str)
            if heure:
                slots = [s for s in slots if s["heure"] == f"{heure}h"]
            if not slots:
                result_text = f"Aucun créneau disponible le {date_str}" + (f" à {heure}h." if heure else ".")
            else:
                chosen = slots[0]
                tennis.reserver(chosen["slot_id"], date_str)
                result_text = f"Réservation confirmée : {chosen['label']} le {date_str}."

        elif action == "annuler":
            tennis = _get_client()
            tennis.get_creneaux(date_str)
            reservations = tennis.get_reservations(date_str)
            if heure:
                reservations = [r for r in reservations if r["heure"] == f"{heure}h"]
            if not reservations:
                result_text = f"Aucune réservation à annuler le {date_str}" + (f" à {heure}h." if heure else ".")
            else:
                chosen = reservations[0]
                tennis.annuler(chosen["idres"], chosen["idpro"], date_str)
                result_text = f"Réservation annulée : {chosen['label']} le {date_str}."

        elif action == "surveiller":
            if not heure:
                result_text = "Précise l'heure à surveiller. Ex : 'Surveille jeudi à 14h'"
            else:
                heure_str = str(heure)
                for w in _watches:
                    if w["date"] == date_str and w["heure"] == heure_str and not w["notified"]:
                        result_text = f"Surveillance déjà active pour le {date_str} à {heure}h."
                        break
                else:
                    _watches.append({"date": date_str, "heure": heure_str, "notified": False})
                    result_text = f"Surveillance activée : je réserverai automatiquement dès qu'un court se libère le {date_str} à {heure}h."

        return jsonify({"reponse": result_text})

    except Exception as e:
        logger.error(f"Erreur /chat: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
