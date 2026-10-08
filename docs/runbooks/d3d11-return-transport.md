# Program/Preview return transport — Pulsar 3.0.0

This runbook describes the **final patch stack**, including 0041–0043.
Patch 0024 introduced an earlier design; its writable external registration
mapping is not the current integration contract.

## Default and scope

ProgramReturn and PreviewReturn expose ordinary system-memory NV12 to
DirectShow consumers. The default transport is a producer-owned CPU queue
with read-only consumer access, latest-frame snapshots and correlated
metadata. These returns are separate from streaming, recording and the
stable Program encoder attachment.

`PULSAR_RETURN_TRANSPORT=d3d11` opts into a bounded internal GPU transport
for the qualified 1920×1080 NV12 return. CPU remains the fallback.
This option does not enable a public host GPU API or end-to-end zero-copy.

## Current ownership

The producer launches and authenticates its own private return helper.
Only that child receives the bootstrap and duplicated capabilities used
for the D3D11 ring. The producer remains authoritative for transport state;
public DirectShow clients do not register a PID in a writable GPU control map.

The helper receives shared textures, performs bounded readback, and relays
frames to the producer-owned CPU mapping. External DirectShow readers consume
that mapping read-only. Runtime/lane identity, epoch, frame sequence and media
metadata must stay consistent across this path.

The path still starts from converted CPU NV12 in the raw-video callback and
includes upload/readback. It neither renders a second composition nor
retains a borrowed raw callback pointer asynchronously. Program NVENC texture
ownership is separate.

## Qualification

Use the [latency harness](probe-take-latency.md) with an explicit
`--return-transport d3d11`. The harness propagates the selection to its
processes. Use `--return-transport cpu` for a controlled comparison; omitting
the argument can inherit an existing environment selection.

Collect the selected path **and fallback reason**, not only a successful
DirectShow capture. Check both Program and Preview with:

- a real compatible GPU and the actual packaged helper/module set;
- first attach, detach, reconnect, consumer termination and producer shutdown;
- unchanged frame identity, PTS, revisions and correlation identifiers;
- no disruption to Program, Preview, audio, encoding, streaming or readiness;
- bounded waits, frame age and resource usage against the CPU control.

A hosted runner without the required hardware can validate source/lifecycle
gates, but cannot establish a successful hardware D3D11 path.

## Fallback and diagnosis

Unsupported format/resolution, adapter mismatch, capability/bootstrap
failure, helper death, device removal and bounded-wait failure must preserve
or return to CPU publication. P010 and non-1080p operation are not qualified
by this route. A functioning CPU capture after fallback is availability
proof, not D3D11 performance proof.

Inspect transport selection, fallback reason/HRESULT, helper liveness,
adapter/epoch, frame sequence, waits, copy/readback and frame age in the
relevant trace. The DirectShow trace has its own sidecar
(`PULSAR_DIRECTSHOW_TRACE_PATH`); do not concurrently append unrelated
processes to the runtime producer trace.

## Rollback

Set `PULSAR_RETURN_TRANSPORT=cpu` before spawning a fresh runtime and repeat
the same capture checks. Preserve the complete matching binary/module set.
Do not remove patch 0024 or 0043 in isolation: later patches depend on the
earlier ABI and source transformations.

For a binary rollback, restore a complete previously validated release.
See [libobs changes](../LIBOBS-CHANGES.md) and the
[lease watcher](directshow-lease-watcher.md) for the complementary lifecycle
contract.
