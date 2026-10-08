# Continuous cockpit Preview transition

Author: Eleven, thread `01a11b19-56f8-7e33-a1c7-9ddfe1bb96c7`.
Authority: requested transition repair, no ADR or Computer Use; existing authority
to push the bounded change and activate/test it locally. Dedicated existing
branch `eleven/local-20261008-webm-transition`. Canonical checkout preserved.

## Cause and change

The preceding Preview protocol paused the configured media at the cut point
while Prism prepared Orion and reconciled native captures. Observed preparation
latency was therefore inserted in the middle of the animated asset.

BeginPreview now installs a private overlay and captures the outgoing composite
in one canvas-sized GPU texture. The live composite stays hot behind that image.
The cover is acknowledged only after capture and first-frame decoder readiness.
EndPreview(false), after host preparation, starts uninterrupted playback at a
native frame boundary. The media clock reveals the incoming composite at the
configured cut without pausing or seeking. Completion/abort restore PreviewView
and release the snapshot, overlay and decoder. Begin/End/runtime/command wire
shapes and host sequencing remain compatible with the existing Prism host.

An isolated test exposed late ffmpeg stop callbacks truncating immediate replay.
Each Preview operation now owns a fresh private decoder cloned from the
configured media settings; previous Preview/Take callbacks cannot stop it.
The configured Take stinger, Program audio, same-lane role map and no-default
asset behavior are preserved. Preview media remains muted.

## Criteria and evidence

| Criterion | Risk | Command/proof | Result |
|---|---|---|---|
| Delayed preparation happens before playback | Mid-video pause | `python scripts/probe-preview-transition.py --exe build-upstream/rundir/RelWithDebInfo/bin/64bit/pulsar.exe --output build-transition/continuous-preview-fixtures --report evidence/local-20261008-webm-transition/eleven/20261008T204416Z-continuous-native.json` | PASS: preparation delays 800/0/350 ms; continuous clock through cut |
| Immediate replay and both abort phases | Late decoder callbacks or retained views | Same synthetic native probe; source destruction observed for all five snapshot/decoder pairs | PASS: 3 completed + 2 aborted; graceful exit 0, no forced native kill |
| Program/Take, alpha, audio and configuration behavior | Shared stinger regression | `python scripts/probe-configurable-transition.py --exe build-upstream/rundir/RelWithDebInfo/bin/64bit/pulsar.exe --output build-transition/continuous-regression-fixtures`; `20261008T204800Z-native-regressions.json` | PASS: same-lane Program/Preview, alpha/audio, TakeCommitted frame/PTS, abort/replay, replace, mute/gain, MP4, clear, invalid media |
| Native protocol and lifecycle | Contract/admission/shutdown regression | `20261008T204747Z-scene-contracts.txt`, `20261008T204716Z-ctest.txt` | 101 pytest contracts + 4 selected CTest checks PASS |
| Actual Prism native return | Mock-only continuity claim | Prism branch evidence `evidence/local-20261008-media-transition/eleven/20261008T205453Z-continuous-rail.json` and two captured native video PNGs | PASS: editable -> Launch -> editable, cut windows advance 500/520 ms, 8/9 distinct pixel hashes, 29/30 HTML video frames, no media pause |
| Activated candidate and idle restoration | Different runtime/binary, operator state lost | `20261008T205739Z-active-runtime.json` | Exact candidate SHA below; configured/ready/operational, busy false, original selection restored, native video 1920x1080 |

Native build: `cmake --build build-transition --config RelWithDebInfo --target
pulsar-headless --parallel 6`, successful. Selected CTest: transition-controller,
media-config, RTWQ lifecycle and headless WebSocket quiesce, all four passed.
The earlier unrelated CEF grace fixture requires a lifecycle marker absent from
the bundled browser DLL; its previously documented baseline limitation was not
changed, bypassed or retested as part of this Preview repair.

The two initial failed synthetic attempts are retained as
`20261008T204015Z-native-first-failure.json` and
`20261008T204136Z-native-replay-failure.json`, with their distinct previous
candidate hash. They led to decoder isolation; the final native probe passed.

## Activation and rollback

Active executable SHA-256:
`fac3466e90a744bff14ca68dac2967e5a51ecd610e9fcf1c1381f8b203299ac8`.
WebSocket DLL remains
`2604e4038f873104aefafd9ec23d24aa4887229d60a04184d009206a7d53288d`.
Only `pulsar.exe` was updated in the existing approved runtime directory
`C:/Users/Mathias/AppData/Roaming/prism-dev/components/pulsar/active`.

The first same-host engine shutdown/ensure attempt reused stale Preview/runtime
state (`20261008T204920Z-engine-activation-attempt.json`). A fresh Main startup
restored readiness. Native PID 20356 exited before the already-quitting Main
PID 5392 required targeted termination. No broad process kill was used.
The same active scene-sync session source snapshot/environment were preserved;
none of its product files were edited. Previously authorized
`PC-LM1E Camera (0458:6006)` was mapped again through the existing startup IPC.
Final Main PID 33260/native PID 44836 remained idle, with no active destinations
or recording. Inspector wrappers were restored and the temporary engine binding
removed after the real rail proof.

Rollback pair: `D:/Documents/Zab/Artifacts/backups/2026-10-08/pulsar-continuous-preview/`
(`pulsar.exe`, `obs-websocket.dll`). Stop the idle session gracefully, restore the
pair to its existing runtime paths, and start that same session. No media
preference or supplied WebM was changed.

## Limits

Actual Live was not started. The broad media regression probe recorded only
synthetic colors/tone in its isolated process; the user's Prism session was not
recorded. During preparation the outgoing scene is deliberately a captured
still; the transition video begins only after readiness. Opacity at the chosen
cut point remains a property of the operator's asset. The measured native and
pixel evidence proves the two tested rail switches, not all possible files,
hardware loads or scene graphs. Local validation is distinct from CI or a main
branch merge. Worktree cleanup remains policy-blocked from the prior task;
no new cleanup attempt was made.
