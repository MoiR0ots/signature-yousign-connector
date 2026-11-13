"""Client ESUP : lire les demandes et réintégrer les documents signés."""

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


def lire_demande(identifiant):
    """Consulter l'API ESUP avec la bibliothèque standard Python."""
    adresse = os.environ.get("ESUP_URL", "").rstrip("/")
    if not adresse:
        raise ValueError("ESUP_URL n'est pas configurée")
    url = adresse + "/ws/signrequests/" + quote(str(identifiant), safe="")
    entetes = {"Accept": "application/json"}
    jeton = os.environ.get("ESUP_API_KEY", "").strip()
    if jeton:
        entetes["x-api-key"] = jeton
    requete = Request(url, headers=entetes)
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
    entetes = {"Accept": "application/pdf"}
    jeton = os.environ.get("ESUP_API_KEY", "").strip()
    if jeton:
        entetes["x-api-key"] = jeton
    requete = Request(url, headers=entetes)
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


def lire_contexte_retour(identifiant):
    """Lire le contexte à conserver avant l'envoi d'un PDF au prestataire."""
    adresse = os.environ.get("ESUP_URL", "").rstrip("/")
    jeton = os.environ.get("ESUP_API_KEY", "").strip()
    if not adresse or not jeton:
        raise ValueError("ESUP_URL et ESUP_API_KEY sont nécessaires pour lire le contexte de retour")
    url = adresse + "/ws/signrequests/" + quote(str(identifiant), safe="") + "/external-completion/context"
    requete = Request(url, headers={"Accept": "application/json", "x-api-key": jeton})
    with urlopen(requete, timeout=10) as reponse:
        return json.load(reponse)


def envoyer_document_signe(identifiant, contexte, reference, document):
    """Ajouter le PDF final comme version signée et valider la dernière étape."""
    adresse = os.environ.get("ESUP_URL", "").rstrip("/")
    jeton = os.environ.get("ESUP_API_KEY", "").strip()
    if not adresse or not jeton:
        raise ValueError("ESUP_URL et ESUP_API_KEY sont nécessaires pour le retour du PDF")

    # L'API attend un formulaire multipart : trois champs texte et le PDF.
    # Seule la frontière du formulaire change en cas de nouvel essai ; le PDF,
    # le contexte initial et la référence métier restent identiques.
    frontiere = "esup-" + uuid.uuid4().hex
    morceaux = []
    champs = {"expectedStepId": contexte["expectedStepId"],
              "contextHash": contexte["contextHash"], "reference": reference}
    for nom, valeur in champs.items():
        morceaux.append(("--" + frontiere + "\r\n"
                         'Content-Disposition: form-data; name="' + nom + '"\r\n\r\n'
                         + str(valeur) + "\r\n").encode())
    morceaux.append(("--" + frontiere + "\r\n"
                     'Content-Disposition: form-data; name="file"; filename="document-yousign.pdf"\r\n'
                     "Content-Type: application/pdf\r\n\r\n").encode())
    morceaux.append(Path(document).read_bytes())
    morceaux.append(("\r\n--" + frontiere + "--\r\n").encode())
    url = adresse + "/ws/signrequests/" + quote(str(identifiant), safe="") + "/external-completion"
    requete = Request(url, data=b"".join(morceaux), method="POST", headers={
        "Accept": "application/json", "x-api-key": jeton,
        "Content-Type": "multipart/form-data; boundary=" + frontiere,
    })
    with urlopen(requete, timeout=30) as reponse:
        return json.load(reponse)
