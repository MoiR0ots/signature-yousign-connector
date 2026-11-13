"""Contrôler le téléchargement avec des réponses HTTP fictives."""

from io import BytesIO
import pytest
from connector import esup


@pytest.mark.parametrize('contenu,taille', [
    (b'%PDF-1.7\ncontenu fictif\n%%EOF\n', None),
    (b'<html>connexion</html>', None),
    (b'%PDF-1.7\ninterrompu', None),
    (b'%PDF-1.7\n%%EOF', '999'),
])
def test_telechargement_preserve_ancien_pdf_si_echec(tmp_path, monkeypatch, contenu, taille):
    monkeypatch.setenv('ESUP_URL', 'http://esup.test')
    reponse = BytesIO(contenu)
    reponse.headers = {} if taille is None else {'Content-Length': taille}
    monkeypatch.setattr(esup, 'urlopen', lambda requete, timeout: reponse)
    dossier = tmp_path / '171' / '177'
    dossier.mkdir(parents=True)
    destination = dossier / 'document.pdf'
    destination.write_bytes(b'ancien document')

    if contenu.endswith(b'%%EOF\n'):
        chemin = esup.telecharger_document('171', 177, tmp_path)
        assert chemin == str(destination)
        assert destination.read_bytes() == contenu
        assert destination.stat().st_mode & 0o777 == 0o600
    else:
        with pytest.raises(ValueError):
            esup.telecharger_document('171', 177, tmp_path)
        assert destination.read_bytes() == b'ancien document'
    assert list(dossier.glob('*.tmp')) == []


def test_contexte_exige_et_transmet_le_jeton_esup(monkeypatch):
    """Le nouveau point d'entrée doit recevoir le jeton ESUP, pas la clé Yousign."""
    monkeypatch.setenv('ESUP_URL', 'http://esup.test')
    monkeypatch.delenv('ESUP_API_KEY', raising=False)
    with pytest.raises(ValueError, match='ESUP_API_KEY'):
        esup.lire_contexte_retour('243')
    monkeypatch.setenv('ESUP_API_KEY', 'jeton-esup-fictif\n')
    monkeypatch.setenv('YOUSIGN_API_KEY', 'cle-yousign-fictive')

    def serveur(requete, timeout):
        assert requete.full_url == 'http://esup.test/ws/signrequests/243/external-completion/context'
        assert requete.get_header('X-api-key') == 'jeton-esup-fictif'
        assert not requete.has_header('Authorization')
        return BytesIO(b'{"signRequestId":243,"expectedStepId":249}')

    monkeypatch.setattr(esup, 'urlopen', serveur)
    assert esup.lire_contexte_retour('243')['expectedStepId'] == 249


def test_retour_pdf_transmet_le_contexte_initial(tmp_path, monkeypatch):
    """Le formulaire correspond au contrat du contrôleur ajouté dans ESUP."""
    monkeypatch.setenv('ESUP_URL', 'http://esup.test')
    monkeypatch.setenv('ESUP_API_KEY', 'jeton-esup-fictif')
    document = tmp_path / 'signe.pdf'
    document.write_bytes(b'%PDF-1.7\nsigne\n%%EOF')
    contexte = {'expectedStepId': 249, 'contextHash': 'a' * 64}

    def serveur(requete, timeout):
        from email.parser import BytesParser
        from email.policy import default

        assert requete.full_url == 'http://esup.test/ws/signrequests/243/external-completion'
        assert requete.method == 'POST'
        assert requete.get_header('X-api-key') == 'jeton-esup-fictif'
        assert not requete.has_header('Authorization')
        formulaire = BytesParser(policy=default).parsebytes(
            ('Content-Type: ' + requete.get_header('Content-type') + '\r\n\r\n').encode() + requete.data)
        champs = {partie.get_param('name', header='content-disposition'): partie.get_payload(decode=True)
                  for partie in formulaire.iter_parts()}
        assert champs == {'expectedStepId': b'249', 'contextHash': b'a' * 64,
                          'reference': b'yousign:demande-test', 'file': document.read_bytes()}
        return BytesIO(b'{"signRequestId":243,"status":"completed","documentId":300}')

    monkeypatch.setattr(esup, 'urlopen', serveur)
    assert esup.envoyer_document_signe('243', contexte, 'yousign:demande-test', document)['documentId'] == 300
