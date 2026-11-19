"""Entrée HTTP : valider les notifications ESUP, les stocker et répondre."""

import sqlite3
from fastapi import FastAPI, HTTPException
from connector import donnees

app = FastAPI(title="Connecteur ESUP / Yousign", version="0.2.0")


def preparer_evenement(notification):
    """Extraire les cinq champs envoyés par ESUP dans le JSON du POST."""
    # Ces trois informations sont indispensables pour comprendre la notification.
    for nom_champ in ("signRequestId", "status", "step"):
        valeur = notification.get(nom_champ)
        if valeur is None or valeur == "":
            raise HTTPException(status_code=400, detail="Champ manquant : " + nom_champ)
        if not isinstance(valeur, (str, int)) or isinstance(valeur, bool):
            raise HTTPException(status_code=400, detail="Champ incorrect : " + nom_champ)

    # Les clés restent en anglais car ce sont les noms envoyés par ESUP.
    # Convertir les numéros en texte donne le même format à chaque réception.
    evenement = {
        "signRequestId": str(notification["signRequestId"]),
        "status": str(notification["status"]),
        "step": str(notification["step"]),
        "userEppn": notification.get("userEppn", ""),
        "comment": notification.get("comment", ""),
    }
    for nom_champ in ("userEppn", "comment"):
        if evenement[nom_champ] is None:
            evenement[nom_champ] = ""
        elif not isinstance(evenement[nom_champ], str):
            raise HTTPException(status_code=400, detail="Champ incorrect : " + nom_champ)
    return evenement


@app.get("/")
def afficher_etat():
    """Confirmer que le serveur HTTP répond, sans interroger les autres services."""
    return {"status": "ok", "version": "0.2.0"}


@app.post("/esup/events")
def recevoir_evenement_post(donnees_recues: dict):
    """Acquitter seulement après l'enregistrement durable de la notification."""
    evenement = preparer_evenement(donnees_recues)
    try:
        # Une réception constitue une opération complète, indépendante du timer.
        donnees.initialiser_base()
        return donnees.enregistrer_evenement(evenement)
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="Impossible d'enregistrer la notification")
