# pulsar-headless

Service-mode entry point for Pulsar.

Builds `bin/64bit/pulsar.exe` for Windows x64. It starts libobs without an
OBS Studio window, but constructs a minimal Qt application required by
loaded components. It owns bootstrap, readiness and teardown; the frontend
component owns the production scene graph and encoder selection.

## Status

The service bootstrap resolves a validated `PULSAR_RUNTIME_INSTANCE_ID`
before Qt/libobs startup, creates a private runtime namespace and acquires
OS-backed identity and cwd leases. Defaults for WebSocket config, logs and
recordings are rooted below that namespace. On Windows, identity and alias
authority is held by canonical `Local`-session named mutexes aligned with the
DirectShow mapping namespace; retained files are metadata/diagnostics only.
`PULSAR_RUNTIME_ROOT` and `PULSAR_LEGACY_ALIAS_LEASE_ROOT` select caller-visible
state paths but cannot partition the authority. The compatibility DirectShow
aliases are protected by one singleton lease; non-holders use instance-specific
mappings and remain observable through `PULSAR_RUNTIME_INSTANCE`,
`PULSAR_RUNTIME_COLLISION` and `PULSAR_LEGACY_ALIAS` log records, including the
canonical authority and metadata identities.

On Windows, the cwd lease resolves the created runtime directory through a
directory handle and uses its volume serial plus file ID as the authority key.
Case variants, junctions/symlinks and available 8.3 aliases therefore contend
for the same physical directory lease. The handle is retained without delete
sharing while the lease is held; reparse-point, DACL or file-identity failures
are reported as hard startup errors rather than falling back to a lexical path.
After acquisition, the bootstrap uses the final path obtained from that same
handle for `PULSAR_RUNTIME_DIR` and process cwd. Drive-letter and UNC forms are
converted from the API's extended spelling before passing them to libobs/Qt;
unsupported forms fail closed rather than falling back to the caller's mutable
spelling. The caller's requested spelling is diagnostic only, so retargeting a
junction cannot move config, logs or recordings to another directory during
activation. `PULSAR_READY` is emitted only after obs-websocket is loaded and its
configured listener reports active.

## Responsibility surface

- `obs_startup` / `obs_shutdown` lifecycle.
- Runtime identity and crash-safe instance/legacy-alias leases.
- Default video / audio backends selected for the host platform.
- Signal pipe-out so `pulsar-websocket` can subscribe to scene /
  source / output events without coupling to libobs internals.
- Direct entry: `pulsar.exe`, configured through the documented environment.
  There is no supported `--service --port --config` CLI.
- WebSocket and browser pre-shutdown barriers before frontend/libobs teardown.
- Explicit inherited anonymous-event shutdown for the native redirected-stdio
  harness; the current Node bundle does not expose that control.

## Out of scope

- UI of any kind. If a debug surface is needed it lives in a separate
  optional plugin or as a developer-only build flag.
- Encoder selection logic — owned by `pulsar-frontend-stub`; multi-stream
  reads/mutates the supported settings through the shared runtime.
- Authentication — handled inside `pulsar-websocket` at the protocol
  layer.


## Boot and shutdown contract

The order is namespace/lease acquisition → minimal Qt/logging → libobs and
video/audio → frontend callback installation → protected WebSocket config →
module load/listener check → frontend production state → session/READY/idle
markers. READY does not prove that a media source has rendered or a remote
stream has reached live.

On graceful native shutdown, quiesce WebSocket, drain browser callbacks/tasks,
stop/release frontend outputs/sources, then stop libobs and release leases.
A failed barrier refuses unsafe continuation. Forceful process termination
does not exercise these same barriers and must not be described as an MP4
finalization guarantee.

[Architecture](../../docs/ARCHITECTURE.md),
[embedding lifecycle](../../docs/PRISM-EMBEDDING.md) and
[environment reference](../../docs/PROTOCOL.md) describe the public contract.
