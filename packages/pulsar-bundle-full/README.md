# @clodocapeo/pulsar-bundle-full

[![npm](https://img.shields.io/npm/v/%40clodocapeo%2Fpulsar-bundle-full)](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle-full)

The **full Windows x64 Pulsar 3.0.0 runtime**, with CEF/browser, native text,
VLC module and gated NVIDIA effect module in addition to the light features.

It exposes the same `spawn()` API and matching client as the
[light bundle](../pulsar-bundle/README.md). That guide is the complete shared
reference for options, errors, namespace isolation, shutdown, offline installs
and packaging. This document covers the full variant's additional behavior.

## Install and first run

```powershell
npm install @clodocapeo/pulsar-bundle-full@3.0.0
```

```js
import { spawn } from "@clodocapeo/pulsar-bundle-full";

const pulsar = await spawn({ readyTimeoutMs: 60_000 });
try {
  console.log(await pulsar.client.capabilities.get());
} finally {
  await pulsar.shutdown();
}
```

Node 18+, ESM and Windows x64 are required. Postinstall downloads
`pulsar-windows-x64-full-v3.0.0.zip` into `binaries/`.
A download warning can leave npm installation successful without a usable
binary; check the artifact before deployment.

## Additional capabilities

| Family | What is included | What to verify |
|---|---|---|
| Browser | Pulsar's `browser_source` fork, CEF/helper/resources | Scene URL, rendering path, helper lifetime and actual PGM frames. |
| Text | GDI+/FreeType native text modules | Registered input kind and installed font. |
| VLC | `vlc-video` module | Whether libVLC and its codecs are actually available on the host/distribution. |
| NVIDIA effects | `nv-filters` module | Validated SDK directory/version/models and live capability report. SDK payload is not bundled. |

Window, display, game and DirectShow capture, WASAPI, encoding, streaming,
recording, replay and dual-lane control are **not full-only**.

The package does not guarantee NDI, arbitrary OBS plugins, Preview audio/AFV,
every VLC codec or every GPU. Query actual capability and source registration.

## Browser capture example

With a running `pulsar` handle and your own reachable scene server:

```js
const result = await pulsar.client.obs.call("CallVendorRequest", {
  vendorName: "pulsar-scene",
  requestType: "SetCaptureSource",
  requestData: {
    kind: "browser_source",
    url: "http://127.0.0.1:3000/scene",
    width: 1920,
    height: 1080,
    fps: 60,
    reroute_audio: false,
  },
});
console.log(result.responseData);
```

This uses the **legacy single managed-capture replacement** helper.
For independent Preview preparation and atomic Takes, use
[scene-switch v1](../../scripts/contracts/scene_switch_v1/README.md).
Creating a browser source is not proof that its page renders healthy frames.

The managed source pins webpage control to None. The page is content, not
authorized broadcast-control code. CEF runs without the sandbox SDK in this
build; the scene server/content is part of the application's trust boundary.

### Render and helper layout

The distribution ships `pulsar-browser.dll` and
`pulsar-browser-page.exe` next to CEF under `obs-plugins/64bit/`.
The upstream `obs-browser.dll` is removed to avoid duplicate source
registration. Preserve all CEF locale/resource files and the helper path.

GPU acceleration is the normal supported hardware rendering path;
software rendering has separate callback tests. Do not disable GPU globally
to make an accelerated-rendering test green. A no-physical-GPU CI skip is
reported as unexecuted hardware proof.

Callback admission, asynchronous source tasks and browser close are fenced
before libobs teardown. Do not kill every browser helper by image name;
multiple Pulsar runtimes may own independent children.

## Text and media sources

Use `GetInputKindList` / the capability manifest to select the actual kind.
Typical source IDs include `text_gdiplus_v2`, `text_ft2_source_v2`,
`vlc_source` and `ffmpeg_source`, subject to registration.

For text, select a font available on the target machine. For VLC, do not
assume the presence of the module means a complete libVLC codec distribution
was shipped. Validate playback on the installed artifact. FFmpeg media source
is a different input from the FFmpeg recording/muxer output.

When using a local stinger, pass an explicit absolute `PULSAR_STINGER_ASSET`.
The legacy cwd-relative fallback is not reliable with private runtime cwd.
Dual-lane Stinger/Fade requires its own opt-in and validates the asset before
use; missing/invalid input must not be treated as a successful transition.

## Lifecycle

The shared launcher waits for the idle marker, reads this child's protected
config and authenticates v5. It does not wait for every browser page to finish
rendering. Observe actual source/Preview readiness separately.

Finalize active outputs and retain files before calling `shutdown()`.
The Node helper terminates the child with a bounded fallback; Windows
termination is not a guarantee of graceful CEF/libobs teardown or MP4
finalization. The native shutdown-event harness is a different path.

Default generated runtime directories are temporary and may be removed after
shutdown. Supply persistent recording/log destinations or an explicit
application-owned runtime directory.

## Packaging Electron

Keep the native tree outside ASAR:

```json
{
  "asar": true,
  "asarUnpack": [
    "node_modules/@clodocapeo/pulsar-bundle-full/binaries/**/*"
  ]
}
```

```ts
import { app } from "electron";
import { resolve } from "node:path";
import { spawn } from "@clodocapeo/pulsar-bundle-full";

const binariesPath = app.isPackaged
  ? resolve(process.resourcesPath, "app.asar.unpacked", "node_modules",
      "@clodocapeo", "pulsar-bundle-full", "binaries")
  : undefined;
const pulsar = await spawn({ binariesPath });
```

Verify the final installer and deployed bytes. Use actual ZIP/install size
instead of historical full-bundle estimates. Preserve the full helper/resource
layout when staging a sidecar runtime.

## Offline and mirror installs

`PULSAR_BUNDLE_SKIP_POSTINSTALL=1` skips download.
`PULSAR_BUNDLE_DOWNLOAD_URL` selects a trusted matching **full** archive.

Both light and full postinstall scripts read the same override name.
An override is not automatically scoped to this package in a workspace install.
Do not send a full ZIP to both installers unintentionally.

The capture-test-only `PULSAR_BUNDLE_FULL_BINARIES_PATH` is read by the test
harness, not by public `spawn()`; applications use `binariesPath`.

## Troubleshooting

| Symptom | Checks |
|---|---|
| Browser black/frozen | Scene URL/content, source settings, real recorded frames and helper logs. HTTP success alone is insufficient. |
| Helper launch/exit error | Matching binary dependencies, one browser loader, correct CEF/helper/resource locations; an exit code alone is not a root-cause proof. |
| CEF GPU unavailable | Actual adapter/driver/session capability; do not relabel software rendering as accelerated proof. |
| Text missing | Font and registered text kind on the target machine. |
| VLC source unavailable | Module plus actual libVLC/runtime/codec availability. |
| Effect filter absent | Validated SDK capability; no SDK/model is bundled. |
| Recording lost after shutdown | Persistent output path and awaited finalization before child termination. |

The [capture/PGM suite](../capture-pgm-compat/README.md) distinguishes black,
frozen and healthy recordings. The [embedding guide](../../docs/PRISM-EMBEDDING.md)
covers artifact verification and multi-instance ownership.

## Version and license

The 3.0.0 package downloads the v3.0.0 full archive and pins client 3.0.0.
npm may resolve multiple versions; explicit version matching remains the
integrator's responsibility.

The runtime bundle is [GPL-2.0-or-later](LICENSE). Component notices retain
their original licenses, and the client remains MIT. Read the
[distribution constraints](../../LICENSE-INVARIANTS.md) and
[consumer audit](../../CONSUMER-AUDIT.md); this README does not promise a
legal outcome for an arbitrary non-GPL host.
