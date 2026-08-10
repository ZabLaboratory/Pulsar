# ADR 005 — Diagnosabilité d'un échec de go-live

- **Status**: accepted
- **Date**: 2026-08-10
- **Decided**: 2026-08-10
- **Deciders**: @ClodoCapeo
- **Author**: Atlas
- **Supersedes**: —
- **Superseded by**: —

---

## 1. Context

Un incident de direct a été abandonné en cours d'investigation, le porteur refusant d'avancer
sur l'état des journaux. L'examen du dépôt montre que « les logs sont illisibles » sous-estime
le problème.

`plugins/pulsar-headless/main.cpp:422` appelle `obs_startup()` sans jamais installer de
gestionnaire via `base_set_log_handler`. Le gestionnaire par défaut de libobs
(`upstream/libobs/util/base.c:27-54`) écrit `debug:` / `info:` / `warning:` sur stdout et
`error:` sur stderr, sans horodatage ni sous-système, et n'écrit aucun fichier. L'interface
d'obs-studio, qui crée d'ordinaire `%APPDATA%\obs-studio\logs\*.txt`, est exclue du build
(`ENABLE_UI=OFF`, `ENABLE_FRONTEND=OFF`, `docs/ARCHITECTURE.md:118-122`). Pulsar ne produit donc
aucun journal persistant : tout diagnostic dépend de ce que le parent a capturé pendant que le
processus vivait, et l'ordre relatif d'une erreur et de son contexte n'est pas garanti puisque
les deux voyagent sur des descripteurs différents.

Côté consommateur, Prism conserve les lignes dans un anneau mémoire de 200 lignes
(`broadcast-engine.ts:844`, `:1331`, `:1415`). Cet anneau n'est pas un journal : c'est le tampon
d'un classificateur. Prism tient par ailleurs un journal de session persistant
(`session-journal.ts:91-129`), mais celui-ci ne reçoit rien de ce que Pulsar a dit.

Ces lignes ne servent pas qu'à l'humain. `broadcast-engine.ts:5721-5730` marque une position
dans l'anneau, lance `StartDestination`, attend un délai de stabilisation, puis soumet la tranche
à `classifyStartFailure` (`broadcast-url.ts:245-263`), qui applique des expressions régulières à
de la prose libobs pour distinguer un refus d'authentification d'un incident d'ingest
transitoire, et retombe sur « transitoire » lorsque rien ne correspond. Une décision de reprise
en direct repose donc sur du texte non contractuel, produit par un tiers, libre de changer à
chaque montée d'upstream.

La cause structurée existe pourtant dans le processus et s'y perd. Les callbacks branchés par
`hookOutputSignals` (`plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp:513-523`)
ignorent le `calldata_t*` du signal `"stop"`, qui porte `code` et `last_error` ; seul l'énuméré
frontend est ré-émis, si bien que le consommateur reçoit un arrêt sans motif.
`plugins/pulsar-multi-stream/src/plugin-main.cpp:431` et `:468` lisent déjà
`obs_output_get_last_error()` et n'en font qu'un `errOut` synchrone ou un avertissement
journalisé — donc rien pour l'échec asynchrone, qui est le cas dominant : le handshake RTMP
échoue après que `obs_output_start` a répondu favorablement.

Un canal structuré est en revanche éprouvé en production :
`obs_websocket_vendor_emit_event(g_vendor, "BitrateAdjusted", ...)` (`plugin-main.cpp:650`),
documenté en `docs/PROTOCOL.md:612-622`.

Deux contrats stdout sont consommés aujourd'hui et bornent toute intervention :

- `main.cpp:463`, la sentinelle `PULSAR_READY ws=<url> password=<pw>`, ancrée en fin de ligne
  par vingt sondes de `scripts/` sous la forme littérale
  `re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")`, et par `docs/PRISM-EMBEDDING.md:209` ;
- `main.cpp:465`, la ligne `pulsar-headless: libobs <version> ready, idling (Ctrl+C to exit)`,
  exigée sur stdout par `packages/pulsar-bundle/src/spawn.ts:140` — conjonction de `READY_MARKER`
  (`:71`) et `IDLE_MARKER` (`:72`) — sous un watchdog de 30 s (`:130-132`). C'est le chemin de
  spawn qu'emprunte Prism à chaque mise à l'antenne.

Contrainte de fork : `upstream/` est un submodule épinglé sur obs-studio 32.1.2, avec trois
patches, et `docs/ARCHITECTURE.md:96-109` impose la discipline « plugin → PR upstream → patch »
en visant la réduction de `patches/`.

Un dernier fait corrige la nature de ce que cet ADR introduit. Pulsar écrit **déjà** des secrets
en clair sur le disque de l'opérateur. `main.cpp:385-401` écrit `<cwd>/obs-websocket/config.json`
à chaque démarrage, contenant `server_password` en clair, dans le répertoire d'installation ;
`plugins/pulsar-browser/obs-browser-plugin.cpp:293-297` entretient un journal CEF persistant. Un
journal de diffusion n'ouvre donc pas une surface vierge : il **élargit** une surface déjà
ouverte et jamais maîtrisée, en y ajoutant des classes de secret plus sensibles et une durée de
vie plus longue.

