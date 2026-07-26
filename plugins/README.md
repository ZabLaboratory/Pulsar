# plugins/

Pulsar-owned plugins. Each plugin is a self-contained CMake target
linked against libobs from `../upstream/`.

## Inventory

| Plugin | Purpose |
|---|---|
| `pulsar-headless` | Service-mode runner: starts libobs without Qt, manages lifecycle, exposes signals to the websocket plugin. |
| `pulsar-websocket` | Fork of `obs-websocket` with the v5 protocol baseline preserved + Pulsar extensions (multi-destination, scene streaming, session auth). |
| `pulsar-multi-stream` | First-class multi-destination output: registers Twitch / YouTube / RTMP / VOD destinations as libobs outputs, exposes per-destination state through `pulsar-websocket`. |
| `pulsar-frontend-stub` | Static library (not an OBS plugin) providing the `obs_frontend_*` callback vtable — Default scene, transition, encoders, sources, outputs — since Pulsar runs no Qt frontend. Linked into `pulsar.exe`, orchestrated by `pulsar-headless`'s `main()`. |
| `pulsar-browser` | Fork of `obs-browser` (CEF-backed `browser_source`) — required at runtime (`-Full` build) for any `browser_source` capture, including the one `pulsar-scene-source` creates. |
| `pulsar-scene-source` | Exposes the `pulsar-scene` vendor namespace (`SetCaptureSource` / `GetCaptureSource`) for swapping the broadcast capture source to a CEF `browser_source` live, decoupling the host app's window from the broadcast canvas (Phase 13.4). |

See `docs/PROTOCOL.md` for the `pulsar` and `pulsar-scene` vendor namespaces, and its `PULSAR_*` environment variable table for every plugin/stub boot-time knob.

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
