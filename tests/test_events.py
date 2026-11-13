"""Quelques vérifications simples, avec une base temporaire et aucun appel à ESUP."""

import sqlite3

from fastapi.testclient import TestClient

from connector import app as connecteur
from connector import donnees


def test_post_repete_represente_le_meme_evenement(tmp_path, monkeypatch):
    """Deux POST identiques conservent un seul événement ; le GET est refusé."""
    chemin_base = tmp_path / "test.sqlite3"
    # Remplacer le chemin uniquement pendant ce test, sans toucher à la vraie base.
    monkeypatch.setattr(donnees, "CHEMIN_BASE", chemin_base)
    monkeypatch.delenv("ESUP_URL", raising=False)
    client = TestClient(connecteur.app)
    notification = {"signRequestId": "34", "status": "pending", "step": "2"}

    # Le GET envoyé par ESUP ne doit ni confirmer ni enregistrer la notification.
    reponse_get = client.get("/esup/events", params=notification)
    assert reponse_get.status_code == 405
    assert not chemin_base.exists()
    assert client.get("/").status_code == 200

    premiere_reponse = client.post("/esup/events", json=notification)
    deuxieme_reponse = client.post("/esup/events", json=notification)
    assert premiere_reponse.status_code == 200
    assert deuxieme_reponse.status_code == 200

    premier_resultat = premiere_reponse.json()
    deuxieme_resultat = deuxieme_reponse.json()
    assert premier_resultat["duplicate"] is False
    assert deuxieme_resultat["duplicate"] is True
    assert premier_resultat["event_id"] == deuxieme_resultat["event_id"]

    # Lire le fichier SQLite pour vérifier que les données sont bien conservées.
    connexion = sqlite3.connect(chemin_base)
    try:
        nombre_evenements = connexion.execute("SELECT count(*) FROM events").fetchone()[0]
        evenement_stocke = connexion.execute("SELECT state, received_at FROM events").fetchone()
    finally:
        connexion.close()
    assert nombre_evenements == 1
    assert evenement_stocke[0] == "received"
    assert evenement_stocke[1] is not None

    # Une nouvelle connexion au stockage retrouve le doublon déjà sur disque.
    evenement = connecteur.preparer_evenement(notification)
    resultat = donnees.enregistrer_evenement(evenement)
    assert resultat["duplicate"] is True


def test_changement_etape_cree_un_autre_evenement(tmp_path, monkeypatch):
    """Passer de l'étape 1 à l'étape 2 donne deux événements distincts."""
    monkeypatch.setattr(donnees, "CHEMIN_BASE", tmp_path / "test.sqlite3")
    monkeypatch.delenv("ESUP_URL", raising=False)
    client = TestClient(connecteur.app)

    reponse_etape_1 = client.post(
        "/esup/events",
        json={"signRequestId": "34", "status": "pending", "step": "1"},
    )
    reponse_etape_2 = client.post(
        "/esup/events",
        json={"signRequestId": "34", "status": "pending", "step": "2"},
    )
    assert reponse_etape_1.status_code == 200
    assert reponse_etape_2.status_code == 200
    assert reponse_etape_2.json()["duplicate"] is False
    assert reponse_etape_1.json()["event_id"] != reponse_etape_2.json()["event_id"]


def test_notification_incomplete_est_refusee(tmp_path, monkeypatch):
    """Sans identifiant de demande, il n'y a rien à enregistrer."""
    chemin_base = tmp_path / "test.sqlite3"
    monkeypatch.setattr(donnees, "CHEMIN_BASE", chemin_base)
    monkeypatch.delenv("ESUP_URL", raising=False)
    client = TestClient(connecteur.app)

    reponse = client.post("/esup/events", json={"status": "pending", "step": "2"})
    assert reponse.status_code == 400
    assert not chemin_base.exists()
