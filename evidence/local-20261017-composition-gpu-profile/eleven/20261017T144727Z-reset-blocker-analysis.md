# Native group-composition reset blocker: source analysis checkpoint

Created: 2026-10-17 14:47:27 UTC. This checkpoint records the bounded profile and source-level diagnosis before moving to the separately authorized native reset-stability worktree. No Pulsar product source was edited, and no build, runtime, or GPU run was started during this diagnosis.

## Checkout and evidence identity

- Repository/worktree: `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-composition-gpu-profile`
- Branch / HEAD: `eleven/local-20261017-composition-gpu-profile` / `b8a2ee202d132a46519e8dd0c0a9a1c8a9e12913`
- Existing profile report: `20261017T143737Z-group-composition-gpu-profile.json`, SHA-256 `ed4e9a6c13d53a5bcab44d5169e159ceaf16ca4b39711406b33a960adf41950d`.
- The report records instrumentation SHA-256 `92f59097fe87b252219b3d7091629bca97463db3e1f49f45adb418a9b405d0a8`. That exact script snapshot is not present in the worktree or the searched run evidence. The currently retained `scripts/probe-group-composition-performance.py` hashes to `b79ec8249e4933ebccb65f6fd9c92b137a1f9ed55078c07c0f5c335c40e50354`; it is a later, unrun version and is not represented as the run source.
- The run used the private runtime under `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-composition-gpu-profile\.cache\native-composition-performance\private-runtime-556f21fc5b8944dd99c283c7e6708abb\runtime`. The qualified OBS source and ABI cache to preserve is `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition\.cache\native-group-composition\upstream-source-25ed780b7f392a1690dfca929cd57679959cf3ad` and sibling `upstream-build-25ed780b7f392a1690dfca929cd57679959cf3ad`. No processes from the recorded profile PID set remain active.

## What the run establishes

The profile was a synthetic OBS workload at 1920×1080, nominal 60 fps, with recording and streaming off. Its sole measured scenario was `four-masked-blended-owners`, duration 20.016 s. It validated the committed composition and a pixel readback distinct from the nominal scene. The scene stayed selected as Program across the window. OBS reported 60.000 fps; render-frame count advanced from 634 to 1774 across the sampled interval, and `renderSkippedFrames` remained 11 (no increase during the window). Sampled rolling `averageFrameRenderTime` averaged 6.636 ms and peaked at 6.955 ms, below the 16.667 ms frame budget. This is scene-level render evidence, not a per-group shader counter. Private process memory stayed about 1401.64–1401.73 MiB during the samples; this does not establish a leak.

After that window, the log records a replacement graph: new group/capture/effect and mask-dependency sources appear, the old composition root is destroyed, and old composition filters are removed. The subsequent `ResetGroupComposition` request never released its WebSocket handler. The client timed out (`TimeoutError`); shutdown closed the socket and IO thread, but quiesce ended with `active_handlers=1 sessions=0` and the runtime took its fail-closed exit. The report says no forced termination was used. Thus the timeout was not merely a client-side response that completed on the server: an admitted synchronous handler remained active after disconnect.

Only the four-owner profile window completed. The three planned replacement/reset retention cycles are empty in the report, so they provide no repeated-reset or memory-growth result. The dynamic-atlas scenario was not measured. Sources and masks were synthetic image/color sources, not a physical capture or end-to-end Solar/CEF workload.

## Synchronous path and lifecycle facts

The plugin registers the vendor callback in `plugins/pulsar-capture-effects/src/capture-effects.cpp:791-792`. `plugins/pulsar-capture-effects/src/group-composition.cpp:1219-1223` calls `obs_queue_task(OBS_TASK_GRAPHICS, reset_graphics, ..., true)`. In `plugins/pulsar-websocket/src/websocketserver/WebSocketServer.cpp:504-517`, `onMessage` acquires a `HandlerLease` and captures it for the worker lambda; the worker processes the request synchronously through `WebSocketServer_Protocol.cpp`, `requesthandler/RequestHandler_General.cpp:168-194`, and `WebSocketApi.cpp:107+` before invoking the vendor callback. The lease lasts until that callback path returns, matching the observed `active_handlers=1` after the WebSocket session disappeared.

