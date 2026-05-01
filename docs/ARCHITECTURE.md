# Pulsar — Architecture

## Process model

Pulsar is a long-running service process. The reference embedder
(Prism) spawns it at boot, talks to it over WebSocket, and stops it
on shutdown. Pulsar has no UI and is invisible to the end user.

```
┌────────────────────────────────────┐
│  Prism (Electron, separate licence)│
│  UI · scene editor · automation    │
└──────────┬─────────────────────────┘
           │ WebSocket localhost (loopback-only, JWT-authenticated)
           ▼
┌────────────────────────────────────┐
│  Pulsar (GPL-2.0-or-later)         │
│  ┌─────────────────────────────┐   │
│  │ pulsar-headless             │   │  service-mode lifecycle
│  ├─────────────────────────────┤   │
│  │ pulsar-websocket            │   │  obs-websocket v5 + extensions
│  ├─────────────────────────────┤   │
│  │ pulsar-multi-stream         │   │  destinations API
│  ├─────────────────────────────┤   │
│  │ libobs + obs-studio plugins │   │  capture · encode · output
│  └─────────────────────────────┘   │
└──────────┬─────────────────────────┘
           │ NVENC / x264 / VideoToolbox / etc.
           ▼
   Twitch · YouTube · RTMP custom · local VOD
```

## Licence boundary

The split into two processes is **the legal boundary** that keeps the
GPL of libobs from propagating to Prism. Pulsar must therefore never
expose itself as a library, FFI, or native module to its embedder. The
only sanctioned channel is the WebSocket protocol over a loopback
socket.

## Fork strategy

```
Pulsar/
├── upstream/   submodule → obsproject/obs-studio @ pinned tag
├── patches/    NNNN-name.patch — applied on upstream/ at build time
└── plugins/    Pulsar-owned plugins, additive features
```

- **Patches** are reserved for changes that cannot live as a plugin:
  build-system tweaks, license metadata, headless-mode hooks. We aim
  to keep `patches/` shrinking by upstreaming as many as possible.
- **Plugins** are where Pulsar's identity lives. New features that can
  be expressed as additive libobs plugins go there.

The discipline: **prefer plugin → upstream PR → patch**, in that order.

## Build pipeline (planned)

1. `git submodule update --init --recursive` initialises `upstream/`.
2. Patches in `patches/` are applied lexically (`git am`) onto the
   `upstream/` working tree.
3. CMake configures `upstream/` with the OBS toolchain, with Qt /
   front-end disabled, plus our plugins enabled.
4. Build produces:
   - `pulsar.exe`: the service binary, a thin entry into libobs that
     loads our plugins by default.
   - Two Windows distribution variants (`scripts/package-win.ps1`):
     `pulsar-windows-x64-v<version>.zip` (light, ~100 MB, no CEF) and
     `pulsar-windows-x64-full-v<version>.zip` (full, ~370 MB, with CEF
     + obs-browser for `browser_source` rendering).
5. `release.yml` publishes both variants as GitHub Release artefacts on
   every `vX.Y.Z` tag push.

V1 ships Windows x64 only. macOS (Phase 7) and Linux (Phase 8) are
deferred — `scripts/build-mac.sh` and `scripts/build-linux.sh` exit 1
with a clear "not implemented" message.

## Embedding contract

A consumer of Pulsar (Prism today, others later) must:

- Bundle the Pulsar distribution under `resources/pulsar/` preserving
  the rundir layout (`bin/64bit/`, `obs-plugins/64bit/`, `data/`).
- Spawn `bin/64bit/pulsar.exe` with `cwd=bin/64bit`, optionally setting
  `PULSAR_PORT` and `PULSAR_PASSWORD` to pin per-session credentials.
- Read stdout line-by-line until the `PULSAR_READY ws=ws://127.0.0.1:<port>
  password=<pw>` sentinel arrives; parse it and use the credentials to
  authenticate the obs-websocket v5 session.
- Keep the WebSocket connection open for the duration of broadcast
  work; reconnect with the same password on transient drops.
- Stop the service via WebSocket close + process termination on
  consumer shutdown.

See **[`PRISM-EMBEDDING.md`](PRISM-EMBEDDING.md)** for the full step-by-step
contract, including the exact directory listing the consumer must ship.

## Non-goals

- **Cloud rendering.** Pulsar runs on the operator's machine. Cloud
  broadcast pipelines belong to a different project (Orion handles
  the relay side).
- **Mobile.** No iOS / Android targets. Mobile companion apps drive
  Pulsar remotely via the protocol, they do not embed it.
- **Replacement for OBS Studio.** Pulsar targets programmatic / embedded
  use cases. Operators who want a desktop UI keep using OBS — this
  fork actively excludes Qt to make the headless cost cheap.
