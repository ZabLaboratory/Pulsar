# Pulsar — Development

## Phase plan

| Phase | Scope | Status |
|---|---|---|
| 0 | Repo scaffold (this doc, layout, READMEs, CMake skeleton, CI placeholders) | in progress |
| 1 | Build pipeline Win — clone upstream, apply patches, configure CMake, produce a runnable headless binary that does nothing useful yet | next |
| 2 | `pulsar-headless` + `pulsar-websocket` plugins functional — service starts, accepts WebSocket connections, responds to obs-websocket v5 baseline | |
| 3 | Window-capture source from Prism's hidden BrowserWindow + local MP4 record output | |
| 4 | `pulsar-multi-stream` plugin — destinations API, Twitch + RTMP custom + VOD local working | |
| 5 | YouTube destination — OAuth Live Streaming API + Helix-equivalent for Twitch | |
| 6 | Audio: mic + app-audio + Meet peer audio via WASAPI process loopback | |
| 7 | Mac build + CI matrix expansion | |
| 8 | Linux build (additional patches likely required, upstream Linux story is uneven) | |
| 9 | 60fps end-to-end + bitrate adaptive | |

## Toolchain

### Windows (target Phase 1 platform)

- Visual Studio 2022 Build Tools — workload "Desktop development with C++"
  + Windows 11 SDK
