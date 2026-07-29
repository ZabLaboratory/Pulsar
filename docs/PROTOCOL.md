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
| Transport | WebSocket over TCP, **loopback (`127.0.0.1`) only** — the server binds that address and nothing else, so it is unreachable from the LAN. Widened only by an explicit `PULSAR_WS_BIND` (see the env table). |
| Port | `PULSAR_PORT` env var at spawn (default `4455`). The chosen port is echoed in the `PULSAR_READY` stdout sentinel. |
| Password | `PULSAR_PASSWORD` env var at spawn. Unset = Pulsar generates a fresh 22-char URL-safe random string and exposes it in the same sentinel. The seeded values are written to `<cwd>/obs-websocket/config.json` before `obs_module_load`, so a stale on-disk password from a prior session is never trusted. |
| Auth | Standard obs-websocket v5 challenge/response (sha256 of password + salt + challenge). A plain v5 client implementation handles the handshake unchanged. |
| Identify | `rpcVersion: 1`. `eventSubscriptions` defaults to `0x7FF` (all baseline categories). |

**Third-party clients: use `127.0.0.1`, not `localhost`.** The bind is a
single address at a time (`Config::BindAddress`, one value — never both
stacks at once), and that address is IPv4 loopback only, never `::1`. On
Windows, a client that resolves `localhost` may get the `::1` (IPv6)
candidate first and fail to connect, since nothing is listening there.
Prism is unaffected — it always targets `127.0.0.1` explicitly (see the
`PULSAR_READY` sentinel below) — but a third-party integration (Stream
Deck, Companion, Streamer.bot) configured with the hostname `localhost`
should be pointed at `127.0.0.1` explicitly instead. `PULSAR_WS_BIND`
(env table below) overrides the bound address if a different reach is
ever needed.

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
| Record | `StartRecord`, `StopRecord`, `PauseRecord`, `ResumeRecord`, `GetRecordStatus`, `SplitRecordFile`, `CreateRecordChapter` (see *Recording — manual split and chapter markers*) |
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

   **The `rtmp_common` service type is not available on this path.**
   `SetStreamServiceSettings` with `streamServiceType: "rtmp_common"` is
   refused (`InvalidRequestField`, 400) whatever platform the settings
   name — Twitch, YouTube, Kick, Trovo, or none at all — and so is a
   `StartStream` that would reach such a service. An `rtmp_common`
   service resolves its ingest from a service list downloaded at runtime,
   which Pulsar does not control, which carries **cleartext** `rtmp://`
   entries, and which falls back to the cleartext
   `rtmp://live.twitch.tv/app` for Twitch when the list is absent (first
   run, cold cache, offline) — the stream key would go on the wire
   unencrypted. Push an `rtmp_custom` service with an explicit server
   instead; for Twitch, send with `pulsar:StartDestination`, whose ingest
   is an `rtmps://` constant pinned at compile time (`static_assert`,
   `pulsar-multi-stream`).

   The **boot placeholder is neutral**: before any configuration the
   stream service is an empty `rtmp_custom` naming no platform, so
   `GetStreamServiceSettings` reports `rtmp_custom` with no server and no
   key, and `StartStream` fails with `OutputNotRunning` for want of a
   destination — not because a platform was rebutted.

   **The destination is validated up front**, with the same rules
   `pulsar:StartDestination` applies: the resolved server must be an
   `rtmp://` or `rtmps://` URL and the stream key must be non-empty, or
   `SetStreamServiceSettings` answers `InvalidRequestField` (400) with
   the cause named and **applies nothing**. Same-type calls still merge
   onto the current settings (unchanged), and it is the merged result
   that is validated — a partial update cannot inherit its way past the
   rules.
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
5. **`SetInputAudioTracks` is judged by the output** (`#157`). Enabling a
   track that no encoder of the streaming output consumes now **fails**
   with `InvalidResourceState` (604) and **applies nothing**; the
   `comment` names the tracks requested and the tracks actually bound,
   both read off libobs (`… Requested: 4. Bound to the output: 1 (read
   from the streaming output's encoder slots). Nothing was changed.`).
   It used to answer success.

   Do not verify this one against the input. `GetInputAudioTracks` reads
   the mixer bits of the source, which libobs writes whatever any output
   carries — and it defaults every fresh source to *all six tracks
   enabled*. So the input reports `true` for tracks that reach no
   encoder; the oracle is `capabilities.audio_tracks.bound`, the same
   walk of the output's encoder slots the request performs.

   Only **enabling** is judged: `{"4": false}` succeeds — turning audio
   off is honest whatever the output carries. And when no streaming
   output exists at all nothing is refused, because nothing was read.

   Since `1.6.0` the output CAN carry several tracks (`#168`, see
   *Multi-track audio* below), so this refusal is no longer a statement
   about Pulsar's ceiling — it is a statement about **this spawn's**
   routing. A default spawn still binds one encoder on track 1
   (`audio_tracks.bound: 1`, `tracks: [1]`) and refuses tracks 2..6
   exactly as before.

   One consequence for anyone reading the output's slots directly: **the
   slot index is not the track number.** OBS packs the selected tracks
   into consecutive slots, so an output carrying tracks 1 and 3 binds
   them at slots 0 and 1. The track is the encoder's own mixer index;
   inferring it from the slot would refuse track 3 and accept track 2 on
   exactly that output — #157's mistake, one level down.

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
| `SetVideoSettings` | Mutate encoder bitrates **live**. Setting `fps` / `width` / `height` is rejected — those require boot-time env vars (`PULSAR_FPS`, `PULSAR_RESOLUTION`). `video_encoder` / `video_preset` / `video_profile` are likewise rejected: encoder identity is boot-fixed via `PULSAR_VIDEO_ENCODER` (no live swap — see ADR 004 §3.4). Audio bitrate is only applied while the audio encoders are idle, and is applied to **every** audio encoder the streaming output carries (`audio_tracks_updated` says how many) — patching slot 0 alone would report a whole write and perform a partial one. | `video_bitrate?`, `audio_bitrate?` | `changed: bool`, `video_bitrate?`, `audio_bitrate?`, `audio_tracks_updated?`, `error?` |
| `GetCapabilities` | **Capability manifest** — the authoritative statement of what this Pulsar can do (Prism ADR 027 §3.1/§3.2). Enumerates the encoder families this build exposes (via `obs_enum_encoder_types()`, mapped to Pulsar short names) plus the bitrate windows, each with its application regime. Declares, per enumerated family, its presets / profiles / rate-controls / keyint and bitrate windows (`capabilities.encoder_families`, all `boot-fixed`), the audio block (monitoring, tracks, sample rate, speaker layout), the presence-only inventories (registered filters, source kinds, destination kinds), the effective video colorimetry, and the graphics adapters + admitted output scales (`capabilities.graphics_adapters` / `output_scales`, ADR 027 Amendment 1). Off-air detection; `active_encoder` is the family bound to the streaming output. See the manifest, encoder-block and list-encoding notes below. | — | `version: number`, `capabilities: { [name]: CapabilityEntry }`, `encoders: {value: string}[]`, `active_encoder: string`, `video_bitrate?: {min, max}`, `audio_bitrate?: {value: number}[]`, `error?` |
| `GetAudioTracks` | What each output actually carries: per output (`stream` / `record` / `replay`), the encoder bound at each slot, the **track** it pulls from (`obs_encoder_get_mixer_index() + 1`, *not* the slot index), its name, codec, bitrate, `active` flag and `encoded_frames`. An output that does not exist is **absent** from the list, never an empty entry. | — | `count: number`, `outputs: { output, slots: { slot, track, encoder, codec?, bitrate, active, encoded_frames }[] }[]`, `error?` |
| `MeasureAudioTrackFlow` | Measure, for a bounded window, the audio that actually **flows** on each of the six libobs mixes — i.e. what each track's encoder is fed. Installs a raw audio callback on every mix for `duration_ms`, then removes it (a permanently connected callback would force libobs to mix all six buses for the process's lifetime). `encoder_bound` says whether the streaming output carries an encoder for that track, and is **omitted** off-air rather than guessed. This is the only read that distinguishes *routed* from *consumed*: see the note below. | `duration_ms?` (50..2000, default 300) | `duration_ms`, `tracks: { track, frames, peak, encoder_bound? }[]`, `error?` |
| `GetAdaptiveState` | Snapshot the bitrate adaptation worker. | — | `enabled`, `target_kbps`, `current_kbps`, `floor_kbps`, `stable_ticks`, `adjustments_total`, `last_delta_total`, `last_delta_dropped`, `last_drop_ratio`, `error?` |
| `SetAdaptiveEnabled` | Toggle the worker. Disabling pauses sampling; the encoder bitrate is left at whatever value the worker last applied. Re-enabling resets `stable_ticks` to 0 so the loop re-warms before any climb attempt. | `enabled` | `enabled: bool`, `error?` |

