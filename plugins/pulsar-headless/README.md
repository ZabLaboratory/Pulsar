# pulsar-headless

Service-mode entry point for Pulsar.

Starts libobs without instantiating any Qt component, manages
lifecycle (init / scene graph load / shutdown), and exposes signals
that `pulsar-websocket` translates into protocol events.

## Status

The service bootstrap resolves a validated `PULSAR_RUNTIME_INSTANCE_ID`
before Qt/libobs startup, creates a private runtime namespace and acquires
OS-backed identity and cwd leases. Defaults for WebSocket config, logs and
recordings are rooted below that namespace. The compatibility DirectShow
aliases are protected by a singleton lease; non-holders use instance-specific
mappings and remain observable through `PULSAR_RUNTIME_INSTANCE`,
`PULSAR_RUNTIME_COLLISION` and `PULSAR_LEGACY_ALIAS` log records.

## Responsibility surface

- `obs_startup` / `obs_shutdown` lifecycle.
- Runtime identity and crash-safe instance/legacy-alias leases.
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
