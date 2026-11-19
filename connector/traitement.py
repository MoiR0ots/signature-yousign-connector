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


def verifier_etape(observation):
    if observation["type_signature"] != "signature":
        raise ValueError("La dernière étape ESUP doit être de type signature")
    if not observation["toutes_signatures_requises"]:
        raise ValueError("Activer l’option imposant la signature de tous les destinataires dans ESUP")


def consulter_evenement(identifiant_evenement):
    """Consulter ou reconsulter un événement enregistré, sans action de signature."""
    evenement = donnees.lire_evenement(identifiant_evenement)
    resultat = {}
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
                verifier_etape(resultat)
                yousign.preparer_signataires(resultat, nombre_documents=None)
                resultat["document_pdf"] = esup.telecharger_document(
                    evenement["signRequestId"], resultat["id_etape"],
                    donnees.CHEMIN_BASE.parent / "documents",
                )
    except Exception as erreur:
        # Cette frontière protège la réception des erreurs réseau ou d'un format
        # ESUP inattendu. Le type d'erreur est conservé sans exposer la réponse API.
        resultat.update(etat="erreur", motif=type(erreur).__name__)
        if isinstance(erreur, ValueError):
            resultat["detail"] = str(erreur)
        journal.warning("Événement %s : %s", identifiant_evenement, resultat.get("detail", resultat["motif"]))

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
    verifier_etape(observation)
    contexte_annonce = esup.lire_contexte_retour(demande)
    parapheur = str(contexte_annonce["signBookId"])
    demandes = contexte_annonce["demandesDuParapheur"]
    if (not isinstance(demandes, list) or any(type(item) is not int or item <= 0 for item in demandes)
            or int(demande) not in demandes or len(set(demandes)) != len(demandes)):
        raise ValueError("Liste de documents ESUP incohérente")
    signataires = yousign.preparer_signataires(observation, len(demandes))

    contextes = {}
    pdfs = {}
    for identifiant in map(str, demandes):
        contexte = contexte_annonce if identifiant == demande else esup.lire_contexte_retour(identifiant)
        if (str(contexte["signRequestId"]) != identifiant or str(contexte["expectedStepId"]) != etape
                or str(contexte["signBookId"]) != parapheur or contexte["demandesDuParapheur"] != demandes):
            raise ValueError("Le parapheur ESUP a changé avant l'envoi")
        chemin = observation["document_pdf"] if identifiant == demande else esup.telecharger_document(
            identifiant, etape, donnees.CHEMIN_BASE.parent / "documents",
        )
        pdf = Path(chemin)
        if contexte["sourceSha256"] != hashlib.sha256(pdf.read_bytes()).hexdigest():
            raise ValueError(f"Le PDF ESUP a changé avant l'envoi (demande {identifiant})")
        contextes[identifiant] = contexte.copy()
        pdfs[identifiant] = pdf

    # Relire les signataires entre deux lectures du contexte avant tout POST.
    observation_actuelle = analyser_demande(esup.lire_demande(demande), notification)
    if observation_actuelle["etat"] != "derniere_etape":
        raise ValueError("La demande ESUP n'est plus disponible à sa dernière étape")
    verifier_etape(observation_actuelle)
    if yousign.preparer_signataires(observation_actuelle, len(demandes)) != signataires:
        raise ValueError("Les signataires ou leurs emplacements ont changé avant l'envoi")
    for identifiant, contexte in contextes.items():
        if esup.lire_contexte_retour(identifiant) != contexte:
            raise ValueError("Le contexte ESUP a changé pendant la préparation")

    for contexte in contextes.values():
        # La liste et le parapheur sont déjà représentés par les lignes documents.
        contexte.pop("demandesDuParapheur")
        contexte.pop("signBookId")
    suivi = donnees.reserver_brouillon(parapheur, etape, contextes)
    if suivi["pret"]:
        return suivi
    if not suivi["demande_id"]:
        reponse = yousign.creer_demande(parapheur, etape)
        suivi["demande_id"] = reponse["id"]
        donnees.sauvegarder_brouillon(parapheur, etape, suivi)
    documents = suivi["documents"]
    for identifiant, document in documents.items():
        if not document["document_id"]:
            suivi["en_cours"] = "document"
            donnees.sauvegarder_brouillon(parapheur, etape, suivi)
            reponse = yousign.ajouter_document(suivi["demande_id"], pdfs[identifiant],
                                               identifiant)
            document["document_id"] = reponse["id"]
            donnees.sauvegarder_brouillon(parapheur, etape, suivi)
    ids_documents = [document["document_id"] for document in documents.values()]
    for personne in signataires:
        cle = str(personne["id_esup"])
        if cle in suivi["signataires"]:
            continue
        suivi["en_cours"] = "signataire"
        suivi["id_destinataire_en_cours"] = cle
        donnees.sauvegarder_brouillon(parapheur, etape, suivi)
        reponse = yousign.ajouter_signataire(suivi["demande_id"],
                                               ids_documents,
                                               personne)
        suivi["signataires"][cle] = reponse["id"]
        donnees.sauvegarder_brouillon(parapheur, etape, suivi)
    if yousign.lire_demande(suivi["demande_id"])["status"] != "draft":
        raise ValueError("La demande Yousign n'est pas en brouillon")
    suivi["pret"] = True
    suivi["en_cours"] = ""
    suivi.pop("id_destinataire_en_cours", None)
    donnees.sauvegarder_brouillon(parapheur, etape, suivi)
    return suivi


