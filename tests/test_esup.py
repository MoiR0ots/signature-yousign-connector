"""Vérifier la règle métier avec un circuit fictif, sans serveur ESUP."""

import sqlite3
import pytest
from urllib.error import URLError

from connector import traitement as connecteur
from connector import donnees, app
from connector.traitement import analyser_demande


@pytest.fixture(autouse=True)
def remplacer_telechargement(monkeypatch):
    """Ces tests de circuit ne doivent pas télécharger un vrai document."""
    monkeypatch.setattr(
        connecteur.esup, "telecharger_document",
        lambda demande, etape, dossier: str(dossier / demande / str(etape) / "document.pdf"),
    )


def construire_demande():
    """Deux étapes, dont la dernière comporte un destinataire externe fictif."""
    derniere = {
        "id": 40,
        "allSignToComplete": True, "signType": "signature",
        "recipients": [{
            "id": 41, "signed": False,
            "user": {"firstname": "Test", "name": "Exemple", "email": "test@example.org", "userType": "external"},
        }],
        "signRequestParams": [{"recipientId": 41, "signPageNumber": 1, "xPos": 216,
                               "yPos": 50, "signWidth": 100, "signHeight": 50,
                               "signDocumentNumber": 0, "rotate": 0}],
    }
    return {
        "id": 105, "status": "pending",
        "parentSignBook": {"liveWorkflow": {
            "currentStep": derniere,
            "liveWorkflowSteps": [{"id": 38}, derniere],
        }},
    }


def test_derniere_etape_et_verificateur_ajoute():
    demande = construire_demande()
    evenement = {"signRequestId": "105", "status": "pending", "step": "2"}
    resultat = analyser_demande(demande, evenement)
    assert resultat["etat"] == "derniere_etape"
    assert resultat["signataires"][0]["emplacements"][0]["x"] == 216

    # Ajouter un vérificateur change le numéro, pas l'identité de la dernière étape.
    circuit = demande["parentSignBook"]["liveWorkflow"]
    circuit["liveWorkflowSteps"].insert(1, {"id": 39})
    assert analyser_demande(demande, evenement)["etat"] == "ignoree"
    evenement["step"] = "3"
    assert analyser_demande(demande, evenement)["id_etape"] == 40

    circuit["currentStep"] = circuit["liveWorkflowSteps"][1]
    evenement["step"] = "2"
    assert analyser_demande(demande, evenement)["motif"] == "Étape intermédiaire"

    demande["status"] = "completed"
    assert analyser_demande(demande, evenement)["etat"] == "ignoree"


def test_erreur_puis_reprise_sans_perdre_evenement(tmp_path, monkeypatch):
    monkeypatch.setattr(donnees, "CHEMIN_BASE", tmp_path / "events.sqlite3")
    evenement = {"signRequestId": "105", "status": "pending", "step": "2"}
    donnees.initialiser_base()
    reception = donnees.enregistrer_evenement(evenement)

    def serveur_indisponible(identifiant):
        raise URLError("Indisponible")

    monkeypatch.setattr(connecteur.esup, "lire_demande", serveur_indisponible)
    assert connecteur.consulter_evenement(reception["event_id"])["etat"] == "erreur"
    monkeypatch.setattr(connecteur.esup, "lire_demande", lambda identifiant: construire_demande())
    assert connecteur.consulter_evenement(reception["event_id"])["etat"] == "derniere_etape"
    connexion = sqlite3.connect(donnees.CHEMIN_BASE)
    try:
        assert connexion.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert connexion.execute("SELECT state FROM events").fetchone()[0] == "a_envoyer"
        assert connexion.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
        document_pdf, = connexion.execute("SELECT document_pdf FROM observations").fetchone()
        assert document_pdf.endswith("/documents/105/40/document.pdf")
    finally:
        connexion.close()


