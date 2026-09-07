# Pulsar 3.0.0 — changes to libobs and the OBS runtime

This is the complete map of the OBS-side modifications shipped with Pulsar
3.0.0. “libobs changes” includes the core library, Windows graphics backend,
OBS encoders, DirectShow return transport and the nested browser patch; the
table below identifies their actual boundaries rather than calling every
plugin a libobs change.

For changes in Pulsar-owned C++ components and the TypeScript packages, use
the [architecture](ARCHITECTURE.md), [plugin inventory](../plugins/README.md)
and [release notes](releases/3.0.0.md).

## Source lineage and reproducibility

The `upstream/` submodule points to
[ZabLaboratory/obs-studio](https://github.com/ZabLaboratory/obs-studio) at
[`bd73b922891e56839b0bc86bdc519802802f9d68`](https://github.com/ZabLaboratory/obs-studio/commit/bd73b922891e56839b0bc86bdc519802802f9d68).
This fork is based on OBS 32.1.2; it is not an unmodified obsproject tag.

Five earlier changes are already incorporated in that pin, so they are not
missing files to recreate in `patches/`:

| Integrated commit | Change already present in the pin |
|---|---|
| [ce60b997b](https://github.com/ZabLaboratory/obs-studio/commit/ce60b997b) | Adds the `-pulsar` version suffix. |
| [2b3ff4171](https://github.com/ZabLaboratory/obs-studio/commit/2b3ff4171) | Gates ATL-dependent modules through `PULSAR_HAVE_ATL`. |
| [f877f3763](https://github.com/ZabLaboratory/obs-studio/commit/f877f3763) | Routes NVIDIA effect SDK loads through the validated-directory policy. |
| [7e7a5a383](https://github.com/ZabLaboratory/obs-studio/commit/7e7a5a383) | Adds a separate Program-return camera. |
| [b33f831b8](https://github.com/ZabLaboratory/obs-studio/commit/b33f831b8) | Resolves embedded modules and registers Program return. |

On top of this pin, 3.0.0 carries **52 patch files**: 51 root-OBS patches
and one nested obs-browser patch. [build-win.ps1](../scripts/build-win.ps1)
sorts complete filenames lexically, routes names containing `obs-browser`
to `upstream/plugins/obs-browser/`, and applies the others to `upstream/`.
There are two `0022-` files; both are required. Numeric prefixes alone are
not unique patch identifiers.

A clean, exact patched checkout can be reused only when its pin, patch
fingerprint and applied HEAD match the recorded state. `-RefreshPatches`
forces replay. A changed/dirty generated checkout is not a place to preserve
unexported upstream work: the build can reset it while reconstructing the
stack. Follow the [development guide](DEVELOPMENT.md) in an isolated worktree.

## What changes in the media pipeline

### Atomic production views

The core adds a queued two-view swap that executes on the video-frame
boundary. Pulsar's frontend builds hot A/B producers and stable Program and
Preview views around this primitive. Preparing a scene is not equivalent to
putting it on air; the command adapter waits for an actual Preview render.
A committed Cut reports the observed frame ID and PTS, while encoders and
output objects remain attached to their stable media identities.

The activation fixes preserve live producers and shared descendants through
role exchange. They do not remove the normal ownership rules for a generic
source replacement or optional transition. The public command contract is
[pulsar.scene-switch.v1](../scripts/contracts/scene_switch_v1/README.md),
not a consumer FFI into the new libobs symbols.

### Output interleaving and encoding

Low-latency interleaving reduces avoidable waiting between a video packet
becoming ready and output delivery. NVENC's ULL queue changes are distinct
from the later ready-drain and asynchronous experiments. An encoder callback,
an interleaver enqueue, an RTMP receiver packet and a decoded image are
different observation points; none substitutes for the next.

The new optional encoder callback is appended to the size-based registration
structure. All shipped modules are nevertheless compiled against the same
patched library. Do not combine a stock obs.dll, random plugin DLLs and a
Pulsar executable on the assumption that this preserves the complete ABI.

### Readback and content identity

Historical CPU staging can submit the preceding rendered surface while
advancing encoder cadence. Current-surface readback removes that particular
frame of waiting in the qualified CPU workload. Content identity travels
separately from cadence and presentation timestamps; audio compensation
preserves the media timeline instead of simply moving the picture early.

Automatic selection requires Windows, no active GPU encoding on that mix,
NV12 1920×1080 at 60/1 fps and a physical graphics adapter. It is not a
universal 60 fps optimization or a claim that software rendering is faster.

### Program and Preview returns

The default outward return is a read-only, latest-frame CPU NV12 queue.
Atomic snapshots, sequence checks, collision refusal and lifecycle leases
keep bytes, metadata and consumer activity coherent. A watcher maintains
consumer state outside the video callback, allowing unused return copies to
be skipped without stopping the broadcast.

The optional D3D11 route is internal to Pulsar's producer/private-helper
pair. It does **not** hand a native GPU handle to an arbitrary host application.
The helper reads back into the producer-owned CPU relay; external DirectShow
clients still see system-memory NV12. This is not end-to-end zero-copy.
Read the [current transport runbook](runbooks/d3d11-return-transport.md);
the early architecture visible in patch 0024 is not the final contract.

### Telemetry and lifecycle

Stage accounting distinguishes render, raw publication, encoder callback,
mutex wait, output enqueue, receiver arrival and decode. Correlated traces
are opt-in, with separately selectable signals. DirectShow writes its own
sidecar observations rather than mixing independent writers into one trace.

Replay stop no longer depends on a timestamp that may never arrive.
Fatal GPU encoder stop is retired outside the encode thread. These are
correctness fixes, not permission to omit stop/restart or recording integrity
checks.

## Defaults, experiments and rollback

| Control | 3.0.0 behavior | Operator meaning |
|---|---|---|
| `PULSAR_RAW_CURRENT_READBACK` unset | Qualified automatic CPU selection described above | Production default only on the qualified hardware/configuration. |
| `PULSAR_RAW_CURRENT_READBACK=0` | Historical CPU staging | Local diagnostic rollback of this optimization. |
| `PULSAR_RAW_CURRENT_READBACK=1` | Explicit CPU current-readback override | Diagnostic opt-in beyond the automatic gate; not new qualification. |
| `PULSAR_NVENC_LOW_LATENCY` | Enabled for NVENC; false values restore HQ | ULL tune, not removal of B-frames or other quality tools. |
| `PULSAR_DSHOW_FRESH_FRAME_POLL=1` | Off unless explicitly selected | Experimental bounded polling. |
| `PULSAR_NVENC_READY_DRAIN=1` | Off unless explicitly selected | Experimental ready-packet draining. |
| `PULSAR_NVENC_ASYNC_OUTPUT=1` | Off unless explicitly selected | Experimental completion servicing; measured tail latency did not justify promotion. |
| `PULSAR_RETURN_TRANSPORT=d3d11` | CPU unless explicitly selected | Internal helper transport with capability/format gates and CPU fallback. |
| `PULSAR_DUAL_LANE_TRANSITIONS=1` | Cut unless explicitly selected | Frontend Fade/Stinger capability, separate from libobs patch selection. |

Do not remove one old patch from the middle of the stack as an operational
rollback: later patches depend on the resulting source and ABI. Use a
documented runtime switch for a bounded comparison, or restore a complete
previously validated release and matching packages. Rebuilding a different
stack requires full qualification.

## Complete patch inventory

Paths below are relative to the patch's target repository. Each link opens
the exact source diff; descriptions explain its purpose and later refinements.
Patch headers can describe an earlier experimental stage. The final defaults
above and subsequent patches determine 3.0.0 behavior.

### 0006-obs-browser-software-osr-and-helper

[Source patch](../patches/0006-obs-browser-software-osr-and-helper.patch)

Stabilizes the patched nested obs-browser software offscreen-rendering path and helper selection. This patch targets upstream/plugins/obs-browser, not the root OBS checkout; Pulsar also ships its separately maintained browser fork.

Affected files: `browser-app.cpp`, `browser-client.cpp`, `obs-browser-plugin.cpp`.

### 0007-fix-win-dshow-namespace-virtual-camera-queues-by-run

[Source patch](../patches/0007-fix-win-dshow-namespace-virtual-camera-queues-by-run.patch)

Namespaces the virtual-camera producer and filter queues by runtime identity. Multiple instances no longer implicitly publish into one camera queue; subsequent namespace and lease patches complete the isolation policy.

Affected files: `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.hpp`, `plugins/win-dshow/virtualcam.c`.

### 0008-use-executable-path-for-libobs-data-with-private-cwd

[Source patch](../patches/0008-use-executable-path-for-libobs-data-with-private-cwd.patch)

Resolves libobs effect/data paths from the executable location, allowing a private working directory for every runtime without losing default.effect and packaged resources.

Affected files: `libobs/obs-windows.c`.

### 0009-feat-libobs-add-frame-boundary-dual-lane-swaps

[Source patch](../patches/0009-feat-libobs-add-frame-boundary-dual-lane-swaps.patch)

Adds the libobs frame-boundary two-view swap primitive, callback/frame identity and stable Program/Preview return plumbing. The graphics thread applies the queued roots together; the frontend owns preparation and command policy.

Affected files: `libobs/obs-internal.h`, `libobs/obs-video.c`, `libobs/obs-view.c`, `libobs/obs.c`, `libobs/obs.h`, `plugins/win-dshow/dshow-plugin.cpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.hpp`, `plugins/win-dshow/virtualcam-module/virtualcam-guid.h.in`, `plugins/win-dshow/virtualcam-module/virtualcam-module.cpp`, `plugins/win-dshow/virtualcam.c`.

### 0010-fix-win-dshow-reject-ambiguous-queue-namespaces

[Source patch](../patches/0010-fix-win-dshow-reject-ambiguous-queue-namespaces.patch)

Centralizes the absent/valid/invalid runtime-ID and legacy-alias decision. An invalid or ambiguous namespace refuses queue access instead of falling back to a historical singleton.

Affected files: `plugins/win-dshow/directshow-namespace.h`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.hpp`, `plugins/win-dshow/virtualcam-module/virtualcam-module.cpp`, `plugins/win-dshow/virtualcam.c`.

### 0011-feat-runtime-telemetry-producer

[Source patch](../patches/0011-feat-runtime-telemetry-producer.patch)

Carries correlated runtime observations through return publication, shared-memory metadata and DirectShow delivery. Trace identity is attached to actual media boundaries rather than synthesized from a successful command response.

Affected files: `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.hpp`, `plugins/win-dshow/virtualcam.c`, `shared/obs-shared-memory-queue/shared-memory-queue.c`, `shared/obs-shared-memory-queue/shared-memory-queue.h`.

### 0012-feat-libobs-add-low-latency-output-interleaving

[Source patch](../patches/0012-feat-libobs-add-low-latency-output-interleaving.patch)

Adds low-latency output interleaving controls and public libobs configuration support, allowing Pulsar to select a bounded interleaver policy without changing the upstream-compatible default for unrelated outputs.

Affected files: `libobs/obs-internal.h`, `libobs/obs-output.c`, `libobs/obs.h`.

### 0013-perf-libobs-decouple-live-video-from-audio-watermark

[Source patch](../patches/0013-perf-libobs-decouple-live-video-from-audio-watermark.patch)

Separates low-latency live-video progress from the audio watermark so a ready video packet need not wait for the historical audio lead. Audio routing and packet timing still require independent validation.

Affected files: `libobs/obs-output.c`, `libobs/obs.h`.

### 0014-perf-nvenc-drain-ULL-bitstreams-immediately

[Source patch](../patches/0014-perf-nvenc-drain-ULL-bitstreams-immediately.patch)

Bounds the pending NVENC bitstream queue for the ultra-low-latency tune. This changes queue draining, not the chosen codec quality settings; it is distinct from the later experimental ready-batch and asynchronous modes.

Affected files: `plugins/obs-nvenc/nvenc.c`.

### 0015-perf-libobs-configurable-aux-view-cache

[Source patch](../patches/0015-perf-libobs-configurable-aux-view-cache.patch)

Makes auxiliary-view video caching configurable. A caller can reduce an avoidable cache in a Preview path without changing the main output or claiming that all buffering is removable.

Affected files: `libobs/obs-internal.h`, `libobs/obs-view.c`, `libobs/obs.c`, `libobs/obs.h`.

### 0016-feat-libobs-expose-video-pipeline-stage-telemetry

[Source patch](../patches/0016-feat-libobs-expose-video-pipeline-stage-telemetry.patch)

Exposes per-stage video-pipeline observations through libobs, covering rendering, conversion/readback and publication. These are diagnostic stage timings, not a decoded/display latency measurement.

Affected files: `libobs/obs-internal.h`, `libobs/obs-video.c`, `libobs/obs.c`, `libobs/obs.h`.

### 0017-perf-libobs-pipeline-borrowed-preview-publication

[Source patch](../patches/0017-perf-libobs-pipeline-borrowed-preview-publication.patch)

Pipelines borrowed Preview publication and adds the supporting output/video hooks. Borrowed data remains subject to callback lifetime rules; consumers must not retain a raw pointer after publication.

Affected files: `libobs/obs-internal.h`, `libobs/obs-output.c`, `libobs/obs-output.h`, `libobs/obs-video.c`, `libobs/obs.c`, `libobs/obs.h`, `plugins/win-dshow/virtualcam.c`.

### 0018-feat-libobs-close-video-mix-stage-accounting

[Source patch](../patches/0018-feat-libobs-close-video-mix-stage-accounting.patch)

Closes gaps in video-mix stage accounting, separating output dispatch and raw-publication work so total pipeline time can be reconciled with its measured stages.

Affected files: `libobs/obs-internal.h`, `libobs/obs-output.c`, `libobs/obs-video.c`, `libobs/obs.h`.

### 0019-perf-win-dshow-pipeline-program-return-publication

[Source patch](../patches/0019-perf-win-dshow-pipeline-program-return-publication.patch)

Extends pipelined borrowed publication to ProgramReturn while preserving the existing Program media identity and encoded output binding.

Affected files: `libobs/obs-video.c`, `plugins/win-dshow/virtualcam.c`.

### 0020-perf-win-dshow-elide-unconsumed-preview-return-copies

[Source patch](../patches/0020-perf-win-dshow-elide-unconsumed-preview-return-copies.patch)

Skips PreviewReturn copies when no return consumer is attached. Consumer activity is a lifecycle decision; patch 0029 moves lease observation off the per-frame callback.

Affected files: `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.hpp`, `plugins/win-dshow/virtualcam.c`.

### 0021-perf-win-dshow-elide-unconsumed-program-return-copies

[Source patch](../patches/0021-perf-win-dshow-elide-unconsumed-program-return-copies.patch)

Applies the same no-consumer copy elimination to ProgramReturn. Encoding and on-air outputs continue independently of the DirectShow return subscriber.

Affected files: `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.hpp`, `plugins/win-dshow/virtualcam.c`.

### 0022-fix-cmake-qt-host-tool-argument-parsing

[Source patch](../patches/0022-fix-cmake-qt-host-tool-argument-parsing.patch)

Fixes CMake argument parsing for Qt host tooling. This is build-system compatibility, not a video-pipeline optimization. Both files with the 0022 prefix are intentional and ordered by their complete filename.

Affected files: `cmake/windows/buildspec.cmake`.

### 0022-perf-libobs-remove-initial-gpu-encode-frame-delay

[Source patch](../patches/0022-perf-libobs-remove-initial-gpu-encode-frame-delay.patch)

Removes the initial GPU-encode frame wait constant. This does not remove codec reordering, B-frames, driver scheduling or the receiver's decode delay.

Affected files: `libobs/obs-internal.h`.

### 0023-fix-win-dshow-atomic-return-queue

[Source patch](../patches/0023-fix-win-dshow-atomic-return-queue.patch)

Introduces/hardens latest-frame atomic return publication, sequence validation and reader consistency. Correlation metadata and frame bytes must belong to one coherent publication.

Affected files: `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.hpp`, `plugins/win-dshow/virtualcam.c`, `shared/obs-shared-memory-queue/shared-memory-queue.c`, `shared/obs-shared-memory-queue/shared-memory-queue.h`.

### 0024-feat-win-dshow-d3d11-return-capability-skeleton

[Source patch](../patches/0024-feat-win-dshow-d3d11-return-capability-skeleton.patch)

Introduces the opt-in D3D11 return ring and control ABI, plus shared-queue mapping validation. Its original architecture is superseded by 0041–0043: do not integrate against the early writable control-map design.

Affected files: `plugins/win-dshow/CMakeLists.txt`, `plugins/win-dshow/virtualcam-module/CMakeLists.txt`, `plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp`, `plugins/win-dshow/virtualcam-module/d3d11-return-transport.hpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.hpp`, `plugins/win-dshow/virtualcam.c`, `shared/obs-shared-memory-queue/shared-memory-queue.c`.

### 0025-fix-win-dshow-bootstrap-consumer-gated-return-format

[Source patch](../patches/0025-fix-win-dshow-bootstrap-consumer-gated-return-format.patch)

Bootstraps a consumer-gated return with a valid format before frames are being copied. This avoids waiting for a first frame that cannot be published until a consumer has negotiated its format.

Affected files: `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`.

### 0026-fix-libobs-pipeline-stats-atomic-snapshot

[Source patch](../patches/0026-fix-libobs-pipeline-stats-atomic-snapshot.patch)

Publishes pipeline statistics as an atomic snapshot. Readers observe one coherent measurement state without racing non-atomic stage accumulators.

Affected files: `libobs/obs-internal.h`, `libobs/obs-output.c`, `libobs/obs-video.c`, `libobs/obs.c`.

### 0027-perf-win-dshow-bulk-copy-tight-nv12

[Source patch](../patches/0027-perf-win-dshow-bulk-copy-tight-nv12.patch)

Uses a bulk copy for tightly packed NV12 while retaining row-aware handling for other strides. This reduces copy overhead; it does not make the CPU return zero-copy.

Affected files: `shared/obs-shared-memory-queue/shared-memory-queue.c`.

### 0028-feat-libobs-telemetry-output-enqueue-timestamp

[Source patch](../patches/0028-feat-libobs-telemetry-output-enqueue-timestamp.patch)

Adds the timestamp at which an encoder packet enters output handling, separating encoder callback time from interleaver enqueue time.

Affected files: `libobs/obs-encoder.h`, `libobs/obs-output.c`.

### 0029-fix-win-dshow-lease-watcher

[Source patch](../patches/0029-fix-win-dshow-lease-watcher.patch)

Moves consumer-lease observation to a bounded watcher. The real-time video callback reads cached atomic activity instead of opening/closing named objects on every frame.

Affected files: `plugins/win-dshow/virtualcam.c`.

### 0030-feat-libobs-interleaver-mutex-wait-telemetry

[Source patch](../patches/0030-feat-libobs-interleaver-mutex-wait-telemetry.patch)

Measures interleaver mutex wait separately from queue residence and dispatch. Waiting to acquire the lock must not disappear into an incorrectly attributed encoder duration.

Affected files: `libobs/obs-encoder.h`, `libobs/obs-output.c`.

### 0031-fix-replay-buffer-stop-without-timestamp

[Source patch](../patches/0031-fix-replay-buffer-stop-without-timestamp.patch)

Stops replay-buffer capture deterministically on the next packet without depending on an absent/unsuitable stop timestamp. Buffer teardown remains coupled to the output lifecycle.

Affected files: `plugins/obs-ffmpeg/obs-ffmpeg-mux.c`.

### 0032-fix-cpu-queue-mapping-collision

[Source patch](../patches/0032-fix-cpu-queue-mapping-collision.patch)

Rejects a pre-existing legacy CPU queue mapping rather than treating an object owned by another creator as this producer's fresh queue.

Affected files: `plugins/win-dshow/shared-memory-queue.c`.

### 0033-fix-consumer-lease-collision

[Source patch](../patches/0033-fix-consumer-lease-collision.patch)

Rejects collided consumer-lease objects before establishing the compatibility subscription, complementing producer queue ownership checks.

Affected files: `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`.

### 0034-fix-d3d11-consumer-liveness

[Source patch](../patches/0034-fix-d3d11-consumer-liveness.patch)

Tracks the registered D3D11 consumer's process lifetime so stale PID metadata cannot keep a return session active after the process exits.

Affected files: `plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp`.

### 0035-fix-modern-cpu-queue-mapping-collision

[Source patch](../patches/0035-fix-modern-cpu-queue-mapping-collision.patch)

Adds the corresponding existing-mapping collision check to the modern shared/obs-shared-memory-queue implementation, not just the legacy win-dshow copy.

Affected files: `shared/obs-shared-memory-queue/shared-memory-queue.c`.

### 0036-fix-d3d11-producer-consumer-liveness

[Source patch](../patches/0036-fix-d3d11-producer-consumer-liveness.patch)

Checks producer and consumer liveness on the D3D11 path and retires an invalid session instead of continuing to use stale shared resources.

Affected files: `plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp`.

### 0037-fix-d3d11-consumer-session-cleanup

[Source patch](../patches/0037-fix-d3d11-consumer-session-cleanup.patch)

Clears D3D11 consumer session state and handles during teardown/failure, making reconnection start from a clean registration.

Affected files: `plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp`.

### 0038-fix-d3d11-stale-registration-invalidation

[Source patch](../patches/0038-fix-d3d11-stale-registration-invalidation.patch)

Invalidates stale D3D11 registration before accepting a replacement, preventing old process metadata from remaining authoritative.

Affected files: `plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp`.

### 0039-fix-d3d11-invalidate-registration-before-session-loo

[Source patch](../patches/0039-fix-d3d11-invalidate-registration-before-session-loo.patch)

Moves invalidation before session lookup so a lookup failure cannot leave the preceding registration live. This refines, rather than replaces, the earlier cleanup patches.

Affected files: `plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp`.

### 0040-fix-dshow-move-registration-to-producer-owned-pipe

[Source patch](../patches/0040-fix-dshow-move-registration-to-producer-owned-pipe.patch)

Moves return-consumer registration behind a producer-owned pipe and kernel-observed client identity/liveness. CPU queue readers remain read-only; an event alone is not proof of an active consumer.

Affected files: `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.hpp`, `plugins/win-dshow/virtualcam.c`, `shared/obs-shared-memory-queue/shared-memory-queue.c`, `shared/obs-shared-memory-queue/shared-memory-queue.h`.

### 0041-fix-d3d11-producer-authoritative-readonly-abi-v2

[Source patch](../patches/0041-fix-d3d11-producer-authoritative-readonly-abi-v2.patch)

Makes the D3D11 control ABI producer-authoritative and consumer-read-only (v2), rejecting mixed ABI registration before sharing handles. The final private-helper architecture further narrows who receives GPU capabilities.

Affected files: `plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp`, `plugins/win-dshow/virtualcam-module/d3d11-return-transport.hpp`, `plugins/win-dshow/virtualcam.c`.

### 0042-feat-libobs-d3d11-direct-nt-shared-copy

[Source patch](../patches/0042-feat-libobs-d3d11-direct-nt-shared-copy.patch)

Adds an optional graphics-backend operation to copy directly into a validated existing NT-shared D3D11 texture, with the existing wrapper route as fallback on backends lacking the export.

Affected files: `libobs-d3d11/d3d11-subsystem.cpp`, `libobs/graphics/device-exports.h`, `libobs/graphics/graphics-imports.c`, `libobs/graphics/graphics-internal.h`, `libobs/graphics/graphics.c`, `libobs/graphics/graphics.h`.

### 0043-fix-d3d11-private-helper-authenticated-return

[Source patch](../patches/0043-fix-d3d11-private-helper-authenticated-return.patch)

Binds D3D11 access to a producer-launched private helper through inherited bootstrap handles and authenticated generation-bound messages. The helper performs readback; the producer remains the CPU queue writer and external DirectShow clients receive read-only NV12.

Affected files: `plugins/win-dshow/CMakeLists.txt`, `plugins/win-dshow/virtualcam-module/CMakeLists.txt`, `plugins/win-dshow/virtualcam-module/d3d11-return-auth.c`, `plugins/win-dshow/virtualcam-module/d3d11-return-auth.h`, `plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp`, `plugins/win-dshow/virtualcam-module/d3d11-return-transport.hpp`, `plugins/win-dshow/virtualcam-module/pulsar-d3d11-return-helper.cpp`, `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`, `plugins/win-dshow/virtualcam.c`.

### 0044-fix-dshow-trace-sidecar

[Source patch](../patches/0044-fix-dshow-trace-sidecar.patch)

Moves DirectShow observations to a dedicated sidecar trace path. Independent producer/consumer writes must not corrupt or impersonate the canonical producer trace.

Affected files: `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`.

### 0045-perf-libobs-preserve-hot-lane-activation

[Source patch](../patches/0045-perf-libobs-preserve-hot-lane-activation.patch)

Preserves hot-source activation through role exchange and maintains Preview activation, avoiding unnecessary producer stop/start churn at a Cut.

Affected files: `libobs/obs-internal.h`, `libobs/obs-source.c`, `libobs/obs-view.c`, `libobs/obs.h`.

### 0046-perf-libobs-preserve-equal-view-activation

[Source patch](../patches/0046-perf-libobs-preserve-equal-view-activation.patch)

Handles the equal-view activation case: exchanging two permanently hot MAIN_VIEW roots does not decrement/re-increment shared descendants. Generic replacements retain their normal activation and drain behavior.

Affected files: `libobs/obs-view.c`.

### 0047-fix-dshow-retain-equal-clock-stage-observations

[Source patch](../patches/0047-fix-dshow-retain-equal-clock-stage-observations.patch)

Accepts equal, nondecreasing stage-clock readings. Finite clock resolution can give zero-duration operations; those observations must not be silently dropped.

Affected files: `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`.

### 0048-perf-dshow-evaluate-fresh-frame-polling

[Source patch](../patches/0048-perf-dshow-evaluate-fresh-frame-polling.patch)

Adds bounded fresh-frame polling for ProgramReturn as an explicit experiment (PULSAR_DSHOW_FRESH_FRAME_POLL=1). It remains off by default; do not describe its existence as a demonstrated production improvement.

Affected files: `plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp`, `shared/obs-shared-memory-queue/shared-memory-queue.c`, `shared/obs-shared-memory-queue/shared-memory-queue.h`.

### 0049-fix-telemetry-encode-content-time

[Source patch](../patches/0049-fix-telemetry-encode-content-time.patch)

Separates encoded content time from frame-cadence timestamps. A packet may describe an older rendered surface even when the encoder submission cadence is current.

Affected files: `libobs/obs-encoder.c`, `libobs/obs-encoder.h`, `libobs/obs-internal.h`, `libobs/obs-video-gpu-encode.c`, `libobs/obs-video.c`.

### 0050-perf-libobs-current-readback-content-identity

[Source patch](../patches/0050-perf-libobs-current-readback-content-identity.patch)

Adds current-surface CPU readback and explicit content identity through video I/O, encoding and return publication. Later patches supply audio alignment and the narrowly qualified automatic default.

Affected files: `libobs/media-io/video-io.c`, `libobs/media-io/video-io.h`, `libobs/obs-encoder.c`, `libobs/obs-encoder.h`, `libobs/obs-video.c`, `plugins/win-dshow/virtualcam.c`.

### 0051-perf-nvenc-drain-ready-batches

[Source patch](../patches/0051-perf-nvenc-drain-ready-batches.patch)

Adds an optional bounded already-ready packet drain on the existing encoder thread and the supporting encoder callback. PULSAR_NVENC_READY_DRAIN=1 is experimental and off by default; timing registration precedes packet delivery.

Affected files: `libobs/obs-encoder.c`, `libobs/obs-encoder.h`, `libobs/obs-internal.h`, `libobs/obs-video-gpu-encode.c`, `plugins/obs-nvenc/nvenc-internal.h`, `plugins/obs-nvenc/nvenc.c`.

### 0052-perf-nvenc-service-asynchronous-output

[Source patch](../patches/0052-perf-nvenc-service-asynchronous-output.patch)

Services completed NVENC output between video ticks using Windows completion events and the existing encode thread. PULSAR_NVENC_ASYNC_OUTPUT=1 is experimental and off by default; preserves compression settings and FIFO input lifetime.

Affected files: `libobs/obs-encoder.h`, `libobs/obs-video-gpu-encode.c`, `libobs/util/threading-windows.c`, `libobs/util/threading.h`, `plugins/obs-nvenc/nvenc-d3d11.c`, `plugins/obs-nvenc/nvenc-internal.h`, `plugins/obs-nvenc/nvenc.c`.

### 0053-fix-libobs-current-readback-audio-alignment

[Source patch](../patches/0053-fix-libobs-current-readback-audio-alignment.patch)

Aligns the presentation clock of current-surface CPU readback with audio while retaining content time separately. Removing a staging frame must not silently advance picture relative to the audio timeline.

Affected files: `libobs/obs-video.c`.

### 0054-fix-libobs-gpu-error-stop-lifecycle

[Source patch](../patches/0054-fix-libobs-gpu-error-stop-lifecycle.patch)

Defers fatal GPU encoder retirement to the destruction-task path, avoiding an encoder thread waiting for itself during stop. Native validation covers inactive state and restart of the same encoder/output.

Affected files: `libobs/obs-encoder.c`, `libobs/obs-internal.h`.

### 0055-perf-libobs-default-qualified-current-readback

[Source patch](../patches/0055-perf-libobs-default-qualified-current-readback.patch)

Promotes current readback only for the qualified Windows CPU NV12 1920×1080 at 60/1 fps path; explicit 0 rolls back, explicit 1 selects the diagnostic override. GPU encoding remains on its prior selection.

Affected files: `libobs/obs-video.c`.

### 0056-fix-libobs-auto-readback-hardware-gate

[Source patch](../patches/0056-fix-libobs-auto-readback-hardware-gate.patch)

Further restricts automatic current readback to a physical graphics adapter. A software-only Microsoft Basic Render Driver runner stays on historical staging; an explicit diagnostic override remains available.

Affected files: `libobs/obs-video.c`.

## Validation boundaries and retained studies

The native CPU readback study records 100 warm-up and 100 measured Cuts per
trial, a changed decoded-image oracle, media timestamps and common Program
audio. Measured x264 p95 moves from 45.897–45.947 ms in the reference trials
to 33.045–37.982 ms in candidate/default trials. These are workload-specific
decoder-return measurements, not physical-display latency or a guarantee
for another adapter, codec, resolution or frame rate.

- [Native optimized result](issue-253-native-optimized.md): promoted path,
  audio checks, rejected experiments and retained local validation limits.
- [Decoded audit](issue-253-decoded-audit.md): packet-versus-picture distinction.
- [Latency study](issue-253-latency-study.md): observation boundaries.
- [NVENC chain study](issue-253-nvenc-chain-study.md): scheduling and quality alternatives.
- [Dual-lane canary](runbooks/pulsar-dual-lane-canary.md): separate codec/gate evidence.
- [Release notes](releases/3.0.0.md): source history since v2.0.0b.

Historical study results retain their original candidate and hardware scope.
Current release CI, release assets and their hashes are separate evidence.
No 4K, multi-camera capacity, universal NVENC gain or physical-display result
is inferred from a passing native unit test.