Un chemin d'écriture mérite d'être isolé dès le contexte, parce qu'il conditionne la faisabilité
même de la décision. `plugins/pulsar-websocket/src/obs-websocket.cpp:181-184` définit
`IsDebugEnabled()` comme `return !_config || _config->DebugEnabled;` — vrai lorsque la
configuration est absente, donc **ouvert par défaut**. Le macro `blog_debug`
(`plugins/pulsar-websocket/src/plugin-macros.h.in:25`) est
`if (IsDebugEnabled()) blog(LOG_INFO, "[debug] " msg, ...)`. Les dumps de charge utile
(`WebSocketServer.cpp:89`, `:421`, `:476`, `WebSocketServer_Protocol.cpp:408-409`) transitent
donc par `blog()`, au niveau `INFO`, et contiennent la requête complète : clé de flux, URL
d'ingest, URL de source navigateur porteuse d'un jeton de show. C'est exactement le flux que la
présente décision persiste, à un niveau qu'aucun seuil de verbosité raisonnable n'écarte.

Enfin, le binding du serveur n'est pas un invariant. `Config.h:51` déclare
`BindAddress = "127.0.0.1"` comme **défaut**, et son commentaire (`:44-50`) énonce qu'un bind
élargi « expose toute la surface v5 à la LAN derrière un seul mot de passe ». `PULSAR_WS_BIND`
le révoque. Toute surface ajoutée au protocole doit en tenir compte.

## 2. Decision drivers

- D1 — Un échec de direct doit se qualifier en moins d'une minute et se distinguer sans
  ambiguïté d'un échec périphérique non bloquant.
- D2 — La preuve doit survivre au processus qui l'a produite.
- D3 — Une décision d'automate ne doit pas dépendre d'une expression régulière posée sur un
  message d'origine tierce.
- D4 — Aucun patch supplémentaire sur `upstream/`, aucun déplacement du SHA du submodule ; la
  mergeabilité du fork prime.
- D5 — Les contrats de démarrage énumérés en §1 restent tenus **octet pour octet**. Aucune ligne
  existante n'est modifiée, ni dans son contenu, ni dans son descripteur de sortie.
- D6 — Le journal élargit une surface d'écriture de secrets **déjà ouverte** (§1) et jamais
  maîtrisée. La décision doit donc réduire le risque net, pas seulement éviter d'en ajouter : un
  mécanisme de rédaction qui ne s'appliquerait qu'aux nouveaux chemins ne satisferait pas ce
  driver.
- D7 — Un critère de résolution que Pulsar ne peut pas vérifier lui-même n'appartient pas à cet
  ADR : il appartient à l'issue du dépôt qui peut l'établir.
- D8 — Aucune condition de sécurité ne peut être honorée par une modification d'`upstream/`.
  Les chemins d'émission situés dans libobs sont couverts en aval, jamais à la source.

## 3. Decision

### 3.0 Précondition : le dump de charge utile devient fail-closed

`IsDebugEnabled()` (`plugins/pulsar-websocket/src/obs-websocket.cpp:183`) rend `false` lorsque
`_config` est nul. La journalisation de debug d'obs-websocket cesse d'être active par défaut en
l'absence de configuration chargée.

Cette clause précède toutes les autres et conditionne §3.1. Le raisonnement est direct :
`blog_debug` écrit par `blog()` au niveau `INFO`, donc le gestionnaire de §3.1 est le mécanisme
qui persisterait ces charges utiles. Installer le gestionnaire sans cette correction reviendrait
à créer, par une décision d'ergonomie de diagnostic, un fichier durable contenant les clés de
flux de l'opérateur.

Cette clause est **satisfaite lorsque `ZabLaboratory/Pulsar#177`
(`work_unit_id: WU-pulsar-ws-debug-failclosed-20260810`) est mergée**. Elle est ouverte et
implémentée indépendamment de l'acceptation du présent ADR : elle corrige un défaut existant et
ne dépend d'aucune autre clause. Le présent ADR ne la décide donc pas — il en dépend. `RC17`
vérifie la propriété elle-même, indépendamment du statut de l'issue : un ADR ne constate pas une
propriété de sécurité sur la foi d'un état d'issue.

### 3.1 Pulsar installe son propre gestionnaire de journal

`pulsar-headless` appelle `base_set_log_handler` avant `obs_startup`. Le gestionnaire écrit
chaque message sur une ligne de forme stable :

```
<ISO8601 UTC> <LEVEL> <session> <subsystem> | <message>
```

`LEVEL` appartient à `ERROR|WARN|INFO|DEBUG`. `subsystem` est dérivé du préfixe de module déjà
présent dans les messages Pulsar (`[pulsar-multi-stream]`, `[pulsar-scene-source]`) et vaut
`libobs` par défaut. Le journal reste du texte : les messages libobs sont de la prose ; les
emballer en JSON produirait du JSON illisible sans gagner un champ exploitable.

Destinations : un fichier tournant, contenant tous les niveaux ; et **stderr**, pour tous les
niveaux, de sorte que l'ordre relatif d'une erreur et de son contexte soit garanti.

**Périmètre exact du gestionnaire.** `base_set_log_handler` n'intercepte que `blog()`. Les
lignes émises par `printf`/`fprintf` depuis `main.cpp` ne le traversent pas. La Decision les
partage nommément en deux ensembles.

