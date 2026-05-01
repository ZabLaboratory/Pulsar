# pulsar-scene-source

A small libobs plugin that exposes two vendor requests on the
**`pulsar-scene`** namespace for swapping the broadcast capture source
live :

```
CallVendorRequest("pulsar-scene", "SetCaptureSource", { kind, url, width?, height?, fps?, reroute_audio?, css? })
CallVendorRequest("pulsar-scene", "GetCaptureSource", {})
```

> **Why a distinct vendor namespace (`pulsar-scene`, not `pulsar`)** : obs-websocket's `vendor_register_cb` rejects a second register on the same vendor name. The second plugin to call it gets NULL and its requests never bind. Each Pulsar plugin therefore owns its own namespace — `pulsar-multi-stream` keeps `pulsar` (destinations + adaptive bitrate + video settings), `pulsar-scene-source` owns `pulsar-scene` (capture source).

This is the Pulsar side of the **Phase 13.4** integration : Prism (or
any future Pulsar-bundling app) drops the legacy `window_capture` —
which would tie the broadcast geometry to the host window — and asks
Pulsar to start a CEF `browser_source` pointed at a local scene server
URL. The DOM rendered inside Pulsar's own CEF subprocess becomes the
captured frame source ; the host application is decoupled from the
broadcast canvas size.

## Vendor requests

### `pulsar-scene:SetCaptureSource`

Replace the active capture source with a fresh `browser_source` on the
current frontend scene. Removes any previously-installed Pulsar-managed
capture items (named `PulsarCapture` from `pulsar-frontend-stub` or
`PulsarSceneSource` from a previous call) before adding the new one.

| Field | Type | Default | Notes |
|---|---|---|---|
| `kind` | string | required | Phase 13.4 ships `"browser_source"` only. `"window_capture"` revert lands in 13.4.b. |
| `url` | string | required | URL the CEF browser source loads. Loopback in practice (Prism's scene server). |
| `width` | int | `1920` | Browser source render width. |
| `height` | int | `1080` | Browser source render height. |
| `fps` | int | `60` | Browser source render fps. |
| `reroute_audio` | bool | `false` | Mix the page's HTML audio into Pulsar's audio bus rather than letting the OS handle it. |
| `css` | string | `""` | Optional CSS injected by obs-browser (e.g. `body{background:transparent}`). |

**Response (success)** : `{ kind, url, width, height, fps, reroute_audio, removed_prior }` — `removed_prior` is the number of managed scene items the call dropped (0 if none, 1 typically — `PulsarCapture` from boot, or the previous `PulsarSceneSource`).

**Errors** :

| `error` | When |
|---|---|
| `"kind_not_supported"` | `kind` is anything other than `"browser_source"`. |
| `"url_required"` | `url` missing or empty. |
| `"browser_source_unavailable"` | obs-browser plugin not loaded (build without `-Full` ?). |
| `"no_current_scene"` | `obs_frontend_get_current_scene()` returned null. |
| `"current_source_not_a_scene"` | The frontend's "current scene" source didn't unwrap into an `obs_scene_t`. |
| `"scene_add_failed"` | `obs_scene_add` returned null. |

### `pulsar-scene:GetCaptureSource`

Return the active capture source state.

**Response** :

| Field | Type | Notes |
|---|---|---|
| `kind` | string | `"browser_source"` after a successful Set, `"window_capture"` if Set has never been called (frontend-stub's default). |
| `url` | string | Present when `kind == "browser_source"`. |
| `width` / `height` / `fps` / `reroute_audio` | int / bool | Same as Set. |
| `last_change_unix` | int | Unix timestamp of the last successful Set, `0` if none yet. |

## Why a separate plugin

`pulsar-scene-source` could in principle live inside `pulsar-multi-stream`
or `pulsar-frontend-stub`. We give it its own DLL because :

- The two vendor requests are unrelated to the destinations API
  (`pulsar-multi-stream`'s scope) and don't belong in the frontend
  callback shim (`pulsar-frontend-stub`'s scope).
- A consumer that doesn't need scene-source swapping (e.g. a CLI bot
  using Pulsar purely for RTMP fan-out) can ship without it by passing
  `-DPULSAR_BUILD_SCENE_SOURCE=OFF`.
- Failure modes are isolated : if obs-browser isn't loaded, the
  request returns `browser_source_unavailable` and the rest of Pulsar
  keeps working.

The two plugins do NOT share a vendor namespace : `pulsar-multi-stream` owns `pulsar` (destinations + adaptive bitrate + video settings), `pulsar-scene-source` owns `pulsar-scene` (capture source). Forced because `obs_websocket_register_vendor` rejects a duplicate vendor name in our fork's `vendor_register_cb`. No cross-plugin includes.

## Build

Driven by the top-level `CMakeLists.txt` :

```
option(PULSAR_BUILD_SCENE_SOURCE "Build the pulsar-scene-source plugin" ON)
```

The build script `scripts/build-win.ps1` picks it up automatically. The
`-Full` flag is required if you want the runtime to actually execute
the request (it pulls obs-browser + CEF) ; the plugin compiles without
`-Full` but `SetCaptureSource` will return `browser_source_unavailable`.

## License

GPL-2.0-or-later, inherited from libobs. See the repo root `LICENSE`
and [`LICENSE-INVARIANTS.md`](../../LICENSE-INVARIANTS.md) for what
this means for consumers that bundle Pulsar.
