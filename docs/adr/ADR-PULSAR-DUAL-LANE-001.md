AGENT_REPORT

- rôle : `Atlas-3`
- thread : `/root/atlas_pulsar_capabilities_3`
- `work_unit_id` : `pulsar-adr-dual-canvas-capabilities-20260828`
- type : continuation du même work unit
- autorité : lecture seule, conception/architecture uniquement
- périmètre : révision sémantique de l’ADR dual-lane Pulsar, sans extension de périmètre
- exclusions : aucune écriture, aucun commit, aucune branche, aucune issue GitHub, aucune action externe, aucune implémentation
- statut du livrable : `READY`
- statut ADR : `DRAFT`
- verdict Vigil : `PENDING_REVIEW`
- SHA-256 : non calculé, conformément au bail
- révision précédente : `draft-r1-dual-lane-20260828`
- nouvelle révision : `draft-r2-dual-lane-20260828`

La révision r2 remplace le brouillon r1 pour la prochaine revue Vigil. Toute approbation éventuelle de r1 ne serait pas applicable à r2 ; Vigil et l’approbation humaine doivent citer exactement `ADR-PULSAR-DUAL-LANE-001` et `draft-r2-dual-lane-20260828`.

# ADR-PULSAR-DUAL-LANE-001 — Deux lanes physiques chaudes et surfaces vidéo stables

## Métadonnées

- ADR-ID : `ADR-PULSAR-DUAL-LANE-001`
- titre : Deux lanes physiques chaudes, rôles Preview/OnAir permutables et surfaces vidéo stables
- auteur : `Atlas-3`
- date : `2026-08-28`
- statut : `DRAFT`
- révision : `draft-r2-dual-lane-20260828`
- emplacement canonique cible : `docs/adr/ADR-PULSAR-DUAL-LANE-001.md`
- SHA-256 : non calculé
- `content_fingerprint` :

```text
ADR-PULSAR-DUAL-LANE-001|draft-r2-dual-lane-20260828|decision=two-hot-physical-lanes+A/B;logical-roles=Preview/OnAir-permutable;stable-surfaces=ProgramView+PreviewView;swap=atomic-frame-boundary;active-video_t=never-rebound;post-take=old-OnAir-becomes-Preview-only-after-TakeCommitted;contract=pulsar.scene-switch.v1;runtime=runtime_instance_id+namespaced-internal-mappings+single-legacy-alias-lease-or-instance-specific-filters;initial-lot=video-cut+common-program-audio+stable-surfaces+contract+runtime-isolation+instrumentation;core-closure=PUL-DL-08→PUL-DL-14-without-extension-blockers;extensions=non-blocking-follow-ups-or-new-ADR;directshow-slo=TakeAccepted→first-valid-Program-frame-on-DirectShow-return;raw-slo=TakeAccepted→first-valid-Program-frame-at-encoder-input/raw;SLO=raw-p95<=50ms+DirectShow-return-p95<=75ms;decoded-antenna=not-guaranteed
```

Historique :

- `r1` : première rédaction du dual-lane.
- `r2` : correction sémantique ciblée de la clôture core, des frontières SLO DirectShow, de l’isolation des alias DirectShow historiques et du cycle de vie post-Take.
- Aucun amendement n’est encore approuvé, persisté ou mergé.

## 1. Contexte et réalité connue

La base factuelle ci-dessous provient des faits canoniques transmis dans le bail. Elle n’a pas été revérifiée localement dans ce tour, celui-ci étant explicitement limité à la rédaction et sans exploration.

Pulsar/libobs dispose actuellement d’une vue principale `Program` et d’une vue `Preview`. Le retour simultané des deux vues est déjà prouvé.

Les mesures disponibles indiquent notamment :

- `Take` sur chemin raw : p95 approximativement `32–35 ms` ;
- `Take` via DirectShow : p95 approximativement `57–67 ms` ;
- timecode CEF continu et PTS identiques sur cinq Takes observés ;
- encodage 1080p60 établi avec x264 et NVENC ;
- premier paquet RTMP observé à p95 `≤ 15 ms` ;
- première frame changée décodée observée approximativement à `136 ms` avec x264 et `253 ms` avec NVENC ;
- deux vues simples ajoutent environ `+0,091 ms/frame` et `+3,13 MB`, mais la charge combinée WGC + CEF + NVENC reste non prouvée.

Les défauts et contraintes actuels sont structurants :

1. Après un `Take`, modifier le nouveau `Preview` peut modifier le `Program`, ce qui révèle un alias ou une racine mutable partagée entre les deux rôles.
2. Les commandes peuvent entrer en course ; un double `Take` n’est pas protégé par révision, idempotence ou commit explicite.
3. L’encodeur actif ne peut pas changer de `video_t` pendant son fonctionnement.
4. Des collisions existent autour du `cwd`, de `config.json` et de mappings globaux.
5. L’indépendance audio `Program`/`Preview` n’est pas démontrée.
6. Seul `Cut` est fiable à ce stade ; `Fade`, `Stinger` et `T-bar` ne sont pas validés.
7. La latence de la première frame décodée ou antenne ne doit pas être confondue avec la latence d’injection ou d’observation de la première frame Programme.
8. Les mappings DirectShow historiques `Program`/`Preview` ne peuvent pas être considérés comme quatre namespaces indépendants : ils nécessitent soit une possession exclusive par lease, soit des filtres/noms dédiés par instance.

Le problème à résoudre est donc double : obtenir un changement de scène déterministe et borné tout en maintenant des producteurs chauds, des sorties stables et des commandes protégées contre les courses, sans jamais réaffecter le `video_t` d’un encodeur actif.

## 2. Objectifs

La décision vise à :

- éliminer l’alias mutable entre `Program` et `Preview` ;
- conserver deux lanes physiques actives et indépendantes ;
- permettre aux rôles logiques `Preview` et `OnAir` de permuter entre les lanes ;
- maintenir des surfaces vidéo stables pour les consommateurs aval ;
- réaliser un `Cut` atomique à une frontière de frame ;
- garder encodeur, stream, record et retours liés à des surfaces stables plutôt qu’à des `video_t` interchangeables ;
- empêcher un `Preview` modifié après un `Take` d’affecter le `Program` ;
- formaliser le protocole de commande avec révisions, séquences serveur, idempotence et rejet des commandes obsolètes ;
- isoler plusieurs instances runtime par `runtime_instance_id` ;
- atteindre les SLO de latence raw et DirectShow définis ci-dessous ;
- rendre les invariants et la preuve observables ;
- permettre la clôture du noyau ADR sur les seules preuves du lot initial, sans rendre les extensions obligatoires.

## 3. Non-objectifs

Ne font pas partie du noyau r2 :

- l’ajout ou la validation de `Fade`, `Stinger` ou `T-bar` ;
- l’audio `Preview` indépendant ou l’AFV ;
- le changement dynamique du `video_t` d’un encodeur actif ;
- la garantie de latence première frame décodée ou antenne ;
- la promesse de capacité sous charge réelle WGC + CEF + NVENC avant mesure ;
- une refonte générale de l’interface opérateur ;
- la résolution de problèmes de diffusion non directement liés au changement de scène ;
- l’inférence d’une indépendance audio actuelle à partir des seules preuves vidéo ;
- l’obligation de terminer le tuning DirectShow/NVENC ou le soak complet avant la clôture du noyau ADR.

Les éléments exclus peuvent devenir des follow-ups optionnels ou être redécoupés dans une nouvelle ADR après clôture du noyau. Ils ne bloquent pas `PUL-DL-14` si toutes les preuves du noyau sont satisfaites.

