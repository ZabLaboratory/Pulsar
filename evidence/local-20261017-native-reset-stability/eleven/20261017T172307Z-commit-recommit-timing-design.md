# Active-scene recommit timing candidate

Source worktree: `D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20261017-native-reset-stability`
Branch: `eleven/local-20261017-native-reset-stability`
Base HEAD before this continuation: `5b28fe10ba999ee804e29ec8c64b82a47597d422`
Common Git directory: `D:/Documents/Zab/Pulsar/.git`

This continuation instruments `CommitGroupComposition` only. It does not add a
same-hash fast path, change scene-switch readiness or duration, or retain an
inactive scene. The existing private `PULSAR_GROUP_COMPOSITION_RESET_TRACE`
compile definition gates an additive `responseData.debugCommitTiming` object.
It records steady-clock milliseconds for queue wait, complete graphics callback,
plan parse, capture resolution, group build, atomic visibility swap, retirement
of the prior candidate, and synchronous vendor handler. It also counts
successfully created capture-effect filter sources during group construction.
The final private build also sums the 12 `gs_effect_create` call durations,
excluding graphics-lock entry and texture allocation, and returns their count.
The callback total includes work not separately timed by the phase fields;
handler total excludes WebSocket transport. Existing per-object reset-trace
logging remains enabled in this private build and can perturb retirement time.

The exact reset probe now takes `GetStats` immediately before and after the
higher-generation, same-plan recommit, before its 0.5-second settling interval
or PNG readback. It records another sample after settling but before PNG, so
the immediate skip delta and later diagnostic effects are distinguishable.
The existing pixel and reset assertions remain in place.

The original corrected DLL used by the three prior exact runs is preserved at
`.cache/native-reset-stability/plugin-build/RelWithDebInfo/pulsar-capture-effects.dll`,
SHA-256 `1deaa45c64f30d877f93aa9133c6b562a306cacb72931a376f293e8c52403375`.
Those runs used this DLL built from then-uncommitted corrected code; their
`sourceHead` field alone is not the DLL provenance. That code was signed in
`5b28fe10` and subsequently integrated into the separate candidate `c71d9dd7`.

The first diagnostic DLL (`54555c50...`) instrumented an unused internal
handler. Its `20261017T172505Z-reset-exact.json` run passed the existing
behavior assertions but had no timing receipt, so it is not attribution
evidence. The corrected handler-only DLL (`64458830c84eeb72256523155d76b9005a8257b7b3d45428dce6f7d9c2b4f242`)
produced three complete exact runs:

| Report UTC | Round trip ms | Build ms | Retire ms | Immediate skipped frames | Filter creates |
| --- | ---: | ---: | ---: | ---: | ---: |
| `20261017T172709Z-reset-exact.json` | 228.5 | 190.5 | 34.8 | +12 | 12 |
| `20261017T172757Z-reset-exact.json` | 184.8 | 163.8 | 14.3 | +10 | 12 |
| `20261017T172840Z-reset-exact.json` | 183.7 | 162.3 | 14.9 | +10 | 12 |

The final shader-timed private DLL is
`.cache/native-reset-stability/commit-timing-build/RelWithDebInfo/pulsar-capture-effects.dll`,
SHA-256 `45f4eb4310de3e23c7fb0be910cd861030637a11dd25af316201f81eb95dae63`.
Its normal compile without the trace definition is
`.cache/native-reset-stability/commit-timing-normal-build/RelWithDebInfo/pulsar-capture-effects.dll`,
SHA-256 `064ea32830586e391dc1c33f341c9711e4393bbc3849b4701d6adffd5e9c3a01`.
Both RelWithDebInfo builds passed against the same pinned libobs headers/import
library. The final build transcripts are
`20261017T173000Z-commit-timing-shader-build.raw.txt` and
`20261017T173000Z-commit-timing-shader-normal-build.raw.txt` beside this note;
earlier configure/build transcripts are retained with their `172307Z` and
`172629Z` prefixes. Python AST parsing and `git diff --check` passed.

| Report UTC | Round trip ms | Build ms | Shader create ms / calls | Retire ms | Immediate skipped frames |
| --- | ---: | ---: | ---: | ---: | ---: |
| `20261017T173129Z-reset-exact.json` | 192.6 | 150.6 | 29.6 / 12 | 24.3 | +9 |
| `20261017T173223Z-reset-exact.json` | 185.0 | 155.8 | 30.1 / 12 | 19.4 | +9 |
| `20261017T173315Z-reset-exact.json` | 216.1 | 191.6 | 33.0 / 12 | 20.0 | +12 |

All six qualifying runs passed the existing pixel/reset checks and used
GetStats immediately after the synchronous recommit, before sleep or PNG.
In the final three, the 20-second steady window rendered 1201 frames each
with no new skipped frames across its 20 samples. Each host exited with code
0 after requested graceful shutdown, no forced termination and no residual
owned PID. These measurements cover the exact synthetic four-owner/eight-capture
active-scene fixture, not every real capture source or driver. The trace build
still logs per-object lifecycle events; its absolute durations need a clean
non-trace comparison before a production performance claim.

`gs_effect_create` accounts for 17.2-19.6% of group construction in the final
three runs. Sharing the mutable libobs effect would require serialization of
uniform writes and draw loops as well as reference-counted last-filter cleanup;
it is not an adequate first correction for the measured recommit cost. No
shader sharing or active-graph reuse was implemented in this checkpoint.
