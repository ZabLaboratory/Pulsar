# Clipping hérité des cadres — preuve native

Continuation du worktree eleven-local-20261009-capture-effects, branche eleven/local-20261009-capture-effects, HEAD ad2c290bb7ab1f73edc68add30fc31d35a577adc. Modifications héritées conservées. Aucun commit/push/merge.

Le filtre GPU capture-effects reçoit les projections locales de 32 cadres ancêtres maximum. Il applique leur intersection et leurs arrondis après les effets, dans sa dernière passe existante. Le nombre de passes et de textures reste inchangé ; un cadre ne crée aucun bitmap ni aucune nouvelle source physique. Le chemin de transport reste CommitAppearance.

Le build natif RelWithDebInfo passe. Le nouveau probe de pixels passe huit cas : cadre aligné, déplacé, tourné, intersection de deux cadres, arrondis, cadre vide, coupure des ombres hors cadre et reset identique au baseline. Les pixels hors cadre contrôlés ne fuient pas, les pixels strictement intérieurs conservent le baseline. Le probe historique capture-effects passe les sept effets, le reset et ses vérifications supplémentaires d'admission/rotation. L'essai utilise des sources fixtures privées dans un runtime jetable ; il ne prouve pas la disponibilité d'un périphérique physique. Aucune diffusion ni aucun enregistrement n'a été lancé.

La première extension détectée avec des uniforms non renseignés a été rejetée sur le banc et corrigée avant installation. Les preuves finales correspondent au module reconstruit et validé ; les premiers fichiers baseline entièrement transparents constituent uniquement la reproduction de cet échec corrigé.

Le module qualifié est installé dans le runtime Prism commun. La preuve de son chargement effectif et les switches appartiennent au rapport Prism : D:/Documents/Zab/Prism/.worktrees/eleven-local-20261008-scene-sync/evidence/local-20261013-render-remaining/eleven/.

Les clips ajoutés concernent les cadres rectangulaires/arrondis du modèle, avec rotations centrées. Cette correction n'ajoute pas de nouveau moteur de masques SVG ni de décomposition de matrices importées avec skew.
