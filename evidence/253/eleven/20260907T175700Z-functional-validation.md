# Pulsar #253 — final functional validation

Date: 2026-09-07

Candidate branch: `eleven/253-latency`

Candidate source HEAD before this evidence-only commit: `e680a1cc9af84d85390b6336b5fe0246d967f138`

Runtime code revision: `5fba882446741f863744f68d1c5726aeb2dacc94`

## Closure verdict

The issue's three explicit acceptance criteria are satisfied on the qualified Windows
1920x1080@60 path:

- raw p95 remains below 50 ms and DirectShow-return p95 remains below 75 ms;
- raw, DirectShow-return, encoded callback, RTMP demux, and native decoded-frame
  timestamps remain separate and frame-correlated;
- the promoted x264 CPU-readback improvement is supported by like-for-like campaigns
  using the same native decoder observer.

The first changed decoded-image p95 improves from 45.897–45.947 ms to
33.045–37.982 ms, a reduction of approximately 8–13 ms (17–28%). The result uses
500 measured Cuts after 500 warm-up Cuts across two reference and three candidate
runs. The qualified path is enabled automatically only for Windows, CPU readback,
NV12, 1920x1080@60. NVENC async output and lower-B-frame candidates remain disabled
because they did not pass p95 or quality gates.

## Regression and media evidence

- Python regression: 236 passed, 1 skipped.
- Native CTest: 21 of 22 passed. The remaining directory-hardening probe is blocked
  by the host not assigning `SeRestorePrivilege` for two ownership-change gestures;
  its requirement was not weakened.
- Audio: 100 x264 Cuts and 100 NVENC Cuts retained continuous, monotonic AAC 48 kHz
  timestamps and stable routing.
- Native decode identity: 876 of 876 video frames matched the reference full visible
  plane MD5 and presentation timestamp.
- Decoded flash/click A/V offset stayed within one 60 Hz video frame plus the 1 ms
  detector tick on the promoted and packaged paths.
- GPU error lifecycle: a forced pending-packet failure stops the encoder, then the same
  encoder/output pair restarts and drains reordered packets successfully.

## Real broadcast validation

The exported full runtime completed three real Twitch broadcasts totalling 245 seconds:

| Broadcast | Duration | Mean FPS | Render mean / p95 | Dropped render/output frames |
| --- | ---: | ---: | ---: | ---: |
| Primary | 120 s | 60.000 | 4.635 / 5.928 ms | 0 / 0 |
| Helix confirmation | 75 s | 60.000 | 3.287 / 4.443 ms | 0 / 0 |
| Public HLS playback | 50 s | 60.000 | 2.751 / 3.866 ms | 0 / 0 |

Each run loaded the local HTML, React, Babel and JSX page through CEF, streamed H.264
1920x1080@60 plus non-silent AAC stereo 48 kHz, recorded the same program locally,
and stopped cleanly. Twitch Helix reported the channel live during the confirmation
runs. Streamlink resolved the public playlist and FFmpeg decoded eight seconds with
both video and audio. The destination was removed after each run, and no Pulsar or
FFmpeg process remained.

Secrets were redacted and excluded. The stream key, OAuth credentials, WebSocket
passwords, private account data, and the DPAPI-protected trace key are not present in
the committed evidence or exported runtime.

## Integration boundary

This file establishes local technical closure readiness. Remote closure still requires
the exact signed candidate to be pushed, reviewed through a pull request, and accepted
by the repository's required CI and review gates. Merge, release, deployment, and issue
closure are intentionally not asserted here.
