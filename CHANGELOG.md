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
