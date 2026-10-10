# WebM / MP4 transition runtime integration

Authority: the user requested native transitions without an ADR, then requested Prism Settings, local activation, verification and a code push. No bundled default media, external broadcast or user-content recording was introduced.

This candidate combines the existing configurable WebM transition branch with `f56fcc2b04ada7bf4123bab21fe7e97907da0a35` (RTWQ initialization and expected process-stop classification). It adds MP4 selection and container validation using the same native decoder, plus a Windows compatibility guard for the optional source-profiler lease symbol. On the locally installed older libobs, GetSourceStats reports NotReady instead of preventing the entire WebSocket plugin from loading. A full newer libobs provides the leased telemetry API.

## Evidence

| Criterion | Observed result | Evidence |
|---|---|---|
| No implicit asset | Fresh runtime unconfigured | `20261008T151400Z-native-media.json`, default state |
| Native WebM with alpha and sound | Decoded synthetic Program frames and audio passed | same_program in the JSON and recordings in the ZIP |
| Same-lane switch and Preview isolation | Completed with frame/PTS; Preview did not change Program or leak transition sound | same_program / same_preview |
| Transactional Take | Correlated TakeCommitted, frame and PTS | take.committed_event |
| Replacement, abort/replay, mute/gain, clear, invalid input | Native probe passed | native-media JSON |
| MP4 | Decoder ready, same-lane command completed | mp4 in native-media JSON |
| Parser | WebM/MP4 accepted, 22 invalid configurations rejected | `20261008T154500Z-config-parser.txt` |
| Scene-control contract | 101 tests passed | `20261008T153900Z-contract-tests.txt` |

The contract suite uses the installer template from the exact staged upstream gitlink `25ed780b7f392a1690dfca929cd57679959cf3ad`, read from its Git object into private test/build source. An initial run against the unrelated canonical source junction exposed an outdated installer template; no test expectation or canonical source was changed to resolve it.

The native decoder evidence was moved out of a nested staging directory into `20261008T154600Z-native-media.zip` (SHA-256 `0468C630DC5050FD44E82D254A4EB411B4E0CBCC21DAE3F74C320AC528E79420`). Paths inside the original result retain their original execution location; the archive preserves those fixture/recording basenames. Recordings contain only generated colors, generated transition imagery and generated sound, in an isolated test runtime.

## Local activation

The existing Prism-approved runtime location was retained. Installed candidate hashes:

- pulsar.exe: `E255CA6767C778532B84B1F2775C8AA1F3C30BC578D6C5BDF60AEC30B67A4BB0`
- obs-websocket.dll: `7EF5CA7B1DBEE9696EECCB27BD38DBB567B537CED21EB2B3D8F50F37E1822DFA`

The previous two files are backed up in `D:/Documents/Zab/Artifacts/backups/2026-10-08/pulsar-media-settings`. Rollback requires stopping only the idle Prism runtime, restoring those files, then restarting Prism. No firewall rule was changed.

Prism owns the UI/restart/current-runtime evidence under `evidence/local-20261008-media-transition/eleven/20261008T154700Z-settings-report.md` in its feature branch. That proof includes real WebM and MP4 decoder readiness, full app restart restoration, and two same-lane switches completed in the runtime hosted by Prism, with the original Program scene restored and temporary scenes removed.

No npm release, tag, main merge, Live transmission or user-content recording was performed. Local binary activation is separate from release and CI status.
