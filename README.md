# Connecteur ESUP / Yousign — première version

## À régler avant la mise en production : reprise des événements

**La reprise automatique n'est pas encore implémentée.** Après trois consultations
espacées de 5 secondes, un décalage persistant avec ESUP laisse l'événement en
`a_reprendre`. Cet état ne déclenche aucun traitement supplémentaire : sans reprise
manuelle ou nouvelle notification identique, la demande reste bloquée indéfiniment
côté connecteur. Un redémarrage du service ne la reprend pas non plus.

Avant la production, il reste à :

- reprendre automatiquement les événements `a_reprendre`, ainsi que les `erreur`
  et les `received` laissés sans traitement après un arrêt ;
- signaler les événements durablement bloqués pour qu'ils ne passent pas inaperçus ;
- vérifier par un test qu'un événement est repris après une indisponibilité ESUP
  et après un redémarrage du connecteur.

Ce point reste ouvert même si les essais du POC passent grâce aux trois tentatives.

Le programme reçoit les notifications ESUP et les garde dans un fichier SQLite.
Il consulte le circuit ESUP lorsque `ESUP_URL` est configurée.
Il ne contacte pas encore Yousign et ne modifie aucun parapheur ESUP.

## Lire le code

La réception et le stockage sont dans `connector/app.py`.
La consultation et la lecture du circuit sont dans `connector/esup.py`. Les fonctions se lisent dans cet ordre :

1. `initialiser_base` crée la table si nécessaire.
2. `preparer_evenement` récupère les cinq informations envoyées par ESUP.
3. `enregistrer_evenement` retrouve ou crée l'événement.
4. Les deux routes à la fin du fichier répondent aux appels HTTP.

`try/finally` permet de fermer une connexion SQLite même si une erreur survient.
`commit()` valide les écritures ; `rollback()` les annule en cas d'erreur.
Les `?` dans les requêtes SQL sont remplacés par les valeurs passées séparément.

Les noms Python sont en français. Les clés envoyées par ESUP gardent leurs noms
d'origine pour respecter le format de ses notifications.

## Lancement depuis envdev

```bash
cd /home/vagrant/youSignConnector
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn connector.app:app --host 127.0.0.1 --port 8001 --no-access-log
```

FastAPI gère les routes et la lecture du JSON. Uvicorn est le serveur HTTP.
Nous n'utilisons plus directement Starlette dans le code.

La base est créée au premier événement, dans `data/events.sqlite3`.
On peut choisir un autre fichier avec la variable d'environnement `CONNECTOR_DB`.
Le déploiement sur une VM est décrit ci-dessous.

## Installer sur vm-signature

Le playbook `../playbooks/svc-signature-yousignconnector/installer.yml` installe le connecteur comme un service autonome.
Il ne modifie ni ESUP-Signature ni Apache. Il installe Python, crée un compte système
et un environnement Python dédié, puis active le service `yousign-connector`.

Depuis envdev :

```bash
aonsible run dev playbook /home/vagrant/playbooks/svc-signature-yousignconnector/installer.yml
```

Les variables sont regroupées et commentées dans `../playbooks/svc-signature-yousignconnector/variables.yml` :

| Variable | Valeur par défaut / rôle |
| --- | --- |
| `connecteur_hotes` | `vm-signature.envdev.test`, cible de l'inventaire Ansible. |
| `connecteur_depot_git` | `git@bonsai.localsi:pszczolk/signature-yousign-connector.git`, source unique du code. |
| `connecteur_version_git` | Branche, tag ou commit à installer ; `HEAD` suit par défaut la branche principale du dépôt. |
| `connecteur_destination` | `/opt/youSignConnector`, contenant `app/` et `venv/`. |
| `connecteur_donnees` | `/var/lib/yousign-connector`, contenant la base `events.sqlite3`. |
| `connecteur_utilisateur` | `yousign-connector`, compte système qui exécute le programme. |
| `connecteur_service` | `yousign-connector`, nom du service systemd. |
| `connecteur_adresse` / `connecteur_port` | `127.0.0.1` / `8001`, écoute locale par défaut. |
| `connecteur_environnement_installation` | Variables de proxy pour apt, pip et Git si nécessaire. |

