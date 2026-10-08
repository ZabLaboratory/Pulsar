# pulsar-output-classify

Header-only classifier for the closed `reason_class` set of ADR-005 §3.4
(`pulsar:OutputFailed`). No DLL, no OBS dependency -- pure logic over
`(is_local_output, code, last_error)`, so it can be linked identically into:

- `pulsar-frontend-stub` (static lib linked into `pulsar.exe`),
  from `hookOutputSignals`'s stop-signal handler.
- `pulsar-multi-stream` (DLL, owns the `"pulsar"` obs-websocket vendor),
  from `DestinationRegistry::start` / `start_all`.

See `pulsar-output-classify.h` for the seven-class mapping and its rationale.
Kept in its own `plugins/` entry rather than a stray `include/` because,
same as `pulsar-nv-secure-load`, its consumers live in different CMake
targets and an `INTERFACE` target here is what lets both name it identically.

Regression coverage: `tests/output-classify-probe` (pure-logic CTest, no
libobs/Qt/OBS runtime needed).


## Current protocol role

This classifier does not start/stop outputs and does not make a request
successful. It keeps failure reasons consistent across frontend and registry
paths. A failed initial attempt and a later on-air failure have separate
settlement/event semantics.

Use [PROTOCOL.md](../../docs/PROTOCOL.md) for the complete event fields and
[failure diagnosis](../../docs/runbooks/diagnose-a-failed-go-live.md) for the
operator procedure. Do not parse free-form error messages as a replacement
for the stable reason class.
