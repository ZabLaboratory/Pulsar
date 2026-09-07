# Pulsar #253 — audit du contenu décodé et seconde étude

> Historical measurement correction. The subsequent
> [native readback study](issue-253-native-optimized.md) contains the promoted
> CPU result. This document retains its own workload and evidence boundary.

Cette continuation remplace les conclusions sur la première image visible de
`issue-253-latency-study.md`, sans réécrire les mesures historiques. Elle ne
mesure toujours pas un affichage à l'écran ni une liaison RTMP externe.

## Corrections de mesure

1. Le candidat décodé est l'image correspondant au paquet sélectionné par la
   télémétrie. Avec les B-frames, ce n'est pas nécessairement la première image
   portant le nouveau Program. Les bornes `decoded_candidate_frame` et
   `decoded_marker_first_frame` sont maintenant distinctes. La seconde exige
   le repère A/B attendu et l'ancien repère immédiatement précédent ; l'audit
   conserve toutes les images décodées et leurs corrélations de paquet.
2. Les sources de couleur des tests de cycle de vie recouvraient les producteurs
   WGC/CEF dans le Program enregistré. Le nouveau layout expose WGC à gauche et
   CEF à droite, au-dessus des couleurs conservées. Les anciennes séries ne sont
   donc pas une référence de performance comparable à cette charge visible.
3. La cadence attribuée à une image et l'instant de son contenu peuvent différer.
   Le chemin GPU Windows utilise une surface de copie partagée : son contenu peut
   être courant alors que son PTS de cadence vient du tick précédent. Les nouveaux
   champs de contenu propagent cette distinction sans changer les PTS média.
4. Le retour DirectShow ne doit pas associer une image antérieure au Take courant.
   Il rejette désormais tout contenu antérieur au commit. Le PTS et le frame_id
   de commande restent inchangés dans l'identité de corrélation ; ce ne sont pas
   des timestamps de contenu à remplacer. La même barrière protège la borne raw.

## Prototypes bornés

- `PULSAR_DSHOW_FRESH_FRAME_POLL=1` attend une publication nouvelle jusqu'à
  l'échéance de cadence existante. Le hint est en lecture seule ; la lecture
  réelle conserve seqlock, copie, arrêt et contrôles de métadonnées.
- `PULSAR_RAW_CURRENT_READBACK=1` lit la surface courante au lieu de la précédente.
  Cela peut éviter une période CPU, mais augmente potentiellement l'attente GPU
  du map. Cadence, répétitions, PTS, audio et paramètres d'encodage sont conservés.

Les deux prototypes sont désactivés par défaut. Les champs ajoutés aux structures
du fork libobs imposent la reconstruction de tous les modules concernés ; ne pas
mélanger ces DLL avec des plugins binaires construits sur l'ancien ABI.

Attention : préserver les timestamps et la continuité audio ne prouve pas la
synchronisation audiovisuelle du contenu. La lecture courante avance les pixels
d'une période par rapport à la cadence CPU historique. Un test flash/clic partagé
avec mesure de phase A/V, absent de cette campagne, reste nécessaire avant toute
promotion. Les tests audio existants vérifient routage, identité et continuité,
pas ce décalage relatif. Aucun gain de ce prototype n'est présenté comme qualifié
sans perte sur tous les critères.

## Preuves et limites

### Série corrigée du 7 septembre 2026

Même candidat binaire `f2c9b05` pour les six configurations/passes ci-dessous ;
100 Takes de chauffe puis 100 Takes mesurés par codec et par passe, soit
1 200 Takes mesurés. Ordre : référence, combiné, combiné, référence, polling,
polling. Les quatre binaires principaux hachés sont identiques entre les passes.
Les chiffres sont l'intervalle entre les **deux p95 observés**, pas un percentile
calculé en mélangeant les séries et pas un intervalle de confiance.

| Codec / mode | Raw p95 ms | DirectShow p95 ms | Premier repère décodé p95 ms |
|---|---:|---:|---:|
| x264 référence corrigée | 33,76–35,16 | 46,20–51,17 | 61,56–63,47 |
| x264 lecture courante + polling | 22,26–23,00 | 27,02–27,24 | 34,89–35,01 |
| x264 polling seul | 34,57–34,95 | 38,25–54,86 | 59,68–74,58 |
| NVENC référence corrigée | 21,19–21,78 | 26,36–38,66 | 74,95–75,07 |
| NVENC lecture courante + polling | 24,12–25,11 | 28,04–28,69 | 75,57–77,02 |
| NVENC polling seul | 21,27–22,65 | 25,38–26,49 | 75,12–75,69 |