*Lignes de contrat, sur stdout, strictement inchangées :*

- `main.cpp:463` — sentinelle `PULSAR_READY ws=<url> password=<pw>` ;
- `main.cpp:465` — `pulsar-headless: libobs <version> ready, idling (Ctrl+C to exit)` ;
- la ligne de session introduite en §3.3.

*Toutes les autres lignes de `main.cpp`* — au minimum `:144`, `:156`, `:173`, `:178`, `:220`,
`:257`, `:272`, `:298`, `:302`, `:307`, `:358`, `:379`, `:387`, `:423`, `:473` — **sont
converties en `blog()`** au niveau qui leur correspond. C'est la condition pour que les échecs de
démarrage (résolution de périphérique, réinitialisation vidéo, écriture de configuration) soient
dans l'artefact de diagnostic plutôt qu'à côté.

**Interdiction de tee.** Le contenu de stdout n'est jamais dupliqué dans le fichier. En
particulier, `main.cpp:463` porte le mot de passe de session **par contrat** ; la convertir en
`blog()` la ferait entrer dans le fichier tout en cassant les vingt sondes qui l'ancrent.
L'exclusion nommée de `:463` et `:465` est un invariant de sécurité autant qu'un invariant de
compatibilité.

**Emplacement.** Le journal vit sous `%LOCALAPPDATA%\Pulsar\logs\`. Jamais `%APPDATA%`
(Roaming) : la redirection de dossier connu synchronise Roaming vers un stockage cloud sur un
poste géré, ce qui exfiltrerait le journal hors de la machine sans action ni consentement de
l'opérateur. Jamais non plus à côté de l'exécutable : le répertoire d'installation est réécrit à
chaque mise à jour de bundle — donc les preuves d'un incident disparaîtraient au prochain bump,
ce que D2 interdit — et se retrouve dans les archives de support.

`PULSAR_LOG_DIR` reste l'échappatoire explicite, et un geste opérateur assumé : la décision
garantit l'emplacement par défaut, pas les propriétés d'un répertoire que l'opérateur désigne.
La clause d'ACL ci-dessous borne le pire cas ; au-delà, un opérateur qui redirige son journal en
connaît la conséquence.

**Permissions.** Le répertoire est créé avec une ACL restreinte au seul compte utilisateur
courant, explicitement posée à la création et non héritée. Lorsque `PULSAR_LOG_DIR` désigne un
répertoire existant dont les permissions sont plus larges, l'écriture fichier est refusée et le
processus se rabat sur stderr seul, en journalisant le refus. Un journal que la décision protège
par ses permissions ne peut pas dépendre de l'endroit où on lui demande d'écrire.

**Échec d'ouverture.** Si le fichier ne peut pas être ouvert — répertoire non inscriptible,
disque plein, permissions refusées par la clause ci-dessus — le processus continue, ne retente
pas, et écrit sur stderr une ligne de niveau `ERROR` nommant le chemin et la cause. Un journal
absent est un état légitime ; un journal absent sans que personne le sache est une enquête perdue
d'avance.

**Rétention.** Trois bornes cumulatives, et non alternatives : `PULSAR_LOG_MAX_FILES` (10) et
`PULSAR_LOG_MAX_BYTES` (16 Mio) bornent le volume ; `PULSAR_LOG_MAX_AGE_DAYS` (7) borne la durée.
La borne temporelle est nécessaire parce que les classes de secret n'ont pas le même cycle de
vie : le mot de passe de session est régénéré à chaque démarrage (`main.cpp:335-349`), mais une
clé de flux Twitch ne tourne que sur action humaine. Une borne en taille seule laisserait une clé
dormir indéfiniment dans un journal peu alimenté.

**Arbitrage assumé.** Cette borne temporelle contredit D2 dans un cas précis et réel : un
incident encore ouvert au huitième jour n'a plus de preuve. L'arbitrage est délibéré — une clé
dormante est le risque le plus lourd des deux. La conséquence opérationnelle est reportée sur
§3.9 : l'ouverture d'un incident **commence** par la copie du répertoire hors de la zone de
purge.

**Configuration**, par variables d'environnement, valeurs par défaut codées dans le binaire :
`PULSAR_LOG_DIR`, `PULSAR_LOG_MAX_FILES`, `PULSAR_LOG_MAX_BYTES`, `PULSAR_LOG_MAX_AGE_DAYS`, et
`PULSAR_LOG_FILE=off` qui empêche l'ouverture du fichier au démarrage en laissant stderr intact.

### 3.2 Rédaction dans le gestionnaire, par deux couches requises

**Emplacement unique.** La rédaction s'exécute **dans le gestionnaire de journal lui-même**,
comme dernier traitement avant écriture. Jamais au site d'appel. Trois raisons, dont la troisième
est décisive : un site d'appel oublié est une fuite silencieuse ; un site d'appel ajouté demain
ne connaît pas la règle ; et une partie des sites d'émission vit dans `upstream/` —
`rtmp-stream.c:1171` colle l'URL complète dans le champ serveur en mode `rtmp_custom` — où D4 et
D8 interdisent d'intervenir. La rédaction en aval est le seul emplacement qui couvre l'ensemble
des chemins sans toucher au fork. Les canaux `plugins/pulsar-browser/browser-client.cpp:114` et
`:637`, qui portent l'URL de source et donc le jeton de show, sont couverts par le même moyen.

**Deux couches, toutes deux requises.** Elles ne se hiérarchisent pas, elles couvrent des cas
disjoints.

- Le **motif** est actif dès l'installation du gestionnaire, sans dépendre d'aucune configuration
  chargée. Il couvre ce qui a une forme reconnaissable, y compris émis par un chemin qui n'a rien
  enregistré — une URL d'ingest recomposée par un module upstream, par exemple. Il ne dépend
  d'aucun ordre. C'est nécessaire : le gestionnaire s'installe avant `obs_startup`
  (`main.cpp:422`), donc avant qu'aucune configuration, collection de scènes ou destination
  persistée n'ait été lue ; une valeur chargée depuis un état persisté à `obs_module_load` n'a
  transité par aucun appel d'enregistrement.
- Le **registre** couvre ce qui n'a aucune forme : une clé de flux nue, émise seule hors d'une
  URL, est indiscernable d'un identifiant ordinaire pour un motif. Seul le registre sait que
  cette chaîne-là est un secret. Les composants qui reçoivent un secret l'y enregistrent au
  moment où ils le reçoivent.

Aucune des deux ne subsume l'autre ; une implémentation qui n'en livrerait qu'une laisse une
classe entière à découvert. L'enregistrement d'une valeur avant tout appel susceptible de la
journaliser est une obligation d'implémentation portée par chaque composant qui reçoit un secret,
non une propriété que la structure du programme garantirait.

Le motif couvre, au minimum :

| Classe | Forme |
|---|---|
| Clé de flux | valeur du champ `key` d'une destination, sous toutes ses occurrences |
| URL d'ingest complète | `rtmp://…`, `rtmps://…`, chemin et clé inclus |
| Mot de passe WebSocket | valeur de `server_password`, et tout `password=` en query |
| Jeton de show | forme brute **et** forme encodée `token%3D` |
| Query param sensible | `token`, `key`, `password`, `auth`, `sig` |

