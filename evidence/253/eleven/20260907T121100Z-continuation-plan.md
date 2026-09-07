# #253 continuation — réduction de latence et audit décodé

Mandat : poursuivre directement les optimisations, descendre aussi bas que possible
sans perte de qualité/capacité/audio/dual-lane, et auditer le candidat décodé.
Branche reprise : eleven/253-latency, base d'étude 88883259807f625dcf513db443e77f6017eec1ec.
Pas d'agents. Éditions et validations locales autorisées ; aucun merge/déploiement
inféré. Ne pas modifier le checkout canonique et son submodule préexistant.

| Critère | Risque | Vérification | Preuve attendue |
|---|---|---|---|
| Décodage : identité et première image changée | ordre B/P, faux premier, logs, contenu absent | repère visuel A/B, PTS/démux exacts, tests négatifs | candidat séparé du premier repère changé |
| Coût d'instrumentation isolé | décodeur ralentissant le demux | même binaire et workload, packet-only / décodage répétés | séries non agrégées et configuration conservée |
| Réduction d'attente réelle | phase consommateur, durée de vie des buffers | profil source, prototype ciblé, comparaison A/B | gain reproductible ou rejet documenté |
| Non-régression | qualité/audio/cadence et propriété des frames | tests, audio, pixels, métriques raw/DShow/RTMP | aucun seuil abaissé, limites explicites |

Toutes les preuves nouvelles restent dans evidence/253/eleven ; fichiers de travail
dans le worktree. Restitution : modifications, mesures, incertitudes, commit signé
et conservation des preuves avant nettoyage.
