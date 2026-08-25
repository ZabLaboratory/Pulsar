# pulsar-browser

Forked from [obs-studio's obs-browser plugin](https://github.com/obsproject/obs-browser), trimmed for Pulsar's headless deployment model.

## Why a fork

The upstream obs-browser plugin assumes OBS Studio's Qt UI host : an attached display, a Qt event loop, an OpenGL/D3D11 swap chain, browser docks, tooltip surfaces. In Pulsar's headless service mode none of that exists, and several upstream behaviours actively break the runtime :

- **`obs-browser-page.exe`** (the CEF subprocess helper) exports
  `NvOptimusEnablement = 1` and `AmdPowerXpressRequestHighPerformance = 1`. These exports tell Windows's laptop GPU switching logic to bind the *dedicated* GPU to the process. On a host with no display attached to that dedicated GPU, the CEF GPU subprocess crashes at the first frame pull (`gpu_data_manager_impl_private.cc: GPU process isn't usable. Goodbye.`) and takes the renderer down with it — the symptom users see is *"Webpage has crashed unexpectedly!"* immediately when the encoder starts.
- The renderer subprocess **needs `--no-sandbox`** because Pulsar is built without the CEF sandbox SDK.
- Half the source includes Qt headers (`QApplication`, `QThread`, `QToolTip`, `QMetaObject`, …) — useful in the OBS Studio UI to integrate with the host event loop, dead weight in a headless service.

## What our fork changes

| File | Change |
|---|---|
| `obs-browser-page/obs-browser-page-main.cpp` | Drop the `NvOptimusEnablement` / `AmdPowerXpressRequestHighPerformance` `__declspec(dllexport)` declarations. Windows now picks the integrated GPU (or falls back to SW). Same posture as Puppeteer / Playwright headless. |
| `browser-app.cpp` | `OnBeforeCommandLineProcessing` appends `--no-sandbox` while preserving CEF GPU acceleration. Shared D3D11 textures remain the primary offscreen-rendering path. |
| `browser-client.cpp` | `OnTooltip` becomes a no-op returning `false` (CEF falls back to its native default — no tooltip surface in headless). Qt includes wrapped in `#ifdef ENABLE_BROWSER_QT_LOOP` (which we never define). |
| `obs-browser-source.cpp` | Same Qt-include guarding. |
| `CMakeLists.txt` | Rewritten Pulsar-style : no OBS macros, direct linkage against libobs/obs-frontend-api, **zero Qt linkage** (no `find_package(Qt6)`, no `Qt6::*` in `target_link_libraries`). The runtime emits `pulsar-browser.dll` + `pulsar-browser-page.exe` into the libobs plugin / bin dirs ; `scripts/build-win.ps1` deletes the upstream `obs-browser.dll` + `obs-browser-page.exe` before our build so libobs picks up our binaries instead. |

## What stays identical to upstream

The wire-level behaviour. From a `browser_source` consumer's perspective (libobs source kind, the `OBS_PROPERTY_*` schema, the obs-websocket `obs-browser` vendor namespace, the URL / FPS / width / height / CSS settings) **nothing changed**. Existing scripts, vendor requests, scene templates work as-is.

The CEF version is the same (vendored `cef_binary_6533_windows_x64`), nlohmann_json version is the same, all the rendering / scheme handling / IPC code is byte-identical to upstream. The fork is intentionally small.

## What stays in the source tree but isn't built

`panel/`, `drm-format.cpp`, `linux-keyboard-helpers.hpp`, `helper-info.plist` — all left untouched (we copied the upstream tree verbatim). The new `CMakeLists.txt` simply doesn't list them in the `add_library` source list, so they don't compile. Keeps the diff against upstream small for future rebases.

## License

GPL-2.0-or-later, inherited from libobs (and from upstream obs-browser, which is BSD-2-Clause but linked against GPL libobs). See the repo root [`LICENSE`](../../LICENSE) and [`LICENSE-INVARIANTS.md`](../../LICENSE-INVARIANTS.md) for what this means for consumers that bundle Pulsar.

The `__declspec(dllexport)` symbols upstream emitted in `obs-browser-page.exe` are gone — `pulsar-browser-page.exe` exports nothing, the same isolation invariant `pulsar.exe` enforces.

## Rebase strategy

When upstream OBS Studio bumps obs-browser, the rebase is :

1. `cp -r upstream/plugins/obs-browser/* plugins/pulsar-browser/` (overwrites the fork)
2. Re-apply this README's bullet list of changes (small, mechanical) :
   - drop NvOptimusEnablement / AmdPowerXpressRequestHighPerformance exports
   - add the required `--no-sandbox` switch in `OnBeforeCommandLineProcessing` without disabling GPU acceleration
   - guard `<QApplication>` / `<QThread>` / `<QToolTip>` includes with `#ifdef ENABLE_BROWSER_QT_LOOP`
   - make `OnTooltip` a no-op returning false outside `ENABLE_BROWSER_QT_LOOP`
   - keep the Pulsar `CMakeLists.txt` (don't re-copy upstream's)
3. Build, run probe-twitch-live, confirm.

If a future upstream change makes one of these patches obvious / non-applicable, drop it. The smaller the fork, the cheaper the rebase.
