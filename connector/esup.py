"""Client ESUP : lire une demande et télécharger son document courant."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


def lire_demande(identifiant):
    """Consulter l'API ESUP avec la bibliothèque standard Python."""
    adresse = os.environ.get("ESUP_URL", "").rstrip("/")
    if not adresse:
        raise ValueError("ESUP_URL n'est pas configurée")
    url = adresse + "/ws/signrequests/" + quote(str(identifiant), safe="")
    requete = Request(url, headers={"Accept": "application/json"})
    # Un délai évite de laisser une consultation attendre indéfiniment.
    with urlopen(requete, timeout=10) as reponse:
        return json.load(reponse)


def telecharger_document(identifiant_demande, identifiant_etape, dossier_documents):
    """Enregistrer le PDF courant sans exposer un téléchargement incomplet."""
    adresse = os.environ.get("ESUP_URL", "").rstrip("/")
    if not adresse:
        raise ValueError("ESUP_URL n'est pas configurée")
    # Les identifiants numériques servent également de noms de dossiers.
    demande = str(int(identifiant_demande))
    etape = str(int(identifiant_etape))
    dossier = Path(dossier_documents) / demande / etape
    dossier.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = dossier / "document.pdf"
    url = adresse + "/ws/signrequests/get-last-file/" + demande
    requete = Request(url, headers={"Accept": "application/pdf"})
    temporaire = None
    try:
        with urlopen(requete, timeout=30) as reponse:
            # Vérification simple du format : ne pas enregistrer une page HTML
            # de connexion ou une erreur JSON sous le nom document.pdf.
            entete = reponse.read(5)
            if entete != b"%PDF-":
                raise ValueError("La réponse ESUP n'est pas un PDF")
            # Un nom temporaire unique évite les collisions entre deux réceptions.
            # Il est créé dans le même dossier, avec des permissions privées.
            with tempfile.NamedTemporaryFile(dir=dossier, suffix=".tmp", delete=False) as fichier:
                temporaire = Path(fichier.name)
                fichier.write(entete)
                shutil.copyfileobj(reponse, fichier)
                taille = fichier.tell()
            longueur = reponse.headers.get("Content-Length")
            if longueur is not None and taille != int(longueur):
                raise ValueError("Le téléchargement du PDF est incomplet")

        # Un fichier PDF complet se termine par %%EOF (suivi éventuellement
        # d'espaces ou de retours à la ligne). Ce contrôle reste sommaire.
        with temporaire.open("rb") as fichier:
            fichier.seek(max(0, taille - 1024))
            if not fichier.read().rstrip().endswith(b"%%EOF"):
                raise ValueError("La fin du PDF est manquante")
        # Remplacer seulement une fois le téléchargement terminé et vérifié.
        # Une reprise récupère de nouveau le document courant d'ESUP.
        temporaire.replace(destination)
        return str(destination)
    finally:
        if temporaire is not None:
            temporaire.unlink(missing_ok=True)
