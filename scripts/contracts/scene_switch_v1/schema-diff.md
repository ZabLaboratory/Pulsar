# Schema diff — `pulsar.scene-switch.v1`

There was no scene-switch command/event contract in the preceding Pulsar
protocol. This is an additive contract; existing obs-websocket v5 requests and
the older `scene_control` overlay leaf remain unchanged.

| Area | Before #242 | v1 contract |
| --- | --- | --- |
| Version identity | No stable scene-switch version | `contract=pulsar.scene-switch.v1`, `schema_version=1` |
| Preparation | Unspecified | `Prepare` → `PrepareAccepted` → `PreviewReady` |
| Take | Scene action had no commit evidence | `Take` → `TakeAccepted` → `TakeCommitted` |
| Commit evidence | Downstream observation only | `frame_id`, `pts_ns`, source/target lane, before/after revisions |
| Ordering | No command-level CAS | `expected_revisions` and optional `expected_server_seq` |
| Retry | No authoritative replay rule | canonical payload SHA-256; same ID + same payload replays original result |
| Idempotency scope | Command ID had no defined runtime scope | cache key is `(runtime_instance_id, command_id)`; one runtime cannot poison another |
| Conflict | No stable command-ID conflict | `IDEMPOTENCY_CONFLICT` with original/received digests |
| Stale work | No no-mutation guarantee | `REVISION_STALE` / `SERVER_SEQ_STALE` before route/surface mutation |
| Pre-commit lifecycle | Future Preview could alias the promoted route | `TakeAccepted` freezes Preview until `TakeCommitted` or `TakeAborted` |
| Callback semantics | Readiness/commit callbacks could race or repeat | command/callback boundary is serialized; `PreviewReady` is immutable/idempotent |
| Failure recovery | Timeout/abort behavior was implicit | bounded deadlines, stable Prepare timeout, explicit `TakeAborted`, mapping/revisions preserved |
| Observability | Request ID was insufficient | runtime, command, intent, sequence, revisions, monotonic timestamp, digest |

The contract intentionally does not define libobs objects, active `video_t`
handling, UI, codec settings, Preview audio/AFV, or runtime namespace/lease
implementation. Those consumers are required to implement the contract but own
their respective follow-up issues.