Pour adapter l'installation, créer un fichier de variables, par exemple
`/home/vagrant/connecteur-dev.yml` :

```yaml
connecteur_destination: /opt/youSignConnector
connecteur_environnement_installation:
  http_proxy: http://vm-proxy-envdev.envdev.test:3128
  https_proxy: http://vm-proxy-envdev.envdev.test:3128
  no_proxy: localhost,127.0.0.1,.envdev.test
```

Puis le passer au playbook :

```bash
aonsible run dev playbook /home/vagrant/playbooks/svc-signature-yousignconnector/installer.yml \
  -e @/home/vagrant/connecteur-dev.yml
```

Le téléchargement Git se fait depuis la VM cible avec le compte root utilisé par
Ansible. Pour un dépôt privé, son accès doit être configuré au préalable ; ne pas
mettre de mot de passe dans l'URL du dépôt. Pour passer d'une installation locale
à Git, choisir une nouvelle `connecteur_destination` : Git ne peut pas cloner dans
le dossier `app/` déjà rempli par la copie locale. Garder `connecteur_donnees`
inchangé permet de conserver la base. Changer ce chemin ne migre pas les données.

Relancer le playbook met à jour le code et redémarre le service si nécessaire.
La base est créée à la première notification et n'est pas remplacée par le playbook.
La vérification finale appelle seulement `GET /` ; elle ne crée aucun événement.

Sur la VM cible, pour vérifier le service et consulter ses messages :

```bash
sudo systemctl status yousign-connector --no-pager
sudo journalctl -u yousign-connector -n 50 --no-pager
curl http://127.0.0.1:8001/
```

ESUP, installé sur la même VM, pourra envoyer ses notifications à
`http://127.0.0.1:8001/esup/events`. La configuration de cette cible dans ESUP reste
une étape distincte. Le connecteur peut aussi être installé sur une autre VM en
adaptant la cible Ansible et son adresse d'écoute.

## Essayer les routes

`GET /` confirme simplement que le programme répond. Il ne vérifie pas le stockage.

ESUP envoie un GET puis un POST pour une même notification. Le connecteur
accepte uniquement le POST. FastAPI refuse automatiquement le GET avec HTTP 405.
ESUP peut journaliser ce refus, mais poursuit avec le POST. L'envoi est considéré
réussi si le POST répond HTTP 200.

Pour envoyer une notification :

```bash
curl http://127.0.0.1:8001/esup/events \
  -H 'Content-Type: application/json' \
  -d '{"signRequestId":"34","status":"pending","step":"2"}'
```

Les champs `signRequestId`, `status` et `step` sont obligatoires.
`userEppn` et `comment` valent une chaîne vide lorsqu'ils sont absents.
Les autres champs sont ignorés dans cette version.

Le premier POST retourne `duplicate: false`. Un POST identique retourne
`duplicate: true` avec le même `event_id`. Les deux répondent HTTP 200.
Une erreur SQLite renvoie HTTP 503 au lieu de confirmer un enregistrement échoué.

## Les cinq données envoyées par ESUP

Ces données arrivent dans le corps JSON du POST envoyé par ESUP à notre route
`/esup/events`. À ce stade, le connecteur reçoit une notification : il consulte ensuite l’API ESUP si `ESUP_URL` est configurée.
Le PDF courant est téléchargé uniquement si la dernière étape est identifiée.

