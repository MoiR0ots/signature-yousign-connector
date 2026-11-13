"""Vérifier le brouillon sans joindre Yousign ni utiliser de vraie clé."""

import pytest
import json
import sqlite3
from pathlib import Path
from connector import yousign, donnees, traitement


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
    return reception["event_id"], observation


def test_brouillon_et_relance_sans_double_creation(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    appels = []

    def repondre(methode, chemin, donnees=None, fichier=None):
        appels.append((methode, chemin, donnees))
        if chemin.endswith("/documents"):
            return {"id": "document-test"}
        if chemin.endswith("/signers"):
            assert donnees["fields"][0]["y"] == 691
            assert donnees["fields"][0]["document_id"] == "document-test"
            return {"id": "signataire-test"}
        if methode == "GET":
            return {"status": "draft"}
        assert donnees["delivery_mode"] == "none"
        return {"id": "demande-test"}

    monkeypatch.setattr(yousign, "appeler_api", repondre)
    resultat = traitement.preparer_brouillon(evenement)
    assert resultat["pret"] is True
    assert len(appels) == 4
    assert traitement.preparer_brouillon(evenement) == resultat
    assert len(appels) == 4
    observation["signataires"][0]["email"] = "autre@example.org"
    with pytest.raises(ValueError, match="ont changé"):
        traitement.preparer_brouillon(evenement)


def test_resultat_incertain_empeche_un_second_post(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    appels = []

    def delai_depasse(*args, **kwargs):
        appels.append(args)
        raise TimeoutError("Connexion perdue après l'envoi")

    monkeypatch.setattr(yousign, "appeler_api", delai_depasse)
    with pytest.raises(TimeoutError):
        traitement.preparer_brouillon(evenement)
    with pytest.raises(ValueError, match="résultat incertain"):
        traitement.preparer_brouillon(evenement)
    assert len(appels) == 1


def test_placement_complexe_refuse_avant_creation(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    observation["signataires"][0]["emplacements"][0]["rotation"] = 90
    with pytest.raises(ValueError, match="Placement non pris en charge"):
        traitement.preparer_brouillon(evenement)


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
    monkeypatch.setenv("YOUSIGN_LIVRAISON", "email")
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
    connexion = sqlite3.connect(donnees.CHEMIN_BASE)
    suivi = {'demande_id': 'demande-test', 'document_id': 'document-test',
             'signataires': {'210': 'signataire-test'}, 'pret': True, 'en_cours': ''}
    connexion.execute('INSERT INTO brouillons_yousign VALUES (?, ?, ?)',
                      ('203', '209', json.dumps(suivi)))
    connexion.commit()
    try:
        traitement.actualiser_demande( '203', '209', suivi)
        assert ('PATCH', '/signature_requests/demande-test', {'delivery_mode': 'email'}) in appels
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
    finally:
        connexion.close()


def test_activation_refusee_ne_repete_pas_les_invitations(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    appels = []

    def repondre(methode, chemin, donnees=None, fichier=None):
        appels.append(methode)
        if methode == 'POST':
            raise ValueError('Refus Sandbox')
        return {'status': 'draft'}

    monkeypatch.setattr(yousign, 'appeler_api', repondre)
    connexion = sqlite3.connect(donnees.CHEMIN_BASE)
    suivi = {'demande_id': 'demande-test', 'document_id': 'document-test',
             'signataires': {}, 'pret': True, 'en_cours': ''}
    connexion.execute('INSERT INTO brouillons_yousign VALUES (?, ?, ?)',
                      ('203', '209', json.dumps(suivi)))
    connexion.commit()
    try:
        with pytest.raises(ValueError, match='Refus Sandbox'):
            traitement.actualiser_demande( '203', '209', suivi)
        traitement.actualiser_demande( '203', '209', suivi)
        assert appels.count('POST') == 1
        assert suivi['activation_bloquee'] is True
    finally:
        connexion.close()


def test_cycle_automatique_jusqu_au_pdf_signe(tmp_path, monkeypatch):
    """Deux cycles suivent une demande ; les suivants ne recréent rien."""
    from connector import traitement

    evenement, observation = creer_observation(tmp_path, monkeypatch)
    monkeypatch.setenv('YOUSIGN_AUTOMATIQUE', '1')
    monkeypatch.setenv('YOUSIGN_LIVRAISON', 'email')
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
        suivi = json.loads(connexion.execute('SELECT suivi FROM brouillons_yousign').fetchone()[0])
        assert suivi['statut_yousign'] == 'done'
    finally:
        connexion.close()


def test_cycle_ne_rejoue_pas_les_anciens_essais(tmp_path, monkeypatch):
    from connector import traitement

    evenement, observation = creer_observation(tmp_path, monkeypatch)
    monkeypatch.setenv('YOUSIGN_AUTOMATIQUE', '1')
    connexion = sqlite3.connect(donnees.CHEMIN_BASE)
    try:
        connexion.execute("UPDATE events SET state='derniere_etape'")
        connexion.execute('INSERT INTO brouillons_yousign VALUES (?, ?, ?)', (
            '203', '209', json.dumps({'pret': True, 'en_cours': '', 'demande_id': 'ancien-test'}),
        ))
        connexion.commit()
    finally:
        connexion.close()

    def aucun_appel(*args, **kwargs):
        raise AssertionError('Un ancien essai ne doit pas être activé')

    monkeypatch.setattr(yousign, 'appeler_api', aucun_appel)
    traitement.executer_cycle()


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


def test_historique_sans_marqueur_reste_sandbox(tmp_path, monkeypatch):
    evenement, observation = creer_observation(tmp_path, monkeypatch)
    connexion = sqlite3.connect(donnees.CHEMIN_BASE)
    try:
        connexion.execute('INSERT INTO brouillons_yousign VALUES (?, ?, ?)', ('203','209','{}'))
        connexion.commit()
    finally:
        connexion.close()
    monkeypatch.setattr(yousign, 'ADRESSE_API', 'https://api.yousign.app/v3')
    with pytest.raises(ValueError, match='nouveau dossier de données'):
        donnees.verifier_environnement(yousign.ADRESSE_API)
