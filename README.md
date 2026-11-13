# Connecteur ESUP / Yousign

Le connecteur reçoit les notifications des circuits ESUP configurés pour lui.
Il envoie uniquement leur **dernière étape** à Yousign, avec le PDF contenant les
signatures internes précédentes, puis récupère le document une fois signé.
La réintégration du PDF final et la clôture du parapheur ESUP restent à développer.

## Organisation du code

| Fichier | Responsabilité |
| --- | --- |
| `connector/app.py` | Recevoir les POST, valider les cinq champs, enregistrer la notification et répondre. Aucun appel aux API. |
| `connector/donnees.py` | Créer et utiliser SQLite. Toutes les requêtes SQL sont ici ; aucun appel réseau. |
| `connector/esup.py` | Lire une demande et télécharger son PDF via l'API ESUP. Aucun accès SQLite ni décision d'envoi. |
| `connector/yousign.py` | Construire les requêtes Yousign : brouillon, document, signataires, activation, statut et téléchargement. Aucun accès SQLite. |
| `connector/traitement.py` | Organiser le parcours, décider si l'étape est la dernière, gérer les reprises et enregistrer la progression. |
| `connector/__init__.py` | Déclarer le paquet Python ; aucune logique applicative. |

Pour comprendre le déroulement métier, lire `traitement.py`. Les noms Python sont
en français ; les clés JSON ESUP/Yousign gardent leurs noms officiels.
La réception HTTP et le traitement périodique sont deux programmes distincts.
Les tables sont initialisées au début de chaque réception, de chaque cycle ou
de chaque commande de diagnostic, puis utilisées sans initialisation répétée
à chaque lecture ou écriture.

## Déroulement

1. ESUP envoie un POST à `http://127.0.0.1:8001/esup/events`. `app.py` valide le
   contenu, l'enregistre par `donnees.py` et répond HTTP 200 après le commit.
   Un doublon retrouve le même événement, sans modifier son état.
2. Au prochain passage, `traitement.py` lit la dernière notification de chaque
   demande à traiter et consulte ESUP avec `esup.py`.
3. Il vérifie que la demande est `pending`, que l'étape annoncée correspond à
   l'étape réelle et que cette étape est la dernière de la liste ordonnée du circuit.
   Aucun nombre d'étapes n'est imposé : des vérificateurs peuvent être ajoutés avant.
4. Si l'API semble en retard, il effectue trois lectures maximum espacées de cinq
   secondes. Un décalage persistant reste `a_reprendre` pour le prochain passage.
5. Pour la dernière étape, il récupère les destinataires, leurs placements et le PDF
   courant. Les comptes ESUP internes (`ldap`) et externes sont acceptés.
6. Il prépare un unique brouillon Yousign par demande et étape, en mémorisant chaque
   identifiant obtenu, puis active la demande. Avec `email`, Yousign envoie les
   invitations ; le connecteur n'utilise pas le SMTP ESUP.
7. Les passages suivants consultent le statut. En `done`, le PDF signé est conservé
   séparément du PDF d'origine. Une erreur de téléchargement sera retentée.

Le timer systemd déclenche un passage après la fin du précédent. Un verrou empêche
également deux passages manuels simultanés. Aucun traitement API n'est exécuté
pendant la réception du POST.

## Les cinq données envoyées par ESUP

Ces informations arrivent dans le JSON du POST. Ce ne sont pas les informations
complètes du circuit : le traitement doit ensuite consulter l'API ESUP.

| Champ | Signification et usage |
| --- | --- |
| `signRequestId` | Identifiant de la demande ESUP (`SignRequest`), par exemple `"243"`. Il permet de consulter l'API. Ce n'est pas l'identifiant du parapheur (`SignBook`) ni celui de l'événement local. |
| `status` | État ou action au moment de l'émission : `pending`, `completed`, `refused`, `deleted`, `cleaned`, `restored`. Le traitement d'envoi s'intéresse à `pending`. |
| `step` | Numéro d'ordre de l'étape annoncé par ESUP, par exemple `"2"`, ou `"end"` en fin de circuit. Il n'indique pas si l'étape est la dernière et n'est pas son identifiant technique. |
| `userEppn` | Utilisateur à l'origine de l'action, ou compte technique tel que `scheduler`. Ce n'est pas forcément un signataire ou une adresse mail. |
| `comment` | Texte associé, par exemple un motif de refus. Il peut être vide. |

```json
{
  "signRequestId": "243",
  "status": "pending",
  "step": "2",
  "userEppn": "utilisateur@exemple.fr",
  "comment": ""
}
```

