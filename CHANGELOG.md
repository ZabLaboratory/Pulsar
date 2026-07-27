# Changelog

All notable changes to Pulsar are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### ✨ Added

- `pulsar-frontend-stub`: the **replay buffer is wired** (#117, ADR Prism 024
  §3.1). `replayOutput` was created at boot but nothing was attached to it —
  no encoders, no settings — so `obs_output_start` declined, and
  `GetLastReplayBufferReplay` returned an empty string forever. It now:
  - **borrows** the very same video + audio encoders already bound to the
    record and stream outputs (encode-once / fan-out, the pattern
    `pulsar-multi-stream::ensure_output` already runs). Arming the buffer
    adds **no** encoder to the process — the replay MP4 comes out at the
    same 6 000 kbps h264 as the recording written beside it;
  - carries real settings: `directory` = `recordDir`, filename template
    `pulsar-replay-%CCYY%MM%DD-%hh%mm%ss.mp4`, `max_time_sec`
    (`PULSAR_REPLAY_MAX_TIME_SEC`, 10..300, default 30) and `max_size_mb`
    (`PULSAR_REPLAY_MAX_SIZE_MB`, 16..8192, default 512);
  - fills `lastReplay` from the output's `get_last_replay` proc handler on
    the `saved` signal, so `GetLastReplayBufferReplay` returns a real path;
  - **refuses an off-air arm, loudly**. The buffer lives off the shared
    encoders; arming it while they are idle would spin one up for a
    partial, invisible pipeline. No replay off-air — an explicit no-go, not
    an oversight.

  The six v5 baseline requests were already compiled — no new `pulsar:*`
  request. `scripts/probe-replay.py` (offline suite, Phase 1d-bis) proves
  the whole round-trip: refused off-air, active on-air, a readable h264+aac
  MP4 on disk at the path the server reports.

### 🐛 Fixed

