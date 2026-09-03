# AGENT_REPORT / CLOSURE_REPORT — Probe-254 c4c7 continuation

Role `probe`; display `Probe-254-Final-Rerun`; thread `probe-254-final-rerun`; work unit `ZabLaboratory/Pulsar#254`; ADR `ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane`. No product edits.

## Exact provenance

- Head: `c4c7bbc5c9cba5f20cd583693617562108869900`.
- CI: pipelines `33783285508` and compliance `33783285497`, all required jobs SUCCESS including build/export/CEF/offline CTest 15/15.
- Artifact `pulsar-rundir` id `9904790004`, 290020139 bytes, SHA-256 `c2c019904f07cf076626b2dde63bec1fda543fe4ec773b8c17299180cc75c38d`.
- Extracted executable SHA-256 `38e5f4d702cdb70b93c9d6f9f6f3f20954d5bdbf15d56211d3b97e266d1e5b62`.

## Independent runtime result

The exact c4c7 executable was used for every probe. The required dual-lane x264 and NVENC campaigns were each attempted three/one times as applicable with 100 warmup + 100 measured requested, WGC+CEF, DirectShow and RTMP enabled. Every attempt stopped before Take 001 at the mandatory WGC non-black gate: `probe-dual-lane-wgc-A` stayed black for 20 s (`distinct=1`, `nonblack_ratio=0.0`, `all_same=true`, `sampled=40659`). No raw/DirectShow/RTMP SLO or 200-Take result is promoted for c4c7. Sidecars and RTMP diagnostics are retained under `C:\Users\Mathias\probe254-evidence\c4c7-final-x264*` and `c4c7-final-nvenc`.

This is a host/runtime capture blocker, not an SLO pass or product failure: the same WGC gate failed identically for both codecs before any measured Take.

## Independent probes that completed

- x264 rollback PASS: frame 115, Program preserved, mutations frozen, stable encoder/video binding count 1, runtime and DirectShow leases observable through release. Recording SHA-256 `5482d0b1c48ff2a00b0f49d3f6d405ac02104ccc29b077e6d044faaf3866a2b5`.
- NVENC rollback PASS: frame 223, same no-hot-rebind and lease assertions. Recording SHA-256 `7defd22cd850951969092fb4768ffb3aa429770e593680d0316b94c6687ed41f`.
- Replay PASS: off-air refused, on-air save produced readable H.264/AAC 1920x1080 MP4 (3,281,343 B, 4.33 s), stop/release completed.
- Capability contract PASS: 54/137 advertised gated coverage, no unexcused effect mismatch, process reaped cleanly.
- Program audio PASS x264 and NVENC, 100 Cuts each; AAC 48 kHz, stable route/output identities, monotone continuous audio PTS, Preview video mutation isolated. Evidence SHA-256 x264 `f3cef9e41632c5cbb4dcfc454ca944555fe71793372e84d823c8f1dea626e9c5`; NVENC `4abdbe147a871832bc3e2d2445fafebb5aa3230e5c413795b2c0ae2bec99f043`.
- NVENC quality A/B PASS for broadcast, complex and chaotic cases; report SHA-256 `4cac0789a6e3a8003d3fbaee4f3efb00f76ee918c966b1b62fd6c2263317c8fc`.
- Final process check: no `pulsar` or `ffmpeg` process remained.

## Criteria and global boundaries

- Issue #254 raw/DirectShow/RTMP target-load SLO: `UNPROVEN` on c4c7 because mandatory WGC gate blocked before Takes.
- Recovery/rollback/no-hot-rebind and runtime/DirectShow lease release: PASS for both codecs.
- Namespace/mapping/lease contract: PASS via exact CI CTest 15/15, capability contract and rollback traces. Stale/squatted `ConsumerActive` without registration versus PID/session/token registration is covered by contract/static evidence; no separate hostile runtime injection was available in the provided probe harness.
- Resource growth/saturation: `UNPROVEN`; dual-lane-only excludes the single-lane baseline and no active-encoder+RTMP sample set was obtained.
- Global AC-12a: `UNPROVEN` for c4c7 (no RTMP Take samples); AC-12b likewise not promotable for this candidate. AC-13: `UNPROVEN` by explicit no-single-lane scope.

Status: `AGENT_REPORT COMPLETE`, `CLOSURE_REPORT COMPLETE`, candidate not promotable for end-to-end SLO due reproducible WGC host blocker. No superseded e028 evidence reused.
