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

- **Phase 2:** introduce the patch pipeline. First patch: build-system
  metadata (rename product strings, version source). Confirms `git am`
  on `upstream/` works cleanly.
- **Phase 3:** disable Qt frontend via CMake options + first headless
  run. At this point `obs64.exe` becomes a no-op service waiting on
  the websocket plugin to drive it.
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

1. `cd upstream && git checkout -b pulsar-work`
2. Make your change, commit with a descriptive message.
3. `git format-patch -1 -o ../patches --start-number NNNN`
4. Add rationale and "upstream candidate" marker in the patch header.
5. PR against Pulsar `main` referencing the upstream concern.

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
