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
[Build from source](#build-from-source)

## What you can build

- **Embedded broadcast applications:** own the runtime lifecycle from Node.js or
  Electron, render HTML/CSS/JS scenes through CEF, and control outputs through typed APIs.
- **Preview/Program workflows:** keep two production lanes warm, prepare and validate
  changes, then commit an atomic Cut with correlated events.
- **Automated streaming and recording:** publish to Twitch or custom RTMP/RTMPS
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
| `client.destinations` | Create, start, stop and remove Twitch, custom RTMP/RTMPS and local-file destinations. |
| `client.video` / `client.adaptive` | Read encoder settings, adjust bitrate and configure adaptive bitrate. |
| `client.record` / `client.stream` | Compatibility recording/streaming lifecycle; recording is separate from `vod_local` destinations. |
| `client.audio` | Audio controls exposed by the runtime. |
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

## Documentation map

| Start here | Contents |
|---|---|
| [Protocol](docs/PROTOCOL.md) | Wire APIs, events, capabilities and environment settings. |
| [Scene-switch contract](scripts/contracts/scene_switch_v1/README.md) | Command envelopes, state machine and commit semantics. |
| [Client](packages/pulsar-client/README.md) | TypeScript API, typed events, errors and examples. |
| [Embedding](docs/PRISM-EMBEDDING.md) | Host lifecycle, readiness and verified bundle integration. |
| [Architecture](docs/ARCHITECTURE.md) | Runtime structure and ownership boundaries. |
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
