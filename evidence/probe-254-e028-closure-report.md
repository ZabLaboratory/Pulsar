# CLOSURE_REPORT — Probe-254-Final-Rerun

Exact candidate `e0286716261f5e9ac32cc9a68893a0bebfe70213`; pipeline `33781343167` SUCCESS including CTest 15/15; artifact `9903980172` SHA-256 `e28e451564ae98efee4759f7b26dd8e516292646e64255c97d619294bd50d933`; executable SHA-256 `94f44e0ee0db0ca4a4b81af149b86be3ff1ae6986c739523c160f25181c66ace`.

Final independent dual-lane-only results:

- x264: 200/200 (100 warmup + 100 measured), WGC/CEF A+B, `dual_lane_ab`, monotone frame/PTS, H.264/AAC recording. Raw/DirectShow/encoded p95 `35.356/31.888/43.054 ms` PASS; RTMP p95 `20.074 ms` FAIL AC-12a.
- NVENC: 200/200, same gates and recording proof. Raw/DirectShow/encoded p95 `37.674/40.877/73.826 ms` PASS; RTMP p95 `18.647 ms` FAIL AC-12a.
- x264/NVENC rollback: PASS; Program preserved, state frozen, no hot rebind, leases observable through release.
- Replay: PASS; off-air refusal, on-air save, readable H.264/AAC MP4, clean stop.
- Program audio: PASS for both codecs, 100 Cuts each, stable route/output identities, AAC 48 kHz and monotone PTS.
- Capability contract: PASS 54/137 gated coverage; NVENC quality A/B: PASS.
- Clean shutdown: PASS; no remaining Pulsar/FFmpeg process.

Issue #254 verdict: raw/DirectShow SLO and rollback/recovery/lease criteria PASS; namespace/mapping/lease evidence PASS through exact CI contracts and runtime traces, with adversarial stale/squatted ConsumerActive only statically/contract-covered; resource growth/saturation NOT PROVEN because single-lane baseline is explicitly excluded and dual-only sampler had no complete active-encoder+RTMP samples.

Global verdict: AC-12a FAIL for both codecs at RTMP p95; AC-12b correlation present for 100 measured Takes per codec; AC-13 UNPROVEN because no single-lane reference was run. No superseded evidence promoted. Full hashes, paths, commands, and limitations are in `probe-254-e028-final-report.md`.