- CMake 3.24+
- Yarn 4.x via Corepack (used by upstream's build scripts)
- Git with submodule support
- Ninja or MSBuild generator

### macOS (Phase 7)

- Xcode 15+ (Command Line Tools at minimum)
- CMake, Ninja
- Apple Silicon and Intel both supported by upstream

### Linux (Phase 8)

- GCC 11+ or Clang 14+
- Per-distro dependencies — upstream documents for Ubuntu / Fedora /
  Arch in `upstream/CI/linux/`

## Local build (Phase 1, Windows)

Phase 1 deliberately reuses obs-studio's own CMake preset
(`windows-x64`) against the vendored `upstream/` submodule. No patches
applied, no Pulsar plugins built, no headless mode yet. The goal is to
prove the toolchain integration end-to-end and produce a runnable
`obs64.exe` (full GUI obs-studio at this stage).

```
git clone --recurse-submodules git@github.com:ZabLaboratory/Pulsar.git
cd Pulsar
scripts\build-win.ps1
```

What `build-win.ps1` does:

1. Locates CMake (prefers `D:\DevTools\CMake\bin\cmake.exe`, falls back
   to `PATH`).
2. Invokes `cmake --preset windows-x64 -S upstream` — the preset fetches
   the prebuilt obs-deps + Qt6 + CEF tarballs from
   `obsproject/obs-deps` based on hashes pinned in
   `upstream/CMakePresets.json`.
3. Generates a Visual Studio 17 2022 solution under
   `upstream/build_x64/`.
4. Invokes `cmake --build --preset windows-x64 --config RelWithDebInfo`.
5. Resulting binary lands under
   `upstream/build_x64/rundir/RelWithDebInfo/bin/64bit/obs64.exe`.

Phase 1 success criteria: that binary launches and runs the obs-studio
UI. We are not (yet) touching its behaviour — only proving we can
build it ourselves.

## Phases 2+ (planned)

- **Phase 2:** DONE. Patch pipeline lives in `scripts/build-win.ps1`:
  resets `upstream/` to the recorded submodule SHA (via `git
  submodule status --cached`), applies every `patches/*.patch` in
  lexical order via `git am`, then runs configure. First patch
  (`0001-build-tag-OBS_VERSION-with-pulsar-suffix.patch`) appends
  `-pulsar` to the runtime version string so the fork is observable
  in `obs64.exe` window titles and logs.
- **Phase 3a:** DONE. `build-win.ps1` defaults to headless mode --
  passes `ENABLE_FRONTEND=OFF`, `ENABLE_UI=OFF`, `ENABLE_BROWSER=OFF`
  on the cmake invocation so the Qt frontend and CEF browser plugin
  are excluded at build time. `obs64.exe` is no longer produced.
  libobs core + 25 modules (encoders, capture, websocket, virtualcam,
  audio etc.) still build. `-GuiBuild` switch restores the original
  obs-studio behaviour. `-Clean` switch wipes the build directory
  while preserving dependency caches.
- **Phase 3b:** DONE. `pulsar.exe` produced from
  `plugins/pulsar-headless/main.cpp`, linked against the libobs
  upstream built. First headless run.
- **Phase 4a:** DONE. `pulsar.exe` is now a real service:
  default 1080p30 video / 48 kHz stereo audio, `obs_load_all_modules`
  loads 20 plugins from libobs's built-in default search paths,
  console-control handler flips an atomic flag for graceful
  shutdown, 100 ms idle loop polls it. ~66 MB RAM idle.
- **Phase 4b:** DONE. Qt infrastructure: `pulsar-headless`
  constructs a `QApplication` (forced `QT_QPA_PLATFORM=minimal`
  so no display server / platform plugin DLL is required for
  rendering -- only the Qt event loop + QString/QJson machinery).
  Build pipeline stages Qt6 runtime + minimal/windows platform
  plugins. Upstream obs-websocket disabled
  (`-DENABLE_WEBSOCKET=OFF`) because it hardcodes Qt UI +
  frontend-api migration calls that crash without a real
  frontend.
- **Phase 4c:** DONE for the source side. `plugins/pulsar-websocket/`
  vendored from upstream obs-websocket v5.7.3 with `src/forms/`
  dropped, `obs-websocket.cpp` stripped of the Qt menu /
  SettingsDialog wiring, and `Config.cpp` migrate functions
  reduced to no-ops. The CMakeLists is still a stub.
- **Phase 4d:** DONE. `obs-websocket.dll` produced from our fork,
  loads inside `pulsar.exe`, listens on `0.0.0.0:4455` and
  `[::]:4455`. Two extra patches beyond Phase 4c: ServerEnabled
  defaults to `true` (Pulsar opts users in by default since there
  is no SettingsDialog to consent), and `Qt6::Widgets` / `Qt6::Gui`
  came back into the link list (source pulls QSystemTrayIcon,
  QImageWriter, QGuiApplication, QMainWindow even outside the
  forms/ directory). Plugin sources, the obs-deps lookup, and the
  Qt6 lookup all flow through
  `plugins/pulsar-websocket/CMakeLists.txt`.
- **Phase 4e:** DONE. `scripts/probe-websocket.py` does the v5
  handshake (Hello -> Identify -> Identified), issues a
  `GetVersion`, prints the response. Run with:

  ```
  pip install websockets
  python scripts/probe-websocket.py
  ```

  Pulsar speaks obs-websocket v5 end-to-end -- 137 v5 request
  types are advertised in the GetVersion response.
- **Phase 4:** wire the `pulsar-websocket` plugin (fork of
  obs-websocket v5). Service speaks v5 baseline.
- See ARCHITECTURE.md for the full phase plan.

## CI

GitHub Actions matrix (Phase 1+):

- `ci.yml` — lightweight checks on every PR (patches/ apply cleanly on
  the pinned upstream SHA; every plugins/* carries CMakeLists.txt + README).
- `build.yml` — Windows x64 build on every PR + push to main. Runs the
  full `-Full` build (CEF + obs-browser) and the binary-export gate
  (`scripts/check-binary-exports.ps1`). **macOS / Linux are deferred
  to Phase 7 / Phase 8 — V1 ships Windows-only.**
- `license-isolation.yml` — source-tree audit for forbidden patterns
  (`__declspec(dllexport)`, `napi_*`, `prism`, `electron`).
- `live-test.yml` — end-to-end Twitch broadcast probe on tag push +
  manual dispatch.
- `release.yml` — on `vX.Y.Z` tag push, builds Windows x64 once and
  packages two variants (light + full) into a GitHub Release.

## Adding a patch

The build pipeline replays `patches/*.patch` onto the recorded
submodule SHA on every run, so authoring a new patch is a matter of
producing a clean `format-patch` artefact.

1. Make sure `upstream/` is at the recorded SHA (run
   `scripts/build-win.ps1 -Stage configure` once if unsure — it
   resets and re-applies whatever patches are present).
2. `cd upstream` and edit the files you want to change. The branch
   you are on does not matter; `git am` will create commits on
   detached HEAD when the build script next runs.
3. Stage and commit with a meaningful message — include
   `Pulsar-Patch: NNNN` and `Upstream-Candidate: yes/no` trailers
   so the patch metadata stays self-describing.
4. `git format-patch -1 --start-number NNNN -o ../patches HEAD`.
   Pick the next free number (gaps are fine — see
   `../patches/README.md`).
5. `git reset --hard <recorded-sha>` to clean upstream/ back to the
   pinned commit. The patch lives in `patches/` now; the build
   script will re-apply it next configure.
6. Run `scripts/build-win.ps1 -Stage configure` to verify the patch
   applies cleanly via the build pipeline. Rebuild and validate
   the change is observable at runtime.
7. Open a PR against Pulsar `main` with the patch file. If the
   patch is upstream-eligible, also open the corresponding PR on
   `obsproject/obs-studio` and link it from the patch header.

## Adding a plugin

1. Create `plugins/pulsar-<name>/` with `CMakeLists.txt` + `README.md`.
2. Register it in the top-level `CMakeLists.txt` under the
   `PULSAR_BUILD_PLUGINS` block.
3. Document the plugin's protocol surface (if any) in `docs/PROTOCOL.md`.
4. Add a CI build step verifying the plugin compiles in isolation.

## Versioning

Semver. Pinned in Prism via exact version match.

- Patch (`0.0.x`): bug fixes, no protocol change.
- Minor (`0.x.0`): new features, additive `pulsar:*` requests, new
  destinations. Prism may need to consume the new version to expose
  the feature.
- Major (`x.0.0`): breaking changes to `pulsar:*` extensions or to
  the embedding contract. Coordinated bump with Prism.

## Pre-alpha caveat

Until Phase 1 produces a building binary, this document describes
**intent**, not behaviour. Steps marked "Phase N deliverable" are not
yet implemented.
