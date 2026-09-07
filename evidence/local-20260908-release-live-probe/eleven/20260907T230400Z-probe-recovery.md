# Release live-probe namespace correction

Work unit: local-20260908-release-live-probe. Owner: Eleven.
Authority: complete the requested Pulsar 3.0.0 release; repair its proven harness
failure without changing the published tag or relaxing broadcast requirements.
Base: 9bb72e9d2373f0bef11128f19864ab223ec62042 (v3.0.0).

The tag pipeline 34167738186 passed build, native CTest, binary exports, packaging,
contracts, and npm publication. Its Twitch job failed before creating a destination:
the probe waited for config beside the binary, while native bootstrap writes into
an isolated runtime directory. GitHub Release attachment was consequently skipped.

## Bounded change and criterion matrix

| Criterion | Risk | Check | Observed evidence |
|---|---|---|---|
| Child and config reader use the same namespace | Startup timeout | Explicit runtime spawn/config test | Pass |
| Default runs do not share state | Stale config/collision | Unique generated paths test | Pass |
| Caller-owned diagnostics survive | Unintended deletion | Main preserves explicit path | Pass |
| Temporary state is cleaned on error | Abandoned session config | Main failure cleanup test | Pass |
| Duration, FPS, resolution, bitrate, return code preserved | Weakened live gate | Spawn and main tests | 600 s / 60 FPS / 1920x1080 / 6000 kbit/s; failure propagated |

Local command: `python -m pytest scripts/test_probe_twitch_live.py scripts/test_m10_setup.py scripts/test_probe_m10_real_orion.py -q`.
Result: 24 passed. `git diff --check`: pass.

The workflow supplies a runner-local runtime directory to both the probe and its
existing deny-by-default redacted-config staging step. No secret value, credential,
authentication requirement, assertion threshold, binary, or SDK is modified.
The new four offline tests are included in the existing required contract gate.

The immutable v3.0.0 archive is separately undergoing the full original 600-second
live probe on physical hardware with only explicit runtime-location adaptation.
That operator recovery must be reported as such, not as a green original tag job.
This document does not pre-claim its completion.

Rollback: revert this harness-only commit; that restores the known startup failure,
not a working release path. The signed v3.0.0 tag and npm versions are immutable.
