# Pulsar

[![npm pulsar-client](https://img.shields.io/npm/v/%40clodocapeo%2Fpulsar-client?label=%40clodocapeo%2Fpulsar-client&logo=npm&color=cb3837)](https://www.npmjs.com/package/@clodocapeo/pulsar-client)
[![npm pulsar-bundle](https://img.shields.io/npm/v/%40clodocapeo%2Fpulsar-bundle?label=%40clodocapeo%2Fpulsar-bundle&logo=npm&color=cb3837)](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle)
[![npm pulsar-bundle-full](https://img.shields.io/npm/v/%40clodocapeo%2Fpulsar-bundle-full?label=%40clodocapeo%2Fpulsar-bundle-full&logo=npm&color=cb3837)](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle-full)
[![GitHub release](https://img.shields.io/github/v/release/ZabLaboratory/Pulsar?logo=github)](https://github.com/ZabLaboratory/Pulsar/releases/latest)
[![Pipeline](https://github.com/ZabLaboratory/Pulsar/actions/workflows/pipeline.yml/badge.svg)](https://github.com/ZabLaboratory/Pulsar/actions/workflows/pipeline.yml)
[![Licence GPL-2.0-or-later](https://img.shields.io/badge/licence-GPL--2.0--or--later-blue)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-windows--x64-0078d4)](#install)
[![libobs 32.1.2](https://img.shields.io/badge/libobs-32.1.2-7c8f9f)](https://github.com/obsproject/obs-studio/releases/tag/32.1.2)

> **Headless broadcast engine for embedding.** A fork of OBS Studio shaped into a service: no UI, no installer, no operator workflow. Boots, listens on a localhost WebSocket, and answers obs-websocket v5 + a `pulsar:*` vendor namespace. Your application drives it.

Pulsar takes the encoder pipeline, capture stack, audio graph, scene compositor, and plugin model that make OBS Studio the de-facto broadcast engine, strips the desktop application around it, and exposes the result as a typed Node API. Every capability the OBS UI surfaces — multi-source scenes, x264 / NVENC / QSV / AV1 encoders, RTMP / SRT / HLS / WHIP outputs, WASAPI / CoreAudio audio capture, GPU-composited transitions, browser overlays, native game capture via DLL injection, recording — is reachable over the wire.

It is built for products that need broadcast capabilities without inheriting OBS as a user-facing application: control stations, operator desktops, headless servers, automation rigs, Electron hosts. You ship `pulsar.exe` next to your binary, spawn it at boot, talk to it like any other service.

---

## What it does

| Capability | Detail |
|---|---|
| **Headless service** | `pulsar.exe` boots libobs with a minimal Qt platform (no display surface). Binds a session-random WebSocket on `127.0.0.1`, prints a session JWT on stdout for the host to pick up. No installer, no tray icon, no window. |
| **obs-websocket v5 baseline** | 155 of the v5 standard requests work out of the box. Stream Deck, Streamer.bot, Companion, Aitum, every existing OBS automation plugs in unchanged. |
| **Pulsar vendor namespace** | `pulsar:*` requests on top of v5 for capabilities the baseline does not model: multi-destination first-class, adaptive bitrate control, per-process audio capture, live encoder retuning, recording paths. |
| **Multi-destination native** | One encoder pair fans out to N outputs (`encode-once / fan-out-N`). Twitch, RTMP custom and local MP4 are first-class destination kinds — not third-party plugin extensions. Add a destination, start it, stop it, all over the wire. |
| **Adaptive bitrate** | Background worker samples `obs_output_get_frames_dropped` every 2s, scales bitrate within `[floor, target]`, emits `pulsar:BitrateAdjusted` events. Bandwidth-aware streaming without a babysitter. |
| **1080p60 by default** | Configurable via `PULSAR_FPS` (24 / 30 / 48 / 60 / 120) and `PULSAR_RESOLUTION` (`<W>x<H>` up to 8K). x264 + AAC encoders pre-tuned: CBR 6000 / 160 kbps, keyint 2s, preset `veryfast`, profile `high`, tune `zerolatency`. Live-tunable via `pulsar:SetVideoSettings`. |
| **WASAPI audio graph** | Microphone (channel 3), desktop loopback (channel 1), per-process loopback for app/Meet capture (channel 2, opt-in). Independent levels, mute, monitoring per channel. |
| **Window capture** | Windows Graphics Capture under the hood, addressable by `Title:ClassName:Process.exe`. Works against any composited window — including Chromium / Electron renderers. |
| **Game capture via DLL injection** *(full bundle)* | The same `graphics-hook32/64.dll` + `inject-helper` + `get-graphics-offsets` chain OBS Studio uses. Captures DirectX 9 / 10 / 11 / 12, OpenGL, Vulkan applications by hooking the present chain. |
| **Browser sources via CEF** *(full bundle)* | `obs-browser` plus the Chromium Embedded Framework runtime. HTML / CSS / JS overlays running in a real Chromium, composited GPU-side, controllable from the host. |
| **Native text + VLC sources** *(full bundle)* | `obs-text` (FreeType-rendered text sources) and `vlc-video` (libVLC-backed media playback). |
| **Recording pipeline** | `ffmpeg_muxer` MP4 writer with auto-timestamped paths under `<cwd>/recordings/`. Concurrent with streaming — same encoders, no double-encoding. |
| **Process boundary preserved** | The single IPC channel is the loopback WebSocket. No FFI, no shared memory, no native bindings. The host application never links libobs — its licence is unaffected. |

End-to-end validated against a live Twitch ingest: 1080p60 frames + audio pushed for 30s on commodity bandwidth, no drops, audio confirmed via ffprobe (`codec=aac, channels=2, sample_rate=48000`).

---

## Live broadcast proof

Every push runs the live-broadcast job inside the [`pipeline.yml` workflow](.github/workflows/pipeline.yml) on a clean `windows-2022` runner — it builds Pulsar from source, spawns `pulsar.exe`, opens a hand-coded HTML/CSS/JS scene through CEF + `browser_source` (Apple-keynote intro, telemetry HUD bound to live `pulsar:GetAdaptiveState` data, Web Audio sound design), and pushes a real broadcast to Twitch using the project's stream key. Throughout, an in-process `StartRecord` writes the same broadcast to a local MP4 — same encoders, same source, same frames the Twitch ingest receives. The MP4 is then re-encoded with `ffmpeg -c:v libx264 -preset fast -crf 23` (typically 5–20× smaller than the source CBR 6 Mbps) and uploaded.

| Trigger | Duration | Where the MP4 lands |
|---|---|---|
| Push to a feature branch | **1 min** smoke | workflow artefact (90 days retention) |
| Push to `main` (post-PR merge) | **30 min** release-grade | workflow artefact **+ GitHub Pages** (replaces the README inline player) |
| Push tag `v*.*.*` | **30 min** release-grade | workflow artefact + GitHub Pages **+ GitHub Release** asset (download) |
| `workflow_dispatch` (manual) | configurable | workflow artefact |

What you can play below is the broadcast produced for the **latest release-grade run** (push to `main` or tag `v*.*.*`). The MP4 is published to GitHub Pages on every release-grade run so it streams inline in the player below — no download required. If the player shows 404 / never loads, no release-grade run has fired yet since the workflow was wired up; the file populates on the next push to `main`.

<video src="https://zablaboratory.github.io/Pulsar/pulsar-live-broadcast-proof.mp4" controls preload="metadata" width="720">
  Your browser doesn't support inline MP4 playback.
  <a href="https://zablaboratory.github.io/Pulsar/pulsar-live-broadcast-proof.mp4">Stream the proof MP4 directly</a>.
</video>

[➡️ Stream inline (GitHub Pages)](https://zablaboratory.github.io/Pulsar/pulsar-live-broadcast-proof.mp4) · [download from latest release](https://github.com/ZabLaboratory/Pulsar/releases/latest/download/pulsar-live-broadcast-proof.mp4) · [browse all pipeline runs](https://github.com/ZabLaboratory/Pulsar/actions/workflows/pipeline.yml)

The probe asserts on the metric side too: `GetDestinations[id].active == true` every 5 s, `GetAdaptiveState.samples` strictly increasing, frame drop ratio < 5 %. The MP4 is the visible byproduct; the gate is the assertion suite.

---

## Quick start — go live in 50 lines

The `live.mjs` script below boots Pulsar, creates a Twitch destination, starts streaming, and parks until you `Ctrl-C`. That is the full surface for "stream this to Twitch from a Node app".

```js
// live.mjs
import { spawn } from "@clodocapeo/pulsar-bundle";

const key = process.env.TWITCH_KEY;
if (!key) {
  console.error("TWITCH_KEY env var required");
  process.exit(1);
}

console.log("[live] spawning pulsar...");
const pulsar = await spawn({
  readyTimeoutMs: 60_000,
  onLog: (stream, line) => {
    if (
      line.includes("rtmp stream") ||
      line.includes("Connection to") ||
      line.includes("Connecting to") ||
      line.includes("error") ||
      line.toLowerCase().includes("fail")
    ) {
      console.log(`  [pulsar/${stream}] ${line}`);
    }
  },
});

console.log(`[live] pulsar ready -- libobs ${pulsar.libobsVersion}, ws :${pulsar.port}`);

const dest = await pulsar.client.destinations.create({
  name: "smoke-test",
  kind: "twitch",
  key,
});
console.log(`[live] destination created -- id=${dest.id}, kind=${dest.kind}`);
console.log(`[live] pinned URL: ${dest.url}`);

const started = await pulsar.client.destinations.start(dest.id);
if (!started) {
  console.error("[live] start failed");
  await pulsar.shutdown();
  process.exit(2);
}
console.log("[live] *** LIVE on Twitch ***");

let shuttingDown = false;
const shutdown = async (sig) => {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`\n[live] ${sig} received, stopping...`);
  await pulsar.client.destinations.stop(dest.id);
  await pulsar.client.destinations.remove(dest.id);
  await pulsar.shutdown();
  process.exit(0);
};
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT",  () => shutdown("SIGINT"));

await new Promise(() => {}); // park
```

```bash
npm install @clodocapeo/pulsar-bundle
TWITCH_KEY=live_xxx node live.mjs
```

That is it. `spawn()` returns a connected, typed client. `destinations.create()` / `start()` / `stop()` / `remove()` is the entire multi-destination surface. The `onLog` hook surfaces libobs's stdout/stderr if you need to look inside.

---

## npm packages

Three packages, one project. Pick the one that matches your use case.

| Package | Ships | Platform | Tarball | Postinstall | Use when |
|---|---|---|---|---|---|
| [`@clodocapeo/pulsar-client`](https://www.npmjs.com/package/@clodocapeo/pulsar-client) | Typed TS wrapper over obs-websocket v5 + `pulsar:*` vendor namespace. ESM, no native deps. | any (Node ≥ 18) | ~ 18 kB | none | You already have a Pulsar (or any v5 server) running and just want to talk to it. Browser tools, CLI utilities, test harnesses. |
| [`@clodocapeo/pulsar-bundle`](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle) | The above + `pulsar.exe` and stripped runtime + `spawn()` API. | windows-x64 | ~ 5 kB | ~ 40 MB zip | You want streaming + recording + window/display capture + WASAPI audio. Lean payload, no browser sources. |
| [`@clodocapeo/pulsar-bundle-full`](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle-full) | The above + `obs-browser` + CEF runtime + `obs-text` + `vlc-video`. | windows-x64 | ~ 5 kB | ~ 150 MB zip | You need HTML overlays, native game capture, text sources or VLC-backed media. Composed scene workflows. |

`pulsar-bundle` and `pulsar-bundle-full` expose **the exact same `spawn()` API** — they differ only in the binary payload downloaded at install time. Switching from one to the other is a `package.json` rename, no code change.

```ts
// identical:
import { spawn } from "@clodocapeo/pulsar-bundle";
import { spawn } from "@clodocapeo/pulsar-bundle-full";
```

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
                       │       scene Default + window/game/browser src  │
                       │       audio: wasapi mic + desktop + process    │
                       │       encoders: x264 + aac (shared)            │
                       │   ↓                                            │
                       │   pulsar-multi-stream.dll                      │
                       │       destinations registry, encoder fan-out,  │
                       │       adaptive bitrate worker                  │
                       │   ↓                                            │
                       │   pulsar-websocket.dll  (forked obs-websocket) │
                       │       v5 handshake + pulsar:* vendor namespace │
                       └─────────────────────┬──────────────────────────┘
                                             │ WebSocket :random loopback
                                             │ (obs-websocket v5 + pulsar:*)
                                             ▼
       ┌─────────────────────────────────────┴─────────────────────────────────────┐
       │                                                                           │
   Your Node host                Stream Deck                Companion / Aitum / etc.
   @clodocapeo/pulsar-bundle     obs-websocket plugin       any v5 client
```

The single IPC channel is the obs-websocket on loopback. **No FFI, no shared memory, no native bindings** between Pulsar and any host application — the process boundary preserves the host's licence under the GPL-2.0 inherited from libobs (mere aggregation, not derivative work).

A typical deployment: the host process spawns `pulsar.exe` at boot, captures the JWT on stdout, opens a WebSocket on the printed port, drives it for the rest of the session, and shuts it down on exit. From the operator's perspective there is one application; from the OS's perspective there are two processes communicating over localhost.

---

## Why a fork

OBS Studio is the broadcast engine. It is not a library, not an SDK, not a service — it is a desktop application around libobs that solves the operator problem extremely well. Programmatic use cases — Streamlabs Desktop, Streamer.bot, Aitum, broadcast control stations, automation rigs — each work around the desktop framing with their own bridges, patches, or partial forks. Pulsar consolidates that work into a project designed from day one to be embedded:

- **Headless first.** Service mode is the default. The Qt UI from upstream is excluded at build time, not deleted from the source tree — rebases stay tractable.
- **Multi-destination native.** Twitch + RTMP custom + VOD local as first-class entities, addressable through one API call. Not "one stream output plus replay buffer plus a third-party multi-rtmp plugin".
- **Stable rebases on upstream.** `upstream/` is a git submodule pinned to a tagged OBS release; our changes live in numbered patches under `patches/` (applied at build) and Pulsar-owned plugins under `plugins/`. Tracking new OBS releases is a submodule bump + maybe a patch refresh.
- **Strict process boundary.** Hosts don't link Pulsar in. The WebSocket-only IPC keeps everyone's licences clean.
- **Two distribution sizes.** Lean (40 MB) for hosts that compose scenes themselves and just need encoders + capture + outputs. Full (150 MB) for hosts that need browser sources / game capture / VLC media.

---

## Use cases

The shape of "headless OBS over WebSocket" fits any application that needs broadcast capabilities without becoming OBS:

- **Broadcast control stations.** Operator desktops that drive multi-stream live productions — esports, talk shows, tournaments — with custom UI tailored to the production rather than OBS's general-purpose surface. Scenes live in the host application; Pulsar handles the encode and the outputs.
- **Composed-scene workflows.** Hosts that compose the canvas in HTML/Chromium and feed it to broadcast — scoreboards, telemetry overlays, dynamic graphics, animation engines. The full bundle ships `obs-browser` so the same DOM tree the operator sees is what gets encoded.
- **Multi-stream productions.** Encode once, fan out to Twitch + a private RTMP for a co-host + a local MP4 archive — without paying the encoder cost three times. Multi-destination is a first-class API, not a plugin.
- **Automation rigs.** CI-driven broadcast checks, scheduled streams, headless recording on a server. No display, no Qt, no DPI scaling problems.
- **Electron / desktop hosts.** Embed Pulsar as a child process; ship one product to your users; preserve your codebase's licence via the WebSocket boundary.

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
│   ├── pulsar-client/         npm @clodocapeo/pulsar-client (TS source)
│   ├── pulsar-bundle/         npm @clodocapeo/pulsar-bundle (lean: encoders + capture)
│   └── pulsar-bundle-full/    npm @clodocapeo/pulsar-bundle-full (+ CEF + game capture)
├── scripts/
│   ├── build-win.ps1        upstream + plugins build pipeline
│   ├── package-win.ps1      strip + zip into dist/pulsar-windows-x64-vX.Y.Z[-full]/
│   ├── probe-events.py      phase-5 events sanity check
│   ├── probe-record.py      phase-6/9/12a record probe (asserts fps + bitrate + AAC)
│   ├── probe-multi-stream.py  phase-7 vendor API probe
│   └── probe-adaptive.py    phase-12b adaptive worker orchestration probe
├── docs/
│   ├── ARCHITECTURE.md
│   ├── PROTOCOL.md           v5 + pulsar:* vendor reference
│   ├── PRISM-EMBEDDING.md    consumer-side spawn / handshake / lifecycle contract
│   └── DEVELOPMENT.md
├── .github/workflows/
│   └── pipeline.yml         single workflow, 9 jobs (lint, build, binary-gate,
│                            offline-probes, live-broadcast, publish-gh-pages,
│                            package, release-attach, npm-publish). One build,
│                            shared artefact across the gates.
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
.\scripts\build-win.ps1                    # configure + build upstream + plugins
.\scripts\package-win.ps1 -Zip             # lean zip into dist/
.\scripts\package-win.ps1 -Zip -Full       # full zip (with CEF) into dist/
```

`build-win.ps1` is idempotent: it resets `upstream/` to the recorded SHA, replays patches with `git am`, runs upstream's `windows-x64` CMake preset (which auto-fetches obs-deps + Qt6 + CEF), then builds the Pulsar plugins on top. First run is ~25–30 min on a typical machine; incremental rebuilds are seconds.

See `docs/DEVELOPMENT.md` for tooling prerequisites (CMake ≥ 3.28, Visual Studio 2022 BuildTools, git LFS not required).

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0–4 | Bootstrap, build pipeline, headless service, Qt-minimal QApp, obs-websocket fork | shipped |
| 5 | `pulsar-frontend-stub` — obs_frontend_callbacks vtable | shipped |
| 6 | Record pipeline (window_capture + x264 + ffmpeg_muxer MP4) | shipped |
| 7 | `pulsar-multi-stream` — destinations vendor API (rtmp_custom, vod_local, twitch) | shipped |
| 9 | WASAPI audio sources (mic, desktop, per-process loopback) | shipped |
| 12 | 1080p60 default + bitrate config + adaptive worker | shipped |
| 12.5 | Packaging script + GitHub Release pipeline | shipped |
| 13a | `@clodocapeo/pulsar-client` (typed TS wrapper) | shipped & published |
| 13b | `@clodocapeo/pulsar-bundle` (lean binary + spawn API) | shipped & published |
| 14 | `@clodocapeo/pulsar-bundle-full` (CEF + game capture + text + VLC) | shipped & published |
| Deferred | YouTube Live destination + OAuth | needs Google Cloud project |
| Deferred | macOS / Linux builds | Windows-only for current consumers |

---

## Licence

GPL-2.0-or-later, inherited from libobs and non-negotiable for anything linking Pulsar's binaries (= every plugin under `plugins/` and the upstream OBS modules). The `packages/pulsar-client/` TypeScript wrapper is MIT (no GPL link), since it speaks to Pulsar over WebSocket only — process boundary breaks GPL propagation.

See [`LICENSE`](LICENSE) and the GPL-2.0 text in `upstream/COPYING`.

### Embedding Pulsar in another application — read this first

The "process boundary breaks GPL propagation" line above is **not magic**. It works only if four invariants are honoured by the consumer (Prism today, any future Pulsar-bundling app tomorrow). Each invariant being broken is enough to retroactively re-license every consumer that has shipped against the breach.

Three documents you must read before bundling Pulsar :

- ➡️ **[`LICENSE-INVARIANTS.md`](LICENSE-INVARIANTS.md)** — the non-negotiable contract : the four invariants, the tempting designs to refuse on sight, the watchdog point on the npm wrapper.
- ➡️ **[`docs/PRISM-EMBEDDING.md`](docs/PRISM-EMBEDDING.md)** — the consumer-side spawn / handshake / lifecycle contract. Mandatory `cwd`, `PULSAR_PORT` / `PULSAR_PASSWORD` env knobs, `PULSAR_READY` stdout sentinel parsing, shutdown protocol. Read this before writing the spawn helper.
- ➡️ **[`CONSUMER-AUDIT.md`](CONSUMER-AUDIT.md)** — the empirical checklist your consumer repo must enforce in CI. Every claim is a runnable script, every script has a pass/fail signal. Includes a copy-pasteable bash script + GitHub Actions workflow for static + binary-linkage checks (Windows / macOS / Linux). **If you have not run the scripts, you have not passed the audit.**

Pulsar's own CI enforces the source-side and binary-side invariants on every push and PR. Everything lives in the single [`pipeline.yml`](.github/workflows/pipeline.yml) workflow — one build, multiple gates that share its artefact.

| Job (in `pipeline.yml`) | Trigger | What it gates |
|---|---|---|
| `lint` | every PR + push to main | source-grep (no `__declspec(dllexport)` / `napi_*` / `node-gyp` / `prism` / `electron`), patches apply cleanly, plugins carry metadata, npm tarball content audit |
| `build` | every PR + push to main | full Windows build via `scripts/build-win.ps1 -Full`, uploads `pulsar-rundir` artefact consumed by the rest |
| `binary-gate` | every PR + push to main | `scripts/check-binary-exports.ps1` over `pulsar.exe`, `pulsar-browser-page.exe`, and every plugin DLL (only the OBS module ABI symbols allowed) |
| `offline-probes` | every PR + push to main | ctest run (`scripts/run-probes.ps1`) — websocket handshake, source kinds, events, adaptive bitrate, recording |
| `live-broadcast` | every PR + push to main | end-to-end Twitch broadcast probe with diagnostic JSON + MP4 recording |
| `publish-gh-pages` | push to main + tag | publishes the broadcast MP4 to GitHub Pages so the README inline player streams the latest run |
| `package` | tag `v*.*.*` push | runs `scripts/package-win.ps1 -Zip` for both light + full variants |
| `release-attach` | tag `v*.*.*` push | `softprops/action-gh-release` with the zips + MP4 + diagnostic JSON attached |
| `npm-publish` | tag `v*.*.*` push | publishes the three packages (`pulsar-client`, `pulsar-bundle`, `pulsar-bundle-full`) to npm |

The four invariants (process boundary / WebSocket-only IPC / no FFI / no copy-paste) are enforced **on Pulsar's side** by `lint` + `binary-gate`. The boundary on YOUR side (the consumer that bundles Pulsar) is yours to enforce — that's what `CONSUMER-AUDIT.md` is for.
