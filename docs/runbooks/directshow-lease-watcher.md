# DirectShow return lease watcher

The consumer-gated `ProgramReturn` and `PreviewReturn` producers observe the
DirectShow consumer lease from a bounded watcher thread. `virtual_video` only
loads the atomic `consumer_active` bit; it does not open an event, close a
handle, perform I/O, or wait.

## Lifecycle contract

The watcher probes immediately at start and then every 20 ms. Every successful
`OpenEventW(SYNCHRONIZE)` is closed in the watcher before `consumer_active` is
published as true. A failed probe publishes false. Therefore a positive lease
state can be stale for at most one poll interval (20 ms), excluding scheduler
delay. Stop, probe failure, event detachment, and watcher setup failure are
fail-closed.

The observable transitions are:

`start -> attach -> detach -> reconnect -> stop`

`consumer_gated=false` keeps the existing unconditional publication path and
does not start a watcher. No producer retains a lease handle, so producer
lifetime cannot keep a DirectShow event alive.

For gated outputs, the event is only a compatibility signal: publication also
requires a live connection to the producer-owned challenge-derived named pipe.
The producer obtains the client PID from the kernel, checks the client session,
opens a liveness handle, and verifies the executable image before setting
`consumer_active`. A stale or pre-created event therefore remains detached,
and a rejected pipe client is disconnected and re-armed for legitimate
reconnection. This is not a cryptographic same-user authorization boundary:
an attacker who can replace an allowlisted executable or win the pipe-creation
race can still deny service. Closing that residual requires an authenticated
broker or producer-duplicated handle and remains outside this patch.

## Counters and diagnostics

Set `PULSAR_DIRECTSHOW_LEASE_TELEMETRY=1` (or `true`) before OBS starts to log
per-output `polls`, `hits`, `misses`, `expiry`, `fallback`, and `poll_ms` at
watcher shutdown. `fallback` counts the ungated compatibility path and watcher
setup failures. The counters are producer-local and reset only when the output
object is recreated.

## Rollback

Use a complete previously validated release and matching modules. Patch 0029
is part of a dependent stack; removing it alone is not an operational rollback.
For a D3D11-specific comparison, select `PULSAR_RETURN_TRANSPORT=cpu` before
spawn, without disabling consumer-liveness checks.

The [transport runbook](d3d11-return-transport.md) describes the private-helper
GPU route in 3.0.0. The event/pipe lease described here gates external return
activity; it does not grant an arbitrary DirectShow client a GPU handle.
Do not restore per-frame `OpenEventW` polling or weaken fail-closed checks.