- `pulsar-websocket`: the four output families no longer report an effect
  they never observed (#120, ADR Prism 026 §3.2). `StartReplayBuffer`,
  `StartRecord`, `StartVirtualCam`, `StartStream` and their `Stop*` /
  `Toggle*` counterparts called a `void` `obs-frontend-api` entry point and
  returned `Success()` unconditionally; libobs declines silently on an
  unconfigured output, so a client was told "started" while the next
  `GetXStatus` reported `outputActive: false`. Each handler now re-reads the
  real state after the action and answers an explicit error —
  `OutputNotRunning` (501) for a start, `OutputRunning` (500) for a stop —
  carrying the cause read off the server: `obs_output_get_last_error()` when
  libobs recorded one, otherwise the structural state that made it refuse
  (no service bound on a service output, no encoder bound on an encoded
  one). **No signature change**: no new request, no new status enum, no new
  response field.
  The refusal is decided from the output's own `"starting"`/`"stopping"`
  signal, which libobs emits only when it actually took the action — so an
  asynchronous completion still in flight (an rtmp connect thread, an
  `ffmpeg_muxer` flush) is reported as success, not as a failure, and no
  request ever waits for activation. The residual state poll is bounded by
  `PULSAR_OUTPUT_VERIFY_MS` (250 ms default) and is not reached on the
  nominal path. `scripts/probe-output-effect.py` drives a real refusal in
  each family plus a positive control and a latency bound.
  Integration of #117 with #120: the off-air replay refusal is Pulsar's own
  policy, taken *before* `obs_output_start`, so libobs recorded no cause and
  the verification could only answer the generic "the output is not
  configured" — false, since the encoders *are* attached, just idle. The stub
  now publishes the cause through `obs_output_set_last_error()` at the point
  of refusal, so #120 names it like any libobs cause. Nothing has to clear
  it: `obs_output_actual_start()` wipes `last_error_message` on the next real
  start.

## [1.2.2] - 2026-07-26

### 🔒 Security

- `pulsar-multi-stream`: the `twitch` destination kind no longer puts the
  Twitch stream key on the wire in cleartext (#113, PR #114). The pinned
  ingest URL moves from `rtmp://live.twitch.tv/app/` to
  `rtmps://ingest.global-contribute.live-video.net/app/` — the
  `url_template_secure` of the `Default` entry of
  <https://ingest.twitch.tv/ingests>. The stream key is a bearer
  credential and travels inside the RTMP connect handshake, so every
  go-live previously exposed it to anyone on the path; the legacy
  `live.twitch.tv` host is not in the published ingest list and refuses
  TLS on :1935, so switching the scheme alone was not an option.
  mbedTLS/librtmp already carry the TLS path (`obs-outputs` built with
  `USE_MBEDTLS`) and librtmp derives the transport from the scheme — no
  extra service setting. There is no cleartext downgrade: a failed
  handshake or a failed certificate verification aborts in
  `RTMP_Connect1`. A `static_assert` pins the scheme at compile time and
  `scripts/probe-twitch-rtmps.py` guards it (real ingest passes the TLS
  stage; two negative controls prove a TLS failure is loud and fatal).
  Refs ADR 021 (Prism) palier 1.

  **Consumers must upgrade** — the fix lives in the compiled plugin, so
  a Prism/embedder still on `1.2.1` keeps streaming in cleartext even
  with this source merged. Bump to `@clodocapeo/pulsar-bundle-full`
  `1.2.2` (postinstall pulls `pulsar-windows-x64-full-v1.2.2.zip`).

## [1.2.1] - 2026-07-05

### 🐛 Fixed

- `pulsar-scene-source`: repeated `pulsar-scene:SetCaptureSource` calls no
  longer strand stale `browser_source` items on the program scene (#110). The
  new source was created (canonical name `PulsarSceneSource`) before the old
  one was removed, so libobs de-duped the fresh instance to
  `PulsarSceneSource 2`; the exact-`strcmp` cleanup then missed every numbered
  variant, leaving them accreting on the scene indefinitely and letting a
  name-based consumer (Prism's `findBrowserSourceName`) lock onto a stale
  instance from the 3rd re-point on. The cleanup now runs before the fresh
  source is added; the outgoing managed source is renamed out of the
  canonical name **synchronously** (`obs_source_set_name` updates libobs's
  global name table under lock, whereas scene-item removal only *schedules*
  the source's deferred destruction — relying on that release was an
  intermittent race), so the fresh source can then reliably reclaim the
  canonical name; and the managed-item matcher recognises libobs de-dup
  variants (`base <n>`) so any pre-existing drift is swept too.
  Regression-guarded by `scripts/probe-scene-name-drift.py` (24 rapid
  re-points) in the offline probe suite.

## [1.2.0] - 2026-07-03

### ✨ Added

- `pulsar-frontend-stub`: boot-time GPU video-encoder selection (ADR 004
  §3.1-3.2). New env vars `PULSAR_VIDEO_ENCODER` (`x264`/`nvenc`/`qsv`/`amf`/
  `auto`), `PULSAR_VIDEO_PRESET`, `PULSAR_VIDEO_PROFILE`,
  `PULSAR_VIDEO_RATE_CONTROL`, `PULSAR_VIDEO_KEYINT_SEC`, all resolved against
  the live `obs_enum_encoder_types()` set (H.264 only) with a mandatory typed
  fallback to `obs_x264` — an absent family, a null `create()`, or an invalid
  knob degrade silently (logged) to today's byte-identical x264 path; the spawn
  never fails on encoder choice. Encoder identity is boot-fixed (no live swap),
  same tier as `PULSAR_FPS`/`PULSAR_RESOLUTION`.
- `pulsar-multi-stream`: new `pulsar:GetCapabilities` vendor request (ADR 004
  §3.3) — enumerates the encoder families this build exposes (mapped from
  `obs_enum_encoder_types()` to the whitelisted short names `x264`/`nvenc`/
  `qsv`/`amf`, never a raw obs id) plus `active_encoder`, the `video_bitrate`
  `{min,max}` window and the `audio_bitrate` ladder. `GetVideoSettings` gains
  `video_encoder`/`video_preset`/`video_profile` for a complete off-air
  snapshot; `SetVideoSettings` now rejects those three fields with the same
  typed boot-fixed error as `fps` (no live encoder swap — ADR 004 §3.4).
- `@clodocapeo/pulsar-client`: `pulsar.capabilities` namespace
  (`client.capabilities.get()` → typed `PulsarCapabilities`) wrapping
  `pulsar:GetCapabilities`; `VideoSettings` gains `videoEncoder`/`videoPreset`/
  `videoProfile`.
- `@clodocapeo/pulsar-client`: `pulsar.audio` namespace — stream-level mic
  control (mute/unmute/toggle, device enumeration + selection via
  `SetInputSettings.device_id`) wrapping the native obs-websocket v5 `Input*`
  requests, no vendor plugin involved. Mute state lives on the mic input
  itself, not on any scene, so cockpit mic controls survive scene switches.
  Adds the typed `inputMuteStateChanged` event.

## [1.1.0] - 2026-06-10

Operability + M10 transition groundwork release. Builds on the V1 headless
broadcast engine with: a governance/merge-gate CI surface, a frozen
cross-service `scene_control` contract (Blue → Orion leaf → Solar/Prism),
the M10 "blue-driven scene transition" harness and probes, and a dormant
native-stinger compositing capability gated OFF by default. No change to the
public spawn/handshake (PRISM-EMBEDDING) or the obs-websocket request surface
— all additions are backward-compatible (minor bump per `docs/PROTOCOL.md`).

### ✨ Added

- **CI compliance workflow (`compliance.yml`).** Org merge-gate conformance,
  kept separate from the build pipeline: `secret-scan` (trufflehog verified
  history+filesystem scan **+** detect-secrets audit against
  `.secrets.baseline`), `deps-audit` (npm `--omit=dev --audit-level=high`,
  high/critical CVE blocks), `lockfile-check` (`npm ci --dry-run`, no drift,
  stray-`yarn.lock` guard) and `codeowners-check` (structural CODEOWNERS
  validation). No error-suppression toggles — every job can turn a PR red.
- **`scene_control` cross-service contract** (`scripts/contracts/scene_control/`).
  Single source of truth for the leaf that travels Blue → `Orion leaf` →
  Solar/Prism/probe, with a schema validator, valid/malicious fixtures and a
  contract test (`test_scene_control_contract.py`) bound to a mirror of Blue's
  leaf_mapper. Wired into CI as the `contract tests (scene_control)` job.
- **M10 transition harness** (`scripts/m10_setup.py`, `run-m10.ps1`,
  `run-m10-live.ps1`, `m10_orion_standin.py`) creating the two
  `monitor_capture` scenes and the Solar/CEF overlay used for the
  blue-driven scene transition, plus the loopback Orion-WS stand-in.
- **Native stinger compositing**, gated behind the `PULSAR_NATIVE_STINGER`
  env flag (**default OFF / dormant**). Adds a fade + stinger transition pair
  bound as the encoder output source, with `PULSAR_STINGER_ASSET` to pin a
  **local-only** demo asset path. The flag is resolved once at boot from the
  process environment and is **never** reachable from a leaf / obs-websocket /
  network value (Bastion invariant, ADR 003 §A4.5).
- **OBS version tagging** — `OBS_VERSION` is suffixed with a `pulsar` marker
  so a built binary is identifiable as the fork (`patches/0001-…`).
- **ATL-dependent plugin build gate** behind `PULSAR_HAVE_ATL`
  (`patches/0002-…`), with a runbook for the missing-build failure mode.
- **New probe suite** — 30 s Twitch scene-switch probe, M1/M2/M3/M6 milestone
  probes (binary smoke, media-output→MP4, CEF browser-source capture, real
  Solar scene on air), the M10 Canvas-live probe, the GPU-coexistence spike
  (`monitor_capture` + CEF `browser_source` on GPU) and a flag-aware stinger
  smoke probe.
- **Pinned stinger demo asset** generator (`scripts/assets/`,
  `generate-stinger-demo.ps1` + manifest) for the dormant native path.

### ♻️ Changed

- **Pivot to a Solar/CEF overlay transition (M10).** The transition is no
  longer an OBS-native media transition: Solar/CEF animates a full-screen
  opaque overlay over the two captures and the underlying screen change is an
  instantaneous hard-cut hidden under the overlay plateau. The leaf
  co-specifies the overlay animation (Solar) and the `cut_at_ms` (Prism); the
  OBS-native form (media `asset_id` / `path` / action verbs) is superseded and
  kept only behind the dormant flag.
- **Harness capture method forced to WGC** (Windows Graphics Capture) to prove
  `monitor_capture` coexists with the CEF browser source headless; Orion scene
  default migrated to the overlay shape.
- **Transition output binding moved out of `setup()`** — the encoder output
  source is now driven by the transition (passthrough when idle, blend
  mid-switch) rather than a raw scene bind.
- **Docs refresh** — full `docs/` set re-synced (ARCHITECTURE, DEVELOPMENT,
  PROTOCOL, PRISM-EMBEDDING) and `CLAUDE.md` untracked (now local-only).

### 📝 Docs

- **ADR 001** — ATL build gate + CI compliance, accepted.
- **ADR 002** — M8 Canvas-authored live test, accepted.
- **ADR 003** — blue-driven OBS scene transition (M10), accepted, through
  Amendment 5 (Solar/CEF overlay pivot, Orion wipe-cover authoring link,
  M9-premise/transport corrections).
- **Runbook** — `docs/runbooks/atl-missing-build-failure.md`.
- Package READMEs expanded (`pulsar-client`, `pulsar-bundle`,
  `pulsar-bundle-full`, `pulsar-frontend-stub`).

### 🔧 CI / Build

- `pipeline.yml` gains the `contract tests (scene_control)` job (ubuntu,
  parallel to lint) plus offline M10 harness/probe tests.
- New `scripts/build-win.ps1` build entrypoint.
- `.gitignore` now excludes the local `/CLAUDE.md` agent constitution.

### ⚠️ Notes

- `PULSAR_NATIVE_STINGER` is **dormant** in this release: unset (the default)
  keeps OBS doing a raw hard cut, the stinger source is never registered and
  no media is decoded. It exists in `main` as a future capability only.
- The live M10 transition on-air leg (overlay actually compositing, leaf read
  off `/show/stream`) is proven by the CTest integration suite + an operator
  antenna run, not by the offline CI contract/probe tests.

## [1.0.0] - 2026-05-02

V1 — first stable release. Pulsar is now a production-grade headless
broadcast engine that Prism (and any future consumer) can bundle and
spawn confidently. The full set of changes squashed into the V1
readiness commits on `main`, summarised :

### Added

- **CEF browser_source via the pulsar-browser fork.** Forked obs-browser,
  dropped the `obs_browser_initialize` FFI surface, co-located the
  helper exe with `libcef.dll` so Windows resolves CEF imports
  without manual staging. browser_source now renders HTML/CSS/JS
  scenes into the encode pipeline, used by the live-broadcast probe.
- **Session credentials seeded at boot.** `PULSAR_PORT` and
  `PULSAR_PASSWORD` env vars override any persisted obs-websocket
  config before plugins load ; a `PULSAR_READY ws=… password=…`
  sentinel is emitted on stdout for the spawning process to parse.
  No more disk race against `obs-websocket/config.json`.
- **/SUBSYSTEM:WINDOWS pulsar.exe** with `AttachConsole(ATTACH_PARENT_PROCESS)`.
  Spawn from Prism / scripts no longer allocates a visible cmd.exe
  window ; direct invocation from a real terminal still prints to
  the operator's console.
- **`docs/PRISM-EMBEDDING.md`** consumer spawn / handshake / lifecycle
  contract (mandatory `cwd`, `windowsHide:true`, READY sentinel
  parse loop, shutdown protocol).
- **Live-broadcast proof on every release.** The pipeline pushes a
  10 min Twitch broadcast, records locally via `StartRecord`,
  re-encodes with ffmpeg CRF 23 (~5-25× smaller than source CBR),
  publishes the MP4 to GitHub Pages so the README `<video>` plays
  inline, and attaches the same MP4 to the GitHub Release.
- **Lag-attribution diagnostic JSON** (`diagnostic.json`). Per-poll
  perf samples (active_fps, render_ms, output_skipped, effective
  bitrate) + summary stats + ffprobe of the MP4. Uploaded as
  workflow artefact on every run.
- **Apple-keynote test scene.** Hand-coded `test-scene.html` shell
  + `prism-v2-app.jsx` React app. Six telemetry stats bound to
  `pulsar:GetAdaptiveState`, Web Audio sound design (event-only,
  no background music), `Introducing Pulsar` letter-cascade intro
  with the `Pulsar` word warming to SF System Orange.
- **CTest-driven offline probe suite**, wired into the pipeline as
  the `offline probe suite` job. Probes : websocket, source-kinds
  (input kind inventory smoke), events, adaptive, record.
- **Binary-export gate broadened** to every Pulsar plugin DLL, not
  just `pulsar.exe`. `pulsar.exe` + `pulsar-browser-page.exe` must
  export zero symbols ; plugin DLLs may export only the OBS module
  ABI (`obs_module_load`, `obs_module_set_pointer`, …).
- **`pulsar-multi-stream.samples` counter** — the adaptive worker
  exposes a monotonic sample count so external observers can
  confirm liveness without waiting for a bitrate adjustment.

### Changed

- **CI consolidated from 6 workflows into 1 `pipeline.yml`** with
  9 isolated jobs sharing a single `pulsar-rundir` artefact. No
  more parallel rebuilds doing the same work. `concurrency:
  pipeline-<ref>` with `cancel-in-progress: true` cancels in-flight
  runs on the same ref.
- Pipeline trigger matrix : push branch / PR = 60 s smoke ; push to
  `main` = 10 min release-grade broadcast + gh-pages publish ; push
  tag `v*.*.*` = + package + GitHub Release attach + npm publish ;
  workflow_dispatch = configurable.
- **WASAPI mic source is opt-in** via `PULSAR_MIC_DEVICE_ID`. Hosts
  without a default input device (CI runners, servers) no longer
  spam `Device '' invalidated. Retrying` every 2 s.
- `pulsar-multi-stream::release_destination_handles_locked` does a
  graceful stop + 500 ms drain tail before release. Avoids a
  use-after-free between `obs_output_release` and the worker
  thread on the rtmp_output ECONNREFUSED-fast path.

### Fixed

- CEF GPU subprocess `Reason: '63'` crash on launch — root cause
  was the helper exe living in `bin/64bit/` while libobs's plugin
  loader expected it in `obs-plugins/64bit/` next to `libcef.dll`.
- Black-screen broadcast on the 30-min main run when unpkg.com
  flaked. React + ReactDOM + Babel are now vendored under
  `scripts/live-test/vendor/`.
- The `pulsar-live-broadcast-proof.mp4` (stable name) was missing
  from the gh-pages upload due to a too-narrow glob ; the README
  inline player no longer 404s.
- pulsar-scene-source vendor namespace renamed (`pulsar` →
  `pulsar-scene`) to disambiguate from `pulsar-multi-stream`.

### Skipped (tracked as TODO upstream-obs)

- `probe-multi-stream.py` is excluded from the offline suite. The
  destination lifecycle has known race-condition crash paths in
  obs upstream (rtmp_output worker vs ECONNREFUSED, ffmpeg_muxer
  flush vs Stop, service-ref vs worker exit ordering). The
  `pulsar:CallVendorRequest` API contract is exercised by the
  live-broadcast probe against a real Twitch ingest. Tracked as
  TODOs in `run-probes.ps1`, the multi-stream plugin source, and
  the probe.
- The two specific sub-tests inside probe-multi-stream that
  exercise the racey paths (`StartDestination` on a dead RTMP
  address + `RemoveDestination` while active) are commented out
  with TODO(upstream-obs) markers for when the upstream fixes land.

## [0.2.1] - 2026-04-30

### Added

- `LICENSE` file shipped inside each npm package tarball — previously
  the `files[]` arrays referenced a `LICENSE` that did not exist on
  disk, so published tarballs had no licence text. Each package now
  ships its own copy.
- README "License" section expanded on all three packages to clarify
  what users actually receive on disk and how the GPL applies.

### Changed

- `@clodocapeo/pulsar-bundle` and `@clodocapeo/pulsar-bundle-full`
  `package.json` `license` field corrected from `MIT` to
  `GPL-2.0-or-later`. The bundles ship `pulsar.exe` (libobs + Pulsar
  plugins, GPL-2.0-or-later); declaring them MIT was misleading and
  hid the GPL §3 source-distribution obligation that flows to
  redistributors. The aggregate is GPL.
- `@clodocapeo/pulsar-client` stays MIT — it contains no libobs code,
  links nothing GPL, and speaks obs-websocket v5 over a WebSocket.
  The README now states this explicitly so consumers building
  proprietary tools on top of the protocol know the wrapper is safe
  to embed.

## [Unreleased - older entries]

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
- **Phase 4e -- v5 round-trip validated.** A v5 client
  (`scripts/probe-websocket.py`, ~140 lines, depends on the
  `websockets` Python package) now successfully connects to
  `ws://127.0.0.1:4455`, completes the obs-websocket v5 handshake
  (Hello -> auth challenge -> Identify -> Identified), issues a
  `GetVersion` request, and receives a full response listing 137
  available v5 requests, the libobs version (32.1.2), the
  obs-websocket version (5.7.3), platform info, and supported
  image formats. Disconnection is clean. Pulsar speaks v5 end-to-
  end.

  One additional fork patch was needed: `WebSocketServer._obsReady`
  defaults to `true` (vs `false` upstream). The upstream gate is
  flipped by `OBS_FRONTEND_EVENT_FINISHED_LOADING`, fired by the
  Qt frontend when its event loop has settled. With no frontend in
  Pulsar the event never fires, the gate never opens, and every
  request returned `RequestStatus::NotReady` (code 207). Pulsar
  has no "loading phase" to wait for -- libobs is initialised by
  pulsar-headless before the websocket server starts accepting
  connections, so requests are valid from the first `Identify`.
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
