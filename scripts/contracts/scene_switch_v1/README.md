# `pulsar.scene-switch.v1`

This directory is the canonical contract for Pulsar's deterministic Preview →
On-air scene switch. It is intentionally independent of OBS/libobs and of any
particular WebSocket implementation. The C++ vendor handler, TypeScript
clients, and probes must preserve this wire shape and state machine.

- `schema.json` is the normative JSON Schema (Draft 2020-12).
- `__init__.py` is a dependency-free reference validator and state machine.
- `fixtures/examples.json` contains correlated command/event examples.
- `test_scene_switch_contract.py` is the executable contract corpus.

The wire uses the snake_case convention of Pulsar's `obs_data_t` handlers.
The contract identifier is `pulsar.scene-switch.v1`; `schema_version` is the
numeric version and is not silently inferred from a missing field.

## Lifecycle

```text
Prepare command
  └─ PrepareAccepted (Preview reservation; preview revision advances)
       └─ PreviewReady (first actually rendered frame_id + pts_ns)
            └─ Take command
                 └─ TakeAccepted (future Preview is frozen)
                      ├─ TakeCommitted (atomic frame-boundary role swap)
                      └─ TakeAborted (explicit abort or timeout)
```

`TakeAccepted` is not a commit. Between `TakeAccepted` and either
`TakeCommitted` or `TakeAborted`, a new Prepare targeting Preview is rejected
with `PREVIEW_FROZEN`. The current role map and all revisions remain unchanged
until the commit. This is the observable form of ADR r2 invariant I9.

At `TakeCommitted`, `target_lane_id` (the ready Preview lane) becomes
`role_map.on_air`; the former `role_map.on_air` becomes `role_map.preview`.
The engine commits the frame and PTS it actually selected at the frame
boundary. It does not rebind an active encoder or prescribe how either lane is
rendered.

## Commands

Every command has these required fields:

```json
{
  "contract": "pulsar.scene-switch.v1",
  "schema_version": 1,
  "message_type": "command",
  "command_type": "Prepare",
  "command_id": "prepare-001",
  "intent_id": "intent-001",
  "runtime_instance_id": "runtime-001",
  "expected_revisions": {"program": 0, "preview": 0, "role_map": 0},
  "expected_server_seq": 0
}
```

`expected_server_seq` is optional. When present it is a compare-and-swap guard
against an observation gap in addition to `expected_revisions`. Revisions are
strictly the three v1 streams:

| Key | Meaning | Changed by |
| --- | --- | --- |
| `program` | Program role/routing revision | `TakeCommitted` |
| `preview` | Preview lane preparation revision | `PrepareAccepted` |
| `role_map` | Logical On-air/Preview lane mapping revision | `TakeCommitted` |

`Prepare` adds a `target` (`lane_id` `A` or `B`, and a non-empty
`scene_id`) and `timeout_ms` in `[1, 60000]`. The target must be the current
Preview lane. A successful preparation advances only the Preview revision.

`Take` adds `prepared_command_id` and `timeout_ms`. Its `intent_id` must be the
same as the accepted preparation, and its expected revisions must still match.

`Abort` adds `take_command_id` and a reason from `operator`, `timeout`,
`shutdown`, or `superseded`. An abort never changes the role map or revisions.

Identifiers are bounded to 128 characters, non-empty, and use
`[A-Za-z0-9][A-Za-z0-9._:-]*` with a strict end-of-input anchor (a trailing
newline is not accepted). Frame IDs and PTS are non-negative integers;
`pts_ns` is explicitly nanoseconds. Draft 2020-12 defines `integer` by numeric
value, so a JSON number such as `1.0` is accepted when finite and integral;
the Python validator normalizes it to `1` before state transitions and event
serialization. Booleans, fractional/non-finite numbers, and unknown fields are
rejected. In particular, `schema_version=true` is not the integer version `1`.

## Events

Every event carries the correlation and state fields below:

```json
{
  "contract": "pulsar.scene-switch.v1",
  "schema_version": 1,
  "message_type": "event",
  "event_type": "TakeCommitted",
  "command_id": "take-001",
  "intent_id": "intent-001",
  "runtime_instance_id": "runtime-001",
  "server_seq": 5,
  "state": "ready",
  "previous_revisions": {"program": 0, "preview": 1, "role_map": 0},
  "revisions": {"program": 1, "preview": 1, "role_map": 1},
  "role_map": {"on_air": "B", "preview": "A"},
  "observed_at_monotonic_ns": 4200000000,
  "payload_sha256": "<64 lowercase hex characters>"
}
```

`previous_revisions` and `revisions` are the before/after snapshot. Rejected
commands carry identical snapshots. `server_seq` is monotone per
`runtime_instance_id` for emitted events. An idempotent retry returns the
original event byte-for-byte (including its original `server_seq`) and does
not append a second event or increment the sequence.

