# Pulsar — Protocol

Pulsar speaks the **obs-websocket v5 baseline** plus a `pulsar:*` vendor
namespace. Existing v5 tooling (Stream Deck, Streamer.bot, Companion,
Aitum, custom dashboards) plugs in unchanged; the vendor namespace
covers capabilities the v5 baseline does not model — multi-destination
first-class, adaptive bitrate, and live encoder retuning.

The reference client is [`@clodocapeo/pulsar-client`](../packages/pulsar-client/),
which exposes typed wrappers over both surfaces.

## Connection

| | |
|---|---|
| Transport | WebSocket over TCP, **loopback (`127.0.0.1`) only**. |
| Port | `PULSAR_PORT` env var at spawn (default `4455`). The chosen port is echoed in the `PULSAR_READY` stdout sentinel. |
| Password | `PULSAR_PASSWORD` env var at spawn. Unset = Pulsar generates a fresh 22-char URL-safe random string and exposes it in the same sentinel. The seeded values are written to `<cwd>/obs-websocket/config.json` before `obs_module_load`, so a stale on-disk password from a prior session is never trusted. |
| Auth | Standard obs-websocket v5 challenge/response (sha256 of password + salt + challenge). A plain v5 client implementation handles the handshake unchanged. |
| Identify | `rpcVersion: 1`. `eventSubscriptions` defaults to `0x7FF` (all baseline categories). |

The READY sentinel format is stable and machine-parseable:

```
PULSAR_READY ws=ws://127.0.0.1:4455 password=8JvK56CjHa0-LYqS3dNC9n
```

A consumer reads stdout line-by-line until `^PULSAR_READY ` matches,
extracts `url` and `password` from the named groups, and opens the
WebSocket. Any line printed before the sentinel is libobs / plugin boot
log and may be forwarded to the consumer's log aggregator — but MUST
NOT block the boot. The sentinel always arrives last (or not at all,
in which case spawn timed out and the consumer should kill the child).

## Baseline — obs-websocket v5

Pulsar advertises **137 v5 request types** in its `GetVersion`
response. The full v5 reference is the source of truth:

➡️ <https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md>

The notable v5 surfaces Pulsar exercises:

| Category | Examples |
|---|---|
| General | `GetVersion`, `GetStats`, `BroadcastCustomEvent` |
| Scenes | `GetSceneList`, `GetCurrentProgramScene`, `SetCurrentProgramScene`, `CreateScene`, `RemoveScene` |
| Inputs | `GetInputList`, `CreateInput`, `RemoveInput`, `SetInputSettings`, `GetInputSettings`, `GetInputKindList` |
| Scene items | `CreateSceneItem`, `RemoveSceneItem`, `SetSceneItemTransform`, `GetSceneItemList` |
| Stream | `StartStream`, `StopStream`, `GetStreamStatus`, `SetStreamServiceSettings` |
| Record | `StartRecord`, `StopRecord`, `PauseRecord`, `ResumeRecord`, `GetRecordStatus` |
| Audio | `GetInputVolume`, `SetInputVolume`, `GetInputMute`, `SetInputMute`, `GetInputAudioSyncOffset` |
| Filters | `GetSourceFilterList`, `CreateSourceFilter`, `RemoveSourceFilter`, `SetSourceFilterSettings` |
| Outputs | `GetOutputList`, `GetOutputStatus`, `StartOutput`, `StopOutput`, `ToggleOutput` (by-name; multi-destination control goes through `pulsar:*`) |
| Vendor | `CallVendorRequest` (the transport for `pulsar:*`, see below) |

Three things to keep in mind when driving the baseline against Pulsar:

1. **A start/stop request never reports an effect it did not observe.**
   `StartStream`/`StopStream`, `StartRecord`/`StopRecord`,
   `StartReplayBuffer`/`StopReplayBuffer`/`SaveReplayBuffer` and
   `StartVirtualCam`/`StopVirtualCam` — **and the by-name generic trio
   `StartOutput`/`StopOutput`/`ToggleOutput`** — re-read the real output
   state after the action and answer:
   - **success** when the output reached the requested state, or when
     libobs accepted the action and is completing it asynchronously (an
     rtmp connect thread, an `ffmpeg_muxer` flush);
   - **error** — `OutputNotRunning` (501) for a start, `OutputRunning`
     (500) for a stop — when the action was refused. The `comment`
     carries `obs_output_get_last_error()` verbatim, never a generic
     message.

   These requests stay bounded and short: they verify a *refusal*, they
   never wait for activation. The request signatures, response fields and
   status enum are unchanged.

   This replaces the pre-`#120` behaviour where an unconfigured output was
   reported as started (`result: true` followed by
   `GetXStatus.outputActive: false`).

   On the generic trio the refusal `comment` also **names the output**:
   ``The output `PulsarStream` did not start: no streaming service is
   configured …``. `ToggleOutput`'s `outputActive` response field is the
   state **read back** from libobs after the action, not `!wasActive` — on
   an accepted-but-still-connecting start it is `false`, matching the
   `GetOutputStatus` the client issues next.

   `GetOutputStatus.outputReconnecting` is a straight read of libobs'
   reconnect atomic (`obs_output_reconnecting`); no mirror of it exists
   anywhere in the plugin. Note the libobs semantics it comes with:
   `obs_output_active` is `active || reconnecting`, so a reconnecting
   output reports **both** `outputActive: true` and
   `outputReconnecting: true`. Read the pair — `outputActive` alone does
   not mean bytes are leaving.
   **Pause is guarded the same way** (`#130`). `PauseRecord` and the pause
   branch of `ToggleRecordPause` refuse rather than report a pause they did
   not take:
   - `OutputNotRunning` (501) when no recording is running at all —
     `obs_output_pause()` returns false on an inactive output and the
     frontend entry point is `void`, so this used to answer success;
   - `InvalidResourceState` (604) when `GetRecordStatus.outputBytes` is
     still `0`. Pausing before the muxer wrote its first byte wedges
     libobs' pause timeline permanently (upstream defect: the pause
     window is computed from an encoder timestamp that is still zero, so
     the pause never lifts and the replay buffer sharing those encoders
     stops producing files). The `comment` names that cause; the client
     lifts the condition itself by polling `outputBytes` — the field is
     already part of `GetRecordStatus`.
2. **`StartStream` ≠ go live.** The v5 `StartStream` request talks to
   the singleton `PulsarStream` rtmp_output created by
   `pulsar-frontend-stub`. Since `#131` the frontend **binds the
   configured service to that output** (`obs_output_set_service`) before
   starting it, so the v5 single-stream path is genuinely usable by
   standard v5 clients (Stream Deck, Companion, Streamer.bot, Aitum) —
   configure a service with `SetStreamServiceSettings`, then
   `StartStream`. It succeeds as soon as libobs accepts the start; the
   TCP connect completes afterwards, so a success here means "accepted",
   not "on air" — poll `GetStreamStatus` for that, and expect
   `outputActive: false` for as long as the connect is in flight.
   When the bound service cannot be connected to at all (no service, or
   an `rtmp_common`/`rtmp_custom` whose server or key is missing) the
   request **fails** with `OutputNotRunning` and a `comment` naming the
   service as the cause.
   `StreamStateChanged` with `outputState: OBS_WEBSOCKET_OUTPUT_STARTING`
   is emitted **only after** `obs_output_start()` really took the action
   (it used to be emitted unconditionally, ahead of the start, so a
   refused start still put a `STARTING` on the wire). The event is
   therefore a reliable signal that libobs accepted the start; the
   `STOPPED` that follows a failed connect is the normal sequel.
   `pulsar:StartDestination` (multi-stream) remains the recommended path
   for Prism and for anything sending to more than one destination.
3. **`StartRecord` writes to `<cwd>/recordings/`** by default. Override
   with `PULSAR_RECORD_DIR` at spawn. Filenames are
   `pulsar-<YYYYMMDD-HHMMSS>.mp4`.
4. **`RemoveInput` really removes** (`#129`). The v5 contract promises the
   input's scene items go with it; libobs delegates that prune to the
   frontend, and Pulsar's frontend stub did not implement it, so the input
   and its items stayed listed forever unless the scene holding them was
   itself removed. The stub now handles libobs' `source_remove` signal and
   prunes every scene, so `GetInputList` and `GetSceneItemList` both stop
   listing the source — including in scenes that are not the program scene
   (which is precisely where the defect used to hide).

## `pulsar:*` vendor namespace

All vendor traffic goes through the v5 `CallVendorRequest` request.
The wire shape is:

