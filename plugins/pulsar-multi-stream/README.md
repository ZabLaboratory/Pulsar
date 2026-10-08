# pulsar-multi-stream

First-class multi-destination streaming for Pulsar.

OBS treats each output (Twitch, YouTube, custom RTMP, file recording) as
a separate libobs `obs_output_t` with its own service, encoder, and
lifecycle. Multiple destinations require either the multi-rtmp plugin
or manual orchestration. Pulsar elevates destinations to a first-class
concept managed by this plugin.

## Architecture

This plugin is a **DLL loaded by libobs** (it lives under
`obs-plugins/64bit/`, not linked into `pulsar.exe`). It registers an
obs-websocket vendor namespace `"pulsar"` and exposes destinations as
vendor requests that v5 clients invoke through `CallVendorRequest`.

Encoders are **not duplicated** per-destination. The plugin reuses the
video + audio encoders that `pulsar-frontend-stub` already attached to
the streaming output (`PulsarStream`) and binds them to one
`obs_output_t` per destination — `rtmp_output` for RTMP destinations,
`ffmpeg_muxer` for `vod_local`. Encode-once / fan-out-N.

The plugin is **additive**: it does not take over the legacy single-
output `StartStream` / `StartRecord` v5 surface. Stream Deck, Companion,
Streamer.bot, etc. keep working unmodified. Multi-destination control
goes through the vendor namespace.

## Vendor API (namespace `"pulsar"`)

| Request | Inputs | Outputs |
|---|---|---|
| `GetDestinations` | — | `destinations: [{id, name, kind, url, enabled, active}, ...]` |
| `CreateDestination` | `name, kind ("rtmp_custom" \| "vod_local" \| "twitch" \| "youtube"), url, key?` | `id` (or `error`) |
| `RemoveDestination` | `id` | `removed: bool` |
| `StartDestination` | `id` | `started: bool, error?: string` |
| `StopDestination` | `id` | `stopped: bool` |
| `StartAllDestinations` | — | `ok: bool` |
| `StopAllDestinations` | — | `ok: bool` |
| `GetVideoSettings` | — | `fps, width, height, video_bitrate, video_rate_control, video_keyint_sec, audio_bitrate` |
| `SetVideoSettings` | `video_bitrate?, audio_bitrate?` | `changed: bool, video_bitrate?, audio_bitrate?` (or `error`) |
| `GetCapabilities` | — | Runtime manifest, inventories and mutation regimes; presence is not operational proof. |
| `GetAudioTracks` / `MeasureAudioTrackFlow` | See protocol | Track/output mapping and actual flow observations. |
| `GetMonitoringDeviceList` / `SetMonitoringDevice` | Optional `device_id` on write | Enumerated monitoring devices and effective readback. |
| `GetDiagnostics` / `StopLogFileWrite` | See protocol | Bounded diagnostics and diagnostic log-sink control under their admission rules. |
| `GetAdaptiveState` | — | `enabled, target_kbps, current_kbps, floor_kbps, stable_ticks, adjustments_total, last_delta_total, last_delta_dropped, last_drop_ratio, samples` |
| `SetAdaptiveEnabled` | `enabled` | `enabled` |
| `GetProgramAudioRoute` | — | Explicit `program-common` / `ProgramAudio` identity, output/source read-back, encoder-fed mixer tracks and monotone PTS counters/history. |

### Vendor events

| Event | Payload | When |
|---|---|---|
| `BitrateAdjusted` | `bitrate, target, floor, reason ("drops" \| "recovery"), drop_ratio` | Each time the adaptive loop applies a new video bitrate. |

`SetVideoSettings` accepts only encoder-level mutations live: `video_bitrate` updates instantly via `obs_encoder_update`; `audio_bitrate` only updates when no output is pulling from the audio encoder (ffmpeg_aac doesn't support mid-stream re-init). `fps` / `width` / `height` are pinned at boot via `PULSAR_FPS` / `PULSAR_RESOLUTION` env vars; trying to set them through this request is rejected with a typed error.

### Common Program audio route (r2, issue #245)

`GetProgramAudioRoute` reports the one process-wide libobs `audio_t` captured
as `program-common` / `ProgramAudio`. Audio-capable frontend outputs must report
the same `audio_identity`; their bound mixer indexes receive persistent raw
callbacks that expose actual frames and monotone PTS evidence. The Program and
Preview return surfaces are video-only and are explicitly marked
`audio_supported=false`. A video Cut swaps only video roots and never changes
the common audio route. Preview audio and AFV are unsupported in r2 and are not
inferred from a Preview scene or output slot. `sources` contains only
audio-capable main-canvas source channels (1..63 in the current libobs build);
channel 0 is the mutable dual-lane video root and is intentionally omitted.

The complete request/event fields, including `OutputAttemptSettled` and
`OutputFailed`, are in [PROTOCOL.md](../../docs/PROTOCOL.md). A start response
is not a remote-platform live guarantee. Observe effective state and later
failure events; the registry does not expose an implemented
`DestinationStateChanged` contract merely because an old roadmap named it.

### Kinds

| Kind | `url` field | `key` field |
|---|---|---|
| `rtmp_custom` | RTMP server URL (`rtmp://...` or `rtmps://...`, validated) | required, non-empty stream key |
| `vod_local` | output file path (parent dir is mkdir-p'd) | unused |
| `youtube` | ignored; pinned `rtmps://a.rtmps.youtube.com:443/live2` | required, non-empty YouTube stream key; no OAuth flow |
| `twitch` | ignored on input — Pulsar pins the URL to `rtmps://ingest.global-contribute.live-video.net/app/` (TLS, so the stream key never travels in cleartext) and surfaces the pinned value in `GetDestinations` | required, non-empty Twitch stream key |

Output paths for `vod_local` are NOT auto-timestamped — supply a fully
resolved path; the client (Prism) is responsible for naming.

`RemoveDestination` while a destination is active is safe: the registry
calls `obs_output_stop` and polls until inactive (with a `force_stop`
fallback after ~1 s) before releasing handles, so the MP4 / RTMP
session can finalize before release. Inspect failures/timeouts rather than
assuming the force fallback always produces an intact archive.

## Current scope and limits

The registry ships all four destination kinds, shared encoder fan-out,
bitrate controls, adaptive sampling, capability/audio/monitoring readback and
diagnostics. It does not manage provider accounts/OAuth or durable
application-level destination persistence across runtime restarts.

Adaptive bitrate changes a shared encoder, not one independent quality tier
per destination. Inspect per-destination activity after a bulk action;
`StartAllDestinations` returning does not prove every output became live.
Encoder family/resolution/fps remain boot-fixed.

Finalize outputs before process termination. The removal path waits with a
bounded force fallback; a fallback is not an unconditional successful MP4
finalization guarantee.

## Validation

`scripts/probe-multi-stream.py` exercises the round-trip: create a
`vod_local` destination → start → wait → stop → assert the MP4 file
exists, then create an `rtmp_custom` to a dead address → start →
expect a clean failure (no crash) → remove → list-empty.