Le jeton de show figure sur cette liste parce qu'il transite par une URL de source navigateur et
ressort sur au moins deux canaux : la console CEF et les dumps de charge utile de §3.0. Le
consommateur le rédige déjà de son côté, sous ses deux formes
(`Prism/src/main/broadcast-engine.ts:4408-4412`, SEC-033) ; Pulsar ne peut pas s'en remettre à
une rédaction faite chez autrui.

**Posture d'échec, non négociable.** Si la rédaction ne peut pas s'exécuter, quelle qu'en soit la
raison, **la ligne est abandonnée**. Elle n'est jamais écrite brute. Un journal incomplet est un
incident d'exploitation ; un journal qui fuit une clé est un incident de sécurité.

**Nature du mécanisme, assumée.** Une rédaction par motif est un filtre au mieux-effort. Elle ne
prétend pas à la complétude : elle est bornée par les permissions du répertoire, par la rétention
de §3.1, et par un critère de non-régression exécutable (RC16) qui la rejoue à chaque build
plutôt que de la supposer tenue.

Cette clause naît avec le fichier : ni différable, ni séparable de §3.1.

### 3.3 Clé de corrélation de session, sur une ligne distincte

Pulsar accepte `PULSAR_SESSION_ID`, ou en dérive une valeur à défaut, et l'émet sur stdout sur
une **ligne propre**, `PULSAR_SESSION <id>`, **avant** la sentinelle.

La sentinelle `main.cpp:463` reste inchangée octet pour octet. C'est la raison même de ce choix :
vingt sondes ancrent `password=(\S+)$` en fin de ligne, et allonger la sentinelle les casserait
toutes d'un coup, en contradiction avec D5. Les deux styles de lecture du dépôt tolèrent une
ligne intercalaire : les sondes bouclent et ignorent ce qui ne matche pas,
`spawn.ts:134-145` également.

La session est portée par chaque ligne de journal et par chaque événement vendeur `pulsar:*`.
Ajoutée aux événements existants, elle ne renomme ni ne retype aucun champ.

Les deux journaux — celui de Pulsar, celui du consommateur — restent deux fichiers distincts :
deux processus, deux licences, deux cycles de vie. Ils deviennent joignables, ils ne fusionnent
pas.

### 3.4 La cause d'échec devient un événement structuré

Un événement vendeur `pulsar:OutputFailed` est émis lorsqu'un output quitte l'état actif
autrement que sur demande, ou lorsqu'un démarrage est refusé. Charge : `output`, `phase`, `code`
(`OBS_OUTPUT_*`), `last_error`, `reason_class`, `session`.

Sources déjà présentes : le `calldata_t*` du signal `"stop"` ignoré en
`pulsar-frontend-stub.cpp:513-523`, et `obs_output_get_last_error()` déjà lu en
`plugin-main.cpp:431` / `:468`.

**Jeu fermé de `reason_class`**, arrêté ici et non renvoyé au protocole :

