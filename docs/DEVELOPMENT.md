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
  upstream built. Currently boots libobs (CPU + sysinfo
  detection, default canvas creation), prints the patched version
  string, then shuts down. No Qt. Phase 4+ extends this with
  video/audio reset, `obs_load_all_modules`, signal-driven idle
  loop, and the websocket server hookup.
- **Phase 4:** wire the `pulsar-websocket` plugin (fork of
  obs-websocket v5). Service speaks v5 baseline.
- See ARCHITECTURE.md for the full phase plan.

## CI

GitHub Actions matrix (Phase 1+):

- `ci.yml` — lightweight checks on every PR (cmake configure, lint
  patches, validate plugin metadata).
- `build.yml` — full build matrix Win/Mac/Linux on PR + main, uploads
  artefacts for QA.
- `release.yml` — on `vX.Y.Z` tag push, fans out the matrix and
  publishes signed artefacts as a GitHub Release.

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
