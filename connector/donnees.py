"""Stockage relationnel du connecteur. Le schéma se trouve dans schema.sql."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DOSSIER_PROJET = Path(__file__).resolve().parent.parent
CHEMIN_BASE = Path(os.environ.get("CONNECTOR_DB", DOSSIER_PROJET / "data/events.sqlite3"))
SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


@contextmanager
def ouvrir_base():
    """Valider ensemble les écritures ou les annuler si le traitement échoue."""
    connexion = sqlite3.connect(CHEMIN_BASE, timeout=10)
    connexion.execute("PRAGMA foreign_keys=ON")
    try:
        with connexion:
            yield connexion
    finally:
        connexion.close()


def initialiser_base():
    CHEMIN_BASE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not CHEMIN_BASE.exists():
        CHEMIN_BASE.touch(mode=0o600)
    with ouvrir_base() as connexion:
        connexion.executescript("BEGIN;\n" + SCHEMA + "\nCOMMIT;")


def enregistrer_evenement(evenement):
    valeurs = (evenement["signRequestId"], evenement["status"], evenement["step"],
               evenement.get("userEppn", ""), evenement.get("comment", ""))
    with ouvrir_base() as connexion:
        ajout = connexion.execute("""
            INSERT INTO events (demande, statut_esup, numero_etape, acteur, commentaire)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
        """, valeurs)
        identifiant = ajout.lastrowid
        doublon = ajout.rowcount == 0
        if doublon:
            identifiant, = connexion.execute("""
                SELECT id FROM events WHERE demande=? AND statut_esup=? AND numero_etape=?
                    AND acteur=? AND commentaire=?
            """, valeurs).fetchone()
    return {"accepted": True, "event_id": identifiant, "duplicate": doublon}


def lire_evenement(identifiant):
    with ouvrir_base() as connexion:
        connexion.row_factory = sqlite3.Row
        ligne = connexion.execute("""
            SELECT demande AS signRequestId, statut_esup AS status, numero_etape AS step,
                   acteur AS userEppn, commentaire AS comment FROM events WHERE id=?
        """, (identifiant,)).fetchone()
        if ligne is None:
            raise ValueError("Événement introuvable")
        return dict(ligne)


def evenements_a_traiter():
    with ouvrir_base() as connexion:
        return connexion.execute("""
            SELECT id FROM events WHERE id IN (SELECT max(id) FROM events GROUP BY demande)
            AND (state IN ('received', 'a_reprendre', 'a_envoyer') OR id IN (
                SELECT evenement FROM dossiers_a_verifier WHERE prochain_essai_utc <= CURRENT_TIMESTAMP
            )) ORDER BY id
        """).fetchall()


def changer_etat_evenement(identifiant, etat):
    with ouvrir_base() as connexion:
        connexion.execute("UPDATE events SET state=? WHERE id=?", (etat, identifiant))


def enregistrer_observation(identifiant, resultat, etat):
    with ouvrir_base() as connexion:
        # Le remplacement supprime aussi les anciennes zones et les anciens destinataires.
        connexion.execute("DELETE FROM observations WHERE event_id=?", (identifiant,))
        connexion.execute("""
            INSERT INTO observations (event_id, etat, motif, detail, id_etape, numero_etape,
                                      type_signature, toutes_signatures_requises, document_pdf)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (identifiant, resultat["etat"], resultat.get("motif"), resultat.get("detail"),
              resultat.get("id_etape"), resultat.get("numero_etape"), resultat.get("type_signature"),
              resultat.get("toutes_signatures_requises"), resultat.get("document_pdf")))
        for personne in resultat.get("signataires", []):
            connexion.execute("""
                INSERT INTO destinataires_observes (event_id, id_destinataire, prenom, nom, email, type, deja_signe)
                VALUES (:event_id, :id_destinataire, :prenom, :nom, :email, :type, :deja_signe)
            """, dict(personne, event_id=identifiant))
            for numero, zone in enumerate(personne["emplacements"], start=1):
                connexion.execute("""
                    INSERT INTO emplacements (event_id, id_destinataire, numero, document, page, x, y,
                                              largeur, hauteur, rotation, echelle, toutes_les_pages, champ_pdf)
                    VALUES (:event_id, :destinataire, :numero, :document, :page, :x, :y,
                            :largeur, :hauteur, :rotation, :echelle, :toutes_les_pages, :champ_pdf)
                """, dict(zone, event_id=identifiant, destinataire=personne["id_destinataire"],
                          numero=numero, echelle=zone.get("echelle", 1)))
        connexion.execute("UPDATE events SET state=? WHERE id=?", (etat, identifiant))


def verifier_environnement(adresse_api):
    with ouvrir_base() as connexion:
        connexion.execute("INSERT INTO configuration_yousign VALUES (1, ?) ON CONFLICT DO NOTHING", (adresse_api,))
        adresse, = connexion.execute("SELECT api_url FROM configuration_yousign WHERE id=1").fetchone()
        if adresse != adresse_api:
            raise ValueError("Cette base utilise une autre API Yousign : choisir un nouveau dossier de données")