def actualiser_demande(parapheur, etape, suivi):
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
            donnees.sauvegarder_brouillon(parapheur, etape, suivi)
            raise
    suivi["statut_yousign"] = reponse["status"]
    suivi["active_le"] = reponse.get("activated_at")
    if reponse["status"] != "draft":
        suivi.pop("activation_bloquee", None)
        suivi.pop("erreur", None)
    donnees.sauvegarder_brouillon(parapheur, etape, suivi)

    if reponse["status"] == "ongoing":
        liens = suivi.setdefault("liens_signature", {})
        statuts = suivi.setdefault("statuts_signataires", {})
        # Le GET de suivi peut déjà contenir les statuts individuels.
        par_id = {personne["id"]: personne for personne in reponse.get("signers", [])}
        for destinataire, identifiant in suivi["signataires"].items():
            personne = par_id.get(identifiant, {})
            statut = personne.get("status") or statuts.get(destinataire)
            if statut in ("signed", "declined", "aborted", "error"):
                liens.pop(destinataire, None)
            elif not liens.get(destinataire):
                personne = yousign.lire_signataire(suivi["demande_id"], identifiant)
                statut = personne.get("status", statut)
                liens[destinataire] = personne.get("signature_link")
            if statut:
                statuts[destinataire] = statut
        donnees.sauvegarder_brouillon(parapheur, etape, suivi)
    elif reponse["status"] == "done":
        suivi["termine_le"] = reponse.get("completed_at")
        reintegrer_document_signe(parapheur, etape, suivi)
    return suivi


def reintegrer_document_signe(parapheur, etape, suivi):
    """Conserver le PDF final, l'envoyer à ESUP et mémoriser sa confirmation."""
    suivi.pop("liens_signature", None)
    documents = suivi["documents"]
    for identifiant, document in documents.items():
        destination = donnees.CHEMIN_BASE.parent / "documents" / identifiant / etape / "document-yousign.pdf"
        if not document.get("document_signe") or not destination.exists():
            yousign.telecharger_pdf_signe(suivi["demande_id"], document["document_id"], destination)
            document["document_signe"] = str(destination)
        donnees.sauvegarder_brouillon(parapheur, etape, suivi)
        if document.get("retour_esup"):
            continue
        contexte = document["contexte_esup"]
        if str(contexte["signRequestId"]) != identifiant or str(contexte["expectedStepId"]) != etape:
            raise ValueError("Le contexte initial ne correspond pas à la demande ESUP")
        # La référence est stable et l'extension vérifie les doublons par demande ESUP.
        confirmation = esup.envoyer_document_signe(
            identifiant, contexte, "yousign:" + suivi["demande_id"], document["document_signe"],
        )
        if (str(confirmation.get("signRequestId")) != identifiant
                or confirmation.get("status") != "completed" or not confirmation.get("documentId")):
            raise ValueError("La confirmation de l'import ESUP est inattendue")
        document["retour_esup"] = confirmation
        donnees.sauvegarder_brouillon(parapheur, etape, suivi)
        journal.info("Demande ESUP %s : PDF final importé, dernière étape validée", identifiant)


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
    for (identifiant,) in donnees.evenements_a_traiter():
        observation = consulter_evenement(identifiant)
        if observation["etat"] != "derniere_etape":
            continue
        demande = donnees.lire_evenement(identifiant)["signRequestId"]
        try:
            commun = donnees.brouillon_du_document(demande, str(observation["id_etape"]))
            if commun is None:
                preparer_brouillon(identifiant, observation)
            elif not commun[0]:
                raise ValueError("Préparation du parapheur en cours ou résultat incertain : vérifier Yousign et SQLite")
            journal.info("Événement %s : brouillon prêt", identifiant)
        except Exception as erreur:
            # Pas de POST répété aveuglément : le suivi garde la dernière phase
            # et bloque une création incertaine pour vérification manuelle.
            observation["detail"] = str(erreur)
            donnees.enregistrer_observation(identifiant, observation, "erreur_yousign")
            # Après réservation, le suivi reste visible même si une nouvelle
            # notification arrive pour la même demande.
            donnees.noter_erreur_preparation(demande, str(observation["id_etape"]), str(erreur))
            journal.error("Événement %s : %s", identifiant, erreur)
            continue
        donnees.changer_etat_evenement(identifiant, "transmis_yousign")


def suivre_demandes():
    """Suivre les demandes prêtes et réintégrer les PDF terminés dans ESUP."""
    for parapheur, etape, suivi in donnees.lister_brouillons():
        if not suivi["pret"] or suivi["en_cours"]:
            continue
        documents = suivi["documents"].values()
        if suivi.get("statut_yousign") == "done" and all(
            doc.get("retour_esup") and doc.get("document_signe") and Path(doc["document_signe"]).exists()
            for doc in documents
        ):
            continue
        if suivi.get("statut_yousign") in ("canceled", "expired", "declined", "deleted", "rejected"):
            continue
        try:
            ancien_statut = suivi.get("statut_yousign")
            actualiser_demande(parapheur, etape, suivi)
            if suivi.get("statut_yousign") != ancien_statut:
                journal.info("Parapheur ESUP %s : Yousign %s", parapheur, suivi.get("statut_yousign"))
        except Exception as erreur:
            if not suivi.get("activation_bloquee"):
                suivi["erreur"] = str(erreur)
                donnees.sauvegarder_brouillon(parapheur, etape, suivi)
            journal.error("Parapheur ESUP %s : %s", parapheur, erreur)


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
