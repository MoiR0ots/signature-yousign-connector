"""Vérifier le brouillon sans joindre Yousign ni utiliser de vraie clé."""

import pytest
import sqlite3
import hashlib
from pathlib import Path
from connector import yousign, donnees, traitement, esup


def creer_observation(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUSIGN_API_KEY", "cle-fictive")
    monkeypatch.setattr(donnees, "CHEMIN_BASE", tmp_path / "events.sqlite3")
    donnees.initialiser_base()
    reception = donnees.enregistrer_evenement(
        {"signRequestId": "203", "status": "pending", "step": "2"}
    )
    pdf = tmp_path / "document.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF")
    observation = {
        "etat": "derniere_etape", "id_etape": 209, "document_pdf": str(pdf),
        "type_signature": "signature", "toutes_signatures_requises": True,
        "signataires": [{
            "id_destinataire": 210, "prenom": "Test", "nom": "Exemple",
            "email": "test@example.org", "type": "external", "deja_signe": False,
            "emplacements": [{"page": 1, "document": 0, "rotation": 0,
                              "toutes_les_pages": False, "champ_pdf": None,
                              "x": 37, "y": 691, "largeur": 100, "hauteur": 50}],
        }],
    }
    monkeypatch.setattr(traitement, "consulter_evenement", lambda identifiant: observation)
    contexte = {"signRequestId": 203, "expectedStepId": 209, "contextHash": "a" * 64,
                "sourceDocumentId": 204, "sourceSha256": hashlib.sha256(pdf.read_bytes()).hexdigest()}
    monkeypatch.setattr(esup, "lire_contexte_retour", lambda identifiant: dict(contexte))
    derniere = {
        "id": 209, "allSignToComplete": True, "signType": "signature",
        "recipients": [{"id": 210, "signed": False, "user": {
            "firstname": "Test", "name": "Exemple", "email": "test@example.org", "userType": "external"}}],
        "signRequestParams": [{"recipientId": 210, "signPageNumber": 1, "signDocumentNumber": 0,
                               "xPos": 37, "yPos": 691, "signWidth": 100, "signHeight": 50,
                               "rotate": 0, "allPages": False}],
    }
    demande = {"id": 203, "status": "pending", "parentSignBook": {"liveWorkflow": {
        "currentStep": derniere, "liveWorkflowSteps": [{"id": 207}, derniere]}}}
    monkeypatch.setattr(esup, "lire_demande", lambda identifiant: demande)
    monkeypatch.setattr(esup, "envoyer_document_signe", lambda *arguments: {
        "signRequestId": 203, "documentId": 300, "status": "completed",
        "signBookStatus": "completed", "duplicate": False,
    })
    return reception["event_id"], observation


