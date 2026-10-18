# Pulsar OBS patch stack

Pulsar 3.0.0 carries **61 patches** above the pinned
`ZabLaboratory/obs-studio` revision
`bd73b922891e56839b0bc86bdc519802802f9d68`.
Five foundational changes are already in that fork revision.

The [complete libobs/OBS reference](../docs/LIBOBS-CHANGES.md) documents every
patch, affected file, purpose, default/experimental status and the integrated
baseline. Start there when reviewing what Pulsar changes in OBS.

## Targets and order

[build-win.ps1](../scripts/build-win.ps1) and the CI apply gate split patches
by the **complete filename**:

- filenames containing `obs-browser` target the pinned nested
  `upstream/plugins/obs-browser/` repository;
- all other patches target `upstream/`;
- each set applies in lexical filename order, root first, nested browser next.

There are intentionally two `0022-` files. Do not select patches by numeric
prefix alone. The historical “apply every patch to upstream in a shell loop”
instruction is wrong for the nested browser patch.

## Format and authoring

Use a descriptive four-digit-prefixed `NNNN-name.patch` filename and a
reviewable `git format-patch` artifact. Preserve existing names/order.
The header should explain the behavioral change, why a plugin cannot express
it, upstream eligibility and the issue/work-unit provenance.

Work in a dedicated checkout. Export source changes before running a build
that may reconstruct/reset generated upstream state. Commit/tag operations
follow the workspace's signature and provenance requirements.

A source rebase is not complete because `git am` succeeds: inspect semantic
changes, ABI, lifecycle and regression evidence. Update the complete change
reference in the same PR.

## Build-cache behavior

Patch 0057 adds `obs_source_get_core_memory_usage`, consumed by the native
websocket `GetSourceStats` request. Windows reads core allocations and cache-owned
frames under the source async mutex; other platforms return an unavailable
sentinel. This is a lower bound and excludes plugin/driver/GPU allocations.
The patch is replayed after the existing stack without changing its upstream pin.

Patch 0058 lets `PULSAR_REQUIRE_CEF` request the pinned, hash-verified CEF
dependency independently of the upstream browser target. `build-win.ps1 -Full`
sets that flag; light builds leave it off. `scripts/test-cef-provisioning.py`
exercises the real dependency helper for full, light and invalid-hash cases.

Patch 0059 bounds read-owned source profiling to a five-second lease, independent
of explicit resource-trace policy. The native `probe-source-telemetry.py` checks
warm samples during polling, expiry after idle and explicit component maxima.

Patch 0060 defers initial/recreated NVIDIA blur loading until its source and
destination images are bound. Live updates still reload and report failures.
The AFX/VFX logger callbacks ignore null/empty messages while preserving actual
SDK diagnostics. The installed-SDK probe records DLL hashes, live intensities,
stacked filters and scene-switch counters. It deliberately fails when AFX
intensity zero does not pass the signal through; this patch does not fix that
separate SDK behavior.

Patch 0061 supplies a zero-impact audio bypass through the existing PCM FIFO
using an atomic settings flag. It retains the filter's enabled state and does
not reset/reload SDK models. Nonzero intensity remains an SDK operation and
must be qualified separately, including intermediate values.

Patch 0062 recreates AFX handles on a persistent control worker when method,
intensity or VAD changes. The installed denoiser captures settings at model
load; live SetFloat alone is insufficient. Audio uses a nonblocking lock and
original PCM during loading. Destruction joins the worker before releasing
its data, outside the loader mutex. The SDK logger survives individual filters.
The installed-SDK probe covers rapid changes, destruction during loading,
multiple audio filters and optional synthetic native Program recording.

The build records the upstream pin, patch-content fingerprint and applied
HEAD. It reuses only an exact clean match; `-RefreshPatches` forces replay.
This preserves incremental object caches without accepting a different
patched source tree as the same candidate.

Use the normal full build for qualification. `-Fast` is only a local target
loop and does not validate every plugin or the complete release package.

## Removing or replacing a patch

Patch 0063 adds native VFX video denoising and same-frame comparison. Patch 0064
adds AR face following and eye contact, demand-driven image allocation and
read-only processing counters. Both use the existing GPU source texture, with
no second capture producer. See the complete change reference for SDK gates,
lifecycle repairs, shader semantics and qualification limits.

Only remove a patch after proving that its behavior is supplied by the new
pin or an explicitly approved replacement. Replay and validate the **whole
dependent stack**. Never delete an old patch from the middle as an operational
rollback: subsequent patches may depend on its source, symbols or ABI.

For current CPU readback, `PULSAR_RAW_CURRENT_READBACK=0` is a bounded
diagnostic rollback. A product rollback uses a complete previously validated
release with matching binaries and packages.
