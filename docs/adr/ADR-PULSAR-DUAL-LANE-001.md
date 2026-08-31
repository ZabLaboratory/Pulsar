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
- `Amendment 1` — `amendment-1-draft-r1-ac12-boundaries-20260831` : correction sémantique approuvée des frontières RTMP AC-12a/AC-12b ; corps canonique SHA-256 `eace0e37a0588d11983de31b4a49b13bb58b52af7809c6e344a7f316032cbba9` annexé ci-dessous sans réécriture de r2.

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

# ADR-PULSAR-DUAL-LANE-001 — Amendment 1 : frontières RTMP AC-12

## 0. Identité et statut

- `adr_id`: `ADR-PULSAR-DUAL-LANE-001`
- `parent_revision_id`: `draft-r2-dual-lane-20260828`
- `parent_merge_sha`: `1252c6e6079f34d6331a8c45f8303e8524552b0c`
- `parent_content_sha256`: `68a8e87aa365bd17ca14a76a642d181f5e236ef6e20bcba3975b348c13218ae4`
- `amendment_id`: `ADR-PULSAR-DUAL-LANE-001-Amendment-1`
- `revision_id`: `amendment-1-draft-r1-ac12-boundaries-20260831`
- `date`: `2026-08-31`
- `author_agent`: `Atlas`
- `author_thread`: `atlas_249_ac12_semantics`
- `source_work_unit`: `ZabLaboratory/Pulsar#249`
- `status_proposed`: `PENDING_VIGIL_REVIEW`
- `decision_type`: `semantic correction of a normative SLO boundary`
- `content_fingerprint`: `ADR-PULSAR-DUAL-LANE-001|Amendment-1|revision=amendment-1-draft-r1-ac12-boundaries-20260831|I14=raw+directshow+rtmp-transport+rtmp-command-to-egress+decoded+antenna-separate|AC12a=exact-correlated-packet:callback-to-receiver:conservative-p95<=15ms:100-warm+100-measured-per-codec|AC12b=TakeAccepted-to-same-correlated-receiver-packet:mandatory-stage-disclosure:no-r2-limit|historical-1-15ms=reference-only-uncorrelated-liveness|AC07+AC08=unchanged|decoded+antenna=no-guarantee|libobs-interleaver=not-core-required|issue249=blocked-until-amendment-merged`

Cet amendement n'est ni approuvé ni applicable tant que Vigil n'a pas rendu `ADR_VERDICT: APPROVED` sur cette révision exacte, qu'une approbation humaine explicite n'a pas cité cette même révision et son SHA-256 de contenu, puis que Vigil n'a pas persisté et mergé fidèlement le texte avec le tag signé prévu par le protocole ADR.

## 1. Contexte et problème

La révision parente r2 impose :

1. I14, qui sépare les mesures raw, retour DirectShow, premier paquet RTMP et première frame décodée/antenne ;
2. AC-12, qui demande de mesurer le premier paquet RTMP séparément et de le comparer à une baseline p95 `<=15 ms` ;
3. AC-07 et AC-08, qui définissent explicitement leurs propres frontières `TakeAccepted ->` raw et DirectShow ;
4. l'absence de garantie r2 sur la première frame décodée ou antenne.

R2 ne définit toutefois ni le timestamp de départ d'AC-12, ni l'identité du paquet à retenir, ni si ce paquet doit porter la frame Program committée. La baseline historique p95 `<=15 ms` provenait d'une observation exploratoire `Take -> premier paquet vidéo vu par le démuxeur FFmpeg`, sans corrélation exacte par runtime, Take, frame, index, PTS ou DTS. Cette observation qualifiait au mieux la continuité d'un flux déjà actif ; elle ne prouvait pas l'arrivée du contenu Program committé.

Le runbook et le parseur construits après l'approbation r2 ont choisi une sémantique plus forte : `TakeAccepted -> observation par le récepteur RTMP du premier paquet exactement corrélé à la frame committée`, avec le même seuil de 15 ms. La campagne x264 exacte au head `26803eccdfc921e62cc41a8e86cb88e591334191` montre que cette combinaison frontière/seuil est physiquement incohérente à 1080p60 :