## 4. Contraintes et hypothèses

### Contraintes

- Les encodeurs x264 et NVENC 1080p60 existants doivent rester utilisables.
- Le `video_t` consommé par un encodeur actif ne doit jamais être remplacé à chaud.
- Le changement doit se produire à une frontière de frame déterministe.
- Les producteurs des deux lanes doivent rester vivants après un `Take`.
- Le chemin raw doit viser un p95 `≤ 50 ms`.
- Le chemin DirectShow doit viser un p95 `≤ 75 ms`, défini sur le retour DirectShow.
- Les surfaces aval doivent rester identifiables et stables.
- Les mappings internes doivent être namespacés par `runtime_instance_id`.
- Les alias historiques DirectShow `Program`/`Preview` doivent être possédés par une seule instance via lease explicite, ou remplacés par des filtres/noms dédiés par instance ; aucun partage silencieux n’est autorisé.
- Les inconnues audio et charge doivent être mesurées, pas supposées.

### Hypothèses à confirmer avant implémentation

- Une primitive étroite de swap atomique peut être introduite dans libobs/Pulsar sans exposer toute la topologie interne.
- Les roots de scène peuvent être encapsulées par des références immuables ou protégées pendant le rendu d’une frame.
- Le runtime peut attribuer un namespace complet à `runtime_instance_id`.
- Un état de commande idempotent peut être conservé au moins pendant la durée nécessaire aux retries et à l’observabilité.
- Le chemin raw et le retour DirectShow sont instrumentables avec des frontières d’horloge distinctes.
- Le lease d’alias historique peut être acquis, renouvelé, libéré et refusé de manière déterministe.

## 5. Invariants architecturaux

Les invariants suivants sont normatifs pour `r2`.

- **I1 — Deux lanes physiques** : les lanes A et B existent simultanément et restent chaudes pendant le fonctionnement nominal.
- **I2 — Indépendance des roots** : les roots de scène et objets mutables des lanes A et B ne sont pas partagés entre `OnAir` et `Preview`, sauf métadonnées explicitement immuables.
- **I3 — Rôles permutables** : `OnAir` et `Preview` sont des rôles logiques pointant vers une lane ; la lane physique n’est pas le rôle.
- **I4 — Surfaces stables** : `ProgramView` et `PreviewView` conservent leur identité pendant les Takes.
- **I5 — Sorties stables** : encodeur, stream, record et `ProgramReturn` sont liés une fois à `ProgramView`; `PreviewReturn` est lié une fois à `PreviewView`.
- **I6 — Aucun rebind actif** : un Take ne modifie jamais le `video_t` d’un encodeur actif.
- **I7 — Cut atomique** : le changement des roots/routages se fait en une opération atomique à une frontière de frame ; aucune frame ne doit exposer un état mixte.
- **I8 — Producteurs vivants** : les producteurs A/B restent en vie pendant et après le Take ; le changement ne dépend pas d’une reconstruction synchrone.
- **I9 — Cycle de vie Preview post-Take** : avant `TakeCommitted`, la lane qui doit être promue et la future lane Preview ne peuvent pas être mutées de manière non versionnée. À `TakeCommitted`, l’ancienne lane `Preview` devient `OnAir` et l’ancienne lane `OnAir` devient la nouvelle lane `Preview` disponible. Après ce commit seulement, cette nouvelle Preview peut être re-préparée ou mutée ; ses mutations ne touchent jamais `Program`.
- **I10 — Commandes ordonnées** : les commandes portent `command_id`, `intent_id`, révisions attendues et séquence serveur ; les commandes obsolètes sont rejetées sans mutation.
- **I11 — Idempotence** : la répétition d’un même `command_id` avec le même payload retourne le résultat antérieur et ne crée pas de second commit.
- **I12 — Audio explicite** : le lot initial conserve un chemin audio commun et explicitement nommé pour le Programme ; aucune indépendance Preview/AFV n’est affirmée.
- **I13 — Namespace runtime et alias legacy** : les mappings internes sont namespacés par `runtime_instance_id`. Les alias historiques DirectShow `Program`/`Preview` constituent une namespace de compatibilité singleton : une seule instance peut en obtenir le lease explicite ; les autres doivent être refusées ou employer des filtres/noms dédiés.
- **I14 — Mesures séparées** : les mesures raw, retour DirectShow, premier paquet RTMP et première frame décodée/antenne sont enregistrées séparément.

## 6. Décision

### 6.1 Topologie vidéo

Adopter deux lanes physiques chaudes et indépendantes :

```text
Lane A ─┐
        ├─ mapping atomique des rôles ── OnAir ──> ProgramView ──> encoder/stream/record/ProgramReturn
Lane B ─┘                                  Preview ─> PreviewView ─> PreviewReturn
```

Le mapping logique est de la forme :

```text
role_map = {
  OnAir:   lane_id,
  Preview: other_lane_id
}
```

avec `lane_id != other_lane_id`.

`ProgramView` et `PreviewView` sont des surfaces vidéo stables. Elles ne sont pas remplacées lors d’un `Take`; le contenu qu’elles présentent est routé à partir du mapping logique courant.

Le cycle de vie d’un `Take` est explicitement le suivant :

1. Avant le Take, une lane est `OnAir` et l’autre est `Preview`.
2. `Prepare` prépare le contenu sur la lane `Preview` courante.
3. Une fois `TakeAccepted` émis, les roots/routages concernés par la promotion sont gelés jusqu’au commit ; aucune mutation de la future `Preview` ne peut être appliquée pendant cet intervalle.
4. À `TakeCommitted`, la lane `Preview` courante devient `OnAir`.
5. L’ancienne lane `OnAir` devient alors la nouvelle lane `Preview` disponible.
6. Cette nouvelle Preview peut être re-préparée ou mutée seulement après `TakeCommitted`.
7. Toute demande de préparation arrivant avant le commit qui tenterait de muter la future Preview doit être rejetée ou mise en file, sans mutation partielle.

Le `Take` échange le mapping des roots/routages à la frontière de frame. Il ne réaffecte pas le `video_t` d’un encodeur actif et ne reconstruit pas les producteurs.

Le primitive libobs/Pulsar recommandé est volontairement étroit, conceptuellement équivalent à :

```text
atomic_scene_role_swap(
    expected_revisions,
    next_role_map,
    frame_boundary,
    command_id,
    intent_id
) -> TakeCommitted(frame_id, pts, new_revisions)
```

Le nom et la signature réels restent à confirmer par Conduit et Forge lors de l’implémentation. Le contrat observable, lui, est normatif.

### 6.2 Contrat `pulsar.scene-switch.v1`

Le protocole minimal est :

```text
PrepareAccepted
  -> PreviewReady(first_frame_id, first_pts)
  -> TakeAccepted
  -> TakeCommitted(frame_id, pts)
```

Chaque commande comporte au minimum :

- `command_id` : identifiant unique de tentative ;
- `intent_id` : identifiant stable d’intention, conservé lors des retries ;
- `runtime_instance_id` ;
- `expected_revisions` par flux concerné ;
- éventuellement le `expected_server_seq` correspondant ;
- lane/root cible et paramètres de préparation ;
- timestamp monotone côté serveur pour la mesure.

Chaque événement comporte au minimum :

- `command_id` ;
- `intent_id` ;
- `server_seq` monotone ;
- révisions avant/après par flux ;
- état de la machine ;
- `first_frame_id` et `first_pts` pour `PreviewReady` ;
- `frame_id` et `pts` de commit pour `TakeCommitted` ;
- cause stable en cas de rejet.

Règles :

