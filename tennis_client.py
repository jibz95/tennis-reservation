import os
import re
import hashlib
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.premier-service.fr/5.11.04/ics.php"
LOGIN_URL = "https://www.premier-service.fr/_start/index.php"
CLUB_ID = "57920393"

COURT_NAMES = {
    "1": "Court 1TB",
    "2": "Court 2TB",
    "3": "Court 3TB",
    "4": "Court 4TB",
    "5": "Court 5TB",
    "6": "Court 6TB",
    "9": "Court 7DUR",
    "8": "Court 8DUR",
}

DAYS_FR = {
    0: "Lundi", 1: "Mardi", 2: "Mercredi", 3: "Jeudi",
    4: "Vendredi", 5: "Samedi", 6: "Dimanche",
}

KNOWN_HIDDEN_FIELDS = {"idact", "idpge", "usermd5", "idgfcmiid", "largeur_ecran",
                       "hauteur_ecran", "pingmax", "pingmin", "userid", "userkey"}


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _date_with_day(date_str: str) -> str:
    """'20/03/2026' -> '20/03/2026 Vendredi'"""
    dt = datetime.strptime(date_str, "%d/%m/%Y")
    return f"{date_str} {DAYS_FR[dt.weekday()]}"


class TennisClient:
    def __init__(self):
        self.login_value = os.environ["TENNIS_LOGIN"]
        self.password = os.environ["TENNIS_PASSWORD"]
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "fr-FR,fr;q=0.9",
        })
        self._idpge_planning = None  # extrait après login depuis le planning

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def login(self):
        # Étape 1 : GET initial (met en place le cookie)
        self.session.get(f"{LOGIN_URL}?club={CLUB_ID}", timeout=10)

        # Étape 2 : POST pour obtenir le vrai formulaire de login
        r2 = self.session.post(BASE_URL, data={"club": CLUB_ID, "idact": "101"}, timeout=10)
        r2.raise_for_status()
        html2 = r2.text

        # Étape 3 : Parser les champs obfusqués et soumettre le login
        soup = BeautifulSoup(html2, "html.parser")

        # Trouver idpge
        idpge_input = soup.find("input", {"name": "idpge"})
        if not idpge_input:
            raise RuntimeError("idpge introuvable dans le formulaire de login")
        idpge = idpge_input["value"]

        # Champ login : input[type=text] visible (pas 'userid')
        login_field = None
        for inp in soup.find_all("input", {"type": "text"}):
            if inp.get("name") not in ("userid",) and inp.get("name"):
                login_field = inp["name"]
                break

        # Champ password : input[type=password] visible (pas 'userkey')
        password_field = None
        for inp in soup.find_all("input", {"type": "password"}):
            if inp.get("name") not in ("userkey",) and inp.get("name"):
                password_field = inp["name"]
                break

        # Champ MD5 : input[type=hidden] sans valeur initiale, hors liste fixe
        md5_field = None
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "")
            value = inp.get("value", "")
            if name not in KNOWN_HIDDEN_FIELDS and value == "":
                md5_field = name
                break

        if not login_field or not md5_field:
            raise RuntimeError(
                f"Champs obfusqués introuvables: login={login_field}, md5={md5_field}"
            )

        md5_value = _md5((self.password + self.login_value).upper())

        payload = {
            "idact": "101",
            "idpge": idpge,
            "usermd5": "",
            md5_field: md5_value,
            "idgfcmiid": "0",
            "largeur_ecran": "1536",
            "hauteur_ecran": "864",
            "pingmax": "401",
            "pingmin": "18",
            "userid": "",
            "userkey": "",
            login_field: self.login_value,
            password_field: "",
        }

        # Normaliser l'action du form si nécessaire
        form = soup.find("form")
        action = BASE_URL
        if form and form.get("action"):
            action = form["action"].replace("/_start/../5.11.04/", "/5.11.04/")
            if not action.startswith("http"):
                action = "https://www.premier-service.fr" + action

        r3 = self.session.post(action, data=payload, timeout=10)
        r3.raise_for_status()
        html3 = r3.text

        if "fiche_identification" in html3 or len(html3) < 5000:
            raise RuntimeError("Échec de connexion — vérifier identifiants")

        # Extraire idpge du planning (pattern 210-xxx) pour les réservations
        match = re.search(r'(210-\w+)', html3)
        if match:
            self._idpge_planning = match.group(1)

    # ------------------------------------------------------------------
    # Créneaux
    # ------------------------------------------------------------------

    def get_creneaux(self, date_str: str) -> list[dict]:
        """date_str: 'JJ/MM/AAAA'"""
        date_with_day = _date_with_day(date_str)
        timestamp = int(time.time() * 1000)

        params = {
            "idact": "328",
            "idses": "S0",
            "CHAMP_SELECTEUR_JOUR": date_with_day,
            "_": str(timestamp),
        }
        r = self.session.get(BASE_URL, params=params, timeout=10)
        r.raise_for_status()
        js = r.text

        # Extraire idpge_planning si pas encore eu
        if not self._idpge_planning:
            match = re.search(r'(210-\w+)', js)
            if match:
                self._idpge_planning = match.group(1)

        # Plages libres : idg_lset("8_0_C","22_0_C",...)
        free_ranges: dict[str, tuple[int, int]] = {}
        for m in re.finditer(r'idg_lset\("(\d+)_0_(\d+)","(\d+)_0_\d+"', js):
            start_h, court, end_h = int(m.group(1)), m.group(2), int(m.group(3))
            free_ranges[court] = (start_h, end_h)

        # Créneaux occupés : idg_pset(Array("H_M_C",...))
        occupied: set[str] = set()
        for m in re.finditer(r'idg_pset\(Array\("(\d+)_(\d+)_(\d+)"', js):
            h, mn, court = m.group(1), m.group(2), m.group(3)
            if mn == "0":
                occupied.add(f"{h}_0_{court}")

        creneaux = []
        for court, (start_h, end_h) in sorted(free_ranges.items(), key=lambda x: int(x[0])):
            for h in range(start_h, end_h):
                slot_id = f"{h}_0_{court}"
                if slot_id not in occupied:
                    court_name = COURT_NAMES.get(court, f"Court {court}")
                    creneaux.append({
                        "slot_id": slot_id,
                        "label": f"{court_name} - {h}h",
                        "heure": f"{h}h",
                        "court": court,
                    })

        return creneaux

    # ------------------------------------------------------------------
    # Réservation
    # ------------------------------------------------------------------

    def reserver(self, slot_id: str, date_str: str) -> str:
        """Réserve le créneau slot_id (ex: '9_0_4') à la date date_str."""
        parts = slot_id.split("_")
        if len(parts) != 3:
            raise ValueError(f"slot_id invalide: {slot_id}")
        heure, _, court = parts[0], parts[1], parts[2]
        date_with_day = _date_with_day(date_str)

        if not self._idpge_planning:
            raise RuntimeError("idpge_planning non disponible — relancer login + get_creneaux")

        # Étape 1 : Ouvrir la fiche (idact=336)
        payload_336 = {
            "idact": "336",
            "idpge": self._idpge_planning,
            "IDOBJ": slot_id,
            "idses": "S0",
            "idcrt": court,
            "pw": "24",
            "dj": "2",
            "CHAMP_SELECTEUR_JEU": "1",
            "ID_TABLEAU": f"1|{CLUB_ID}|1",
            "CHAMP_SELECTEUR_JOUR": date_with_day,
            "nc": "30",
        }
        r1 = self.session.post(BASE_URL, data=payload_336, timeout=10)
        r1.raise_for_status()
        html1 = r1.text

        # Extraire idpge 330-xxx
        match = re.search(r'["\']?(330-\w+)["\']?', html1)
        if not match:
            raise RuntimeError("idpge 330-xxx introuvable dans la fiche de réservation")
        idpge_330 = match.group(1)

        # Étape 2 : Sélectionner le partenaire (idact=332)
        payload_332 = {
            "idact": "332",
            "idpge": idpge_330,
            "IDOBJ": "100",
            "idpar": "100",
            "CHAMP_TYPE_1": "-100",
            "idses": "S0",
            "b_i": "0",
        }
        r2 = self.session.post(BASE_URL, data=payload_332, timeout=10)
        r2.raise_for_status()

        # Étape 3 : Valider (idact=366)
        payload_366 = {
            "idact": "366",
            "idpge": idpge_330,
            "idses": "S0",
            "b_i": "0",
        }
        r3 = self.session.post(BASE_URL, data=payload_366, timeout=10)
        r3.raise_for_status()

        # Vérification basique : pas de message d'erreur dans la réponse
        html3 = r3.text
        if "erreur" in html3.lower() and "fiche_identification" in html3.lower():
            raise RuntimeError("Échec de la réservation — vérifier le créneau")

        return "Reservation confirmee"
