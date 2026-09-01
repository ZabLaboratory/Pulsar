# D3D11 return transport qualification

Status: opt-in and fail-closed for ProgramReturn and PreviewReturn.

Patch `0024` adds a separate D3D11 control mapping and three-slot NV12 texture
ring for each return lane. The outward DirectShow sample remains ordinary
system-memory NV12. The default remains the existing CPU seqlock queue.

The transport is enabled only with `PULSAR_RETURN_TRANSPORT=d3d11` and only for
1920x1080 NV12. It duplicates NT handles into the consumer process after the
consumer publishes PID/session, records the producer adapter LUID and epoch,
uses keyed mutex key 0→1/1→0 ownership, and polls a D3D11 event query with a
2 ms deadline before readback. A borrowed frame pointer is copied into a
tightly-packed upload vector synchronously; no pointer crosses the callback.

The current implementation is a bounded GPU transport, not a zero-copy path:
the `raw_video` callback still receives OBS's converted system-memory NV12 frame,
copies it synchronously into the shared texture, and the consumer reads it back
into the ordinary DirectShow sample. It does not render a second composition and
does not retain a borrowed CPU pointer asynchronously. A future producer-side
GPU texture hand-off must be a separate architectural change.

The implementation satisfies these gates:

- explicit `PULSAR_RETURN_TRANSPORT=d3d11` opt-in and independent ProgramReturn
  and PreviewReturn control maps/rings;
- three NV12 textures per lane, fixed-width `uint64_t` duplicated handles,
  monotonic sequence/epoch metadata, matching adapter LUID, and bounded keyed
  mutex waits;
- consumer-side GPU copy/readback into the existing system-memory NV12 sample
  while preserving frame ID, PTS, revisions, and correlation identifiers;
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
sequences, waits, copy/readback, gaps, retries, torn reads, and frame age.