def test_brouillon_et_relance_sans_double_creation(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    appels = []
    base = donnees

    def repondre(methode, chemin, donnees=None, fichier=None):
        appels.append((methode, chemin, donnees))
        if methode == "POST" and chemin == "/signature_requests":
            # La sauvegarde doit précéder même le tout premier POST Yousign.
            historique = base.lister_brouillons()[0][2]
            assert historique['contexte_esup']['expectedStepId'] == 209
        if chemin.endswith("/documents"):
            return {"id": "document-test"}
        if chemin.endswith("/signers"):
            assert donnees["fields"][0]["y"] == 691
            assert donnees["fields"][0]["document_id"] == "document-test"
            return {"id": "signataire-test"}
        if methode == "GET":
            return {"status": "draft"}
        assert donnees["delivery_mode"] == "email"
        return {"id": "demande-test"}

    monkeypatch.setattr(yousign, "appeler_api", repondre)
    resultat = traitement.preparer_brouillon(evenement, observation)
    assert resultat["pret"] is True
    assert len(appels) == 4
    assert traitement.preparer_brouillon(evenement, observation) == resultat
    assert len(appels) == 4
    # Le contexte conservé suffit à refuser une préparation différente,
    # sans calculer une deuxième empreinte locale du PDF et des signataires.
    contexte_modifie = dict(resultat["contexte_esup"], contextHash="b" * 64)
    monkeypatch.setattr(esup, "lire_contexte_retour", lambda identifiant: contexte_modifie)
    with pytest.raises(ValueError, match="contexte ESUP a changé"):
        traitement.preparer_brouillon(evenement, observation)
    assert len(appels) == 4
    monkeypatch.setattr(esup, "lire_contexte_retour", lambda identifiant: resultat["contexte_esup"])
    observation["signataires"][0]["email"] = "autre@example.org"
    with pytest.raises(ValueError, match="ont changé"):
        traitement.preparer_brouillon(evenement, observation)


def test_resultat_incertain_empeche_un_second_post(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    appels = []

    def delai_depasse(*args, **kwargs):
        appels.append(args)
        raise TimeoutError("Connexion perdue après l'envoi")

    monkeypatch.setattr(yousign, "appeler_api", delai_depasse)
    with pytest.raises(TimeoutError):
        traitement.preparer_brouillon(evenement, observation)
    with pytest.raises(ValueError, match="résultat incertain"):
        traitement.preparer_brouillon(evenement, observation)
    assert len(appels) == 1


def test_placement_complexe_refuse_avant_creation(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    observation["signataires"][0]["emplacements"][0]["rotation"] = 90
    with pytest.raises(ValueError, match="Placement non pris en charge"):
        traitement.preparer_brouillon(evenement, observation)


def test_upload_envoie_le_format_multipart_attendu(tmp_path, monkeypatch):
    """Le document doit être signable_document, et non signable."""
    from io import BytesIO

    monkeypatch.setenv("YOUSIGN_API_KEY", "cle-fictive")
    fichier = tmp_path / "document.pdf"
    fichier.write_bytes(b"%PDF-1.7\n%%EOF")

    def serveur(requete, timeout):
        assert requete.full_url.startswith("https://api-sandbox.yousign.app/v3/")
        assert b'\r\n\r\nsignable_document\r\n' in requete.data
        assert fichier.read_bytes() in requete.data
        return BytesIO(b'{"id":"document-test"}')

    monkeypatch.setattr(yousign, "urlopen", serveur)
    assert yousign.appeler_api("POST", "/signature_requests/test/documents", fichier=fichier)["id"] == "document-test"


def test_signataire_interne_accepte_a_la_derniere_etape(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    observation["signataires"][0]["type"] = "ldap"
    signataires = yousign.preparer_signataires(observation)
    assert len(signataires) == 1
    assert signataires[0]["info"]["email"] == "test@example.org"
    observation["signataires"][0]["deja_signe"] = True
    with pytest.raises(ValueError, match="déjà signé"):
        yousign.preparer_signataires(observation)


def test_activation_email_et_recuperation_apres_signature(tmp_path, monkeypatch):
    """Activer une seule fois et reprendre le téléchargement après une panne."""
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    appels = []
    etat = {"status": "draft"}

    def repondre(methode, chemin, donnees=None, fichier=None):
        appels.append((methode, chemin, donnees))
        if chemin.endswith('/activate'):
            etat['status'] = 'ongoing'
        if '/signers/' in chemin:
            return {'signature_link': 'lien-de-test'}
        return dict(etat)

    monkeypatch.setattr(yousign, 'appeler_api', repondre)
    suivi = {'demande_id': 'demande-test', 'document_id': 'document-test',
             'signataires': {'210': 'signataire-test'}, 'pret': True, 'en_cours': ''}
    suivi['contexte_esup'] = esup.lire_contexte_retour('203')
    donnees.reserver_brouillon('203', '209', esup.lire_contexte_retour('203'))
    donnees.sauvegarder_brouillon('203', '209', suivi)
    traitement.actualiser_demande( '203', '209', suivi)
    assert all(methode != 'PATCH' for methode, chemin, donnees in appels)
    assert suivi['statut_yousign'] == 'ongoing'
    traitement.actualiser_demande( '203', '209', suivi)
    assert sum(chemin.endswith('/activate') for methode, chemin, donnees in appels) == 1
    etat['status'] = 'done'

    def telecharger(chemin, document, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'%PDF-1.7\n%%EOF')

    monkeypatch.setattr(yousign, 'telecharger_pdf_signe', telecharger)
    traitement.actualiser_demande( '203', '209', suivi)
    assert Path(suivi['document_signe']).exists()
    assert 'liens_signature' not in suivi


def test_activation_refusee_ne_repete_pas_les_invitations(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    appels = []

    def repondre(methode, chemin, donnees=None, fichier=None):
        appels.append(methode)
        if methode == 'POST':
            raise ValueError('Refus Sandbox')
        return {'status': 'draft'}

    monkeypatch.setattr(yousign, 'appeler_api', repondre)
    suivi = {'demande_id': 'demande-test', 'document_id': 'document-test',
             'signataires': {}, 'pret': True, 'en_cours': ''}
    donnees.reserver_brouillon('203', '209', esup.lire_contexte_retour('203'))
    donnees.sauvegarder_brouillon('203', '209', suivi)
    with pytest.raises(ValueError, match='Refus Sandbox'):
        traitement.actualiser_demande( '203', '209', suivi)
    traitement.actualiser_demande( '203', '209', suivi)
    assert appels.count('POST') == 1
    assert suivi['activation_bloquee'] is True


def test_cycle_automatique_jusqu_au_pdf_signe(tmp_path, monkeypatch):
    """Deux cycles suivent une demande ; les suivants ne recréent rien."""
    from connector import traitement

    evenement, observation = creer_observation(tmp_path, monkeypatch)
    etat = {'status': 'draft'}
    appels = []

    def repondre(methode, chemin, donnees=None, fichier=None):
        appels.append((methode, chemin))
        if chemin.endswith('/activate'):
            etat['status'] = 'ongoing'
        if chemin.endswith('/documents'):
            return {'id': 'document-test'}
        if chemin.endswith('/signers'):
            return {'id': 'signataire-test'}
        if '/signers/' in chemin:
            return {'signature_link': 'lien-de-test'}
        if methode == 'POST':
            return {'id': 'demande-test'}
        return dict(etat)

    def telecharger(chemin, document, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'%PDF-1.7\n%%EOF')

    monkeypatch.setattr(yousign, 'appeler_api', repondre)
    monkeypatch.setattr(yousign, 'telecharger_pdf_signe', telecharger)
    traitement.executer_cycle()
    etat['status'] = 'done'
    traitement.executer_cycle()
    nombre_appels = len(appels)
    traitement.executer_cycle()
    assert len(appels) == nombre_appels
    assert sum(chemin.endswith('/activate') for methode, chemin in appels) == 1
    assert (tmp_path / 'documents/203/209/document-yousign.pdf').exists()
    assert Path(observation['document_pdf']).read_bytes() == b'%PDF-1.7\n%%EOF'
    connexion = sqlite3.connect(donnees.CHEMIN_BASE)
    try:
        assert connexion.execute('SELECT state FROM events').fetchone()[0] == 'transmis_yousign'
        suivi = donnees.lister_brouillons()[0][2]
        assert suivi['statut_yousign'] == 'done'
        assert suivi['retour_esup']['documentId'] == 300
    finally:
        connexion.close()


def test_changement_api_refuse_sur_une_base_existante(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    monkeypatch.setattr(yousign, 'ADRESSE_API', 'https://api-sandbox.yousign.app/v3')
    donnees.verifier_environnement(yousign.ADRESSE_API)
    monkeypatch.setattr(yousign, 'ADRESSE_API', 'https://api.yousign.app/v3')
    with pytest.raises(ValueError, match='nouveau dossier de données'):
        donnees.verifier_environnement(yousign.ADRESSE_API)
    monkeypatch.setattr(donnees, 'CHEMIN_BASE', tmp_path / 'prod/events.sqlite3')
    donnees.initialiser_base()
    donnees.verifier_environnement(yousign.ADRESSE_API)


def test_pdf_modifie_refuse_avant_envoi(tmp_path, monkeypatch):
    """Le PDF local doit être celui qu'ESUP associe au contexte initial."""
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    Path(observation['document_pdf']).write_bytes(b'%PDF-1.7\nautre version\n%%EOF')
    with pytest.raises(ValueError, match='PDF ESUP a changé'):
        traitement.preparer_brouillon(evenement, observation)
    assert donnees.lister_brouillons() == []


def test_retour_repris_apres_perte_de_reponse(tmp_path, monkeypatch):
    """ESUP a importé le PDF, mais sa réponse au premier POST a été perdue."""
    creer_observation(tmp_path, monkeypatch)
    contexte = esup.lire_contexte_retour('203')
    suivi = {'demande_id': 'demande-test', 'document_id': 'document-test',
             'signataires': {}, 'pret': True, 'en_cours': '',
             'contexte_esup': contexte}
    donnees.reserver_brouillon('203', '209', esup.lire_contexte_retour('203'))
    donnees.sauvegarder_brouillon('203', '209', suivi)
    monkeypatch.setattr(yousign, 'lire_demande', lambda identifiant: {'status': 'done'})
    telechargements = []

    def telecharger(demande, document, destination):
        telechargements.append(destination)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b'%PDF-1.7\nsigne\n%%EOF')

    envois = []

    def importer(demande, contexte_initial, reference, document):
        envois.append((demande, contexte_initial, reference, Path(document).read_bytes()))
        if len(envois) == 1:
            raise TimeoutError('Réponse perdue après import')
        return {'signRequestId': 203, 'documentId': 301, 'status': 'completed',
                'signBookStatus': 'pending', 'duplicate': True}

    monkeypatch.setattr(yousign, 'telecharger_pdf_signe', telecharger)
    monkeypatch.setattr(esup, 'envoyer_document_signe', importer)
    # Aucun nouveau contexte ne doit être demandé au retour.
    monkeypatch.setattr(esup, 'lire_contexte_retour', lambda identifiant: pytest.fail('Relecture du contexte'))
    traitement.suivre_demandes()
    traitement.suivre_demandes()
    traitement.suivre_demandes()
    assert len(envois) == 2
    assert envois[0] == envois[1]
    assert len(telechargements) == 1
    resultat = donnees.lister_brouillons()[0][2]
    assert resultat['retour_esup']['duplicate'] is True


def test_reprise_avant_envoi_et_disparition_de_la_vue(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    contexte = esup.lire_contexte_retour("203")
    tentatives = []

    def lire_contexte(demande):
        tentatives.append(demande)
        raise TimeoutError("Lecture du contexte indisponible")

    monkeypatch.setattr(esup, "lire_contexte_retour", lire_contexte)
    traitement.traiter_evenements()
    traitement.traiter_evenements()
    assert tentatives == ["203"]
    with sqlite3.connect(donnees.CHEMIN_BASE) as connexion:
        demande, motif, date = connexion.execute(
            "SELECT demande, motif, prochain_essai_utc FROM dossiers_a_verifier"
        ).fetchone()
        assert (demande, motif) == ("203", "Lecture du contexte indisponible")
        assert date
        connexion.execute("UPDATE observations SET observed_at=datetime('now', '-6 minutes')")

    monkeypatch.setattr(esup, "lire_contexte_retour", lambda demande: contexte)
    monkeypatch.setattr(yousign, "appeler_api", lambda *args, **kwargs: {"id": "test", "status": "draft"})
    traitement.traiter_evenements()
    assert donnees.evenements_a_traiter() == []
    with sqlite3.connect(donnees.CHEMIN_BASE) as connexion:
        assert connexion.execute("SELECT * FROM dossiers_a_verifier").fetchall() == []
        assert connexion.execute("SELECT state FROM events").fetchone() == ("transmis_yousign",)


def test_post_incertain_reste_visible_sans_relance(tmp_path, monkeypatch):
    creer_observation(tmp_path, monkeypatch)
    appels = []

    def perdre_reponse(*args):
        appels.append(args)
        raise TimeoutError("Réponse perdue")

    monkeypatch.setattr(yousign, "creer_demande", perdre_reponse)
    traitement.traiter_evenements()
    with sqlite3.connect(donnees.CHEMIN_BASE) as connexion:
        connexion.execute("UPDATE observations SET observed_at='2000-01-01'")
        assert connexion.execute("SELECT phase, motif, prochain_essai_utc FROM dossiers_a_verifier").fetchall() == [
            ("creation", "Réponse perdue", None)
        ]
    traitement.traiter_evenements()
    assert len(appels) == 1


def test_lien_initialement_vide_puis_disponible(tmp_path, monkeypatch):
    creer_observation(tmp_path, monkeypatch)
    donnees.reserver_brouillon("203", "209", esup.lire_contexte_retour("203"))
    suivi = {"demande_id": "test", "pret": True, "en_cours": "", "signataires": {"210": "a", "211": "b"},
             "liens_signature": {"210": None, "211": None}}
    monkeypatch.setattr(yousign, "lire_demande", lambda demande: {
        "status": "ongoing", "signers": [{"id": "a", "status": "notified"}, {"id": "b", "status": "signed"}]
    })
    appels = []

    def lire(demande, signataire):
        appels.append(signataire)
        return {"status": "notified", "signature_link": "lien-fictif"}

    monkeypatch.setattr(yousign, "lire_signataire", lire)
    traitement.actualiser_demande("203", "209", suivi)
    traitement.actualiser_demande("203", "209", suivi)
    assert appels == ["a"]
    assert suivi["liens_signature"] == {"210": "lien-fictif"}
    assert suivi["statuts_signataires"] == {"210": "notified", "211": "signed"}


def test_refus_http_conserve_uniquement_le_type(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    def refuser(requete, timeout):
        corps = BytesIO(b'{"type":"validation.error","detail":"donnee-privee"}')
        raise HTTPError(requete.full_url, 400, "Bad Request", {}, corps)

    monkeypatch.setenv("YOUSIGN_API_KEY", "cle-fictive")
    monkeypatch.setattr(yousign, "urlopen", refuser)
    with pytest.raises(ValueError, match=r"HTTP 400.*validation\.error") as erreur:
        yousign.activer_demande("test")
    assert "donnee-privee" not in str(erreur.value)


def test_ancienne_erreur_et_notification_plus_recente(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    donnees.enregistrer_observation(evenement, observation, "erreur_yousign")
    with sqlite3.connect(donnees.CHEMIN_BASE) as connexion:
        connexion.execute("UPDATE observations SET observed_at='2000-01-01'")
    assert donnees.evenements_a_traiter() == [(evenement,)]
    reception = donnees.enregistrer_evenement({"signRequestId": "203", "step": "2", "status": "completed"})
    assert donnees.evenements_a_traiter() == [(reception["event_id"],)]
    with sqlite3.connect(donnees.CHEMIN_BASE) as connexion:
        assert connexion.execute("SELECT * FROM dossiers_a_verifier").fetchall() == []


def test_suivi_relationnel_apres_reouverture(tmp_path, monkeypatch):
    creer_observation(tmp_path, monkeypatch)
    contexte = esup.lire_contexte_retour("203")
    suivi = donnees.reserver_brouillon("203", "209", contexte)
    suivi.update(demande_id="demande-test", document_id="document-test", en_cours="", pret=True,
                 statut_yousign="ongoing", active_le="2026-11-19T10:00:00Z",
                 signataires={"210": "alice", "211": "bob"}, liens_signature={"211": "lien-fictif"},
                 statuts_signataires={"210": "signed", "211": "notified"})
    donnees.sauvegarder_brouillon("203", "209", suivi)
    assert donnees.lister_brouillons() == [("203", "209", suivi)]
    suivi.pop("liens_signature")
    suivi.update(statut_yousign="done", termine_le="2026-11-19T11:00:00Z", document_signe="signe.pdf",
                 retour_esup={"signRequestId": 203, "documentId": 300, "status": "completed",
                              "signBookStatus": "completed", "duplicate": False})
    donnees.sauvegarder_brouillon("203", "209", suivi)
    assert donnees.lister_brouillons() == [("203", "209", suivi)]
    with sqlite3.connect(donnees.CHEMIN_BASE) as connexion:
        assert connexion.execute("SELECT lien_signature FROM signataires").fetchall() == [(None,), (None,)]
