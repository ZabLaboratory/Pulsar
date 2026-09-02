# D3D11 return transport qualification

Status: experimental and opt-in only; not retained on the nominal
ProgramReturn/PreviewReturn path until hardware evidence proves a gain.

Patch `0024` adds a separate D3D11 control mapping and three-slot NV12 texture
ring for each return lane. The outward DirectShow sample remains ordinary
system-memory NV12. The existing CPU seqlock queue remains the fail-closed
fallback.

The transport is enabled only with `PULSAR_RETURN_TRANSPORT=d3d11` for the
fixed 1920x1080 NV12 experiment. An unset value, `cpu`, `off`, `disabled`, or
an unknown value keeps the existing CPU queue and is the operational default.
This explicit opt-in is the rollback for the measured regression: the D3D11
path still duplicates NT handles into the consumer process after the consumer
publishes PID/session, records the producer adapter LUID and epoch,
uses keyed mutex key 0→1/1→0 ownership. The DirectShow consumer probes keyed
mutex ownership without waiting; a busy slot immediately takes the existing
CPU queue fallback. The GPU copy then uses a D3D11 event query with a bounded
2 ms deadline before readback. A borrowed frame pointer is copied into a
tightly-packed upload vector synchronously; `cpu_upload_ns` measures this
required conversion and no pointer crosses the callback.

The current implementation is a bounded GPU transport, not a zero-copy path:
the `raw_video` callback still receives OBS's converted system-memory NV12 frame,
copies it synchronously into the shared texture, and the consumer reads it back
into the ordinary DirectShow sample. It does not render a second composition and
does not retain a borrowed CPU pointer asynchronously. A future producer-side
GPU texture hand-off must be a separate architectural change.

The implementation satisfies these safety gates, but is not a claimed
optimization:

- explicit `PULSAR_RETURN_TRANSPORT=d3d11` attempt with CPU-by-default
  selection, and independent ProgramReturn and PreviewReturn control maps/rings;
- three NV12 textures per lane, fixed-width `uint64_t` duplicated handles,
  monotonic sequence/epoch metadata, matching adapter LUID, and bounded keyed
  mutex waits;
- consumer-side GPU copy/readback into the existing system-memory NV12 sample
  while preserving frame ID, PTS, revisions, and correlation identifiers;
- separate `cpu_upload_ns`, `gpu_copy_ns`, `fence_wait_ns`, and `readback_ns`
  telemetry so a future claim cannot hide a CPU conversion or the DirectShow
  sample readback;
- observable fallback on creation/open, adapter, device-removed, timeout,
  format, and inter-bitness failures, without stopping Program, Preview, audio,
  encoding, RTMP, or readiness;
- proof that the existing converted Program/Preview frame is copied once and
  that the Program NVENC texture lifetime is unchanged; this does not claim
  producer-side zero-copy.

P010 and non-1080p formats fail closed to the CPU queue. Create/open,
adapter/session, device-removed, duplicate-handle, keyed-mutex, and query
timeout failures set a numeric fallback reason and HRESULT in the control ABI,
clear `consumer_ready`, and make the producer resume CPU publication on the
next frame. Telemetry includes selected path, fallback, adapter LUID, epoch,
sequences, waits, CPU upload, GPU copy/readback, gaps, retries, torn reads, and
frame age. Because DirectShow exposes an `IMediaSample` system-memory buffer,
the consumer-side `Map` and row copies are required for this boundary; this is
not a zero-copy DirectShow path and hardware A/B evidence is required before
calling the experimental attempt an optimization. The measured DirectShow
NVENC run still observed fallback `HRESULT 258` and a +8.87 ms p95 candidate,
so this revision deliberately does not pay the D3D11 probe on the nominal path.

The consumer-side keyed-mutex probe intentionally uses a zero-millisecond
timeout. `HRESULT 258` (`WAIT_TIMEOUT`) therefore costs one non-blocking probe,
not a 2 ms sleep; it remains fail-closed and is visible in `fallback_hresult`.
