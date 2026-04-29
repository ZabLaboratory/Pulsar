# pulsar-websocket

Pulsar's WebSocket control surface. Fork of `obs-websocket` v5 with
the baseline protocol preserved (existing tooling — Stream Deck,
Streamer.bot, Aitum — works against Pulsar without modification) plus
Pulsar-specific extensions in the `pulsar:*` namespace.

## Status

Placeholder. Forking and building lands in Phase 2.

## Protocol

- **Baseline:** obs-websocket v5 — see `../../docs/PROTOCOL.md`.
- **Pulsar extensions:** documented in `../../docs/PROTOCOL.md` under
  the `pulsar:*` namespace. Designed so a strict v5 client never sees
  unexpected messages.

## Auth

- Session JWT issued by Pulsar at startup, given to the spawning
  process (Prism) over stdout. Clients present it via the v5
  authentication challenge flow.
- Authentication failure on a non-loopback connection terminates the
  socket. Pulsar binds loopback-only by default.

## Why fork instead of using obs-websocket as-is

Pulsar wants the websocket plugin to manage Pulsar-specific state
(multi-destination outputs, scene streaming, session lifecycle). That
state belongs alongside the v5 dispatch table, not bolted on as a
sidecar plugin. The fork stays close enough to upstream that v5
features land via rebase rather than reimplementation.
