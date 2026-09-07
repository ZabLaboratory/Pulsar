# Pulsar — architecture (3.0.0)

Pulsar is a headless Windows x64 media engine built from a pinned OBS fork,
an ordered native patch stack, Pulsar-owned C++ components and a pure
WebSocket TypeScript SDK. It is designed for application-controlled live
production, not for hosting the OBS Studio desktop interface.

This document describes the **current implementation**. Approved ADRs retain
their decision history; they are not rewritten to make old milestones look
current. Start with the [README](../README.md) for usage, the
[protocol](PROTOCOL.md) for wire fields and the
[libobs change reference](LIBOBS-CHANGES.md) for every native patch.

## 1. System and process boundaries

```text
Host application / automation
  ├─ owns UI, scene authoring, credentials, lifecycle and persistent files
  └─ obs-websocket v5 + vendor requests over authenticated loopback WS
       |
       v
pulsar.exe
  ├─ headless bootstrap + minimal Qt application
  ├─ frontend callback implementation + dual-lane production controller
  ├─ obs-websocket.dll
  ├─ pulsar-multi-stream.dll / pulsar-scene-source.dll
  ├─ patched libobs + D3D11 + capture / audio / encode / output modules
  ├─ pulsar-browser.dll ── CEF helper processes (full distribution)
  └─ private D3D11 return helper (optional internal transport)
       |
       ├─ RTMP/RTMPS destinations
       ├─ recording / replay files
       └─ read-only CPU NV12 ProgramReturn / PreviewReturn → DirectShow reader
```

“Headless” means **no OBS Studio window or operator UI**. It does not mean
“no Qt”: the bootstrap creates a `QApplication` with
`QT_QPA_PLATFORM=minimal`, and the WebSocket integration uses Qt facilities.
The separately maintained browser plugin removes its own Qt UI coupling.

There are three different channel classes:

| Channel | Boundary and purpose |
|---|---|
| Host control | Authenticated obs-websocket v5 on loopback by default. The host does not link/load libobs as its application API. |
| Media egress | RTMP/RTMPS, local media files and ordinary DirectShow samples. These are media, not a second command protocol. |
| Runtime-internal native transport | libobs module calls, private helper handles/pipes and producer-owned return queues. These exist inside the native implementation and are not a public host FFI. |

The project distribution constraints are recorded in
[LICENSE-INVARIANTS.md](../LICENSE-INVARIANTS.md) and the
[consumer audit](../CONSUMER-AUDIT.md). They are engineering/distribution
requirements, not a blanket legal determination about every possible
consumer. The runtime remains GPL-2.0-or-later; the WebSocket client is MIT.

## 2. Component ownership

| Component | Artifact / owner | Responsibilities |
|---|---|---|
| `pulsar-headless` | `bin/64bit/pulsar.exe` | Runtime identity and physical-directory leases, log/config initialization, video/audio bootstrap, module load, readiness and teardown barriers. |
| `pulsar-frontend-stub` | Static library linked into the executable | Frontend callback table, live scene inventory, encoders and singleton outputs, hot lanes/views, common Program audio, scene-switch vendor adapter, transition controller and runtime observations. |
| `pulsar-websocket` | `obs-plugins/64bit/obs-websocket.dll` | v5 framing/authentication, baseline handlers/events, vendor dispatch, verified output-attempt feedback and bounded shutdown. |
| `pulsar-multi-stream` | `pulsar-multi-stream.dll` | Destination registry, shared-encoder fan-out, adaptive bitrate, capabilities, audio/monitoring/diagnostic vendor requests. Owns vendor `pulsar`. |
| `pulsar-scene-source` | `pulsar-scene-source.dll` | Legacy single browser-capture replacement. Owns vendor `pulsar-scene`. |
| `pulsar-browser` | `pulsar-browser.dll` + `pulsar-browser-page.exe` | CEF browser source, accelerated/software rendering callbacks, source-task lifecycle and browser shutdown fence. Full distribution only. |
| `pulsar-output-classify` | Header-only interface target | Shared stable output-failure classification used by frontend and registry. |
| `pulsar-nv-secure-load` | Header-only interface target | Shared validated SDK-directory/loading policy used by upstream effect module, capability probe and native tests. |
| Patched OBS | `obs.dll`, graphics and upstream module binaries | Rendering/audio/video I/O, atomic view swap, encoders, output interleaving, capture and return transport. |

