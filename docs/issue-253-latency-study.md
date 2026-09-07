# #253 — étude libobs, DirectShow et retour décodé

## Verdict du 7 septembre 2026

Étude directe, sans agents. La réduction de travail libobs est implémentée et testée,
mais **aucune baisse reproductible de latence média n'est démontrée**. Les six séries
respectent raw p95 ≤ 50 ms et DirectShow p95 ≤ 75 ms. La promotion globale reste non
acquise : le seuil callback→récepteur RTMP de 15 ms échoue dans plusieurs séries,
y compris une répétition de référence. Ces résultats ne permettent pas de déclarer
une absence générale de régression ni de fermer #253 comme entièrement validée.

## Protocole et résultats

Windows, RTX 3070, Preview et Program simultanés en 1080p60 ; capture WGC animée
et source CEF réellement actives. Retour DirectShow CPU, émission RTMP locale,
enregistrement H.264/AAC. Pour chacun des deux encodeurs et chacune des six séries :
100 Cuts de chauffe puis 100 Cuts mesurés. Pas de référence single-lane, pas de
diffusion externe, pas d'interface OBS lancée. Les réglages de qualité, B-frames,
résolution, fréquence et audio sont conservés. L'ordre des séries est fixe, pas
randomisé ; les fluctuations ne sont pas une preuve causale d'un gain.

Chaque cellule est un p95 en millisecondes, calculé séparément par série et codec.

| Série / codec | Raw | DirectShow | Paquet encodé | Candidat décodé | Callback→RTMP |
|---|---:|---:|---:|---:|---:|
| Référence / x264 | 34,13 | 33,09 | 40,96 | 62,51 | 12,42 |
| Référence / NVENC | 35,85 | 41,00 | 69,86 | 133,00 | 13,46 |
| Échange optimisé / x264 | 34,02 | 37,01 | 42,26 | 65,72 | 24,38 |
| Échange optimisé / NVENC | 35,28 | 34,79 | 70,52 | 137,20 | 17,49 |
| Référence répétée / x264 | 34,55 | 33,50 | 40,71 | 59,73 | 15,22 |
| Référence répétée / NVENC | 35,40 | 36,40 | 69,12 | 135,53 | 11,92 |
| Flush regroupé, rejeté / x264 | 34,97 | 39,88 | 43,02 | 60,42 | 19,64 |
| Flush regroupé, rejeté / NVENC | 36,08 | 41,02 | 71,68 | 136,36 | 8,17 |
| Flush restauré / x264 | 35,29 | 36,58 | 41,94 | 62,42 | 19,42 |
| Flush restauré / NVENC | 37,73 | 31,67 | 71,99 | 137,92 | 13,52 |
| Candidat final, horloge corrigée / x264 | 34,45 | 30,87 | 42,30 | 61,60 | 19,02 |
| Candidat final, horloge corrigée / NVENC | 36,57 | 41,29 | 73,24 | 131,58 | 23,90 |

Source numérique : `evidence/253/eleven/20260907T113300Z-study.json` : percentiles
non arrondis, nombre de prises, identité du runtime, empreintes des traces et
rapports, distributions détaillées et couverture. Les rapports originaux ont été
produits par le parseur authentifié ; le script de synthèse est secondaire et ne
remplace pas cette validation. Les enregistrements et traces complets restent des
artefacts locaux de campagne, pas des fichiers produit.

Les traces, manifestes d'authentification, rapports et empreintes binaires des six
séries sont archivés dans
`evidence/253/eleven/20260907T115000Z-run-evidence.zip`
(SHA-256 `48683855AE949A406624081BF364E344F34C03C7E852BF1A5DA519186EAEF018`).
Les dossiers originaux, avec les vidéos, sont conservés localement dans
`D:/Documents/Zab/Artifacts/2026-09-07/pulsar-253-study/`.
La clé d'authentification DPAPI reste locale et hors Git dans le dépôt canonique ;
elle est liée au compte Windows. L'archive seule ne fournit pas cette clé.

Le « candidat décodé » est l'image correspondant au paquet sélectionné, observée à
FFmpeg showinfo puis à l'arrivée du log. Ce n'est **ni la première image changée
garantie, ni le premier pixel affiché, ni la latence antenne**. Le décodage ajoute
une charge et une latence de journalisation au récepteur. Les échecs RTMP ne peuvent
donc pas être attribués à libobs seul sans comparaison packet-only contrôlée.

## Modifications retenues dans la branche d'étude

1. `0046-perf-libobs-preserve-equal-view-activation.patch` : le chemin rapide
   précédent MAIN/AUX n'était pas utilisé par les deux vues MAIN du frontend.
   Pour une permutation pure de deux racines distinctes entre vues du même type,
   les compteurs d'activation et d'affichage restent invariants. On évite quatre
   appels d'activation/désactivation et leurs huit parcours d'arbre ; propriété
   des références, callbacks, barrière de drain et cas génériques sont conservés.
   Un test C natif exécute le corps réel extrait du patch sur huit topologies,
   avec références supplémentaires et seuil d'admission. Ce n'est pas une preuve
   de concurrence ni un gain chiffré en millisecondes.