| Classe | Sens |
|---|---|
| `auth_rejected` | L'ingest a refusé les identifiants — clé invalide ou révoquée. |
| `ingest_unreachable` | Aucune connexion établie — DNS, routage, port, serveur injoignable. |
| `ingest_dropped` | Connexion établie puis perdue en cours de diffusion. |
| `encoder_failed` | L'encodeur n'a pas démarré ou s'est arrêté en erreur. |
| `config_rejected` | libobs a refusé la configuration avant toute tentative réseau. |
| `disconnected_local` | Arrêt provoqué localement hors demande du client. |
| `unknown` | Aucune classe ne s'applique. Le `last_error` brut est joint tel quel. |

`unknown` est une valeur légitime et attendue. Une classe approchée serait pire que l'aveu : elle
relancerait le scraping en aval, ce que cet ADR existe pour supprimer.

### 3.5 Un verdict de tentative, et c'est lui qui fait autorité

À l'issue d'une tentative de mise à l'antenne, Pulsar émet `pulsar:OutputAttemptSettled`, une
fois exactement par tentative et par destination, succès compris. Charge : `output`,
`destination`, `attempt`, `outcome` (`live` | `failed`), `reason_class` (jeu fermé de §3.4 ;
**champ absent** lorsque `outcome` vaut `live` — jamais un caractère de présentation dans un
champ que des machines comparent), `code`, `last_error`, `duration_ms`, `session`.

**Répartition d'autorité, tranchée :**

- `pulsar:OutputAttemptSettled` fait autorité pour la décision de reprise. Il est borné à la
  tentative, toujours émis, et donc décidable sans fenêtre temporelle ni corrélation ;
- `pulsar:OutputFailed` fait autorité pour les défaillances **en cours de diffusion**, hors
  tentative de démarrage.

Un consommateur n'a donc jamais à agréger lui-même des événements pour savoir si le direct a
démarré, ni à arbitrer entre deux sources pour un même fait.

### 3.6 Surface de diagnostic

Deux requêtes vendeur, de profil délibérément différent, décrites séparément parce qu'elles ne
portent ni le même contenu ni le même risque.

**Condition de service commune aux réponses porteuses de contenu.** Aucune ligne de journal
n'est servie lorsque le serveur n'est pas lié à la boucle locale. Le prédicat est celui que le
serveur calcule déjà (`WebSocketServer.cpp:116`) ; `PULSAR_WS_BIND` peut élargir le bind
(`Config.h:51`, commentaire `:44-50`), et un journal servi sur une interface non-loopback est un
point d'exfiltration, non un outil de diagnostic. Le refus est une **erreur explicite**, nommant
la raison — jamais une liste vide, jamais une réponse tronquée en silence : un diagnostic qui
ment sur sa propre indisponibilité est pire que son absence.

Cette condition ne lie que §3.6.1, seule à transporter du contenu de message.

#### 3.6.1 Extraction de diagnostic

Rend le chemin du journal courant, les compteurs par niveau depuis le démarrage, l'état des
outputs connus, et les `N` dernières lignes `WARN`/`ERROR`.

Le contenu servi est celui déjà rédigé par §3.2, en mémoire. La requête ne relit pas le fichier
et n'ouvre aucun chemin : il n'y a pas de chemin en paramètre. `N` est plafonné côté serveur
quelle que soit la demande. Une requête de lecture ne doit pas devenir un primitif de lecture de
fichier arbitraire atteignable depuis le socket.

Les compteurs par niveau et l'état des outputs, qui ne portent aucun contenu de message, restent
servis même lorsque la condition de bind n'est pas remplie.

Le filtrage sur `WARN`/`ERROR` écarte structurellement le pire canal — les dumps de charge utile
de §3.0, émis au niveau `INFO`.

#### 3.6.2 Coupe-circuit d'écriture

Arrête l'écriture fichier à chaud, sans redémarrage. Elle ne transporte aucun contenu de
journal : elle rend un statut.

Elle est délibérément asymétrique :

- elle **arrête** l'écriture, elle ne la reprend jamais — la reprise exige un redémarrage ;
- elle ne modifie aucun chemin et n'en accepte aucun en paramètre ;
- l'arrêt est lui-même journalisé comme dernière ligne du fichier, avant fermeture.

L'asymétrie est un choix de sécurité, pas une simplification. Une bascule symétrique offrirait à
un client authentifié le moyen de couper la journalisation, d'agir, puis de la rétablir en ne
laissant qu'un trou muet. Ici, toute coupure est datée, terminale et visible dans le fichier
qu'elle ferme.

Cette requête porte le levier de rollback de R1 : c'est le seul moyen d'arrêter une fuite en
cours de diffusion sans couper l'antenne.

### 3.7 Le consommateur cesse de scraper

Prism consomme §3.5 et §3.4 et abandonne `classifyStartFailure` comme chemin de décision. Les
expressions régulières survivent comme repli explicite et journalisé, emprunté uniquement face à
un Pulsar antérieur à cet ADR.

Cette clause **décrit une intention pour un autre dépôt** ; conformément à D7, aucun critère de
résolution du présent ADR ne prétend la vérifier. Elle est portée par une issue Prism dédiée,
dont les critères sont : parité de décision face à un Pulsar sans l'événement, mesurée contre
l'oracle existant `Prism/src/main/broadcast-url.test.ts:170-240` ; emprunt du repli journalisé et
nommé comme tel ; décision dirigée par l'événement prouvée face à un Pulsar qui l'émet ;
jointure effective des deux journaux sur la session ; et existence d'un interrupteur restaurant
le chemin regex comme chemin primaire, actionnable sans redéploiement.

