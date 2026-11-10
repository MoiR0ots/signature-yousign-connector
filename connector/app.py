"""Recevoir les notifications ESUP et les enregistrer dans un fichier SQLite.

Pour l'instant, ce programme observe seulement les notifications.
Il consulte ESUP si configuré, sans contacter Yousign ni modifier les parapheurs.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException

from connector.esup import analyser_demande, lire_demande


# CONNECTOR_DB permet de choisir le fichier au lancement du programme.
# Sans cette variable, la base est dans le dossier data du projet.
DOSSIER_PROJET = Path(__file__).resolve().parent.parent
CHEMIN_BASE = Path(os.environ.get("CONNECTOR_DB", DOSSIER_PROJET / "data/events.sqlite3"))

app = FastAPI(title="Connecteur ESUP / Yousign", version="0.1.0")


def initialiser_base():
    """Créer la base et sa table si elles n'existent pas encore."""
    CHEMIN_BASE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not CHEMIN_BASE.exists():
        CHEMIN_BASE.touch(mode=0o600)

    connexion = sqlite3.connect(CHEMIN_BASE)
    try:
        # Un événement est identifié directement par son contenu JSON unique.
        # state servira à suivre son traitement dans la suite du POC.
        connexion.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'received',
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Une table séparée évite de modifier les événements déjà enregistrés.
        connexion.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                event_id INTEGER PRIMARY KEY,
                resultat TEXT NOT NULL,
                observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        connexion.commit()
    finally:
        # finally est exécuté même lorsqu'une erreur interrompt le bloc try.
        connexion.close()


def preparer_evenement(donnees):
    """Extraire les cinq champs envoyés par ESUP dans le JSON du POST."""
    # Ces trois informations sont indispensables pour comprendre la notification.
    for nom_champ in ("signRequestId", "status", "step"):
        valeur = donnees.get(nom_champ)
        if valeur is None or valeur == "":
            raise HTTPException(status_code=400, detail="Champ manquant : " + nom_champ)
        if not isinstance(valeur, (str, int)) or isinstance(valeur, bool):
            raise HTTPException(status_code=400, detail="Champ incorrect : " + nom_champ)

    # Les clés restent en anglais car ce sont les noms envoyés par ESUP.
    # Convertir les numéros en texte donne le même format à chaque réception.
    evenement = {
        "signRequestId": str(donnees["signRequestId"]),
        "status": str(donnees["status"]),
        "step": str(donnees["step"]),
        "userEppn": donnees.get("userEppn", ""),
        "comment": donnees.get("comment", ""),
    }
    return evenement


def enregistrer_evenement(evenement):
    """Enregistrer un événement nouveau ou retrouver celui qui existe déjà."""
    # Pour ce POC, vérifier la table à chaque réception est suffisant.
    # CREATE TABLE IF NOT EXISTS ne supprime pas les données déjà présentes.
    initialiser_base()

    # Trier les clés et retirer les espaces donne un texte JSON toujours identique.
    # SQLite peut ainsi comparer directement les contenus des notifications.
    texte_evenement = json.dumps(evenement, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    try:
        # Réserver l'écriture pendant la recherche et l'enregistrement.
        # Cela évite que deux POST simultanés créent chacun le même événement.
        connexion.execute("BEGIN IMMEDIATE")
        curseur = connexion.execute(
            "SELECT id FROM events WHERE payload = ?",
            (texte_evenement,),
        )
        ligne = curseur.fetchone()

        if ligne is None:
            curseur = connexion.execute(
                "INSERT INTO events (payload) VALUES (?)",
                (texte_evenement,),
            )
            identifiant_evenement = curseur.lastrowid
            doublon = False
        else:
            # Un doublon conserve la date et l'état du premier enregistrement.
            identifiant_evenement = ligne[0]
            doublon = True

        connexion.commit()
    except sqlite3.Error:
        # Si une écriture échoue, annuler toute la réception.
        connexion.rollback()
        raise
    finally:
        connexion.close()

    # ESUP attend HTTP 200. FastAPI utilise ce code lorsqu'on retourne un
    # dictionnaire normalement. Cette réponse arrive après le commit SQLite.
    return {
        "accepted": True,
        "event_id": identifiant_evenement,
        "duplicate": doublon,
    }


def consulter_evenement(identifiant_evenement):
    """Consulter ou reconsulter un événement enregistré, sans action de signature."""
    initialiser_base()
    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    try:
        ligne = connexion.execute(
            "SELECT payload FROM events WHERE id = ?", (identifiant_evenement,)
        ).fetchone()
    finally:
        connexion.close()
    if ligne is None:
        raise ValueError("Événement introuvable")
    evenement = json.loads(ligne[0])
    try:
        if evenement["status"] != "pending":
            resultat = {"etat": "ignoree", "motif": "Notification autre que pending"}
        else:
            demande = lire_demande(evenement["signRequestId"])
            resultat = analyser_demande(demande, evenement)
    except Exception as erreur:
        # Cette frontière protège la réception des erreurs réseau ou d'un format
        # ESUP inattendu. Le type d'erreur est conservé sans exposer la réponse API.
        resultat = {"etat": "erreur", "motif": type(erreur).__name__}

    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    try:
        connexion.execute(
            "INSERT OR REPLACE INTO observations (event_id, resultat) VALUES (?, ?)",
            (identifiant_evenement, json.dumps(resultat, ensure_ascii=False)),
        )
        connexion.execute(
            "UPDATE events SET state = ? WHERE id = ?",
            (resultat["etat"], identifiant_evenement),
        )
        connexion.commit()
    finally:
        connexion.close()
    # Les noms, adresses et placements restent dans SQLite, pas dans le journal.
    logging.getLogger("uvicorn.error").info(
        "Événement %s, demande %s : %s",
        identifiant_evenement, evenement["signRequestId"], resultat["etat"],
    )
    return resultat


@app.get("/")
def afficher_etat():
    """Page simple permettant de vérifier que le connecteur répond."""
    return {"status": "ok", "mode": "observe", "version": "0.1.0"}


@app.post("/esup/events")
def recevoir_evenement_post(donnees: dict, taches: BackgroundTasks):
    """Recevoir une notification ESUP dans le corps JSON du POST."""
    # FastAPI lit directement le JSON et le fournit comme dictionnaire Python.
    evenement = preparer_evenement(donnees)
    try:
        reponse = enregistrer_evenement(evenement)
        # Répondre à ESUP avant de le consulter : son envoi de notification peut
        # encore être en cours. Une relecture manuelle reste possible ensuite.
        if os.environ.get("ESUP_URL"):
            taches.add_task(consulter_evenement, reponse["event_id"])
        return reponse
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="Impossible d'enregistrer la notification")


# Reprise manuelle : python -m connector.app IDENTIFIANT_EVENEMENT
# On relit l'état actuel d'ESUP ; aucune ancienne observation n'est rejouée.
if __name__ == "__main__":
    import argparse

    arguments = argparse.ArgumentParser(description="Reconsulter un événement dans ESUP")
    arguments.add_argument("evenement", type=int)
    options = arguments.parse_args()
    resultat = consulter_evenement(options.evenement)
    print(json.dumps(resultat, ensure_ascii=False, indent=2))
    if resultat["etat"] == "erreur":
        raise SystemExit(1)
