# Same-scene mask patch harness checkpoint

## Diagnosis

The preserved 20261017T161417Z run failed the assertion “mask update did not revise the same scene through the same presenter.” Its actual initial/updated receipts retained sceneId, schema, canvas size, coverage count and mask-owner mapping; sceneVersion changed, while pixelRevision remained 0. The actual CEF coverage atlas changed at the three in-mask sample points from [127,127,127,127] to [255,255,255,255], with both clipped overflow samples still [0,0,0,0]. The report and pixels are preserved unchanged in commit a9bc923337321f8e2a469a5fc211cc43ab0721c0.

That was a harness error, not evidence of a Vision revision bug. The old page generated a new hashed LSML document for each opacity and sent type=load for both. Vision constructs each new VisionScene with display_revision 0 (vision3d source commit 044ddb62df050eee9869150a69a1b88fd26f2962, crates/vision-web/src/lib.rs:798-806); loading replaces the active scene (1744-1763). The contribution request uses the active scene identity and display revision (1446-1452). The source contract defines display_revision as advancing on accepted effective patches, while paint-only changes preserve layout_projection_revision (crates/vision-web/README.md:83-92; lib.rs:832-845, 1312-1316; test at lib.rs:5191-5206). The browser presenter patch handler calls engine.apply_patch(request.patchJson) and then submits the frame (crates/vision-web/ui/mainpresenter.mjs:487-526). Thus revision zero after a separate scene load is expected, and the old assertion did not exercise a same-scene patch.

## Harness correction

The page now loads one immutable LSML source version. Both opacity-bearing stencil frames bind opacity to the same state path, mask.opacity, with default 0.5. The update uses the existing main-presenter type=patch request with patchJson setting mask.opacity to 1.0. It requires non-empty changed_nodes metadata and the subsequent frame-submitted message with the same request sequence, then sends that actual frame and unchanged LSML document through the existing Solar projector and Prism emitter.

The probe now requires stable sceneId and sceneVersion, increasing pixelRevision, stable layoutProjectionRevision, owners and coverageSurfaces, a non-empty accepted mask.opacity patch, and frame sequence agreement. It preserves all alpha/clipping, native invalid-mask rejection, candidate generation reuse, full decoded-pixel equality on rejection, native reset, and old-presenter negative assertions. None of those oracles was weakened or removed. The README labels this path as a Vision state-patch-to-composition proof, not an end-to-end Orion/LSDP proposal transaction.

## Pins and validation

- Worktree: D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-cef-coverage-chain
- Branch: eleven/local-20261017-cef-coverage-chain
- HEAD before this checkpoint: a9bc923337321f8e2a469a5fc211cc43ab0721c0
- Offline Prism pin updated to 09b893dee789519bf63a0726427bd663c1e03104. The adopted Prism consumer remains compared against the unchanged product baseline 49080662c422eded328bcf0e7fdf8dd16febb0d2.
- Node syntax check for page.mjs: pass.
- Python AST parse for probe.py and preflight.py: pass.
- git diff --check for owned source/docs: pass.
- Offline provenance preflight: pass, runtimeStarted=false, buildStarted=false, gpuUsed=false; report 20261017T162726Z-chain-preflight.json.
- No CEF/GPU/native host or build was started after the diagnosis. The 161417 host had already stopped gracefully; its shutdown proof remains in the earlier checkpoint.

## Still unqualified

The corrected real patch path has not yet been run through the native CEF atlas and native screenshot. The next private run must use the current Prism pin and prove the actual patch frame through Solar projection, Prism wire, CEF atlas, native initial/update pixels, reset, and the pinned historical-presenter negative. This does not measure transitions or switch duration. The direct Vision patch path is not a separate proof of the complete Prism/Orion LSDP proposal flow.
## Source and preflight manifest

| File | Bytes | SHA-256 |
|---|---:|---|
| scripts/cef-coverage-chain/page.mjs | 14304 | 34b3b8c90257b7a401bed9f3be493c8e07748534d775578efc6d30416fbaccb9 |
| scripts/cef-coverage-chain/probe.py | 49669 | 8b80e24033132c77f9727e086d4cb4082dc766f776c35fc1a3676cf9aa035799 |
| scripts/cef-coverage-chain/preflight.py | 15236 | 7043e42e2be16c414eb98f75d0de04e7cffea2b957c7f8e8bb43e7fbc5d01e68 |
| scripts/cef-coverage-chain/README.md | 3908 | f80790c07d14f88dbcf98bed17680dce4a79c66980576d9c2573e3106284674e |
| evidence/local-20261017-cef-coverage-chain/eleven/20261017T162726Z-chain-preflight.json | 11096 | e6e2369ceb8012f78c64d6edfdaaa38bdd784c3c60c8826abce7cc4ec99c3428 |

## Pause and retained runtime inputs

The corrected patch harness has not been run. Per the active D: cleanup priority, this unit is paused before any CEF/native host, GPU use, rebuild, or file-generating test. No process from this unit is currently active.

The next real-chain proof depends on preserving these already qualified local inputs (the full hashes are recorded in the preflight JSON):

- Native host and OBS runtime: `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition\.cache\native-group-composition\upstream-build-25ed780b7f392a1690dfca929cd57679959cf3ad\rundir\RelWithDebInfo\bin\64bit\pulsar.exe`, `...\bin\64bit\obs.dll`, and `...\obs-plugins\64bit\obs-websocket.dll` (selected private-stage binary SHA-256 `d3ecaf55d8adc4a9e593589d0ec232ac4974db00980a41361a3fb8df989ef26b`).
- Native composition plugin: `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-native-group-coverage\.cache\native-group-coverage\plugin-build\RelWithDebInfo\pulsar-capture-effects.dll`.
- CEF overlay: `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-cef-runtime-qualification\.cache\cef-runtime\upstream-build\rundir\RelWithDebInfo\obs-plugins\64bit\pulsar-browser.dll`, `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-cef-runtime-qualification\.cache\cef-runtime\upstream-build\rundir\RelWithDebInfo\obs-plugins\64bit\pulsar-browser-page.exe`, and `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-cef-runtime-qualification\.cache\cef-runtime\upstream-build\rundir\RelWithDebInfo\obs-plugins\64bit\libcef.dll` (exact qualified files and hashes are listed in `20261017T162726Z-chain-preflight.json`).
- Solar's adopted Vision presenter and WASM assets: `D:\Documents\Zab\Solar\.worktrees\eleven-local-20261015-project-integration\public\vision` and `...\dist\host\vision`; presenter, fonts, glue, and WASM hashes are pinned in the same preflight record.
- LSML fixture: `D:\Documents\Lumencast\lumencast-vision\.worktrees\eleven-local-20261017-native-mask-coverage\crates\vision-web\tests\contribution-webgl2-proof\contribution-scene.lsml`.

These are inputs owned by their respective product worktrees; this unit did not create or delete them. The exact source pins and all harness artifacts remain under the assigned Pulsar worktree.
