import os
import sqlite3
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

DB_PATH = os.path.join(os.path.dirname(__file__), "tennis.db")


def _get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            heure TEXT NOT NULL,
            intervalle INTEGER DEFAULT 60,
            notified INTEGER DEFAULT 0,
            dernier_check TEXT DEFAULT '0001-01-01T00:00:00'
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS reservations_differees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id TEXT NOT NULL,
            date TEXT NOT NULL,
            invitation INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0,
            execute_at TEXT NOT NULL
        )""")


_init_db()


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
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": tags,
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Erreur ntfy: {e}")


def _notify(title: str, message: str, tags: str = "tennis"):
    _send_ntfy(title, message, tags)


def _check_watches():
    """Tâche planifiée : vérifie les créneaux surveillés (toutes les minutes, intervalle par veille)."""
    with _get_db() as conn:
        active = conn.execute("SELECT * FROM watches WHERE notified=0").fetchall()
    if not active:
        return

    now = datetime.now()
    due = [w for w in active if (now - datetime.fromisoformat(w["dernier_check"])).total_seconds() >= w["intervalle"]]
    if not due:
        return

    logger.info(f"Vérification de {len(due)} surveillance(s)...")
    try:
        client = _get_client()
    except Exception as e:
        logger.error(f"Échec login pour surveillance: {e}")
        return

    for watch in due:
        date_str = watch["date"]
        heure = watch["heure"]
        with _get_db() as conn:
            conn.execute("UPDATE watches SET dernier_check=? WHERE id=?", (now.isoformat(), watch["id"]))
        try:
            slots = client.get_creneaux(date_str)
            matches = [s for s in slots if s["heure"] == f"{heure}h"]
            if matches:
                chosen = matches[0]
                logger.info(f"Créneau disponible : {chosen['label']} — tentative de réservation automatique...")
                try:
                    try:
                        existing = client.get_reservations(date_str)
                        for res in existing:
                            logger.info(f"Annulation réservation existante {res['idres']} avant veille")
                            client.annuler(res["idres"], res["idpro"], date_str)
                    except Exception as e_annul:
                        logger.warning(f"Impossible d'annuler la réservation existante : {e_annul}")

                    client.reserver(chosen["slot_id"], date_str)
                    logger.info(f"Réservation automatique réussie : {chosen['label']}")
                    _notify("Tennis - Creneau libere et reserve !", f"{chosen['label']} le {date_str} a {heure}h", tags="bell,tennis")
                except Exception as e_res:
                    logger.warning(f"Réservation automatique échouée ({e_res}) — notification simple")
                    _notify("Tennis - Creneau disponible", f"{chosen['label']} le {date_str} a {heure}h (reservation manuelle necessaire)", tags="warning,tennis")
                with _get_db() as conn:
                    conn.execute("UPDATE watches SET notified=1 WHERE id=?", (watch["id"],))
            else:
                logger.info(f"Pas de créneau à {heure}h le {date_str}")
        except Exception as e:
            logger.error(f"Erreur surveillance {date_str} {heure}h: {e}")


# Démarrer le scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(_check_watches, "interval", seconds=30, id="watch_job")
scheduler.start()


def _check_reservations_differees():
    """Exécute les réservations différées dont le délai est écoulé."""
    now = datetime.now()
    with _get_db() as conn:
        pending = conn.execute(
            "SELECT * FROM reservations_differees WHERE done=0 AND execute_at<=?",
            (now.isoformat(),)
        ).fetchall()
    for r in pending:
        with _get_db() as conn:
            conn.execute("UPDATE reservations_differees SET done=1 WHERE id=?", (r["id"],))
        try:
            client = _get_client()
            client.get_creneaux(r["date"])
            if r["invitation"]:
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

@app.route("/health")
def health():
    with _get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM watches WHERE notified=0").fetchone()[0]
    return jsonify({"status": "ok", "surveillances_actives": count})


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

    execute_at = (datetime.now() + timedelta(seconds=5)).isoformat()
    with _get_db() as conn:
        cur = conn.execute(
            "UPDATE reservations_differees SET slot_id=?, execute_at=? WHERE slot_id=? AND date=? AND done=0",
            (nouveau_slot, execute_at, ancien_slot, date_str)
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Aucune réservation différée en attente trouvée"}), 404
    return jsonify({"status": "ok", "message": f"Réservation mise à jour : {nouveau_slot} le {date_str} — exécution dans 5s"})


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

    execute_at = (datetime.now() + timedelta(seconds=delai)).isoformat()
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO reservations_differees (slot_id, date, invitation, done, execute_at) VALUES (?,?,?,0,?)",
            (slot_id, date_str, int(invitation), execute_at)
        )
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
        intervalle = int(request.args.get("intervalle", 60))
    else:
        body = request.get_json(silent=True) or {}
        date_str = body.get("date")
        heure = re.sub(r'\D.*', '', str(body.get("heure", ""))).strip()
        intervalle = int(body.get("intervalle", 60))

    if not date_str:
        return jsonify({"error": "Champ 'date' manquant (format: JJ/MM/AAAA)"}), 400
    if not heure.isdigit():
        return jsonify({"error": "Champ 'heure' manquant ou invalide (ex: 14)"}), 400
    if not _validate_date(date_str):
        return jsonify({"error": f"Format de date invalide: '{date_str}'"}), 400
    intervalle = max(30, min(intervalle, 3600))

    with _get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM watches WHERE date=? AND heure=? AND notified=0",
            (date_str, heure)
        ).fetchone()
        if existing:
            return jsonify({"status": "ok", "message": f"Surveillance deja active pour {date_str} a {heure}h"})
        conn.execute(
            "INSERT INTO watches (date, heure, intervalle, notified, dernier_check) VALUES (?,?,?,0,'0001-01-01T00:00:00')",
            (date_str, heure, intervalle)
        )
    logger.info(f"Surveillance ajoutee: {date_str} a {heure}h (intervalle {intervalle} min)")
    try:
        dt = datetime.strptime(date_str, "%d/%m/%Y")
        jours_fr = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]
        jour_nom = jours_fr[dt.weekday()]
        notif_msg = f"{jour_nom} {date_str} de {heure}h a {int(heure)+1}h — verification toutes les {intervalle}s"
    except Exception:
        notif_msg = f"{date_str} a {heure}h — verification toutes les {intervalle}s"
    _notify("Tennis - Veille activee 👀", notif_msg, tags="eyes,tennis")
    return jsonify({"status": "ok", "message": f"Veille activee : je verifierai toutes les {intervalle}s et reserverai automatiquement le {date_str} a {heure}h des qu'un court se libere"})


@app.route("/surveiller", methods=["DELETE"])
def annuler_surveillance():
    body = request.get_json(silent=True) or {}
    date_str = body.get("date")
    heure = str(body.get("heure", "")).replace("h", "")

    with _get_db() as conn:
        cur = conn.execute(
            "UPDATE watches SET notified=1 WHERE date=? AND heure=? AND notified=0",
            (date_str, heure)
        )
        removed = cur.rowcount
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
    with _get_db() as conn:
        rows = conn.execute("SELECT * FROM watches WHERE notified=0").fetchall()
    return jsonify({"surveillances": [dict(r) for r in rows]})


@app.route("/declencher_veille")
def declencher_veille():
    """Déclenche manuellement la vérification des veilles (debug)."""
    with _get_db() as conn:
        avant = conn.execute("SELECT COUNT(*) FROM watches WHERE notified=0").fetchone()[0]
    _check_watches()
    with _get_db() as conn:
        apres = conn.execute("SELECT COUNT(*) FROM watches WHERE notified=0").fetchone()[0]
        watches = [dict(r) for r in conn.execute("SELECT * FROM watches").fetchall()]
    return jsonify({"veilles_avant": avant, "veilles_apres": apres, "watches": watches})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