- `TakeAccepted -> CTS` p95 `15.863394 ms` ;
- `CTS -> FER` p95 environ `18.338 ms` ;
- `FER -> FERC` p95 environ `7.881 ms` ;
- `FERC -> PIR` p95 environ `115.328 ms` ;
- `PIR -> callback` p95 environ `0.00383 ms` ;
- `callback -> récepteur RTMP` pour le même paquet p95 `1.2515 ms` ;
- `TakeAccepted -> récepteur RTMP` pour le même paquet p95 `151.798385 ms`, ou `151.823785 ms` de manière conservative avec la borne d'horloge.

Même un contournement complet de l'attente d'interleave `FERC -> PIR` ne permettrait pas de tenir 15 ms, puisque les étages amont dépassent déjà ce budget. La correction ne peut donc pas consister à affaiblir la corrélation, à masquer le délai complet, ni à supprimer sans décision la barrière d'ordre audio/vidéo de libobs.

## 2. Objectifs et non-objectifs

### Objectifs

- Donner au seuil historique de 15 ms une frontière RTMP explicite, mesurable et techniquement cohérente.
- Conserver une preuve exacte du même paquet entre le producteur et le récepteur.
- Rendre obligatoire la publication de la latence complète commande-vers-egress et de ses sous-étages afin qu'un transport rapide ne masque jamais un pipeline lent.
- Maintenir AC-07, AC-08, l'audio Programme commun, les surfaces stables et l'absence de garantie decoded/antenna.
- Permettre à #249 de juger le noyau avec des critères réalisables sans fabriquer de PASS.

### Non-objectifs

- Fixer dans cet amendement un nouveau SLO `TakeAccepted -> paquet Program committé au récepteur`.
- Garantir la latence décodée, player ou antenne.
- Autoriser un bypass de l'ordre A/V, un rebind du `video_t` actif ou une modification du contrat audio I12.
- Imposer un patch libobs, win-wasapi, obs-x264 ou NVENC pour clore le noyau.
- Requalifier la baseline historique non corrélée en preuve d'acceptation.
- Modifier la topologie dual-lane, le Cut atomique, le namespace runtime, les leases ou les extensions non bloquantes.

## 3. Options considérées

### Option A — Conserver `TakeAccepted -> paquet committé <=15 ms`

Rejetée. Les mesures exactes prouvent que le budget est dépassé avant même l'interleaver. Un patch libobs ne peut pas corriger les étages amont et risquerait l'ordre A/V pour poursuivre un critère incohérent.

### Option B — Revenir à `TakeAccepted -> n'importe quel prochain paquet RTMP`

Rejetée comme critère recommandé. Cette lecture est la plus proche de la provenance historique, mais devient presque un test de cadence sur un flux 60 fps continu. Elle peut passer avec un paquet antérieur au contenu committé et n'apporte pas la preuve de transport du paquet sélectionné.

### Option C — Séparer garde transport et latence commande-vers-egress

Retenue. AC-12a applique le seuil de 15 ms au transport du même paquet, depuis l'entrée dans le callback de sortie encodée Pulsar jusqu'au récepteur/démux RTMP. AC-12b conserve obligatoirement `TakeAccepted -> récepteur du même paquet` et toute sa décomposition, sans seuil r2. Cette option ne cache ni ne supprime la latence de 151.8 ms observée.

## 4. Décision

Adopter deux frontières RTMP nommées et non substituables :

```text
TakeAccepted
  -> CTS -> FER -> FERC -> PIR -> Pulsar encoded-output callback
                                      |                         |
                                      |<-- AC-12a transport --->| RTMP receiver/demux
  |                                                             |
  |<---------------- AC-12b command-to-egress ----------------->|
```

AC-12a est le garde-fou RTMP normatif portant le p95 conservateur `<=15 ms`. AC-12b est une divulgation normative obligatoire, sans limite r2. Les deux utilisent le même paquet vidéo corrélé. AC-12a ne peut pas satisfaire AC-07 ou AC-08 ; AC-12b ne devient pas une garantie decoded/antenna.

