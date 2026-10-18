# Persistent Preview transition correction

Owner: Eleven; existing `local-20261008-webm-transition` branch, continuing the user-authorized scene transition. Host/render proof belongs to Prism. No ADR, default media, stream, Computer Use or user-content recording was introduced.

Pulsar's staged `SwitchLane` operation did not apply to Prism's in-place Orion Preview updates. The frontend now provides `BeginPreview`, `GetPreviewResult` and `EndPreview`: a private scene overlays the actual bound `ZabPreviewComposite` in PreviewView, pauses the shared media decoder at the cut, then reopens and restores the composite at a native frame boundary. Program's bound scene and audio remain independent. Cover playback mutes only the media child. Existing Take and same-lane transitions are refused while the cover is active; native source reconciliation stays allowed. Abort and a native deadline restore the view even if the host abandons its request.

The existing decoder and atomic view-swap primitive are reused. `SetPreviewComposite` acknowledges the existing base without stripping a held cover. Outcomes correlate runtime/command IDs and provide start/terminal frame IDs and PTS. The contract documentation lists the lifecycle and distinguishes it from staged lane replacement.

## Evidence

- `20261008T161700Z-preview-runtime.json`: closing, held cover and reopening, terminal frame 4337/positive PTS; Program scene identity unchanged.
- `20261008T162900Z-preview-abort-replay.json`: two consecutive aborts, then immediate normal playback, all with terminal frame/PTS; host handles decoder deactivation's pre-admission readiness race.
- `20261008T161930Z-preview-deadline.json`: abandoned cover self-restored in 22393 ms; Configure returns TRANSITION_BUSY during playback, busy false afterwards, Program identity unchanged.
- `20261008T163100Z-scene-contracts.txt`: 101 scene-control contract tests passed.
- `20261008T163100Z-native-checks.txt`: native transition controller, media parser, RTWQ lifecycle and headless WebSocket quiesce all passed. The RTWQ test target was built before its final run.
- `20261008T163100Z-build-validation.json`: activated executable/WebSocket hashes and the pre-existing full CEF fixture limitation. Headless and WebSocket targets built successfully with RelWithDebInfo.
- Prism's final rail-click JSON and cover PNG under its matching evidence unit prove the actual returned video, not just command acceptance or a synthetic scene swap.

The full CEF graceful-shutdown fixture fails because it does not observe `CEF source creation marker`; both bundled browser DLLs lack its expected `PULSAR_CEF_LIFECYCLE` marker. The previous headless binary with the same browser bundle reproduces exactly the same failure. The test was not weakened, and those DLLs were not replaced as part of this transition fix. Four native tests above and actual idle Prism restarts succeed; do not call the complete CEF fixture green.

## Activation and rollback

Only the executable and WebSocket DLL were activated in the user's existing approved Prism bundle. The previous pair is backed up at `Artifacts/backups/2026-10-08/pulsar-preview-transition`. Native runtime identity, leases and expected stop behavior remain intact; final Prism PID 51496 / Pulsar PID 31472 is idle, with no recording, destination or exit error. The selected original scene is restored.

Rollback is the backed-up native pair after the idle host has stopped, plus the previous Prism snapshot. No main merge, CI success or release publication is claimed. The worktree is retained following the earlier cleanup policy rejection, without an alternate deletion attempt.