```jsonc
{
  "op": 6,                            // Request opcode
  "d": {
    "requestType": "CallVendorRequest",
    "requestId": "<client-correlation-id>",
    "requestData": {
      "vendorName": "pulsar",
      "requestType": "GetDestinations",  // or any of the requests below
      "requestData": { /* request-specific fields */ }
    }
  }
}
```

The response wraps the same way: the v5 envelope's `responseData` field
holds the vendor's `responseData`. On error, the inner `responseData`
includes a string `error` field; everything else returns the
request-specific shape.

The reference client wraps this for you — `pulsar.callVendor("X", { ... })`
deals with the envelope.

### Field naming

The vendor handlers are written in C++ against `obs_data_t`, which uses
**snake_case**. The TypeScript client in this repo translates to/from
camelCase at its boundary (`packages/pulsar-client/src/wire.ts`); raw
clients see snake_case on the wire.

### Requests

| Request | Purpose | Request fields | Response fields |
|---|---|---|---|
| `GetDestinations` | List every registered destination. | — | `destinations: Destination[]`, `error?` |
| `CreateDestination` | Register a new destination. Server validates kind + URL/key combination. | `kind`, `name?`, `url?`, `key?` | `id`, `error?` |
| `RemoveDestination` | Remove a destination. Gracefully stops it first if active. | `id` | `removed: bool`, `error?` |
| `StartDestination` | Lazily create the underlying `obs_output_t`, attach shared encoders, call `obs_output_start`. | `id` | `started: bool`, `error?` |
| `StopDestination` | `obs_output_stop` + async muxer trailer write. | `id` | `stopped: bool`, `error?` |
| `StartAllDestinations` | Start every registered destination concurrently. Failed starts are logged server-side; the call resolves once the registry has tried them all. | — | `ok: bool`, `error?` |
| `StopAllDestinations` | Mirror of the above. | — | `ok: bool`, `error?` |
| `GetVideoSettings` | Snapshot the encoder + reset_video state. Includes the boot-fixed encoder identity (`video_encoder` / `video_preset` / `video_profile`). | — | `fps`, `width`, `height`, `video_bitrate`, `video_rate_control`, `video_keyint_sec`, `audio_bitrate`, `video_encoder`, `video_preset`, `video_profile`, `error?` |
| `SetVideoSettings` | Mutate encoder bitrates **live**. Setting `fps` / `width` / `height` is rejected — those require boot-time env vars (`PULSAR_FPS`, `PULSAR_RESOLUTION`). `video_encoder` / `video_preset` / `video_profile` are likewise rejected: encoder identity is boot-fixed via `PULSAR_VIDEO_ENCODER` (no live swap — see ADR 004 §3.4). Audio bitrate is only applied while the audio encoder is idle. | `video_bitrate?`, `audio_bitrate?` | `changed: bool`, `video_bitrate?`, `audio_bitrate?`, `error?` |
| `GetCapabilities` | Enumerate the encoder families this build exposes (via `obs_enum_encoder_types()`, mapped to Pulsar short names) plus the bitrate windows. Off-air detection; `active_encoder` is the family bound to the streaming output. See list-encoding note below. | — | `encoders: {value: string}[]`, `active_encoder: string`, `video_bitrate: {min, max}`, `audio_bitrate: {value: number}[]`, `error?` |
| `GetAdaptiveState` | Snapshot the bitrate adaptation worker. | — | `enabled`, `target_kbps`, `current_kbps`, `floor_kbps`, `stable_ticks`, `adjustments_total`, `last_delta_total`, `last_delta_dropped`, `last_drop_ratio`, `error?` |
| `SetAdaptiveEnabled` | Toggle the worker. Disabling pauses sampling; the encoder bitrate is left at whatever value the worker last applied. Re-enabling resets `stable_ticks` to 0 so the loop re-warms before any climb attempt. | `enabled` | `enabled: bool`, `error?` |

> **List encoding on the wire.** libobs vendor handlers can only serialise
> arrays as arrays of *objects* (`Utils::Json::ObsDataToJson` walks each
> `obs_data_array` item as an `obs_data`), never bare JSON scalar arrays. So
> `GetCapabilities` wraps each `encoders` / `audio_bitrate` element in a
> one-field `{ "value": … }` object. `@clodocapeo/pulsar-client`'s
> `CapabilitiesNamespace.get()` unwraps these back into flat `string[]` /
> `number[]` before handing the typed `PulsarCapabilities` to callers.

### Destination kinds

