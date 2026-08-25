# Pulsar

[![GitHub release](https://img.shields.io/github/v/release/ZabLaboratory/Pulsar?logo=github)](https://github.com/ZabLaboratory/Pulsar/releases/latest)
[![Pipeline](https://github.com/ZabLaboratory/Pulsar/actions/workflows/pipeline.yml/badge.svg)](https://github.com/ZabLaboratory/Pulsar/actions/workflows/pipeline.yml)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-0078d4)](#requirements)
[![Client license](https://img.shields.io/badge/client%20license-MIT-2ea44f)](packages/pulsar-client/LICENSE)
[![Runtime license](https://img.shields.io/badge/runtime%20license-GPL--2.0--or--later-blue)](LICENSE)

> Headless broadcast runtime for embedding.

Pulsar is a Windows x64 fork of OBS Studio built as a service rather than a
desktop application. It starts pulsar.exe, exposes obs-websocket v5 on a
loopback WebSocket, and adds typed vendor APIs for destinations, encoder
settings, adaptive bitrate and live scene capture.

Pulsar is only the media plane. It does not author ZabCanvas scenes, run Orion
or Solar, or resolve ZabTruth/ZabRanking data. Prism owns those control-plane
concerns and sends Pulsar a local scene URL to capture.

## Version and releases

The source version is stored in [VERSION](VERSION). The current source line and
the three published npm packages are version 2.0.0.

A release tag runs the release-grade pipeline in
[.github/workflows/pipeline.yml](.github/workflows/pipeline.yml):

1. Build the Windows headless runtime and light/full distributions.
2. Run binary, license, protocol and offline probe gates.
3. Run the real Twitch broadcast probe and produce encoded-output evidence.
4. Attach the full zip, MP4, diagnostic JSON and
   prism-pulsar-runtime-manifest.json to the GitHub release.
5. Publish immutable npm package versions when they do not already exist.

Prism consumes the full manifest, verifies its SHA-256 digest and caches the
verified bundle locally. Pulsar must already be ready before a live scene
switch; the switch must not download or build the runtime.

- [Latest release](https://github.com/ZabLaboratory/Pulsar/releases/latest)
- [Prism embedding contract](docs/PRISM-EMBEDDING.md)
- [Protocol reference](docs/PROTOCOL.md)
- [Consumer audit](CONSUMER-AUDIT.md)

## Supported surface

| Area | Current behavior |
|---|---|
| Process | One headless pulsar.exe process. No OBS desktop UI or host-side FFI. |
| IPC | Session-authenticated obs-websocket v5 over a loopback WebSocket. |
| Video | 1920x1080 at 60 FPS by default. Resolution and FPS are boot-fixed. |
| Encoder | H.264 family selected at boot: x264, nvenc, qsv, amf or auto. Missing hardware falls back to x264 with a warning. |
| Live tuning | Video bitrate can be changed through pulsar:SetVideoSettings. Resolution, FPS and encoder family cannot be changed live. |
| Audio | AAC, configurable track count and per-track bitrate, plus WASAPI input and monitoring controls. |
| Destinations | twitch, rtmp_custom and vod_local. One encoder pair is shared across destinations. |
| Scene capture | Managed browser_source capture through the pulsar-scene vendor namespace. |
| Browser runtime | The full Windows bundle includes obs-browser and CEF for HTML/CSS/JS scene capture. |
| Recording | Legacy v5 recording output and vod_local destination, with separate lifecycles. |
| Adaptive bitrate | Optional dropped-frame worker that adjusts video bitrate between floor and target. |
| GPU | GPU acceleration is preserved. NVENC and accelerated CEF require matching hardware and real hardware proof. |

Pulsar does not claim that every OBS UI feature or third-party OBS plugin is
available. The supported surface is the protocol, vendor namespaces and bundle
contents built by this repository.

## NPM packages

| Package | Contents |
|---|---|
| [@clodocapeo/pulsar-client](https://www.npmjs.com/package/@clodocapeo/pulsar-client) | Typed TypeScript client only; no native runtime. |
| [@clodocapeo/pulsar-bundle](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle) | Client plus lean Windows runtime. |
| [@clodocapeo/pulsar-bundle-full](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle-full) | Client plus full Windows runtime, obs-browser/CEF, text and VLC support. |

The two bundle packages expose the same spawn() API. Prism uses the full bundle
for browser-rendered scenes.

## Quick start

~~~bash
npm install @clodocapeo/pulsar-bundle-full
~~~

~~~js
// live.mjs
import { spawn } from "@clodocapeo/pulsar-bundle-full";

const key = process.env.TWITCH_STREAM_KEY;
if (!key) throw new Error("TWITCH_STREAM_KEY is required");

const pulsar = await spawn({
  readyTimeoutMs: 60_000,
  onLog: (stream, line) => {
    if (/error|fail|connect|rtmp/i.test(line)) {
      console.log("[pulsar/" + stream + "] " + line);
    }
  },
});

const destination = await pulsar.client.destinations.create({
  name: "Twitch",
  kind: "twitch",
  key,
});

if (!await pulsar.client.destinations.start(destination.id)) {
  await pulsar.shutdown();
  throw new Error("Twitch destination did not start");
}

console.log("Twitch destination started");

const shutdown = async () => {
  await pulsar.client.destinations.stop(destination.id);
  await pulsar.client.destinations.remove(destination.id);
  await pulsar.shutdown();
};

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
~~~

~~~bash
TWITCH_STREAM_KEY=live_xxx node live.mjs
~~~

For Prism, the renderer must not recreate this lifecycle. Prism starts Pulsar,
receives PULSAR_READY, connects over loopback, and keeps preview and on-air
destinations independent.

## Runtime handshake

After initialization Pulsar prints exactly one readiness line:

~~~text
PULSAR_READY ws=ws://127.0.0.1:<port> password=<session-password>
~~~

The host reads the line without logging the password, connects with those
credentials, treats missing readiness as startup failure, and waits for clean
process exit during shutdown. PULSAR_PORT and PULSAR_PASSWORD exist for
controlled test harnesses; production hosts should let Pulsar generate them.

See [docs/PRISM-EMBEDDING.md](docs/PRISM-EMBEDDING.md).

## Client and vendor APIs

pulsar-client exposes obs, destinations, video, adaptive, record, stream and
audio namespaces.

The vendor namespaces are separate:

- pulsar: destinations, video settings and adaptive bitrate.
- pulsar-scene: managed browser-source capture.

The separate pulsar-scene name is required because obs-websocket permits only
one registration per vendor name. Scene capture is a v5 CallVendorRequest with
vendorName pulsar-scene.

Destination kinds:

- twitch ignores the input URL and pins a TLS Twitch ingest.
- rtmp_custom requires an rtmp:// or rtmps:// URL and a non-empty key.
- vod_local requires a fully resolved output path and does not add a timestamp.

Use the typed Twitch destination for Twitch. StartStream remains for v5
compatibility but is not the recommended Twitch path.

## Scene capture

Prism renders Solar in a local scene server and asks Pulsar to capture it:

~~~json
{
  "vendorName": "pulsar-scene",
  "requestType": "SetCaptureSource",
  "requestData": {
    "kind": "browser_source",
    "url": "http://127.0.0.1:<scene-port>/scene",
    "width": 1920,
    "height": 1080,
    "fps": 60,
    "reroute_audio": false
  }
}
~~~

Pulsar removes older Pulsar-managed capture items from known scenes so a stale
CEF page cannot survive a switch. The browser source stays alive while Solar
changes the rendered scene inside the page.

The full bundle is required. Without obs-browser/CEF,
SetCaptureSource returns browser_source_unavailable. See
[plugins/pulsar-scene-source/README.md](plugins/pulsar-scene-source/README.md).

## Boot configuration

Restart Pulsar to change these values.

| Variable | Default | Purpose |
|---|---:|---|
| PULSAR_RESOLUTION | 1920x1080 | Output canvas size. |
| PULSAR_FPS | 60 | 24, 30, 48, 60 or 120 FPS. |
| PULSAR_VIDEO_ENCODER | x264 | x264, nvenc, qsv, amf or auto. |
| PULSAR_VIDEO_BITRATE | 6000 | Video bitrate, 200-50000 kbps. |
| PULSAR_VIDEO_RATE_CONTROL | CBR | H.264 rate control. |
| PULSAR_VIDEO_PROFILE | high | baseline, main or high. |
| PULSAR_VIDEO_KEYINT_SEC | 2 | Keyframe interval, 0-20 seconds. |
| PULSAR_AUDIO_BITRATE | 160 | AAC bitrate, 32-512 kbps. |
| PULSAR_AUDIO_TRACKS | 1 | Audio track count, 1-6. |
| PULSAR_CAPTURE_WINDOW | unset | Window target in <title>:<class>:<exe>; unset produces black frames. |
| PULSAR_RECORD_DIR | <cwd>/recordings | Singleton recording directory. |
| PULSAR_ADAPTIVE_BITRATE | enabled | Set to off to disable the worker. |
| PULSAR_NATIVE_STINGER | disabled | Experimental native stinger path. |
| PULSAR_BROWSER_GPU | runtime dependent | CEF browser GPU path used by the accelerated probe. |

Invalid or unavailable encoder choices fall back to x264 with a warning.
Pulsar does not disable GPU acceleration to make a test pass.

## Build from source

### Requirements

- Windows x64.
- Visual Studio 2022 with C++ desktop workload and MSVC.
- CMake, PowerShell and Git with submodule support.
- Node.js 22.
- Python 3.11.
- FFmpeg for media inspection and live evidence probes.

~~~powershell
git submodule update --init --recursive
npm ci
.\scripts\build-win.ps1 -Full
~~~

CI uses the runtime directory:

~~~text
upstream/build_x64/rundir/RelWithDebInfo/
~~~

Create distributions:

~~~powershell
.\scripts\package-win.ps1 -Variant light -Zip
.\scripts\package-win.ps1 -Variant full -Zip
~~~

Use -SkipBuild only when the runtime was built from the same source revision.

## Validation and proof

~~~powershell
npm run lint
npm run build
npm test
.\scripts\run-probes.ps1
~~~

The offline probes cover readiness, WebSocket authentication, sources, scenes,
destinations, recording, adaptive bitrate, encoder contracts and failures.

Real CEF/PGM compatibility is opt-in and needs a real binary. Accelerated
coverage needs a physical GPU:

~~~powershell
$env:PULSAR_LIVE_CAPTURE_COMPAT = "1"
$env:PULSAR_BUNDLE_FULL_BINARIES_PATH = "D:\path\to\upstream\build_x64\rundir\RelWithDebInfo"
npm run test -w @clodocapeo/capture-pgm-compat
~~~

A hosted runner without a physical GPU is not proof of NVENC or accelerated
CEF. The release-grade Twitch pipeline records the encoded output and attaches
diagnostic.json and pulsar-live-broadcast-proof.mp4. A CEF screenshot or
successful WebSocket call is not antenna proof.

## Release manifest

CI generates and attaches prism-pulsar-runtime-manifest.json:

~~~json
{
  "schema_version": "prism.component.release.v1",
  "component": "pulsar",
  "version": "2.0.0",
  "release_tag": "v...",
  "artifact_name": "pulsar-windows-x64-full-v2.0.0.zip",
  "artifact_url": "https://github.com/ZabLaboratory/Pulsar/releases/download/v.../pulsar-windows-x64-full-v2.0.0.zip",
  "artifact_sha256": "..."
}
~~~

The release tag and digest are generated by CI. Do not hand-edit this manifest
or copy a digest from another archive.

## Repository layout

~~~text
Pulsar/ßuÁ‚ùÁ\∫w^~)ﬁvÈ›y¯ßy– upstream/                  OBS source submodule
∫w^~)ﬁvÈ›y¯ßy€ßuÁ‚ùÁ@ patches/                   numbered upstream patches
∫w^~)ﬁvÈ›y¯ßy€ßuÁ‚ùÁ@ plugins/                   headless, websocket, streams and scene sourceßuÁ‚ùÁ\∫w^~)ﬁvÈ›y¯ßy– packages/                  client, bundles and internal proof tooling
È›y¯ßy€ßuÁ‚ùÁ@∫w^~)ﬁt scripts/                   build, package and probe automationßuÁ‚ùÁ\∫w^~)ﬁvÈ›y¯ßy– docs/                      protocol, embedding and development contracts
È›y¯ßy€ßuÁ‚ùÁ@∫w^~)ﬁt .github/workflows/         CI, live proof and release pipeline
~~~

## Scope and non-goals

- Windows x64 is the supported runtime target.
- Pulsar does not replace Prism, Orion, Solar, ZabCanvas, ZabTruth, ZabRanking,
  Quasar or ZabCam.
- Current first-class destination kinds are twitch, rtmp_custom and vod_local.
- Resolution, FPS and encoder family are not switchable during a live session.
- The typed client does not auto-reconnect; the host decides the retry policy.
- The native stinger path is experimental and disabled by default.

## License

The Pulsar runtime and OBS-derived plugins are GPL-2.0-or-later. The TypeScript
client is MIT because it communicates over WebSocket and does not link libobs.

Consumers bundling the runtime must follow
[LICENSE-INVARIANTS.md](LICENSE-INVARIANTS.md) and
[CONSUMER-AUDIT.md](CONSUMER-AUDIT.md).

## Further documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)
- [docs/PROTOCOL.md](docs/PROTOCOL.md)
- [docs/PRISM-EMBEDDING.md](docs/PRISM-EMBEDDING.md)
- [plugins/pulsar-multi-stream/README.md](plugins/pulsar-multi-stream/README.md)
- [plugins/pulsar-scene-source/README.md](plugins/pulsar-scene-source/README.md)
- [packages/pulsar-client/README.md](packages/pulsar-client/README.md)
- [packages/pulsar-bundle/README.md](packages/pulsar-bundle/README.md)
- [packages/pulsar-bundle-full/README.md](packages/pulsar-bundle-full/README.md)
