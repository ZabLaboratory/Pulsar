# Pulsar

[![Release](https://img.shields.io/github/v/release/ZabLaboratory/Pulsar?logo=github)](https://github.com/ZabLaboratory/Pulsar/releases/latest)
[![Pipeline](https://github.com/ZabLaboratory/Pulsar/actions/workflows/pipeline.yml/badge.svg)](https://github.com/ZabLaboratory/Pulsar/actions/workflows/pipeline.yml)
[![Windows x64](https://img.shields.io/badge/runtime-Windows%20x64-0078d4)](#install)
[![Client MIT](https://img.shields.io/badge/client-MIT-2ea44f)](packages/pulsar-client/LICENSE)
[![Runtime GPL](https://img.shields.io/badge/runtime-GPL--2.0--or--later-blue)](LICENSE)

**A programmable live production engine, built on OBS.**

Pulsar brings capture, composition, encoding, recording and streaming into a
headless Windows process that your application controls over WebSocket. Embed it
with the TypeScript SDK, run it as a local media service, or connect your own
obs-websocket v5 client.

Pulsar 3.0.0 adds a validated **Preview/Program production core**: prepare the next
scene on a hot lane, commit a Cut at a video-frame boundary, and keep the encoder,
outputs and Program audio attached throughout.

[Download 3.0.0](https://github.com/ZabLaboratory/Pulsar/releases/tag/v3.0.0) ·
[Release notes](docs/releases/3.0.0.md) ·
[Protocol](docs/PROTOCOL.md) ·
[Client API](packages/pulsar-client/README.md) ·
[Build from source](#build-from-source) ·
[All libobs changes](docs/LIBOBS-CHANGES.md) ·
[Documentation index](docs/README.md)

## Contents

- [What you can build](#what-you-can-build)
- [The production model](#the-production-model)
- [Install and first run](#install)
- [Control surfaces](#control-surfaces)
- [Capture, composition and browser rendering](#capture-composition-and-browser-rendering)
- [Streaming, recording and replay](#streaming-recording-and-replay)
- [Audio and monitoring](#audio-and-monitoring)
- [Capabilities and operator feedback](#capabilities-and-operator-feedback)
- [Configuration](#configuration)
- [Runtime isolation and return transports](#runtime-isolation-and-return-transports)
- [Observability and measurement](#observability-and-measurement)
- [How Pulsar modifies OBS and libobs](#how-pulsar-modifies-obs-and-libobs)
- [What 3.0.0 validates](#what-300-validates)
- [Build from source](#build-from-source)
- [Troubleshooting](#troubleshooting)
- [Documentation map](#documentation-map)
- [Documentation index](docs/README.md)
- [License](#license)

## What you can build

- **Embedded broadcast applications:** own the runtime lifecycle from Node.js or
  Electron, render HTML/CSS/JS scenes through CEF, and control outputs through typed APIs.
- **Preview/Program workflows:** keep two production lanes warm, prepare and validate
  changes, then commit an atomic Cut with correlated events.
- **Automated streaming and recording:** publish to Twitch, YouTube or custom RTMP/RTMPS
  destinations and create local recordings using shared encoders.
- **Concurrent production sessions:** give each process its own identity,
  configuration, WebSocket port, recordings and namespaced DirectShow returns.
- **Observable media pipelines:** follow a command through preparation, frame
  commit, encoding and output; distinguish accepted requests from actual results.

Your application owns scene authoring, the operator interface and automation.
Pulsar owns the local media execution.

## The production model

```text
Capture / browser / media sources
               |
        +------+------+
        |  Hot lanes  |
        |    A   B    |
        +------+------+
               | Prepare -> Take -> frame-boundary commit
        +------+--------+
        | Stable views  |
        | Program   PVW |
        +------+--------+
               |
     Encoders · DirectShow returns
               |
       Stream · Record · Replay
```

The lanes exchange On-Air and Preview roles; their producers stay alive.
Downstream consumers keep their stable view bindings. Program audio follows an
explicit common route across Cuts.

The `pulsar.scene-switch.v1` contract defines `Prepare`, `Take`, `Abort`,
revisions, ordering and idempotency. Consumers observe `TakeCommitted` and its
frame identity to know when a switch happened.

Cut is the default. Set `PULSAR_DUAL_LANE_TRANSITIONS=1` to enable the optional
Fade/Stinger composition path. Preview audio and audio-follow-video are outside
the supported dual-lane audio contract.

Read the [scene-switch contract](scripts/contracts/scene_switch_v1/README.md)
and [runtime protocol](docs/PROTOCOL.md#scene-switch-runtime-vendor-v1)
before integrating a production control surface.

## Install

The native runtime supports **Windows x64**. The JavaScript packages are ESM and
require **Node.js 18 or later**.

| Package | Choose it when |
|---|---|
| [`@clodocapeo/pulsar-bundle-full`](packages/pulsar-bundle-full/README.md) | You need the runtime with browser/CEF, native text and VLC modules. |
| [`@clodocapeo/pulsar-bundle`](packages/pulsar-bundle/README.md) | You need the smaller runtime without those full-bundle modules. |
| [`@clodocapeo/pulsar-client`](packages/pulsar-client/README.md) | Your application connects to a runtime whose lifecycle is managed elsewhere. |

```powershell
npm install @clodocapeo/pulsar-bundle-full@3.0.0
```

Both bundle packages expose the same `spawn()` API and include the matching client.
Their installation step downloads the Windows archive for the package version.
For offline distribution, custom binary locations and download troubleshooting,
see the [bundle guide](packages/pulsar-bundle-full/README.md).

You can also download the light or full ZIP directly from the
[GitHub release](https://github.com/ZabLaboratory/Pulsar/releases/tag/v3.0.0).

### First run

Save this as `hello-pulsar.mjs`:

```js
import { spawn } from "@clodocapeo/pulsar-bundle-full";

const pulsar = await spawn({
  readyTimeoutMs: 60_000,
  env: {
    PULSAR_RESOLUTION: "1920x1080",
    PULSAR_FPS: "60",
    PULSAR_VIDEO_ENCODER: "x264",
  },
});

try {
  const video = await pulsar.client.video.get();
  console.log({
    runtimeInstanceId: pulsar.runtimeInstanceId,
    runtimeDir: pulsar.runtimeDir,
    port: pulsar.port,
    libobsVersion: pulsar.libobsVersion,
    video,
  });
} finally {
  await pulsar.shutdown();
}
```

```powershell
node hello-pulsar.mjs
```

`spawn()` starts the native engine, waits for readiness and connects an
authenticated client. This example reads its configuration and shuts it down
cleanly. An unset capture source initially produces black video.

### Runtime ownership

Every spawned process gets a private runtime directory and a validated identity.
Configuration, logs and default recordings live in that directory, while the
executable and plugins are resolved from the installed bundle.

Automatically created runtime directories are temporary and cleaned up by
`shutdown()`. Set `PULSAR_RUNTIME_DIR` to an application-owned absolute directory,
or set `PULSAR_RECORD_DIR` to a durable recording destination, when files must
survive the session.

Native hosts can parse the single `PULSAR_READY` stdout line to obtain the
loopback URL and session password. Keep those credentials private. The Node
bundle handles this handshake and redacts the password from its public log
callbacks.

See the [embedding contract](docs/PRISM-EMBEDDING.md) for lifecycle, readiness and
artifact verification.

## Control surfaces

| Surface | Purpose |
|---|---|
| `client.obs` | Baseline obs-websocket v5 calls and events. |
| `client.destinations` | Create, start, stop and remove Twitch, YouTube, custom RTMP/RTMPS and local-file destinations. |
| `client.video` / `client.adaptive` | Read encoder settings, adjust bitrate and configure adaptive bitrate. |
| `client.record` / `client.stream` | Compatibility recording/streaming lifecycle; recording is separate from `vod_local` destinations. |
| `client.audio` | Input mute/device/monitoring controls and common Program-audio route readback. |
| `client.capabilities` | Runtime manifest: presence, supported settings and mutation regimes. |
| `pulsar-scene` vendor | Manage a browser capture source through `SetCaptureSource` / `GetCaptureSource`. |
| `pulsar-scene-switch` vendor | Dual-lane preparation, commit, abort and state inspection. |

The scene-switch commands use `CallVendorRequest`; they are not top-level
obs-websocket requests. For example, with a running `pulsar` instance:

```js
const response = await pulsar.client.obs.call("CallVendorRequest", {
  vendorName: "pulsar-scene-switch",
  requestType: "GetState",
  requestData: {},
});
console.log(response.responseData);
```

For browser capture, the **full bundle** supplies CEF. Your application serves
the scene and provides its URL through the
[`pulsar-scene` API](plugins/pulsar-scene-source/README.md).
Use the scene-switch contract for coordinated Preview/Program changes.

Use `kind: "twitch"` for Twitch: the runtime selects its TLS ingest.
`rtmp_custom` accepts an RTMP/RTMPS URL and key; `vod_local` takes a fully resolved
file path. See [destinations](plugins/pulsar-multi-stream/README.md) and the
[typed client examples](packages/pulsar-client/README.md).

## Capture, composition and browser rendering

Pulsar retains OBS's source and scene model. The engine loads the source types
provided by the selected bundle and the host's available hardware; query
`GetInputKindList` and the capability manifest instead of assuming that a DLL
in the archive means a source is operational.

| Input family | How it fits |
|---|---|
| Window / display / game capture | OBS Windows capture modules; the boot window descriptor is optional. Game capture is also in the light bundle. |
| DirectShow devices | Cameras and compatible video devices when the ATL-dependent module is built and the device exists. |
| Images and media | Image sources and FFmpeg media playback; VLC-backed sources belong to the full distribution and still require an available VLC runtime. |
| Browser sources | Full bundle only: Pulsar's CEF fork renders HTML/CSS/JS without an OBS Studio window. |
| Native text | GDI+/FreeType text modules in the full distribution, subject to actual source registration and installed fonts. |
| Audio inputs | Desktop loopback, opt-in microphone and opt-in process loopback; independent of the dual-lane video role map. |

NDI, AJA, DeckLink, VST hosting and arbitrary third-party OBS plugins are not
promised by the standard distributions. Do not equate upstream OBS support
with a module shipped and tested by Pulsar.

### Two scene-control paths

The baseline v5 scene/input/item APIs serve general OBS-compatible authoring.
The older `pulsar-scene:SetCaptureSource` helper replaces Pulsar-managed
capture items with one browser source and removes prior managed items across
scenes. It is useful for a single composed page, but it is not a substitute
for preparing an independent Preview lane.

For production switching, use `pulsar-scene-switch`: inspect `GetState`,
prepare the current Preview lane, wait for `PreviewReady`, issue Take with
matching revisions, and observe `TakeCommitted`. A Take freezes the prepared
candidate until one terminal commit/abort wins. A stale revision or reused
command ID with different content is rejected before mutation.

The runtime retains at most **4096 command outcomes** per session. Known IDs
replay their original result without creating another transition; the cache
does not evict them to admit an unbounded stream of new commands. Once full,
new commands fail closed until a controlled runtime restart. Plan session
length and recovery explicitly.

### CEF rendering and lifecycle

The full runtime ships `pulsar-browser.dll` and
`pulsar-browser-page.exe`, not a second concurrent upstream browser loader.
The helper stays next to CEF in `obs-plugins/64bit/`. Offscreen accelerated
rendering uses shared D3D11 textures when available; software rendering and
callback/shutdown barriers have separate compatibility tests.

Pulsar-managed browser sources pin webpage control to `None`. A page is
rendered content, not an authorized controller of the broadcast engine.
The CEF build runs without its sandbox SDK; loading a page is therefore a
trust decision, not an isolation guarantee. Keep the scene server and its
dependencies within the application's trust boundary.

Source replacement and shutdown drain browser work before releasing libobs
state. URL acceptance is not a rendering proof: a source can be black,
frozen or stale while a control request succeeds. The
[capture/PGM integration suite](packages/capture-pgm-compat/README.md)
checks real recorded images for those cases.

## Streaming, recording and replay

### Destinations and encoder sharing

The native destination registry supports four kinds:

| Kind | Configuration | Result |
|---|---|---|
| `twitch` | Stream key | Compile-time pinned TLS ingest. |
| `youtube` | Stream key | Compile-time pinned YouTube TLS ingest; not an OAuth/account-management API. |
| `rtmp_custom` | RTMP/RTMPS URL and key | Application-selected ingest. Prefer RTMPS when available. |
| `vod_local` | Fully resolved output filename | Local file destination; the application names the file. |

Destinations share the frontend encoders. This avoids encoding once per
destination, but it also means a shared bitrate change is not an independent
per-destination quality profile. Multiple destinations still consume network,
muxing and storage resources; their number is not an unlimited capacity claim.

A request being accepted does not prove that a remote platform is live.
Follow output state and settlement/failure events, and inspect actual output
activity. The [output diagnosis runbook](docs/runbooks/diagnose-a-failed-go-live.md)
explains reason classes and the difference between a failed start attempt
and a later on-air failure.

The compatibility `client.stream` API uses the singleton frontend output.
Configure its service before starting. Use the destination API for Twitch;
the legacy `rtmp_common` Twitch service is refused because its fallback
ingest does not meet Pulsar's TLS policy.

### Recording

The singleton `client.record` recorder auto-generates a filename under
`PULSAR_RECORD_DIR`. Choose `mp4` or `mkv` at boot with
`PULSAR_RECORD_CONTAINER`. Manual file splitting and chapter-marker
requests are exposed through v5 where the runtime advertises support.
File naming for a `vod_local` destination remains caller-owned.

Always stop and finalize active recordings **before** terminating the native
process. The bundle's `shutdown()` disconnects and terminates the child;
it is not a guarantee that an active MP4 will be finalized by Windows
process termination. Export logs and recordings before cleanup of an
automatically generated runtime directory.

### Replay buffer

Replay borrows the active stream/record encoders. It does not start a second,
off-air encoding pipeline. Start an eligible live encoder output first, then
use v5 replay-buffer commands. A saved replay returns its actual file path;
the buffer is bounded by both time and memory.

Defaults are 30 seconds and 512 MB, configurable at boot through
`PULSAR_REPLAY_MAX_TIME_SEC` and `PULSAR_REPLAY_MAX_SIZE_MB`.
The 3.0.0 replay-stop fix is a lifecycle correction, not removal of those
bounds or permission to stop the source encoders before finalizing replay.

## Audio and monitoring

Program audio uses one explicit common libobs route, stable across video
Cuts. `client.audio.programRoute()` reports its identity, bound outputs,
source channels and real encoder-fed PTS observations.

| Main mixer channel | Role | Boot policy |
|---|---|---|
| 0 | Mutable dual-lane video root | Excluded from the common-audio source inventory. |
| 1 | Desktop playback loopback | Default audio device unless overridden. |
| 2 | Per-process loopback | Created only when `PULSAR_PROCESS_AUDIO_NAME` is set. |
| 3 | Microphone | Created only when `PULSAR_MIC_DEVICE_ID` is set. |

Process loopback depends on the Windows build and available WASAPI source.
Use the manifest and actual source state to distinguish unsupported,
unavailable and intentionally disabled inputs.

Up to six AAC encoders can map to six mixer tracks. Select the tracks carried
by stream, recording and replay independently using their boot lists.
Output slot order is the rank in the configured list, **not** the original
track number. Audio bitrate updates are allowed only when the affected
encoders are idle.

Mute, input-device and monitoring controls act on the audio input, not on
the currently selected video scene. Monitoring-device availability is read
from the running engine. **Preview audio and audio-follow-video are not
supported by the dual-lane contract**; a prepared Preview scene must not be
presented as a separately monitored or automatically switched audio bus.

## Capabilities and operator feedback

`client.capabilities.get()` reads the running engine's manifest. It covers
encoder families and properties, audio, source/filter/transition inventories,
graphics adapters, output scales, recording and other implemented controls.

Three concepts must remain separate:

- **Presence:** the runtime enumerates an item.
- **Mutation regime:** the field is live, boot-fixed, read-only or otherwise
  constrained by the manifest/contract.
- **Operational proof:** the selected device, source or output actually works
  on this machine and in this session.

An inventory entry is not permission to apply every property, and a listed
hardware encoder is not proof of a successful hardware session. Drive controls
from the reported regime and read back the resulting state.

The TypeScript SDK normalizes native snake_case wire fields to its typed
camelCase API. Low-level `client.obs` vendor calls still use the wire schema.
Errors expose stable codes/envelopes; human-readable messages are diagnostic
text, not parsing keys. The optional bundle `onPrismLog` callback and
client `prismLog` events provide structured projections for an operator UI.
Never persist the raw READY password or stream keys in application logs.

## Configuration

Resolution, frame rate and encoder family are fixed at startup. Video bitrate
can be adjusted live.

| Variable | Default | Purpose |
|---|---|---|
| `PULSAR_RESOLUTION` | `1920x1080` | Output canvas. |
| `PULSAR_FPS` | `60` | Output frame rate. |
| `PULSAR_VIDEO_ENCODER` | `x264` | `x264`, `nvenc`, `qsv`, `amf` or `auto`. |
| `PULSAR_VIDEO_BITRATE` | `6000` | Video bitrate in kbps. |
| `PULSAR_AUDIO_BITRATE` | `160` | AAC bitrate in kbps. |
| `PULSAR_AUDIO_TRACKS` | `1` | Audio track count, 1–6. |
| `PULSAR_CAPTURE_WINDOW` | Unset | Window target: `<title>:<class>:<exe>`. |
| `PULSAR_RUNTIME_DIR` | Private directory from `spawn()` | Explicit application-owned session directory. |
| `PULSAR_RECORD_DIR` | `<runtimeDir>/recordings` | Default recording directory. |
| `PULSAR_ADAPTIVE_BITRATE` | Enabled | Set to `off` to disable adaptation. |
| `PULSAR_DUAL_LANE_TRANSITIONS` | Disabled | Opt into dual-lane Fade/Stinger. |

An unavailable hardware encoder falls back to x264 with a warning. Hardware
encoding and accelerated browser rendering depend on the installed adapter and
drivers. Consult the [full configuration reference](docs/PROTOCOL.md) for
validation rules, runtime identity, alias leases and diagnostics.

## Runtime isolation and return transports

A runtime identity is distinct from a log session ID. `runtimeInstanceId`
selects process leases and dedicated media-return names; session correlation
joins logs and output events. A native startup rejects collisions on either
the identity or the physical runtime directory. Different path spellings
must not create two owners for the same directory.

The bundle allocates a free loopback port per child unless explicitly pinned.
That port reservation is not atomic with the later WebSocket bind; readiness
and authenticated connection remain authoritative.

Historical DirectShow aliases are a singleton compatibility surface.
`PULSAR_LEGACY_ALIAS=required` requires that lease;
`dedicated` or `off` avoids claiming it. Non-holders remain available through
their runtime-specific names. An external DirectShow reader of a dedicated
return must receive the matching runtime identity and explicit non-legacy
selection, as documented in the [embedding guide](docs/PRISM-EMBEDDING.md).

ProgramReturn and PreviewReturn are **video-only**. Their public samples are
CPU NV12, even when the optional internal D3D11 helper path is selected.
The private helper's authenticated GPU transport is not an SDK for passing
native handles to the host application. See
[D3D11 return transport](docs/runbooks/d3d11-return-transport.md) and
[consumer-lease lifecycle](docs/runbooks/directshow-lease-watcher.md).

## Observability and measurement

The runtime exposes readiness, session identity, output attempts and failures,
diagnostic counters and common Program-audio observations. Durable logs live
under the runtime namespace unless an explicit log directory overrides it.
Collect them before the application's retention/cleanup policy removes them.

Detailed runtime traces are opt-in. `PULSAR_TRACE_SIGNALS` selects `all`,
`none`, or a comma-separated subset such as `program,raw,output_mux_enqueue`.
Available families cover Program, Preview, raw/borrowed publication, GPU,
queues, encoder readiness, return readback, callback enqueue, mux enqueue,
interleaver mutex wait and socket send. A trace path and the trace producer's
required configuration must also be supplied; use the probe runbook rather
than treating one environment variable as a complete tracing setup.

The observation chain is:

`command → rendered Preview → frame commit → encoder → output → receiver → decoded image`

Each boundary has a different clock/meaning. The probes retain frame, packet,
revision and correlation identities; they do not convert a callback timestamp
into an assumed displayed pixel. DirectShow observations use a separate
sidecar. Tests of synthetic timelines, real recorded frames, authenticated
native execution and live network output are reported separately.

The [latency probe guide](docs/runbooks/probe-take-latency.md) defines the
measurement procedure. [pgm-correlator](packages/pgm-correlator/README.md)
correlates external scene-state and recorded visual timelines by time; it
does not recover scene identity from arbitrary pixels.

## How Pulsar modifies OBS and libobs

Pulsar is a maintained native fork, not only a JavaScript wrapper around stock
OBS. Its OBS submodule is pinned to
`bd73b922891e56839b0bc86bdc519802802f9d68`, an OBS 32.1.2-derived revision.
Five foundational modifications are incorporated in that pin; **52 additional
patch files** build the current media core.

| Area | What Pulsar changes |
|---|---|
| Embedding and build | Runtime version suffix, ATL capability gate, executable-relative resources, deterministic patch replay/cache and Qt host-tool compatibility. |
| Production core | Atomic two-view frame-boundary swaps, stable returns, hot-lane activation and shared-descendant preservation. |
| Video path | Auxiliary cache controls, borrowed Program/Preview publication, no-consumer copy elimination, qualified current-surface CPU readback. |
| Encoding and output | Low-latency interleaving, ULL NVENC queue draining, content-time tracking, optional ready-batch/asynchronous completion. |
| Return transport | Runtime namespaces, atomic latest-frame queues, collision checks, live consumer leases, read-only external access and the private D3D11 helper. |
| Measurement | Per-stage snapshots, packet enqueue/mutex timing, independent consumer traces and content-versus-cadence identity. |
| Correctness | Replay stop, GPU fatal-error stop/restart, readback/audio timeline alignment and software-adapter exclusion from automatic promotion. |
| Browser | A separately maintained headless CEF plugin with callback gates, source-task ownership and teardown barriers. |

The [complete libobs/OBS change reference](docs/LIBOBS-CHANGES.md) describes
**every patch**, its affected files, the integrated baseline changes, ordering,
defaults, experimental status, validation and rollback. It also distinguishes
the nested upstream browser patch from the browser DLL actually distributed.

These modifications are built and qualified as one stack. Replacing only
`obs.dll` or deleting an intermediate patch is not a supported upgrade or
rollback. Keep the native runtime and npm packages matched by release.

## What 3.0.0 validates

The dual-lane core is integrated and independently reviewed: hot lanes, stable
outputs, atomic switching, ordered commands, idempotency, common Program audio,
runtime isolation and correlated observations.

On the qualified Windows NV12 1080p60 CPU-encoding workload, first changed
decoded-image p95 improved from **45.897–45.947 ms** to **33.045–37.982 ms** across
the recorded comparison campaigns. The optimization is enabled automatically
only for that qualified configuration with a physical graphics adapter.

Those measurements describe decoder output on the tested workload. They do not
establish physical-display latency, a universal NVENC improvement, or a 4K or
multi-camera capacity guarantee. The [performance study](docs/issue-253-native-optimized.md)
retains the workload, results and rejected experiments. Use
`PULSAR_RAW_CURRENT_READBACK=0` to restore the previous CPU staging path.

The release workflow builds the runtime, runs contract/native/integration gates,
packages both distributions and performs a real Twitch broadcast. Release assets
include the archives, broadcast proof, diagnostics and
`prism-pulsar-runtime-manifest.json` with the full archive's SHA-256 digest.

See the [complete 3.0.0 changelog](docs/releases/3.0.0.md), including every commit
since `v2.0.0b`, and the [release pipeline](.github/workflows/pipeline.yml).

## Build from source

Use Windows x64, Visual Studio 2022 with the C++ desktop workload, CMake 3.28+,
Git with submodule support, PowerShell, Node.js and Python 3.11+. FFmpeg is used
by the media validation tools. See [development setup](docs/DEVELOPMENT.md) for
the detailed toolchain.

```powershell
git clone --recurse-submodules https://github.com/ZabLaboratory/Pulsar.git
cd Pulsar
$env:PULSAR_BUNDLE_SKIP_POSTINSTALL = "1"
npm ci
.\scripts\build-win.ps1 -Full
```

The full build writes the runtime to:

```text
upstream/build_x64/rundir/RelWithDebInfo/
```

After a compatible full build, use `.\scripts\build-win.ps1 -Fast` for supported
local edit/probe cycles. CI and releases use the complete build.

To package the just-built revision:

```powershell
.\scripts\package-win.ps1 -Variant light -Zip -SkipBuild
.\scripts\package-win.ps1 -Variant full -Zip -SkipBuild
```

### Validate a build

```powershell
npm run lint
.\scripts\run-probes.ps1
```

The pipeline also runs package builds/tests, binary-export checks, protocol tests
and real CEF/PGM integration. Hosted runners without a physical GPU report that
limitation explicitly; local hardware results and release broadcast evidence
cover different parts of the pipeline.

## Troubleshooting

| Symptom | First check |
|---|---|
| `PULSAR_BINARY_UNAVAILABLE` / missing executable | Confirm the matching ZIP exists and postinstall downloaded it; npm installation can succeed after a download warning. |
| READY timeout | Inspect the first boot failure: namespace collision, listener bind, missing module/data, CEF initialization or quarantined file. Do not restore a shared binary-directory cwd. |
| Black or frozen browser capture | Verify full bundle, one browser loader, helper/CEF layout, reachable scene URL and actual decoded PGM. |
| Start request failed or output stopped | Inspect settlement/failure event, stable reason class and `GetDiagnostics`; do not infer success from a sent command. |
| Hardware encoder unavailable | Read the selected encoder and warning; x264 fallback is intentional, but it changes the measured workload. |
| Recording vanished after shutdown | Automatic runtime directories are temporary. Use explicit persistent recording/log destinations and finalize before termination. |
| Wrong ProgramReturn instance | Check runtime identity, alias lease and the reader's dedicated namespace configuration. |
| Unexpected latency | Separate raw, DirectShow, encoded, receiver and decoded boundaries; inspect enabled experimental switches before comparing runs. |

Use the [documentation index](docs/README.md) for the focused runbook.
Do not disable authentication, weaken a gate or suppress a failing assertion
as a troubleshooting shortcut.

## Documentation map

| Start here | Contents |
|---|---|
| [Protocol](docs/PROTOCOL.md) | Wire APIs, events, capabilities and environment settings. |
| [Scene-switch contract](scripts/contracts/scene_switch_v1/README.md) | Command envelopes, state machine and commit semantics. |
| [Client](packages/pulsar-client/README.md) | TypeScript API, typed events, errors and examples. |
| [Embedding](docs/PRISM-EMBEDDING.md) | Host lifecycle, readiness and verified bundle integration. |
| [Architecture](docs/ARCHITECTURE.md) | Runtime structure and ownership boundaries. |
| [All libobs changes](docs/LIBOBS-CHANGES.md) | Integrated OBS changes and all 52 source patches. |
| [All documentation](docs/README.md) | Current guides, operational runbooks, contracts and historical studies. |
| [Development](docs/DEVELOPMENT.md) | Build toolchain and local development. |
| [Dual-lane canary](docs/runbooks/pulsar-dual-lane-canary.md) | Qualification and operational checks. |
| [Changelog](CHANGELOG.md) | Release history and upgrade information. |

Source layout: `upstream/` contains the OBS submodule, `patches/` its ordered
Pulsar changes, `plugins/` the native integration, `packages/` the client and
bundles, and `scripts/` the build and validation tooling.

## License

The runtime and OBS-derived plugins are **GPL-2.0-or-later**.
The WebSocket TypeScript client is **MIT**.

Applications distributing the native runtime must follow
[LICENSE-INVARIANTS.md](LICENSE-INVARIANTS.md) and
[CONSUMER-AUDIT.md](CONSUMER-AUDIT.md).
