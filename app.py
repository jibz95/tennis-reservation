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


def _send_ntfy(title: str, message: str, tags: str = "tennis"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        logger.warning("NTFY_TOPIC non defini, notification ignoree")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": tags},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Erreur ntfy: {e}")


def _notify(title: str, message: str, tags: str = "tennis"):
    _send_ntfy(title, message, tags)


def _check_watches():
    """Tâche planifiée : vérifie les créneaux surveillés (toutes les minutes, intervalle par veille)."""
    active = [w for w in _watches if not w["notified"]]
    if not active:
        return

    now = datetime.now()
    due = [w for w in active if (now - w.get("dernier_check", datetime.min)).total_seconds() / 60 >= w.get("intervalle", 5)]
    if not due:
        return

    logger.info(f"Vérification de {len(due)} surveillance(s)...")
    try:
        client = _get_client()
    except Exception as e:
        logger.error(f"Échec login pour surveillance: {e}")
        return

    for watch in due:
        watch["dernier_check"] = now
        date_str = watch["date"]
        heure = watch["heure"]
        try:
            slots = client.get_creneaux(date_str)
            matches = [s for s in slots if s["heure"] == f"{heure}h"]
            if matches:
                chosen = matches[0]
                logger.info(f"Créneau disponible : {chosen['label']} — tentative de réservation automatique...")
                try:
                    # Annuler toute réservation existante avant de réserver
                    try:
                        existing = client.get_reservations(date_str)
                        for res in existing:
                            logger.info(f"Annulation réservation existante {res['idres']} avant veille")
                            client.annuler(res["idres"], res["idpro"], date_str)
                    except Exception as e_annul:
                        logger.warning(f"Impossible d'annuler la réservation existante : {e_annul}")

                    client.reserver(chosen["slot_id"], date_str)
                    logger.info(f"Réservation automatique réussie : {chosen['label']}")
                    _notify(
                        "Tennis - Creneau libere et reserve !",
                        f"{chosen['label']} le {date_str} a {heure}h",
                        tags="bell,tennis",
                    )
                except Exception as e_res:
                    logger.warning(f"Réservation automatique échouée ({e_res}) — notification simple")
                    _notify(
                        "Tennis - Creneau disponible",
                        f"{chosen['label']} le {date_str} a {heure}h (reservation manuelle necessaire)",
                        tags="warning,tennis",
                    )
                watch["notified"] = True
            else:
                logger.info(f"Pas de créneau à {heure}h le {date_str}")
        except Exception as e:
            logger.error(f"Erreur surveillance {date_str} {heure}h: {e}")


# Démarrer le scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(_check_watches, "interval", minutes=1, id="watch_job")
scheduler.start()


_reservations_differees: list[dict] = []


def _check_reservations_differees():
    """Exécute les réservations différées dont le délai est écoulé."""
    now = datetime.now()
    pending = [r for r in _reservations_differees if not r["done"] and now >= r["execute_at"]]
    for r in pending:
        r["done"] = True
        try:
            client = _get_client()
            client.get_creneaux(r["date"])
            if r.get("invitation"):
                client.reserver_invitation(r["slot_id"], r["date"])
                _notify("Tennis - Reservation avec invitation", f"{r['slot_id']} le {r['date']}", tags="ticket,tennis")
            else:
                client.reserver(r["slot_id"], r["date"])
                _notify("Tennis - Reservation confirmee", f"{r['slot_id']} le {r['date']}", tags="white_check_mark,tennis")
            logger.info(f"Réservation différée réussie : {r['slot_id']} le {r['date']}")
        except Exception as e:
            logger.error(f"Réservation différée échouée : {e}")
            _notify("Tennis - Echec reservation differee", str(e), tags="warning,tennis")


scheduler.add_job(_check_reservations_differees, "interval", seconds=5, id="differe_job")


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
        # Grouper par court pour affichage rapide
        par_court = {}
        for s in slots:
            nom = s["court"]
            par_court.setdefault(nom, []).append(s["heure"])
        return jsonify({"date": date_str, "creneaux": slots, "par_court": par_court})
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
        _notify("Tennis - Reservation confirmee", f"{slot_id.replace('_0_', 'h Court ')} le {date_str}", tags="white_check_mark,tennis")
        return jsonify({"status": "ok", "message": message})
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Erreur inattendue: {e}"}), 500


