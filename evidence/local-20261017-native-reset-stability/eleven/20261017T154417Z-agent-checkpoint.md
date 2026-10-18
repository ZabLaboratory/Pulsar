# Checkpoint — instrumentation du blocage de ResetGroupComposition

Horodatage : 2026-10-17 15:44:17 UTC

## Espace et base

- Dépôt : `D:\Documents\Zab\Pulsar`
- Worktree : `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-native-reset-stability`
- Branche : `eleven/local-20261017-native-reset-stability`
- HEAD avant ce checkpoint : `cf35af590fc0244dcfb14909e6e2f9ab611c17e7`
- HEAD initial de l’unité : `2738e3646fe5be32f10cd7d25aa8cf4ff5a292ee`
- Source OBS qualifiée : HEAD `6328ff8b3c29f10f29b7e1fad75f170efc41e198`, arbre propre. Le suffixe `25ed780b...` dans le nom de cache n’est pas sa révision Git.
- `obs.lib` qualifié : SHA256 `b11eee9ea91c6bcff2d9fe62cfe6edba2fbcff57036c8cfb9d8f3f1b5a0729e4`.

## Fait établi avant l’instrumentation

Le workload de référence reste le rapport [20261017T152059Z-reset-exact.json](20261017T152059Z-reset-exact.json), SHA256 `899043c64b760808fc78b310aa7d60400e37d382380fd04554abb6b8f6303c85`, et son journal [20261017T152059Z-reset-exact-host-log.txt](20261017T152059Z-reset-exact-host-log.txt), SHA256 `20cd0a724c3c884181eec8c04357a9d1c5912122980d4d2f883b46ab6ff982c2`.

Le scénario a conservé quatre owners masqués/mélangés, huit captures masquées/mélangées, une racine `normal`, visible, opacité `1`, sans masque, en `1920x1080@60`. La fenêtre de rendu a duré `20.015 s` : les compteurs GetStats sont passés de `588` à `1789` images rendues. Le remplacement gen 2 a répondu `ok=true`. Ensuite la capture d’écran de la scène a expiré ; sur la fenêtre Reset de `20.016 s`, GetStats répondait encore sur la connexion indépendante, avec `renderTotalFrames=1820` inchangé. La trace du binaire de ce run ne contient que `request_enter` et `queue_wait_begin` : le callback graphique n’est pas entré et l’attente synchrone n’est pas revenue. Ce fait ne localise pas encore le verrou ni l’appel bloquant. Le rapport complet conserve les erreurs de screenshot, les stats brutes et le statut d’arrêt du host : le helper de shutdown était enregistré, mais le pré-shutdown et le quiesce websocket n’ont pas réussi, le code de sortie était `1`, sans terminaison forcée. Ce run ne prouve donc pas un arrêt propre.

La note d’ordre d’exécution antérieure est [20261017T153020Z-reset-source-order.md](20261017T153020Z-reset-source-order.md), SHA256 `5cf5dbea81b88456d60aa76865bcf44f6c80ceaadd90dce274f41650fcb27abc`. Elle identifie les endroits à observer, pas une cause prouvée.

## Instrumentation préparée pour le prochain run

Les traces natives sous `PULSAR_GROUP_COMPOSITION_RESET_TRACE`/`PULSAR_NATIVE_RESET_TRACE` consignent le thread Windows et les étapes de retirement, du callback `Effect.destroy`, du verrou de registre, des libérations de références, de `obs_enter_graphics`, des ressources GPU et de la sortie. Les phases déjà présentes autour de `reset_graphics` distinguent l’entrée du callback, les validations, l’update atomique, la suppression de la racine, la libération d’état et le retour au thread appelant.

Le probe exact peut lancer un helper privé après `2 s` sans progression de `renderTotalFrames`. Le helper utilise `MiniDumpWriteDump` avec `MiniDumpWithProcessThreadData|MiniDumpWithThreadInfo` (`0x1100`) et les informations ordinaires de thread/contexte/pile. Il exclut les dumps de mémoire privée, indirecte ou complète, les handles et les modules déchargés. Le plafond de rapport est `64 MiB`; le rapport conservera la taille réelle, le hash, le PID, les heures de début/fin et la durée. Le helper a une limite de `8 s`, puis une terminaison bornée à `2 s`. Les piles ne sont pas symbolisées par ce mécanisme.