| Champ | Signification | Utilité pour le POC |
| --- | --- | --- |
| `signRequestId` | Identifiant de la demande de signature ESUP (`SignRequest`), par exemple `"34"`. Ce n'est ni l'identifiant du parapheur (`SignBook`), ni celui de l'événement enregistré dans notre base. | Retrouver la demande dans l'API ESUP pour récupérer ses informations et son PDF. |
| `status` | État ou action signalé au moment de l'envoi, par exemple `"pending"` (en attente), `"completed"` (terminé) ou `"refused"` (refusé). ESUP peut aussi envoyer `"deleted"`, `"cleaned"` et `"restored"` pour une suppression, un nettoyage ou une restauration. | Décider si l'événement nécessite un traitement. Une notification `pending` pourra déclencher la vérification de l'étape avant un éventuel envoi à Yousign. |
| `step` | Numéro de l'étape courante, par exemple `"2"`. ESUP utilise aussi `"end"` lors de l'envoi de fin de circuit. Ce numéro n'est pas l'identifiant technique de l'étape dans l'API. | Repérer l'avancement du circuit. Il faudra consulter ESUP pour connaître les destinataires et les paramètres de cette étape. |
| `userEppn` | Identifiant de l'utilisateur à l'origine de l'action. Il peut aussi désigner un traitement automatique, par exemple `"scheduler"`. Ce n'est pas nécessairement un signataire ni une adresse mail. | Comprendre l'origine de l'événement. Ce champ ne suffit pas à déterminer à qui envoyer une invitation Yousign. |
| `comment` | Texte associé à l'action, par exemple un motif de refus. Il peut être vide. | Conserver une explication utile au suivi ou au diagnostic. |

Exemple de notification complète, avec un identifiant utilisateur fictif :

```json
{
  "signRequestId": "34",
  "status": "pending",
  "step": "2",
  "userEppn": "utilisateur@exemple.fr",
  "comment": ""
}
```

ESUP envoie ces cinq valeurs sous forme de chaînes de caractères. Notre connecteur
exige les trois premières et accepte l'absence des deux dernières en les remplaçant
par une chaîne vide.

Ces données décrivent l'événement au moment où il s'est produit. Avant de créer
une demande Yousign, le connecteur devra relire l'état courant dans ESUP : le
circuit peut avoir avancé depuis la notification. Le PDF, la liste des signataires
et les emplacements des signatures ne sont pas inclus dans ces cinq champs ; ils
devront être récupérés séparément via l'API ESUP.

## Ce que conserve SQLite

La table `events` contient :

- `id` : l'identifiant de l'événement dans le connecteur ;
- `payload` : les cinq champs ESUP sous forme de JSON, avec une contrainte `UNIQUE` ;
- `state` : l'état du traitement, initialement `received` (reçu) ;
- `received_at` : la date UTC de la première réception.

Le JSON est toujours écrit avec les clés triées et sans espaces inutiles.
Deux notifications identiques donnent donc le même texte. On retrouve directement
ce texte dans SQLite, sans calculer d'empreinte SHA-256.

Un doublon retourne l'identifiant existant. Il ne change ni la date ni l'état du
traitement. Les répétitions ne sont plus conservées individuellement.
La déduplication reste effective après redémarrage grâce au fichier SQLite.

Cela déduplique les notifications reçues. Le futur traitement Yousign devra aussi
vérifier l'étape et le document concernés avant de créer une demande de signature.

## Vérifications simples

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Les tests de réception vérifient les doublons, le refus du GET, le changement
d’étape et le refus des notifications incomplètes.
Les tests utilisent une base temporaire et ne contactent jamais ESUP.
Les tests de consultation couvrent aussi la dernière étape, l’ajout d’un
vérificateur, une notification ancienne et la reprise après une erreur réseau.

Le service reste destiné à une écoute locale sur `127.0.0.1`. L'accès par Apache
sera préparé lors du déploiement. La base peut contenir des identités et commentaires.


## Observer la dernière étape dans ESUP

La variable `ESUP_URL` indique la racine de l'application ESUP, sans `/ws`.
Le playbook la configure via `connecteur_esup_url`, par défaut
`http://127.0.0.1:8080`. ESUP doit autoriser l'adresse du connecteur à consulter
son API. Ce premier jet utilise cet accès par IP, sans authentification par jeton.
Sans `ESUP_URL`, le programme conserve son fonctionnement de réception seule.