#### The capability manifest (`GetCapabilities`)

`GetCapabilities` is the **single authoritative statement** of what the running
Pulsar can do. Two structural rules hold for every entry — including the
encoder / audio / inventory / video blocks that land later:

1. **Values are read, never decreed.** Every number comes from libobs (encoder
   properties). A value Pulsar cannot read is **declared absent** — the key is
   simply omitted — and is *never* replaced by a plausible constant. Absence is
   a positive answer: the consumer keeps its own static bound.
2. **Every entry declares its application regime** next to its values, so the
   consumer derives its apply-class instead of decreeing one.

| Regime | Meaning |
|---|---|
| `live` | settable on a running Pulsar |
| `boot-fixed` | fixed at boot (env vars), refused hot |
| `read-only` | observable, never settable |

```jsonc
{
  "version": 1,
  // Pre-manifest top-level keys, kept verbatim for backward compatibility.
  "encoders": [{ "value": "x264" }],
  "active_encoder": "x264",
  "video_bitrate": { "min": 200, "max": 50000 },
  "audio_bitrate": [{ "value": 64 }, { "value": 96 }],
  // Regime-carrying entries, keyed by capability name.
  "capabilities": {
    "encoders":       { "applicability": "boot-fixed", "values": [{ "value": "x264" }] },
    "active_encoder": { "applicability": "boot-fixed", "value": "x264" },
    "video_bitrate":  { "applicability": "live", "min": 200, "max": 50000, "step": 50 },
    "audio_bitrate":  { "applicability": "live", "min": 64, "max": 512, "step": 32,
                        "values": [{ "value": 64 }] },
    // Per-family encoder detail (ADR 027 §3.3 bloc 1) — see below.
    "encoder_families": { "applicability": "boot-fixed", "values": [ /* … */ ] },
    // Inventories -- presence only, no bound, ever (see below).
    "filters":           { "applicability": "live",
                           "values": [{ "value": "color_filter_v2" }] },
    "source_kinds":      { "applicability": "live",
                           "values": [{ "value": "window_capture" }] },
    "destination_kinds": { "applicability": "live",
                           "values": [{ "value": "rtmp_custom" }, { "value": "twitch" }] },
    // Effective video colorimetry -- observable, never settable.
    "video_colorimetry": { "applicability": "read-only", "value": "709",
                           "range": "Partial", "format": "NV12" },
    // Audio block (ADR 027 §3.3 bloc 2).
    "audio_monitoring":    { "applicability": "read-only", "available": true,
                             "device_bound": false },
    "audio_tracks":        { "applicability": "read-only", "count": 6, "bound": 1,
                             "tracks": [{ "value": 1 }] },
    "audio_sample_rate":   { "applicability": "read-only", "hz": 48000 },
    "audio_speaker_layout":{ "applicability": "read-only", "layout": "stereo",
                             "channels": 2 },
    // Adapters and scales (ADR 027 Amendment 1) -- see below.
    "graphics_adapters": { "applicability": "read-only", "active_index": 0,
                           "values": [{ "value": "NVIDIA GeForce RTX 4070", "index": 0 }] },
    "output_scales":     { "applicability": "boot-fixed",
                           "canvas": { "width": 1920, "height": 1080 },
                           "values": [{ "value": "1920x1080", "width": 1920,
                                        "height": 1080, "scale": 1.0 }] }
  }
}
```

