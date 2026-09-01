# D3D11 return transport qualification

Status: `NEEDS_ARCHITECTURE` for ProgramReturn and PreviewReturn.

Patch `0024` carries a compile-time capability contract only. It does not open a
D3D11 resource, publish a GPU handle, add a DirectShow media type, or retain a
borrowed frame. The existing three-slot CPU seqlock NV12 queue therefore remains
the only selected path and keeps ordinary DirectShow applications compatible.

The future transport must satisfy all of these gates in one reviewed change:

- an explicit `PULSAR_RETURN_TRANSPORT=d3d11` opt-in and a capability proof for
  both return lanes;
- at least three NV12 textures, fixed-width `uint64_t` duplicated handles,
  monotonic sequence/epoch metadata, matching adapter LUID, and bounded keyed
  mutex or fence waits;
- a consumer-side GPU copy/readback into the existing system-memory NV12 sample
  while preserving frame ID, PTS, and revisions;
- observable fallback on creation/open, adapter, device-removed, timeout,
  format, and inter-bitness failures, without stopping Program, Preview, audio,
  encoding, RTMP, or readiness;
- proof that the existing converted Program/Preview texture or readback is
  copied once and that the Program NVENC texture lifetime is unchanged.

The current OBS D3D11 exports use a 32-bit shared-handle ABI and the DirectShow
consumer emits ordinary system-memory NV12 samples. Selecting a GPU path before
those contracts are adapted would be a false readiness signal, so a forced
D3D11 request reports `architecture_required` with `E_NOTIMPL` and continues on
CPU. P010 reports `format_unsupported` and also continues on CPU.
