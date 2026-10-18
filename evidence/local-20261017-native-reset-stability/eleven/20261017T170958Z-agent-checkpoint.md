# Native capture lock correction checkpoint

Owner: Eleven; root thread 01a11a8b-8f4f-7fc2-abc4-7983042e4f63. Worktree: D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20261017-native-reset-stability. Branch: eleven/local-20261017-native-reset-stability. Base: 355ac2553d4d9912f4bf358770963c055475a806. Root took over retained work when the previous agent became unavailable.

The live mutex-owner witness 20261017T164854Z identifies reciprocal ownership between scene enumeration during removal and capture-effect rendering resolving a parent by global name. The root independently maps exact caller slots/PDB identities in the Prism aggregate proof 20261017T165300Z-native-lock-independent-review.json. This is bounded live typed-memory evidence plus exact caller slots, not a full or atomic memory snapshot.

Product changes:
- Weak backdrop parent bound directly by composition/appearance admission; render promotes it without global lookup. Missing/replaced parents recover in video tick outside scene/effect locks. Valid identity survives rename.
- Appearance preparation resolves coverage and constructs detached filters before entering the atomic scene transaction. Admission revalidates layer/input/filter identity.
- Snapshot application receives the retained coverage directly. The replaced reference is released outside scene/effect locks so its last-owner destruction cannot invert their lock order.
- Generic update also resolves before the effect mutex and releases replaced coverage afterward. No inactive scene cache, rendering pipeline replacement, transition or switch-duration behavior was added.

Independent review by Sol identified the synchronous coverage lookup and final-reference destruction paths; both are corrected. Final review found no further concrete issue in the reviewed files, without claiming all schedules are proved.

Validation:
- CMake RelWithDebInfo build PASS, plugin SHA-256 1deaa45c64f30d877f93aa9133c6b562a306cacb72931a376f293e8c52403375. Existing trace flag remains enabled in this private build. Original plugin/PDB preserved in owned cache.
- Exact original four-owner/eight-capture workload: 170202, 170236, 170311 all PASS; each 20 s at 1920x1080, 1200/1200/1199 render-counter increments, zero added steady-state skips. All reset readback pixels restored; each host exited 0 without force. Independent verification: 20261017T170535Z-lock-fix-independent-validation.json.
- Broader capture effects/framing/group probe 170355 PASS: seven effects, 80 atomic appearances, six rotations, fractional framing, omitted-geometry reset, strict mask/wire and source-removal lifecycle cases, clean shutdown.
- Parent/coverage lifecycle plus full group probe 170855 PASS: late parent creation, rename with competing old name, remove/recreate, removed-mask replacement, foreign-filter collisions. Host 7480 exit 0, no force.
- Two harness issues were corrected, with failed attempts retained: forwarding new group-probe optional arguments, and using whole-image canonical-collision comparison after source dimensions change. Module startup diagnostics then isolated a long staging path that skipped the test helper; shortening it restored helper load and the existing shutdown fence.

Limits: the exact replacement workload still adds 20/16/16 render skips between pre-recommit and pre-reset measurements. This patch fixes identified lock paths, not that reconstruction cost or full fluidity. Synthetic private runtime only, no physical-device/CEF/two-lane/installed/Live/REC or endurance qualification. The retained obs.dll has actual CMake source HEAD 70a5830 with recorded dirty browser/auxiliary paths; clean repository 6328ff is only a source/ABI reference. No full reproducible native runtime build claim.
