# Frontend callback shutdown-order extension

Unit: `local-20261015-native-group-composition`
Worktree: `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition`
Branch: `eleven/local-20261015-native-group-composition`
Foundation checkpoint: `c44767a272332b46cbd3afaaabc3c10dc8764fbd`

## Authorized extension

After the signed foundation checkpoint, the unit coordinator authorized a
minimal correction for the independently reproduced frontend callback lifecycle
error. Allowed scope is the obs-websocket EventHandler and its existing frontend
stub EXIT lifecycle, the owner tests/probe and their focused docs/evidence. No
common runtime, transition, switch, or duration path is in scope.

## Evidence and owner diagnosis

The no-composition private shutdown control
`20261015T232140Z-group-composition-shutdown-control.json` and the full private
probe `20261015T233047Z-group-composition-probe.json` used the same
`obs-websocket.dll` SHA-256
`0d12aa1e1a887a5402f1d6c275d8eb5fa8735d6373de2e46a7ac465c13ff6e52`. Both
show the same `Tried to call obs_frontend_remove_event_callback with no
callbacks!` error after the frontend stub reports callbacks uninstalled and
during obs-websocket module unload. The first report's `passed=true` denotes
its earlier process-exit criterion only; its captured error means it is not
clean-shutdown evidence.

Current source path and hashes:

- `plugins/pulsar-websocket/src/eventhandler/EventHandler.cpp` —
  `21a99abfe4acd05638dcf319d8b35fb62aab51730e2d000f4ca250d4bdc6a13b`.
  Constructor registers `OnFrontendEvent` at line 26; destructor unconditionally
  calls `obs_frontend_remove_event_callback` at line 51. Its event switch handles
  `SCRIPTING_SHUTDOWN` but not `OBS_FRONTEND_EVENT_EXIT`.
- `plugins/pulsar-websocket/src/obs-websocket.cpp` —
  `a7218ae8699ad65214ab2d77726afa73cebabd6ec85f1dceb9a0700a5e67382b`.
  `obs_module_unload` releases `_eventHandler` at line 231, after the websocket
  server stop.
- `plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp` —
  `6798dcf360c088203a48d22036769ae549ed7b7b70f20e3e1724bb7b1f09500a`.
  `pulsar_frontend_shutdown` emits EXIT at line 7892, then removes the callback
  table at line 7896. The existing `PulsarFrontendAPI::on_event` snapshots the
  callback vector under `callbacksMutex` and invokes the snapshot after releasing
  the lock (lines 3289-3297), so removing the currently executing callback during
  EXIT dispatch does not recurse into that mutex.
- Pinned upstream `76c902175d102476b4263cac85af76b31c8f01c9`,
  `frontend/api/obs-frontend-api.cpp` SHA-256
  `66560bd457969e197d814676205628d614eb22d8448fcadf47adebb5e5eea99d`:
  `obs_frontend_set_callbacks_internal(nullptr)` resets the owning unique_ptr;
  the exported `obs_frontend_remove_event_callback` checks that pointer before
  virtual dispatch and logs exactly the observed error when it is absent.
  The upstream checkout had unrelated pre-existing modified files at read time;
  none were edited by this unit.

The observed call chain is `pulsar_frontend_shutdown()` → EXIT emission →
callback table unset → `obs_shutdown()` module unload → `obs-websocket::obs_module_unload()`
→ `_eventHandler = nullptr` → `EventHandler::~EventHandler()` → exported
`obs_frontend_remove_event_callback()` with no installed table. The headless
shutdown test already requires frontend shutdown before `obs_shutdown`, so
changing that host order is outside and would contradict an existing lifecycle
contract.

## Bounded correction plan before edits

Use the existing EXIT event instead of adding another shutdown mechanism:
route `OBS_FRONTEND_EVENT_EXIT` through EventHandler's existing exit cleanup,
unregister its frontend callback while the callback table is still alive, and
track registration so the eventual destructor is idempotent. Keep the exported
API's missing-table error intact. Validate unregister ordering and no error in a
private no-composition shutdown control, then rerun the 33-image synthetic
composition/lifecycle probe with the pending-removal assertion (`1` observed,
`1` released). Keep shutdown errors as a hard failure. Update the focused
obs-websocket/frontend-stub documentation and canonical evidence in the same
unit.

No product edit has been made for this extension yet. No shared runtime or
installed binary was run or changed.
