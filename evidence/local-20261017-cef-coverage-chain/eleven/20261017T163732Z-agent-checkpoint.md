# CEF Vision coverage-chain checkpoint

## Result

The single authorized private CEF/native run passed on 2026-10-17 16:36:10–16:36:42 UTC (host PID 9188). The runner used the corrected, committed `a175e365f5b8381d6e77ada61e2cc2a374366bb9` harness. It did not rebuild. The page loaded one immutable LSML scene, then changed the bound mask opacity from 0.5 to 1.0 through the existing main-presenter `type=patch` / `patchJson` path. The exact subsequent `frame-submitted` receipt flowed through Solar projection, Prism wire, the actual CEF browser-source atlas, and the native group-composition plugin.

The receipt kept `sceneId=vision-frame-mask-browser-proof` and sceneVersion `sha256:17a74ee4d77b2e62c03a92818262eeab74754ffb31a841c27df4cf0e3f68037f`; `pixelRevision` advanced 0→1 while `layoutProjectionRevision` stayed 0. The patch reported `changedNodes=[1,5,8]` at request sequence 3. Coverage stayed one surface and owner mappings stayed unchanged. Native scene-item identity remained 4. This demonstrates the same-scene paint update rather than a second load.

The actual CEF atlas captured the 0.5 coverage as RGBA `[127,127,127,127]` at partial, overlapping, and unclipped-child samples, with both clipped overflow samples `[0,0,0,0]`. After the patch those covered samples were `[255,255,255,255]`; clipped overflow remained zero. Native pixels at the same scene points changed from `[133,136,140,255]` to `[255,255,255,255]`, while the clipped overflow stayed `[12,17,25,255]`. The equal overlap sample confirms single-alpha composition. The root LSML band paints opaque `#0c1119` before the owner, so zero coverage correctly reveals `[12,17,25,255]`; this is not the separate OBS backdrop `[64,48,32,255]`.

The invalid-mask negative altered crop width by one texel. Native rejected candidate generation 2 with `invalid-mask-rect: mask crop and global rect must have exact one-to-one dimensions`; decoded screenshot pixel buffers remained exactly unchanged, and the rejected candidate did not advance the commit fence. Reset returned `ok=true`, `reset=true`, and the original source visibility was restored. The pinned historical presenter received the exact /2 receipt and rejected it in render with `The Rust engine returned an invalid native composition frame receipt.`; it emitted no `frame-submitted` event.

Shutdown was clean: exit code 0, graceful shutdown requested, no forced termination, browser count drained to zero at `barrier_released phase=Drained browser_count=0`, ordered CEF shutdown markers passed, and the HTTP page server stopped. PID 9188 is absent, and a read-only process scan found no process whose command line contains this run-specific runtime scratch path, so no host/helper child remains. No Live/REC or shared development runtime was used.

## Scope and limits

This is a real `Vision state patch → Solar projection → Prism wire → CEF atlas → native pixel samples` proof for the pinned 320×180 LSML fixture and its browser atlas. It is not proof of an Orion/LSDP proposal transaction, 1080p60 output, lane interchangeability, physical-camera capture, general parity, or switch transition/duration. The fixture's capture binding is synthetic. The CEF-reported websocket hash `3130c2cf…` remains absent from its retained overlay; this run used the retained native-stage `d3ecaf55…` websocket binary and successfully exercised the registered vendor API path.

The preserved earlier failed run `20261017T161417Z` remains unchanged. Its same-scene assertion was invalid because it reloaded a different LSML version; source inspection showed each load resets `pixelRevision` to zero. That earlier attempt is diagnostic history, not a product failure and not overwritten by this successful patch run.

A separate `python probe.py --help` introspection attempt failed before runtime because argparse's Unicode arrow could not be encoded by the Windows CP1252 terminal. The run flags were checked directly in the parser source; the actual bounded run completed normally. No product assertion or runtime was implicated by that help-output encoding issue.

## Pins and preflight

- Pulsar worktree: `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-cef-coverage-chain`
- Branch: `eleven/local-20261017-cef-coverage-chain`; source HEAD: `a175e365f5b8381d6e77ada61e2cc2a374366bb9` (signed)
- Solar: `e8ca9a3a6944fac68430fa9ddb79b93366a2f99a`
- Prism: `09b893dee789519bf63a0726427bd663c1e03104`; runtime consumer blob equals product baseline blob `016a579264550a4377d932ed447259da55e69ea0`
- Vision: `044ddb62df050eee9869150a69a1b88fd26f2962`; historical presenter negative baseline `85c49664a029744369b063ddffaa92415a59c8a8`
- Native clean source checkout pin: `6328ff8b3c29f10f29b7e1fad75f170efc41e198`; CEF source: `47cb08c56e010c5da45923a5eee147f294216756`. These are source/provenance checks, not a claim that the actual OBS runtime binary was built from the clean native checkout.
- Offline pin/runtime preflight: `20261017T163000Z-chain-preflight.json` passed with no runtime, build, or GPU use.
- Actual single run: `20261017T163604Z-cef-coverage-chain.json`, `passed=true`, `buildStarted=false`, `gpuUsed=true`.

