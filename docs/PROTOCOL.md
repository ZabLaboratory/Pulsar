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
| Outputs | `GetOutputList`, `GetOutputStatus` (informational; multi-destination control goes through `pulsar:*`) |
| Vendor | `CallVendorRequest` (the transport for `pulsar:*`, see below) |

Two things to keep in mind when driving the baseline against Pulsar:

1. **`StartStream` ≠ go live.** The v5 `StartStream` request talks to
   the singleton `PulsarStream` rtmp_output created by
   `pulsar-frontend-stub`. It succeeds on the wire even when no
   streaming service URL is configured — the underlying
   `obs_output_start` declines silently. To actually go live through
   the v5 surface, configure a service via `SetStreamServiceSettings`
   first; or use the `pulsar:StartDestination` multi-stream API
   instead (recommended).
2. **`StartRecord` writes to `<cwd>/recordings/`** by default. Override
   with `PULSAR_RECORD_DIR` at spawn. Filenames are
   `pulsar-<YYYYMMDD-HHMMSS>.mp4`.

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
