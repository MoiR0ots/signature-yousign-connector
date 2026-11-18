"""Orchestration métier : sélectionner les étapes, envoyer, suivre et reprendre.

Ce fichier décrit le parcours. Le SQL reste dans donnees.py et les appels HTTP
restent dans esup.py et yousign.py.
"""

import fcntl
import hashlib
import json
import logging
import time
from pathlib import Path
from connector import donnees, esup, yousign

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
            # les événements a_reprendre au prochain passage.
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

    # Une dernière étape validée est toujours à envoyer au prochain traitement.
    if resultat["etat"] == "derniere_etape":
        etat_evenement = "a_envoyer"
    else:
        etat_evenement = resultat["etat"]
    donnees.enregistrer_observation(identifiant_evenement, resultat, etat_evenement)
    # Les noms, adresses et placements restent dans SQLite, pas dans le journal.
    journal.info(
        "Événement %s, demande %s : %s",
        identifiant_evenement, evenement["signRequestId"], resultat["etat"],
    )
    return resultat


def preparer_brouillon(identifiant_evenement, observation):
    """Organiser la préparation, en sauvegardant chaque identifiant obtenu."""
    if observation["etat"] != "derniere_etape":
        raise ValueError("La demande n'est pas disponible à sa dernière étape")
    notification = donnees.lire_evenement(identifiant_evenement)
    demande = notification["signRequestId"]
    etape = str(observation["id_etape"])
    if observation["type_signature"] != "signature" or not observation["toutes_signatures_requises"]:
        raise ValueError("Le POC nécessite une étape de signature avec tous les signataires requis")
    signataires = yousign.preparer_signataires(observation)
    pdf = Path(observation["document_pdf"])
    # Ce contexte sera présenté tel quel au retour. Il doit correspondre au PDF
    # et aux signataires que nous allons réellement envoyer, avant tout POST.
    contexte = esup.lire_contexte_retour(demande)
    if str(contexte["signRequestId"]) != demande or str(contexte["expectedStepId"]) != etape:
        raise ValueError("L'étape ESUP a changé avant l'envoi")
    if contexte["sourceSha256"] != hashlib.sha256(pdf.read_bytes()).hexdigest():
        raise ValueError("Le PDF ESUP a changé avant l'envoi")
    # Une modification a pu survenir depuis l'observation. Relire les signataires
    # entre deux lectures du contexte évite de mémoriser un état plus récent
    # tout en envoyant les anciens placements ou destinataires.
    observation_actuelle = analyser_demande(esup.lire_demande(demande), notification)
    if observation_actuelle["etat"] != "derniere_etape":
        raise ValueError("La demande ESUP n'est plus disponible à sa dernière étape")
    if (observation_actuelle["type_signature"] != "signature"
            or not observation_actuelle["toutes_signatures_requises"]):
        raise ValueError("Les règles de signature ESUP ont changé avant l'envoi")
    if yousign.preparer_signataires(observation_actuelle) != signataires:
        raise ValueError("Les signataires ou leurs emplacements ont changé avant l'envoi")
    if esup.lire_contexte_retour(demande) != contexte:
        raise ValueError("Le contexte ESUP a changé pendant la préparation")
    # Le contexte ESUP couvre déjà le PDF, les destinataires et les placements.
    suivi = donnees.reserver_brouillon(demande, etape, contexte)
    if suivi["pret"]:
        return suivi
    if not suivi["demande_id"]:
        reponse = yousign.creer_demande(demande, etape)
        suivi["demande_id"] = reponse["id"]
        donnees.sauvegarder_brouillon(demande, etape, suivi)
    if not suivi["document_id"]:
        suivi["en_cours"] = "document"
        donnees.sauvegarder_brouillon(demande, etape, suivi)
        reponse = yousign.ajouter_document(suivi["demande_id"], pdf)
        suivi["document_id"] = reponse["id"]
        donnees.sauvegarder_brouillon(demande, etape, suivi)
    for personne in signataires:
        cle = str(personne["id_esup"])
        if cle in suivi["signataires"]:
            continue
        suivi["en_cours"] = "signataire_" + cle
        donnees.sauvegarder_brouillon(demande, etape, suivi)
        reponse = yousign.ajouter_signataire(suivi["demande_id"], suivi["document_id"], personne)
        suivi["signataires"][cle] = reponse["id"]
        donnees.sauvegarder_brouillon(demande, etape, suivi)
    if yousign.lire_demande(suivi["demande_id"])["status"] != "draft":
        raise ValueError("La demande Yousign n'est pas en brouillon")
    suivi["pret"] = True
    suivi["en_cours"] = ""
    donnees.sauvegarder_brouillon(demande, etape, suivi)
    return suivi


