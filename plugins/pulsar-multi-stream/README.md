# pulsar-multi-stream

First-class multi-destination streaming for Pulsar.

OBS treats each output (Twitch, YouTube, custom RTMP, file recording)
as a separate libobs `obs_output_t` with its own service, encoder, and
lifecycle. Multiple destinations require either the multi-rtmp plugin
or manual orchestration. Pulsar elevates destinations to a first-class
concept managed by this plugin.

## Status

Placeholder. Implementation lands in Phase 4 (after the websocket and
headless plugins are functional).

## Responsibility

- Register destination kinds: `twitch`, `youtube`, `rtmp_custom`,
  `vod_local`. Each kind owns its config schema, OAuth handshake (where
  applicable), and the libobs output / encoder it instantiates.
- Share a single video / audio encoder across RTMP destinations
  (encode-once-fan-out-N model). VOD recording reuses the same
  encoder by default; v2 may add a separate high-bitrate record
  encoder for edit masters.
- Expose destination state as `pulsar:*` websocket events:
  `DestinationCreated`, `DestinationStateChanged`,
  `DestinationDropped`, `DestinationReconnected`.
- Per-destination retry policy (RTMP failure on Twitch must not kill
  YouTube).

## Why a plugin, not a patch

Multi-destination logic is additive: it composes existing libobs
outputs without modifying their behaviour. A plugin keeps it isolated,
upstream-rebase-safe, and conditionally compilable for embedders that
do not need it.