1. Un `Prepare` accepté ne vaut pas commit.
2. `PreviewReady` prouve que la première frame Preview requise est disponible.
3. `TakeAccepted` réserve l’intention si les révisions attendues sont encore valides.
4. Entre `TakeAccepted` et `TakeCommitted`, les roots et routages participant à la promotion sont gelés contre toute mutation non versionnée.
5. La mutation de routage n’a lieu qu’au `TakeCommitted`.
6. Après `TakeCommitted`, l’ancienne OnAir devient la seule nouvelle Preview disponible pour une préparation ultérieure.
7. Une révision obsolète entraîne un rejet stable, sans mutation partielle.
8. La répétition d’un `command_id` identique renvoie le résultat déjà produit.
9. La réutilisation d’un `command_id` avec un payload différent est rejetée comme conflit d’idempotence.
10. Deux Takes concurrents sur la même révision ne peuvent pas tous deux committer.
11. Toute commande bloquée ou expirée laisse le mapping courant intact.
12. `TakeCommitted` doit toujours référencer la frame et le PTS de la frontière effectivement utilisée.

### 6.3 Audio du lot initial

Le lot initial fixe un chemin audio commun explicitement rattaché au Programme. Un `Cut` vidéo ne doit pas provoquer implicitement une permutation audio non spécifiée.

L’architecture r2 ne promet pas :

- un mix Preview indépendant ;
- un AFV ;
- une permutation audio parallèle aux lanes.

Si l’implémentation actuelle ne permet pas de démontrer le chemin audio commun, le changement doit être traité comme un défaut de validation et non comme une hypothèse silencieuse.

### 6.4 Isolation des instances et alias DirectShow historiques

Chaque instance reçoit un `runtime_instance_id` unique et non vide.

Les mappings internes doivent être dérivés ou protégés par ce namespace pour :

- `cwd` ;
- `config.json` ;
- ports ;
- logs ;
- enregistrements ;
- mappings internes ;
- noms de filtres internes ;
- sockets ou fichiers temporaires propres au runtime.

Les alias historiques DirectShow `Program` et `Preview` ne sont pas considérés comme quatre namespaces indépendants. Ils constituent une namespace de compatibilité singleton.

La règle de compatibilité est :

- une seule instance peut acquérir un lease explicite sur les alias legacy `Program`/`Preview` ;
- une deuxième instance doit être refusée avec une erreur stable tant que le lease est détenu ;
- une instance sans lease peut fonctionner avec des filtres ou noms d’instance dédiés ;
- aucune instance ne peut prendre silencieusement le contrôle des alias ;
- l’acquisition, le renouvellement, la libération et l’expiration éventuelle du lease doivent être observables et corrélables à `runtime_instance_id`.

## 7. Alternatives crédibles rejetées

### Option A — Deux lanes chaudes et surfaces stables

C’est l’option retenue. Elle satisfait simultanément l’indépendance des scènes, la stabilité des consommateurs, l’absence de rebind actif et le maintien des producteurs.

### Option B — Recréer ou rebinder l’encodeur à chaque Take

Rejetée.

- Elle viole la contrainte du `video_t` actif.
- Elle introduit une interruption ou une fenêtre de reconfiguration.
- Elle rend le SLO de latence plus difficile à tenir.
- Elle augmente le risque de dérive encoder/stream/record/return.
- Elle ne traite pas correctement l’idempotence ni l’alias des roots.

### Option C — Conserver une seule lane avec verrouillage et copies ponctuelles

Rejetée pour le noyau.

- Un verrou de commande ne supprime pas l’alias des objets mutables.
- Une copie ou un snapshot ponctuel n’est pas équivalent à deux producteurs chauds.
- Le coût de copie et de synchronisation est incertain sous WGC + CEF + NVENC.
- La modification du Preview peut encore affecter le Program si le graphe n’est pas profondément séparé.

### Option D — Échanger directement les surfaces ou les `video_t`

Rejetée.

- Elle déstabilise les consommateurs aval.
- Elle entre en conflit avec l’impossibilité de changer le `video_t` actif.
- Elle rend les retours, enregistrements et encodeurs dépendants du timing de rebind.
- Elle ne donne pas une frontière de commit suffisamment claire.

### Option E — Ajouter uniquement révisions et idempotence au système actuel

Rejetée comme solution complète.

- Elle améliore les courses de commandes mais ne supprime pas l’alias Program/Preview.
- Elle ne garantit pas l’indépendance des roots.
- Elle ne résout ni la stabilité des surfaces ni le problème du `video_t` actif.

Règle de choix : aucune option n’est acceptable si elle ne respecte simultanément I2, I4, I5, I6, I7, I9 et I10. L’option A est la seule option crédible identifiée qui les satisfait sans reconfiguration de l’encodeur.

## 8. Conséquences

### Conséquences positives

- L’alias Program/Preview devient détectable et doit disparaître.
- Un Take peut échanger le rôle logique des lanes sans reconstruire les producteurs.
- Les consommateurs aval voient toujours les mêmes surfaces.
- L’encodeur actif reste lié à son `video_t` ou à sa surface stable.
- Les doubles Takes et retries deviennent déterministes.
- Les commandes obsolètes ne modifient plus silencieusement le routage.
- Les événements peuvent être corrélés de bout en bout par `runtime_instance_id`, `command_id`, `intent_id`, révision et PTS.
- L’isolation de plusieurs runtimes devient explicite.
- Les alias DirectShow historiques ne sont plus supposés multi-instance ; leur lease ou leur remplacement par des filtres dédiés devient vérifiable.
- Les SLO raw et retour DirectShow deviennent mesurables indépendamment du délai de décodage.
- Le noyau ADR peut être clôturé sur ses preuves propres sans attendre les extensions.

### Conséquences négatives

- Deux lanes chaudes augmentent la durée de vie des ressources.
- Les coûts déjà observés d’environ `+0,091 ms/frame` et `+3,13 MB` pour deux vues simples ne suffisent pas à prouver la capacité sous WGC + CEF + NVENC.
- La complexité de cycle de vie, de nettoyage et de récupération augmente.
- Un magasin d’idempotence et une machine de révisions sont nécessaires.
- La stratégie audio est volontairement limitée au Programme commun dans r2.
- Les transitions opérateur restent indisponibles jusqu’à validation dédiée.
- Les alias legacy DirectShow peuvent limiter la compatibilité à une instance tant qu’aucun filtre ou nom dédié n’est utilisé.
- Un rollback vers l’ancien chemin peut restaurer la disponibilité mais ne corrige pas le défaut historique d’alias ; ce chemin doit donc être explicitement marqué comme mode dégradé.
- Les extensions non bloquantes auront potentiellement besoin d’une nouvelle ADR si elles modifient les invariants ou le contrat.

## 9. Pre-mortem

