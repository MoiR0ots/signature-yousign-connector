"""Orchestration métier : sélectionner les étapes, envoyer, suivre et reprendre.

Ce fichier décrit le parcours. Le SQL reste dans donnees.py et les appels HTTP
restent dans esup.py et yousign.py.
"""

import json
import logging
import os
import time
from pathlib import Path
from connector import donnees, esup

journal = logging.getLogger(__name__)


def analyser_demande(demande, evenement):
    """Comparer les étapes réelles, puis extraire uniquement la dernière."""
    if str(demande["id"]) != evenement["signRequestId"]:
        raise ValueError("La réponse ESUP ne correspond pas à la demande")
    if demande["status"] in ("completed", "exported", "refused", "deleted", "archived", "cleaned"):
        return {"etat": "ignoree", "motif": "La demande est terminée ou refusée"}
    if demande["status"] != "pending":
        # ESUP peut notifier avant que sa transaction soit visible par l'API.
        return {"etat": "a_reprendre", "motif": "L'API n'annonce pas encore pending"}

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
    numero_annonce = int(evenement["step"])
    if numero_courant < numero_annonce:
        return {"etat": "a_reprendre", "motif": "L'API annonce encore une étape antérieure"}
    if numero_courant > numero_annonce:
        return {"etat": "ignoree", "motif": "La notification concerne une autre étape"}
    if courante["id"] != etapes[-1]["id"]:
        return {"etat": "ignoree", "motif": "Étape intermédiaire"}

    signataires = []
    for destinataire in courante["recipients"]:
        utilisateur = destinataire["user"]
        emplacements = []
        for parametres in courante["signRequestParams"]:
            identifiant_destinataire = parametres.get("recipientId")
            if identifiant_destinataire is None and parametres.get("recipient"):
                identifiant_destinataire = parametres["recipient"].get("id")
            # ESUP autorise un emplacement générique pour une étape à un seul
            # destinataire. Dans ce cas, aucun recipientId n'est renseigné.
            emplacement_unique = (
                identifiant_destinataire is None and len(courante["recipients"]) == 1
            )
            if identifiant_destinataire == destinataire["id"] or emplacement_unique:
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
                    "echelle": parametres.get("signScale", 1),
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

def consulter_evenement(identifiant_evenement):
    """Consulter ou reconsulter un événement enregistré, sans action de signature."""
    evenement = donnees.lire_evenement(identifiant_evenement)
    try:
        if evenement["status"] != "pending":
            resultat = {"etat": "ignoree", "motif": "Notification autre que pending"}
        else:
            # Trois lectures au maximum : immédiatement, puis après 5 secondes
            # entre chaque tentative. Aucune connexion SQLite ne reste ouverte.
            for tentative in range(1, 4):
                demande = esup.lire_demande(evenement["signRequestId"])
                resultat = analyser_demande(demande, evenement)
                if resultat["etat"] != "a_reprendre":
                    break
                if tentative < 3:
                    time.sleep(5)
            # Si ESUP reste en retard, le traitement périodique reprendra
            # a_reprendre lorsqu'il est activé ; sinon la reprise reste manuelle.
            # Une demande dépassée ou terminée n'est pas réessayée.
            if resultat["etat"] == "derniere_etape":
                # Les documents suivent le dossier de la base : aucun chemin
                # supplémentaire à configurer dans le playbook.
                resultat["document_pdf"] = esup.telecharger_document(
                    evenement["signRequestId"], resultat["id_etape"],
                    donnees.CHEMIN_BASE.parent / "documents",
                )
    except Exception as erreur:
        # Cette frontière protège la réception des erreurs réseau ou d'un format
        # ESUP inattendu. Le type d'erreur est conservé sans exposer la réponse API.
        resultat = {"etat": "erreur", "motif": type(erreur).__name__}
        if isinstance(erreur, ValueError):
            resultat["detail"] = str(erreur)

    etat_evenement = resultat["etat"]
    donnees.enregistrer_observation(identifiant_evenement, resultat, etat_evenement)
    # Les noms, adresses et placements restent dans SQLite, pas dans le journal.
    journal.info(
        "Événement %s, demande %s : %s",
        identifiant_evenement, evenement["signRequestId"], resultat["etat"],
    )
    return resultat

if __name__ == "__main__":
    import argparse

    arguments = argparse.ArgumentParser(description="Consulter un événement ESUP enregistré")
    arguments.add_argument("evenement", type=int)
    options = arguments.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    donnees.initialiser_base()
    resultat = consulter_evenement(options.evenement)
    print(json.dumps(resultat, ensure_ascii=False, indent=2))
