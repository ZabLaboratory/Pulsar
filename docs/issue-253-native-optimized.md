# Issue 253 — native CPU readback optimization

## Delivered result

On the qualified Windows 1920x1080, 60/1 fps, NV12 CPU-encoding path, the native libobs current-frame readback is now the default. It removes a staging-frame delay while preserving the audio/video media timeline. The GPU-encoding path retains its existing selection. No codec preset, bitrate, B-frame count, audio format or compression setting was reduced to obtain this gain.

The measured boundary is **Take accepted → first fully decoded image containing the changed Program marker**, not merely an encoded packet, decoder submission, or a physical display. Tests use the same native software RTMP receiver before and after, a visible WGC source and a CEF source, two live lanes, local RTMP, and 100 warm-up plus 100 measured Cuts per trial.

| x264 trial | Native readback | First changed decoded image p95 |
| --- | --- | ---: |
| Reference 1 | Previous frame, explicit `0` | 45.947 ms |
| Reference 2 | Previous frame, explicit `0` | 45.897 ms |
| Candidate 1 | Current frame, explicit `1` | 33.045 ms |
| Candidate 2 | Current frame, explicit `1` | 35.762 ms |
| Production default | No override | 37.982 ms |

Across these 500 measured Cuts, the candidate improves this p95 by approximately **8–13 ms (17–28%)**. These are trial results on this machine, not universal minimum latency or a confidence interval. Canonical evidence archive: `evidence/253/eleven/20260907T163000Z-native-run-evidence.zip`.

## Native changes and measurement integrity

- Patch 0053 restores the current readback's presentation timestamp to the matching audio timeline, retaining content timestamps separately.
- Patch 0055 enables the default only for the qualified Windows CPU path, resolution, frame rate and format. Explicit `PULSAR_RAW_CURRENT_READBACK=0` retains a diagnostic rollback.
- Native per-packet content audit follows renderer catch-up and duplicated pictures, avoiding invalid linear extrapolation between content time and encoded time. Telemetry remains separately opt-in.
- A single-owner libavformat/libavcodec diagnostic receiver records the producer's QPC clock at actual packet/frame return. The observation bound is one QPC tick (100 ns on this machine). This receiver is a measurement tool, not the product speedup.
- Offline receiver validation compared every visible YUV pixel hash and presentation timestamp for 876 video frames against FFmpeg; all matched. The abandoned reference file containing audio is not used as video evidence.
- Patch 0054 defers fatal GPU encoder stop to the existing destruction task queue, avoiding self-wait on the encoding thread. The native GPU failure test requires the encoder to become inactive and the same encoder/output to restart successfully.

## Preserved behavior and rejected experiments

The ProgramAudio probe passes 100 Cuts each for x264 and NVENC, maintaining common-route identity, isolated Preview mutation, AAC 48 kHz output and continuous monotone AAC PTS. Flash/click checks inspect decoded audio and video against a calibrated zero-offset source; the recorded offsets are reported with the final evidence, not represented as physical display lip-sync or guaranteed zero phase.

Final verification: 236 Python tests pass, one is skipped. CTest passes 21/22, including native asynchronous drain, fatal-error stop/restart, content timestamps and lifecycle checks. The remaining Windows directory-hardening test correctly fails because this runner lacks `SeRestorePrivilege` for two owner-substitution gestures; neither assertion is disabled. This is not a globally green security suite.

The packaged runtime passes an additional five-measured-Cut x264 smoke (30.192 ms marker p95, **not** a 100-Cut performance claim) and 100-measured-Cut NVENC regression (74.860 ms marker p95, AC-13 capacity still unproven). The promoted readback flash/click trials measured +5.000, -8.667 and +15.333 ms; the last was the packaged runtime, with a contemporaneous reference at +14.333 ms. All are within one 60 Hz frame plus the 1 ms detector sampling bound. Variation between source-start phases is retained in the evidence.

Native asynchronous NVENC completion was implemented and tested but is **off by default**: its measured first-changed-frame p95 was 77.478 ms versus 72.505 ms with it disabled, despite a lower median. Lower-B-frame quality alternatives were also rejected after quality thresholds failed on at least one source. Neither experiment is promoted as a performance improvement.

## Delivery scope

The full Windows runtime and signed local Git bundle are exported under `Artifacts/2026-09-07/pulsar-253-optimized/`. The code revision used for the production-default run is `5fba882446741f863744f68d1c5726aeb2dacc94`; later evidence-only commits do not change these binaries. There is no remote push, PR merge, issue closure, release publication or deployed-runtime claim in this local delivery.

Unproven scope includes 4K/other frame rates, capacity relative to a single-lane baseline, remote RTMP network/service delay and physical display latency. NVENC receives no claimed end-to-end latency improvement.