Décision : le gain du mode combiné x264 se reproduit dans cet A/B/B/A, mais sa
phase audiovisuelle n'est pas qualifiée. NVENC n'en bénéficie pas ; sa borne raw
régresse. Le polling seul ne donne pas un gain x264 reproductible sur les deux
passes. Aucun des deux prototypes n'est promu par défaut.

Pour NVENC, la référence donne un candidat décodé sélectionné à 144,66–146,29 ms
p95 contre 74,95–75,07 ms pour le premier repère réellement changé. C'est la
correction d'un indicateur, pas une accélération de l'encodeur. Pour x264 en
lecture courante, le candidat sélectionné reste à 62,15–63,41 ms alors que le
premier repère arrive à 34,89–35,01 ms : là encore les bornes doivent être séparées.

Callback→récepteur RTMP reste à 14,35–30,28 ms p95 selon codec/passe, et dépasse
15 ms dans 11 des 12 séries. Les précédentes paires packet-only/decoded montrent
que le décodage additionnel n'est pas l'unique explication ; elles précèdent la
dernière correction de contenu et ne sont pas utilisées pour chiffrer son gain.
La capacité comparative, les pertes sous surcharge, la qualité à débit constant
et la latence d'affichage ne sont pas attestées par ces mesures de Takes.

Les séries sont séparées par codec et configuration ; aucune moyenne de p95 et
aucune attribution d'un gain média à une simple correction de timestamp.
Les résultats HMAC, manifests, vidéos et réglages de chaque série font foi.
Les sondes de latence terminées ne signifient pas que tous les SLO passent :
callback→récepteur RTMP et capacité comparative gardent leurs verdicts propres.

Le cache natif est testé avec trois répétitions d'un même contenu : les trois
timestamps de cadence progressent, le timestamp de contenu reste constant. L'API
historique conserve contenu=timestamp. Le test initialise et ferme libobs.
Les contrôles Python conservent les refus d'identité, de repère ambigu, de trou,
de chronologie incohérente et de corrélation de paquet divergente.

### Validation finale

- Reconstruction libobs/headless avec CEF puis reconstruction complète des
  plugins et tests natifs : réussies. Les hashes des quatre binaires principaux
  restent identiques à ceux des douze séries finales.
- Python : 193 tests réussis, 1 ignoré. Une précédente exécution a échoué avec
  `WinError 10055` lors de la création répétée de boucles asyncio ; le test
  conserve ses 200 corrélations dans une unique boucle, comme le runtime réel.
- CTest complet, polling=1 et lecture courante=0 : 18/19 réussis. Le seul échec
  est `pulsar-dir-hardening-probe` : les gestes différents propriétaires et
  substitution post-rename exigent `SeRestorePrivilege`, absent de ce token
  Windows. Les contrôles ne sont ni supprimés ni transformés en succès.
- Audio runtime dans ce même mode : 100 Cuts x264 et 100 Cuts NVENC réussis,
  route/outputs stables, AAC 48 kHz, PTS continus et isolation de Preview. Cela
  ne qualifie pas la phase A/V du prototype lecture courante.
- Fade/Stinger x264 : quatre cas queued/final_queued réussis, pixels avant/après,
  événements terminaux uniques, erreur de corrélation PTS de 8 ns. Pas de
  couverture NVENC des transitions inférée de ce script.

Preuves : `evidence/253/eleven/20260907T130000Z-corrected-study.json`, logs de
tests du même préfixe, `20260907T131000Z-audio.zip`, log transitions et archive
`20260907T130400Z-decoded-run-evidence.zip` (SHA256
`6FD4DCF544DB8A26AA62D93157F80E2A27BBDD788E2F2A46D24610A9B457D9CB`).
Les 273 entrées de cette archive conservent traces, authentification, rapports,
hashes et diagnostics, sans les vidéos. Les 16 dossiers originaux, y compris les
essais rejetés, et les vidéos sont relocalisés dans
`D:/Documents/Zab/Artifacts/2026-09-07/pulsar-253-decoded-audit/` ; les anciens
chemins absolus présents dans les journaux désignent ces mêmes fichiers avant
relocalisation. Aucun secret HMAC n'est livré dans cette archive.

Rollback local : laisser les deux variables à 0 et ne pas intégrer cette branche.
Un éventuel retrait des patches 0048–0050 exige une reconstruction cohérente de
libobs et des modules. Aucun merge, déploiement ou changement distant n'est
autorisé par ce document.