## 5. Clauses exactes modifiées

### 5.1 I14 — remplacement intégral

Remplacer I14 par :

> **I14 — Mesures séparées et non substituables** : la première frame Program valide à l'entrée encodeur/raw, la première frame Program valide observée sur le retour DirectShow, le transport du premier paquet vidéo RTMP corrélé, la latence complète `TakeAccepted -> récepteur RTMP` de ce même paquet, la première frame décodée et la première frame antenne/player sont enregistrées et rapportées sous des frontières distinctes. Aucune de ces mesures ne peut être renommée, fusionnée ou substituée à une autre pour satisfaire un critère.

### 5.2 Étape 0 — remplacement des lignes RTMP de baseline

Dans `Migration par étapes / Étape 0 — Baseline et instrumentation minimale`, remplacer `premier paquet RTMP séparément` et la baseline RTMP associée par :

> - pour le premier paquet vidéo corrélé à la frame Program committée, `entrée dans le callback de sortie encodée Pulsar -> observation du même paquet par le récepteur/démux RTMP`, séparément, avec p95 conservateur cible `<=15 ms` ;
> - pour ce même paquet, `TakeAccepted -> observation par le récepteur/démux RTMP`, avec p50/p95/p99/max et décomposition des étages, obligatoire mais sans SLO r2 ;
> - première frame décodée et antenne/player séparément, sans SLO r2.
>
> La baseline historique de 1 à 15 ms `Take -> premier paquet démux observé` ne possédait pas d'identité de paquet ou de frame committée. Elle est conservée comme observation de continuité `reference_only` et ne constitue pas une preuve d'acceptation du contenu Program commuté.

Les baselines raw et DirectShow restent inchangées. Les observations decoded x264/NVENC restent diagnostiques et sans SLO r2.

### 5.3 Pre-mortem — remplacement du risque RTMP/décodage

Remplacer la ligne `La première frame décodée est prise à tort pour un échec du Cut` par les deux risques suivants :

| Échec supposé | Signal précoce | Mitigation |
|---|---|---|
| Un transport RTMP rapide masque un pipeline commande-vers-egress lent | AC-12a passe sous 15 ms mais AC-12b ou `FERC -> PIR` augmente fortement | Rendre AC-12b et tous ses sous-étages obligatoires dans chaque rapport ; interdire qu'AC-12a soit présenté comme latence totale de switch ou première frame décodée |
| Un paquet en vol non lié au nouveau Program crée un PASS vacu | Le paquet récepteur n'a pas le même index/PTS/DTS que le paquet producteur sélectionné ou précède la frontière `TakeCommitted` | Corrélation exacte et fail-closed par runtime, Take, révisions, frame/PTS, index monotone et intervalle rationnel d'offset FLV ; aucun fallback vers le prochain paquet arbitraire |

Le risque déjà reconnu de confondre frame décodée et paquet RTMP reste couvert par I14 : decoded/player/antenna demeure une mesure séparée sans SLO r2.

### 5.4 Observabilité — ajouts obligatoires

Ajouter aux métriques :

- `rtmp_transport_same_packet_ms = receiver_observed_normalized_ns - packet_callback_monotonic_ns` ;
- `rtmp_command_to_egress_same_packet_ms = receiver_observed_normalized_ns - TakeAccepted.observed_at_monotonic_ns` ;
- `take_to_cts_ms`, `cts_to_fer_ms`, `fer_to_ferc_ms`, `ferc_to_pir_ms`, `pir_to_callback_ms`, `callback_to_receiver_ms` ;
- compte de paquets corrélés, duplicats, trous d'index, ambiguïtés, dérives d'offset et bornes d'horloge ;
- identité exacte du runtime, de la session, du codec, du Take, des révisions, de la frame/PTS et du paquet.

Ajouter aux alertes :

- p95 conservateur AC-12a > 15 ms ;
- paquet récepteur absent, dupliqué, ambigu ou non corrélable ;
- croissance de AC-12b ou d'un sous-étage par rapport à la baseline exacte du codec, sans transformer cette alerte en SLO r2 implicite ;
- rapport AC-12a sans rapport AC-12b correspondant.

