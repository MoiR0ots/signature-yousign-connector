"""Tester les requêtes et placements du client Yousign sans API réelle."""

import pytest
from connector import yousign


def creer_observation(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUSIGN_API_KEY", "cle-fictive")
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
    return 0, observation


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
