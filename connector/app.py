"""Recevoir les notifications ESUP et les enregistrer dans un fichier SQLite.

Pour l'instant, ce programme observe seulement les notifications.
Il ne contacte ni ESUP ni Yousign et ne modifie aucun parapheur.
"""

import json
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException


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


@app.get("/")
def afficher_etat():
    """Page simple permettant de vérifier que le connecteur répond."""
    return {"status": "ok", "mode": "observe", "version": "0.1.0"}


@app.post("/esup/events")
def recevoir_evenement_post(donnees: dict):
    """Recevoir une notification ESUP dans le corps JSON du POST."""
    # FastAPI lit directement le JSON et le fournit comme dictionnaire Python.
    evenement = preparer_evenement(donnees)
    try:
        return enregistrer_evenement(evenement)
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="Impossible d'enregistrer la notification")