### 5.5 AC-12 — remplacement intégral

Remplacer AC-12 par :

> **AC-12a — Garde transport RTMP corrélée** : pour x264 et NVENC dans des campagnes indépendantes, après au moins 100 Takes warm-up observés, au moins 100 Takes mesurés sélectionnent chacun le premier paquet vidéo producteur dont la frame et le PTS sont égaux ou postérieurs à la frontière `TakeCommitted` correspondante. Le paquet producteur et le paquet observé par le récepteur/démux RTMP dédié doivent être prouvés identiques par `runtime_instance_id`, session, codec, `command_id`, `intent_id`, révisions post-commit, frame/PTS, index vidéo monotone, PTS/DTS dans leurs timebases rationnelles et un unique intervalle calibré d'offset mux FLV stable pour le flux. Mesurer `packet_callback_monotonic_ns -> receiver_observed_normalized_ns` pour ce même paquet. Le rapport publie count, p50/p95/p99/max bruts, borne d'horloge et p95 conservateur ; le p95 conservateur doit être `<=15 ms` pour chaque codec, sans pooling. Toute corrélation manquante, dupliquée, ambiguë, hors ordre ou hors borne échoue fermée. Cette preuve est un garde de transport récepteur/démux, pas une mesure wire-level, décodée ou antenne.
>
> **AC-12b — Latence commande-vers-egress corrélée obligatoire** : pour les mêmes campagnes, Takes et paquets qu'AC-12a, mesurer et publier séparément `TakeAccepted.observed_at_monotonic_ns -> receiver_observed_normalized_ns`, avec count, p50/p95/p99/max et les distributions `TakeAccepted -> CTS`, `CTS -> FER`, `FER -> FERC`, `FERC -> PIR`, `PIR -> callback` et `callback -> receiver`. AC-12b est obligatoire pour que la preuve AC-12 soit complète mais ne porte aucun seuil de réussite r2. AC-12a ne peut ni cacher, ni remplacer, ni renommer AC-12b, AC-07, AC-08, la première frame décodée ou la première frame antenne/player. Tout futur seuil pass/fail sur AC-12b requiert une décision SLO approuvée séparément.

### 5.6 PUL-DL-05 et PUL-DL-08 — conséquences de mapping

Le périmètre et les sorties de PUL-DL-05 doivent nommer AC-12a et AC-12b séparément. Les preuves attendues deviennent : au moins 100 warm-up observés puis 100 Takes mesurés par codec, même-paquet corrélé, p95 conservateur AC-12a, rapport AC-12b complet et sous-étages, sans pooling x264/NVENC.

PUL-DL-08/#249 ne peut marquer AC-12 couvert que si AC-12a passe indépendamment pour x264 et NVENC et si AC-12b est publié pour les deux codecs. Un AC-12a rapide sans AC-12b est `UNPROVEN`, pas `PASS`.

## 6. Clauses explicitement inchangées

- I1 à I13, notamment I5/I6 sur les sorties stables et l'absence de rebind actif, et I12 sur l'audio Programme commun.
- AC-01 à AC-11, en particulier AC-07 raw p95 `<=50 ms` et AC-08 retour DirectShow p95 `<=75 ms`.
- AC-13 et AC-14.
- La décision de deux lanes physiques chaudes, les rôles permutables, les surfaces stables, le Cut atomique et le cycle Preview post-Take.
- L'absence de garantie r2 decoded/player/antenna.
- Le caractère non bloquant de Fade/Stinger/T-bar, Preview audio/AFV, tuning decoder et soak complet.
- Les règles de namespace runtime et de lease des alias DirectShow.

## 7. Absence d'affaiblissement

Cet amendement ne transforme pas un échec en PASS en supprimant des données :

