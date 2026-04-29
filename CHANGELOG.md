# Changelog

All notable changes to Pulsar are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial repository scaffold: README, CLAUDE.md, top-level
  CMakeLists.txt skeleton, docs (architecture, protocol, development),
  patches/ + plugins/ + scripts/ placeholders, GitHub Actions workflow
  skeletons.
- LICENSE — GPL-2.0-or-later, verbatim text from gnu.org.
- `upstream/` git submodule wiring to `ZabLaboratory/obs-studio`
  (fork of `obsproject/obs-studio`), pinned to tag **32.1.2**
  (commit `fb4d98bf88fae5fc85cb11fc57f7c5e309282194`, released
  2026-04-21).
- Patch pipeline — `scripts/build-win.ps1` now resets `upstream/`
  to the recorded submodule SHA (read via `git submodule status
  --cached`) and applies every `patches/*.patch` via `git am` in
  lexical order before configure. Idempotent: each run starts from
  the pinned commit + N patches.
- `patches/0001-build-tag-OBS_VERSION-with-pulsar-suffix.patch` —
  appends a `-pulsar` suffix to the runtime `OBS_VERSION` string
  so any binary built from this fork is observable as such (window
  title, About dialog, log preamble). First demonstrator that the
  patch lifecycle works end-to-end. Validated runtime: window title
  reads `OBS 32.1.2-1-g<sha>-pulsar`.
- Headless build mode (Phase 3a). `scripts/build-win.ps1` defaults
  to disabling Qt frontend and the CEF browser source plugin via
  `-DENABLE_FRONTEND=OFF -DENABLE_UI=OFF -DENABLE_BROWSER=OFF`.
  No `obs64.exe`, no Qt6 DLLs in the rundir, libobs core + 25
  modules build. `User Interface` and `Browser sources are not
  enabled by default` listed under Disabled Features at configure
  time. Pass `-GuiBuild` to opt back in to the full obs-studio
  build for debugging or comparison.
- `-Clean` switch on `build-win.ps1` — wipes `upstream/build_x64`
  before configure so stale artefacts from a previous mode (e.g.
  obs64.exe + Qt6 DLLs left in rundir by a GUI build) do not
  contaminate a subsequent headless run. Dep caches under
  `upstream/.deps/` are preserved.
- **Phase 3b — first headless run.** New `pulsar.exe` executable
  built from `plugins/pulsar-headless/main.cpp` (37 KB), linked
  against the libobs that upstream produced. Phase 3b proof of
  life: the binary calls `obs_startup`, libobs initialises (CPU
  detection, default video canvas creation, etc.), prints
  `pulsar-headless: libobs 32.1.2-1-g<sha>-pulsar initialised`,
  then `obs_shutdown` cleans up. Exit code 0, no Qt loaded.
- **Phase 4a — long-running headless service.** `pulsar.exe`
  becomes a real service: configures default video (1080p30
  NV12 D3D11) via `obs_reset_video`, audio (48 kHz stereo) via
  `obs_reset_audio`, lets libobs's default module paths
  discover plugins, calls `obs_load_all_modules` +
  `obs_post_load_modules`. Wires `SetConsoleCtrlHandler` so
  Ctrl+C / window close / system shutdown flips an atomic
  `g_running` flag, then a 100 ms idle loop polls it. Graceful
  `obs_shutdown` on exit. ~66 MB RAM idling, vs 216 MB for the
  Qt obs64.exe. **20 plugins loaded exactly once** (not the
  duplicate registrations the first attempt produced).
- obs-deps runtime staging in `build-win.ps1`. Upstream's CMake
  copies FFmpeg / zlib DLLs to the rundir as part of the
  frontend build steps; with `ENABLE_FRONTEND=OFF` the copy
  never happens and `pulsar.exe` fails to load with
  `STATUS_DLL_NOT_FOUND` (0xC0000135) as soon as the loader
  resolves obs.dll's imports. The build script now stages every
  `*.dll` from `upstream/.deps/obs-deps-*/bin/` into
  `rundir/bin/64bit/` after the Pulsar plugin build.
- **`obs_add_module_path` removed from `pulsar-headless`.** libobs's
  built-in `add_default_module_paths()` (in
  `upstream/libobs/obs-windows.c:43`) already registers
  `../../obs-plugins/64bit/` and `../../data/obs-plugins/%module%/`
  on Windows. Calling `obs_add_module_path` ourselves with the
  same paths was duplicating every module load.
- **Phase 4b — Qt infrastructure for libobs Qt-linked plugins.**
  `pulsar-headless` now constructs a `QApplication` early in `main`
  with `QT_QPA_PLATFORM=minimal`, so libobs plugins that link
  against Qt6 (notably the upstream obs-websocket) can be loaded
  without crashing on `QObject` machinery. The build script stages
  the Qt6 runtime (`Qt6Core`, `Qt6Gui`, `Qt6Widgets`,
  `Qt6Network`, `Qt6Svg`, `Qt6Xml`) plus the `qminimal` /
  `qwindows` platform plugins from the `obs-deps-qt6-*-x64`
  tarball into the rundir. RAM cost: ~5 MB (66 MB → 71 MB idle).
