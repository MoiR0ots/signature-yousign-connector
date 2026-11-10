"""Lire ESUP sans modifier les demandes ni télécharger les documents."""

import json
import os
from urllib.parse import quote
from urllib.request import Request, urlopen


def lire_demande(identifiant):
    """Consulter l'API ESUP avec la bibliothèque standard Python."""
    adresse = os.environ.get("ESUP_URL", "").rstrip("/")
    if not adresse:
        raise ValueError("ESUP_URL n'est pas configurée")
    url = adresse + "/ws/signrequests/" + quote(str(identifiant), safe="")
    requete = Request(url, headers={"Accept": "application/json"})
    # Un délai évite de laisser une consultation attendre indéfiniment.
    with urlopen(requete, timeout=10) as reponse:
        return json.load(reponse)


def analyser_demande(demande, evenement):
    """Comparer les étapes réelles, puis extraire uniquement la dernière."""
    if str(demande["id"]) != evenement["signRequestId"]:
        raise ValueError("La réponse ESUP ne correspond pas à la demande")
    if demande["status"] != "pending":
        return {"etat": "ignoree", "motif": "La demande n'est plus en attente"}

    circuit = demande["parentSignBook"]["liveWorkflow"]
    etapes = circuit["liveWorkflowSteps"]
    courante = circuit["currentStep"]
    if not etapes or not courante or courante.get("id") is None:
        raise ValueError("Le circuit ne contient pas d'étape courante exploitable")

    # Le numéro de notification n'est pas l'identifiant technique de l'étape.
    # Relire la liste permet de tenir compte des vérificateurs ajoutés au circuit.
    numero_courant = None
    for numero, etape in enumerate(etapes, start=1):
        if etape["id"] == courante["id"]:
            numero_courant = numero
            break
    if numero_courant is None:
        raise ValueError("L'étape courante est absente du circuit")
    if str(numero_courant) != evenement["step"]:
        return {"etat": "ignoree", "motif": "La notification concerne une autre étape"}
    if courante["id"] != etapes[-1]["id"]:
        return {"etat": "ignoree", "motif": "Étape intermédiaire"}

    signataires = []
    for destinataire in courante["recipients"]:
        utilisateur = destinataire["user"]
        emplacements = []
        for parametres in courante["signRequestParams"]:
            if parametres.get("recipientId") == destinataire["id"]:
                # Conserver les coordonnées ESUP brutes : leur conversion pour
                # Yousign sera traitée lors de la préparation du document.
                emplacements.append({
                    "page": parametres.get("signPageNumber"),
                    "document": parametres.get("signDocumentNumber"),
                    "x": parametres.get("xPos"),
                    "y": parametres.get("yPos"),
                    "largeur": parametres.get("signWidth"),
                    "hauteur": parametres.get("signHeight"),
                    "rotation": parametres.get("rotate"),
                    "toutes_les_pages": parametres.get("allPages"),
                    "champ_pdf": parametres.get("pdSignatureFieldName"),
                })
        signataires.append({
            "id_destinataire": destinataire["id"],
            "prenom": utilisateur.get("firstname"),
            "nom": utilisateur.get("name"),
            "email": utilisateur.get("email"),
            "type": utilisateur.get("userType"),
            "deja_signe": destinataire["signed"],
            "emplacements": emplacements,
        })

    # Identifier la dernière étape ne signifie pas encore qu'elle est prête
    # pour Yousign (identités, types de signature et placements restent à valider).
    return {
        "etat": "derniere_etape",
        "id_etape": courante["id"],
        "numero_etape": numero_courant,
        "toutes_signatures_requises": courante["allSignToComplete"],
        "type_signature": courante.get("signType"),
        "signataires": signataires,
    }