def test_post_ne_consulte_pas_esup_et_reprise_par_traitement(tmp_path, monkeypatch):
    """La réception stocke seulement ; le traitement organise la consultation séparément."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(donnees, "CHEMIN_BASE", tmp_path / "events.sqlite3")
    monkeypatch.setenv("ESUP_URL", "http://esup.test")

    def serveur_indisponible(identifiant):
        raise URLError("Indisponible")

    monkeypatch.setattr(connecteur.esup, "lire_demande", serveur_indisponible)
    client = TestClient(app.app)
    evenement = {"signRequestId": "105", "status": "pending", "step": "2"}
    premiere = client.post("/esup/events", json=evenement)
    assert premiere.status_code == 200
    connexion = sqlite3.connect(donnees.CHEMIN_BASE)
    try:
        assert connexion.execute("SELECT state FROM events").fetchone()[0] == "received"
    finally:
        connexion.close()
    # Le POST n'a fait aucun appel ESUP, malgré le client indisponible.
    assert connecteur.consulter_evenement(premiere.json()["event_id"])["etat"] == "erreur"
    monkeypatch.setattr(connecteur.esup, "lire_demande", lambda identifiant: construire_demande())
    seconde = client.post("/esup/events", json=evenement)
    assert seconde.status_code == 200
    assert seconde.json()["duplicate"] is True
    connecteur.consulter_evenement(seconde.json()["event_id"])
    connexion = sqlite3.connect(donnees.CHEMIN_BASE)
    try:
        assert connexion.execute("SELECT state FROM events").fetchone()[0] == "a_envoyer"
    finally:
        connexion.close()


def test_api_en_retard_puis_a_jour(tmp_path, monkeypatch):
    """Retenter après un brouillon et une étape ancienne, puis garder le résultat."""
    monkeypatch.setattr(donnees, "CHEMIN_BASE", tmp_path / "events.sqlite3")
    attentes = []
    monkeypatch.setattr(connecteur.time, "sleep", attentes.append)
    brouillon = construire_demande()
    brouillon["status"] = "draft"
    ancienne = construire_demande()
    circuit = ancienne["parentSignBook"]["liveWorkflow"]
    circuit["currentStep"] = circuit["liveWorkflowSteps"][0]
    reponses = iter([brouillon, ancienne, construire_demande()])
    monkeypatch.setattr(connecteur.esup, "lire_demande", lambda identifiant: next(reponses))
    evenement = {"signRequestId": "105", "status": "pending", "step": "2"}
    donnees.initialiser_base()
    reception = donnees.enregistrer_evenement(evenement)
    assert connecteur.consulter_evenement(reception["event_id"])["etat"] == "derniere_etape"
    assert attentes == [5, 5]


def test_decalage_persistant_reste_a_reprendre(tmp_path, monkeypatch):
    """Après trois lectures trop anciennes, ne pas classer l'événement ignoré."""
    monkeypatch.setattr(donnees, "CHEMIN_BASE", tmp_path / "events.sqlite3")
    attentes = []
    monkeypatch.setattr(connecteur.time, "sleep", attentes.append)
    demande = construire_demande()
    demande["status"] = "draft"
    monkeypatch.setattr(connecteur.esup, "lire_demande", lambda identifiant: demande)
    donnees.initialiser_base()
    reception = donnees.enregistrer_evenement(
        {"signRequestId": "105", "status": "pending", "step": "2"}
    )
    assert connecteur.consulter_evenement(reception["event_id"])["etat"] == "a_reprendre"
    assert attentes == [5, 5]
    connexion = sqlite3.connect(donnees.CHEMIN_BASE)
    try:
        assert connexion.execute("SELECT state FROM events").fetchone()[0] == "a_reprendre"
    finally:
        connexion.close()


def test_echec_pdf_marque_evenement_en_erreur(tmp_path, monkeypatch):
    monkeypatch.setattr(donnees, "CHEMIN_BASE", tmp_path / "events.sqlite3")
    monkeypatch.setattr(connecteur.esup, "lire_demande", lambda identifiant: construire_demande())

    def reponse_non_pdf(demande, etape, dossier):
        raise ValueError("La réponse ESUP n'est pas un PDF")

    monkeypatch.setattr(connecteur.esup, "telecharger_document", reponse_non_pdf)
    donnees.initialiser_base()
    reception = donnees.enregistrer_evenement(
        {"signRequestId": "105", "status": "pending", "step": "2"}
    )
    resultat = connecteur.consulter_evenement(reception["event_id"])
    assert resultat["etat"] == "erreur"
    assert resultat["detail"] == "La réponse ESUP n'est pas un PDF"
    assert "document_pdf" not in resultat


