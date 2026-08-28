# Pulsar — Architecture

This document describes the V1 architecture as actually shipped. Phase
plans and historical notes live in `CHANGELOG.md` and
`docs/DEVELOPMENT.md`.

## Process model

Pulsar is a long-running headless service. The reference embedder
(Prism) spawns it at boot, talks to it over a localhost WebSocket, and
shuts it down on exit. From the operator's point of view there is one
application; from the OS's point of view there are two processes.

```
┌────────────────────────────────────┐
│  Consumer (Prism, your app, ...)   │
│  separate licence — UI / scenes /  │
│  automation                        │
└──────────┬─────────────────────────┘
           │ WebSocket on 127.0.0.1:<random>
           │ obs-websocket v5 + pulsar:* vendor
           ▼
┌────────────────────────────────────┐
│  pulsar.exe   (single Win32 process, GPL-2.0-or-later)
│                                    │
│  ┌─────────────────────────────┐   │
│  │ pulsar-headless             │   │  service-mode lifecycle, idle loop,
│  │  (= the executable's main)  │   │  AttachConsole stdio, READY sentinel
│  ├─────────────────────────────┤   │
│  │ pulsar-frontend-stub        │   │  obs_frontend_callbacks vtable,
│  │  (static lib)               │   │  scene + encoder + output bring-up
│  ├─────────────────────────────┤   │
│  │ pulsar-multi-stream.dll     │   │  destinations registry (rtmp_custom,
│  │                             │   │  vod_local, twitch), encoder fan-out,
│  │                             │   │  adaptive bitrate worker
│  ├─────────────────────────────┤   │
│  │ pulsar-websocket.dll        │   │  forked obs-websocket v5.7.3 with
│  │                             │   │  Qt/forms stripped, pulsar:* vendor
│  │                             │   │  namespace dispatcher
│  ├─────────────────────────────┤   │
│  │ pulsar-browser.dll          │   │  forked obs-browser (full bundle only),
│  │  + pulsar-browser-page.exe  │   │  CEF helper exe in obs-plugins/64bit/
│  ├─────────────────────────────┤   │
│  │ libobs + obs-studio plugins │   │  capture · audio · encode · output
│  └─────────────────────────────┘   │
└──────────┬─────────────────────────┘
           │ NVENC / x264 / QSV / AMF
           ▼
   Twitch · RTMP custom · local MP4
```

## Licence boundary

The split into two processes is **the legal boundary** that keeps the
GPL of libobs from propagating to consumers. Pulsar therefore exposes
itself only as a stand-alone executable; never as a library, FFI, or
native module. The only sanctioned channel is the WebSocket protocol
on a loopback socket.

Four invariants enforce this. They live in
[`LICENSE-INVARIANTS.md`](../LICENSE-INVARIANTS.md) and are gated by
the `lint` + `binary-gate` jobs in `.github/workflows/pipeline.yml`:

1. **Process boundary.** `pulsar.exe` is always a separate OS process.
   Nobody loads `pulsar.exe`, `pulsar-*.dll`, `libcef.dll`, or any
   libobs binary into another address space.
2. **WebSocket-only IPC.** No FFI, no shared memory, no native bindings.
3. **No FFI surface on Pulsar's side.** `pulsar.exe` and
   `pulsar-browser-page.exe` export zero symbols. Plugin DLLs export
   only the OBS module ABI (12-symbol whitelist in
   `scripts/check-binary-exports.ps1`).
4. **No source copy-paste.** Consumer code never includes lines copied
   from libobs / obs-websocket / obs-browser source trees.

## Repo layout

