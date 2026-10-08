# Restore the real 1080p60 qualification and bound Pages previews

Owner: Eleven. Work unit: `local-20260908-twitch-x264`.
Thread: `01a045e2-996f-7913-99f0-2cee24ee86ee`.
Base: main `2ba44b47766a1c2672f3d6e7eb093d2e388248ee`.
Authority: user explicitly requests 1920x1080, 60 FPS, 6000 kbps; earlier x264
selection remains in force. No lower-profile fallback is acceptable.

## Findings from the completed hosted run

Run [34187415384](https://github.com/ZabLaboratory/Pulsar/actions/runs/34187415384)
completed its live job: x264 allocation, 600 seconds, 120 active-destination
polls, 298 adaptive samples, zero output/network drops, successful StopDestination
and finalized MP4. Twitch's player visibly showed the animated source at 00:40,
02:05, 05:26 and 08:51. Five sampled recording segments were non-black and moving.

This is **not a 1080p60 qualification**. The MP4 is 1280x720 at a nominal 60 FPS,
but the native renderer averaged 26.7039 FPS (minimum 20.3226), with 19,905 skipped
render frames out of 36,045 and mean CPU usage 95.59%. The native boot parser
accepts only 24/30/48/60/120; the previous hosted 15-FPS request silently fell
back to 60. The probe's threshold still used the requested 15. That mismatch
incorrectly admitted the low-cadence run. No claim of sustained 60-FPS rendering
is made for it.

The run's remaining failure is independently proven: GitHub rejected its
122.01-MiB MP4 from gh-pages with `GH001`, over the 100-MiB Git blob limit.
This was publication failure after successful transport/record finalization.

## Changes and criterion -> risk -> validation matrix

| Criterion | Risk | Validation | Observed result |
| --- | --- | --- | --- |
| 1080p60 / 6000 kbps / x264 on every runner | Silent quality downgrade | Workflow constants; default FPS=60 (explicit supported operator FPS remains possible) | No GPU-dependent geometry/bitrate/FPS fallback |
| Native FPS must accept the request | Probe/native drift | FPS allowlist contract reads native parser; unsupported 15 rejected before spawn | Local regression passed |
| Native settings actually match | Env request mistaken for applied setting | GetVideoSettings reads obs_get_video_info; geometry and numerator/denominator checked | Local runtime returned 1920x1080, 60/1 |
| 27 FPS cannot pass a 60-FPS qualification | Ten minutes of invalid evidence | Existing 90% cadence threshold also applied after 30 s, with warmup exclusion | 27 fails; stable 60 after one warmup sample passes |
| Full proof retained, Pages copy below Git limit | Proof degradation or another GH001 | Separate disjoint output directory, source SHA before/after, 60-MiB target and hard 90-MiB per-file gate | Full 127,940,994-byte proof unchanged; 60,722,687-byte preview |
| Preview is explicitly not full proof | Misleading viewing derivative | README, source-run link and source/preview manifest; full artifact untouched | 960x540@15 preview, 600.277 s duration |
| No black/frozen preview | Broken transcoding | Five 2-second segments at 5/30/120/300/590, grayscale 96x54 at 4 FPS, spatial/temporal metrics; rendered frame inspected | All five passed, frame visibly retained the animated mire |
| Offline contracts and patch hygiene | Regression | `python -m pytest scripts/test_probe_twitch_live.py scripts/test_ci_native_cache.py scripts/test_prepare_pages_proof.py -q`; YAML parse; `git diff --check` | 33 passed in 0.79 s; YAML/diff clean |

## Local native 1080p60 check

The unchanged preserved native runtime was launched on the local GPU-equipped
Windows host using x264, 1920x1080, 60 FPS, 6000 kbps and the animated ffmpeg_source.
It recorded locally without a network destination. Six five-second observations
returned 60.0000024 FPS; render times were 1.7880, 4.5098, 4.8932, 4.8297, 4.8189
and 4.5824 ms; CPU observations were 9.04-12.10%. StopRecord finalized the MP4.
This proves local native profile/readback/cadence, not hosted 1080p60 capability.
The separate earlier exact-tag 600-second GPU live proof remains unchanged.

## Original and Pages-preview hashes

- Complete hosted proof SHA256: `ce240219591efcf6f9ef45e17614431b346f30e078037245cb310534d8867c76`.
- Pages preview SHA256: `872b8ccc601d211ce87d3177de3520c1a36c05fc5292e7eff3adef4658dfdc2b`.
- Preview samples: minimum spatial luma stddev 52.5191; mean adjacent-frame
  differences 3.1874 / 4.0072 / 2.6533 / 2.6406 / 3.9365 at the five timestamps.
- Preview generation refuses overlapping/nonempty destinations, mismatched source
  aliases, altered source hashes, duration loss above one second and oversized output.

## Limits and rollback

Hosted 1080p60 execution after this patch and actual revised gh-pages publication
are pending at commit time. The repository has no registered self-hosted runners
(live API readback: total_count=0). A hosted runner that cannot sustain the requested
profile must fail; no GPU runner registration or infrastructure purchase is authorized
or performed by this patch. The unrelated local vod_local reentrancy stall remains
documented in the preceding readiness report; no native code is changed here.

Rollback: revert this workflow/probe/preview change. Full release binaries, signed
v3.0.0 tag, npm 3.0.0 packages and the full recorded proof are not overwritten.
