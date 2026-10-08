# #253 — campagne directe de réduction de toute la chaîne

Mandat utilisateur : cette conversation est la session de travail. Aucun agent,
aucune nouvelle tâche, aucun redispatch. Réduire chaque attente réelle, en priorité
NVENC ; modifications profondes de libobs, OBS et de l'intégration NVENC autorisées.
La demande de lancement de modèle ne doit plus servir de substitut à l'exécution.

Propriétaire : Eleven, thread 01a045e2-996f-7913-99f0-2cd237d3a6f8.
Work-Unit : ZabLaboratory/Pulsar#253 ; continuation de ba148405d325119ced5f43e8ba2c275b52be458e.
Worktree : Pulsar/.worktrees/eleven-253-latency ; branche eleven/253-latency.
Main et upstream canoniques préexistants sont exclus des écritures.
Autorité : source/tests/docs, expériences locales, commits signés. Aucun changement
de pilote/firmware, publication, push, merge ou déploiement implicite.

| Critère | Risque | Vérification | Preuve de décision |
|---|---|---|---|
| Take/composition et transferts plus rapides | déplacer l'attente ou avancer les pixels relativement à l'audio | bornes sur même contenu, cache/répétitions et phase A/V | réduction propre + bout-en-bout, sinon rejet |
| NVENC réellement moins latent | confondre débit, B/P et sélection de paquets | audit SDK/file d'attente, corrélation PTS/DTS/contenu, A/B | paramètres et images inchangés ou qualité à débit égal prouvée |
| Émission/réception sans rétention inutile | horodatage tardif dans le démux, collecte intrusive | séparer émission, arrivée, démux et décodage | durées appariées, traces authentifiées |
| Première restitution décodée | détourner le marqueur ou supprimer des images | identité de chaque image, ordre, trous, p95/p99 | vrai gain, pas changement de définition |
| Non-régression | qualité, A/V, cadence, capacité, arrêt | tests natifs/Python, audio/vidéo et charge ciblée | aucune exigence diminuée |

Acquis conservés : rapports et 12 séries dans Artifacts/2026-09-07/pulsar-253-decoded-audit.
NVENC référence : raw21-22ms, paquet RTMP71-72ms, premier repère décodé75ms,
image sélectionnée145-146ms p95. Current-readback+polling reste off : gain x264,
pas NVENC, phase A/V non qualifiée. Polling seul variable. Ne pas refaire les
contrôles valides sans changement les invalidant ; contrôler les nouvelles hypothèses.

Clôture de l'incrément : preuves de gains/non-régressions ou rejets expliqués,
code signé et exporté, limites restantes explicites. Aucun minimum global promis.
