# POC de connexion ESUP-Signature / Yousign

## Objectif

Permettre aux utilisateurs de préparer leur parapheur dans ESUP-Signature,
de réaliser les étapes internes habituelles, puis de confier la dernière étape
à Yousign. Le document final revient dans le parapheur ESUP existant.

## Parcours

1. Création du parapheur dans ESUP, choix des signataires et placement des signatures.
2. Réalisation des étapes internes dans ESUP.
3. À la dernière étape, création et activation automatiques de la demande Yousign,
   avec le PDF contenant les signatures internes et les emplacements prévus.
4. Envoi des invitations par mail par Yousign, puis signature par les destinataires.
5. Récupération du PDF final, ajout comme nouvelle version signée dans ESUP et
   clôture du parapheur.

Les signataires de la dernière étape peuvent être des utilisateurs internes ou
externes. Après l'envoi à Yousign, les modifications ESUP ne sont pas synchronisées :
le document, les signataires et les emplacements doivent rester inchangés.

## Installation

Le connecteur est une application autonome, installée sur la même VM qu'ESUP.
Le playbook installe son service et son traitement périodique. Il ajoute aussi
une API de retour dans ESUP et une méthode de clôture dans `SignBookService.java`,
puis recompile ESUP. L'installation nécessite donc un arrêt d'ESUP pendant la
compilation. La version de l'extension est actuellement prévue pour ESUP 1.37.29.

Les correspondances ESUP/Yousign, le contexte initial et la progression sont
conservés dans SQLite. L'API de retour refuse un parapheur modifié depuis l'envoi
et reconnaît un import déjà effectué pour éviter une deuxième version identique.

## État du POC et validation en test

Le parcours complet a été réalisé sur envdev en Sandbox : activation Yousign,
signature, retour du PDF, ajout d'une version signée et clôture ESUP.

La prochaine étape est de reproduire ce parcours sur la VM de test avec les
documents et circuits représentatifs des métiers.

Le connecteur prépare une seule invitation Yousign par signataire pour tous les
PDF d’un parapheur avec une dernière étape commune. Chaque signataire doit avoir
un emplacement sur chaque PDF. Les placements complexes sont hors périmètre.
Le retour multi-PDF est couvert par des tests automatisés et reste à vérifier
de bout en bout sur l’environnement cible.

Les points ouverts sont l'absence de l'apparence de signature Sandbox dans
l'aperçu ESUP malgré sa présence dans le PDF téléchargé, la livraison des mails
à vérifier sur l'environnement cible, ainsi que les alertes et certaines reprises
après erreur. Le timer reprend les échecs avant envoi après cinq minutes. La vue SQLite
`dossiers_a_verifier` présente les motifs et les opérations à vérifier.

Cette validation Sandbox porte sur le parcours technique. Elle ne constitue pas
une validation de la signature en environnement Yousign de production.