Le chemin d'adoption comporte une étape que la Decision nomme explicitement plutôt que de la
laisser implicite : les changements Pulsar n'atteignent Prism qu'après publication d'un bundle
npm et bump côté consommateur, selon `docs/runbooks/cut-a-release-and-propagate.md`.

### 3.8 Frontières

Aucune modification de `upstream/`, aucun patch nouveau, aucun déplacement du SHA du submodule.
`base_set_log_handler` est le point d'extension sanctionné de libobs, employé par obs-studio
lui-même, appelé depuis du code possédé par Pulsar. Les événements de base v5 restent émis
inchangés.

### 3.9 Le runbook est un livrable de cette décision

`docs/runbooks/diagnose-a-failed-go-live.md` fait partie de la décision, non de son commentaire :
un arbre de décision partant du verdict et de l'événement, jamais du fichier, et une table
`reason_class` → cause probable → geste opérateur alignée sur le jeu fermé de §3.4. Sans lui, D1
n'a pas d'énoncé vérifiable côté opérateur.

**Première étape de l'arbre, avant tout diagnostic** : copier le répertoire de journaux hors de
la zone de purge. C'est la contrepartie opérationnelle de l'arbitrage de rétention de §3.1.

### 3.10 Séquence imposée par la décision elle-même

- §3.0 précède §3.1 : sans elle, le gestionnaire persiste des charges utiles complètes.
- §3.2 est indissociable de §3.1 : le rédacteur naît avec le fichier, en un seul changement.
- §3.3 est sérialisé derrière §3.1, non parallèle : même fichier, même contrat stdout.
- §3.5 et §3.6 dépendent du jeu de classes de §3.4.
- **§3.6.2 est indissociable du merge de §3.1** : elle porte le levier de rollback de R1. Livrer
  le journal sans son coupe-circuit reviendrait à créer un risque avant son moyen de maîtrise.
  Les deux se mergent ensemble ou pas du tout — c'est un fait de gate, non une préférence de
  séquencement.
- §3.7 est en aval de §3.4, §3.5, §3.3, puis de la publication du bundle et de son adoption.

## 4. Consequences

- Un incident laisse un artefact daté, typé, corrélé, qui survit au processus.
- Les diagnostics de démarrage de `main.cpp` rejoignent l'artefact au lieu de le côtoyer.
- La distinction « le direct n'a pas démarré » / « un filtre optionnel a échoué » se lit sur un
  verdict, plus sur un volume de texte.
- Une décision de reprise cesse de dépendre d'une expression régulière posée sur un message
  d'origine tierce.
- Pulsar écrit sur le disque de l'opérateur : emplacement, permissions, rotation, rétention et
  rédaction deviennent des propriétés dont il répond.
- La surface stdout est désormais **close et énumérée** : trois lignes, pas une classe. Toute
  ligne future y sera un choix explicite.
- `docs/PROTOCOL.md` gagne deux entrées d'événement (`OutputFailed`, `OutputAttemptSettled`) et
  deux entrées de requête (§3.6.1, §3.6.2).
- Le fork ne s'éloigne pas d'upstream : `patches/*.patch` reste à trois, le SHA du submodule ne
  bouge pas.

## 5. Risks

- R1 — **Risque résiduel — secrets at rest dans le journal local.** Le fichier de log concentre,
  sur la machine de l'opérateur, des messages dérivés de payloads qui portent des secrets longue
  durée (clé de flux Twitch) et courte durée (mot de passe de session obs-websocket, show-token).
  La rédaction est un contrôle *best-effort par motif* : elle ne peut pas garantir qu'une forme
  d'encodage non anticipée d'un secret échappe au masquage. Le risque accepté est qu'un attaquant
  disposant déjà d'une exécution de code sous le compte de l'opérateur puisse lire ce fichier ;
  il n'accorde aucune capacité nouvelle à un attaquant distant. Le fichier lui-même n'est jamais
  transmis ni exposé sur le réseau. Seul un extrait borné, rédigé et servi depuis la mémoire est
  accessible (§3.6), à un client authentifié, et uniquement lorsque le serveur obs-websocket est
  lié à la boucle locale : §3.6 refuse de servir des lignes de journal dès que `PULSAR_WS_BIND`
  élargit le bind au-delà du loopback. La surface distante reste donc nulle par construction, et
  non par configuration par défaut. Ce risque est borné par : ACL user-only sous `%LOCALAPPDATA%`
  (jamais de répertoire synchronisé), rétention temporelle bornée, et un critère de
  non-régression qui vérifie l'absence des classes de secrets connues dans le fichier produit. Le
  mot de passe obs-websocket étant régénéré à chaque spawn, sa fuite est bornée à la session ;
  **la clé de flux Twitch ne tourne pas seule et reste le pire cas — sa rotation est la réponse
  d'incident attendue.**
- R2 — Croissance disque non bornée. Mitigation : les trois bornes cumulatives de §3.1, vérifiées
  par RC8 et RC19.