Source-directory names and deployed filenames are not always identical.
In particular, the directory `pulsar-websocket` emits
`obs-websocket.dll`; it does not emit a second `pulsar-websocket.dll`.
The full package removes the upstream browser loader so two modules cannot
race to register `browser_source`.

## 3. Boot and readiness

The native sequence in
[main.cpp](../plugins/pulsar-headless/main.cpp) is:

1. Preserve redirected stdio or attach the parent console for direct use.
2. Resolve and validate the runtime identity and directory; acquire instance,
   physical-directory and optional legacy-alias ownership before native state
   is created. Adopt an explicitly inherited shutdown control when provided.
3. Construct the minimal Qt application; resolve the log session ID and
   install the durable redacted log handler.
4. Initialize libobs with the private runtime directory as module config root,
   then configure video and audio.
5. Install frontend callbacks **before** module loading, so plugins can
   register their event callbacks against a real frontend.
6. Seed a trustworthy per-session WebSocket configuration before loading the
   WebSocket plugin. A protected-config failure aborts startup.
7. Load modules and check that the configured WebSocket listener is active.
8. Complete frontend state creation: sources, hot lanes, views, encoders,
   singleton outputs, vendor adapters and capability-dependent features.
9. Print the separate `PULSAR_SESSION` line, the stable
   `PULSAR_READY ws=<url> password=<password>` sentinel, then the
   `pulsar-headless: libobs <version> ready, idling` marker.
10. Enter the service loop, maintain lease metadata and wait for shutdown.

READY is a native-service/listener signal. It is not proof that a URL has
rendered, a camera is supplying frames or Twitch is live. Those have their
own readiness and output observations.

The Node bundles currently wait for the **idle marker**, then read the
seeded config inside the private runtime directory and complete the
authenticated v5 handshake. Manual hosts can parse the READY sentinel.
These are two implemented launch paths; documentation must not claim the
bundle parses the sentinel password when its code reads config.

## 4. Production video graph

```text
                         two persistent producers
                         Lane A          Lane B
                            |              |
                        logical On-Air / Preview roles
                            |              |
                         ProgramView    PreviewView
                         main canvas    auxiliary mix
                            |              |
                         programVideo   previewVideo
                            |              |
           +----------------+---+          +---- PreviewReturn
           |                    |
        encoder(s)         ProgramReturn
           |
      stream / record / replay / destination fan-out
```

ProgramView aliases libobs's main canvas; PreviewView is one distinct active
auxiliary mix. The frontend binds the video encoder once to the stable
Program video object. Return outputs similarly receive their media identities
during setup, not once per Take.

The mutable part is the root-source role assignment. A Cut exchanges two
roots at one video boundary while preserving:

- lane/source lifetime and activation;
- ProgramView and PreviewView identity;
- their video objects and downstream binding;
- encoder/output objects;
- common Program audio.

The core swap primitive is in the patched libobs view/video path. Command
admission, revision guards, deadlines and policy live in the frontend.
A graphics callback records actual frame/PTS and queues state work; it does
not perform filesystem/media analysis in the rendering critical section.

### Prepare, Take, Abort

The frontend owns vendor `pulsar-scene-switch`. Requests arrive through
v5 `CallVendorRequest`, not top-level `Prepare`/`Take` messages.

- **Prepare** targets the current Preview lane and a scene, checks expected
  revisions, stages the candidate and waits for an actual Preview-mix frame.
- **PreviewReady** reports that observed frame and PTS.
- **Take** freezes that candidate and queues the atomic commit.
- **TakeCommitted** reports the real committed role map, revision changes,
  frame ID and PTS.
- **Abort/timeout** can cancel a still-pending commit. If the frame callback
  already won, the commit remains authoritative; there is no second terminal
  route mutation.

The contract uses monotone server sequence and three revision streams
(`program`, `preview`, `role_map`). Idempotence is scoped by runtime and
command ID over normalized payloads. Exact retries replay their original
outcome; conflicting reuse rejects without mutation.

The bounded runtime cache retains up to 4096 outcomes without eviction.
Capacity exhaustion refuses new commands until restart; clients must not
assume indefinite command admission in one process.

### Optional transitions and rollback

