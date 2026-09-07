# Pulsar native components (3.0.0)

This directory contains executable, static, module and header-only components.
They are not all OBS plugins. The top-level [CMake project](../CMakeLists.txt)
builds them against the same patched OBS headers and libraries.

| Component | Built artifact | Ownership |
|---|---|---|
| [pulsar-headless](pulsar-headless/README.md) | `pulsar.exe` | Bootstrap, runtime namespace, readiness, logs and teardown. |
| [pulsar-frontend-stub](pulsar-frontend-stub/README.md) | Static library in `pulsar.exe` | Frontend API, hot production lanes/views, encoders, singleton outputs, common audio and scene-switch vendor. |
| [pulsar-websocket](pulsar-websocket/README.md) | `obs-websocket.dll` | v5 control/authentication/events and vendor dispatch. |
| [pulsar-multi-stream](pulsar-multi-stream/README.md) | `pulsar-multi-stream.dll` | Twitch/YouTube/custom RTMP/local destinations, adaptive bitrate, capabilities, audio and diagnostics. |
| [pulsar-browser](pulsar-browser/README.md) | `pulsar-browser.dll`, `pulsar-browser-page.exe` | Full-bundle CEF rendering and callback/task lifetime. |
| [pulsar-scene-source](pulsar-scene-source/README.md) | `pulsar-scene-source.dll` | Legacy managed browser-capture replacement. |
| [pulsar-nv-secure-load](pulsar-nv-secure-load/README.md) | Header-only interface | Shared effect-SDK loading/probe policy. |
| [pulsar-output-classify](pulsar-output-classify/README.md) | Header-only interface | Shared stable output-failure classification. |

## Control ownership

The `pulsar` vendor belongs to multi-stream, `pulsar-scene` to scene-source
and `pulsar-scene-switch` to the frontend production controller.
All are reached through obs-websocket's `CallVendorRequest`; a plugin must
not attempt a second registration under another owner's vendor name.

Baseline OBS-compatible scene authoring is distinct from the deterministic
Prepare/Take contract. A source-replacement helper is not an atomic production
switch, and output-request acceptance is not proof of effective on-air state.

## Native versus patched code

Features that can live in owned components belong here. Core video/view/encode,
DirectShow and graphics changes that require upstream internals live in the
[patch stack](../patches/README.md). The pin already incorporates foundational
Pulsar changes; the [complete inventory](../docs/LIBOBS-CHANGES.md) distinguishes
those from the 52 additional patches and this separate browser fork.

Public shared headers are explicit cross-target contracts. Private ownership
must not be bypassed with ad hoc cross-component access. Build, package,
protocol and native tests must all target the same revision.

## Headless and licensing

There is no OBS Studio UI, but the executable and WebSocket integration still
use a minimal Qt runtime. The browser component removes its own Qt UI linkage.
Internal module symbols are not a host FFI; external application control is
WebSocket. See [architecture](../docs/ARCHITECTURE.md),
[protocol](../docs/PROTOCOL.md) and
[distribution constraints](../LICENSE-INVARIANTS.md).