@app.route("/reserver_auto", methods=["POST"])
def reserver_auto():
    """Trouve le premier créneau disponible à l'heure demandée et réserve immédiatement."""
    body = request.get_json(silent=True) or {}
    date_str = body.get("date")
    heure = re.sub(r'\D.*', '', str(body.get("heure", ""))).strip()
    invitation = bool(body.get("invitation", False))
    court_prefere = str(body.get("court", "")).strip()

    if not date_str:
        return jsonify({"error": "Champ 'date' manquant"}), 400
    if not heure.isdigit():
        return jsonify({"error": "Champ 'heure' manquant ou invalide (ex: 14)"}), 400
    if not _validate_date(date_str):
        return jsonify({"error": f"Format de date invalide: '{date_str}'"}), 400

    try:
        client = _get_client()
        slots = client.get_creneaux(date_str)
        matches = [s for s in slots if s["heure"] == f"{heure}h"]
        if not matches:
            autres = sorted({s["heure"] for s in slots})
            suggestion = f" Créneaux disponibles : {', '.join(autres[:5])}" if autres else ""
            return jsonify({"error": f"Aucun créneau disponible le {date_str} à {heure}h.{suggestion}"}), 404

        chosen = next((s for s in matches if court_prefere.lower() in s["label"].lower()), matches[0])

        if invitation:
            client.reserver_invitation(chosen["slot_id"], date_str)
            _notify("Tennis - Reservation avec invitation", f"{chosen['label']} le {date_str}", tags="ticket,tennis")
        else:
            client.reserver(chosen["slot_id"], date_str)
            _notify("Tennis - Reservation confirmee", f"{chosen['label']} le {date_str}", tags="white_check_mark,tennis")

        autres_courts = [s["label"] for s in matches if s["slot_id"] != chosen["slot_id"]]
        msg = f"Réservé : {chosen['label']} le {date_str} à {heure}h."
        if autres_courts:
            msg += f" (autres courts disponibles : {', '.join(autres_courts)})"
        return jsonify({"status": "ok", "message": msg, "slot_id": chosen["slot_id"], "court": chosen["label"]})
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Erreur : {e}"}), 500


@app.route("/changer_reservation_differee", methods=["POST"])
def changer_reservation_differee():
    """Change le court d'une réservation différée en attente."""
    body = request.get_json(silent=True) or {}
    ancien_slot = body.get("ancien_slot_id")
    nouveau_slot = body.get("nouveau_slot_id")
    date_str = body.get("date")

    if not ancien_slot or not nouveau_slot or not date_str:
        return jsonify({"error": "Champs 'ancien_slot_id', 'nouveau_slot_id' et 'date' requis"}), 400

    for r in _reservations_differees:
        if r["slot_id"] == ancien_slot and r["date"] == date_str and not r["done"]:
            r["slot_id"] = nouveau_slot
            r["execute_at"] = datetime.now() + timedelta(seconds=5)
            return jsonify({"status": "ok", "message": f"Réservation mise à jour : {nouveau_slot} le {date_str} — exécution dans 5s"})

    return jsonify({"error": "Aucune réservation différée en attente trouvée"}), 404


@app.route("/reserver_differe", methods=["POST"])
def reserver_differe():
    body = request.get_json(silent=True) or {}
    slot_id = body.get("slot_id")
    date_str = body.get("date")
    invitation = bool(body.get("invitation", False))
    delai = int(body.get("delai", 2))

    if not slot_id:
        return jsonify({"error": "Champ 'slot_id' manquant"}), 400
    if not date_str:
        return jsonify({"error": "Champ 'date' manquant"}), 400
    if not _validate_date(date_str):
        return jsonify({"error": f"Format de date invalide: '{date_str}'"}), 400

    execute_at = datetime.now() + timedelta(seconds=delai)
    _reservations_differees.append({
        "slot_id": slot_id, "date": date_str,
        "invitation": invitation, "done": False, "execute_at": execute_at,
    })
    type_str = "avec invitation" if invitation else "standard"
    return jsonify({"status": "ok", "message": f"Reservation {type_str} programmee dans {delai}s pour {slot_id} le {date_str}"})


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
        _notify("Tennis - Reservation avec invitation", f"{slot_id.replace('_0_', 'h Court ')} le {date_str}", tags="ticket,tennis")
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
        intervalle = int(request.args.get("intervalle", 5))
    else:
        body = request.get_json(silent=True) or {}
        date_str = body.get("date")
        heure = re.sub(r'\D.*', '', str(body.get("heure", ""))).strip()
        intervalle = int(body.get("intervalle", 5))

    if not date_str:
        return jsonify({"error": "Champ 'date' manquant (format: JJ/MM/AAAA)"}), 400
    if not heure.isdigit():
        return jsonify({"error": "Champ 'heure' manquant ou invalide (ex: 14)"}), 400
    if not _validate_date(date_str):
        return jsonify({"error": f"Format de date invalide: '{date_str}'"}), 400
    intervalle = max(1, min(intervalle, 60))

    # Éviter les doublons
    for w in _watches:
        if w["date"] == date_str and w["heure"] == heure and not w["notified"]:
            return jsonify({"status": "ok", "message": f"Surveillance deja active pour {date_str} a {heure}h"})

    _watches.append({"date": date_str, "heure": heure, "notified": False, "intervalle": intervalle, "dernier_check": datetime.min})
    logger.info(f"Surveillance ajoutee: {date_str} a {heure}h (intervalle {intervalle} min)")
    return jsonify({"status": "ok", "message": f"Veille activee : je verifierai toutes les {intervalle} min et reserverai automatiquement le {date_str} a {heure}h des qu'un court se libere"})


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
        _notify("Tennis - Reservation annulee", f"Reservation {idres} du {date_str} annulee", tags="x,tennis")
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
