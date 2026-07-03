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

## Phase 5 / Phase 6 / Phase 9 / Phase 12a scope

- One scene `Default` with one `window_capture` source (target read from `PULSAR_CAPTURE_WINDOW` env var, format `<title>:<class>:<exe>`, method=WGC). Unset = source emits black frames.
- One `fade_transition`.
- **Native stinger compositing — DORMANT by default (ADR 003 §A4.3, #73).** The
  OBS-native stinger path added in #67 (a registered `obs_stinger_transition`
  source + the transition-through-output compositing on a program-scene change)
  is gated behind the boot env flag `PULSAR_NATIVE_STINGER` (**default off**).
  Flag **off** (default): no stinger source is registered, no transition is bound
  to the program output, and a `SetCurrentProgramScene` performs a brute hard cut
  (`obs_set_output_source(0, scene)`, the pre-#67 behaviour) — the M10 animated
  transition is rendered by Solar/CEF as an overlay, never by OBS (C-MECH). Flag
  **on** (`1`/`true`/`on`/`yes`): the #67 compositing runs, kept for a future
  capability. The flag is **operator/env-controlled only**, resolved once at boot
  in `setup()` from `std::getenv` — it is **never** derived from or reachable by a
  leaf / obs-websocket / network value (Bastion #76 invariant). The
  `stinger-demo.webm` asset (#64, sha256-pinned) is retained but only decoded
  under the flag.
- Single immutable `Default` scene_collection and profile.
- Video pipeline at **1080p60** by default (Phase 12a). `PULSAR_FPS` (24/30/48/60/120) and `PULSAR_RESOLUTION` (`<W>x<H>`) override at boot.
- **Video encoder** selected at boot (ADR 004 §3.1-3.2). `PULSAR_VIDEO_ENCODER` picks a family — `x264` (default) \| `nvenc` \| `qsv` \| `amf` \| `auto` — resolved against the live `obs_enum_encoder_types()` set (H.264 only). If the family is absent on the machine, `obs_video_encoder_create` returns null, or a knob is invalid, boot degrades silently to `obs_x264` with a logged warning — the spawn never fails on encoder choice. No live encoder swap (boot-fixed tier, like `PULSAR_FPS`). Knobs: `PULSAR_VIDEO_PRESET` (validated per family, unknown → family default), `PULSAR_VIDEO_PROFILE` (`baseline`/`main`/`high`, default `high`), `PULSAR_VIDEO_RATE_CONTROL` (`CBR`/`VBR`/`CQP`, default `CBR`), `PULSAR_VIDEO_KEYINT_SEC` (0..20, default 2).
- Default / x264 fallback path: `obs_x264` CBR at **6000 kbps**, keyint 2 s, preset `veryfast`, profile `high`, tune `zerolatency` (byte-identical to prior behaviour). `PULSAR_VIDEO_BITRATE` (200..50000 kbps) overrides at boot. `pulsar-multi-stream` exposes `GetVideoSettings` / `SetVideoSettings` to mutate the bitrate live via `obs_encoder_update`.
- `ffmpeg_aac` at **160 kbps** by default, `PULSAR_AUDIO_BITRATE` (32..512 kbps) overrides at boot.
- Both encoders are bound to both `recordOutput` (`ffmpeg_muxer`) and `streamOutput` (`rtmp_output`); `pulsar-multi-stream` (Phase 7) fans out destinations on the same encoder pair.
- **Audio** (Phase 9) — `wasapi_output_capture` on channel 1 (desktop), `wasapi_input_capture` on channel 3 (mic). Both use `device_id="default"` unless overridden via `PULSAR_DESKTOP_AUDIO_DEVICE_ID` / `PULSAR_MIC_DEVICE_ID`. Channel 2 is reserved for `wasapi_process_output_capture` (per-process loopback, e.g. a Google Meet tab in Chrome) — created only when `PULSAR_PROCESS_AUDIO_NAME` is set, and tolerated as missing on Windows builds older than 10 19041 where the source ID isn't registered.
- `recording_start()` resolves `<recordDir>/pulsar-<YYYYMMDD-HHMMSS>.mp4`, mkdir-p the directory, `obs_output_update({path})`, then `obs_output_start`. `recordDir` defaults to `<cwd>/recordings`, override via `PULSAR_RECORD_DIR`.
- `streaming_start()` returns without effect until `pulsar-multi-stream` configures a real destination URL via `set_streaming_service`.
- Virtual camera and replay buffer outputs created but inactive — no encoders are wired to them yet.

### Audio mixer convention

Pulsar follows OBS Studio's main-mixer channel layout:

| Channel | Source | env override |
|---|---|---|
| 0 | Default scene (video) | — |
| 1 | Desktop audio (system playback loopback) | `PULSAR_DESKTOP_AUDIO_DEVICE_ID` |
| 2 | Process audio (per-process loopback) — opt-in | `PULSAR_PROCESS_AUDIO_NAME` |
| 3 | Microphone | `PULSAR_MIC_DEVICE_ID` |

Channels 4-5 are unused; Phase 12+ may grow them for guest mics or VST chains.

## Validation

- `scripts/probe-events.py` — `SetStudioModeEnabled` round-trip → `StudioModeStateChanged` event end-to-end.
- `scripts/probe-record.py` — `StartRecord` → 3 s capture → `StopRecord` → file ≥ 100 KB on disk.
