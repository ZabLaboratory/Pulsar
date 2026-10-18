# Native group-composition foundation checkpoint

Unit: `local-20261015-native-group-composition`
Worktree: `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition`
Branch: `eleven/local-20261015-native-group-composition`
Base / pre-checkpoint HEAD: `b64bf5c2eaf09ddbc8a842f26208a5a354536a1c`

## Scope frozen at this checkpoint

The Pulsar vendor implementation now owns a validated ordered group graph,
explicit capture and Solar-band source bindings, affine/local-opacity handling,
replacement-set visibility leases, generation/runtime fences, reset, source
removal and shutdown payload ownership. Parsing/validation and graph ownership
are split across the focused group-composition modules. The private test helper
and readback probe exercise those contracts; the plugin README records the
ownership boundary and current exclusions.

Non-null coverage masks, non-normal group blend modes and affine shear remain
explicit pre-swap refusals. This is a foundation checkpoint, not completion of
the full style contract. No transition, readiness, lane or switch behavior was
changed.

## Validation and proof

- `cmake --build .cache/native-group-composition/plugin-build --target pulsar-capture-effects group-composition-graphics-remove-helper --config RelWithDebInfo` — passed.
- `python -m py_compile scripts/probe-group-composition.py` — passed.
- `git diff --check` and `git diff --cached --check` — passed.
- Latest private synthetic probe: `20261015T233047Z-group-composition-probe.json`; SHA-256 `1c0733e41d75566560fd9eaed12d0bc0efe52599994a04fe74db5922f4b3302`.
  It contains 33 PNG readbacks (the 33 `20261015T233111Z-3aaa3a56c5-*.png`
  artifacts). Reported checks include mixed capture/band composition under
  group opacity 0.5 (`[0,127,0,127]`, expected quantized alpha 127 or 128),
  successive commit (`[0,0,191,191]`), reset restoration, translated local
  opacity, nested transformed bounds, atomic refusal pixels, source removal,
  legacy visibility restoration and new-generation commit after same-name scene
  UUID recreation. The report and PNGs retain exact provenance and hashes.
- Stopped-graphics-thread ownership proof passed: helper removal happened after
  `stop_video` and before capture-plugin unload; capture unload observed
  `pending_before=1` and released `1`. The helper explicitly forwards a
  test-only private synthetic source pointer through the global signal because
  libobs intentionally suppresses `source_remove` for private sources. This
  tests the plugin's real listener and the stopped-queue ownership boundary; it
  does not claim ordinary private-source removal emits that global signal.
- The process exited without forced termination (exit code 0, 0.265 seconds),
  and the probe scratch directory was removed. This is process-exit/lifecycle
  evidence, not an error-free global shutdown result. The frontend stub drained
  its source graph before capture-plugin unload (`active_before=0`), so this run
  does not prove an active composition map was present at module-unload entry.

## Remaining shutdown diagnostic

The probe's overall result is RED and `cleanShutdown=false` because it retained
one error verbatim:

`libobs | Tried to call obs_frontend_remove_event_callback with no callbacks!`

The log places `pulsar-frontend-stub | callbacks uninstalled` before the
`obs-websocket` module unload messages; the error occurs during that unload.
The probe does not attribute this error to group composition, does not suppress
it, and does not claim a clean global shutdown. I will inspect the exact private
module/callback order after this checkpoint without changing the common runtime.

No installed runtime/DLL, shared runtime, physical capture device, public
output, transition, scene switch, network or push was used. The synthetic-only
bootstrap initialized no WASAPI endpoints.