- R3 — Régression d'un contrat stdout. C'est le risque létal : `spawn.ts:130-132` échouerait au
  bout de 30 s, donc **tous** les directs. Mitigation : D5, §3.1 énumère les lignes de contrat,
  §3.3 n'en modifie aucune, RC3 et RC4 exercent les deux consommateurs réels.
- R4 — `reason_class` trop grossière, réintroduisant du scraping. Mitigation : jeu fermé arrêté
  en §3.4, `unknown` explicite, et RC5 exige la couverture des signatures aujourd'hui reconnues
  par le consommateur.
- R5 — Divergence de version entre un Pulsar émetteur et un Prism qui ignore l'événement.
  Mitigation : repli explicite de §3.7, interrupteur de bascule, et étape de publication nommée.
- R6 — La requête de §3.6.1 dérive en primitif de lecture de fichier. Mitigation : absence de
  chemin en paramètre, service depuis la mémoire rédigée, condition de bind, clearance Bastion.
- R7 — Une condition de sécurité honorée au site d'émission plutôt que dans le gestionnaire
  conduirait à patcher `upstream/` et à casser la mergeabilité du fork. Mitigation : §3.2 nomme
  l'interdiction, D8 la porte au rang de driver.
- R8 — La conversion des `printf` de `main.cpp` en `blog()` atteint par erreur `:463`, faisant
  entrer le mot de passe de session dans le fichier **et** cassant vingt sondes. Mitigation :
  exclusion nommée en §3.1, RC3, RC4 et RC20.
- R9 — Le mot de passe de session écrit en clair dans le répertoire d'installation
  (`main.cpp:385-401`) donne à tout compte local qui le lit une session v5 authentifiée, donc
  l'accès aux extraits de journal de §3.6.1. Le défaut est antérieur à cette décision ; sa
  gravité augmente avec elle. Traité par une unité dédiée, hors du chemin critique de la présente
  campagne.

## 6. Resolution criteria

Tous vérifiables depuis le seul dépôt Pulsar (D7).

- RC1 — Après un démarrage, un fichier de journal existe ; **chaque** ligne matche le gabarit de
  §3.1, vérifié par un contrôle automatisé parcourant le fichier entier ; le fichier est lisible
  pendant que le processus tourne.
- RC2 — Les quatre niveaux apparaissent et correspondent au niveau libobs d'origine ; les lignes
  de `main.cpp` énumérées en §3.1 comme converties sont présentes dans le fichier et **absentes**
  de stdout.
- RC3 — Les vingt sondes `scripts/probe-*.py` qui ancrent `READY_RE` s'exécutent sans
  modification et reconnaissent la sentinelle. Preuve par exécution de `scripts/run-probes.ps1`.
- RC4 — `packages/pulsar-bundle/src/spawn.ts` obtient son signal de disponibilité sans
  modification et sans atteindre le watchdog de 30 s.
- RC5 — Chaque signature aujourd'hui reconnue par `PERSISTENT_RTMP_SIGNATURES` et
  `TRANSIENT_RTMP_SIGNATURES` (`Prism/src/main/broadcast-url.ts`) correspond à exactement une
  `reason_class` de §3.4 ; la table est produite et aucune signature n'est orpheline. Cette table
  est un artefact Pulsar, produite une fois à partir de l'état du consommateur au moment de la
  campagne ; son maintien en phase avec les évolutions ultérieures du consommateur est hors
  périmètre.
- RC6 — Clé de flux invalide : `pulsar:OutputAttemptSettled` portant `outcome="failed"` et
  `reason_class="auth_rejected"`. Ingest injoignable : `reason_class="ingest_unreachable"`.
  Aucune ligne de journal n'est lue pour l'établir.
- RC7 — Arrêt demandé par le client : aucun `pulsar:OutputFailed`. Échec d'attache d'un filtre
  optionnel : aucun `pulsar:OutputFailed`, trace `WARN` présente dans le fichier.
- RC8 — Un volume forcé déclenche la rotation ; le répertoire reste sous
  `PULSAR_LOG_MAX_FILES × PULSAR_LOG_MAX_BYTES`, mesuré.
- RC9 — Le rédacteur de §3.2 est couvert par un test unitaire sur un corpus de formes : valeur
  enregistrée nue, enchâssée dans une URL, répétée dans la ligne, casse différente, URL `rtmp://`
  et `rtmps://` non enregistrée, et un cas d'échec du rédacteur prouvant que la ligne est
  abandonnée et non écrite brute. Les deux couches de §3.2 sont exercées séparément.
- RC10 — Aucune clé de flux, aucune URL d'ingest complète, aucun mot de passe de session
  n'apparaît dans le fichier, sur un scénario de bout en bout qui en fait transiter.
- RC11 — Une étape du job `lint` de `.github/workflows/pipeline.yml` (aux côtés de « Patch lint »,
  `:166`) échoue si une PR touche `upstream/`, ajoute ou retire un fichier de `patches/*.patch`
  au-delà de trois, ou déplace le SHA du submodule par rapport à la base. Preuve par un run rouge
  provoqué, puis vert.