Les trois premiers champs sont obligatoires ; les deux autres valent une chaîne
vide lorsqu'ils sont absents. Les champs inconnus sont ignorés. ESUP envoie aussi
un GET avant le POST : il est refusé avec HTTP 405 et n'écrit rien dans SQLite.
Le POST réussi suffit à acquitter l'envoi ESUP. `GET /` vérifie simplement que le
serveur HTTP répond ; il ne vérifie ni SQLite ni les API.

## Données persistantes et reprises

`CONNECTOR_DB` désigne SQLite. Sans cette variable, la base se trouve dans
`data/events.sqlite3` à la racine du projet.

| Table | Contenu |
| --- | --- |
| `events` | Notification JSON unique, identifiant local, état et date UTC de première réception. |
| `observations` | Dernière analyse ESUP par événement, signataires, placements et chemin du PDF courant. Une nouvelle consultation remplace l'observation. |
| `brouillons_yousign` | Suivi unique par demande ESUP et étape : empreinte des paramètres/PDF, identifiants Yousign, phase en cours, statut, liens privés et chemin du PDF signé. |
| `configuration_yousign` | URL API liée à cette base, pour éviter de mélanger Sandbox et production. |

Les états d'événement principaux sont : `received`, `ignoree`, `a_reprendre`,
`erreur`, `a_envoyer`, `transmis_yousign` et `erreur_yousign`. Une consultation
manuelle sans automatisation conserve `derniere_etape` au lieu de `a_envoyer`.

Le traitement reprend automatiquement `received`, `a_reprendre` et `erreur`.
Il ne réactive pas une demande déjà en cours, ni un ancien brouillon créé
manuellement. Une demande terminée dont le PDF existe n'est plus interrogée.
Les états de refus, annulation et expiration ne produisent pas un document présenté
comme entièrement signé.

**À compléter avant production :** une création au résultat incertain conserve
`en_cours` et bloque les nouveaux POST pour éviter les doublons. Une activation
refusée conserve `activation_bloquee` et son erreur. Ces cas nécessitent encore
une vérification manuelle des ressources distantes avant reprise. Il reste à
prévoir leur réconciliation et une alerte en dehors des logs.

Une modification ESUP avant création est prise en compte par la relecture du
parapheur. Une modification après préparation Yousign n'est pas synchronisée :
le changement d'empreinte bloque la réutilisation silencieuse du brouillon.

## PDF et emplacements

Les fichiers sont conservés à côté de la base :

```text
documents/<id_demande>/<id_etape>/document.pdf         # PDF courant ESUP
documents/<id_demande>/<id_etape>/document-yousign.pdf # résultat Yousign
```

Le téléchargement se fait dans un fichier temporaire privé, renommé une fois
terminé. Le début `%PDF-`, la fin `%%EOF` et la longueur annoncée si disponible
sont contrôlés. Ce n'est pas une validation cryptographique des signatures.
Un échec préserve un éventuel ancien PDF. Les fichiers ne sont pas purgés automatiquement.

Les coordonnées partent du coin supérieur gauche dans les deux systèmes.
Le POC suppose `fixFactor=1` côté ESUP, une échelle de signature de 1, un PDF sans
rotation, le document 0 et des placements explicites sans option toutes-pages ni
champ PDF préexistant. Les cas complexes sont refusés. Un emplacement générique
sans destinataire est accepté seulement si l'étape contient une seule personne.
Le rendu Yousign reste à valider visuellement, notamment le ratio des signatures.
Les essais utilisent une signature simple (`electronic_signature`) sans OTP (`no_otp`).

## Installation par Ansible

Le déploiement est dans `../playbooks/svc-signature-yousignconnector/` :
`installer.yml`, `tasks/`, `templates/`, `variables.yml` et `inventaire/`.
Il crée le compte système, l'environnement Python, le service HTTP et le timer,
sans modifier ESUP ni Apache. Le connecteur reste sur la même VM qu'ESUP.

```bash
aonsible run dev playbook ~/playbooks/svc-signature-yousignconnector/installer.yml
```

Le playbook récupère les sources depuis
`git@bonsai.localsi:pszczolk/signature-yousign-connector.git`.
L'accès Git SSH de root doit être configuré sur la cible. `connecteur_version_git`
permet de choisir une branche, un tag ou un commit (`HEAD` par défaut).

