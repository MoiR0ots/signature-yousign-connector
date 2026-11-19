# Connecteur ESUP / Yousign

Le connecteur reçoit les notifications des circuits ESUP configurés pour lui.
Il envoie uniquement leur **dernière étape** à Yousign, avec le PDF contenant les
signatures internes précédentes, puis réintègre le document signé comme nouvelle
version dans ESUP et valide la dernière étape. Le parapheur est clôturé lorsque
toutes ses demandes sont terminées. Ce retour nécessite l'extension ESUP installée
par le playbook ; le parcours complet reste à valider sur une nouvelle demande de test.

Une [fiche de présentation du POC](docs/presentation-poc.md) résume le parcours
et le périmètre de la validation en test.

## Consigne impérative à communiquer aux métiers

**Dès l'arrivée à la dernière étape, l'envoi à Yousign est automatique. À partir
de cet envoi, ne plus modifier dans ESUP les signataires, leurs emplacements,
le document ou la structure de cette dernière étape.** Préparer ces éléments
avant de terminer l'étape interne précédente.

**ESUP peut encore permettre ces modifications à l'écran : cela ne signifie pas
qu'elles sont répercutées dans Yousign. Aucune synchronisation automatique des
modifications après envoi n'est implémentée.** Un signataire retiré dans ESUP
pourrait donc toujours disposer de sa demande de signature Yousign.

Si une correction est nécessaire après l'envoi, interrompre le parcours et faire
annuler la demande Yousign avant d'organiser un nouvel envoi. La procédure
d'annulation et de recréation n'est pas encore automatisée dans le connecteur.
Une modification dans ESUP seule ne suffit pas à annuler la demande Yousign.

Avant l'envoi, le connecteur conserve le contexte fourni par l'extension ESUP :
demande, étape, document source et empreinte des destinataires et placements.
Au retour, l'extension compare ce contexte à l'état courant et refuse l'import
en cas de divergence. **L'édition dans ESUP n'est pas verrouillée.** Un refus
d'import nécessite une vérification manuelle ; il ne corrige ni ESUP ni Yousign.

## Organisation du code

| Fichier | Responsabilité |
| --- | --- |
| `connector/app.py` | Recevoir les POST, valider les cinq champs, enregistrer la notification et répondre. Aucun appel aux API. |
| `connector/donnees.py` | Lire et écrire les données SQLite. Le fichier `connector/schema.sql` définit les tables et la vue. |
| `connector/esup.py` | Lire une demande, son PDF et son contexte, puis envoyer le PDF signé à l'API ESUP. Aucun accès SQLite ni décision d'envoi. |
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
6. Il vérifie la cohérence du PDF et des signataires avec le contexte ESUP, qu'il
   sauvegarde dans SQLite avant tout envoi. Il prépare un unique brouillon Yousign
   par demande et étape, en mémorisant chaque
   identifiant obtenu, puis active la demande. Yousign envoie systématiquement les
   invitations ; le connecteur n'utilise pas le SMTP ESUP.
7. Les passages suivants consultent le statut. En `done`, le PDF signé est conservé
   séparément du PDF d'origine. Une erreur de téléchargement sera retentée.
8. Il envoie ce PDF à `POST /ws/signrequests/{id}/external-completion`, avec le
   contexte initial et la référence stable `yousign:<identifiant Yousign>`.
   ESUP ajoute la version signée, valide la dernière étape et clôture le parapheur
   si toutes ses demandes sont terminées. La confirmation est conservée dans SQLite.

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
| `events` | Notification ESUP. Les cinq colonnes demande, statut, numéro d’étape, acteur et commentaire identifient un doublon. |
| `observations` | Dernière analyse par événement, règles de l’étape, motif, chemin du PDF courant et date UTC. |
| `destinataires_observes` | Destinataires lus dans ESUP pour cet événement, avec leur identité et leur état de signature. |
| `emplacements` | Une ligne par zone du destinataire observé, avec le document, la page, les coordonnées, les dimensions et les options de placement. |
| `brouillons_yousign` | Une ligne par demande ESUP et étape. Elle porte le contexte initial, les identifiants du brouillon et du document, la progression, les erreurs et la confirmation du retour du PDF. |
| `signataires` | Correspondance entre destinataire ESUP et signataire Yousign, statut individuel et lien privé. |
| `configuration_yousign` | URL de l’API liée à la base pour distinguer Sandbox et production. |

Pour passer au nouveau schéma en développement, arrêter le timer et les services
du connecteur, supprimer l’ancienne base `events.sqlite3`, puis redémarrer le
service HTTP et le timer. Le connecteur crée les tables au premier cycle ou à
la première notification.

Pour consulter les signataires et leurs liens dans DBeaver, filtrer la table
`signataires` sur la colonne `demande`. Les colonnes `demande` et `etape` la relient
à `brouillons_yousign`. Les emplacements se retrouvent par `event_id` et `id_destinataire`.

Les états d'événement principaux sont : `received`, `ignoree`, `a_reprendre`,
`erreur`, `a_envoyer`, `transmis_yousign` et `erreur_yousign`. Une dernière étape
validée passe toujours à `a_envoyer`.

Le traitement reprend automatiquement `received`, `a_reprendre` et `erreur`.
Il ne réactive pas une demande déjà en cours. Une demande terminée n'est plus interrogée lorsque son PDF existe
et que son import ESUP a été confirmé.
Les états de refus, annulation et expiration ne produisent pas un document présenté
comme entièrement signé.