Atomic Cut is the default. `PULSAR_DUAL_LANE_TRANSITIONS=1` permits
Fade/Stinger composition on the stable Program view, followed by a final
frame-boundary role exchange. Invalid assets, invalid duration or unavailable
transition resources have explicit refusal/fallback behavior. An interruption
must preserve one winning terminal result and a coherent role map.

This flag is independent of the older `PULSAR_NATIVE_STINGER` path.
Neither means that arbitrary network-provided media paths are admitted.

The operational rollback/freeze drill is separate from a normal scene-switch
command. Once frozen, `GetState` reports `operational=false` and
`frozen=true`, while the committed Program route remains live. See the
[canary runbook](runbooks/pulsar-dual-lane-canary.md).

## 5. Audio, encoders and outputs

### Common Program audio

The frontend captures the process-wide libobs audio identity once.
Audio-capable frontend encoders share that route across video Cuts.
Desktop loopback, opt-in process loopback and opt-in microphone occupy
separate main-mixer channels; the mutable video root is omitted from the
common-audio source inventory.

Up to six AAC encoders map to mixer indexes. Stream/record/replay track lists
select which encoders each output carries. Output slot rank and mixer/track
identity are different and are exposed accordingly.

`GetProgramAudioRoute` reports actual route/output/source identities and
bounded encoder-fed PTS observations. PreviewReturn and ProgramReturn are
video-only. Preview audio and audio-follow-video remain explicitly unsupported.

### Encoder selection and sharing

Video family, resolution and frame rate are boot-fixed. The frontend resolves
x264/NVENC/QSV/AMF/auto against live encoder registration and falls back to
x264 with a warning when selection is unavailable. Bitrate can change live;
audio bitrate writes require idle affected encoders.

The singleton stream, record and replay outputs and the destination registry
reuse encoders rather than creating one video encoder per destination.
This is shared compression, not independent quality per destination or
unlimited capacity.

### Output ownership

| Output | Owner / behavior |
|---|---|
| Singleton stream | Frontend v5 compatibility output; requires configured service and verified state. |
| Singleton recording | Frontend recorder; auto-generated path, MP4/MKV boot choice, split/marker support as advertised. |
| Replay buffer | Frontend; taps already-active encoders, bounded time/memory, no off-air encoder startup. |
| Destinations | Registry; Twitch, YouTube, custom RTMP/RTMPS or caller-named local file. |
| Program/Preview returns | Stable frontend media bindings; consumer-gated publication independent of RTMP. |
| Compatibility virtual camera | Separate frontend output; do not confuse it with the two stable production returns. |

Output-attempt settlement and a later output failure are distinct events.
Wire acceptance must be checked against effective state; error classification
is shared between frontend and registry to avoid contradictory reason classes.

## 6. Browser ownership and shutdown

The browser fork runs CEF offscreen without browser docks/Qt UI.
Accelerated callbacks use D3D11 textures when available; software callbacks
have their own deterministic behavior. A source owns its asynchronous task
state and callback admission gate.

Pulsar-managed browser content has webpage control pinned to None.
Replacing a managed capture source removes its prior managed scene items.
This legacy replacement helper is not the dual-lane Prepare path: hot
production lanes intentionally preserve producers during role exchange.

Native shutdown quiesces WebSocket callbacks, drains browser work, tears down
frontend sources/outputs, then calls libobs shutdown and releases leases.
If a required barrier fails, the runtime does not continue unsafe teardown
under a still-live callback.

The native redirected-stdio harness uses an explicitly inherited anonymous
event for graceful shutdown. The current Node bundle instead disconnects
and invokes child termination, with a bounded force fallback. On Windows
this is **not** proof of graceful libobs teardown. Applications must finalize
recording/replay/stream outputs and export durable files before calling
`shutdown()`; native lifecycle tests exercise a different control path.

## 7. Isolation and return transport

Runtime ID and cwd leases are backed by OS ownership, not merely a lock-file
timestamp. Physical directory identity prevents case aliases/junction spellings
from acquiring two independent owners. Runtime files are configuration and
diagnostics; changing a metadata root does not create a new authority.

Legacy DirectShow names have one compatibility owner. Other runtimes use
dedicated namespaces. An invalid/empty identity or an alias selector without
a valid identity fails closed rather than silently opening the singleton.