Après avoir enregistré et acquitté une notification, le connecteur consulte ESUP
pour les événements `pending`. Il compare l'identifiant de l'étape courante à
celui de la dernière étape de la liste réelle du parapheur. Il vérifie aussi que
la demande est encore en attente et que le numéro notifié correspond à l'étape
actuelle. Aucun nombre d'étapes n'est fixé dans la configuration.

Les événements proviennent des circuits auxquels la destination du connecteur
est associée dans ESUP. Il n'y a pas encore de liste supplémentaire de circuits
autorisés dans le connecteur.

Le résultat est conservé dans une table `observations` (une observation par
événement, remplacée à chaque consultation). Le champ `events.state` devient :

- `ignoree` : statut autre que pending, demande terminée, notification ancienne
  ou étape intermédiaire ;
- `derniere_etape` : dernière étape identifiée, avec ses destinataires et leurs
  emplacements ESUP bruts ; cela ne signifie pas encore « prêt pour Yousign » ;
- `a_reprendre` : l’API reste en retard après trois consultations ;
- `erreur` : consultation impossible ou réponse inexploitable, avec le type d'erreur.

Le journal affiche seulement les identifiants et le résultat. Les identités et
placements sont dans SQLite. Les coordonnées ne sont pas encore converties pour
Yousign ; le PDF courant est téléchargé, mais aucune demande n’est envoyée ou clôturée.

La consultation se fait après la réponse HTTP, dans le même processus. Ce n'est
pas une file de travail persistante : un arrêt du service peut laisser un événement
`received`. Il n’y a pas de relance périodique automatique. Une notification
doublonnée déclenche une nouvelle lecture, sans créer un nouvel événement.
Si l’API annonce une étape antérieure ou un statut pas encore `pending` (hors
statuts terminés ou refusés), le connecteur effectue au maximum trois lectures,
espacées de **5 secondes** : une lecture immédiate, puis deux nouvelles tentatives.
Si le décalage persiste, il conserve `a_reprendre` pour une reprise manuelle.
Les erreurs réseau restent marquées `erreur`, sans nouvelle tentative automatique.
Le délai s'applique après l'acquittement du POST, sans garder SQLite ouvert.

Pour relire l'événement **2** de notre demande **105**, depuis vm-signature après
installation de cette version :

```bash
cd /opt/youSignConnector/app
sudo -u yousign-connector env \
  CONNECTOR_DB=/var/lib/yousign-connector/events.sqlite3 \
  ESUP_URL=http://127.0.0.1:8080 \
  /opt/youSignConnector/venv/bin/python -m connector.app 2
```

Adapter les chemins si les variables de déploiement ont été changées.
Cette commande relit l'état actuel d'ESUP et imprime l'observation ; elle ne
modifie pas le parapheur. Elle fonctionne aussi pour les événements enregistrés
avant cette évolution. La table `observations` est créée sans effacer `events`.


## PDF de la dernière étape

Après identification de la dernière étape, le connecteur télécharge le PDF courant
via `GET /ws/signrequests/get-last-file/{id}`. Il le conserve à côté de la base :
`documents/<id_demande>/<id_etape>/document.pdf`, soit sous
`/var/lib/yousign-connector/documents/` avec la configuration du playbook.
Le chemin est enregistré dans `observations.resultat`, champ `document_pdf`.
Aucune variable supplémentaire ni modification du playbook n'est nécessaire.

Le téléchargement utilise un fichier temporaire privé, renommé une fois terminé.
Le programme vérifie l'en-tête PDF, sa terminaison et, si fournie par ESUP, la
longueur annoncée. Ce ne sont pas une validation complète du PDF ni une vérification
cryptographique des signatures. Une erreur laisse l'événement en `erreur` avec un
motif ; un éventuel ancien PDF reste intact. La commande de reprise retélécharge
le document courant et remplace le fichier après vérification.

Le connecteur ne télécharge rien pour les étapes intermédiaires. Il ne transmet
encore aucun document à Yousign et ne modifie pas ESUP. Les téléchargements déjà
conservés ne sont pas supprimés automatiquement.
