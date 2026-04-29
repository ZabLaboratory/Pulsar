# pulsar-websocket

Vendor fork of [`obsproject/obs-websocket`](https://github.com/obsproject/obs-websocket)
v5.7.3. Provides the WebSocket protocol surface that drives Pulsar
from external clients (Stream Deck, Streamer.bot, Aitum, Prism, any
v5-compatible client).

## Status

Phase 4c — **source vendored, build wiring deferred to Phase 4d**.
The plugin sources live here under `src/`, with the upstream Qt UI
(`forms/SettingsDialog`, `forms/ConnectInfo`) removed and the
frontend-api migration calls (`MigrateGlobalConfigData`,
`MigratePersistentData`) reduced to no-ops so the plugin will load
inside the headless `pulsar-headless` service. The CMakeLists.txt
build target is **not yet active** — Phase 4d wires it.

## Why a fork

Upstream obs-websocket assumes the Qt-based obs-studio frontend is
present:

- `forms/SettingsDialog.cpp` and `forms/ConnectInfo.cpp` construct
  Qt widgets at module load time (a Tools menu entry plus a settings
  dialog). With `ENABLE_FRONTEND=OFF`, no frontend exists, no main
  window exists, and `obs_frontend_get_main_window()` returns null.
  Constructing the dialog with a null parent in a process that has no
  main window crashes the service.
- `Config::MigrateGlobalConfigData()` and `Config::MigratePersistentData()`
  call `obs_frontend_get_app_config()` and
  `obs_frontend_get_current_profile_path()` to migrate legacy
  obs-studio config files. Without a registered frontend, both return
  null, and the migrate path dereferences them.

The fork drops the UI code and stubs out the migrate calls (Pulsar
starts from a clean state — there are no legacy obs-studio configs
to migrate). The websocket server itself, the v5 protocol, the event
handlers and the request handlers are unchanged: this fork stays as
close to upstream as possible so v5 compat remains exact.

## Layout

```
plugins/pulsar-websocket/
├── CMakeLists.txt            -- build wiring (Phase 4d)
├── README.md                 -- this file
├── UPSTREAM-LICENSE          -- GPL-2.0 from obs-websocket
├── cmake/                    -- vendored upstream cmake helpers
│   ├── obs-websocket-api.cmake
│   ├── obs-websocket-apiConfig.cmake.in
│   └── macos/Info.plist.in
├── data/locale/en-US.ini     -- text strings the plugin reads via obs_module_text
├── lib/
│   ├── example/              -- upstream sample client (kept for reference)
│   └── obs-websocket-api.h   -- public C API for other plugins
└── src/                      -- the plugin itself
    ├── obs-websocket.cpp     -- entry point; Qt UI block stripped
    ├── obs-websocket.h
    ├── Config.cpp            -- Migrate* functions stubbed to no-ops
    ├── Config.h
    ├── WebSocketApi.cpp
    ├── WebSocketApi.h
    ├── plugin-macros.h.in
    ├── eventhandler/         -- libobs signal -> v5 event translation
    ├── requesthandler/       -- v5 request type implementations
    ├── utils/                -- helpers (Crypto, Json, Obs, Compat...)
    └── websocketserver/      -- WebSocket++ server, v5 framing
```

## Patches applied

The diff between this tree and upstream's `plugins/obs-websocket/`
is intentionally small:

1. **`src/forms/` directory removed** — drops `SettingsDialog`,
   `ConnectInfo`, `*.ui`, `images/`, `resources.qrc`. No Qt UI.
2. **`src/obs-websocket.cpp`** — the `#include "forms/SettingsDialog.h"`,
   the `SettingsDialog *_settingsDialog` global, and the
   `obs_frontend_*` Tools-menu wiring inside `obs_module_load()` are
   replaced with a comment block explaining the fork.
3. **`src/Config.cpp`** — `MigrateGlobalConfigData()` returns an
   empty `json{}`, `MigratePersistentData()` only ensures the
   module config directory exists. Neither calls the frontend API.

## Phase 4d plan

- Author the build wiring in `CMakeLists.txt`. Mirror upstream's
  source list (minus `src/forms/*`), find the deps (websocketpp,
  asio, nlohmann_json, qrcodegencpp) from
  `upstream/.deps/obs-deps-*-x64/`, link `Qt6::Core` + `Qt6::Network`
  + `OBS::libobs` (the latter via direct `obs.lib` reference like
  `pulsar-headless` does).
- Set the output to `obs-websocket.dll` alongside the other plugins
  in `rundir/obs-plugins/64bit/` so libobs's default module path
  picks it up.
- Re-enable `PULSAR_BUILD_WEBSOCKET=ON` by default in the top-level
  Pulsar CMakeLists.

## Phase 4e

Validate the v5 round-trip with an external client (e.g.
`obsws-python`) connecting to `ws://127.0.0.1:<port>` after the
plugin has produced its session JWT on `pulsar.exe` stdout.
