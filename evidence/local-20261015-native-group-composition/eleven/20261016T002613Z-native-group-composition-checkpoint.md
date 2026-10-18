# Native group composition checkpoint

Recorded at 2026-10-16 00:26:13 UTC in the assigned Pulsar worktree. This is a private candidate checkpoint, not an installed-runtime admission.

Worktree: D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition  
Branch: eleven/local-20261015-native-group-composition  
HEAD before this checkpoint commit: c03d4f9ef77874e8b90d679f74b73d6999e21776

## Rebuilt binary provenance

The earlier probe at 20261016T000840Z recorded a WebSocket plugin hash of 3130c2cf... Its exact build producer was not yet proven, so that run remains historical.

The current private build was forced through MSBuild /t:Rebuild for the generated pulsar-websocket.vcxproj, configuration RelWithDebInfo|x64; it completed with zero warnings and errors. The project outputs obs-websocket.dll into the private OBS build run directory and links the obs.lib and obs-frontend-api.lib from that same private build. The current DLL hash is d3ecaf55d8adc4a9e593589d0ec232ac4974db00980a41361a3fb8df989ef26b; its associated PDB hash is 9b315c67362d33932aba18d9bceda08c119535141d024c88f8fcd99732931e37.

The private upstream checkout is 70a58302435308e609e0b1114efe3ef402f810b7, with a valid signature, descended from the Pulsar gitlink pin 25ed780b7f392a1690dfca929cd57679959cf3ad. The private Pulsar build source's upstream junction points to that exact checkout. The compiler-visible OBS public/internal/graphics/frontend headers match the checkout hashes. obs.dll and obs-frontend-api.dll in the probe runtime match their private-build copies. The build output, import libraries, CMake caches, source copies, link and compile tlog hashes are recorded in 20261016T002526Z-websocket-abi-provenance.json; complete raw MSBuild output is 20261016T002430Z-websocket-build.raw.txt.

## Full probe

The full private synthetic probe at 20261016T002156Z-group-composition-probe.json passed and loaded the exact rebuilt DLL hash above. The wrapper exited 0; the owned process exited without forced termination, the scratch was removed, shutdown diagnostics were error-free, and pending-removal markers reached pending_before=1 and pending_released=1.

All 33 PNG readbacks match the probe report hashes. Four direct pixel reads were:
- mixed capture/band overlap at (260,150): [0,127,0,127] for group opacity 0.5;
- second commit at (32,32): [0,0,191,191];
- translated local-opacity sample at (288,32): [0,0,127,127];
- nested transformed sample at (772,378): [0,64,127,127]. The report's computed expected blue channel is 128; the persisted pixel is 127, a one-unit quantization difference already identified in review.

The source-red/source-blue fixture names refer to OBS v3 color-source parameter ordering; the saved RGB values are explicitly listed in the probe report. The test-only no-browser provider confirms zero browser sources but does not run CEF teardown. The probe uses synthetic-only sources, not a physical capture device or public output.

## Scope and limits

The supported capture/Solar-band composition, atomic refusal/reset, successive commits, source-dependency removal, same-name/new-UUID recreation, and graphics-task/module-unload pending-event proofs remain the tested scope. The active graph is removed by the frontend source-graph cleanup before capture-plugin unload; the report correctly distinguishes activeCompositionDisposedBeforeModuleUnload=true from activeCompositionCleanlyUnloaded=false.

The earlier browser-fence RED (20261016T000505Z) remains preserved: missing private pre-shutdown provider followed by exit 0xC0000005. The current no-browser fixture does not resolve that fail-closed path. No code change to pulsar-headless/main.cpp is part of this checkpoint. Coverage masks, non-normal group blends, and affine shear still refuse before replacing the active graph. No shared runtime, CEF/browser teardown, physical capture, install, or transition/switch behavior was exercised.

## Validation

- MSBuild /t:Rebuild of the single private WebSocket project: passed, 0 warnings, 0 errors.
- Full group-composition private GPU/readback probe with rebuilt DLL: passed; 33/33 hashes matched; exact DLL hash in probe matches the build output.
- python -m pytest -q tests/pulsar-headless-shutdown/test_websocket_quiesce.py: 9 passed.
- python -m pytest -q tests/pulsar-headless-shutdown/test_graceful_shutdown.py -k "not d3d11_direct_shared_copy_api_is_optional_and_validated": 12 passed, 1 deselected. The excluded test reads the deinitialized canonical upstream submodule; no submodule or common checkout was initialized or modified.
- git diff --check: passed.

The checkpoint does not claim support for masks, the non-normal blend set, shear, CEF teardown, or an installed runtime.