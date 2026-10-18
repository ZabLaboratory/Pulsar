# Reset gen2 freeze: static source-order checkpoint

Status: diagnosis in progress; no product fix is justified by the current evidence.

## Reproduction evidence

- Worktree: `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-native-reset-stability`
- Branch / HEAD: `eleven/local-20261017-native-reset-stability` / `2738e3646fe5be32f10cd7d25aa8cf4ff5a292ee`
- Exact reproduction report: `evidence/local-20261017-native-reset-stability/eleven/20261017T152059Z-reset-exact.json`, SHA-256 `899043C64B760808FC78B310AA7D60400E37D382380FD04554ABB6B8F6303C85`.
- Host log: `evidence/local-20261017-native-reset-stability/eleven/20261017T152059Z-reset-exact-host-log.txt`, SHA-256 `20CD0A724C3C884181EEC8C04357A9D1C5912122980D4D2F883B46AB6FF982C2`.
- The report records the qualified OBS source at commit `6328ff8b3c29f10f29b7e1fad75f170efc41e198`, `obs.lib` SHA-256 `b11eee9ea91c6bcff2d9fe62cfe6edba2fbcff57036c8cfb9d8f3f1b5a0729e4`, and the instrumented capture-effects plugin SHA-256 `46672529bd84619e5d1cda632ba4c9c7173ddab5bd282fdbdff84b61fe511221`.
- The exact plan had four owners and eight captures, root appearance normal/visible/opacity 1/no mask, 1920x1080 at 60 fps. Generation 2 recommit returned `ok: true`. The render counter advanced from 588 to 1820 during the active 20.015-second window, then stayed at 1820 throughout the independent GetStats samples during reset; the WebSocket stats requests themselves continued to return.
- The post-generation-2 screenshot timed out. Reset timed out after 20.016 seconds. Trace phases are only `request_enter` and `queue_wait_begin`; neither `graphics_enter` nor `queue_wait_end` appears. The shutdown helper fixture was registered, but WebSocket quiescence later failed with active handlers, so the host exit is not evidence of a fully clean shutdown.

## Source order established

1. In `plugins/pulsar-capture-effects/src/group-composition.cpp:953-1027`, generation 2 builds a candidate, calls `obs_scene_atomic_update`, swaps the candidate into `active_compositions`, updates the generation fence, and sets the success response. `CandidateGuard::~CandidateGuard` at lines 691-699 removes/releases the displaced root and releases saved originals when the callback scope exits. Because the synchronous commit returned `ok: true`, that callback and this synchronous guard cleanup completed; the reset freeze is later than the commit callback.
2. `reset_group_composition_request` at lines 1270-1278 calls `obs_queue_task(OBS_TASK_GRAPHICS, reset_graphics, ..., true)` and emits `queue_wait_begin` immediately before it. The missing `graphics_enter` means this run never entered `reset_graphics`; therefore none of reset's atomic visibility restoration, root removal, state release, or active-map erase began.
3. In qualified OBS `libobs/obs.c:3307-3340`, a synchronous task from a non-task thread is queued through `task_wait_callback` and waits on an event. `libobs/obs-video.c:1099-1135` runs the graphics loop and drains graphics tasks after tick/output/render work. Its `execute_graphics_tasks` at lines 959-974 invokes each task while holding `video->task_mutex`. The current trace shows the reset waiter did not receive its turn; it does not by itself say why the graphics loop stopped.
4. The host log's last old-generation teardown entries are `source 'Pulsar composition group 1 prism.native-composition.root.v1' destroyed` followed by removal of old composition filters 54 and 53. There are no corresponding filter `source ... destroyed` entries before the frame counter freezes. This narrows the time window to retirement/destruction, but a missing log line does not prove the thread is blocked in a particular destructor.
5. OBS `libobs/obs-source.c:765-780` defers source destruction, calls `obs_context_wait(&source->context)`, emits `source_destroy`, then calls the plugin's `info.destroy`. `capture-effects.cpp:234-255` removes the filter from its registry, releases `background_scene` and `coverage_source`, then enters the graphics context and destroys the shader/texrenders. These are concrete next instrumentation boundaries, not a proven deadlock location.

## Conclusion and next discriminating evidence

The proved order is: gen2 commit and old `CandidateGuard` cleanup return; old root/filter retirement is logged; rendering stops; ResetGroupComposition waits before its graphics callback. No exact mutex owner, re-entrant call, or blocking instruction is established. In particular, `obs_context_wait`, filter `Effect.destroy`, release of the backdrop/coverage dependencies, and `obs_enter_graphics` remain candidates only. Do not change reset semantics or add asynchronous/timeout success behavior from this evidence.

The next private reproduction should add phase markers around old source/filter retirement, every operation in `Effect.destroy`, and both sides of `obs_enter_graphics`; preserve the same four-owner/eight-capture workload. At the first frozen frame, capture native stacks for the OBS graphics thread and source-destruction worker, then correlate each wait with its owning lock/call site. Keep the exact runtime/plugin hashes with the run. No GPU host was started for this checkpoint.