| Échec supposé | Signal précoce | Mitigation |
|---|---|---|
| L’alias Program/Preview persiste malgré les lanes | Une modification du nouveau Preview change le hash ou le contenu de `ProgramView` dans les 30 frames suivantes | Test d’identité des roots, comparaison de frame hashes, mutation systématique du Preview uniquement après `TakeCommitted` |
| Une mutation touche la future Preview avant le commit | Une commande de préparation modifie l’ancienne OnAir entre `TakeAccepted` et `TakeCommitted` | Geler la lane future Preview durant la transaction ; rejeter ou mettre en file toute mutation prématurée |
| Deux Takes concurrents committent tous les deux | Deux `TakeCommitted` pour une même révision ou un même `intent_id` | CAS sur les révisions, séquence serveur monotone, test de concurrence et rejet stale avant toute mutation |
| Le SLO échoue sous WGC + CEF + NVENC | p95 raw > 50 ms ou p95 retour DirectShow > 75 ms alors que le test simple passe | Mesures séparées par frontière, maintien des producteurs chauds, profilage avant toute optimisation, tuning DirectShow/NVENC en follow-up |
| Une instance lit la configuration d’une autre | Ports, logs, mappings ou `config.json` identiques entre deux runtimes | `runtime_instance_id` obligatoire, namespace vérifié au démarrage, test de coexistence et refus de collision |
| Deux instances prennent les alias DirectShow historiques | Le second runtime acquiert `Program`/`Preview` sans lease ou remplace le premier | Lease singleton explicite, erreur stable en cas de possession, filtres/noms dédiés pour les autres instances |
| Le chemin audio suit implicitement la lane vidéo | Changement de source, rupture de PTS ou discontinuité audio lors d’un Cut | Route Programme audio nommée et stable, preuve de continuité audio, refus de déclarer Preview/AFV supporté en r2 |
| Une commande reste bloquée après `TakeAccepted` | État durablement coincé sans commit ni rejet | Timeout explicite, état d’abort, mapping inchangé tant que le commit n’a pas eu lieu, métrique de Takes pendants |
| La première frame décodée est prise à tort pour un échec du Cut | Premier paquet RTMP dans le budget mais frame décodée à 136/253 ms | Séparer les horloges et SLO raw, retour DirectShow, RTMP, décodage et antenne |
| Une lane n’est plus réellement chaude | Recréation de producteur, frame noire ou identifiant de producteur changé au Take | Instrumenter les identifiants de producteurs, interdire le rebind dans le primitive de swap, test de 100 Takes consécutifs |

## 10. Migration par étapes

### Étape 0 — Baseline et instrumentation minimale

Capturer les mesures existantes avec les frontières suivantes :

- `PrepareAccepted → PreviewReady` ;
- `TakeAccepted → TakeCommitted` ;
- `TakeAccepted → première frame Program valide entrée encodeur/raw` ;
- `TakeAccepted → première frame Program valide observée sur le retour DirectShow` ;
- premier paquet RTMP séparément ;
- première frame décodée et antenne séparément, sans SLO r2.

Conserver les baselines connues :

- raw p95 approximativement `32–35 ms` ;
- DirectShow p95 approximativement `57–67 ms`, à remesurer à la frontière normative du retour DirectShow ;
- RTMP premier paquet p95 `≤15 ms` ;
- décodage x264/NVENC observé à environ `136/253 ms`.

Aucune comparaison directe entre la métrique raw et la métrique retour DirectShow ne doit être faite sans normalisation de la frontière.

### Étape 1 — Isolation runtime et alias legacy

Introduire `runtime_instance_id` et les namespaces `cwd`, configuration, ports, logs, recordings et mappings internes.

Ajouter le contrôle d’alias historique :

- acquisition explicite du lease `Program`/`Preview` ;
- refus déterministe d’un second détenteur ;
- filtres ou noms dédiés pour les instances qui ne possèdent pas le lease ;
- observation de l’identité du détenteur.

Aucun changement de topologie vidéo ne doit être activé avant qu’au moins quatre instances internes puissent coexister sans collision et que le comportement singleton des alias legacy soit démontré.

### Étape 2 — Contrat de commande en mode contrôlé

Introduire `pulsar.scene-switch.v1` et la machine d’état en mode shadow ou contrôlé :

- génération des révisions ;
- validation de `expected_revisions` ;
- séquence serveur ;
- déduplication des `command_id` ;
- conservation du résultat d’un retry ;
- rejet explicite des stale commands ;
- blocage des mutations de la future Preview avant `TakeCommitted`.

À cette étape, une commande refusée ne doit pas modifier le mapping historique.

### Étape 3 — Deux lanes physiques

Créer ou encapsuler les lanes A/B et leurs roots indépendants. Vérifier que les producteurs restent vivants et que les roots mutables ne sont pas partagés.

### Étape 4 — Surfaces stables

Introduire `ProgramView` et `PreviewView`. Lier une fois les consommateurs :

- encodeur ;
- stream ;
- record ;
- `ProgramReturn` vers `ProgramView` ;
- `PreviewReturn` vers `PreviewView`.

Le maintien de l’identité de ces surfaces doit être observable.

### Étape 5 — Primitive de Cut atomique

Implémenter le primitive étroit de swap des roots/routages à une frontière de frame. Aucun `video_t` actif ne doit être rebinding lors de cette opération.

Vérifier explicitement le cycle :

```text
Preview courante → OnAir à TakeCommitted
ancienne OnAir → nouvelle Preview disponible après TakeCommitted
nouvelle Preview mutable uniquement après le commit
```

### Étape 6 — Audio Programme commun

Rendre explicite le chemin audio Programme commun, sans annoncer de Preview audio indépendant. Vérifier que les Cuts vidéo ne provoquent pas de permutation audio implicite.

### Étape 7 — Validation des courses et de l’alias

Exécuter les tests :

- double Take ;
- retry identique ;
- même `command_id` avec payload différent ;
- expected revision stale ;
- tentative de mutation de la future Preview avant commit ;
- mutation de la nouvelle Preview après commit ;
- Takes concurrents sur une même révision ;
- lease concurrent des alias DirectShow historiques.

### Étape 8 — Canary x264 et NVENC

Activer le dual-lane sur un périmètre canary en 1080p60 pour x264 et NVENC. Mesurer :

- raw jusqu’à l’entrée encodeur ;
- retour DirectShow jusqu’à la première frame Programme valide observée ;
- premier paquet RTMP ;
- décodage et antenne séparément.

Le comportement décodé/antenne reste observé mais hors garantie r2.

### Étape 9 — Follow-ups optionnels après le noyau

Après validation et clôture du noyau, traiter séparément :

- transitions `Fade`/`Stinger` ;
- interaction `T-bar` ;
- audio Preview/AFV ;
- tuning DirectShow/NVENC decoder ;
- charge, soak et recovery complets.

Ces éléments peuvent être conservés comme issues optionnelles provisoires ou requalifiés dans une nouvelle ADR. Ils ne doivent pas retarder la clôture core si `PUL-DL-08` fournit toutes les preuves du noyau.

## 11. Rollback

Le mécanisme de déploiement doit être protégé par un feature flag ou une capacité runtime désactivable, par exemple `dual_lane_enabled`.

Le rollback se fait uniquement à une frontière de frame :

1. arrêter l’acceptation de nouveaux Takes ;
2. laisser le `ProgramView` courant produire une frame valide ;
3. conserver le mapping courant si un swap n’a pas atteint `TakeCommitted` ;
4. marquer les commandes pendantes comme annulées ou expirées ;
5. désactiver le dual-lane pour les nouvelles commandes ;
6. libérer ou invalider proprement le lease d’alias si l’instance l’a acquis ;
7. retourner au chemin de compatibilité uniquement comme mode de disponibilité dégradé ;
8. ne jamais changer le `video_t` actif pendant le rollback ;
9. conserver les événements, révisions et raisons de rollback.

Déclencheurs de rollback :

- violation observée de I2, I4, I5, I6, I7, I9 ou I13 ;
- alias Program/Preview reproduit une seule fois en production ;
- commit double ou révision régressée ;
- deux fenêtres de mesure consécutives au-dessus du SLO ;
- collision de namespace ;
- collision ou usurpation d’un alias DirectShow détenu ;
- sortie Programme invalide, noire ou non corrélable.

Le rollback n’est pas considéré comme la correction de l’alias historique. Une fois la cause comprise et corrigée, la validation complète du dual-lane doit être relancée.