| Variable | Rôle / valeur par défaut |
| --- | --- |
| `connecteur_hotes` | Cible Ansible : `vm-signature.envdev.test`. |
| `connecteur_destination` | Code et environnement Python : `/opt/youSignConnector`. |
| `connecteur_donnees` | Données persistantes : `/var/lib/yousign-connector`. |
| `connecteur_utilisateur` / `connecteur_service` | Compte et service : `yousign-connector`. |
| `connecteur_adresse` / `connecteur_port` | Écoute HTTP : `127.0.0.1` / `8001`. |
| `connecteur_esup_url` | API ESUP locale : `http://127.0.0.1:8080`. L'accès par IP doit être autorisé côté ESUP. |
| `connecteur_yousign_api_url` | URL API avec `/v3`, Sandbox par défaut. |
| `connecteur_yousign_automatique` | Active le traitement et son timer. |
| `connecteur_yousign_intervalle` | Intervalle en secondes après la fin d'un passage. 120 dans notre inventaire Sandbox pour limiter les appels. |
| `connecteur_yousign_livraison` | `email` envoie les invitations ; `none` conserve les liens dans SQLite. |
| `connecteur_fichier_secrets` | Fichier privé généré : `/etc/yousign-connector/secrets.env`. |
| `connecteur_environnement_installation` | Proxy éventuel pour apt, pip et Git. |
| `connecteur_yousign_proxy` | Proxy HTTPS éventuel du traitement vers Yousign. |

`variables.yml` fournit les valeurs générales. `inventaire/signature-dev_main.yml`
les complète pour notre VM, avec une seule valeur Vault : `connecteur_yousign_api_key`.
Le mot de passe Vault reste hors du dépôt. Pour chiffrer une nouvelle valeur sans
mettre la clé dans l'historique du terminal :

```bash
ansible-vault encrypt_string --stdin-name connecteur_yousign_api_key
```

Le template `secrets.env.j2` écrit `YOUSIGN_API_KEY` dans un fichier root `0600`,
dans un dossier `0700`. La tâche utilise `no_log: true` et `diff: false`.
Il n'est pas nécessaire de créer ce fichier à la main. Le traitement le charge
au prochain passage. Le service HTTP ne reçoit pas la clé.

Pour une autre VM, préparer un fichier de configuration et le sélectionner :

```bash
aonsible run dev playbook ~/playbooks/svc-signature-yousignconnector/installer.yml \
  -e connecteur_inventaire=inventaire/signature-prod_main.yml
```

Adapter aussi l'inventaire aonsible `dev` si nécessaire.
Les API officielles sont `https://api-sandbox.yousign.app/v3` et
`https://api.yousign.app/v3`. Chaque environnement nécessite sa propre clé.
Pour passer de Sandbox en production, choisir également un nouveau dossier de
données : la base refuse de changer d'API. L'ancien historique du POC est considéré
comme Sandbox s'il contient déjà des demandes Yousign.

## Lancement local et commandes de diagnostic

```bash
cd /home/vagrant/youSignConnector
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn connector.app:app --host 127.0.0.1 --port 8001 --no-access-log
```

Pour consulter un événement ou préparer un brouillon seul, configurer les variables
appropriées puis lancer :

```bash
.venv/bin/python -m connector.traitement consulter 10
.venv/bin/python -m connector.traitement preparer 10
```

`consulter` nécessite `ESUP_URL` et `CONNECTOR_DB` ; `preparer` nécessite aussi
`YOUSIGN_API_KEY` et l'URL API choisie dans `YOUSIGN_API_URL`.
L'action `preparer` n'active pas à elle seule le brouillon.
Le cycle complet se lance avec `python -m connector.traitement cycle` et
`YOUSIGN_AUTOMATIQUE=1`. Sur la VM, systemd fournit ces variables et lit le secret.

```bash
sudo systemctl start yousign-connector-traitement.service
sudo journalctl -u yousign-connector-traitement.service -n 50 --no-pager
sudo systemctl status yousign-connector-traitement.timer --no-pager
```

## Tests et documentation des API

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Les tests utilisent des bases et fichiers temporaires, avec des réponses API
fictives. Ils n'envoient ni demande de signature ni mail à de vrais destinataires.

Documentation officielle : [environnements](https://developers.yousign.com/docs/environments-new),
[création de demande](https://developers.yousign.com/reference/post-signature_requests-1),
[dépôt de document](https://developers.yousign.com/reference/post-signature_requests-signaturerequestid-documents-1)
et [coordonnées des champs](https://developers.yousign.com/docs/field-creation-with-api-endpoints).
Les domaines de documentation et d'API externes restent ceux publiés par le fournisseur.
