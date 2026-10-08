# Offline probes: ConnectionRefused / ConnectionClosed

This is a current diagnostic checklist with a historical incident reference:
run `30230046422` on `0d04641` (2026-07-27), instrumented by PR #132.
Its suspected WASAPI race and estimated occurrence rate were not a general
diagnosis of later failures.

## Classify the failed boundary

A WebSocket refusal can mean the server never started, exited, used another
port, or rejected the expected runtime setup. A connection closing mid-probe
can be a server death or an independent client/transport error.

1. Find the earliest failure in the build/probe output, not the final client
   exception. Inspect process exit status and startup diagnostics.
2. Look for the suite's `FATAL: the shared pulsar.exe DIED` banner.
   Its “last alive” probe locates the failure in time; it does not prove causality.
3. Correlate the actual runtime instance, session, port and private working
   directory. A newly allocated port reduces collisions; it is not a proof
   that all startup races are impossible.
4. If the process remains alive, query `GetDiagnostics` through the expected
   authenticated client. Check whether a probe assertion failed independently.
5. If it exited, preserve the stderr tail, exit code and retained probe
   artifacts. Absence of the FATAL banner alone does not prove the process lived.

## Preserve evidence

Use the log path reported by the runtime: explicit `PULSAR_LOG_DIR`, otherwise
its private runtime's `logs/`, with the historical local-app-data fallback
only when applicable. A temporary wrapper-owned runtime can be removed at
shutdown. Do not assume the old global log directory survives every run.

Logs are redacted by the native handler, but review exported artifacts and
raw subprocess output before sharing. See
[failed go-live](diagnose-a-failed-go-live.md).

## Historical lead, not a standing exemption

In the July incident, a hosted Windows runner had no usable audio endpoint.
WASAPI reconnect activity overlapped source teardown, making a lifetime race
a plausible lead. The original stderr tail was unavailable; the cause was
not proven.

For a new incident, verify the actual runner, audio devices, probe sequence
and process state. Do not rule out port/configuration or stale-process issues
merely because an older job ran on a fresh VM.

## Retry and resolution

Inspect the current workflow for its retry policy. Never widen it, disable
the failing gate or add `continue-on-error` to make a release pass.

Reduce a reproducible failure to its owning code/test/runtime boundary.
A confirmed runner incident can justify retrying the same immutable candidate;
a code fix needs its own validated revision. Preserve the first failure
before retrying. A second failure is evidence to investigate, not a cue to
keep rerunning.

The diagnostic-only PR #132 is historical provenance, not the recommended
rollback of today's complete probe suite.
