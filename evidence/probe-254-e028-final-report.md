# AGENT_REPORT / CLOSURE_REPORT — Probe-254-Final-Rerun

Role `probe`; display `Probe-254-Final-Rerun`; thread `probe-254-final-rerun`; work unit `ZabLaboratory/Pulsar#254`; ADR `ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane`. No product edits.

## Provenance and gates

- Exact PR head: `e0286716261f5e9ac32cc9a68893a0bebfe70213`.
- Pipeline: `33781343167`, SUCCESS; build, CTest 15/15, CEF, export, lint, contracts and compliance green.
- Exact artifact: `pulsar-rundir`, id `9903980172`, 290475438 bytes, SHA-256 `e28e451564ae98efee4759f7b26dd8e516292646e64255c97d619294bd50d933`.
- Extracted executable SHA-256: `94f44e0ee0db0ca4a4b81af149b86be3ff1ae6986c739523c160f25181c66ace`.
- Executable path: `D:\Documents\Zab\pulsar-e0286716-artifact\upstream\build_x64\rundir\RelWithDebInfo\bin\64bit\pulsar.exe`.

## Independent dual-lane campaign

Both final runs used `--takes 100` (100 warmup + 100 measured, 200 total), WGC + CEF workloads, duplicated WGC/CEF producer instances, `topology=dual_lane_ab`, producer_count=2, DirectShow consumer and native loopback RTMP receiver. No single-lane reference was run.

| Codec | Result | Raw p95 | DirectShow p95 | Encoded p95 | RTMP p95 | Recording |
|---|---|---:|---:|---:|---:|---|
| x264 | PASS 200/200; WGC/CEF A+B; monotone frame/PTS | 35.35612 ms | 31.887855 ms | 43.05421 ms | 20.07363 ms | H.264 1920x1080 60fps, AAC; 18,540,049 B |
| NVENC | PASS 200/200; WGC/CEF A+B; monotone frame/PTS | 37.673765 ms | 40.87664 ms | 73.82559 ms | 18.647055 ms | H.264 1920x1080 60fps, AAC; 22,095,041 B |

Evidence hashes:

- x264 trace `4215303921edbb449d6d9b6dd97e37c70f56a3fd4a238f3a41d317e9926dfbcc`; latency report `db6ab6810de03abf4f4e25c0eee016c8b1d9443ec171359968c811010265e0d1`; MP4 `e1da3ebf3b82b1208670e4d10ac82cfffef91b520f87f9986bc80cbaa0de7f3`.
- NVENC trace `66c74b450727f97199c64924cf7e7279b243ae26f3b43e9b1fc1d9d0b360b0ee`; latency report `4687073ae3752e6960764cd34902b44f73d7fb0b4d18f7dfc6db1448f872f8bf`; MP4 `57f2dbd4d93219a4c4bc65e9b08393ccb6f7989bf2d936aba666968d67e094cc`.

## Recovery, rollback, replay, audio, quality

- x264 rollback: PASS; rollback committed at frame 58/PTS `410090781604162`; Program preserved, mutations frozen, stable binding count 1, runtime and DirectShow leases observable through release. MP4 SHA-256 `1d8c109b78cbc2e59fa7520ad4075215a10266414ef1fc59e0dd70841463cdb`.
- NVENC rollback: PASS; rollback committed at frame 60/PTS `410110762512494`; same no-hot-rebind and lease assertions. MP4 SHA-256 `f64b040c79888c55e3092e384df85f03464ae133981ffbc3764f34fd9c42cd92`.
- Replay: PASS; off-air arm refused, on-air arm/save succeeded, readable H.264/AAC 1920x1080 MP4 (3,238,480 B, 4.28 s), stop/release completed.
- Program audio x264 and NVENC: PASS, 100 Cuts each; stable route/output identities, AAC 48 kHz, monotone continuous audio PTS, Preview video mutation isolated. Evidence SHA-256: x264 `719eed6938799885a6be6f50d2be9b5738858df9de15d7bbc765d9e90e44b82f`; NVENC `bc3c88023b738ddb561c3174fe7a6335864f31445836c69c5d95b6714244eff5`.
- NVENC quality gate: PASS; broadcast VMAF +0.041 / PSNR -0.175 dB / SSIM +0.000483, complex +4.681 / +0.546 / +0.005146, chaotic +1.703 / +0.102 / +0.013752. Report SHA-256 `4cac0789a6e3a8003d3fbaee4f3efb00f76ee918c966b1b62fd6c2263317c8fc`.
- Capability contract: PASS, 54/137 advertised gated coverage, no unexcused effect mismatch; process reaped cleanly.
- Clean shutdown: both main runs and rollback probes completed bounded cleanup; final process check found no `pulsar` or `ffmpeg` process.

## Criteria verdicts

Issue #254: (1) raw/DirectShow target-load evidence PASS for x264 and NVENC; (2) no-hot-rebind rollback/recovery PASS; (3) namespace/mapping/lease contract coverage PASS via exact CTest 15/15, capability contract, dual-lane traces and rollback leases; adversarial stale/squatted ConsumerActive cases are contract/static evidence only, not a separate hostile runtime injection; (4) resource growth/saturation NOT PROVEN because dual-lane-only forbids the single-lane baseline required by the sampler/AC-13 comparison, and dual-only sampler run produced no complete active-encoder+RTMP-load samples.

Global AC-12/AC-13: AC-12b take/receiver correlation is present for 100 measured takes per codec; conservative AC-12a RTMP callback-to-receiver p95 FAILS for x264 (20.074 ms) and NVENC (18.647 ms) against <=15 ms. Raw/DirectShow/encoded boundaries pass their applicable gates. AC-13 is UNPROVEN by explicit scope: no single-lane reference phase was executed.

## Limitations and residual risk

The first x264 attempt was rejected by the WGC non-black gate because the visible Chrome title changed; a fresh retry with the observed title passed. A dual-only resource sampler retry passed WGC/CEF but returned `SKIP` for no complete active encoder+RTMP samples. No prior candidate evidence is promoted. RTMP latency remains an open global performance issue; the issue-scoped raw/DirectShow and recovery verdicts are independent of that global failure.

Status: `AGENT_REPORT COMPLETE`; scoped issue evidence ready for review, with global AC-12a FAIL and AC-13 UNPROVEN explicitly retained.
