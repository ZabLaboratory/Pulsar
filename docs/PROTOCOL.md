# Pulsar — Protocol

Pulsar speaks an extended dialect of the obs-websocket v5 protocol.

## Baseline

The full v5 protocol is the reference spec. Existing tooling that
targets obs-websocket v5 (Stream Deck, Streamer.bot, Aitum, custom
dashboards) works against Pulsar without modification.

Spec: [obs-websocket v5 protocol reference](https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md)

## Connection

- **Transport:** WebSocket over TCP, loopback (`127.0.0.1`) only by
  default.
- **Port:** session-random, allocated by Pulsar at startup. The port
  is printed to stdout in a `PULSAR_READY <port> <jwt>` line so the
  spawning process can pick it up.
- **Auth:** v5 challenge / response with the session JWT as the
  password. Session JWT is single-use and rotated on every Pulsar
  start. A plain v5 client implementation handles the handshake
  unchanged.

## Pulsar extensions

Pulsar-specific requests and events live in the `pulsar:` prefix to
guarantee strict v5 clients never observe unknown payloads. Examples
(provisional names — finalised in Phase 4):

### Requests

| Request | Purpose |
|---|---|
| `pulsar:GetDestinations` | List configured destinations across all kinds. |
| `pulsar:CreateDestination` | Register a Twitch / YouTube / RTMP / VOD destination. |
| `pulsar:UpdateDestination` | Modify an existing destination's config. |
| `pulsar:DeleteDestination` | Remove a destination. |
| `pulsar:EnableDestination` | Mark a destination as participating in the next stream. |
| `pulsar:DisableDestination` | Exclude a destination without deleting it. |
| `pulsar:GetDestinationStatus` | Per-destination live state. |
| `pulsar:Shutdown` | Graceful service termination. |

### Events

| Event | Trigger |
|---|---|
| `pulsar:DestinationCreated` | Config-time event. |
| `pulsar:DestinationStateChanged` | live/paused/error/dropped transitions. |
| `pulsar:DestinationDropped` | Destination disconnected during a live stream. |
| `pulsar:DestinationReconnected` | Destination re-established after drop. |
| `pulsar:ServiceReady` | Service finished init, ready to accept commands. |
| `pulsar:ServiceShuttingDown` | Service received Shutdown, draining outputs. |

## Authentication details

- The session JWT is symmetric (HS256), generated from a per-session
  random secret. The JWT carries `sub: "pulsar-session"`, `iat`, `exp`
  (24h), and a session UUID.
- Connections from non-loopback addresses are refused at the socket
  layer — Pulsar binds to `127.0.0.1` exclusively unless launched
  with `--bind <addr>` (intended for development only).

## Stability guarantees

- **v5 baseline** is considered stable across Pulsar minor versions.
  Breaking v5 changes happen only when upstream obs-websocket itself
  breaks them.
- **`pulsar:` extensions** follow Pulsar's semver. Major bumps may
  rename or remove `pulsar:*` requests; minor bumps add new ones,
  patch bumps fix bugs without behavioural change.
- A consumer should advertise the Pulsar protocol version it expects
  on connect; mismatch returns a structured error rather than a
  silent half-broken session.