Compatibility contract, in both directions:

- **`version`** is bumped only on a *structural* change. Adding an entry under
  `capabilities` is additive and does **not** bump it.
- A client that does not know `capabilities` ignores it and keeps reading the
  top-level keys. A client that does must tolerate entries — and regime strings
  — it has never heard of.
- A **missing block** leaves the consumer's static bound intact; it is not an
  error and must not be read as `read-only`.
- The published bitrate windows are the **intersection** of what the encoder
  advertises and what Pulsar's own setter accepts (`SetVideoSettings`:
  `200..50000` kbps video, `32..512` kbps audio). The manifest therefore can
  never announce a value the setter would reject — it may only narrow.

##### Encoder entries (ADR 027 §3.3 bloc 1) — `capabilities.encoder_families`

What each encoder family this build enumerates actually offers. The **whole
block is `boot-fixed`**: preset, profile, rate-control and keyint are chosen by
`PULSAR_VIDEO_*` at spawn and `SetVideoSettings` refuses them hot. That is the
fact, not a limitation waiting to be lifted (ADR 027 §3.5).

```jsonc
"encoder_families": {
  "applicability": "boot-fixed",
  "values": [
    {
      "value": "x264",                                  // whitelisted family short name
      "presets":       [{ "value": "veryfast" }],       // the family's preset knob
      "profiles":      [{ "value": "high" }],           // H.264 profiles
      "rate_controls": [{ "value": "CBR" }],
      "keyint_sec":    { "min": 0, "max": 20, "step": 1 },
      "bitrate":       { "min": 200, "max": 50000, "step": 50 }
    }
  ]
}
```

- **Every value is read from that family's libobs properties**, never listed in
  Pulsar's source: presets come from the family's own preset knob (`preset` for
  x264/AMF and the 31.0+ NVENC encoder, `preset2` on the pre-31.0 NVENC compat
  ids `jim_nvenc` / `ffmpeg_nvenc`, `target_usage` for QSV),
  profiles from `profile`, rate-controls from `rate_control`, and the two
  windows from the `keyint_sec` / `bitrate` int properties.
- **A family the binary does not register is absent from the list.** No entry is
  fabricated for an encoder this build was not compiled with, and a field the
  encoder does not advertise is omitted rather than defaulted.