Event-specific evidence:

| Event | Required evidence |
| --- | --- |
| `PrepareAccepted` | target lane/scene and preparation deadline |
| `PreviewReady` | target lane/scene, `first_frame_id`, `first_pts_ns` |
| `TakeAccepted` | Take ID, target lane/scene, freeze deadline |
| `TakeCommitted` | Take ID, source/target lanes, `frame_id`, `pts_ns`, resulting Program/Preview lanes; `previous_role_map` is included by the reference model |
| `TakeAborted` | Take ID and stable abort reason |
| `CommandRejected` | stable `error_code`, bounded message, details, and the caller's expected guards |

`PreviewReady` is emitted only after the producer observes the first valid
rendered Preview frame. WebSocket acceptance alone is not readiness. The
callback is one-shot: an exact repeat returns the original event byte-for-byte
without another sequence number, while a different frame/PTS for the same
preparation is rejected with `IDEMPOTENCY_CONFLICT`. A
`TakeCommitted` frame/PTS is the frame selected at the atomic swap boundary,
not a timestamp inferred by a downstream decoder.

## Idempotence and stale guards

The idempotency scope is `(runtime_instance_id, command_id)`:

1. A new command ID is checked against the current revision and optional server
   sequence before any mutation.
2. A repeated ID with the same canonical payload (sorted JSON keys, UTF-8,
   compact separators, no NaN/Infinity) returns the original result. This
   applies to accepted and rejected command results.
3. Reusing an ID with another payload returns `IDEMPOTENCY_CONFLICT`; the
   original result remains authoritative and the runtime is not changed.
4. A stale `expected_revisions` returns `REVISION_STALE`; an optional stale
   `expected_server_seq` returns `SERVER_SEQ_STALE`. Both reject before any
   route, surface, lane, or revision mutation.
5. Two Takes racing on one revision cannot both reach `TakeCommitted`: the
   first `TakeAccepted` freezes the candidate, and the other command is
   rejected by state or revision guard.

The reference machine serializes command dispatch, readiness callbacks, frame
commit callbacks, expiry polling, and inspection snapshots under one reentrant
lock. This is part of the reference semantics: callers may race, but only one
serialized transition can observe and consume a revision or pending callback.

The reference state machine exposes `payload_sha256()` so a server can record
the digest in logs and in every correlated event without logging scene
content beyond the contract fields.

## Stable errors and recovery

The error code is machine-readable and stable; `error_message` is diagnostic,
not a parsing key. The v1 codes are:

`SCHEMA_INVALID`, `RUNTIME_MISMATCH`, `REVISION_STALE`, `SERVER_SEQ_STALE`,
`IDEMPOTENCY_CONFLICT`, `PREVIEW_FROZEN`, `PREVIEW_LANE_MISMATCH`,
`PREVIEW_NOT_READY`, `PREPARE_NOT_FOUND`, `TAKE_NOT_PENDING`,
`TAKE_INTENT_CONFLICT`, `TIMEOUT`, and `ABORTED`.

If the preparation does not produce a first frame before its deadline, polling
or a late readiness callback clears the pending preparation and emits one
stable `CommandRejected` with `TIMEOUT`. The initial `PrepareAccepted` command
response remains the result returned by an exact command retry; the terminal
timeout event is retained and replayed to late readiness callbacks. A
replacement preparation must use the new revision snapshot; a late callback
for the expired preparation cannot replace it silently. If a Take reaches its deadline before the frame-boundary commit, the
machine emits `TakeAborted(reason=timeout)`. Explicit abort uses
`TakeAborted` with the requested reason. In all three cases the role map and
route revisions remain unchanged; a later commit callback cannot create a
commit.

The safe rollback sequence is: stop accepting new Takes, let the current
Program frame drain, abort/expire pending Takes, preserve the current role map,
disable the feature flag, and return to the compatibility path at a frame
boundary. The contract does not authorize a `video_t` rebind during rollback.

## Cross-service rollout

1. Ship the schema and validator in shadow mode. Producers and consumers must
   reject unknown schema versions rather than guessing.
2. Add server-side command handling and event emission using this schema;
   retain the existing scene path behind the dual-lane feature flag.
3. Enable `Prepare`/`PreviewReady`/`Take` for a canary runtime and verify
   event sequence, revisions, frame/PTS and the no-mutation freeze tests.
4. Expand only after Probe has verified duplicate, stale, conflict, abort,
   timeout, and concurrent command behavior.

Rollback is additive: disable new command acceptance, abort pending commands,
and keep the old route. The schema, event log, and sequence remain useful for
diagnosis. No existing obs-websocket v5 request is changed by this contract.
