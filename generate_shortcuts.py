"""
Génère les fichiers .shortcut importables sur iPhone.
Utilise uniquement des requêtes GET pour éviter les problèmes de corps JSON.
"""
import plistlib
import uuid
import os

BASE_URL = "https://tennis-reservation-ct8h.onrender.com"

def uid():
    return str(uuid.uuid4()).upper()

def var_ref(name, var_uuid):
    """Référence directe à la sortie d'une action précédente."""
    return {
        "Value": {
            "OutputName": name,
            "OutputUUID": var_uuid,
            "Type": "ActionOutput"
        },
        "WFSerializationType": "WFTextTokenAttachment"
    }

def text_with_vars(parts):
    """
    Construit un WFTextTokenString à partir d'une liste de parties :
    - str : texte statique
    - (name, uuid) : référence à une variable
    """
    string = ""
    attachments = {}
    for part in parts:
        if isinstance(part, str):
            string += part
        else:
            name, var_uuid = part
            pos = len(string)
            string += "\ufffc"
            attachments[f"{{{pos}, 1}}"] = {
                "OutputName": name,
                "OutputUUID": var_uuid,
                "Type": "ActionOutput"
            }
    return {
        "Value": {"attachmentsByRange": attachments, "string": string},
        "WFSerializationType": "WFTextTokenString"
    }

def action(identifier, params):
    return {
        "WFWorkflowActionIdentifier": identifier,
        "WFWorkflowActionParameters": params,
    }

def make_shortcut(name, actions_list):
    return {
        "WFWorkflowActions": actions_list,
        "WFWorkflowClientRelease": "2.0",
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowIcon": {
            "WFWorkflowIconGlyphNumber": 59511,
            "WFWorkflowIconStartColor": 431817727,
        },
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowName": name,
        "WFWorkflowTypes": ["NCWidget", "WatchKit"],
        "WFWorkflowOutputContentItemClasses": [],
    }


# ──────────────────────────────────────────────
# RACCOURCI 1 : Réserve un court
# ──────────────────────────────────────────────

ask_date_uuid  = uid()
get_cren_uuid  = uid()
cren_list_uuid = uid()
choose_uuid    = uid()
slot_id_uuid   = uid()
get_res_uuid   = uid()
msg_uuid       = uid()

shortcut_reserver = make_shortcut("Reserve un court", [

    # 1. Demander la date
    action("is.workflow.actions.ask", {
        "WFAskActionPrompt": "Pour quelle date ? (JJ/MM/AAAA)",
        "WFInputType": "Text",
        "CustomOutputName": "Date",
        "UUID": ask_date_uuid,
    }),

    # 2. GET /creneaux?date=...
    action("is.workflow.actions.getcontentsofurl", {
        "WFURL": text_with_vars([
            f"{BASE_URL}/creneaux?date=",
            ("Date", ask_date_uuid)
        ]),
        "WFHTTPMethod": "GET",
        "CustomOutputName": "Reponse creneaux",
        "UUID": get_cren_uuid,
    }),

    # 3. Extraire le tableau "creneaux"
    action("is.workflow.actions.getdictionaryvalue", {
        "WFInput": var_ref("Reponse creneaux", get_cren_uuid),
        "WFDictionaryKey": "creneaux",
        "CustomOutputName": "Liste creneaux",
        "UUID": cren_list_uuid,
    }),

    # 4. Choisir dans la liste
    action("is.workflow.actions.choosefromlist", {
        "WFChooseFromListActionList": var_ref("Liste creneaux", cren_list_uuid),
        "CustomOutputName": "Creneau choisi",
        "UUID": choose_uuid,
    }),

    # 5. Extraire slot_id
    action("is.workflow.actions.getdictionaryvalue", {
        "WFInput": var_ref("Creneau choisi", choose_uuid),
        "WFDictionaryKey": "slot_id",
        "CustomOutputName": "slot_id",
        "UUID": slot_id_uuid,
    }),

    # 6. GET /reserver?slot_id=...&date=...
    action("is.workflow.actions.getcontentsofurl", {
        "WFURL": text_with_vars([
            f"{BASE_URL}/reserver?slot_id=",
            ("slot_id", slot_id_uuid),
            "&date=",
            ("Date", ask_date_uuid)
        ]),
        "WFHTTPMethod": "GET",
        "CustomOutputName": "Reponse reservation",
        "UUID": get_res_uuid,
    }),

    # 7. Extraire le message
    action("is.workflow.actions.getdictionaryvalue", {
        "WFInput": var_ref("Reponse reservation", get_res_uuid),
        "WFDictionaryKey": "message",
        "CustomOutputName": "Message",
        "UUID": msg_uuid,
    }),

    # 8. Afficher le résultat
    action("is.workflow.actions.showresult", {
        "Text": var_ref("Message", msg_uuid),
    }),
])


# ──────────────────────────────────────────────
# RACCOURCI 2 : Surveille un créneau
# ──────────────────────────────────────────────

ask_date2_uuid   = uid()
ask_heure_uuid   = uid()
get_surv_uuid    = uid()
msg2_uuid        = uid()

shortcut_surveiller = make_shortcut("Surveille un creneau", [

    # 1. Demander la date
    action("is.workflow.actions.ask", {
        "WFAskActionPrompt": "Pour quelle date ? (JJ/MM/AAAA)",
        "WFInputType": "Text",
        "CustomOutputName": "Date",
        "UUID": ask_date2_uuid,
    }),

    # 2. Demander l'heure
    action("is.workflow.actions.ask", {
        "WFAskActionPrompt": "A quelle heure ? (ex: 14)",
        "WFInputType": "Number",
        "CustomOutputName": "Heure",
        "UUID": ask_heure_uuid,
    }),

    # 3. GET /surveiller?date=...&heure=...
    action("is.workflow.actions.getcontentsofurl", {
        "WFURL": text_with_vars([
            f"{BASE_URL}/surveiller?date=",
            ("Date", ask_date2_uuid),
            "&heure=",
            ("Heure", ask_heure_uuid)
        ]),
        "WFHTTPMethod": "GET",
        "CustomOutputName": "Reponse surveillance",
        "UUID": get_surv_uuid,
    }),

    # 4. Extraire le message
    action("is.workflow.actions.getdictionaryvalue", {
        "WFInput": var_ref("Reponse surveillance", get_surv_uuid),
        "WFDictionaryKey": "message",
        "CustomOutputName": "Message",
        "UUID": msg2_uuid,
    }),

    # 5. Afficher le résultat
    action("is.workflow.actions.showresult", {
        "Text": var_ref("Message", msg2_uuid),
    }),
])


# ──────────────────────────────────────────────
# Écriture des fichiers
# ──────────────────────────────────────────────

output_dir = os.path.dirname(os.path.abspath(__file__))

for name, data in [
    ("Reserve_un_court.shortcut", shortcut_reserver),
    ("Surveille_un_creneau.shortcut", shortcut_surveiller),
]:
    path = os.path.join(output_dir, name)
    with open(path, "wb") as f:
        plistlib.dump(data, f, fmt=plistlib.FMT_XML)
    print(f"Cree : {path}")

print("\nTransfere ces fichiers sur ton iPhone.")
