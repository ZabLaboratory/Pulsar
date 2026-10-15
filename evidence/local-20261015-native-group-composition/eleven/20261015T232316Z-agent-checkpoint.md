# Group-composition shutdown checkpoint

Unit: `local-20261015-native-group-composition`
Worktree: `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition`
Branch: `eleven/local-20261015-native-group-composition`

## Finding and correction

The first bounded shutdown probe timed out at 30 seconds and needed a controlled
termination. A second no-composition control failed the same way, so the delay
was not caused by a live composition or the pixel probe. The private logs reached
`[pulsar-headless] shutting down`, drained the frontend source graph, and then
stopped after other modules' unload messages.

The pinned upstream source at revision
`76c902175d102476b4263cac85af76b31c8f01c9` explains the boundary:
`libobs/obs.c` calls `stop_video()` and joins the graphics worker before its
module-unload loop. The group plugin's previous `obs_queue_task(..., wait=true)`
therefore waited on a graphics queue with no worker. The plugin now closes and
disconnects source-removal admission, waits for signal callbacks to finish,
releases any still-owned removal-event payloads, and cleans graph references
directly under the still-live graphics context. A completion log records active
composition, generation-fence, and pending-event counts.

The source-removal event set owns each payload before enqueue. A normal graphics
callback removes its own payload from the set; module unload takes and releases
anything left after the worker stops. This covers the race where a removal task
was queued after the final graphics-task drain.

## Validation

- `cmake --build .cache/native-group-composition/plugin-build --target pulsar-capture-effects --config RelWithDebInfo` — passed.
- `python -m py_compile scripts/probe-group-composition.py` — passed.
- `git diff --check` — passed.
- Private shutdown control without a composition, report
  `20261015T232140Z-group-composition-shutdown-control.json` — passed; exit code
  0, no forced termination, 0.609 s shutdown; module-unload completion marker
  observed.
- Full private synthetic composition probe, report
  `20261015T232203Z-group-composition-probe.json` — passed; exit code 0, no
  forced termination, 0.234 s shutdown after the tested composition/lifecycle
  path; pixel assertions, source removal, graphics-task reentrancy, reset and
  scene UUID recreation all passed.

The full probe reports `active_before=0`, `fences_before=0`, and
`pending_before=0/pending_released=0` at module unload. The authored composition
was left active before host shutdown but frontend graph cleanup disposed it
before `obs_module_unload`. Therefore this evidence establishes successful
shutdown after an active composition lifecycle and successful empty-plugin
module unload; it does not claim that the final module-unload callback observed
and removed a nonempty composition map. The pending-event cleanup is implemented,
but a nonzero pending-event count was not observed in this pair of runs.

The pre-fix 30-second timeout and its no-composition control remain preserved as
`20261015T231307Z-group-composition-probe.json` and
`20261015T231457Z-group-composition-shutdown-control.json`; those reports record
forced termination and are not treated as passing evidence.

## Provenance and limits

The successful probe records the private runtime and plugin hashes plus the
upstream revision and source hashes for shutdown ordering and graphics-task
execution. No installed Pulsar runtime or common runtime was modified.

Non-null coverage masks, non-normal group blend modes, and affine shear still
refuse before swap. No physical capture, public output, transition, or scene
switch was exercised. The earlier capture/band pixel proofs remain separate.