Le dump sera écrit uniquement dans `.cache/native-reset-stability/`, jamais dans `evidence/`. Les connexions `GetStats` restent indépendantes pendant la capture du screenshot et pendant Reset. Le budget de `64 MiB` est un seuil explicite de qualification enregistré après écriture; un dépassement sera marqué et ne sera pas accepté comme capture dans le budget. Aucun host ni dump réel n’a été lancé avec cette instrumentation ; le helper a été vérifié avec un PID inexistant, qui retourne l’erreur attendue `OpenProcess failed`, code `2`, sans créer de fichier. `cdb`, WinDbg et ProcDump n’étaient pas disponibles dans les chemins vérifiés ; le probe appelle le `DbgHelp` système directement.

## Validation et artefact privé

- Le plugin a été compilé seul avec `cmake --build .cache/native-reset-stability/plugin-build --config RelWithDebInfo --target pulsar-capture-effects -- /m:4`.
- `python --version` : `3.11.9`; AST du probe exact et `--help` réussis.
- Helper minidump : erreur négative attendue vérifiée sur PID inexistant, sans artefact.
- `git diff --check` réussi.
- DLL privée : `.cache/native-reset-stability/plugin-build/RelWithDebInfo/pulsar-capture-effects.dll`, SHA256 `efb62a4996a7962fa65b7a28d5cc444aba07d6c21aea67a9e476a89778ae2dfa`.
- Le DLL n’a pas été installé ni copié vers le runtime commun. Aucun host Pulsar, CMake, MSBuild ou compilateur `cl.exe` n’était actif à la vérification.
- Le GPU reste réservé à CEF ; aucun nouveau run n’a été demandé ou exécuté.

## Hashes exacts des sources de diagnostic

| Fichier | SHA256 |
|---|---|
| `plugins/pulsar-capture-effects/src/capture-effects.cpp` | `a2221d6f123c4674ade79891bff0db54ede86243486a94732ccdb41d7db32b27` |
| `plugins/pulsar-capture-effects/src/group-composition-world-source.cpp` | `8ba7093896e355edc3490661878e74434fa086e75a10cbe61ac7b3b40148e4cf` |
| `plugins/pulsar-capture-effects/src/group-composition.cpp` | `93ce68c0d44d35cd5f5cbbed73aafdb127871969e1375c40ed7dc4d60646aee91` |
| `plugins/pulsar-capture-effects/src/reset-trace.hpp` | `3216cdc611212bda00e08e1bc27b9d2e910d29b323eb8e4f8d1aa5ecad6764d6` |
| `scripts/probe-group-composition-reset-exact.py` | `eb5aec5aec3cbc4b63ecbe6635dbcd9e1b564e9b8630f390acb5266bae4e05d4` |
| `scripts/probe-group-composition-reset-stability.py` | `77a183b79adb54babd7b5e5c76528795a03bd4b1aeaff9009de1f7737de3bdff` |
| Private instrumented DLL | `efb62a4996a7962fa65b7a28d5cc444aba07d6c21aea67a9e476a89778ae2dfa` |
| Qualified `obs.lib` | `b11eee9ea91c6bcff2d9fe62cfe6edba2fbcff57036c8cfb9d8f3f1b5a0729e4` |

## Limites et suite

Ce checkpoint prouve que le probe et le binaire instrumenté sont prêts, pas que le dump natif fonctionne sur un host bloqué et pas que la cause du blocage est connue. Après libération du créneau CEF, rejouer le workload de référence exact, conserver l’échec si le helper ne parvient pas à capturer, vérifier séparément le screenshot/GetStats et rapprocher les TID des marqueurs natifs des stacks non symbolisées. Ne modifier ni contourner la sémantique Reset avant d’identifier l’étape bloquante. Aucun changement de transition, readiness ou durée de switch n’est dans ce périmètre.
