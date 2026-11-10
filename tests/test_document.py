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