## 12. Observabilité et preuves

Chaque événement de commande doit porter :

- `runtime_instance_id` ;
- `command_id` ;
- `intent_id` ;
- lane source et lane cible ;
- révisions attendues et réelles ;
- `server_seq` ;
- état de la machine ;
- timestamps monotones de chaque transition ;
- `first_frame_id` et PTS ;
- `TakeCommitted.frame_id` et PTS ;
- surface de sortie et producteur concernés ;
- état du lease DirectShow legacy si concerné.

Métriques minimales :

- compte de `PrepareAccepted`, `PreviewReady`, `TakeAccepted`, `TakeCommitted` ;
- compte de rejets stale ;
- compte de doublons idempotents ;
- conflits d’idempotence ;
- timeouts et aborts ;
- latences p50/p95/p99 par chemin ;
- latence raw jusqu’à l’entrée encodeur ;
- latence retour DirectShow jusqu’à la première frame Programme valide observée ;
- premier paquet RTMP ;
- première frame décodée et antenne, séparées ;
- identité et santé des producteurs A/B ;
- identité des surfaces `ProgramView` et `PreviewView` ;
- CPU, RAM, GPU, WGC, CEF et NVENC ;
- profondeur de files encodeur ;
- continuité audio et PTS ;
- collisions de `runtime_instance_id` ou namespace ;
- acquisitions, refus, renouvellements et libérations de lease legacy.

Alertes :

- `TakeAccepted` sans `TakeCommitted` au-delà du timeout ;
- `TakeCommitted` multiple pour le même `intent_id` ;
- révision ou `server_seq` non monotone ;
- tentative de mutation de la future Preview avant commit ;
- changement d’identité de surface ;
- changement de `video_t` actif ;
- divergence de frame hash entre Program et Preview après mutation ;
- p95 raw > 50 ms ;
- p95 retour DirectShow > 75 ms ;
- rupture audio ;
- collision de configuration, ports, logs ou mappings ;
- second runtime acceptant un alias legacy sans lease ;
- lease expiré ou détenu par une instance non joignable.

## 13. Critères d’acceptation testables

Les critères suivants sont normatifs pour le noyau r2.

- **AC-01 — Lanes chaudes** : après warm-up, au moins 100 Takes consécutifs en 1080p60 avec x264 et au moins 100 avec NVENC conservent les identifiants des producteurs A/B ; aucune reconstruction de producteur ni rebind du `video_t` actif n’est observé.
- **AC-02 — Surfaces stables** : `ProgramView` et `PreviewView` conservent leur identité sur au moins 100 Takes ; encodeur, stream, record et `ProgramReturn` restent attachés à `ProgramView`, et `PreviewReturn` à `PreviewView`.
- **AC-03 — Cut atomique** : chaque Take committé contient `frame_id` et PTS ; le changement de routage est associé à une frontière de frame et aucune frame mixte n’est observée sur l’échantillon de test.
- **AC-04 — Cycle de vie et absence d’alias** : sur au moins 100 séquences, une tentative de mutation de la future Preview entre `TakeAccepted` et `TakeCommitted` est rejetée ou mise en file sans effet observable. Après `TakeCommitted`, l’ancienne OnAir est la nouvelle Preview disponible ; sa mutation pendant au moins 30 frames n’altère pas `ProgramView`. L’ancienne Preview promue devient OnAir et ne peut plus être mutée comme Preview.
- **AC-05 — Idempotence** : sur au moins 1 000 essais concurrents ou retries contrôlés, un `command_id` répété avec payload identique retourne le même résultat sans second commit ; un payload différent sous le même identifiant est rejeté.
- **AC-06 — Stale rejection** : une commande avec `expected_revisions` obsolètes est rejetée sans mutation de mapping, sans changement de surface et sans incrément de révision métier.
- **AC-07 — SLO raw** : sur au moins 100 Takes warm-up par codec, la mesure `TakeAccepted → première frame Program valide à l’entrée de l’encodeur/raw` respecte p95 `≤ 50 ms` sur le chemin raw.
- **AC-08 — SLO DirectShow retour** : sur au moins 100 Takes warm-up par codec, la mesure `TakeAccepted → première frame Program valide observée sur le retour DirectShow` respecte p95 `≤ 75 ms`. Cette mesure est distincte de AC-07 et ne doit pas être remplacée par une mesure à l’entrée encodeur.
- **AC-09 — Audio Programme** : la source ou route du Programme audio reste stable pendant au moins 100 Cuts ; les PTS restent monotones et aucune permutation Preview implicite n’est observée. L’absence de Preview audio indépendant est explicitement documentée.
- **AC-10 — Isolation runtime et alias legacy** : au moins quatre instances concurrentes, chacune avec un `runtime_instance_id` distinct, n’ont aucune collision de `cwd`, `config.json`, ports, logs, recordings ou mappings internes. Lors d’un essai d’utilisation des alias historiques DirectShow `Program`/`Preview`, une seule instance peut acquérir le lease ; toute seconde instance est refusée tant que le lease est détenu, ou doit utiliser des filtres/noms dédiés. Aucun partage silencieux n’est accepté.
- **AC-11 — Événements complets** : 100 % des commandes acceptées et rejetées contiennent les identifiants et révisions requis ; toute transition peut être corrélée à une frame et un PTS.
- **AC-12 — RTMP comme garde-fou séparé** : le premier paquet RTMP est mesuré séparément et comparé à la baseline p95 `≤15 ms`; il ne remplace ni AC-07 ni AC-08 et ne constitue pas une garantie de première frame décodée.
- **AC-13 — Charge explicitement mesurée** : l’impact WGC + CEF + NVENC est mesuré et comparé à la référence connue de `+0,091 ms/frame` et `+3,13 MB`; aucune capacité de production n’est déclarée sur la seule base de cette référence. Le soak complet n’est pas requis pour la clôture core mais reste un follow-up.
- **AC-14 — Rollback sûr** : un timeout, stale rejection ou abort ne modifie pas le mapping courant ; un rollback n’effectue aucun changement de `video_t` actif et ne laisse pas un lease legacy ambigu.

## 14. Verdict

`ADR_VERDICT: PENDING_REVIEW`

Recommandation : `GO` conditionnel pour le noyau de conception dual-lane, sous réserve de validation exacte de cette révision par Vigil, puis d’une approbation humaine explicite de :

```text
ADR-PULSAR-DUAL-LANE-001
revision_id=draft-r2-dual-lane-20260828
```

La clôture core de l’ADR peut être évaluée dès que `PUL-DL-08` et toutes les preuves du lot initial sont terminés. Elle ne doit pas attendre :

- `PUL-DL-09` Fade/Stinger ;
- `PUL-DL-10` T-bar ;
- `PUL-DL-11` Preview audio/AFV ;
- `PUL-DL-12` tuning DirectShow/NVENC ;
- `PUL-DL-13` load/soak/recovery complet.

Ces unités restent des follow-ups optionnels et non bloquants, à conserver sous cette ADR uniquement si leur contenu reste compatible avec ses invariants, ou à redécouper dans une nouvelle ADR si elles modifient la décision.

Aucune issue GitHub dérivée ne doit être créée avant :

1. `ADR_VERDICT: APPROVED` de Vigil citant exactement cet ADR-ID et cette révision ;
2. approbation humaine explicite de cette même révision ;
3. persistance, push et merge de l’ADR par Vigil ;
4. signal `ADR_MERGED` avec PR et SHA canonique.

# Graphe provisoire des unités de travail

