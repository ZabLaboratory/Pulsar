# Pulsar — Development

V1 ships Windows x64 only. macOS and Linux are deferred. The build
scripts live in `scripts/`; everything they wrap is reproducible by
hand for debugging.

## Toolchain

| | |
|---|---|
| OS | Windows 10/11 x64 |
| Compiler | Visual Studio 2022 Build Tools — workload "Desktop development with C++" + Windows 11 SDK. The optional "C++ ATL" component (`Microsoft.VisualStudio.Component.VC.ATL`) is **not** required for the headless path but enables `obs-qsv11` / `win-dshow` locally (see [ATL runbook](runbooks/atl-missing-build-failure.md)). |
| CMake | 3.28+ |
| Generator | Visual Studio 17 2022 (default) or Ninja |
| Yarn | 4.x via Corepack (used by upstream's build scripts) |
| Git | with submodule + LFS support |
| Python | 3.11+ for the probe scripts |
| ffmpeg | for the live broadcast probe (the runner uses `FedericoCarboni/setup-ffmpeg`) |
| PowerShell | the build / probe orchestrators are `.ps1` |

`scripts/build-win.ps1` prefers `D:\DevTools\CMake\bin\cmake.exe` and
falls back to `PATH`. Override with `-CMakeExe <path>` if needed.

## Local build

```powershell
git clone --recurse-submodules https://github.com/ZabLaboratory/Pulsar
cd Pulsar
.\scripts\build-win.ps1                    # configure + build (light)
.\scripts\build-win.ps1 -Full              # full build (CEF + obs-browser)
.\scripts\build-win.ps1 -Clean             # wipe build dir, keep dependency caches
.\scripts\build-win.ps1 -GuiBuild          # restore upstream's full obs64.exe (debugging only)
.\scripts\package-win.ps1 -Zip             # produce the light distributable zip
.\scripts\package-win.ps1 -Zip -Full       # produce the full distributable zip
```

What `build-win.ps1` does, in order:

1. Reset `upstream/` to `git submodule status --cached` (the recorded SHA).
2. Replay every `patches/*.patch` lexically via `git am`.
3. Configure: `cmake --preset windows-x64 -S upstream` + Pulsar overrides
   (`ENABLE_FRONTEND=OFF`, `ENABLE_UI=OFF`, `ENABLE_BROWSER=OFF` in
   light mode; `-Full` flips them on).
4. Build: `cmake --build --preset windows-x64 --config RelWithDebInfo`.
5. Compile Pulsar plugins under `plugins/` against the just-built libobs.
6. Output lands in `upstream/build_x64/rundir/RelWithDebInfo/`.

First run is ~25–30 min on a typical machine — the obs-deps + Qt6 +
CEF tarballs (a few hundred MB) download once into the local cache.
Incremental rebuilds are seconds.

## Running it

```powershell
cd upstream\build_x64\rundir\RelWithDebInfo\bin\64bit
$env:PULSAR_PORT     = "4455"
$env:PULSAR_PASSWORD = "dev-only-do-not-ship-this"
.\pulsar.exe
# look for: PULSAR_READY ws=ws://127.0.0.1:4455 password=dev-only-...
```

Then drive it from any v5 client. With the typed client in this repo:

```powershell
cd packages\pulsar-client
npm install
node -e "
import('./dist/index.js').then(async ({ PulsarClient }) => {
  const p = new PulsarClient();
  await p.connect({ url: 'ws://127.0.0.1:4455', password: 'dev-only-do-not-ship-this' });
  console.log(await p.video.get());
  await p.disconnect();
});
"
```

## Probes

Seven Python probe scripts live under `scripts/`. Each is self-contained
and is the source of truth for what it asserts.

| Probe | What it covers |
|---|---|
| `probe-websocket.py` | v5 handshake (Hello → Identify → Identified), `GetVersion` round-trip. |
| `probe-source-kinds.py` | `GetInputKindList` against the expected V1 source matrix. |
| `probe-events.py` | scene/input/source CRUD + the matching v5 events. |
| `probe-scene-list-truth.py` | `CreateScene` → `GetSceneList` → `RemoveScene`: the scene list must be libobs's live truth, never a stub-side snapshot (#119, ADR Prism 026 §3.1). |
| `probe-record.py` | `StartRecord` / `StopRecord` lifecycle, ffprobes the resulting MP4 (codec=h264, audio=aac, fps, bitrate). |
| `probe-adaptive.py` | adaptive bitrate worker — drives a destination, induces drops by stress, checks `pulsar:BitrateAdjusted` event ordering. |
| `probe-multi-stream.py` | multi-destination CRUD + start/stop. **Excluded** from `run-probes.ps1` because of upstream-obs races; covered by the live broadcast probe instead. |

`scripts/run-probes.ps1` orchestrates the offline suite end-to-end:
spawns `pulsar.exe`, waits for `PULSAR_READY`, runs each probe
sequentially, collects exit codes, prints the tail of stdout/stderr on
failure, and shuts the process down. Used by:

- `ctest` (top-level `CMakeLists.txt` adds `add_test(NAME probes ...)`).
- The `offline-probes` job in `.github/workflows/pipeline.yml`.
- Local development — just run it directly.

```powershell
.\scripts\run-probes.ps1
```

The live broadcast probe (`scripts/probe-twitch-live.py`) is **not**
part of the offline suite — it needs a real Twitch stream key + ~1 min
of network bandwidth. It runs from the `live-broadcast` job in
`pipeline.yml` against the project's Twitch credentials, produces a
local MP4 + a diagnostic JSON, and uploads them as artefacts.
Since #132 that job is **off the per-commit path** — tag push and
`workflow_dispatch` only — so a real-ingest regression surfaces at
release time or on an antenna run, not on the PR that introduced it.
Run it on demand from the Actions tab before merging anything that
touches the encoder / service / rtmp lifecycle.

## CI — `.github/workflows/pipeline.yml`

A single workflow with 9 jobs. One build, multiple gates that share
its artefact via `upload-artifact` / `download-artifact`.

| Job | Runs on | Triggers | What it does |
|---|---|---|---|
| `lint` | ubuntu-latest | every PR + push to main | source-grep (no `__declspec(dllexport)` / `napi_*` / `node-gyp` / `prism` / `electron`), patches apply cleanly, plugins carry metadata, npm tarball content audit |
| `build` | windows-2022 | every PR + push to main | `scripts/build-win.ps1 -Full`, uploads `pulsar-rundir` artefact consumed by all subsequent gates |
| `binary-gate` | windows-2022 | every PR + push to main | `scripts/check-binary-exports.ps1` over `pulsar.exe`, `pulsar-browser-page.exe`, and every plugin DLL |
| `offline-probes` | windows-2022 | every PR + push to main | `ctest` with retry, runs the offline probe suite |
| `live-broadcast` | windows-2022 | tag `v*.*.*` + `workflow_dispatch` **only** (#132) | end-to-end Twitch broadcast — 600 s on tag, operator-chosen on dispatch — produces MP4 + diagnostic JSON |
| `publish-gh-pages` | ubuntu-latest | push to main + tag | `peaceiris/actions-gh-pages` — publishes the broadcast MP4 to `gh-pages` so the README inline player streams the latest run |
| `package` | windows-2022 | tag `v*.*.*` push | `scripts/package-win.ps1 -Zip` for both light + full variants |
| `release-attach` | ubuntu-latest | tag push | `softprops/action-gh-release` with the zips + MP4 + diagnostic JSON |
| `npm-publish` | ubuntu-latest | tag push | `npm publish` for the three packages (parallel to `build` — does not need the Windows artefact) |

Triggers (deduped to avoid duplicate runs):

- `push: branches: [main]` + `tags: ['v*.*.*']`
- `pull_request: branches: [main]`
- `workflow_dispatch` with toggles for `enable_package`,
  `enable_release_attach`, `enable_npm_publish`, and a custom
  `live_test_duration_seconds`.

Skip a run from a docs-only commit by appending `[skip ci]` to the
commit message — GitHub Actions honours that natively.

## Adding a patch

The build pipeline replays `patches/*.patch` onto the recorded
submodule SHA on every run, so authoring a new patch is producing a
clean `format-patch` artefact.

1. Make sure `upstream/` is at the recorded SHA. If unsure, run
   `scripts/build-win.ps1 -Stage configure` once — it resets and
   re-applies whatever patches are present.
2. `cd upstream` and edit. Branch identity does not matter; `git am`
   creates commits on detached HEAD when the build script next runs.
3. Stage and commit with a meaningful message — include
   `Pulsar-Patch: NNNN` and `Upstream-Candidate: yes/no` trailers so
   the patch metadata stays self-describing.
4. `git format-patch -1 --start-number NNNN -o ../patches HEAD`. Pick
   the next free number (gaps are fine).
5. `git reset --hard <recorded-sha>` to restore upstream/. The patch
   lives in `patches/` now; the build script will re-apply it on the
   next configure.
6. Run `scripts/build-win.ps1 -Stage configure` to verify the patch
   applies cleanly. Rebuild and validate the change is observable at
   runtime.
7. Open a PR. If the patch is upstream-eligible, also open the
   corresponding PR on `obsproject/obs-studio` and link it from the
   patch header.

## Adding a plugin

1. Create `plugins/pulsar-<name>/` with `CMakeLists.txt` + `README.md`
   + your sources.
2. Register it in the top-level `CMakeLists.txt` under the
   `PULSAR_BUILD_PLUGINS` block.
3. If the plugin adds a vendor request handler or event, document the
   surface in [`PROTOCOL.md`](PROTOCOL.md) and add a typed wrapper to
   `packages/pulsar-client/src/`.
4. Add a probe script under `scripts/probe-<name>.py` if the surface
   is non-trivial. Wire it into `scripts/run-probes.ps1`.

## Versioning

Semver. Pinned in consumers via exact version match.

- **Patch** (`x.y.Z`): bug fix, no protocol change, no env-var rename.
- **Minor** (`x.Y.0`): new feature — additive `pulsar:*` request,
  new destination kind, new env var. Consumers may need to consume
  the new version to expose the feature.
- **Major** (`X.0.0`): breaking change to `pulsar:*` extensions, env
  var rename, or embedding contract change. Coordinated bump with
  consumers.

The single source of truth for the version string is the top-level
`VERSION` file. C++ reads it via the build, npm reads it via
`package.json`, and the postinstall scripts read it to fetch the
matching binary. Bump it, commit, tag.

## Troubleshooting

### `error C1083: Cannot open include file: 'atlbase.h'` (or `atlcomcli.h` / `atlstr.h`)

ATL headers missing — the "C++ ATL" VS component is not installed on this machine.
`scripts/build-win.ps1` detects this automatically and skips the three affected
plugins (`obs-qsv11`, `win-dshow`, `virtualcam-module`) with `PULSAR_HAVE_ATL=OFF`.
If you are invoking CMake directly (bypassing the script) the build will fail.
Full diagnosis, gate mechanics, rollback, and optional ATL install instructions:
[docs/runbooks/atl-missing-build-failure.md](runbooks/atl-missing-build-failure.md).

### `pulsar.exe did not signal ready within 30000ms`

The most common cause is `cwd` being wrong — libobs cannot find
`data/libobs/default.effect`. Always spawn with
`cwd = <pulsar root>/bin/64bit`.

Other causes: a port conflict (another Pulsar / OBS Studio instance
on the same port), an antivirus quarantining a fresh `pulsar.exe`,
or the obs-websocket plugin failing to load (check `obs-plugins/64bit/`
exists and contains `obs-websocket.dll`).

### `Failed to find file 'default.effect'`

Same root cause: wrong `cwd`. The error appears in pulsar's stdout
before the READY sentinel.

### `obs_output_start declined silently`

The v5 `StartStream` request returns success but no actual stream
is opened. Either configure a service via `SetStreamServiceSettings`
first, or — better — use `pulsar:CreateDestination` +
`pulsar:StartDestination` from the multi-stream API.

### `WARN: download failed: 404` during `npm install`

The matching `pulsar-windows-x64-v<version>.zip` GitHub Release does
not exist yet (or the version you bumped to has not been tagged).
The postinstall soft-fails so `npm install` completes; the bundle is
unusable until you publish the matching release. For monorepo dev,
override `binariesPath` in `spawn()` to point at a local
`upstream/build_x64/rundir/RelWithDebInfo/` instead.

### `npm install ... EBADPLATFORM` on Linux/macOS

`pulsar-bundle` declares `os: ["win32"]` and `cpu: ["x64"]`. On other
platforms `npm install` skips it cleanly. If you need to install on a
non-target platform anyway (CI matrix, test scaffolding), pass
`--force` to npm install — the package's own `postinstall` then
detects the platform mismatch and exits 0 without downloading.

### Probe times out / `pulsar.exe FAILED (exit 1)` in CI

The `live-broadcast` job uploads the full pulsar stdout/stderr +
diagnostic JSON as workflow artefacts. Download them from the failed
run page. The JSON includes per-poll perf samples (active fps, render
time, drops) so you can attribute lag to encoder vs network.
