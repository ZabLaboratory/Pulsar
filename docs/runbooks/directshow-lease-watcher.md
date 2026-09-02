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

## Counters and diagnostics

Set `PULSAR_DIRECTSHOW_LEASE_TELEMETRY=1` (or `true`) before OBS starts to log
per-output `polls`, `hits`, `misses`, `expiry`, `fallback`, and `poll_ms` at
watcher shutdown. `fallback` counts the ungated compatibility path and watcher
setup failures. The counters are producer-local and reset only when the output
object is recreated.

## Rollback

Remove `0026-fix-win-dshow-lease-watcher.patch` from the lexical patch set and
replay the pinned OBS submodule from `bd73b922891e56839b0bc86bdc519802802f9d68`.
The prior `0025` bootstrap behavior remains intact. Do not restore per-frame
`OpenEventW` polling without re-establishing the bounded-lifecycle and
fail-closed tests.