Les identifiants ci-dessous sont provisoires et stables dans cette révision. Ils ne sont pas encore des `work_unit_id` GitHub canoniques. Chaque unité est indépendante sauf lorsque `depends_on` l’indique ; `continues=null` partout signifie qu’aucune unité n’est une continuation implicite d’une autre.

Les unités `PUL-DL-01` à `PUL-DL-08` constituent le noyau. `PUL-DL-09` à `PUL-DL-13` sont des follow-ups optionnels non bloquants. `PUL-DL-14` est la clôture core et ne dépend que du noyau.

## Gate G0 — validation ADR

```text
G0: Vigil review de ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane-20260828
    + approbation humaine
    + ADR_MERGED par Vigil
```

Tant que G0 n’est pas franchi, les unités ci-dessous restent des brouillons.

## G1 — unités prêtes en parallèle après G0

### PUL-DL-01 — Contrat `pulsar.scene-switch.v1`

- `continues` : `null`
- `depends_on` : `null`
- groupe : `G1`
- état initial post-G0 : `ready`
- rôle recommandé : `Conduit`
- périmètre possédé : événements `PrepareAccepted`, `PreviewReady`, `TakeAccepted`, `TakeCommitted`; `command_id`; `intent_id`; révisions; `server_seq`; expected revisions; idempotence; stale rejection; gel avant commit.
- exclusions : aucune topologie vidéo concrète, aucune UI, aucun tuning encodeur, aucune définition de Preview audio.
- entrées : ADR r2, invariants I7/I9/I10/I11, besoins de retry et concurrence.
- sorties : schéma versionné, machine d’état, erreurs stables, règles de déduplication et de commit.
- critères : AC-04, AC-05, AC-06, AC-11, AC-14.
- preuves attendues : schéma exact, tests de transitions, tests de duplicate/stale/conflit, test d’interdiction de mutation pré-commit, exemples d’événements corrélables.
- risque : protocole trop large ou ambigu, entraînant des implémentations incompatibles.
- `required_agent_reports` initial : `Conduit`, `Probe`.
- clauses ADR : I7, I9, I10, I11, I14.

### PUL-DL-02 — Isolation `runtime_instance_id` et lease DirectShow

- `continues` : `null`
- `depends_on` : `null`
- groupe : `G1`
- état initial post-G0 : `ready`
- rôle recommandé : `Keeper`
- périmètre possédé : namespace par instance pour `cwd`, `config.json`, ports, logs, recordings et mappings internes ; lease exclusif des alias historiques DirectShow ; filtres/noms dédiés pour les autres instances.
- exclusions : changement de scène, surface vidéo, protocole de Take, orchestration de transitions.
- entrées : ADR r2, invariants I13, collisions connues et contraintes DirectShow historiques.
- sorties : règle de génération/validation du runtime ID, détection de collision, API de lease, stratégie de cleanup et refus stable.
- critères : AC-10, AC-11, AC-14.
- preuves attendues : test avec au moins quatre instances internes simultanées, test de possession singleton des alias, refus du second détenteur, fonctionnement avec filtres/noms dédiés.
- risque : fuite de configuration ou de mapping entre instances, ou usurpation d’un alias legacy.
- `required_agent_reports` initial : `Keeper`, `Bastion`.
- clauses ADR : I13, I14, AC-10.

## G2 — noyau vidéo

### PUL-DL-03 — Lanes A/B, surfaces stables et Cut atomique

- `continues` : `null`
- `depends_on` : `[PUL-DL-01, PUL-DL-02]`
- groupe : `G2`
- état initial : `queued`
- rôle recommandé : `Forge`
- périmètre possédé : roots physiques A/B, mapping logique `OnAir`/`Preview`, `ProgramView`, `PreviewView`, primitive atomique de swap à frontière de frame, maintien en vie des producteurs et cycle de vie post-Take.
- exclusions : transitions Fade/Stinger/T-bar, Preview audio/AFV, tuning DirectShow/NVENC, reconfiguration du `video_t` actif.
- entrées : contrat PUL-DL-01, isolation PUL-DL-02, invariants I1–I9.
- sorties : topologie dual-lane, surfaces stables, primitive de Cut, télémétrie d’identité des roots/surfaces/producteurs.
- critères : AC-01, AC-02, AC-03, AC-04, AC-14.
- preuves attendues : diagramme de routage réel, tests d’identité, test de gel avant commit, 100 Takes x264/NVENC, preuve d’absence de rebind et de frame mixte.
- risque : séparation superficielle laissant un alias mutable, permettant une mutation pré-commit ou changeant indirectement le `video_t`.
- `required_agent_reports` initial : `Forge`, `Probe`.
- clauses ADR : I1, I2, I3, I4, I5, I6, I7, I8, I9.

## G3 — audio du Programme

### PUL-DL-04 — Route audio Programme commune et explicite

- `continues` : `null`
- `depends_on` : `[PUL-DL-01, PUL-DL-03]`
- groupe : `G3`
- état initial : `queued`
- rôle recommandé : `Conduit`
- périmètre possédé : chemin audio commun attaché au Programme, invariants de continuité et non-permutation implicite pendant un Cut.
- exclusions : audio Preview indépendant, AFV, mixage opérateur avancé.
- entrées : topologie PUL-DL-03, contrat PUL-DL-01, inconnue audio actuelle.
- sorties : contrat audio r2, route stable, métriques PTS et continuité, documentation de l’absence de Preview/AFV.
- critères : AC-09, AC-11.
- preuves attendues : source/route observée avant et après 100 Cuts, série PTS, preuve qu’un changement Preview vidéo n’altère pas l’audio Programme.
- risque : supposer une indépendance audio non démontrée ou lier accidentellement l’audio au swap vidéo.
- `required_agent_reports` initial : `Conduit`, `Probe`.
- clauses ADR : I12, I14.

## G4 — validations indépendantes en parallèle

### PUL-DL-05 — Instrumentation et banc SLO

- `continues` : `null`
- `depends_on` : `[PUL-DL-02, PUL-DL-03]`
- groupe : `G4`
- état initial : `queued`
- rôle recommandé : `Probe`
- périmètre possédé : horloges, événements, corrélation frame/PTS, mesures raw/retour DirectShow/RTMP/décodage, compteurs ressources et lease.
- exclusions : modification du comportement de Cut, tuning de codec, garantie decoded/antenna.
- entrées : surfaces et événements PUL-DL-03, namespace PUL-DL-02.
- sorties : métriques et traces séparées, rapport p50/p95/p99, scripts de banc x264/NVENC.
- critères : AC-07, AC-08, AC-11, AC-12, AC-13.
- preuves attendues : résultats sur au moins 100 Takes warm-up par chemin et codec, définition exacte des timestamps, rapport charge WGC/CEF/NVENC.
- risque : mesurer une mauvaise frontière et déclarer à tort le SLO respecté, notamment en confondant DirectShow retour et raw.
- `required_agent_reports` initial : `Probe`, `Keeper`.
- clauses ADR : I8, I14, AC-07, AC-08, AC-12, AC-13.

### PUL-DL-06 — QA alias, cycle de vie, concurrence et idempotence

