# #253 — continuation native, résultat exigé

Mandat : poursuivre directement ici, sans agent, les modifications profondes de
libobs/OBS/NVENC jusqu'à un gain réel prouvé ; ne pas confondre une piste rejetée
avec une optimisation terminée. Ne pas annoncer un minimum global non démontré.
Base : 6b94d5c7b317640c787a80a9b9c2533a97b8e786, eleven/253-latency.
Worktree : Pulsar/.worktrees/eleven-253-latency ; propriétaire Eleven.
Thread : 01a045e2-996f-7913-99f0-2cd237d3a6f8 ; Work-Unit ZabLaboratory/Pulsar#253.
Autorité : modifications natives, tests et expériences locales ; pas de pilote,
firmware, secret, push, merge ou déploiement. Main sale exclu.

| Critère | Risque | Commande / contrôle | Preuve attendue |
|---|---|---|---|
| Récupérer les sorties NVENC indépendamment du prochain tick | attente déplacée, ordre B-frames, buffer réutilisé en vol | completion events NVENC, soumission/récupération distinctes, test natif de cadence/arrêt | paquets délivrés entre deux entrées sans altérer PTS/DTS |
| Réduire l'image réellement décodée | métrique trompeuse, gain perdu au récepteur | probe-253-benchmark mode marker, mêmes réglages et sources visibles, A/B apparié | baisse reproduite de premier repère, pas seulement du paquet sélectionné |
| Réduire le reste de la chaîne si nécessaire | changement A/V, comportement sources | mesurer rendu/transfert/retour et qualifier les changements | gain propre et bout-en-bout |
| Conserver les invariants | qualité, audio, capacité, arrêt | Python/natif, audio runtime, pixels/PTS/continuité et charge | aucun affaiblissement des critères |

Les rejets ready-drain synchrone et CUVID de l'incrément précédent sont conservés,
pas rejoués comme pistes nouvelles. Catalogue NVIDIA actuel : 350 compétences,
aucune correspondance Windows NVENC spécifique ; aucune installation. Guide
NVENC 13.0 consulté : async Windows/WDDM, événement par sortie, FIFO de soumission,
libération après récupération ; pool et compression inchangés.
