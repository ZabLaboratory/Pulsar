# Pulsar

[![npm pulsar-client](https://img.shields.io/npm/v/%40clodocapeo%2Fpulsar-client?label=%40clodocapeo%2Fpulsar-client&logo=npm&color=cb3837)](https://www.npmjs.com/package/@clodocapeo/pulsar-client)
[![npm pulsar-bundle](https://img.shields.io/npm/v/%40clodocapeo%2Fpulsar-bundle?label=%40clodocapeo%2Fpulsar-bundle&logo=npm&color=cb3837)](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle)
[![GitHub release](https://img.shields.io/github/v/release/ZabLaboratory/Pulsar?logo=github)](https://github.com/ZabLaboratory/Pulsar/releases/latest)
[![Release workflow](https://github.com/ZabLaboratory/Pulsar/actions/workflows/release.yml/badge.svg)](https://github.com/ZabLaboratory/Pulsar/actions/workflows/release.yml)
[![Licence GPL-2.0-or-later](https://img.shields.io/badge/licence-GPL--2.0--or--later-blue)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-windows--x64-0078d4)](#install)
[![libobs 32.1.2](https://img.shields.io/badge/libobs-32.1.2-7c8f9f)](https://github.com/obsproject/obs-studio/releases/tag/32.1.2)

> **Headless broadcast engine for embedding.** Drives Twitch, RTMP custom and local MP4 from a Node host with a typed TypeScript client. Built on a vendored OBS Studio submodule, controlled exclusively over obs-websocket v5 + Pulsar's `pulsar:*` vendor namespace.

`pulsar.exe` is a service: no UI, binds a session-random WebSocket on `127.0.0.1`, and answers v5 requests. Your application drives it. The reference embedder is [Prism](https://github.com/ZabLaboratory/Prism), the broadcast control station for the Zablab platform.

---

## Install

```bash
npm install @clodocapeo/pulsar-bundle
```

The postinstall step downloads the matching version's
`pulsar-windows-x64-vX.Y.Z.zip` from this repo's [Releases](https://github.com/ZabLaboratory/Pulsar/releases) and extracts it into `node_modules/@clodocapeo/pulsar-bundle/binaries/`. **Windows x64 only** — `os: ["win32"]` in the `package.json` makes `npm install` skip the binary on every other platform.

```ts
import { spawn } from "@clodocapeo/pulsar-bundle";

const pulsar = await spawn({
  env: {
    PULSAR_CAPTURE_WINDOW: "Untitled - Notepad:Notepad:notepad.exe",
  },
});

const dest = await pulsar.client.destinations.create({
  kind: "twitch",
  key: process.env.TWITCH_KEY!,
});
await pulsar.client.destinations.start(dest.id);

pulsar.client.on("bitrateAdjusted", (e) =>
  console.log(`bitrate -> ${e.bitrate} kbps (${e.reason})`));

await pulsar.shutdown();
```

Installable client-only (no binary, useful for browser/CLI tools that talk to a Pulsar already running):
```bash
npm install @clodocapeo/pulsar-client
```

---

## What's in v0.1.0

| Capability | Detail |
|---|---|
| **Service mode** | `pulsar.exe` headless on Windows x64. Boots libobs, loads plugins, exposes obs-websocket v5 on `127.0.0.1:4455` (random password generated on first run). |
| **1080p60 default** | Configurable via `PULSAR_FPS` (24/30/48/60/120) and `PULSAR_RESOLUTION` (`<W>x<H>` up to 8K). |
| **x264 + AAC encoders** | CBR 6000 kbps + 160 kbps, keyint 2 s, preset `veryfast`, profile `high`, tune `zerolatency`. Live-tunable through `pulsar:SetVideoSettings`. |
| **Multi-destination** | First-class `rtmp_custom`, `vod_local`, `twitch` kinds. One encoder pair fans out to N outputs (encode-once / fan-out-N). |
| **Adaptive bitrate** | Background worker samples `obs_output_get_frames_dropped` every 2 s, scales bitrate within `[floor, target]`, emits `pulsar:BitrateAdjusted` events. |
| **Audio sources** | WASAPI desktop loopback (channel 1), microphone (channel 3), per-process loopback for app/Meet capture (channel 2, opt-in). |
| **Window capture** | Pulsar-specific source pointing at any Windows window (Windows Graphics Capture under the hood). Used for browser-composed scenes. |
| **Recording** | `ffmpeg_muxer` MP4 writer with auto-timestamped paths under `<cwd>/recordings/`. |
| **OBS-websocket v5 baseline** | 155 of the v5 standard requests work out of the box (Stream Deck, Streamer.bot, Companion, Aitum compat). |

End-to-end validated against a live Twitch ingest: 1080p60 frames + audio pushed for 30 s on commodity bandwidth, no drops, audio stream confirmed via ffprobe (`codec=aac, channels=2, sample_rate=48000`).

---

## Architecture

```
                       ┌────────────────────────────────────────────────┐
                       │  pulsar.exe   (single Win32 process, headless) │
                       │                                                │
                       │   QApplication("minimal")  (no display, no UI) │
                       │   ↓                                            │
                       │   libobs core   D3D11 compositor offscreen     │
                       │   ↓                                            │
                       │   pulsar-frontend-stub  obs_frontend_callbacks │
                       │       scene Default + window_capture           │
                       │       audio: wasapi mic + desktop + process    │
                       │       encoders: x264 + aac (shared)            │
                       │   ↓                                            │
                       │   pulsar-multi-stream.dll                      │
                       │       destinations registry, encoder fan-out,  │
                       │       adaptive bitrate worker                  │
                       │   ↓                                            │
                       │   pulsar-websocket.dll  (forked obs-websocket) │
                       │       v5 handshake + Pulsar vendor namespace   │
                       └─────────────────────┬──────────────────────────┘
                                             │ WebSocket :4455 loopback
                                             │ (obs-websocket v5 + pulsar:*)
                                             ▼
       ┌─────────────────────────────────────┴─────────────────────────────────────┐
       │                                                                           │
   Prism (Electron)             Stream Deck                Companion / Aitum / etc.
   @clodocapeo/pulsar-bundle    obs-websocket plugin       any v5 client
```

The single IPC channel is the obs-websocket on loopback. **No FFI, no shared memory, no native bindings** between Pulsar and any embedder — process-boundary preserves the embedder's licence under the GPL-2.0 inherited from libobs (mere aggregation, not derivative work).

---

## Why a fork

OBS Studio is the de-facto open-source broadcast engine, but its distribution model is built around the desktop app. Programmatic use cases — Streamlabs Desktop, Stream Deck, Streamer.bot, Aitum, [Prism](https://github.com/ZabLaboratory/Prism) — each work around this with their own bridges, plugins or partial forks. Pulsar consolidates that work into a project designed from the start to be embedded:

- **Headless first.** Service mode is the default, the Qt UI from upstream is excluded at build time.
- **Multi-destination native.** Twitch + RTMP custom + VOD local as first-class entities, not third-party plugin extensions.
- **Stable rebases on upstream.** `upstream/` is a git submodule pinned to a tag; our changes live in numbered patches under `patches/` (applied at build) and Pulsar-owned plugins under `plugins/`. Tracking new OBS releases is a submodule bump + maybe one or two patch refreshes.
- **Strict process boundary.** Embedders don't link Pulsar in. The WebSocket-only IPC keeps everyone's licences clean.

---

## npm packages

| Package | Purpose | Platform | Size |
|---|---|---|---|
| [`@clodocapeo/pulsar-client`](https://www.npmjs.com/package/@clodocapeo/pulsar-client) | Typed TypeScript wrapper over obs-websocket v5 + the `pulsar:*` vendor namespace. ESM, no native deps. | any (Node ≥ 18) | ~ 18 kB tarball |
| [`@clodocapeo/pulsar-bundle`](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle) | The above + `pulsar.exe` and dependencies, plus a `spawn()` API that returns a connected client. | windows-x64 | ~ 5 kB tarball + 40 MB postinstall |

---

## Repo layout

```
Pulsar/
├── upstream/                git submodule -> obsproject/obs-studio @ 32.1.2
├── patches/                 numbered .patch files applied to upstream/ at build
├── plugins/
│   ├── pulsar-headless/     pulsar.exe entry point, libobs init + idle loop
│   ├── pulsar-frontend-stub/  static lib: obs_frontend_callbacks vtable +
│   │                          scene/encoder/output bring-up
│   ├── pulsar-websocket/    vendored fork of obs-websocket v5 (forms/ stripped)
│   └── pulsar-multi-stream/ destinations + adaptive bitrate plugin
├── packages/
│   ├── pulsar-client/       npm @clodocapeo/pulsar-client (TS source)
│   └── pulsar-bundle/       npm @clodocapeo/pulsar-bundle (TS source + postinstall)
├── scripts/
│   ├── build-win.ps1        upstream + plugins build pipeline
│   ├── package-win.ps1      strip + zip into dist/pulsar-windows-x64-vX.Y.Z/
│   ├── probe-events.py      phase-5 events sanity check
│   ├── probe-record.py      phase-6/9/12a record probe (asserts fps + bitrate + AAC)
│   ├── probe-multi-stream.py  phase-7 vendor API probe
│   └── probe-adaptive.py    phase-12b adaptive worker orchestration probe
├── docs/
│   ├── ARCHITECTURE.md
│   ├── PROTOCOL.md          v5 + pulsar:* vendor reference
│   └── DEVELOPMENT.md
├── .github/workflows/
│   ├── ci.yml               PR-time lint + tests on packages/
│   ├── release.yml          tag push -> Windows build + zip + GitHub Release
│   └── publish-npm.yml      tag push -> publish both packages to npm
├── CMakeLists.txt           top-level entry; adds plugins, reads VERSION
├── VERSION                  single source of truth (consumed by C++ + npm + scripts)
└── CHANGELOG.md
```

---

## Build from source

Windows x64 only, MSVC toolchain.

```powershell
git clone --recurse-submodules https://github.com/ZabLaboratory/Pulsar
cd Pulsar
.\scripts\build-win.ps1               # configure + build upstream + plugins
.\scripts\package-win.ps1 -Zip        # strip + zip into dist/
```

`build-win.ps1` is idempotent: it resets `upstream/` to the recorded SHA, replays patches with `git am`, runs upstream's `windows-x64` CMake preset (which auto-fetches obs-deps + Qt6), then builds the Pulsar plugins on top. First run is ~25-30 min on a typical machine; incremental rebuilds are seconds.

See `docs/DEVELOPMENT.md` for tooling prerequisites (CMake ≥ 3.28, Visual Studio 2022 BuildTools, git LFS not required).

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0–4 | Bootstrap, build pipeline, headless service, Qt-minimal QApp, obs-websocket fork | ✅ shipped |
| 5 | `pulsar-frontend-stub` — obs_frontend_callbacks vtable | ✅ shipped |
| 6 | Record pipeline (window_capture + x264 + ffmpeg_muxer MP4) | ✅ shipped |
| 7 | `pulsar-multi-stream` — destinations vendor API (rtmp_custom, vod_local, twitch) | ✅ shipped |
| 8 | YouTube destination + OAuth Live Streaming API | ⏸ deferred (needs Google Cloud project) |
| 9 | WASAPI audio sources (mic, desktop, per-process loopback) | ✅ shipped |
| ~~10–11~~ | ~~macOS / Linux build~~ | ❌ out of scope (Pulsar bundles inside Prism's Windows installer) |
| 12 | 1080p60 default + bitrate config + adaptive worker | ✅ shipped |
| 12.5 | Packaging script + GitHub Release pipeline | ✅ shipped |
| 13a | `@clodocapeo/pulsar-client` (typed TS wrapper) | ✅ shipped & published |
| 13b | `@clodocapeo/pulsar-bundle` (binary + spawn API) | ✅ shipped & published |
| 13c | Prism integration — replace `overlay-broadcaster.ts` | 🚧 next |
| Final | Animation engine (CSS+JS sandbox via DOM capture) | ⏳ blocked on 13c |

---

## Companion project

[Prism](https://github.com/ZabLaboratory/Prism) — broadcast control station built on Electron. Reference embedder; bundles `pulsar.exe` in its installer and spawns it transparently at boot. Pulsar's protocol design is anchored on Prism's needs first; other v5 clients (Stream Deck, Companion, Streamer.bot) work via the baseline.

---

## Licence

GPL-2.0-or-later, inherited from libobs and non-negotiable for anything linking Pulsar's binaries (= every plugin under `plugins/` and the upstream OBS modules). The `packages/pulsar-client/` TypeScript wrapper is MIT (no GPL link), since it speaks to Pulsar over WebSocket only — process boundary breaks GPL propagation.

See [`LICENSE`](LICENSE) and the GPL-2.0 text in `upstream/COPYING`.
