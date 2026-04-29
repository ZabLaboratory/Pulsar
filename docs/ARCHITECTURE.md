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
   - `pulsar` (or `pulsar.exe` on Windows): the service binary, a
     thin entry into libobs that loads our plugins by default.
   - Platform-native package (`.zip` Win, `.tar.gz` Mac/Linux) with
     the binary, libobs `.so/.dll`, plugins, locales, and resource
     bundle.
5. CI (Phase 1+) publishes the package as a GitHub Release artefact.

## Embedding contract

A consumer of Pulsar (Prism today, others later) must:

- Bundle the platform-appropriate Pulsar package alongside its own
  binary.
- Spawn the service with a session-random loopback port and capture
  the JWT printed on Pulsar's stdout at boot.
- Keep the WebSocket connection open for the duration of broadcast
  work; reconnect with the same JWT on transient drops.
- Stop the service on consumer shutdown via the protocol's `Shutdown`
  request and `SIGTERM` / `taskkill` as fallback.

## Non-goals

- **Cloud rendering.** Pulsar runs on the operator's machine. Cloud
  broadcast pipelines belong to a different project (Orion handles
  the relay side).
- **Mobile.** No iOS / Android targets. Mobile companion apps drive
  Pulsar remotely via the protocol, they do not embed it.
- **Replacement for OBS Studio.** Pulsar targets programmatic / embedded
  use cases. Operators who want a desktop UI keep using OBS — this
  fork actively excludes Qt to make the headless cost cheap.