- **Upstream obs-websocket disabled.** Pass
  `-DENABLE_WEBSOCKET=OFF` to upstream's CMake. The upstream plugin
  hardcodes Qt UI dependencies (`forms/SettingsDialog`,
  `forms/ConnectInfo`) and calls `obs_frontend_get_current_profile_path`
  in its `obs_module_load` migration path, which crashes under our
  headless service because no frontend has registered. Phase 4c
  introduces a `plugins/pulsar-websocket/` vendor fork with Qt UI
  and frontend-api migration calls stripped.
- **Phase 4c — pulsar-websocket vendor fork (source).** The
  obs-websocket plugin source tree (v5.7.3) is vendored under
  `plugins/pulsar-websocket/` with three intentional differences
  from upstream:
  1. `src/forms/` removed entirely (no Qt SettingsDialog /
     ConnectInfo / images / resources.qrc).
  2. `src/obs-websocket.cpp`'s `obs_module_load` no longer
     constructs `SettingsDialog` and no longer registers a Tools
     menu entry. The `forms/SettingsDialog.h` include and the
     `_settingsDialog` global are gone.
  3. `src/Config.cpp`'s `MigrateGlobalConfigData()` and
     `MigratePersistentData()` are reduced to no-ops. Pulsar starts
     from a clean state -- there are no legacy obs-studio
     obs-websocket configs to migrate, so the calls into
     `obs_frontend_get_app_config()` and
     `obs_frontend_get_current_profile_path()` (which return null
     under headless and crash the migrate path) are not made.

  The CMakeLists.txt is a stub that aborts with `FATAL_ERROR` if
  `PULSAR_BUILD_WEBSOCKET=ON` is set. Phase 4d adds the actual
  build wiring (sources, deps from `upstream/.deps/`,
  Qt6::Core + Qt6::Network link, `obs.lib` link, output target).
- **Phase 4d -- pulsar-websocket builds + listens.** The plugin
  CMakeLists is now a real build target. Output:
  `obs-websocket.dll` (2.8 MB) dropped into
  `rundir/obs-plugins/64bit/`, en-US locale staged into
  `rundir/data/obs-plugins/obs-websocket/locale/`. Linkage:
  `obs.lib` + `obs-frontend-api.lib` from
  `upstream/build_x64/`, `Qt6::Core` + `Qt6::Gui` +
  `Qt6::Widgets` + `Qt6::Network` from the obs-deps Qt6 tarball,
  `nlohmann_json::nlohmann_json` from obs-deps. Header-only Asio
  + websocketpp resolved via include path on the
  `obs-deps-*-x64/include/` directory.

  Two extra patches were needed beyond the Phase 4c source changes
  to make it actually load + listen under headless:

  - **`src/Config.h` -- `ServerEnabled` defaults to `true` (was
    `false` upstream).** Upstream relied on `SettingsDialog`
    consenting to start the server. With the dialog removed, the
    server is the entire reason the plugin exists, so it must be
    enabled out of the box.
  - **`src/obs-websocket.cpp` -- include `forms/SettingsDialog.h`
    and the `_settingsDialog` global removed, alongside the
    `obs_frontend_*` Tools-menu wiring inside `obs_module_load`.**
    These were applied in Phase 4c but documenting them here too
    since they are part of the runtime story.

  Also: `Qt6::Widgets` and `Qt6::Gui` had to come back into the
  link list (Phase 4b's plan was Core+Network only) -- the source
  pulls in `QSystemTrayIcon`, `QImageWriter`, `QGuiApplication`,
  `QMainWindow`, etc. across `utils/Platform.h`,
  `requesthandler/RequestHandler_Config.cpp`,
  `requesthandler/RequestHandler_General.cpp`,
  `requesthandler/RequestHandler_Sources.cpp`,
  `requesthandler/RequestHandler_Ui.cpp`. The widgets are never
  instantiated at runtime in headless mode (no menu, no UI
  request paths exercised) but the headers must compile and the
  symbols must link.

  Validated end-to-end: `pulsar.exe` starts, `obs-websocket.dll`
  loads, `[Config::Load] Existing configuration not found, using
  defaults.`, `(FirstLoad) Generating new server password.`,
  `obs_module_post_load: WebSocket server is enabled, starting...`,
  and `netstat` shows `0.0.0.0:4455 LISTENING` + `[::]:4455
  LISTENING`.

  Phase 4e: validate v5 round-trip from an external client.
- Top-level `CMakeLists.txt` rewritten as a real build entry: the
  Pulsar root project now adds `plugins/pulsar-headless/` (via
  `PULSAR_BUILD_HEADLESS=ON` default) and reserves slots for
  `pulsar-websocket` (Phase 4) and `pulsar-multi-stream` (Phase 4)
  plugins. Top-level configure happens AFTER upstream finishes its
  own build — this CMakeLists does not build upstream/.
- `scripts/build-win.ps1` extended with a Pulsar-side build stage:
  after upstream's RelWithDebInfo build completes, configures and
  builds the top-level Pulsar CMake project. Output `pulsar.exe`
  lands next to `obs.dll` in
  `upstream/build_x64/rundir/RelWithDebInfo/bin/64bit/` so the
  Windows loader resolves libobs without touching `PATH`.