Si l'appel de retour échoue, le prochain passage renvoie le même PDF, la même
référence et le même contexte initial. Si ESUP avait déjà traité l'import mais
que sa réponse avait été perdue, il répond `duplicate: true` sans ajouter une
seconde version. La table `brouillons_yousign` conserve cette confirmation, notamment
l'identifiant du document et le statut du parapheur. Un conflit HTTP 409 signifie
qu'il faut vérifier manuellement le parapheur : le connecteur ne renouvelle pas
le contexte pour forcer l'import. Les erreurs restent visibles dans le journal.

**À compléter avant production :** une création au résultat incertain conserve
`en_cours` et bloque les nouveaux POST pour éviter les doublons. Une activation
refusée conserve `activation_bloquee` et son erreur. Ces cas nécessitent encore
une vérification manuelle des ressources distantes avant reprise. Il reste à
prévoir leur réconciliation et une alerte en dehors des logs.

Dans DBeaver, ouvrir la vue `dossiers_a_verifier` ou exécuter :

```sql
SELECT * FROM dossiers_a_verifier ORDER BY demande;
```

Avant tout envoi, le motif et la prochaine tentative (UTC) sont visibles dans la
vue. Le timer relit ESUP après cinq minutes, y compris pour les anciennes erreurs
sans réservation. Une correction dans ESUP suffit pour reprendre. Une notification
plus récente prend la priorité. Une préparation incertaine ou une activation
refusée reste visible avec sa phase et nécessite une vérification. Les erreurs
de suivi ordinaires sont retentées au prochain cycle. Les lignes disparaissent
quand le problème est résolu. Aucun paramètre de messagerie à configurer.

Pendant la signature, le suivi conserve les statuts individuels disponibles et
relit les liens manquants des destinataires en attente. Un lien peut rester vide
avec la livraison par mail. Le diagnostic des refus HTTP conserve leur type API
quand Yousign le fournit.

Une modification ESUP avant création est prise en compte par la relecture du
parapheur. Une modification après préparation Yousign n'est pas synchronisée :
le changement de contexte ESUP bloque la réutilisation silencieuse du brouillon.

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
`installer.yml`, `tasks/`, `templates/`, `defauts.yml` et `inventaire/`.
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
| `connecteur_port` | Port HTTP : `8001`. Écoute fixée à `127.0.0.1`, sur la même VM qu’ESUP. |
| `connecteur_esup_url` | API ESUP locale : `http://127.0.0.1:8080`. L'accès par IP doit être autorisé côté ESUP. |
| `connecteur_yousign_api_url` | URL API avec `/v3`, définie dans l’inventaire. |
| `connecteur_yousign_intervalle` | Intervalle en secondes après la fin d'un passage. 120 dans notre inventaire Sandbox pour limiter les appels. |
| `connecteur_fichier_secrets` | Fichier privé généré : `/etc/yousign-connector/secrets.env`. |
| `connecteur_environnement_installation` | Proxy éventuel pour apt, pip et Git. |
| `connecteur_yousign_proxy` | Proxy HTTPS éventuel du traitement vers Yousign. |

`defauts.yml` fournit les valeurs générales. `inventaire/signature-dev_main.yml`
les complète pour notre VM : cible, URL API, proxy et intervalle Sandbox,
avec deux valeurs Vault : `connecteur_yousign_api_key` et `connecteur_esup_api_key`.
Le mot de passe Vault reste hors du dépôt. Pour chiffrer une nouvelle valeur sans
mettre la clé dans l'historique du terminal :

```bash
ansible-vault encrypt_string --stdin-name connecteur_yousign_api_key
```

Le template `secrets.env.j2` écrit `YOUSIGN_API_KEY` et `ESUP_API_KEY` dans un fichier root `0600`,
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
données : la base refuse de changer d'API.

## Lancement local et commandes de diagnostic

```bash
cd /home/vagrant/youSignConnector
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn connector.app:app --host 127.0.0.1 --port 8001 --no-access-log
```

Pour consulter un événement enregistré, configurer `ESUP_URL`, `ESUP_API_KEY`
et `CONNECTOR_DB`, puis lancer :

```bash
.venv/bin/python -m connector.traitement consulter 10
```

Cette commande ne crée pas de demande Yousign. Elle met à jour l'observation et
l'état de l'événement dans SQLite ; une dernière étape valide devient `a_envoyer`
et pourra donc être traitée au prochain passage du timer.
La préparation du brouillon est réservée au parcours automatique.
Le cycle complet se lance avec `python -m connector.traitement cycle`.
Le traitement est toujours automatique ; aucune variable d'activation n'est
nécessaire. Sur la VM, systemd fournit la configuration et lit les secrets.

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


Le jeton ESUP `connecteur_esup_api_key` est distinct de la clé Yousign. Il est
transmis dans l'en-tête `x-api-key` lors des lectures ESUP si configuré, et requis
par `esup.lire_contexte_retour()` pour la nouvelle API. Il doit autoriser la
modification du circuit concerné. Pour renouveler seulement les secrets :

```bash
aonsible run dev playbook ~/playbooks/svc-signature-yousignconnector/installer.yml \
  --tags secrets_connecteur
```

Le traitement conserve le contexte avant création Yousign et appelle l'API de
retour avec ce contexte initial après récupération du PDF signé.