`CreateDestination` accepts one of three kinds. Each has its own
validation and lifecycle.

| Kind | URL | Key | Output type | Notes |
|---|---|---|---|---|
| `rtmp_custom` | required, `rtmp://` or `rtmps://` | required | `rtmp_output` | Generic RTMP — your private ingest, a co-host's RTMP, an OBS-compatible CDN. |
| `vod_local` | required, file path with `.mp4` extension | unused | `ffmpeg_muxer` | Local MP4 archive. Path is created if missing; existing file is overwritten. |
| `twitch` | **server-pinned** (Pulsar picks the closest Twitch ingest) | required | `rtmp_output` | The client's `url` field, if provided, is ignored. The server resolves and pins a Twitch ingest URL on `CreateDestination`; the resulting URL is what `GetDestinations[i].url` shows. |

### Events

All events are delivered via the v5 `VendorEvent` opcode. The envelope
includes `vendorName: "pulsar"`, `eventType`, and `eventData`.

| Event | Trigger | Payload |
|---|---|---|
| `BitrateAdjusted` | The adaptive worker changed the encoder bitrate (after a drop spike or a recovery climb). | `bitrate`, `target`, `floor`, `reason: "drops" \| "recovery"`, `drop_ratio` |

The v5 baseline events (`StreamStateChanged`, `RecordStateChanged`,
`InputCreated`, …) are emitted unchanged by `pulsar-websocket`.

## `pulsar-scene:*` vendor namespace

A second, distinct vendor name — `pulsar-scene`, owned by the
`pulsar-scene-source` plugin, not `pulsar-multi-stream`. obs-websocket's
`vendor_register_cb` rejects a second `vendor_register` call under the
same name, so each Pulsar plugin that needs vendor requests owns its
own namespace.

```jsonc
{
  "op": 6,
  "d": {
    "requestType": "CallVendorRequest",
    "requestId": "<client-correlation-id>",
    "requestData": {
      "vendorName": "pulsar-scene",
      "requestType": "SetCaptureSource",  // or GetCaptureSource
      "requestData": { /* request-specific fields */ }
    }
  }
}
```

