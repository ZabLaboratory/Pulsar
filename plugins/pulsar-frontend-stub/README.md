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
into `pulsar.exe` and initialised explicitly by `pulsar-headless`'
`main()` *before* `obs_load_all_modules()`, so the websocket plugin
finds the callback table populated when its `obs_module_load` runs.

## Surface

| API | Purpose |
|---|---|
| `pulsar_frontend_init()` | Construct the callbacks object, create default scene + transition + outputs + service, install via `obs_frontend_set_callbacks_internal`. |
| `pulsar_frontend_finished_loading()` | Emit `OBS_FRONTEND_EVENT_FINISHED_LOADING`. Called once `obs_post_load_modules()` returns. |
| `pulsar_frontend_shutdown()` | Emit `OBS_FRONTEND_EVENT_EXIT`, release outputs/scene/service, set callbacks back to `nullptr`. |

## Event sources

| `obs_frontend_event` | Triggered by |
|---|---|
| `FINISHED_LOADING` | Explicit, after module post-load. |
| `EXIT` | Explicit, before `obs_shutdown`. |
| `SCENE_CHANGED` | `set_current_scene()` mutation. |
| `SCENE_LIST_CHANGED` | Currently single-scene; reserved for Phase 6+. |
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
| `SCENE_COLLECTION_*`, `PROFILE_*` | Single `Default` collection/profile in Phase 5; mutations no-op. |

## Limits in Phase 5

- One scene `Default` and one transition `Fade`. Multi-scene management
  ships in Phase 6 along with window-capture sources.
- Streaming output exists but has no encoders or service URL configured.
  `streaming_start()` will return `false` until Phase 7 (`pulsar-multi-stream`)
  configures destinations.
- Recording output exists; can be driven by setting the `path` on its
  settings before calling `recording_start()`.
- Profile / scene collection / theme APIs return `Default` and are immutable.
- Virtual camera and replay buffer outputs created but inactive until
  configured externally.