1. le seuil `<=15 ms` est conservé sur une frontière RTMP explicitement nommée ;
2. l'identité exacte du même paquet est plus forte que la baseline historique non corrélée ;
3. la borne d'horloge reste incluse dans le p95 conservateur ;
4. x264 et NVENC restent indépendants, avec 100 warm-up et 100 mesurés chacun ;
5. le chemin complet qui échouait à 151.8 ms devient une divulgation obligatoire et ne peut pas disparaître du rapport ;
6. AC-07 et AC-08 restent les SLO du Cut aux frontières raw et DirectShow ;
7. aucune garantie decoded/antenna n'est inventée ;
8. aucun seuil AC-12b n'est inventé sans baseline x264/NVENC et choix produit ;
9. aucun paquet arbitraire en vol ne peut satisfaire AC-12a.

La modification est néanmoins sémantique, parce qu'elle assigne formellement le seuil de 15 ms à `callback -> receiver`. C'est pourquoi elle exige le cycle complet d'amendement au lieu d'une simple correction silencieuse du runbook.

## 8. Conséquences positives et négatives

### Positives

- Le seuil AC-12 devient cohérent avec un segment de transport mesurable.
- L'evidence reste exacte et corrélée de bout en bout.
- Le délai d'interleaver, d'encodage ou de frame phase ne peut plus être caché.
- Les responsabilités sont nettes : AC-07/08 qualifient le Cut, AC-12a le transport RTMP, AC-12b la performance totale observée, decoded/antenna restent diagnostiques.
- Un patch upstream peut être décidé sur une mesure causale, pas pour satisfaire artificiellement un mauvais budget.

### Négatives

- La clôture de #249 est suspendue pendant la validation et la persistance de l'amendement.
- Le runbook, le parseur, les fixtures et rapports doivent être alignés avant une nouvelle preuve d'acceptation.
- Un AC-12a passant ne signifie pas que la latence command-to-egress de 151.8 ms est acceptable pour le produit ; cette question reste ouverte et visible.
- Une nouvelle exigence produit sur AC-12b pourra nécessiter une autre ADR, du tuning Pulsar ou une modification upstream avec validation A/V.

## 9. Pre-mortem de l'amendement

| Échec supposé | Détection | Prévention/mitigation |
|---|---|---|
| Le rapport n'affiche que le 1.25 ms transport | AC-12b ou un sous-étage absent | Schéma et parseur fail-closed ; AC-12 complet = AC-12a PASS + AC-12b présent |
| Le paquet sélectionné n'est pas celui du Program committé | index/PTS/DTS/frame/révisions incohérents | Corrélation exacte et intervalle FLV unique ; ambiguïté = FAIL |
| L'incertitude d'horloge est ignorée | p95 brut seul publié | Borne et p95 conservateur obligatoires |
| Des codecs sont poolés | un rapport agrégé masque un échec codec | Deux sessions et verdicts indépendants requis |
| Un patch libobs dégrade l'A/V sans besoin core | continuité/PTS/drift audio régressent alors qu'AC-12a passait déjà | Ne pas imposer de patch upstream pour cet amendement ; traiter tout nouveau SLO complet séparément |
| La baseline historique est présentée comme preuve de contenu commuté | absence de même-paquet corrélé | Marquage `reference_only`, jamais acceptance |

## 10. Migration

1. Vigil revoit ce payload exact et rend un verdict citant `amendment_id`, `revision_id`, `content_fingerprint` et SHA-256 du contenu.
2. Après `APPROVED`, un humain autorisé approuve explicitement la même révision et le même SHA-256.
3. Vigil persiste fidèlement l'amendement sans changement sémantique, ouvre/met à jour la PR ADR, publie son attestation, merge et crée le tag signé `adr/ADR-PULSAR-DUAL-LANE-001/amendment-1`.
4. Après `ADR_MERGED`, Atlas met à jour le mapping de #249 vers Amendment 1 ; aucune nouvelle issue dérivée n'est requise pour le seul alignement du banc existant.
5. Conduit/Probe alignent contrat de trace, parseur, fixtures, rapport et runbooks : AC-12a calcule callback-vers-récepteur même-paquet avec borne ; AC-12b calcule et publie le chemin complet et ses étages.
6. Les traces anciennes restent immuables. Elles peuvent être re-analysées comme diagnostics si tous les champs existent, mais ne deviennent pas automatiquement une preuve du head final.
7. Keeper exécute sur l'artefact exact final, sans pooling : x264 puis NVENC, chacun 100 warm-up observés + 100 mesurés, WGC+CEF, consommateur DirectShow réel, stream/record actifs, récepteur RTMP dédié, corrélation exacte et cleanup gracieux.
8. Probe vérifie indépendamment les deux rapports, les absences de substitution et toutes les autres preuves #249 ; Bastion revalide uniquement si le diff final touche une surface de sécurité ou d'intégrité.
9. #249 ne peut être mergée/fermée qu'après AC-12a PASS x264+NVENC, AC-12b complet x264+NVENC, les autres AC/I applicables et les rapports requis.