def test_emplacement_generique_pour_un_seul_destinataire():
    demande = construire_demande()
    etape = demande["parentSignBook"]["liveWorkflow"]["currentStep"]
    etape["signRequestParams"][0]["recipientId"] = None
    evenement = {"signRequestId": "105", "status": "pending", "step": "2"}
    resultat = analyser_demande(demande, evenement)
    assert len(resultat["signataires"][0]["emplacements"]) == 1
    # Ne pas attribuer silencieusement un champ générique à plusieurs personnes.
    etape["recipients"].append({"id": 42, "signed": False, "user": {}})
    resultat = analyser_demande(demande, evenement)
    assert all(not personne["emplacements"] for personne in resultat["signataires"])


@pytest.mark.parametrize(("champ", "valeur", "motif"), [
    ("signType", "visa", "type signature"),
    ("allSignToComplete", False, "tous les destinataires"),
    ("signWidth", 84, "Destinataire ESUP 41, page 1, position (216, 50) : champ de 84 × 50"),
])
def test_diagnostic_avant_telechargement(tmp_path, monkeypatch, champ, valeur, motif):
    demande = construire_demande()
    etape = demande["parentSignBook"]["liveWorkflow"]["currentStep"]
    if champ == "signWidth":
        etape["signRequestParams"][0][champ] = valeur
    else:
        etape[champ] = valeur
    monkeypatch.setattr(donnees, "CHEMIN_BASE", tmp_path / "events.sqlite3")
    monkeypatch.setattr(connecteur.esup, "lire_demande", lambda identifiant: demande)
    monkeypatch.setattr(connecteur.esup, "telecharger_document", lambda *args: pytest.fail("Téléchargement"))
    donnees.initialiser_base()
    reception = donnees.enregistrer_evenement({"signRequestId": "105", "status": "pending", "step": "2"})
    observation = connecteur.consulter_evenement(reception["event_id"])
    assert motif in observation["detail"]
    assert observation["etat"] == "erreur"


def test_remplacement_atomique_des_zones_et_destinataires(tmp_path, monkeypatch):
    monkeypatch.setattr(donnees, "CHEMIN_BASE", tmp_path / "events.sqlite3")
    donnees.initialiser_base()
    evenement = {"signRequestId": "105", "status": "pending", "step": "2"}
    identifiant = donnees.enregistrer_evenement(evenement)["event_id"]
    observation = analyser_demande(construire_demande(), evenement)
    personne = observation["signataires"][0]
    personne["emplacements"].append(dict(personne["emplacements"][0], page=2))
    donnees.enregistrer_observation(identifiant, observation, "a_envoyer")

    # Si la deuxième écriture échoue, les emplacements précédents restent lisibles.
    observation["signataires"].append(personne)
    with pytest.raises(sqlite3.IntegrityError):
        donnees.enregistrer_observation(identifiant, observation, "erreur")
    with sqlite3.connect(donnees.CHEMIN_BASE) as connexion:
        assert connexion.execute("SELECT state FROM events").fetchone() == ("a_envoyer",)
        assert connexion.execute("SELECT page FROM emplacements ORDER BY numero").fetchall() == [(1,), (2,)]
        assert connexion.execute("SELECT id_destinataire FROM destinataires_observes").fetchall() == [(41,)]
        donnees.enregistrer_observation(identifiant, {"etat": "ignoree"}, "ignoree")
        assert connexion.execute("SELECT * FROM emplacements").fetchall() == []
        assert connexion.execute("SELECT * FROM destinataires_observes").fetchall() == []
        assert connexion.execute("PRAGMA foreign_key_check").fetchall() == []