| Request | Purpose | Request fields | Response fields |
|---|---|---|---|
| `SetCaptureSource` | Replace the active capture source with a fresh `browser_source` on the current frontend scene, removing any previously-installed Pulsar-managed capture item (`PulsarCapture` from `pulsar-frontend-stub`, or a prior `PulsarSceneSource`). | `kind` (only `"browser_source"` supported), `url`, `width?` (default `1920`), `height?` (default `1080`), `fps?` (default `60`), `reroute_audio?` (default `false`), `css?` | `kind`, `url`, `width`, `height`, `fps`, `reroute_audio`, `removed_prior: int`, `error?` |
| `GetCaptureSource` | Return the active capture source state. | — | `kind` (`"browser_source"` after a successful `Set`, else `"window_capture"` — `pulsar-frontend-stub`'s boot default), `url?`, `width?`, `height?`, `fps?`, `reroute_audio?`, `last_change_unix: int` (`0` if never set), `error?` |

**Errors** (`SetCaptureSource`) :

| `error` | When |
|---|---|
| `"kind_not_supported"` | `kind` is anything other than `"browser_source"`. |
| `"url_required"` | `url` missing or empty. |
| `"browser_source_unavailable"` | `pulsar-browser` (obs-browser fork) not loaded — a non-`-Full` build. |
| `"no_current_scene"` | `obs_frontend_get_current_scene()` returned null. |
| `"current_source_not_a_scene"` | The frontend's "current scene" source didn't unwrap into an `obs_scene_t`. |
| `"scene_add_failed"` | `obs_scene_add` returned null. |

Full detail: `plugins/pulsar-scene-source/README.md`.

## Replay buffer

`pulsar-frontend-stub` creates a replay-buffer output (`PulsarReplay`) at
boot **and wires it**: it borrows the exact same video/audio encoders
already bound to the record and stream outputs — *encode-once / fan-out*,
so arming the buffer adds **no** encoder to the process — and carries real
settings (directory, filename template, `max_time_sec`, `max_size_mb`).

No `pulsar:*` request is involved. The capability is driven entirely by
the **six v5 baseline requests**, which Pulsar has always compiled
(`RequestHandler.cpp:158-163`) and which now do what they say:

| Request | Behaviour |
|---|---|
| `GetReplayBufferStatus` | `outputActive` — `true` once armed. |
| `StartReplayBuffer` | Arms the buffer. **On-air only**, see below. |
| `StopReplayBuffer` | Disarms. |
| `ToggleReplayBuffer` | Arm/disarm. |
| `SaveReplayBuffer` | Flushes the buffered packets to an MP4 under the recording directory. Asynchronous: the file exists once the `ReplayBufferSaved` event fires. |
| `GetLastReplayBufferReplay` | `savedReplayPath` — the real path of the last saved replay (empty string until the first save of the session). |

**On-air only.** The buffer feeds off the shared encoders, which run only
while the stream or the recording output is active. `StartReplayBuffer`
issued with the encoders idle **fails** with `OutputNotRunning` and a
`comment` naming that exact cause (#120) — *"the encoders are idle —
nothing is streaming or recording…"* — rather than silently spinning an
encoder up for a partial, off-air pipeline, or reporting success on a
buffer that never started. The refusal is Pulsar's own, decided before
`obs_output_start`, so the stub publishes the cause through
`obs_output_set_last_error()`: the verification reads it off the output
like any libobs-recorded cause, and never falls back to a generic
message. Arm after go-live, disarm at stop.

Memory cost is `max_time_sec × (video + audio bitrate)`, held in RAM and
capped by `max_size_mb` — ~23 MB at 30 s / 6 000 kbps.

## Environment variables (`PULSAR_*`)

Every variable below is read once at process boot (`std::getenv`), never
re-read live, and never reachable from a leaf / obs-websocket / network
value — operator/env-controlled only.

| Variable | Read by | Default | Effect |
|---|---|---|---|
| `PULSAR_PORT` | `pulsar-headless` | `4455` | obs-websocket listen port. Rejected if outside `1..65535`. |
| `PULSAR_PASSWORD` | `pulsar-headless` | fresh 22-char random string | obs-websocket session password, seeded into `obs-websocket/config.json` before module load. |
| `PULSAR_FPS` | `pulsar-headless` | `60` | Output fps. Accepts only `24`/`30`/`48`/`60`/`120`; anything else is rejected with a warning. Boot-fixed (no live change). |
| `PULSAR_RESOLUTION` | `pulsar-headless` | `1920x1080` | Base/output resolution, format `<W>x<H>`, up to `7680x4320`. Boot-fixed. |
| `PULSAR_CAPTURE_WINDOW` | `pulsar-frontend-stub` | unset | `window_capture` target, format `<title>:<class>:<exe>`. Unset ⇒ source produces black frames (pipeline still encodes/records). Superseded per-scene by `pulsar-scene:SetCaptureSource` when used. |
| `PULSAR_VIDEO_BITRATE` | `pulsar-frontend-stub` | `6000` (kbps) | Boot video bitrate, `200..50000`. Mutable live afterwards via `pulsar:SetVideoSettings`. |
| `PULSAR_VIDEO_ENCODER` | `pulsar-frontend-stub` | `x264` | Encoder family: `x264`\|`nvenc`\|`qsv`\|`amf`\|`auto`. Resolved against the live `obs_enum_encoder_types()` set; unavailable/unknown/null-create all fall back silently to `obs_x264`. Boot-fixed, no live swap (ADR 004 §3.1-3.2/§3.4). |
| `PULSAR_VIDEO_RATE_CONTROL` | `pulsar-frontend-stub` | `CBR` | `CBR`\|`VBR`\|`CQP`, only applied when a non-fallback encoder binds. |
| `PULSAR_VIDEO_PROFILE` | `pulsar-frontend-stub` | `high` | `baseline`\|`main`\|`high`. |
| `PULSAR_VIDEO_KEYINT_SEC` | `pulsar-frontend-stub` | `2` | Keyframe interval, `0..20` seconds. |
| `PULSAR_VIDEO_PRESET` | `pulsar-frontend-stub` | family-specific | Validated against the preset set for the resolved encoder family; unknown value ⇒ family default. |
| `PULSAR_AUDIO_BITRATE` | `pulsar-frontend-stub` | `160` (kbps) | `ffmpeg_aac` bitrate, `32..512`. Mutable live via `pulsar:SetVideoSettings` while the audio encoder is idle. |
| `PULSAR_DESKTOP_AUDIO_DEVICE_ID` | `pulsar-frontend-stub` | `"default"` | `wasapi_output_capture` device id (mixer channel 1). |
| `PULSAR_MIC_DEVICE_ID` | `pulsar-frontend-stub` | unset (source not created) | `wasapi_input_capture` device id (mixer channel 3) — opt-in, since mic devices are absent on CI/servers. |
| `PULSAR_PROCESS_AUDIO_NAME` | `pulsar-frontend-stub` | unset (source not created) | Executable name for `wasapi_process_output_capture` (mixer channel 2, per-process loopback). Requires Windows 10 19041+ / recent win-wasapi; tolerated as unavailable otherwise. |
| `PULSAR_RECORD_DIR` | `pulsar-frontend-stub` | `<cwd>/recordings` | Recording output directory, created lazily on first `recording_start`. Replay saves land here too. |
| `PULSAR_REPLAY_MAX_TIME_SEC` | `pulsar-frontend-stub` | `30` | Replay buffer depth in seconds, `10..300`. Out of range ⇒ warning + default. Boot-fixed. |
| `PULSAR_REPLAY_MAX_SIZE_MB` | `pulsar-frontend-stub` | `512` | Replay buffer RAM cap in MB, `16..8192`. Out of range ⇒ warning + default. Boot-fixed. |
| `PULSAR_STINGER_ASSET` | `pulsar-frontend-stub` | `<cwd>/../../data/pulsar/stinger-demo.webm` | Local path to the stinger media asset (never leaf/network-derived, ADR 003 Amendment 2 §A2.1). |
| `PULSAR_NATIVE_STINGER` | `pulsar-frontend-stub` | off | Truthy set `1`/`true`/`on`/`yes` (case-insensitive) enables the dormant OBS-native stinger compositing path (#67); default off means the M10 transition renders via Solar/CEF overlay and OBS only hard-cuts. Security invariant: env-only, never leaf-reachable (Bastion #76, ADR 003 §A4.5 R1′·R7). |
| `PULSAR_ADAPTIVE_BITRATE` | `pulsar-multi-stream` | enabled | Set to `off`/`0`/`false` to disable the adaptive bitrate worker at start. |
| `PULSAR_OUTPUT_VERIFY_MS` | `pulsar-websocket` | `250` (ms) | Upper bound of the post-action state poll behind the start/stop verification above (`0..2000`; out-of-range ⇒ default with a warning). Only the non-nominal paths ever reach it — an output that activates inside `obs_output_start` settles on the first read. |

## Adaptive bitrate worker — operational notes

The worker samples `obs_output_get_total_frames` and
`obs_output_get_frames_dropped` across all active destinations on a 2 s
interval.

- **Drop detected** (`drop_ratio > threshold`): scale bitrate down,
  emit `pulsar:BitrateAdjusted` with `reason="drops"`.
- **Stable window** (15 consecutive ticks without drops): attempt a
  climb back toward `target_kbps`, emit
  `pulsar:BitrateAdjusted` with `reason="recovery"`.
- **Floor**: 30 % of the target by default. The worker will not drop
  below this — better drops than encoding garbage.
- **Disable**: `pulsar:SetAdaptiveEnabled { enabled: false }` pauses
  sampling. The encoder stays at whatever bitrate the worker last
  applied. Re-enabling resets `stable_ticks` to 0 so the loop
  re-warms before any climb.

The encoder's *configured* bitrate (`current_kbps`) and the *target*
the worker tries to maintain (`target_kbps`) drift apart during a
sustained drop event; they re-converge during recovery.

## Authentication details

- v5 challenge/response — `sha256(base64(sha256(password + salt)) + challenge)`.
- Pulsar binds `127.0.0.1` and `::1` only. Connections from non-loopback
  addresses are refused at the socket layer.
- The same `password` is honoured across reconnects within a single
  Pulsar lifetime. After a `pulsar.exe` restart the password rotates
  (unless pinned via `PULSAR_PASSWORD`).

## Stability guarantees

- **v5 baseline** is considered stable across Pulsar minor versions.
  Breaking v5 changes happen only when upstream obs-websocket itself
  breaks them.
- **`pulsar:*` extensions** follow Pulsar's semver. Major bumps may
  rename or remove `pulsar:*` requests; minor bumps add new ones;
  patch bumps fix bugs without behavioural change.
- The `PULSAR_READY` sentinel format is stable and will not change
  without a major bump.
- Boot env var names (`PULSAR_*`) are stable and will not change
  without a major bump.