## 11. Rollback

Si Vigil ou l'humain rejette l'amendement :

- ne modifier ni l'ADR r2 ni les critères de #249 ;
- conserver le candidat `26803ecc...` et ses 151.8 ms comme échec selon l'interprétation actuelle du runbook ;
- ne pas remplacer le gate par le prochain paquet arbitraire et ne pas affaiblir la corrélation ;
- maintenir #249 ouverte jusqu'à une nouvelle décision.

Si l'amendement est mergé puis doit être retiré avant clôture core :

- produire un amendement de retrait explicite ; ne pas réécrire silencieusement l'historique ;
- revenir aux runbooks/parseurs de la révision précédente par commits signés et preuves de non-régression ;
- invalider tout PASS AC-12 dépendant de la frontière retirée ;
- aucun rollback ne touche les lanes, surfaces, encodeurs actifs, mappings ou leases en production.

Un changement expérimental WASAPI/libobs/encodeur reste réversible indépendamment de cet amendement. Il ne fait pas partie du rollback architectural AC-12.

## 12. Preuves requises

### Preuves de décision déjà observées

- ADR parent exact, SHA et digest approuvé ci-dessus.
- Trace x264 exacte `26803ecc...`: SHA-256 `d499a304cf083bd4293112dd7497f0f29d9ab609c52d006ac2987a39a896909b`.
- Rapport analyseur x264 : SHA-256 `b72b7e4c549dcdbc8050410441d31b924be17e5c075cf89fc97c623108f95a85`.
- 200/200 paquets producteur/récepteur corrélés, 100 warm-up + 100 mesurés, borne horloge `0.0254 ms`.
- AC-07 raw p95 `33.292715 ms` PASS ; AC-08 DirectShow p95 `25.995510 ms` PASS ; AC-11 200 acceptés/committés PASS.
- AC-12 actuel full path p95 `151.798385 ms`, conservateur `151.823785 ms`, FAIL contre 15 ms.
- Même-paquet callback-vers-récepteur p95 `1.2515 ms`, observation suffisante pour justifier la frontière proposée mais pas pour accepter le futur head final.

### Preuves d'acceptation après amendement

- exact SHA du code et de l'artefact, hash du binaire, identité runtime/session/host/GPU/codec ;
- 100 warm-up observés + 100 mesurés par codec, sans pooling ;
- corrélation même-paquet fail-closed et calibration FLV stable ;
- AC-12a count/p50/p95/p99/max, borne d'horloge et p95 conservateur `<=15 ms` pour x264 puis NVENC ;
- AC-12b count/p50/p95/p99/max et six sous-étages pour x264 puis NVENC ;
- AC-07/08/11 toujours PASS ;
- audio Programme/AAC continu, PTS/DTS monotones, absence de régression A/V si un changement audio ou interleaver intervient ;
- aucune frame décodée/antenne présentée comme garantie ;
- shutdown gracieux, leases libérés, readers/writers joints, aucun processus ou registre temporaire résiduel ;
- CI exacte verte et AGENT_REPORT des rôles requis.

## 13. Impact sur #249 et la clôture du noyau

Jusqu'au merge de l'amendement :

- #249 reste `OPEN / BLOCKED_BY_ADR_SEMANTICS` pour AC-12 ;
- aucun nouveau hardware AC-12 n'est accepté comme preuve de clôture ;
- les travaux de performance peuvent continuer seulement comme expériences explicitement non qualifiées, sans claim AC-12.

Après merge de l'amendement :