def actualiser_demande(demande, etape, suivi):
    """Activer un brouillon prêt, puis réintégrer son PDF signé dans ESUP."""
    reponse = yousign.lire_demande(suivi["demande_id"])
    # Une activation déjà faite n'est jamais répétée, même après redémarrage.
    if reponse["status"] == "draft":
        if suivi.get("activation_bloquee"):
            return suivi
        try:
            # La demande a été créée avec livraison par email. Son activation
            # déclenche les invitations ; une demande ongoing ne repasse pas ici.
            reponse = yousign.activer_demande(suivi["demande_id"])
        except Exception as erreur:
            # Un refus Sandbox ou un résultat incertain doit être vérifié avant
            # une nouvelle activation. Le cycle suivant relira tout de même GET.
            suivi["activation_bloquee"] = True
            suivi["erreur"] = str(erreur)
            donnees.sauvegarder_brouillon(demande, etape, suivi)
            raise
    suivi["statut_yousign"] = reponse["status"]
    suivi["active_le"] = reponse.get("activated_at")
    if reponse["status"] != "draft":
        suivi.pop("activation_bloquee", None)
        suivi.pop("erreur", None)
    donnees.sauvegarder_brouillon(demande, etape, suivi)

    if reponse["status"] == "ongoing" and not suivi.get("liens_signature"):
        # Les invitations sont envoyées par Yousign. Les liens restent aussi
        # privés dans SQLite pour le diagnostic en cas de mail non reçu.
        liens = {}
        for destinataire, identifiant in suivi["signataires"].items():
            personne = yousign.lire_signataire(suivi["demande_id"], identifiant)
            liens[destinataire] = personne.get("signature_link")
        suivi["liens_signature"] = liens
        donnees.sauvegarder_brouillon(demande, etape, suivi)
    elif reponse["status"] == "done":
        suivi["termine_le"] = reponse.get("completed_at")
        reintegrer_document_signe(demande, etape, suivi)
    return suivi


def reintegrer_document_signe(demande, etape, suivi):
    """Conserver le PDF final, l'envoyer à ESUP et mémoriser sa confirmation."""
    suivi.pop("liens_signature", None)
    destination = donnees.CHEMIN_BASE.parent / "documents" / demande / etape / "document-yousign.pdf"
    if not suivi.get("document_signe") or not destination.exists():
        yousign.telecharger_pdf_signe(suivi["demande_id"], suivi["document_id"], destination)
        suivi["document_signe"] = str(destination)
    # Enregistrer le fichier avant l'import pour le réutiliser en cas de reprise.
    donnees.sauvegarder_brouillon(demande, etape, suivi)
    if not suivi.get("retour_esup"):
        contexte = suivi["contexte_esup"]
        if str(contexte["signRequestId"]) != demande or str(contexte["expectedStepId"]) != etape:
            raise ValueError("Le contexte initial ne correspond pas à la demande ESUP")
        # Le PDF et son chemin sont déjà validés et sauvegardés ci-dessus.
        # Si la réponse se perd après l'import, le prochain cycle renvoie la même
        # référence : ESUP reconnaît l'import et ne crée pas une seconde version.
        confirmation = esup.envoyer_document_signe(
            demande, contexte, "yousign:" + suivi["demande_id"], suivi["document_signe"],
        )
        if (str(confirmation.get("signRequestId")) != demande
                or confirmation.get("status") != "completed" or not confirmation.get("documentId")):
            raise ValueError("La confirmation de l'import ESUP est inattendue")
        suivi["retour_esup"] = confirmation
        donnees.sauvegarder_brouillon(demande, etape, suivi)
        journal.info("Demande ESUP %s : PDF final importé, dernière étape validée", demande)