The qualified libobs implementation is in the cache named above. Its `obs_queue_task` (`libobs/obs.c:3307+`, SHA-256 `cddf76be515395b7eb7e2c7add700d0ce69197dfc195bc9dbc7bded85239f2c3`) executes inline when already on the requested task thread. Otherwise, `wait=true` queues a wrapper and blocks on `os_event_wait`; the wrapper signals only after the requested callback returns. There is no deadline in that wait. The graphics loop drains queued tasks in `obs-video.c:960-973` (SHA-256 `960f447681d1bc2aaccdacece16f126cd4ca4c4d9ee58ddbdbfde85838fdad68`) and runs each task while holding `video->task_mutex`. A client timeout therefore cannot cancel the native wait or callback.

`reset_graphics` (`group-composition.cpp:1022-1086`) validates scene, generation, runtime instance and active-root identity, then atomically restores item visibility, removes the composition root, releases retained state/dependency references, erases the active composition and sets the response. There are no phase markers in this run, so it is unknown whether the graphics task was still queued, had entered validation, or blocked during scene removal/release. The returned timeout and outstanding lease alone do not distinguish those cases.

Replacement cleanup uses `CandidateGuard` (`group-composition.cpp:666-681`) to remove the old root and release saved references. The log proves the old root and filters were later destroyed/removed, but not that deferred source destruction completed before the next request. `obs_sceneitem_remove` in the matching `obs-scene.c` (`:2424-2440`, SHA-256 `aa9527e4ddc00377ef9feF749055E94687DAEBDA12339665077B427D78753350`) detaches the scene item under the scene lock and releases its reference after unlocking. That operation emits the scene-item removal signal; it does not itself call `obs_source_remove`, so the plugin’s `source_remove_signal` cleanup path is not an obvious self-recursive callback for its own root removal. Source destruction can be deferred; matching `obs-source.c` SHA-256 is `5a28c52f1cb67bd37963f6f881680c067868b654670c731130698725eb961879`.

The mask dependency wrapper retains a reference to the referenced atlas/source and renders no pixels of its own. The composition graph edge is one-way in the reviewed paths; validation excludes recursive scene/group references. Reset releases these dependency refs through `release_state`. No direct reference cycle or nested blocking graphics enqueue from `reset_graphics` was found. This source inspection does not rule out a lock interaction with deferred destruction or unrelated concurrent source removal.

## Diagnosis and next discriminator

The proven failure boundary is the synchronous handler waiting for `reset_graphics` to finish. The exact blocked point is not proven. Do not change the request to fire-and-forget or treat the request timeout as successful cleanup.

The next private diagnostic should mark: request callback entry; immediately before and after graphics-task submission/wait; graphics-task entry; validation completion; before/after `obs_scene_atomic_update`; before/after `obs_sceneitem_remove`; before/after `release_state`; active-map erase; task exit. During the same bounded request, collect `GetStats` over a second connection and capture host thread stacks at timeout. If render counters advance but no task-entry marker appears, the task has not run; if they freeze and the video thread stack is in a prior task, reset is queued behind it; if the reset task entered, its final marker and stack identify the teardown stage. Preserve atomic replacement/reset semantics while instrumenting.

## Source hashes reviewed

- `plugins/pulsar-capture-effects/src/group-composition.cpp`: `b64e050f6931944d66497950a377546929948e2cf0e4d54411c09be8d8d3478e`
- `plugins/pulsar-capture-effects/src/group-composition-world-source.cpp`: `c99f3d539f820a3233120a0e2013c82ecc8f11674a8a2397e241b53976582bf6`
- `plugins/pulsar-capture-effects/src/capture-effects.cpp`: `1f625a897b2a86604a6b0d5ba619152b4f88eb5c0a043ea45ccbfe9dcf284e71`

No transition, readiness, switch-duration, shared-runtime, or product-source changes are included.
