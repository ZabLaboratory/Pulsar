# Hosted Twitch x264 correction

- Work unit: `local-20260908-twitch-x264`
- Base: `c9c9980d12387edc42411c99b4562a1bccacdb95`
- Failed recovery run: `34180380347` (cancelled after direct Twitch inspection showed the new 720p15 CEF stream was still black)

## Root cause refinement

Reducing the declared geometry and cadence removed the impossible 1080p60 load, but did not make CEF render on the GPU-less `windows-2022` runner. Twitch was live from the new run and still displayed a black player. The job was cancelled instead of allowing a misleading ten-minute result.

## Correction

On a hosted runner without a physical GPU, the live job now:

- forces `PULSAR_VIDEO_ENCODER=x264`;
- generates a deterministic animated 720p15 A/V fixture with FFmpeg and x264;
- installs that fixture as a looping, software-decoded `ffmpeg_source` on the active program scene;
- reads the source kind back through obs-websocket;
- requires the native allocation log to attest `video encoder allocated: family=x264`;
- retains the sustained-FPS and asynchronous StopRecord gates introduced in PR #291.

On a physical-GPU runner, the existing CEF browser source and automatic production encoder selection remain unchanged.

## Local validation

- `python -m pytest scripts/test_probe_twitch_live.py -q`: 10 passed.
- `python -m pytest scripts/test_m10_setup.py scripts/test_probe_m10_real_orion.py scripts/test_probe_twitch_live.py -q`: 30 passed.
- Real local fixture generation: 12-second 1280x720@15 H.264/AAC Matroska file, 7,522,548 bytes.
- `python -m py_compile scripts/probe-twitch-live.py`: passed.
- YAML parse of `.github/workflows/pipeline.yml` with PyYAML: passed.
- `git diff --check`: passed.

Completion still requires a fresh 600-second workflow run, direct Twitch viewer inspection, and inspection of the uploaded VOD/diagnostic artifact.
