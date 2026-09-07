# pulsar-frontend-stub

The name is historical: in 3.0.0 this is Pulsar's **headless frontend and
production controller**, not a frozen single-scene mock.

It is a static C++ library linked into `pulsar.exe`, not a loadable OBS
plugin. It implements `obs_frontend_callbacks` so obs-websocket and other
modules can use frontend state without the OBS Studio UI.

## Initialization and ownership

| Entry point | Responsibility |
|---|---|
| `pulsar_frontend_init()` | Install callbacks/UI-task routing before modules load; do not create plugin-owned factories prematurely. |
| `pulsar_frontend_finished_loading()` | Create sources, views, encoders, outputs and adapters after factories are registered; emit finished-loading. |
| `pulsar_frontend_shutdown()` | Stop/release owned state and signal exit after upstream WebSocket/browser barriers have quiesced. |
| `pulsar_frontend_cleanup_succeeded()` | Expose whether cleanup completed safely before the bootstrap continues to libobs shutdown. |

The implementation is [src/pulsar-frontend-stub.cpp](src/pulsar-frontend-stub.cpp).
Public shared headers under `include/` define production, audio, telemetry,
transition and egress contracts for other targets.

## Production video graph

- Two persistent hot lane roots, A and B, exchange logical On-Air/Preview roles.
- ProgramView aliases the main canvas; PreviewView is a separate active mix.
- The video encoder binds once to the stable Program video object.
- ProgramReturn/PreviewReturn keep their stable media binding across Takes.
- A queued libobs two-view swap commits at an actual video-frame boundary.
- Equal-view role exchange preserves show/activation ownership, including
  shared descendants; it does not restart hot browser/capture producers.

The baseline scene inventory comes from live libobs scenes, not a cached
single-Default list. Profile/scene-collection compatibility should not be
confused with a complete OBS Studio profile-management UI.

## Deterministic scene-switch vendor

This component owns `pulsar-scene-switch`, reached through v5
`CallVendorRequest`. It implements Prepare, Take, Abort, GetState and the
compatibility Dispatch adapter for
[pulsar.scene-switch.v1](../../scripts/contracts/scene_switch_v1/README.md).

Preparation targets Preview and waits for an observed Preview-mix frame.
Take freezes the candidate and commits one role swap. Abort/timeout can cancel
a pending swap; a frame-boundary callback that already won remains the single
commit. Revisions/server sequence and canonical payload hashes enforce
ordering/idempotence. The runtime retains 4096 outcomes and refuses new
command IDs when full rather than evicting earlier results.

Operational rollback freezes new Takes while leaving committed Program live.
The state API reports that freeze explicitly.

## Transitions

Cut is the default. `PULSAR_DUAL_LANE_TRANSITIONS=1` enables the optional
Fade/Stinger controller above the same stable views. It tracks queued,
running, final-queued and terminal phases with actual frame/PTS observations.
Missing/invalid local media or start/queue failures have explicit fallback;
an out-of-contract duration is rejected before mutation.

The older `PULSAR_NATIVE_STINGER` switch is a distinct dormant compatibility
path. Do not equate it with the dual-lane transition capability or suggest that
all program-scene changes still use the early direct-rebind implementation.
Asset selection is local/operator-controlled.

## Encoders and settings

The default is x264 H.264, 1080p60, 6000 kbps, veryfast/high/zerolatency and
AAC at 160 kbps. Family selection accepts x264, NVENC, QSV, AMF or auto and
falls back to x264 with a warning when the requested choice is unavailable.
Read the actual selected encoder; a fallback is a different workload.

Family/resolution/fps are boot-fixed. Video bitrate is mutable live.
NVENC ULL selection preserves configured compression tools; later ready-drain
and asynchronous modes are separate off-by-default experiments.
Current-surface CPU readback is automatic only for the qualified configuration
and physical adapter documented in [LIBOBS-CHANGES](../../docs/LIBOBS-CHANGES.md).

Audio bitrate can be changed only while affected encoders are idle.

## Common Program audio

The process-wide libobs audio identity is captured once. Frontend audio
encoders reuse it across video Cuts. Source channel 0 is the mutable video
root and is omitted from the common-audio source inventory.

| Channel | Source | Boot selection |
|---|---|---|
| 1 | Desktop loopback | Default device or `PULSAR_DESKTOP_AUDIO_DEVICE_ID`. |
| 2 | Process loopback | Opt-in `PULSAR_PROCESS_AUDIO_NAME`; Windows/source availability required. |
| 3 | Microphone | Opt-in `PULSAR_MIC_DEVICE_ID`; absent means no microphone source. |

`PULSAR_AUDIO_TRACKS` creates 1–6 AAC encoders, one per mixer index.
Per-track bitrate overrides and stream/record/replay track lists choose the
actual encoder slots. Slot rank is not track number.

`GetProgramAudioRoute` exposes route/output/source identity and actual
encoder-fed PTS evidence. ProgramReturn/PreviewReturn are video-only.
Preview audio and AFV are explicitly unsupported.

## Outputs

- Singleton stream: compatibility v5 output with a configured service.
- Singleton recorder: auto-generated file under `PULSAR_RECORD_DIR`,
  MP4/MKV boot choice, split and chapter-marker surfaces where supported.
- Replay: shared active encoders, bounded time/memory, actual last-saved path;
  refuses off-air encoder startup.
- Registry destinations: created by multi-stream and sharing frontend encoders.
- Program/Preview return outputs: stable production views.
- Compatibility virtual camera: separate output/source selection, not a
  synonym for either stable return.

The component emits effective stream/record/replay/virtual-camera events from
output signals and shares stable failure classification with multi-stream.
A successful request dispatch is not itself a STARTED state.

## Browser/source lifetime and telemetry

Hot-lane role changes preserve producers. Actual source replacement and
shutdown release owned references only after relevant callback/task fences.
The legacy managed browser-source replacement lives in
`pulsar-scene-source`; it is not the Prepare implementation.

Opt-in telemetry observes Program/Preview, raw/borrowed frames, queue/GPU
stages, packet enqueue/interleaver and downstream identities. Signal selection
is defined in `include/pulsar-runtime-telemetry-signals.h`; malformed selector
lists fail validation. Graphics callbacks do not run filesystem/decode work.

## Validation and references

Native tests cover view activation, queue safety, transition control, shutdown,
audio/encoder behavior and runtime contracts. Offline probes cover scene truth,
events, capabilities, multi-track audio, recording and service state.
Hardware canaries separately cover real WGC/CEF lanes and media observations.

- [Architecture](../../docs/ARCHITECTURE.md)
- [Protocol and environment](../../docs/PROTOCOL.md)
- [Dual-lane canary](../../docs/runbooks/pulsar-dual-lane-canary.md)
- [All libobs changes](../../docs/LIBOBS-CHANGES.md)

License: GPL-2.0-or-later.
