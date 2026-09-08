# StartDestination boot-readiness continuation

Owner: Eleven. Work unit: `local-20260908-twitch-x264`.
Agent thread: `01a045e2-996f-7913-99f0-2cee24ee86ee`.
Base: main `47495ae772f54965bb8d434babae4a3bdafd4320`.

## Observed failure and bounded change

Hosted live run [34182546525](https://github.com/ZabLaboratory/Pulsar/actions/runs/34182546525/job/101926062170)
selected 1280x720 at 15 FPS, x264, 3000 kbps, and ffmpeg_source. Source creation
and readback succeeded. StartDestination then returned an accepted vendor envelope
with `started=false` and `encoders not bound on streaming output` at 03:20:42 UTC.

Native `DestinationRegistry::ensure_output` distinguishes a missing frontend
output from an output with missing video/audio encoder pointers. Both can be
observed during initialization. The old probe retried only the first state.
The change retries only these two exact readiness errors inside one monotonic
20-second budget. Each request timeout and sleep is capped by the remaining
budget. Other errors and rejected envelopes return immediately; unresolved
readiness remains a failure. No native code, encoder settings, FPS gate, source
policy, duration, image-health check or release gate is changed.

## Criteria, risks, commands and evidence

| Criterion | Risk | Command / evidence | Result |
| --- | --- | --- | --- |
| Missing output -> missing encoders -> started | Premature failure | `python -m pytest scripts/test_probe_twitch_live.py -q` | 14 passed, including the three-response sequence |
| Permanent missing encoder | Masked failure / unbounded retry | Same suite, 2.5-second simulated budget | Stops exactly at 2.5, calls capped to 2.5 / 1.5 / 0.5 seconds |
| Non-readiness error / rejected envelope | Inappropriate retry | Same suite | One attempt, no sleep |
| Native cache remains content-scoped | Unnecessary native rebuild | `python -m pytest scripts/test_probe_twitch_live.py scripts/test_ci_native_cache.py -q` | 21 passed in 0.57 seconds |
| Patch hygiene | Whitespace / accidental native changes | `git diff --check`, scoped diff | Passed; only probe, unit tests and this report |
| Actual hosted Twitch startup and pixels | Readiness may be permanently broken | New authorized live run after CI | Pending; unit tests are not native/Twitch proof |

## Local native diagnostic limit

The preserved native artifact from the build optimization was launched locally
with an isolated runtime and the same x264/720p15 profile, without a stream key
or network destination. Native logs attest `family=x264 id=obs_x264`, audio
allocation and the stable ProgramView binding. A `vod_local` destination did not
return from StartDestination within 20 seconds, both without and with an animated
ffmpeg source. This is **not a passing runtime/live test**. Processes were stopped.

Source inspection identifies a separate local-output reentrancy risk: start holds
the registry mutex across obs_output_start, while its start-signal callback also
takes that mutex. A synchronous file-output start can therefore stall; Twitch's
asynchronous connection path is different. This finding is recorded, not silently
fixed or used as evidence that the hosted readiness state will clear. No claim
of a successful live broadcast is made by this report.

Rollback: revert this probe/test/report commit; native binaries remain unchanged.