2. `0047-fix-dshow-retain-equal-clock-stage-observations.patch` : des lectures
   successives de l'horloge peuvent être égales. Les exiger strictement croissantes
   supprimait des mesures valides, en favorisant les opérations lentes. Les champs
   complets, positifs et non décroissants sont désormais acceptés ; retours en
   arrière, zéro et données incomplètes restent refusés. Aucun scheduling changé.
3. Diagnostic décodé optionnel, corrélation stricte par PTS et diagnostic séparé
   du décalage PTS de l'image encodée. Le comportement par défaut reste packet-only.
4. Build `-Fast` : conserve la capacité CEF du cache headless existant. Il ne
   transforme plus implicitement un cache `-Full` en build sans navigateur ;
   les restrictions frontend/UI restent en place.

## Optimisation rejetée et explications mesurées

- Supprimer le Flush D3D11 avant conversion en gardant les soumissions de handoff
  et readback ne donne pas de gain reproductible. Prototype retiré du stack actif ;
  diff conservé en preuve. Pas de suppression aveugle de synchronisation GPU.
- Le paquet NVENC sélectionné peut être une image future dans l'ordre d'affichage.
  En référence : décalage de 0 / 1 / 2 images dans 21 / 55 / 24 prises ; x264 : zéro
  dans 100/100. Une partie du grand intervalle Take→CTS est donc la sélection liée
  au réordonnancement, pas le temps d'exécution de Take.
- CTS→retour callback est voisin d'une image dans la référence (p95 environ
  17,9 ms x264, 17,6 ms NVENC). Cela ne mesure pas isolément l'exécution matérielle
  de l'encodeur : un callback peut restituer un paquet antérieur.
- Après correction de couverture, le filtre DirectShow couvre 100/100 prises
  mesurées par codec. Son temps total p95 vaut 2,04 ms x264 et 4,87 ms NVENC ;
  lecture de file 0,98 / 1,83 ms, livraison 0,72 / 2,17 ms. Ne pas additionner
  les percentiles. Le consommateur tourne à sa propre phase 60 Hz : l'attente
  avant son entrée n'est pas le temps de copie à l'intérieur du filtre.

## Leviers restants et coût de preuve

| Levier | Potentiel / contrainte | Preuve manquante avant promotion |
|---|---|---|
| Réveil DirectShow sur image disponible | Réduire l'attente de phase jusqu'à une période ; conserver cadence et horodatage | Pacing, réveils perdus, arrêt/redémarrage, sous-charge/surcharge et consommateurs réels |
| Retrait d'attentes du staging borrowed | Éviter une attente de surface ; réutilisation interdite tant que le consommateur lit | Propriété et durée de vie GPU/CPU, génération des slots et tests de concurrence |
| Sélection de la première image changée décodée | Corriger la mesure sous réordonnancement plutôt que promettre un gain encodeur | Identifier le contenu de toutes les images et trouver la première image nouvelle |
| Politique NVENC de faible délai | Réduire la profondeur de réordonnancement | Comparaison de qualité à débit identique ; aucun changement de B-frames dans cette étude |
| Récepteur RTMP moins intrusif | Séparer retard du récepteur et émission | Campagne packet-only A/B sans décodage additionnel, puis décodage mesuré séparément |

Ces pistes ne sont pas implémentées ni épuisées. Une étude ne démontre pas que
toutes les optimisations imaginables sont terminées. La priorité est de rendre
la mesure décodée indépendante et de qualifier les attentes de phase, avant une
refonte des synchronisations avec un gain présumé.

## Validation et statut de livraison

- Build headless avec CEF de référence réussi ; replay du stack final et build
  `-Fast` réussis, sans perte de CEF.
- 308 tests Python réussis, 1 ignoré ; test natif intégré au CMake racine réussi.
- Audio runtime : 100 Cuts x264 et 100 Cuts NVENC réussis, identité/output Program
  stables, AAC 48 kHz, PTS continus, mutation Preview isolée.
- Fade et Stinger x264 : quatre cas `queued`/`final_queued` réussis, pixels
  décodés avant/après, événements terminaux uniques et corrélation PTS. Première
  invocation invalide avec chemin relatif ; relance avec chemin absolu réussie.
  Aucune couverture NVENC de ces transitions n'est déduite de ce script x264.
- Les douze probes de latence ont terminé sans crash, mais les analyseurs ne sont
  pas tous verts : conserver les verdicts RTMP en échec et la capacité non prouvée.
- Ni CI distante, ni merge, ni déploiement, ni fermeture globale #255 ne sont
  attestés par cette étude. Le candidat reste séparé de main.

Rollback : ne pas intégrer la branche ; après intégration éventuelle, revert des
patches 0046/0047 et des changements de diagnostic/build. Aucun changement de
format de média, de codec ou de contrat public n'est requis.
