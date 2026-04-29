# pulsar-headless

Service-mode entry point for Pulsar.

Starts libobs without instantiating any Qt component, manages
lifecycle (init / scene graph load / shutdown), and exposes signals
that `pulsar-websocket` translates into protocol events.

## Status

Placeholder — implementation lands in Phase 2 of the build plan.

## Responsibility surface

- `obs_startup` / `obs_shutdown` lifecycle.
- Default video / audio backends selected for the host platform.
- Signal pipe-out so `pulsar-websocket` can subscribe to scene /
  source / output events without coupling to libobs internals.
- CLI entry: `pulsar --service [--port N] [--config path]`.

## Out of scope

- UI of any kind. If a debug surface is needed it lives in a separate
  optional plugin or as a developer-only build flag.
- Encoder selection logic — that is `pulsar-multi-stream`'s job.
- Authentication — handled inside `pulsar-websocket` at the protocol
  layer.