## Evidence manifest

The main report contains the complete structured request/receipt/wire/native assertions and hashes for each captured image. Raw host log is retained locally for the report reference but excluded from Git because authentication material appears in two lines. A redacted line-preserving copy is included below.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `20261017T163604Z-cef-coverage-chain.json` | 59732 | `b46a6651ba70d8ed36cb10f373c5d3e6f0211af2e9bc3a0d2a92f8ac0cfe98df` |
| `20261017T163000Z-chain-preflight.json` | 14432 | `600aa91d97b833a5a1d857841dc0e4d546edc53e403704ec9c14776fbae23511` |
| `20261017T163610Z-3038d2d1-cef-atlas-initial.png` | 1748 | `798ab5b72be0fcc49885b5be1d301538c6ccdf234a51b3acfb08611b8db0e80c` |
| `20261017T163610Z-3038d2d1-cef-atlas-updated.png` | 1747 | `279143956b520eddc22b234bfa68a15d6c0bacab2d48902ee9ad499483b48ca2` |
| `20261017T163610Z-3038d2d1-scene-mask-050.png` | 735 | `8951b4883a4b8694baf40e73d92e14cf3904a0154bb4ef980513ac5dfc8c2ef9` |
| `20261017T163610Z-3038d2d1-scene-mask-100.png` | 734 | `285cd3411bc2141cd74a31109ea15d5120788622bf8f39420f2495f6bd562bb9` |
| `20261017T163610Z-3038d2d1-scene-after-negative.png` | 735 | `8951b4883a4b8694baf40e73d92e14cf3904a0154bb4ef980513ac5dfc8c2ef9` |
| `20261017T163610Z-3038d2d1-initial-chain.json` | 31015 | `37e95145a7d264231203aa4a1746e14101561fd5791c33c9b03037b49e0f8982` |
| `20261017T163610Z-3038d2d1-updated-chain.json` | 31015 | `3c368a87b3b6128e6f813dce4410eb2d716cfb6479b5a6266cfe77445d1959fe` |
| `20261017T163610Z-3038d2d1-backdrop-source.png` | 697 | `14f66420e3fa9965c27f7890a5ed155a711836af83ed10d4a508e982fc9e43f5` |
| `20261017T163610Z-3038d2d1-host-redacted.log` | 35761 | `b103af9133c48b79b69ab59722833fd8cdee4f5392e60ba82600e50abab23f58` |

The ignored raw host log is `20261017T163610Z-3038d2d1-host.log` (35842 bytes, SHA-256 `0857d4e7ff11ec001361c56727c5a8b36ce4b1625e35939ec828720c8a8fef94`). Two full lines matching password/key patterns were replaced, and trailing whitespace was trimmed from three other lines in the committed copy; the raw local file was not altered.

## OBS runtime build provenance caveat

The successful run proves behavior of the exact staged bytes, but its `obs.dll` source lineage is not the clean native checkout pin above. Parent-verified CMakeCache provenance says the `obs.dll` SHA-256 `891f6c58b1a9e865836fb94f03f8d90b238198b55c70d2656214cde4790478b1` was built from `X:/.cache/.../upstream-source-25ed780b7f392a1690dfca929cd57679959cf3ad-windows-checkout`, HEAD `70a58302435308e609e0b1114efe3ef402f810b7`, with modified `obs-browser` and untracked `nv-secure-load`; PDB `2083` corresponds to that `70a583` source state. The local CMakeCache confirms that original `CMAKE_HOME_DIRECTORY` string. The equivalent retained D: source checkout is `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition\.cache\native-group-composition\upstream-source-25ed780b7f392a1690dfca929cd57679959cf3ad-windows-checkout`; direct read-only Git verification returned HEAD `70a58302435308e609e0b1114efe3ef402f810b7` and status `M plugins/obs-browser`, `?? plugins/pulsar-nv-secure-load/`. The PDB mapping is parent-verified. Historical records: `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition\evidence\local-20261015-native-group-composition\eleven\20261016T002526Z-websocket-abi-provenance.json` and `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition\evidence\local-20261015-native-group-composition\eleven\20261016T003624Z-browser-failclosed-provenance.json`.

The clean `6328ff8` sibling checkout is not that CMake home. Therefore `nativeSourceCommit` remains the repository source pin audited by preflight and must not be cited as the source build for this `obs.dll`. The run used the captured runtime hash and successfully exercised the vendor API, while reproducibility from the clean pinned tree remains unproven. The provenance reconciliation is now an additive field in both the preflight and run JSON; it leaves their pass/assertion results unchanged. The original runner-output hashes before annotation are recorded within those fields. The separately staged capture plugin hash is `9b17835e8db5ed452fd59e0fbe31d07f12e022df888540db6986e3a73918d093`; its origin is recorded separately by the preflight/runtime manifest.