- `profiles`, `rate_controls` and the two windows are **intersected** with what
  Pulsar's boot setter accepts (`PULSAR_VIDEO_PROFILE` ∈ {baseline, main,
  high}, `PULSAR_VIDEO_RATE_CONTROL` ∈ {CBR, VBR, CQP}, `PULSAR_VIDEO_KEYINT_SEC`
  0..20) — the manifest may only narrow. `presets` are **not** narrowed: the
  boot whitelist is a per-family table that the manifest deliberately does not
  mirror, so a preset listed here may still be normalised to the family default
  at spawn.
- The family bound to the streaming output is read from the **live encoder**;
  the others from their registered encoder id.

##### Audio entries (ADR 027 §3.3 bloc 2)

| Entry | Fields | Regime | Source |
|---|---|---|---|
| `audio_monitoring` | `available: bool`, `device_bound: bool`, `device_id?`, `device_name?` | `read-only` | `obs_audio_monitoring_available()`, `obs_get_audio_monitoring_device()` |
| `audio_tracks` | `count: number`, `bound?: number`, `tracks?: {value: number}[]` | `read-only` | `MAX_AUDIO_MIXES`; `bound` counts the streaming output's occupied slots and `tracks` names them, each read from its encoder's mixer index (#168) |
| `audio_sample_rate` | `hz: number` | `read-only` | `obs_get_audio_info()` |
| `audio_speaker_layout` | `layout: string`, `channels: number` | `read-only` | `obs_get_audio_info()` + `get_audio_channels()` |

`layout` is one of `mono`, `stereo`, `2.1`, `4.0`, `4.1`, `5.1`, `7.1`. A libobs
`SPEAKERS_UNKNOWN` yields **no** `audio_speaker_layout` entry at all — an unknown
layout is declared absent, not published as the string `"unknown"`.

Two points carry the whole block:

- **`available` and `device_bound` are always emitted**, `true` or `false`. A
  Pulsar with no monitoring device answers an explicit, readable `false`; it does
  not stay silent. `device_id` / `device_name` appear **only** when
  `device_bound` is `true`. A consumer must not read a *missing*
  `audio_monitoring` entry (a pre-#143 Pulsar) as `device_bound: false` — an
  absence is not a "no".
- **Monitoring is `read-only`, not `live`.** `live` requires the write *and* the
  read-back to be genuinely supported hot; Pulsar exposes **no** monitoring write
  path — `obs_set_audio_monitoring_device()` is never called anywhere in this
  tree — so nothing can bind a device today. Prism must not offer the headphone
  monitoring keys as settable. (libobs seeds its monitoring device with
  `"Default"` / `"default"` in `obs_init_audio()`; Pulsar treats that seed as
  *not bound*, since it is a placeholder no one chose.)

`bound` is omitted off-air, when no streaming output exists to read tracks from.

##### Inventories — presence, never permission (ADR 027 §3.3 block 3)

`filters`, `source_kinds` and `destination_kinds` answer **what exists in this
binary**, and nothing else. All three are enumerated from the running process —
`obs_enum_filter_types()`, `obs_enum_input_types()`, and Pulsar's own
`DestinationKind` walked through `kind_to_string()` and gated on the obs output
type that serves it (`rtmp_output` / `ffmpeg_muxer`) being registered. There is
no hard-coded list behind any of them.

- **No filter property bound is ever emitted here.** Which filter settings may
  be written, and between which values, stays owned by the consumer's closed
  whitelist (Prism ADR 023 §3.3, under its own security clearance). Deriving a
  bound from this inventory would void that control — it is the one thing
  ADR 027 §3.1 forbids outright.
- **Destination kinds do not alter anyone's dispatch.** The list is informative;
  a `kind` the consumer does not know stays ignorable and is never routed.
- `source_kinds` enumerates **inputs**, not `obs_enum_source_types()` (which
  also yields filter and transition types — not things one creates as a source).
- The regime is `live`: a filter, a source or a destination of a declared kind
  can be created on a running Pulsar.
- An enumeration that yields nothing publishes **no entry at all**, so the
  consumer keeps its own static list instead of reading an empty array as
  "this binary registers none".

##### Video colorimetry (ADR 027 §3.3 block 4)

`video_colorimetry` reports the colourspace (`value`), range and pixel format
actually in force, read back from `obs_get_video_info()`. They are pinned once
at `obs_reset_video` (`plugins/pulsar-headless/main.cpp`) and the regime is
**`read-only`, not `boot-fixed`**: no request *and no env var* selects another
one, so promising a respawn knob would be a lie. For the same reason the entry
publishes **no list of "available" spaces** — nothing can select one, and
announcing a choice the binary cannot honour is exactly the decree §3.1 exists
to prevent. `range` and `format` carry libobs' own names (`Partial`/`Full`,
`NV12`, …), `value` the compact token `601` / `709` / `srgb` / `2100pq` /
`2100hlg`. A colourspace enum this build cannot name is declared absent.

##### Adapters and output scales (ADR 027 Amendment 1)

Two facts of the machine that a consumer used to decree: which graphics
adapters exist, and which output resolutions are admissible for the canvas this
Pulsar is running.

| Entry | Fields | Regime | Source |
|---|---|---|---|
| `graphics_adapters` | `values: {value: string, index: number}[]`, `active_index?` | `read-only` | `gs_enum_adapters()` inside `obs_enter_graphics()`; `active_index` from `obs_video_info.adapter` |
| `output_scales` | `canvas: {width, height}`, `values: {value: "<W>x<H>", width, height, scale?}[]` | `boot-fixed` | `obs_get_video_info()` — `base_*` for the canvas, `output_*` for the admitted resolution |

- **Adapters are enumerated by libobs**, never listed here: `gs_enum_adapters()`
  dispatches to the graphics subsystem's own enumeration (d3d11 on Windows).
  Each item carries the **index** `obs_video_info.adapter` is expressed in — a
  name without it could not be matched against `active_index`, which is the
  whole point of the entry (Prism pinned adapter `0` without ever asking).
  The regime is **`read-only`, not `boot-fixed`**, for the same reason as
  colorimetry: `pulsar-headless` pins `ovi.adapter` at `obs_reset_video` and
  exposes no env var, so `boot-fixed` would advertise a knob that does not
  exist. A walk that yields nothing publishes **no entry at all**.
- **Scales are derived from what this binary can establish**, not from a ladder
  of downscale factors. `reset_video()` sets base *and* output from the single
  `PULSAR_RESOLUTION` value, `SetVideoSettings` refuses `width`/`height` hot,
  and nothing calls `obs_encoder_set_scaled_size()` — so today the admitted set
  is exactly the one resolution libobs reports, and it will grow by itself the
  day a downscale path lands. Publishing a factor ladder would announce
  resolutions Pulsar cannot honour, which is what §3.1 forbids. The regime is
  `boot-fixed` because `PULSAR_RESOLUTION` genuinely selects it at spawn.
- `scale` is the ratio to the canvas, and is **omitted** when the two axes do
  not share one — a single ratio that holds on one axis only is not a scale.
  `value` is the `"<W>x<H>"` label; `width`/`height` are the load-bearing pair.
- **Restrictive, as everywhere else**: an adapter or a scale the consumer's own
  registry does not know stays non-selectable. The manifest may only narrow.

> **List encoding on the wire.** libobs vendor handlers can only serialise
> arrays as arrays of *objects* (`Utils::Json::ObsDataToJson` walks each
> `obs_data_array` item as an `obs_data`), never bare JSON scalar arrays. So
> `GetCapabilities` wraps each `encoders` / `audio_bitrate` /
> `encoder_families.*` element in a one-field `{ "value": … }` object (encoder
> family items carry their detail beside that `value`). `@clodocapeo/pulsar-client`'s
> `CapabilitiesNamespace.get()` unwraps these back into flat `string[]` /
> `number[]` before handing the typed `PulsarCapabilities` to callers.

### Multi-track audio (`#168`)

`PULSAR_AUDIO_TRACKS` creates *N* `ffmpeg_aac` encoders, encoder *i* bound to
libobs mixer index *i*. `PULSAR_{STREAM,RECORD,REPLAY}_AUDIO_TRACKS` then pick
which of those tracks each output carries — the antenna, the recording and the
replay buffer have no reason to agree, and nothing forces them to.

Everything defaults to the pre-`#168` wiring: **one** encoder, on track 1, at
slot 0 of the three outputs. A spawn that sets none of these variables is
byte-for-byte what it was.

```
PULSAR_AUDIO_TRACKS=3
PULSAR_AUDIO_BITRATE_2=96
PULSAR_STREAM_AUDIO_TRACKS=1,3      # slot 0 -> track 1, slot 1 -> track 3
PULSAR_RECORD_AUDIO_TRACKS=1,2,3
PULSAR_REPLAY_AUDIO_TRACKS=2
```

Two facts a consumer must not conflate:

- **A track is bound** — an encoder for it sits in one of the output's slots.
  `GetAudioTracks` and `capabilities.audio_tracks.tracks` answer that, and
  `SetInputAudioTracks` judges an enable request against it (`#157`).
- **A track is fed** — an input's audio actually reaches that mix. Nothing on
  the input side can establish this: `obs_source_set_audio_mixers()` writes the
  bit whatever anything downstream carries, and libobs hands every fresh source
  `audio_mixers = 0xFF`, so `GetInputAudioTracks` reports tracks as enabled that
  reach no encoder at all. `MeasureAudioTrackFlow` is the read that answers it,
  taken on the very bus the encoder is attached to — an encoder created with
  `obs_audio_encoder_create(…, mixer_idx, …)` and the probe's raw callback are
  both registered as inputs of `obs->audio.mixes[mixer_idx]`.

A track can be bound and fed nothing (no input routed to it), or fed and bound
to nothing (an input's default `0xFF` mixer mask on a spawn that binds one
encoder). `MeasureAudioTrackFlow` reports both halves side by side: `peak` /
`frames` for the flow, `encoder_bound` for the binding.

### Destination kinds

`CreateDestination` accepts one of four kinds. Each has its own
validation and lifecycle.

| Kind | URL | Key | Output type | Notes |
|---|---|---|---|---|
| `rtmp_custom` | required, `rtmp://` or `rtmps://` | required | `rtmp_output` | Generic RTMP — your private ingest, a co-host's RTMP, an OBS-compatible CDN. |
| `vod_local` | required, file path with `.mp4` extension | unused | `ffmpeg_muxer` | Local MP4 archive. Path is created if missing; existing file is overwritten. |
| `twitch` | **server-pinned** (Pulsar picks the closest Twitch ingest) | required | `rtmp_output` | The client's `url` field, if provided, is ignored. The server resolves and pins a Twitch ingest URL on `CreateDestination`; the resulting URL is what `GetDestinations[i].url` shows. |
| `youtube` | **server-pinned** (YouTube's primary RTMPS ingest) | required | `rtmp_output` | Same contract as `twitch`: the client's `url` is never read, the server pins its own ingest. |

Named-platform kinds (`twitch`, `youtube`) are pinned, not merely
defaulted: the stream key is a bearer credential for an account Pulsar
does not own, so it may only ever be handed to an ingest this binary
holds as a constant — never to a URL that arrived over the wire (ADR 010
§3.3 R1). Both pinned URLs are `rtmps://`, enforced at compile time.
A caller that wants to choose its own server uses `rtmp_custom`, whose
key is theirs to give away.

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
| `SetCaptureSource` | Replace the active capture source with a fresh `browser_source` on the current frontend scene, removing any previously-installed Pulsar-managed capture item (`PulsarCapture` from `pulsar-frontend-stub`, or a prior `PulsarSceneSource`) **from every scene**, not only the current one. See *Browser sources — control level and lifecycle* below. | `kind` (only `"browser_source"` supported), `url`, `width?` (default `1920`), `height?` (default `1080`), `fps?` (default `60`), `reroute_audio?` (default `false`), `css?` | `kind`, `url`, `width`, `height`, `fps`, `reroute_audio`, `removed_prior: int`, `error?` |
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

## Browser sources — control level and lifecycle

Normative. Issue #158 / ADR Prism 028 §3.2. Applies to **every** browser
source Pulsar creates, whichever path created it.

### Control level — pinned to `None`, always

A page loaded in a browser source runs **inside the broadcast process**. Left
alone, obs-browser hands it a `window.obsstudio` object whose reach is set by
`webpage_control_level`, and upstream's default (`ReadObs`) is already enough
for that page to read this process's **streaming / recording / replay-buffer /
virtual-cam status**. One level up (`ReadUser`) it reads the **scene list** and
the **current scene**; at `Advanced` it **switches the program scene**.

Pulsar loads third-party pages by design — partner overlays, sponsor widgets,
Solar compositions built from authored scenes. So:

- **`webpage_control_level` is pinned to `0` (`ControlLevel::None`) on every
  path that can hand obs-browser a settings object**, explicitly, never
  inherited:
  - `pulsar-scene:SetCaptureSource` (`pulsar-scene-source/src/plugin-main.cpp`),
  - the v5 `CreateInput` request (`pulsar-websocket`, `Obs_ActionHelper.cpp`),
  - the v5 `SetInputSettings` request, **both** `overlay=true`
    (`obs_source_update`) and `overlay=false` (`obs_source_reset_settings`).
    Creation is not the whole surface: this request re-writes the very key the
    creation pin set, on a source that is already live, and the `overlay=false`
    branch clears the user settings before applying. Pinning only at creation
    would be self-cancelling.
- All three go through one function,
  `Utils::Obs::ActionHelper::PinBrowserControlLevel` — one policy, one
  implementation.
- On the v5 paths the pin **overrides the request**. A request carrying
  `webpage_control_level` is logged and pinned back to `None` anyway. Nothing
  in Zab reads `window.obsstudio`, so there is no named need to honour; raising
  the level for one would be a reviewed code change here, not a wire field.
  The threat this answers is not a hostile client — a client on that socket
  already starts streams — but a **settings blob nobody chose**: a scene
  collection imported into Prism, an overlay template copied from an OBS
  profile. That blob reaches `SetInputSettings` as easily as `CreateInput`.
- `DEFAULT_CONTROL_LEVEL` in `pulsar-browser` is `ControlLevel::None` too. That
  default is a **floor**, not the mechanism: it covers the one case with no
  creation call at all — a scene collection loaded from disk whose stored
  settings predate this rule.
- `None` still answers `getControlLevel()`, which returns `0`. A page can
  therefore *discover* it is sandboxed instead of hanging on a callback that
  never fires. Every other `window.obsstudio` getter answers `null`.

Gates: `scripts/check-webpage-control-level.py` (lint job — fails on any
creation *or* settings-update path that does not pin) and
`scripts/probe-webpage-control-level.py`
(offline probe suite — asserts, from inside a real CEF page, that the level is
`None` and that `getStatus` / `getCurrentScene` / `getScenes` return nothing).

### Lifecycle — kept while active, destroyed on swap

A browser source that survives keeps its **JS state**: timers, WebSocket
connections, accumulated DOM, anything the page chose to hold. The rule is
therefore stated in both directions.

**Kept alive while it is the active capture source, across program-scene
changes.** The managed source is created with `shutdown = false` and
`restart_when_active = false`. This is deliberate: Pulsar is scene-agnostic and
composes scene changes *inside* the page (Solar), so a program-scene change
must not tear CEF down — doing so would blank the antenna on every cut and
restart the page's animations. A page that is on air stays loaded.

**Destroyed, never parked, when the capture source is swapped.** Every
`SetCaptureSource` sweeps the Pulsar-managed capture items (`PulsarSceneSource`,
`PulsarCapture`, and libobs's de-dup variants of those names) out of **every
scene libobs knows** — not just the current frontend scene — renaming each out
of the canonical name synchronously before removing its scene item. Once no
scene references it, the source's refcount reaches zero, libobs frees it and
the CEF browser goes with it.

The "every scene" part is the point. Sweeping only the current scene left a
page stranded on a scene the operator had since left: invisible in the program
mix, absent from `GetCaptureSource`, and still running — with its JS state,
its timers and its network access — for the rest of the session. Scenes beyond
the boot one are reachable over the v5 wire (`CreateScene`), so that was a
reachable state, not a hypothetical one.

There is no third regime: a Pulsar-managed browser source is either the active
capture source, or gone. `removed_prior` in the `SetCaptureSource` response is
how many items that swap retired.

## Recording — manual split and chapter markers

Until `#169` the two frontend entry points behind these requests
(`obs_frontend_recording_split_file`, `obs_frontend_recording_add_chapter`)
were stubbed to an unconditional `false`: the requests were registered
and advertised, but could only ever fail. They now **delegate to the
recording output's proc handler**, the way upstream does, and every
refusal carries its cause.

| Request | Behaviour |
|---|---|
| `SplitRecordFile` | Closes the current file and opens the next one, **without stopping the recording**. Success means the muxer armed the split; the switch itself lands on the next keyframe and is announced by the `RecordFileChanged` event, whose `newOutputPath` is the file now being written. |
| `CreateRecordChapter` | **Always refused on this build**, with the cause named — see below. |

**File splitting is enabled on the record output, thresholds are not.**
`recording_start` posts `split_file: true` alongside `directory` /
`format` / `extension`, because the muxer builds the *next* file name
from those three keys and not from `path`. Both automatic thresholds
(`max_time_sec`, `max_size_mb`) stay at `0`, so **nothing splits by
itself**: the only trigger is an explicit `SplitRecordFile`. Split files
follow the same `pulsar-<CCYY><MM><DD>-<hh><mm><ss>.mp4` template as the
first one, in `PULSAR_RECORD_DIR`.

**Chapter markers are not available.** Chapters exist only on OBS's
hybrid-MP4 output (`mp4_output`); Pulsar records through `ffmpeg_muxer`,
which does not expose the `add_chapter` procedure at all.
`CreateRecordChapter` therefore fails — `RequestProcessingFailed` — with
a `comment` **naming the output and the missing procedure**, not
upstream's generic "verify that the output being used supports chapter
markers". Changing the recording container to gain chapters is a
separate decision, not a defect of this path.

Both requests refuse rather than pretend, and the refusal is never mute
(ADR Prism 026 §3.2): the frontend publishes the cause through
`obs_output_set_last_error()` on the record output, and the request
reads it back verbatim into its `comment`. The refusals you can meet:

- no recording running — `OutputNotRunning` (the request's own guard);
- recording paused — the split/chapter would apply to a frozen timeline;
- the output does not expose the procedure (chapters, always; splitting,
  never on this build);
- file splitting disabled on the output (cannot happen while
  `recording_start` sets it, kept as a real refusal rather than an
  assumption).

`scripts/probe-record-split.py` (offline suite) holds the contract: it
proves the split **on disk** — two files, plus the `RecordFileChanged`
event — and that the chapter refusal names its cause.

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
| `PULSAR_WS_BIND` | `pulsar-websocket` | `127.0.0.1` | Address the obs-websocket server binds. The default keeps the v5 surface off the network entirely; any other value is an explicit decision and logs a warning when it is not a loopback address. Not persisted in `config.json`. |
| `PULSAR_FPS` | `pulsar-headless` | `60` | Output fps. Accepts only `24`/`30`/`48`/`60`/`120`; anything else is rejected with a warning. Boot-fixed (no live change). |
| `PULSAR_RESOLUTION` | `pulsar-headless` | `1920x1080` | Base/output resolution, format `<W>x<H>`, up to `7680x4320`. Boot-fixed. |
| `PULSAR_CAPTURE_WINDOW` | `pulsar-frontend-stub` | unset | `window_capture` target, format `<title>:<class>:<exe>`. Unset ⇒ source produces black frames (pipeline still encodes/records). Superseded per-scene by `pulsar-scene:SetCaptureSource` when used. |
| `PULSAR_VIDEO_BITRATE` | `pulsar-frontend-stub` | `6000` (kbps) | Boot video bitrate, `200..50000`. Mutable live afterwards via `pulsar:SetVideoSettings`. |
| `PULSAR_VIDEO_ENCODER` | `pulsar-frontend-stub` | `x264` | Encoder family: `x264`\|`nvenc`\|`qsv`\|`amf`\|`auto`. Resolved against the live `obs_enum_encoder_types()` set; unavailable/unknown/null-create all fall back silently to `obs_x264`. Boot-fixed, no live swap (ADR 004 §3.1-3.2/§3.4). |
| `PULSAR_VIDEO_RATE_CONTROL` | `pulsar-frontend-stub` | `CBR` | `CBR`\|`VBR`\|`CQP`, only applied when a non-fallback encoder binds. |
| `PULSAR_VIDEO_PROFILE` | `pulsar-frontend-stub` | `high` | `baseline`\|`main`\|`high`. |
| `PULSAR_VIDEO_KEYINT_SEC` | `pulsar-frontend-stub` | `2` | Keyframe interval, `0..20` seconds. |
| `PULSAR_VIDEO_PRESET` | `pulsar-frontend-stub` | family-specific | Validated against the preset set of the resolved encoder family — x264 `ultrafast..veryslow` (default `veryfast`), NVENC `p1..p7` (`p5`, written to `preset` on `obs_nvenc_h264_tex` but to **`preset2` on the compat ids `jim_nvenc` / `ffmpeg_nvenc`** — the property name is resolved per encoder id, not per family), **QSV `TU1..TU7` (`TU4`, written to the `target_usage` property, not `preset`)**, AMF `speed`/`balanced`/`quality` (`balanced`). Matched case-insensitively, applied in the canonical spelling `capabilities.encoder_families` publishes; unknown value ⇒ family default (logged). |
| `PULSAR_AUDIO_BITRATE` | `pulsar-frontend-stub` | `160` (kbps) | Default `ffmpeg_aac` bitrate for every track, `32..512`. Mutable live via `pulsar:SetVideoSettings` while the audio encoders are idle. |
| `PULSAR_AUDIO_TRACKS` | `pulsar-frontend-stub` | `1` | Number of audio encoders created, `1..6` (`MAX_AUDIO_MIXES`). Encoder *i* is bound to libobs mixer index *i*, i.e. track *i+1*. Out of range ⇒ warning + `1`. Boot-fixed. |
| `PULSAR_AUDIO_BITRATE_<n>` | `pulsar-frontend-stub` | `PULSAR_AUDIO_BITRATE` | Per-track bitrate override for track *n* (`1..PULSAR_AUDIO_TRACKS`), `32..512`. |
| `PULSAR_STREAM_AUDIO_TRACKS` | `pulsar-frontend-stub` | `1` | Tracks the **streaming** output carries, as a comma-separated list of 1-based track numbers (`1,3`). Unknown numbers and duplicates are dropped with a warning; an empty result falls back to `1`. The slot each track lands on is its rank in this list. |
| `PULSAR_RECORD_AUDIO_TRACKS` | `pulsar-frontend-stub` | `1` | Same, for the **recording** output. |
| `PULSAR_REPLAY_AUDIO_TRACKS` | `pulsar-frontend-stub` | `1` | Same, for the **replay buffer** output. |
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
- Pulsar binds `127.0.0.1` only. Connections from non-loopback addresses
  are refused at the socket layer — there is no listener for them. This
  is enforced in `plugins/pulsar-websocket` (`Config.h` `BindAddress`,
  `WebSocketServer::Start`) and asserted by
  `scripts/probe-loopback-bind.py`, which connects to this host's own LAN
  address on the same port and requires the connect to fail.
- `PULSAR_WS_BIND` overrides the address (e.g. `0.0.0.0`, `::1`, a
  specific interface). A non-loopback value logs a warning at start: the
  whole v5 surface, including the stream egress path, then sits behind
  the session password alone. Env only — never read from
  `config.json`, so a stale or tampered config cannot widen the bind.
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