def lire_suivi(connexion, ligne):
    """Rassembler la ligne du brouillon et ses signataires pour le cycle en cours."""
    suivi = {
        "en_cours": ligne["en_cours"], "pret": bool(ligne["pret"]),
        "demande_id": ligne["id_demande_yousign"], "document_id": ligne["id_document_yousign"],
        "contexte_esup": {
            "signRequestId": int(ligne["demande"]), "expectedStepId": int(ligne["etape"]),
            "contextHash": ligne["empreinte_contexte"], "sourceDocumentId": ligne["id_document_source"],
            "sourceSha256": ligne["sha256_source"],
        },
        "signataires": {},
    }
    for champ in ("id_destinataire_en_cours", "erreur", "statut_yousign", "active_le", "termine_le", "document_signe"):
        if ligne[champ] is not None:
            suivi[champ] = ligne[champ]
    if ligne["activation_bloquee"]:
        suivi["activation_bloquee"] = True
    if ligne["id_document_retour"] is not None:
        suivi["retour_esup"] = {
            "signRequestId": int(ligne["demande"]), "documentId": ligne["id_document_retour"],
            "status": ligne["statut_demande_retour"], "signBookStatus": ligne["statut_parapheur_retour"],
            "duplicate": bool(ligne["retour_doublon"]),
        }
    for personne in connexion.execute("SELECT * FROM signataires WHERE demande=? AND etape=?",
                                      (ligne["demande"], ligne["etape"])):
        cle = personne["id_destinataire_esup"]
        suivi["signataires"][cle] = personne["id_signataire_yousign"]
        if personne["statut_yousign"] is not None:
            suivi.setdefault("statuts_signataires", {})[cle] = personne["statut_yousign"]
        if personne["lien_signature"] is not None:
            suivi.setdefault("liens_signature", {})[cle] = personne["lien_signature"]
    return suivi


def reserver_brouillon(demande, etape, contexte):
    """Réserver le premier POST et conserver le contexte initial en une transaction."""
    with ouvrir_base() as connexion:
        connexion.row_factory = sqlite3.Row
        connexion.execute("BEGIN IMMEDIATE")
        connexion.execute("""
            INSERT INTO brouillons_yousign (demande, etape, empreinte_contexte, id_document_source,
                                            sha256_source, en_cours)
            VALUES (?, ?, ?, ?, ?, '') ON CONFLICT (demande, etape) DO NOTHING
        """, (demande, etape, contexte["contextHash"], contexte["sourceDocumentId"], contexte["sourceSha256"]))
        ligne = connexion.execute("SELECT * FROM brouillons_yousign WHERE demande=? AND etape=?",
                                  (demande, etape)).fetchone()
        suivi = lire_suivi(connexion, ligne)
        if suivi["en_cours"]:
            raise ValueError("Préparation en cours ou résultat incertain : vérifier Yousign et SQLite avant reprise")
        if suivi["contexte_esup"] != contexte:
            raise ValueError("Le contexte ESUP a changé : vérifier la demande")
        if not suivi["pret"]:
            suivi["en_cours"] = "reprise" if suivi["demande_id"] else "creation"
            connexion.execute("UPDATE brouillons_yousign SET en_cours=? WHERE demande=? AND etape=?",
                              (suivi["en_cours"], demande, etape))
        return suivi


def sauvegarder_brouillon(demande, etape, suivi):
    retour = suivi.get("retour_esup", {})
    with ouvrir_base() as connexion:
        connexion.execute("""
            UPDATE brouillons_yousign SET id_demande_yousign=?, id_document_yousign=?, en_cours=?,
                id_destinataire_en_cours=?, pret=?, activation_bloquee=?, erreur=?, statut_yousign=?, active_le=?, termine_le=?,
                document_signe=?, id_document_retour=?, statut_demande_retour=?, statut_parapheur_retour=?,
                retour_doublon=? WHERE demande=? AND etape=?
        """, (suivi.get("demande_id"), suivi.get("document_id"), suivi["en_cours"],
              suivi.get("id_destinataire_en_cours"), suivi["pret"],
              suivi.get("activation_bloquee", False), suivi.get("erreur"), suivi.get("statut_yousign"),
              suivi.get("active_le"), suivi.get("termine_le"), suivi.get("document_signe"),
              retour.get("documentId"), retour.get("status"), retour.get("signBookStatus"),
              retour.get("duplicate"), demande, etape))
        for destinataire, identifiant in suivi["signataires"].items():
            connexion.execute("""
                INSERT INTO signataires (demande, etape, id_destinataire_esup, id_signataire_yousign,
                                         statut_yousign, lien_signature)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (demande, etape, id_destinataire_esup)
                DO UPDATE SET statut_yousign=excluded.statut_yousign, lien_signature=excluded.lien_signature
            """, (demande, etape, destinataire, identifiant,
                  suivi.get("statuts_signataires", {}).get(destinataire),
                  suivi.get("liens_signature", {}).get(destinataire)))


def lister_brouillons():
    with ouvrir_base() as connexion:
        connexion.row_factory = sqlite3.Row
        connexion.execute("BEGIN")
        return [(ligne["demande"], ligne["etape"], lire_suivi(connexion, ligne))
                for ligne in connexion.execute("SELECT * FROM brouillons_yousign")]


def noter_erreur_preparation(demande, etape, motif):
    with ouvrir_base() as connexion:
        connexion.execute("""
            UPDATE brouillons_yousign SET erreur=? WHERE demande=? AND etape=? AND en_cours != ''
        """, (motif, demande, etape))
