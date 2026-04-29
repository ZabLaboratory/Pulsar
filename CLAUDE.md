# Pulsar

@../../docs/rules/git.md
@../../docs/rules/security.md
@../../docs/rules/agents.md
@../agents/_shared/architecture.md
@../agents/_shared/conventions.md
@../agents/_shared/deploy.md
@../agents/_shared/projects.md

## Description

Pulsar is the broadcast engine that powers Prism. It is a fork of
OBS Studio (libobs + plugins) stripped to a headless service that
exposes its scene graph, sources, encoders, and outputs over a
WebSocket protocol (obs-websocket v5 compat + Pulsar extensions).
Prism bundles Pulsar in its installer and spawns it transparently at
boot — the user sees only Prism.

## Stack

| Layer | Technology |
|---|---|
| Engine | libobs (C, C++) — vendored as `upstream/` git submodule |
| Build | CMake + per-platform native toolchains |
| Plugins | C++ (Pulsar-owned plugins under `plugins/`) |
| Protocol | WebSocket (obs-websocket v5 baseline + Pulsar extensions) |
| Distribution | platform-native binaries packaged as release artefacts |

**Pulsar does not follow Zab Python conventions.** No FastAPI, no uv,
no `/api/v1`, no `/health` HTTP endpoint, no `JWT_SECRET`. These
conventions apply to network-exposed Python services on the
`zab_network`. Pulsar is a local-process bundled with Prism, never
on the shared network — different layer, different rules.

## Licence

**GPL-2.0-or-later** — inherited from libobs, non-negotiable. Anything
linking Pulsar (statically or dynamically) becomes a derivative work
under GPL. Prism stays under its own licence by communicating with
Pulsar **only over the WebSocket process boundary** (mere aggregation,
not derivative work).

## Architecture

See `docs/ARCHITECTURE.md` for the full picture. Key invariants:

1. **Process boundary with Prism.** Pulsar runs as an OS-level child
   process spawned by Prism's main process; the only IPC is the
   WebSocket protocol on a localhost port. No FFI, no shared memory,
   no native bindings linked into Prism.
2. **Upstream tracking via patches.** All modifications to obs-studio
   live in `patches/` as numbered patch files applied at build time.
   No direct edits in `upstream/`. This keeps rebases on new OBS
   releases tractable.
3. **Pulsar features live in plugins.** Multi-destination streaming,
   service mode, extended websocket — each is a Pulsar-owned plugin
   under `plugins/`, not a patch on upstream.
4. **No GUI.** Service mode is the default. The Qt UI from upstream
   is excluded at build time, not deleted from the source tree.

## Setup local

Build environment is platform-specific — see `docs/DEVELOPMENT.md`.

## Conventions

- **Patches.** One concern per patch, numbered `NNNN-short-name.patch`.
  Each patch ships with a header comment explaining the rationale and
  whether it is a candidate for upstream submission.
- **Plugins.** Each Pulsar plugin owns its CMakeLists.txt, README, and
  protocol surface. No cross-plugin includes — communicate via libobs
  signals or websocket events.
- **Versioning.** Semver at the top-level CMakeLists.txt. Pin in
  Prism via exact version match — minor bumps require Prism updates.
- **Branches & commits.** Inherits root rules (`@../../docs/rules/git.md`).
  `feature/`, `fix/`, `hotfix/`, `chore/`. Squash merge only.

## Inter-project

| Consumer | How |
|---|---|
| Prism | Bundles `pulsar.exe` (or platform equivalent) in `resources/pulsar/`, spawns at boot, talks WebSocket on a session-random localhost port with a session JWT. |
| Other (future) | Anything that speaks obs-websocket v5 works against Pulsar baseline. Pulsar-specific features require the Pulsar protocol extensions. |

## Resolution criteria

Each branch is **resolved after merge** when:

1. Squash merge effected on `main` by the maintainer.
2. CI green on the merge commit (build matrix Win/Mac/Linux).
3. Release artefacts produced for the target platform(s).
4. Prism updated to consume the new version (if minor/major bump).
5. Branch deleted from remote.

## Status

Pre-alpha. No binary yet. See `CHANGELOG.md` for milestones.