```
Pulsar/
├── upstream/         git submodule → obsproject/obs-studio @ pinned tag
├── patches/          NNNN-name.patch — applied to upstream/ at build time
├── plugins/          Pulsar-owned plugins, additive features
│   ├── pulsar-headless/        the pulsar.exe entry point
│   ├── pulsar-frontend-stub/   obs_frontend_callbacks + scene/encoder bring-up
│   ├── pulsar-websocket/       fork of obs-websocket v5
│   ├── pulsar-multi-stream/    destinations + adaptive bitrate
│   └── pulsar-browser/         fork of obs-browser (full bundle only)
├── packages/         npm packages
│   ├── pulsar-client/          MIT, no native deps
│   ├── pgm-correlator/         MIT, no native deps -- PGM/Orion-identity time correlation (#230)
│   ├── pulsar-bundle/          GPL, ships pulsar.exe (light)
│   └── pulsar-bundle-full/     GPL, ships pulsar.exe (with CEF)
├── scripts/          build, package, probes, CI orchestration
├── docs/             this directory
└── .github/workflows/pipeline.yml
```

## Fork strategy

Three layers, each with a clear discipline. Prefer the topmost layer
that can express a change.

| Layer | When to use it | Cost |
|---|---|---|
| **Plugin** under `plugins/` | New feature that fits libobs's plugin model (a new source kind, output kind, vendor request handler, signal listener). | Cheap. No upstream coordination. |
| **Patch** under `patches/` | Change that cannot live as a plugin: a tweak to libobs's build, a license metadata update, a hook into headless boot. | Each patch is one more thing to maintain across upstream rebases. Aim to upstream it. |
| **Upstream PR** | Anything generally useful to OBS Studio. | Wait for upstream review, but the patch goes away once merged. |

The discipline: **plugin → upstream PR → patch**, in that order.
`patches/` is intended to shrink as we upstream what we can. `plugins/`
is where Pulsar's identity lives.

## Build pipeline

Implemented in `scripts/build-win.ps1`. Idempotent — every run resets
`upstream/` to the recorded SHA, replays patches, and rebuilds.

1. `git submodule update --init --recursive` initialises `upstream/`.
2. Reset `upstream/` to `git submodule status --cached`.
3. Replay every `patches/*.patch` in lexical order via `git am`.
4. Configure: `cmake --preset windows-x64 -S upstream` plus Pulsar
   overrides (`ENABLE_FRONTEND=OFF`, `ENABLE_UI=OFF`,
   `ENABLE_BROWSER=OFF` in light mode; `-Full` flips them on for the
   full bundle).
5. Build: `cmake --build --preset windows-x64 --config RelWithDebInfo`.
6. Pulsar plugins compile against the freshly built libobs.
7. Output lands under
   `upstream/build_x64/rundir/RelWithDebInfo/{bin,obs-plugins,data}/`.

First run is ~25–30 min on a typical machine (obs-deps + Qt6 + CEF
download once into the cache); incremental rebuilds are seconds.

`scripts/package-win.ps1 -Zip [-Full]` wraps a curated subset of the
rundir into the distributable zip:

| Variant | Size | Plugins included |
|---|---|---|
| **light** (`pulsar-windows-x64-v<version>.zip`) | ~40 MB compressed, ~100 MB extracted | encoders, capture (window/monitor/game/dshow), WASAPI, ffmpeg muxer, multi-stream, websocket |
| **full** (`pulsar-windows-x64-full-v<version>.zip`) | ~150 MB compressed, ~370 MB extracted | the above + obs-browser + CEF + obs-text + text-freetype2 + vlc-video |

Both variants share an always-stripped list (obs-vst, nv-filters,
obs-webrtc, decklink, frontend-tools, obs-libfdk) — those are deliberate
omissions documented in `scripts/package-win.ps1`.

## Boot sequence

1. `pulsar.exe` starts. `wWinMain` calls `AttachConsole(ATTACH_PARENT_PROCESS)`
   so direct invocation from cmd.exe / PowerShell still prints to the
   operator's terminal. Spawned with piped stdio, the inherited pipes
   take precedence and AttachConsole is a no-op.
2. The bootstrap resolves a validated `runtime_instance_id`, creates its
   private runtime directory, acquires OS-backed identity and cwd leases, and
   opportunistically acquires the singleton DirectShow legacy-alias lease. A
   second claimant remains usable through namespaced mappings and emits a
   correlated refusal record.
