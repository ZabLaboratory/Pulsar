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
| `CreateDestination` | `name, kind ("rtmp_custom" \| "vod_local" \| "twitch"), url, key?` | `id` (or `error`) |
| `RemoveDestination` | `id` | `removed: bool` |
| `StartDestination` | `id` | `started: bool, error?: string` |
| `StopDestination` | `id` | `stopped: bool` |
| `StartAllDestinations` | — | `ok: bool` |
| `StopAllDestinations` | — | `ok: bool` |
| `GetVideoSettings` | — | `fps, width, height, video_bitrate, video_rate_control, video_keyint_sec, audio_bitrate` |
| `SetVideoSettings` | `video_bitrate?, audio_bitrate?` | `changed: bool, video_bitrate?, audio_bitrate?` (or `error`) |
| `GetAdaptiveState` | — | `enabled, target_kbps, current_kbps, floor_kbps, stable_ticks, adjustments_total, last_delta_total, last_delta_dropped, last_drop_ratio, samples` |
| `SetAdaptiveEnabled` | `enabled` | `enabled` |

### Vendor events

| Event | Payload | When |
|---|---|---|
| `BitrateAdjusted` | `bitrate, target, floor, reason ("drops" \| "recovery"), drop_ratio` | Each time the adaptive loop applies a new video bitrate. |

`SetVideoSettings` accepts only encoder-level mutations live: `video_bitrate` updates instantly via `obs_encoder_update`; `audio_bitrate` only updates when no output is pulling from the audio encoder (ffmpeg_aac doesn't support mid-stream re-init). `fps` / `width` / `height` are pinned at boot via `PULSAR_FPS` / `PULSAR_RESOLUTION` env vars; trying to set them through this request is rejected with a typed error.

### Kinds

| Kind | `url` field | `key` field |
|---|---|---|
| `rtmp_custom` | RTMP server URL (`rtmp://...` or `rtmps://...`, validated) | required, non-empty stream key |
| `vod_local` | output file path (parent dir is mkdir-p'd) | unused |
| `twitch` | ignored on input — Pulsar pins the URL to `rtmps://ingest.global-contribute.live-video.net/app/` (TLS, so the stream key never travels in cleartext) and surfaces the pinned value in `GetDestinations` | required, non-empty Twitch stream key |

Output paths for `vod_local` are NOT auto-timestamped — supply a fully
resolved path; the client (Prism) is responsible for naming.

`RemoveDestination` while a destination is active is safe: the registry
calls `obs_output_stop` and polls until inactive (with a `force_stop`
fallback after ~1 s) before releasing handles, so the MP4 / RTMP
session is finalised gracefully.

## Phase scope

- **PR1 (Phase 7a-d)** — `rtmp_custom` + `vod_local`, registry, vendor API, encode sharing.
- **PR2 (Phase 7e)** — `twitch` kind alias, input validation, validated graceful remove-during-active.
- **PR3 (Phase 12a)** — `GetVideoSettings` / `SetVideoSettings` for live bitrate mutation.
- **PR4 (Phase 12b)** — adaptive bitrate worker (background thread sampling `obs_output_get_frames_dropped`, scaling within `[floor, target]`, emitting `pulsar:BitrateAdjusted`); `GetAdaptiveState` / `SetAdaptiveEnabled` for runtime control. `PULSAR_ADAPTIVE_BITRATE=off` opt-out at boot.
- **Later** — destination state events (`pulsar:DestinationStateChanged`), per-destination retry policy, persistence of destination configs, YouTube OAuth (Phase 8 deferred), YouTube kind.

## Validation

`scripts/probe-multi-stream.py` exercises the round-trip: create a
`vod_local` destination → start → wait → stop → assert the MP4 file
exists, then create an `rtmp_custom` to a dead address → start →
expect a clean failure (no crash) → remove → list-empty.