- `continues` : `null`
- `depends_on` : `[PUL-DL-01, PUL-DL-03]`
- groupe : `G4`
- état initial : `queued`
- rôle recommandé : `Probe`
- périmètre possédé : tests de mutation du Preview avant/après commit, double Take, retries, stale revisions, conflit de `command_id`, commit unique par intention.
- exclusions : mesure de capacité longue durée, transitions non-Cut, audio AFV.
- entrées : protocole PUL-DL-01, topologie PUL-DL-03.
- sorties : suite QA déterministe, matrice de races, tests de non-régression.
- critères : AC-04, AC-05, AC-06, AC-14.
- preuves attendues : au moins 100 séquences d’alias et 1 000 essais contrôlés de commandes concurrentes/retries ; preuve que l’ancienne OnAir ne devient mutable qu’après commit.
- risque : ne tester que des scénarios séquentiels et manquer une course réelle ou une mutation pré-commit.
- `required_agent_reports` initial : `Probe`.
- clauses ADR : I2, I7, I9, I10, I11.

### PUL-DL-07 — Revue Bastion de la surface de contrôle et de l’isolation

- `continues` : `null`
- `depends_on` : `[PUL-DL-01, PUL-DL-02, PUL-DL-03]`
- groupe : `G4`
- état initial : `queued`
- rôle recommandé : `Bastion`
- périmètre possédé : analyse des races, stale commands, collisions runtime, lease des alias, séparation des namespaces, exposition des identifiants et journalisation.
- exclusions : ajout de fonctionnalités produit, optimisation de codec, création d’authentification non demandée.
- entrées : contrat, topologie, modèle de namespace et lease.
- sorties : avis sécurité/robustesse, findings priorisés, conditions de mise en production.
- critères : absence de mutation stale, absence de collision inter-instance, absence de prise d’alias sans lease, absence de secret dans les traces ; couverture AC-05, AC-06, AC-10, AC-11.
- preuves attendues : rapport Bastion avec menaces, scénarios d’abus, décisions de risque et mitigations vérifiables.
- risque : accepter une commande d’une instance ou d’une révision étrangère, ou autoriser deux détenteurs d’un alias historique.
- `required_agent_reports` initial : `Bastion`.
- clauses ADR : I10, I11, I13, I14.

## G5 — intégration et preuve du noyau

### PUL-DL-08 — Intégration, canary et exploitation du noyau

- `continues` : `null`
- `depends_on` : `[PUL-DL-04, PUL-DL-05, PUL-DL-06, PUL-DL-07]`
- groupe : `G5`
- état initial : `queued`
- rôle recommandé : `Keeper`
- périmètre possédé : activation contrôlée du noyau, feature flag, canary x264/NVENC, runbook de rollback, collecte CI/observabilité et consolidation des preuves core.
- exclusions : transitions avancées, Preview audio/AFV, tuning dédié et soak complet ; ces éléments sont des follow-ups non bloquants.
- entrées : toutes les preuves du noyau initial.
- sorties : configuration canary, runbook rollback, rapport d’intégration, décision d’activation core.
- critères : AC-01 à AC-14 applicables au noyau ; aucun invariant I1–I14 en échec.
- preuves attendues : CI verte, canary x264/NVENC, métriques raw et retour DirectShow, preuve de lease/isolation, journal de rollback testé.
- risque : activer le noyau avant d’avoir réconcilié des preuves contradictoires.
- `required_agent_reports` initial : `Keeper`, `Probe`, `Bastion`.
- clauses ADR : ensemble I1–I14 du noyau.

## G6 — extensions optionnelles post-noyau

Les unités suivantes ne bloquent pas la clôture core par `PUL-DL-14`. Elles peuvent être exécutées après `PUL-DL-08`, mais il est préférable de les requalifier dans une nouvelle ADR si elles modifient le contrat, les invariants ou les SLO.

### PUL-DL-09 — Fade et Stinger

- `continues` : `null`
- `depends_on` : `[PUL-DL-08]`
- groupe : `G6`
- état initial : `queued`
- statut de blocage : `non_blocking_follow_up=true`
- rôle recommandé : `Forge`
- périmètre possédé : transitions `Fade` et `Stinger` au-dessus du primitive de routage validé.
- exclusions : modification des invariants dual-lane, audio AFV, tuning DirectShow/NVENC.
- entrées : Cut atomique et surfaces stables.
- sorties : transitions versionnées, comportement d’interruption, métriques de durée.
- critères : aucune transition ne change le `video_t` actif ; résultats déterministes à frame boundary ; rollback vers Cut sûr.
- preuves attendues : tests de transition, interruption et retour Cut.
- risque : contourner le commit atomique avec une animation liée à une surface instable.
- `required_agent_reports` initial : `Forge`, `Probe`.
- clauses ADR : I4, I5, I6, I7, I9.

### PUL-DL-11 — Audio Preview et AFV

- `continues` : `null`
- `depends_on` : `[PUL-DL-04, PUL-DL-08]`
- groupe : `G6`
- état initial : `queued`
- statut de blocage : `non_blocking_follow_up=true`
- rôle recommandé : `Conduit`
- périmètre possédé : conception et validation d’un éventuel chemin Preview audio/AFV.
- exclusions : aucune modification silencieuse du contrat audio Programme r2.
- entrées : route audio commune validée et observabilité.
- sorties : décision audio séparée, contrat, tests de continuité et de rollback.
- critères : soit un contrat AFV complet et prouvé, soit un `descope` explicite ; aucun changement audio implicite pendant un Cut vidéo.
- preuves attendues : matrice audio Program/Preview, PTS, tests de pertes et reprises.
- risque : introduire une seconde machine d’état audio sans cohérence avec les révisions vidéo.
- `required_agent_reports` initial : `Conduit`, `Probe`, `Bastion`.
- clauses ADR : I10, I12, I14.

### PUL-DL-12 — Tuning DirectShow/NVENC et chaîne decoder

- `continues` : `null`
- `depends_on` : `[PUL-DL-05, PUL-DL-08]`
- groupe : `G6`
- état initial : `queued`
- statut de blocage : `non_blocking_follow_up=true`
- rôle recommandé : `Forge`
- périmètre possédé : réduction et compréhension de la latence DirectShow et du délai de première frame NVENC décodée, sans déplacer le SLO raw ni la frontière normative DirectShow retour.
- exclusions : redéfinition de l’architecture dual-lane, garantie decoded/antenna, rebind de `video_t`.
- entrées : mesures PUL-DL-05 et canary PUL-DL-08.
- sorties : tuning documenté, comparaison avant/après, limites de garantie.
- critères : aucune régression AC-07/AC-08 ; amélioration uniquement si elle est prouvée par mesures comparables.
- preuves attendues : p50/p95/p99 x264/NVENC, raw/retour DirectShow et décodé séparés.
- risque : optimiser le mauvais segment et dégrader la stabilité du Cut.
- `required_agent_reports` initial : `Forge`, `Probe`.
- clauses ADR : I6, I7, I14.

## G7 — UX/T-bar optionnel

### PUL-DL-10 — T-bar, interaction et accessibilité opérateur

- `continues` : `null`
- `depends_on` : `[PUL-DL-09]`
- groupe : `G7`
- état initial : `queued`
- statut de blocage : `non_blocking_follow_up=true`
- rôle recommandé : `Atelier`
- périmètre possédé : parcours opérateur, affordances Preview/OnAir, contrôle T-bar, états Pending/Committed/Rejected, accessibilité.
- exclusions : primitive libobs, protocole bas niveau, audio AFV, tuning encoder.
- entrées : contrat validé, transitions Fade/Stinger, événements d’état.
- sorties : spécification UX, critères d’accessibilité, tests du parcours réel.
- critères : l’opérateur distingue sans ambiguïté Preview, OnAir, préparation, acceptation, commit et rejet ; une action stale ou idempotente n’est pas présentée comme un second commit.
- preuves attendues : scénarios UX, captures ou tests instrumentés, validation clavier/états d’erreur.
- risque : l’interface masque une commande en attente ou fait croire à un commit qui n’a pas eu lieu.
- `required_agent_reports` initial : `Atelier`, `Probe`.
- clauses ADR : I3, I7, I10, I11.