The default return queue publishes latest-frame CPU NV12 with coherent
metadata. Consumer liveness is maintained by a watcher so the render callback
does not perform named-object I/O per frame. Unused return copies are skipped.

The D3D11 option is limited to the supported format/capability path and uses
a private producer-launched helper with authenticated bootstrap. GPU handles
are not handed to an arbitrary DirectShow client. The helper readback is
relayed through the producer-owned CPU queue; external clients remain
read-only. ABI mismatch, liveness/capability/format/timeout failure is
observable and falls back without changing Program/audio/encoder ownership.
This is not a zero-copy consumer API.

## 8. Performance and observability

The native stack separates rendering, conversion, borrowed publication,
encoder callback, interleaver lock wait, output enqueue, receiver packet and
decoded-picture observations. A content timestamp is not interchangeable
with an encoder cadence timestamp or a decoder frame index.

Qualified automatic current-surface readback is limited to Windows CPU
NV12 1080p60 with a physical graphics adapter. GPU encoding and unqualified
modes keep their prior automatic path. The explicit rollback is
`PULSAR_RAW_CURRENT_READBACK=0`.

Fresh-frame polling, NVENC ready drain and asynchronous output are retained
as opt-in experiments, not promoted as universal improvements.
See [LIBOBS-CHANGES](LIBOBS-CHANGES.md) for defaults and all 52 patches and
the [native study](issue-253-native-optimized.md) for measured results/limits.

Trace signals are individually selectable. Producer and DirectShow sidecar
observations remain distinct; trace/report tools preserve clock and media
identity instead of joining unrelated timestamps into an apparent gain.

## 9. Source, build and distribution graph

```text
OBS fork pin + 51 root patches + 1 nested browser patch
                         |
                 upstream CMake build
                         |
        Pulsar CMake components + matching headers/libs
                         |
             full validated runtime directory
                         |
         +---------------+---------------+
         |                               |
      light ZIP                       full ZIP
         |                               |
   pulsar-bundle                    pulsar-bundle-full
         +---------------+---------------+
                         |
                 pulsar-client (MIT)
```

The build reuses only exact fingerprinted patched checkouts. The default
complete build is required for CI/release qualification; `-Fast` is a
narrow local target loop, not a replacement for the whole pipeline.

The full variant adds CEF/browser, native text, VLC module and nv-filters.
NVIDIA SDK binaries/models are not redistributed; the effect module may
remain inert on a machine without a validated SDK. Module presence is not
operational availability. Light strips those optional families.

Both variants exclude OBS Studio UI, AJA/DeckLink, VST, WebRTC and other
modules selected by [package-win.ps1](../scripts/package-win.ps1).
Use actual release asset sizes and manifests rather than old approximate
file counts. Preserve `bin/64bit/`, `obs-plugins/64bit/` and `data/`.

CI separates source/contract checks, Windows build, binary exports, native
CTest/offline probes and real CEF-to-recorded-PGM checks. A tag additionally
runs the real broadcast, packages, npm publication and release attachment.
A green unit test is not a deployed binary; a published npm package is not
proof that its matching ZIP exists.

## 10. Scope and non-goals

- Supported native platform: Windows x64. The TypeScript client can run
  elsewhere against a managed runtime; native bundles cannot.
- No bundled operator desktop UI, cloud render service or mobile runtime.
- No Preview audio/AFV contract, arbitrary public native-handle API or
  automatic cross-consumer deployment.
- No claim that every OBS plugin, device, codec or extension is supported.
- No universal NVENC latency gain, physical-display timing guarantee, or
  4K/multi-camera capacity qualification derived from the 1080p60 study.

## Source map

| Topic | Source |
|---|---|
| Bootstrap/lifecycle | [headless component](../plugins/pulsar-headless/README.md) |
| Production graph | [frontend component](../plugins/pulsar-frontend-stub/README.md) |
| Wire | [protocol](PROTOCOL.md), [scene-switch contract](../scripts/contracts/scene_switch_v1/README.md) |
| Native changes | [complete OBS/libobs inventory](LIBOBS-CHANGES.md) |
| Host lifecycle | [embedding](PRISM-EMBEDDING.md), [bundle API](../packages/pulsar-bundle/README.md) |
| Build/release | [development](DEVELOPMENT.md), [release runbook](runbooks/cut-a-release-and-propagate.md) |
| Other guides and historical records | [documentation index](README.md) |
