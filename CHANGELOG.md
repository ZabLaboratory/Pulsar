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
