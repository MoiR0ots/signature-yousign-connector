"""Stockage SQLite : notifications, observations et suivi des demandes Yousign.

Ce fichier ne fait aucun appel HTTP et n'importe aucun des clients API.
L'appelant exécute initialiser_base() au début de son opération ; les autres
fonctions utilisent ensuite les tables sans relancer leur création.
"""

import json
import os
import sqlite3
from pathlib import Path

DOSSIER_PROJET = Path(__file__).resolve().parent.parent
CHEMIN_BASE = Path(os.environ.get("CONNECTOR_DB", DOSSIER_PROJET / "data/events.sqlite3"))


def initialiser_base():
    """Créer les tables au début d’une réception, d’un cycle ou d’une commande."""
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

def enregistrer_evenement(evenement):
    """Enregistrer un événement nouveau ou retrouver celui qui existe déjà."""
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

    # L'appelant peut acquitter la réception après le commit SQLite.
    return {
        "accepted": True,
        "event_id": identifiant_evenement,
        "duplicate": doublon,
    }

def lire_evenement(identifiant):
    """Lire une notification déjà enregistrée."""
    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    try:
        ligne = connexion.execute("SELECT payload FROM events WHERE id=?", (identifiant,)).fetchone()
        if ligne is None:
            raise ValueError("Événement introuvable")
        return json.loads(ligne[0])
    finally:
        connexion.close()

def enregistrer_observation(identifiant, resultat, etat):
    """Conserver ensemble l'observation et le nouvel état de l'événement."""
    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    try:
        connexion.execute("INSERT OR REPLACE INTO observations (event_id,resultat) VALUES (?,?)",
                          (identifiant, json.dumps(resultat, ensure_ascii=False)))
        connexion.execute("UPDATE events SET state=? WHERE id=?", (etat, identifiant))
        connexion.commit()
    finally:
        connexion.close()