- #249 conserve son identité `PUL-DL-08`; elle n'est pas remplacée ;
- sa checklist AC-12 devient `AC-12a PASS x264 + NVENC` et `AC-12b complet x264 + NVENC` ;
- le rapport de clôture doit citer le SHA mergé et le tag signé de l'amendement ;
- les 151.8 ms x264 observés restent dans le ledger comme performance command-to-egress, même si AC-12a passe ;
- la clôture core n'implique toujours ni Fade/Stinger/T-bar, ni Preview audio/AFV, ni decoded/antenna, ni soak complet ;
- si la latence AC-12b devient ultérieurement un objectif produit, elle reçoit un seuil et un work unit distincts après décision approuvée.

## 14. Décision sur les modifications upstream

Pulsar est autorisé à modifier upstream lorsque nécessaire, mais Amendment 1 ne rend aucun patch upstream obligatoire pour le noyau. La campagne montre que :

- obs-x264 n'est pas le segment dominant ;
- le callback et le récepteur RTMP sont rapides ;
- l'attente `FERC -> PIR` est réelle mais sa suppression ne permettrait toujours pas `TakeAccepted -> paquet committé <=15 ms` ;
- affaiblir la barrière d'ordre A/V sans SLO AC-12b approuvé serait un risque non justifié.

Un patch Pulsar/libobs pourra être proposé pour améliorer AC-12b uniquement avec : objectif chiffré distinct, source-to-sink, invariants audio/PTS, A/B x264+NVENC, drift/soak, rollback et validation indépendante. Son absence ne bloque pas AC-12a si le transport même-paquet tient le seuil.

## 15. Gates de validation

Le verdict Vigil attendu doit citer exactement :

```text
ADR_VERDICT: APPROVED | CHANGES_REQUIRED | REJECTED
ADR: ADR-PULSAR-DUAL-LANE-001
AMENDMENT: ADR-PULSAR-DUAL-LANE-001-Amendment-1
REVISION: amendment-1-draft-r1-ac12-boundaries-20260831
CONTENT_FINGERPRINT: ADR-PULSAR-DUAL-LANE-001|Amendment-1|revision=amendment-1-draft-r1-ac12-boundaries-20260831|I14=raw+directshow+rtmp-transport+rtmp-command-to-egress+decoded+antenna-separate|AC12a=exact-correlated-packet:callback-to-receiver:conservative-p95<=15ms:100-warm+100-measured-per-codec|AC12b=TakeAccepted-to-same-correlated-receiver-packet:mandatory-stage-disclosure:no-r2-limit|historical-1-15ms=reference-only-uncorrelated-liveness|AC07+AC08=unchanged|decoded+antenna=no-guarantee|libobs-interleaver=not-core-required|issue249=blocked-until-amendment-merged
CONTENT_SHA256: <sha256 du corps canonique exact>
```

Après un verdict `APPROVED`, l'approbation humaine doit citer exactement :

```text
HUMAN_APPROVAL: APPROVED
ADR: ADR-PULSAR-DUAL-LANE-001
AMENDMENT: ADR-PULSAR-DUAL-LANE-001-Amendment-1
REVISION: amendment-1-draft-r1-ac12-boundaries-20260831
CONTENT_SHA256: <sha256 du corps canonique exact>
```

Toute modification sémantique de I14, AC-12a, AC-12b, du seuil, de la corrélation, du nombre de Takes, du statut de la baseline historique, de l'absence de SLO AC-12b ou de l'impact #249 crée une nouvelle révision et invalide les validations de celle-ci.

## 16. Verdict Atlas

`AMENDMENT_RECOMMENDATION: GO`

`ISSUE_249_CORE_CLOSURE: NO_GO_UNTIL_AMENDMENT_MERGED_AND_EVIDENCED`

Raison : l'amendement conserve le seuil RTMP, renforce l'identité du paquet, maintient toutes les frontières du Cut et rend obligatoire la latence totale observée. Il corrige une ambiguïté devenue un critère physiquement incohérent sans fabriquer de performance, sans cacher l'échec de 151.8 ms et sans imposer un patch upstream risqué.