def executer_cycle():
    """Préparer les nouvelles demandes puis suivre les signatures déjà envoyées."""
    yousign.verifier_configuration()
    # Une seule initialisation pour l'ensemble des demandes de ce passage.
    donnees.initialiser_base()
    donnees.verifier_environnement(yousign.ADRESSE_API)
    # Le timer ne superpose pas ses exécutions. Ce verrou protège aussi contre
    # un lancement manuel simultané de cette même commande.
    verrou = open(donnees.CHEMIN_BASE.parent / "traitement.lock", "a")
    try:
        try:
            fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            journal.info("Un traitement est déjà en cours")
            return
        traiter_evenements()
        suivre_demandes()
    finally:
        verrou.close()


def traiter_evenements():
    """Relire les notifications en attente, sans rejouer les anciennes étapes."""
    lignes = donnees.lister_evenements()
    # Une demande peut avoir plusieurs notifications : seule la dernière reçue
    # sert de point de départ. L'API ESUP reste la source de son état courant.
    derniers = {}
    for identifiant, texte, etat in lignes:
        evenement = json.loads(texte)
        derniers[evenement["signRequestId"]] = (identifiant, etat)
    for identifiant, etat in derniers.values():
        if etat not in ("received", "a_reprendre", "erreur", "a_envoyer"):
            continue
        try:
            observation = consulter_evenement(identifiant)
            if observation["etat"] != "derniere_etape":
                continue
            preparer_brouillon(identifiant, observation)
            nouvel_etat = "transmis_yousign"
            journal.info("Événement %s : brouillon prêt", identifiant)
        except Exception as erreur:
            # Pas de POST répété aveuglément : le suivi garde la dernière phase
            # et bloque une création incertaine pour vérification manuelle.
            nouvel_etat = "erreur_yousign"
            journal.error("Événement %s : %s", identifiant, erreur)
        donnees.changer_etat_evenement(identifiant, nouvel_etat)


def suivre_demandes():
    """Suivre les demandes prêtes et réintégrer les PDF terminés dans ESUP."""
    for demande, etape, texte in donnees.lister_brouillons():
        suivi = json.loads(texte)
        if not suivi["pret"] or suivi["en_cours"]:
            continue
        if suivi.get("statut_yousign") == "done" and suivi.get("retour_esup") and suivi.get("document_signe"):
            if Path(suivi["document_signe"]).exists():
                continue
        if suivi.get("statut_yousign") in ("canceled", "expired", "declined", "deleted", "rejected"):
            continue
        try:
            ancien_statut = suivi.get("statut_yousign")
            actualiser_demande(demande, etape, suivi)
            if suivi.get("statut_yousign") != ancien_statut:
                journal.info("Demande ESUP %s : Yousign %s", demande, suivi.get("statut_yousign"))
        except Exception as erreur:
            journal.error("Demande ESUP %s : %s", demande, erreur)


if __name__ == "__main__":
    import argparse

    arguments = argparse.ArgumentParser(description="Traiter les demandes ESUP / Yousign")
    arguments.add_argument("action", choices=["cycle", "consulter"], nargs="?", default="cycle")
    arguments.add_argument("evenement", type=int, nargs="?")
    options = arguments.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if options.action == "cycle":
        executer_cycle()
    else:
        if options.evenement is None:
            arguments.error("Préciser l'identifiant de l'événement")
        # Les fonctions de traitement utilisent ensuite les tables existantes.
        donnees.initialiser_base()
        resultat = consulter_evenement(options.evenement)
        print(json.dumps(resultat, ensure_ascii=False, indent=2))