- RC12 — La ligne `PULSAR_SESSION <id>` précède la sentinelle ; l'identifiant fourni par
  l'environnement est repris à l'identique ; à défaut une valeur est générée, non vide, stable
  sur la session, différente entre deux démarrages.
- RC13 — Chaque ligne de journal et chaque événement `pulsar:*` porte la session ; aucun champ
  existant de `pulsar:BitrateAdjusted` n'a changé de nom ni de type.
- RC14 — La requête de §3.6.1 rend chemin, compteurs et dernières lignes `WARN`/`ERROR` sur un
  processus actif ; les compteurs concordent avec un comptage indépendant du fichier ; une
  demande de `N` hors borne est plafonnée sans erreur ; aucun secret n'y apparaît.
- RC15 — Un lecteur n'ayant pas participé à la campagne qualifie les scénarios de RC6 et RC7 en
  suivant `docs/runbooks/diagnose-a-failed-go-live.md`, sans ouvrir le fichier de journal, en
  moins d'une minute, le parcours commençant par la copie du répertoire hors zone de purge.
  Vérifié par exécution.
- RC16 — Non-régression de rédaction, exécutable en CI : debug WebSocket forcé actif, une clé de
  flux factice, une URL `?token=<jeton>` et sa forme `token%3D<jeton>` envoyées à travers le
  protocole ; recherche des trois valeurs dans l'intégralité du répertoire de journaux ; **zéro
  occurrence**. Ce critère porte sur le répertoire décidé en §3.1, et sur lui seul. Il ne dit
  rien des deux surfaces d'écriture de secret préexistantes recensées en §1 —
  `<cwd>/obs-websocket/config.json` et le journal CEF — qui ne sont pas traitées par cet ADR. Un
  RC16 vert établit « aucun secret connu dans le journal Pulsar », jamais « aucun secret sur le
  disque ».
- RC17 — `IsDebugEnabled()` rend `false` lorsque `_config` est nul, couvert par un test unitaire ;
  aucune charge utile de requête n'apparaît dans le journal sur un démarrage sans configuration.
  Vérifié par lecture du code et par test, indépendamment du statut de `#177`.
- RC18 — Le répertoire de journaux est créé sous `%LOCALAPPDATA%\Pulsar\logs\` ; aucun fichier de
  journal n'est créé sous `%APPDATA%` ni dans le répertoire de l'exécutable, vérifié après un
  démarrage complet. Ne porte que sur le défaut, non sur un `PULSAR_LOG_DIR` surchargé.
- RC19 — Un fichier antidaté au-delà de `PULSAR_LOG_MAX_AGE_DAYS` est supprimé au démarrage
  suivant, y compris lorsque les bornes de taille et de nombre ne sont pas atteintes.
- RC20 — Aucune ligne du journal ne provient de stdout : les trois lignes de contrat de §3.1 sont
  absentes du fichier, et `main.cpp:463` en particulier n'y figure sous aucune forme.
- RC21 — Le répertoire est créé avec une ACL restreinte au compte courant, vérifiée après
  création ; un `PULSAR_LOG_DIR` pointant vers un répertoire lisible par un autre compte local
  entraîne le refus d'écriture fichier et la conservation de stderr, prouvé par exécution.
- RC22 — Un répertoire non inscriptible entraîne le démarrage nominal, une ligne `ERROR` sur
  stderr nommant chemin et cause, et aucune interruption du reste du démarrage, prouvé par
  exécution.
- RC23 — La requête de §3.6.2 ferme le fichier sur un processus en cours de diffusion, sans
  interruption de la diffusion ; la dernière ligne du fichier atteste l'arrêt ; une seconde
  requête ne rouvre rien.
- RC24 — Avec `PULSAR_WS_BIND` élargi au-delà du loopback, §3.6.1 refuse de servir des lignes de
  journal par une erreur explicite ; avec le bind par défaut, elle sert normalement. Les
  compteurs et l'état des outputs répondent dans les deux cas.

## 7. Rollback

Le rollback n'est pas un revert de commit : à l'instant où le risque se manifeste, des fichiers
ont déjà été écrits sur le disque de l'opérateur, et un revert les y laisserait.

- **Fuite de secret constatée en cours de diffusion (R1)** — la requête de §3.6.2 coupe le
  fichier à chaud, sans interrompre l'antenne ; puis purge du répertoire ; puis rotation de la clé
  de flux exposée, seul geste qui interrompt la diffusion et qui reste donc une décision de
  l'opérateur, jamais une étape automatique — le runbook de §3.9 la présente comme telle.
  `PULSAR_LOG_FILE=off` empêche la réouverture au démarrage suivant : c'est la persistance de la
  coupure, pas la coupure elle-même.
- **Régression d'un contrat stdout (R3)** — revert de la PR concernée. Les lignes de contrat
  n'étant jamais modifiées, seule l'introduction de §3.3 est réversible isolément, ce pour quoi
  elle est sérialisée et livrée seule.
- **Régression de reprise côté consommateur (R5)** — l'interrupteur de §3.7 restaure le chemin
  regex comme chemin primaire, sans redéploiement. Un revert ne suffit pas : la panne se
  manifesterait en direct.
- **Croissance disque (R2)** — abaissement des plafonds par variable d'environnement, effectif au
  démarrage suivant.
