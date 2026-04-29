# plugins/

Pulsar-owned plugins. Each plugin is a self-contained CMake target
linked against libobs from `../upstream/`.

## Inventory

| Plugin | Purpose |
|---|---|
| `pulsar-headless` | Service-mode runner: starts libobs without Qt, manages lifecycle, exposes signals to the websocket plugin. |
| `pulsar-websocket` | Fork of `obs-websocket` with the v5 protocol baseline preserved + Pulsar extensions (multi-destination, scene streaming, session auth). |
| `pulsar-multi-stream` | First-class multi-destination output: registers Twitch / YouTube / RTMP / VOD destinations as libobs outputs, exposes per-destination state through `pulsar-websocket`. |

## Why plugins, not patches

Pulsar features that **add** functionality belong here, not in
`../patches/`. Patches are reserved for changes to upstream behaviour
that cannot be expressed as plugins (build system tweaks, headless
mode hooks, license metadata). This split keeps upstream rebases small.

## Conventions

- Each plugin owns its CMakeLists.txt, README, and protocol surface.
- No cross-plugin includes. Communicate via libobs signals, websocket
  events, or shared types declared in a plugin's public header.
- Each plugin targets the same libobs ABI as `../upstream/` — bumping
  the upstream submodule may require recompiling all plugins, which
  is what the top-level CMake is for.
