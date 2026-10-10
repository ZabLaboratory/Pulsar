# Native camera-effects handoff

Worktree : Pulsar/.worktrees/eleven-local-20261009-capability-audit. Branche : eleven/local-20261009-capability-audit. Base : c05dca2627afbfa42b3cc8bb5e830432147c390f.

Ajout autonome : plugins/pulsar-camera-effects, option CMake PULSAR_BUILD_CAMERA_EFFECTS ON, documentation, probe-camera-effects.py. Aucun patch libobs, CEF ou capture-effects. Le shader embarqué preserve alpha et n'ouvre pas de producteur de capture. Packaging utilise le staging existant de tous les modules.

Build : CMake/Visual Studio 17 2022 x64 RelWithDebInfo, headers libobs canonique en lecture seule et import library de .cache/upstream-build. DLL SHA256 80D778EA19654EB131F7743A3626381E00F6E80948B6022CA0C7DA5BBB79610D.

Preuve native finale : evidence/local-20261009-capability-audit/eleven/20261010T082800Z-vignette-native.json. Registration/manifeste, pixels RGB, alpha 0/128/255, paramètres live, bypass/amount zero, empilement chroma et retrait passent. À 1080p60 : bypass 0,353230 ms, effet 0,428321 ms moyens ; 303 frames/5s par mode, zéro frame de rendu sautée. Source blanche synthétique, pas de vrai périphérique/Live/enregistrement ; charge GPU concurrente, mesure indicative. Arrêt code 0 sans kill forcé, zéro erreur staging.

Deux premières sondes sont conservées : la première lisait le pixel immédiatement après la commande avant le tick vidéo et échouait. La sonde attend maintenant une frame conforme avec délai borné. Cela ne prouve pas un effet NVIDIA IA : les SDK/modèles sont absents.

Le runtime commun et les worktrees étrangers restent préservés, ainsi que le .cache préexistant non suivi. Intégration proposée seulement du nouveau module, CMake et documentation ; conserver le module capture-effects du coordinateur. Synthèse transverse dans le rapport feature-report de Prism, même dossier de preuve.
