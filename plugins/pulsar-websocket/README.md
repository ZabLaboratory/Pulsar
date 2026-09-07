# pulsar-websocket

The integrated Pulsar fork of obs-websocket v5.7.3. The active CMake target
builds **`obs-websocket.dll`**, loaded from `obs-plugins/64bit/`.
It is not a deferred phase and does not emit `pulsar-websocket.dll`.

## Responsibilities

- v5 Hello/Identify authentication, requests, batches and events;
- baseline scene/input/filter/audio/output control against the headless frontend;
- vendor registration and dispatch for Pulsar components;
- output-attempt verification and structured failure/settlement feedback;
- listener readiness and bounded callback quiescence before shutdown.

The native bootstrap seeds per-session config before module load.
The default listener is `127.0.0.1`, with an explicitly configured
`PULSAR_WS_BIND` override. Authentication uses the v5 password challenge,
**not a session JWT**. The READY line is emitted by `pulsar.exe`, not by
this plugin, after listener and frontend initialization.

## Fork behavior

The fork removes upstream settings/connect dialogs and Tools-menu integration
and avoids profile migration through absent OBS Studio UI state. Qt support
used by the protocol implementation remains; “no OBS UI” is not “zero Qt.”

The current fork also contains effective-output-state checks, service
validation, headless request adaptations, diagnostics integration and shutdown
coordination. It is no longer accurate to say every request handler is
byte-identical to upstream. The [protocol](../../docs/PROTOCOL.md) defines the
supported baseline and explicit refusals.

## Vendor namespaces

| Vendor | Owner |
|---|---|
| `pulsar` | Multi-stream: destinations, capabilities, video/audio, adaptive control and diagnostics. |
| `pulsar-scene` | Scene-source: managed browser-capture replacement. |
| `pulsar-scene-switch` | Frontend: deterministic Prepare/Take/Abort/GetState. |
| Browser-specific surface | Browser module, subject to its own available requests/control policy. |

A command is sent through v5 `CallVendorRequest` with `vendorName`,
`requestType` and `requestData`. Vendor events are v5 `VendorEvent`
envelopes. Do not turn a vendor command into a top-level v5 request or register
two independent owners under the same vendor name.

The scene-switch contract carries its own revisions, command IDs and event
sequence. Generic v5 delivery does not supply those production semantics.

## Effective output state

A successful network exchange is not sufficient to report a successful start.
The request layer verifies the effective output state with the bounded
`PULSAR_OUTPUT_VERIFY_MS` policy and exposes settlement/failure data.
Twitch via the legacy `rtmp_common` service is refused; use the destination
API's pinned TLS ingest.

Diagnostic message content is restricted by the implemented loopback policy.
Counters/state and detailed log lines have different admission rules.
See the [failure runbook](../../docs/runbooks/diagnose-a-failed-go-live.md).

## Lifecycle and layout

The frontend callback table is installed before this module loads, so
event subscriptions have a live owner. Teardown stops request/event admission
and drains active callbacks before libobs/frontend state is released.

`src/websocketserver/` owns transport, `src/requesthandler/` baseline
requests, `src/eventhandler/` libobs-to-v5 events, `Config.cpp`
configuration and `WebSocketApi.cpp` the native vendor API.
The public plugin header is `lib/obs-websocket-api.h`.

## Validation

The pipeline compiles the active target, checks binary exports, executes
native shutdown/debug/output-attempt gates and runs real v5 offline probes.
The TypeScript client tests additionally cover wire mapping, but do not
replace native-runtime checks.

See [development](../../docs/DEVELOPMENT.md) and
[component architecture](../../docs/ARCHITECTURE.md).
License: inherited GPL-2.0-or-later; preserve the upstream notice in
`UPSTREAM-LICENSE`.