3. A `QApplication` is constructed with `QT_QPA_PLATFORM=minimal` (no
   display, no platform plugin DLL).
4. `obs_startup()` initialises libobs with the runtime directory as its
   module-config path.
5. `seed_websocket_config()` writes `<runtime-dir>/obs-websocket/config.json`
   from `PULSAR_PORT` + `PULSAR_PASSWORD` env vars (or defaults: an
   allocated loopback port + a fresh 22-char URL-safe random string). This happens *before*
   plugins load so `obs-websocket.dll`'s config loader reads the
   seeded values rather than a stale on-disk copy from a prior run.
6. `obs_load_all_modules()` loads ~25 OBS plugins + the Pulsar plugins.
7. `pulsar-frontend-stub` brings up the scene graph: a `Default` scene
   with WASAPI mic + desktop capture + (optional) window capture, an
   x264 video encoder + AAC audio encoder, a singleton `PulsarStream`
   rtmp_output, a singleton `PulsarRecord` ffmpeg_muxer.
8. `pulsar-multi-stream` initialises its registry + adaptive bitrate
   worker.
9. `pulsar-websocket` binds the configured port on `127.0.0.1` (and
   `::1`) and starts accepting v5 handshakes.
10. `pulsar-headless` prints the sentinel:
   `PULSAR_READY ws=ws://127.0.0.1:<port> password=<pw>`
11. The idle loop polls a graceful-shutdown atomic every 100 ms and renews
     the identity/cwd/alias lease metadata. On shutdown, libobs is stopped
     before all leases are released.
    `Ctrl-C` (in a real terminal) or `WM_CLOSE` (from a parent
    process's `taskkill` / `child.kill()`) flips the flag; the loop
    exits and `obs_shutdown()` runs to completion.

## Embedding contract

A consumer (Prism today, others later) must:

- Bundle the chosen variant under `resources/pulsar/`, preserving the
  rundir layout (`bin/64bit/`, `obs-plugins/64bit/`, `data/`).
- Resolve the executable from `bin/64bit/pulsar.exe`, but give each process
  a private runtime cwd and pass a validated `PULSAR_RUNTIME_INSTANCE_ID`.
  The native bootstrap resolves OBS modules/data from the executable and
  uses the runtime cwd for config, logs and recordings.
- Pass `PULSAR_PORT` + `PULSAR_PASSWORD` env vars to pin per-session
  credentials when required; otherwise the bundle allocates a free loopback
  port and parses the generated values from the READY sentinel.
- Treat the returned runtime identity as the correlation key. If an external
  DirectShow consumer needs a non-holder's dedicated mapping, launch it with
  the same `PULSAR_RUNTIME_INSTANCE_ID` and
  `PULSAR_DIRECTSHOW_LEGACY_ALIAS=0`.
- Read stdout line-by-line until `^PULSAR_READY ` arrives; extract
  `url` + `password`; open the obs-websocket v5 session.
- Hold the connection open for the lifetime of broadcast work.
  Reconnect with the same password on transient drops.
- Stop via WebSocket close + process termination on shutdown. Never
  `taskkill /F` first — it skips `obs_shutdown` and leaks encoder
  threads.

The full step-by-step contract — including the exact file listing the
consumer must ship and the spawn-helper pseudocode — lives in
[`PRISM-EMBEDDING.md`](PRISM-EMBEDDING.md).

## Non-goals

- **Cloud rendering.** Pulsar runs on the operator's machine. Cloud
  broadcast pipelines belong elsewhere.
- **Mobile.** No iOS / Android targets. Mobile companion apps drive
  Pulsar remotely via the protocol, they do not embed it.
- **Replacement for OBS Studio.** Pulsar targets programmatic /
  embedded use cases. Operators who want a desktop UI keep using OBS
  — this fork actively excludes Qt to make the headless cost cheap.
- **macOS / Linux for V1.** Windows x64 only. Mac and Linux are
  deferred until a consumer needs them; the process model and
  patches/plugins discipline are platform-agnostic, but the build
  scripts are not yet ported.
