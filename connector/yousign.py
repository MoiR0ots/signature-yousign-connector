"""Client Yousign : créer, activer, consulter et télécharger via son API."""

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ADRESSE_API = os.environ.get("YOUSIGN_API_URL", "https://api-sandbox.yousign.app/v3").rstrip("/")


def verifier_configuration():
    """Contrôler la configuration sans interroger le réseau ni la base."""
    if ADRESSE_API not in ("https://api-sandbox.yousign.app/v3", "https://api.yousign.app/v3"):
        raise ValueError("YOUSIGN_API_URL doit désigner l'API Sandbox ou production Yousign")
    if not os.environ.get("YOUSIGN_API_KEY"):
        raise ValueError("YOUSIGN_API_KEY n'est pas configurée")


def appeler_api(methode, chemin, donnees=None, fichier=None):
    """Envoyer du JSON, ou un PDF en multipart, et lire la réponse JSON."""
    cle = os.environ.get("YOUSIGN_API_KEY", "")
    if not cle:
        raise ValueError("YOUSIGN_API_KEY n'est pas configurée")
    entetes = {"Authorization": "Bearer " + cle, "Accept": "application/json"}
    corps = None
    if fichier is not None:
        # Multipart est le format exigé pour envoyer un fichier binaire.
        # Le PDF du POC est petit : construire ce corps en mémoire suffit.
        separateur = uuid.uuid4().hex
        debut = (
            f"--{separateur}\r\nContent-Disposition: form-data; name=\"nature\"\r\n\r\nsignable_document\r\n"
            f"--{separateur}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"document.pdf\"\r\n"
            "Content-Type: application/pdf\r\n\r\n"
        )
        corps = debut.encode() + Path(fichier).read_bytes() + f"\r\n--{separateur}--\r\n".encode()
        entetes["Content-Type"] = "multipart/form-data; boundary=" + separateur
    elif donnees is not None:
        corps = json.dumps(donnees).encode()
        entetes["Content-Type"] = "application/json"
    requete = Request(ADRESSE_API + chemin, data=corps, headers=entetes, method=methode)
    try:
        with urlopen(requete, timeout=30) as reponse:
            return json.load(reponse)
    except HTTPError as erreur:
        # Ne jamais journaliser la requête, son en-tête d'authentification ou le
        # corps complet d'une réponse contenant des informations personnelles.
        raise ValueError(f"Yousign répond HTTP {erreur.code} sur {chemin}") from None


def preparer_signataires(observation):
    """Traduire les placements simples ESUP pour les champs Yousign."""
    signataires = []
    for personne in observation["signataires"]:
        # Le circuit choisit les destinataires de Yousign : un compte ESUP
        # interne (ldap) peut signer ici comme un compte externe.
        if personne["deja_signe"]:
            raise ValueError("Un destinataire a déjà signé la dernière étape")
        if not personne["prenom"] or not personne["nom"] or not personne["email"]:
            raise ValueError("Identité incomplète pour un signataire")
        champs = []
        for emplacement in personne["emplacements"]:
            # Ces autres cas demandent une conversion spécifique, hors premier POC.
            if (emplacement["document"] != 0 or emplacement["rotation"] != 0
                    or emplacement["toutes_les_pages"] or emplacement["champ_pdf"]):
                raise ValueError("Placement non pris en charge : rotation, champ PDF ou plusieurs documents/pages")
            if emplacement.get("echelle", 1) != 1:
                raise ValueError("Une échelle de signature différente de 1 n'est pas encore prise en charge")
            champ = {
                "type": "signature", "page": int(emplacement["page"]),
                "x": int(emplacement["x"]), "y": int(emplacement["y"]),
                "width": int(emplacement["largeur"]), "height": int(emplacement["hauteur"]),
            }
            # ESUP et Yousign utilisent le coin supérieur gauche et, avec le
            # fixFactor ESUP par défaut (1), la même unité de placement.
            if champ["page"] < 1 or champ["x"] < 0 or champ["y"] < 0:
                raise ValueError("Page ou coordonnées incorrectes")
            if not 85 <= champ["width"] <= 2000 or not 37 <= champ["height"] <= 1000:
                raise ValueError("Dimensions du champ incompatibles avec Yousign")
            champs.append(champ)
        if not champs:
            raise ValueError("Un signataire n'a aucun emplacement")
        signataires.append({
            "id_esup": personne["id_destinataire"],
            "info": {"first_name": personne["prenom"], "last_name": personne["nom"],
                     "email": personne["email"], "locale": "fr"},
            "fields": champs,
        })
    if not signataires:
        raise ValueError("Aucun signataire")
    return signataires


def creer_demande(demande, etape):
    """Créer un brouillon ; son activation est une opération distincte."""
    return appeler_api("POST", "/signature_requests", {
        "name": f"ESUP {demande} - étape {etape}",
        "external_id": f"esup-{demande}-etape-{etape}",
        "delivery_mode": "email",
        "ordered_signers": False,
    })


def ajouter_document(identifiant_demande, pdf):
    return appeler_api("POST", "/signature_requests/" + identifiant_demande + "/documents", fichier=pdf)


def ajouter_signataire(identifiant_demande, identifiant_document, personne):
    champs = []
    for champ in personne["fields"]:
        champs.append(dict(champ, document_id=identifiant_document))
    return appeler_api("POST", "/signature_requests/" + identifiant_demande + "/signers", {
        "info": personne["info"], "fields": champs,
        "signature_level": "electronic_signature", "signature_authentication_mode": "no_otp",
    })


def lire_demande(identifiant):
    return appeler_api("GET", "/signature_requests/" + identifiant)


def activer_demande(identifiant):
    chemin = "/signature_requests/" + identifiant
    return appeler_api("POST", chemin + "/activate", {})


def lire_signataire(demande, signataire):
    return appeler_api("GET", "/signature_requests/" + demande + "/signers/" + signataire)


def telecharger_pdf_signe(identifiant_demande, identifiant_document, destination):
    """Conserver le PDF Yousign dans un fichier distinct du PDF ESUP d'origine."""
    url = ADRESSE_API + "/signature_requests/" + identifiant_demande + "/documents/" + identifiant_document + "/download"
    requete = Request(url, headers={
        "Authorization": "Bearer " + os.environ["YOUSIGN_API_KEY"], "Accept": "application/pdf",
    })
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporaire = None
    try:
        with urlopen(requete, timeout=30) as reponse:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as fichier:
                temporaire = Path(fichier.name)
                shutil.copyfileobj(reponse, fichier)
            longueur = reponse.headers.get("Content-Length")
            if longueur is not None and temporaire.stat().st_size != int(longueur):
                raise ValueError("Le PDF signé est incomplet")
        with temporaire.open("rb") as fichier:
            if fichier.read(5) != b"%PDF-":
                raise ValueError("La réponse Yousign n'est pas un PDF")
            fichier.seek(max(0, temporaire.stat().st_size - 1024))
            if not fichier.read().rstrip().endswith(b"%%EOF"):
                raise ValueError("La fin du PDF signé est manquante")
        temporaire.replace(destination)
    finally:
        if temporaire is not None:
            temporaire.unlink(missing_ok=True)
