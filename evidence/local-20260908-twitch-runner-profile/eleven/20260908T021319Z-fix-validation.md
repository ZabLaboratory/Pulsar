# Twitch hosted-runner profile correction

- Work unit: `local-20260908-twitch-runner-profile`
- Base: `d2213165cfb9261778e03ec68bf7aaad61297eb4`
- Scope: GitHub-hosted Twitch transport profile, render-cadence gate, and asynchronous recording finalisation.

## Reproduced cause

Workflow run `34177588475` used a GitHub-hosted `windows-2022` runner without a physical GPU while declaring `1920x1080@60` and 6000 kb/s. The live metrics remained near 16 fps with average render time near 62 ms, and the Twitch viewer starved. At teardown, `StopRecord` returned the typed accepted/pending code 702 while `outputActive` was still true; the probe incorrectly classified that accepted asynchronous stop as a rejection.

## Correction

- Detect physical GPU availability in the live job.
- Use an explicit `1280x720@15`, 3000 kb/s Twitch transport profile on software-rendered hosted runners.
- Keep the production `1920x1080@60`, 6000 kb/s profile on physical-GPU runners and keep hardware validation as a separate release proof.
- Fail when post-warmup `activeFps` averages below 90% of the declared profile.
- Treat only code 702 as an accepted pending recording stop, then poll `GetRecordStatus` for an inactive terminal state and final output path with a 30-second bound.

## Local validation

- `python -m pytest scripts/test_probe_twitch_live.py -q`: 9 passed.
- `python -m pytest scripts/test_m10_setup.py scripts/test_probe_m10_real_orion.py scripts/test_probe_twitch_live.py -q`: 29 passed.
- `python -m py_compile scripts/probe-twitch-live.py`: passed.
- YAML parse of `.github/workflows/pipeline.yml` with PyYAML: passed.
- `git diff --check`: passed.

The final authority remains a new full 600-second Twitch workflow run plus direct viewer and recorded-artifact inspection.
