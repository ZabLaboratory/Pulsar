# Pulsar

Open-source headless broadcast engine — a modernised fork of OBS Studio
focused on programmatic control, multi-destination streaming, and
embedding inside other applications.

Pulsar runs as a background process. It does not ship a UI of its own.
Applications drive Pulsar over WebSocket (obs-websocket v5 protocol +
Pulsar extensions for multi-destination streaming). The reference
embedding is Prism, the Zablab broadcast control station, which bundles
Pulsar in its installer and spawns it transparently at boot.

## Status

Pre-alpha scaffolding. No working binary yet. See `docs/ARCHITECTURE.md`
for the target design and `docs/DEVELOPMENT.md` for the build plan.

## Why this exists

OBS Studio is the de-facto open-source broadcast engine but its
distribution model is built around the desktop app. Programmatic use
cases — Streamlabs Desktop, Stream Deck, Streamer.bot, Aitum, our own
Prism — each work around this with their own bridges, plugins, and
forks. Pulsar consolidates that work into a project designed from the
start to be embedded.

Goals:

- **Headless first.** Service mode is the default, GUI is optional.
- **Multi-destination native.** Twitch + YouTube + RTMP + local VOD as
  first-class entities, not plugin extensions.
- **Modern Electron compat.** Stays current with active Electron LTS.
- **Stable rebases on upstream.** Patches kept small and isolated so
  we follow obs-studio releases without dragging.

## Licence

GPL-2.0-or-later (inherited from libobs). See `LICENSE`.

## Repo layout

```
Pulsar/
├── upstream/        git submodule pointing at obsproject/obs-studio
├── patches/         our patches against upstream/, applied at build time
├── plugins/         Pulsar's own plugins (websocket fork, multi-stream, headless)
├── scripts/         per-platform build scripts
├── docs/            architecture, protocol, development guides
├── .github/         CI workflows + CODEOWNERS
├── CMakeLists.txt   top-level cmake entry
├── CHANGELOG.md
├── LICENSE
└── README.md
```

## Build

Not yet implemented. Build pipeline ships in Phase 1 — see
`docs/DEVELOPMENT.md`.

## Companion project

[Prism](https://github.com/ZabLaboratory/Prism) is the reference
embedder. Pulsar is functional standalone but its first consumer drives
the protocol design.
