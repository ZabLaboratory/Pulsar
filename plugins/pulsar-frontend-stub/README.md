# pulsar-frontend-stub

Frontend callbacks for Pulsar's headless service.

`libobs` exposes a `frontend-api` layer (`obs-frontend-api.dll`) whose
function table is filled by whichever frontend is running — the OBS
Studio Qt UI in upstream, this stub in Pulsar. Without callbacks set,
every `obs_frontend_*` call logs `"Tried to call X with no callbacks"`
and returns null. obs-websocket's `EventHandler` registers an event
callback through this layer; if no frontend is in place its events
never fire and v5 clients see a frozen state.

This component is **not** an OBS plugin. It is a static library linked
into `pulsar.exe` and orchestrated by `pulsar-headless`'s `main()` in
two phases: the vtable is installed *before* `obs_load_all_modules()`
so plugins find populated callbacks during their own `obs_module_load`,
then the heavy state (encoders, sources, outputs that depend on
plugin-registered factories) is built *after* `obs_post_load_modules()`.

## Surface

| API | Purpose |
|---|---|
| `pulsar_frontend_init()` | Construct the callbacks object, install it via `obs_frontend_set_callbacks_internal`, and register an `obs_set_ui_task_handler`. **No state creation here** — `obs_x264`, `ffmpeg_aac`, `window_capture`, `rtmp_common`, `ffmpeg_muxer` factories are owned by plugins not yet loaded. |
| `pulsar_frontend_finished_loading()` | Run `setup()` (Default scene + fade transition + x264/aac encoders + outputs with encoders attached + window_capture source + record directory) then emit `OBS_FRONTEND_EVENT_FINISHED_LOADING`. |
| `pulsar_frontend_shutdown()` | Emit `OBS_FRONTEND_EVENT_EXIT`, then hand the object back to `obs-frontend-api` (which deletes it). The destructor runs `teardown()` which gracefully stops any active output (poll-with-timeout-then-force_stop), unbinds the main video mixer channel, and releases all libobs handles. |

## Event sources

| `obs_frontend_event` | Triggered by |
|---|---|
| `FINISHED_LOADING` | Explicit, from `pulsar_frontend_finished_loading()`. |
| `EXIT` | Explicit, from `pulsar_frontend_shutdown()`. |
| `SCENE_CHANGED` | `set_current_scene()` mutation. Also rebinds main mixer channel 0. |
| `TRANSITION_CHANGED` | `set_current_transition()`. |
| `TRANSITION_DURATION_CHANGED` | `set_transition_duration()`. |
| `STREAMING_STARTING` | Manual, before `obs_output_start` on stream output. |
| `STREAMING_STARTED` | Signal `start` on stream output. |
| `STREAMING_STOPPING` | Manual, before `obs_output_stop`. |
| `STREAMING_STOPPED` | Signal `stop` on stream output. |
| `RECORDING_*` | Same pattern, on recording output. |
| `RECORDING_PAUSED` / `UNPAUSED` | Signals `pause` / `unpause`. |
| `REPLAY_BUFFER_*` | Same pattern, on replay buffer output. |
| `REPLAY_BUFFER_SAVED` | Signal `saved`. |
| `VIRTUALCAM_*` | Same pattern, on virtualcam output. |
| `STUDIO_MODE_ENABLED` / `DISABLED` | `set_preview_program_mode()`. |
| `PREVIEW_SCENE_CHANGED` | `set_current_preview_scene()`. |
| `TBAR_VALUE_CHANGED` | `set_tbar_position()`. |
| `SCENE_LIST_CHANGED`, `SCENE_COLLECTION_*`, `PROFILE_*` | Single Default scene/collection/profile in current phases; mutations no-op. Phase 7+ may wire these once `pulsar-multi-stream` introduces multi-scene routing. |

## Phase 5 / Phase 6 scope

- One scene `Default` with one `window_capture` source (target read from `PULSAR_CAPTURE_WINDOW` env var, format `<title>:<class>:<exe>`, method=WGC). Unset = source emits black frames.
- One `fade_transition`.
- Single immutable `Default` scene_collection and profile.
- `obs_x264` (default ~2500 kbps) + `ffmpeg_aac` (mixer 0, 160 kbps) bound to both `recordOutput` (`ffmpeg_muxer`) and `streamOutput` (`rtmp_output`). Encoders share between both — Phase 7 (`pulsar-multi-stream`) will fan out destinations on top of the same encoder.
- `recording_start()` resolves `<recordDir>/pulsar-<YYYYMMDD-HHMMSS>.mp4`, mkdir-p the directory, `obs_output_update({path})`, then `obs_output_start`. `recordDir` defaults to `<cwd>/recordings`, override via `PULSAR_RECORD_DIR`.
- `streaming_start()` will return without effect until Phase 7 configures a real destination URL via `set_streaming_service`.
- Virtual camera and replay buffer outputs created but inactive — no encoders are wired to them yet.

## Validation

- `scripts/probe-events.py` — `SetStudioModeEnabled` round-trip → `StudioModeStateChanged` event end-to-end.
- `scripts/probe-record.py` — `StartRecord` → 3 s capture → `StopRecord` → file ≥ 100 KB on disk.