## G8 — charge et résilience optionnelles

### PUL-DL-13 — Load, soak et recovery complets

- `continues` : `null`
- `depends_on` : `[PUL-DL-08, PUL-DL-09, PUL-DL-11, PUL-DL-12]`
- groupe : `G8`
- état initial : `queued`
- statut de blocage : `non_blocking_follow_up=true`
- rôle recommandé : `Keeper`
- périmètre possédé : charge WGC + CEF + NVENC, endurance, redémarrage, crash/recovery, coexistence de runtimes, saturation et rollback.
- exclusions : nouvelle fonctionnalité de transition, changement de contrat non approuvé.
- entrées : noyau canary, extensions validées, instrumentation complète.
- sorties : rapport capacité, seuils opérationnels, runbook incident/recovery.
- critères : SLO AC-07/AC-08 tenus dans la charge cible définie ; recovery ne change jamais le `video_t` actif à chaud ; namespace, mappings internes et lease legacy sont restaurés sans collision.
- preuves attendues : soak horodaté, scénarios de panne, métriques ressources, temps de recovery, validation rollback.
- risque : une fuite de ressources ou une saturation tardive n’apparaît pas dans le test court.
- `required_agent_reports` initial : `Keeper`, `Probe`, `Bastion`.
- clauses ADR : I1, I6, I8, I13, I14.

## G9 — clôture core ADR

### PUL-DL-14 — Revue core et clôture par Vigil

- `continues` : `null`
- `depends_on` : `[PUL-DL-08]`
- groupe : `G9`
- état initial : `queued`
- statut : `core_closure=true`, `extensions_non_blocking=true`
- rôle recommandé : `Vigil`
- périmètre possédé : couverture globale du noyau ADR, vérification des issues core terminées, absence de drift architectural sur r2, critères et preuves.
- exclusions : validation obligatoire de Fade/Stinger/T-bar, Preview audio/AFV, tuning decoder ou load/soak complet ; ces extensions peuvent être suivies séparément.
- entrées : rapports de `PUL-DL-01` à `PUL-DL-08`, CI, métriques, PR et preuves de rollback.
- sorties : `ADR_CLOSURE_VERDICT: CLOSABLE`, `GAPS_FOUND` ou `ADR_DRIFT`.
- critères : tous les invariants core I1–I14 et AC-01 à AC-14 applicables au noyau sont couverts ; aucune issue core oubliée ; aucune divergence entre implementation et ADR r2.
- preuves attendues : rapport Vigil de clôture et matrice clauses ADR → issues core → preuves ; liste explicite des extensions non bloquantes restantes.
- risque : clôturer sur une CI verte sans couvrir l’alias, les courses, le cycle de vie pré/post-commit, l’audio ou le lease DirectShow.
- `required_agent_reports` initial : `Vigil`, avec les rapports hérités des rôles ayant travaillé sur le noyau selon le protocole du work unit.
- clauses ADR : ensemble I1–I14 du noyau.

## Chemin critique core

```text
G0
  → PUL-DL-01 + PUL-DL-02
  → PUL-DL-03
  → PUL-DL-04
  → PUL-DL-05 + PUL-DL-06 + PUL-DL-07
  → PUL-DL-08
  → PUL-DL-14
```

Les extensions ne figurent pas dans le chemin critique de clôture core :

```text
PUL-DL-08
  ├─→ PUL-DL-09 ─→ PUL-DL-10       [optionnel]
  ├─→ PUL-DL-11                    [optionnel]
  ├─→ PUL-DL-12                    [optionnel]
  └─→ PUL-DL-13                    [optionnel]
```

Les premières unités prêtes simultanément après G0 sont `PUL-DL-01` et `PUL-DL-02`. Le premier groupe de validation réellement parallèle est `PUL-DL-05`, `PUL-DL-06` et `PUL-DL-07`.

## Passages obligatoires entre rôles

- `Conduit` : contrat `pulsar.scene-switch.v1`, séquencement des commandes, gel pré-commit, route audio Programme et éventuel AFV.
- `Forge` : lanes A/B, surfaces stables, primitive Cut et extensions de transition/tuning.
- `Probe` : mesures SLO raw et retour DirectShow, alias, races, idempotence, régressions et soak avec Keeper.
- `Bastion` : stale commands, isolation runtime, lease des alias historiques, exposition des namespaces, risque de double commit et revues avant canary/recovery.
- `Keeper` : isolation d’instance, feature flag, lease opérationnel, canary, rollback, charge et récupération.
- `Atelier` : parcours opérateur et accessibilité de T-bar/Preview/OnAir.
- `Vigil` : gate de cette révision avant toute issue GitHub, puis clôture core globale après exécution de `PUL-DL-08`.

## Changelog r1 → r2

La révision r2 applique uniquement les corrections sémantiques demandées :

1. **Clôture core découplée des extensions**
   - r1 : `PUL-DL-14` dépendait de `PUL-DL-10` et `PUL-DL-13`, ce qui rendait T-bar et load/soak nécessaires à la clôture.
   - r2 : `PUL-DL-14` dépend uniquement de `PUL-DL-08`.
   - Les unités `PUL-DL-09` à `PUL-DL-13` sont explicitement marquées `non_blocking_follow_up=true`.
   - La clôture core s’appuie uniquement sur les preuves du noyau ; les extensions peuvent devenir de nouvelles ADR/issues après clôture.

2. **Frontière SLO DirectShow corrigée**
   - r1 : la définition restait ambiguë entre DirectShow et l’entrée encodeur.
   - r2 : AC-08 est précisément :
     `TakeAccepted → première frame Program valide observée sur le retour DirectShow`.
   - AC-07 reste distinct :
     `TakeAccepted → première frame Program valide à l’entrée encodeur/raw`.
   - Les métriques, le banc Probe et les critères PUL-DL-05/PUL-DL-12 utilisent désormais ces deux frontières séparées.

3. **Mappings DirectShow historiques corrigés**
   - r1 : l’isolation runtime pouvait être lue comme une promesse de quatre instances utilisant les mêmes alias.
   - r2 : les mappings internes sont namespacés par `runtime_instance_id`.
   - Les alias legacy `Program`/`Preview` sont une namespace singleton protégée par lease explicite, ou doivent être remplacés par des filtres/noms dédiés par instance.
   - AC-10 teste quatre instances internes sans collision puis vérifie le refus correct du second détenteur ou l’usage de noms dédiés.

4. **Cycle de vie post-Take clarifié**
   - r1 : l’indépendance post-Take était formulée sans préciser le moment où l’ancienne OnAir devient Preview.
   - r2 : avant `TakeCommitted`, les roots concernés sont gelés et aucune mutation de la future Preview n’est appliquée.
   - À `TakeCommitted`, l’ancienne Preview devient OnAir et l’ancienne OnAir devient la nouvelle Preview disponible.
   - Cette nouvelle Preview est la seule lane pouvant être re-préparée après le commit ; sa mutation ne touche jamais Program.
   - AC-04 et PUL-DL-03/PUL-DL-06 couvrent ce cycle.

5. **Éléments conservés**
   - ADR-ID, emplacement canonique cible, décision dual-lane, surfaces stables, absence de rebind `video_t`, contrat `pulsar.scene-switch.v1`, audio Programme commun, instrumentation, alternatives, risques, migration, rollback, rôles et découpage core restent inchangés sauf les précisions directement liées aux quatre corrections ci-dessus.

`AGENT_REPORT` — Atlas-3 — `READY`.
