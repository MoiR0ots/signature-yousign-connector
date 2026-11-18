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
        connexion.execute("""
            CREATE TABLE IF NOT EXISTS brouillons_yousign (
                demande TEXT NOT NULL, etape TEXT NOT NULL, suivi TEXT NOT NULL,
                PRIMARY KEY (demande, etape)
            )
        """)
        connexion.execute("""
            CREATE TABLE IF NOT EXISTS configuration_yousign (
                id INTEGER PRIMARY KEY, api_url TEXT NOT NULL
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


def lister_evenements():
    """Lire les événements dans leur ordre de réception, sans décision métier."""
    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    try:
        return connexion.execute("SELECT id, payload, state FROM events ORDER BY id").fetchall()
    finally:
        connexion.close()


def changer_etat_evenement(identifiant, etat):
    """Enregistrer la décision prise par le traitement."""
    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    try:
        connexion.execute("UPDATE events SET state=? WHERE id=?", (etat, identifiant))
        connexion.commit()
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


def verifier_environnement(adresse_api):
    """Lier la base à une API pour ne pas mélanger Sandbox et production."""
    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    try:
        connexion.execute("BEGIN IMMEDIATE")
        ligne = connexion.execute("SELECT api_url FROM configuration_yousign WHERE id=1").fetchone()
        if ligne is None:
            adresse = adresse_api
            connexion.execute("INSERT INTO configuration_yousign VALUES (1,?)", (adresse,))
        else:
            adresse = ligne[0]
        connexion.commit()
        if adresse != adresse_api:
            raise ValueError("Cette base utilise une autre API Yousign : choisir un nouveau dossier de données")
    finally:
        connexion.close()


def reserver_brouillon(demande, etape, empreinte, contexte):
    """Réserver atomiquement une préparation et empêcher un deuxième POST incertain."""
    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    try:
        connexion.execute("BEGIN IMMEDIATE")
        ligne = connexion.execute("SELECT suivi FROM brouillons_yousign WHERE demande=? AND etape=?",
                                  (demande, etape)).fetchone()
        if ligne is None:
            suivi = {"empreinte": empreinte, "en_cours": "", "demande_id": None,
                     "document_id": None, "signataires": {}, "pret": False,
                     "contexte_esup": contexte}
            connexion.execute("INSERT INTO brouillons_yousign VALUES (?,?,?)",
                              (demande, etape, json.dumps(suivi)))
        else:
            suivi = json.loads(ligne[0])
        if suivi["en_cours"]:
            raise ValueError("Préparation en cours ou résultat incertain : vérifier Yousign et SQLite avant reprise")
        if suivi["empreinte"] != empreinte:
            raise ValueError("Le PDF ou les signataires ont changé : vérifier le brouillon existant")
        # Ne jamais remplacer le contexte initial par l'état courant d'ESUP.
        if suivi["contexte_esup"] != contexte:
            raise ValueError("Le contexte ESUP a changé : vérifier la demande")
        if not suivi["pret"]:
            suivi["en_cours"] = "creation" if not suivi["demande_id"] else "reprise"
            connexion.execute("UPDATE brouillons_yousign SET suivi=? WHERE demande=? AND etape=?",
                              (json.dumps(suivi), demande, etape))
        connexion.commit()
        return suivi
    finally:
        connexion.close()


def sauvegarder_brouillon(demande, etape, suivi):
    """Valider le suivi après chaque étape de préparation ou de signature."""
    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    try:
        connexion.execute("UPDATE brouillons_yousign SET suivi=? WHERE demande=? AND etape=?",
                          (json.dumps(suivi, ensure_ascii=False), demande, etape))
        connexion.commit()
    finally:
        connexion.close()


def lister_brouillons():
    """Lire les correspondances ESUP / Yousign conservées sur disque."""
    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    try:
        return connexion.execute("SELECT demande, etape, suivi FROM brouillons_yousign").fetchall()
    finally:
        connexion.close()
