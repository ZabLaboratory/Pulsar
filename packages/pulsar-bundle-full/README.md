# @clodocapeo/pulsar-bundle-full

[![npm](https://img.shields.io/npm/v/%40clodocapeo%2Fpulsar-bundle-full?logo=npm&color=cb3837)](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle-full)
[![Licence GPL-2.0-or-later](https://img.shields.io/badge/licence-GPL--2.0--or--later-blue)](./LICENSE)
[![Platform](https://img.shields.io/badge/platform-windows--x64-0078d4)](https://github.com/ZabLaboratory/Pulsar/releases)
[![Node ≥ 18](https://img.shields.io/badge/node-%E2%89%A518-339933)](https://nodejs.org)

The **full** Pulsar bundle: ships `pulsar.exe` + libobs runtime +
encoders + capture plugins **plus** `obs-browser` (CEF for HTML /
JS overlays), `obs-text` (native text sources), and `vlc-video`
(libVLC media playback). Same Node `spawn()` API as
[`@clodocapeo/pulsar-bundle`](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle).

If you don't need browser sources / native text / VLC media, use
the lighter `@clodocapeo/pulsar-bundle` instead — same API, ~110 MB
less to download.

## Table of contents

- [What's in the box](#whats-in-the-box)
- [Choosing between light and full](#choosing-between-light-and-full)
- [Install](#install)
- [Quick start](#quick-start)
- [Browser / text / VLC sources](#browser--text--vlc-sources)
- [`spawn()` API](#spawn-api)
- [Bundling for distribution](#bundling-for-distribution)
- [CI / offline / mirror](#ci--offline--mirror)
- [Troubleshooting](#troubleshooting)
- [Versioning](#versioning)
- [Compatibility](#compatibility)
- [Licence](#licence)

## What's in the box

Everything in the **light** bundle, plus:

| Plugin | What it does |
|---|---|
| `obs-browser` (Pulsar fork) | HTML / CSS / JS scenes via Chromium Embedded Framework. Real Chromium runtime, GPU-composited, controllable from the host. |
| `pulsar-browser-page.exe` | The CEF helper executable. Lives next to `libcef.dll` in `obs-plugins/64bit/`. |
| `obs-text` | Native text sources (`text_gdiplus`). Direct GDI+ rendering — fast, sharp, no DOM overhead. |
| `text-freetype2` | FreeType-rendered text sources (`text_ft2_source_v2`). Better cross-script support than gdiplus, less Windows-coupled. |
| `vlc-video` | Local media playback (`vlc_source`) backed by libVLC. Playlists, all the codecs libVLC handles. |

### Size

| | Compressed | Extracted |
|---|---|---|
| Light bundle | ~40 MB | ~100 MB |
| **Full bundle** | **~150 MB** | **~370 MB** |

The ~110 MB inflation is **CEF** — the Chromium runtime obs-browser
links against. It's what makes browser sources possible.

## Choosing between light and full

**Use the full bundle when** your scenes need any of:

- HTML / CSS / JS overlays composed in a real browser engine
- Operator-controlled web UIs as broadcast inputs
- Native text overlays (lower-bargain than rendering text in a browser)
- Local video file playback via VLC (formats ffmpeg doesn't ship by default)
- Any obs-studio plugin that depends on the listed plugins

**Use [`@clodocapeo/pulsar-bundle`](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle) when** you can express your overlays
some other way: text rendered in a `Canvas` upstream of `pulsar.exe`,
overlays drawn into images / video files, scenes composed
client-side. The lean bundle saves ~110 MB and skips a lot of moving
parts (CEF subprocess management, GPU process, V8, …).

The two packages are **interchangeable from the API point of view**:

```ts
// Identical:
import { spawn } from "@clodocapeo/pulsar-bundle";
import { spawn } from "@clodocapeo/pulsar-bundle-full";
```

Switching is a `package.json` rename — no code change.

## Install

```bash
npm install @clodocapeo/pulsar-bundle-full
```

Windows x64 only — `os: ["win32"]` + `cpu: ["x64"]` in `package.json`
make `npm install` skip on every other platform.

A `postinstall` step downloads
`pulsar-windows-x64-full-v<VERSION>.zip` from the matching Pulsar
GitHub Release (~150 MB) and extracts it into
`node_modules/@clodocapeo/pulsar-bundle-full/binaries/`. Cached: the
download is skipped if `binaries/.version-stamp` already matches the
package version.

If the download fails (network error, unpublished version, 404), the
postinstall **soft-fails** with a warning — `npm install` completes,
the package installs, but `spawn()` will throw a clear error. Same
behaviour as the light bundle.

## Quick start

```ts
import { spawn } from "@clodocapeo/pulsar-bundle-full";

const pulsar = await spawn({
  env: {
    PULSAR_FPS: "60",
    PULSAR_RESOLUTION: "1920x1080",
    PULSAR_VIDEO_BITRATE: "6000",
  },
});

console.log(`pulsar booted: libobs ${pulsar.libobsVersion}, ws :${pulsar.port}`);

// HTML overlay via browser_source — the headline feature of -full
await pulsar.client.obs.call("CreateInput", {
  sceneName: "Default",
  inputName: "Overlay",
  inputKind: "browser_source",
  inputSettings: {
    url: "https://example.com/my-overlay",
    width: 1920,
    height: 1080,
  },
});

// Multi-destination + adaptive bitrate (same as light bundle)
const dest = await pulsar.client.destinations.create({
  kind: "twitch",
  key: process.env.TWITCH_KEY!,
});
await pulsar.client.destinations.start(dest.id);

// ... your application's broadcast workflow ...

await pulsar.shutdown();
```

The full client surface (six namespaces, typed events, typed errors,
v5 baseline passthrough) is documented in
[`@clodocapeo/pulsar-client`'s README](https://www.npmjs.com/package/@clodocapeo/pulsar-client)
— this package re-exports every symbol so a single import gets you
both the spawn API and the client types.

## Browser / text / VLC sources

The three plugins exclusive to this bundle expose new
[obs-websocket v5 input kinds](https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md#getinputkindlist).
You create them via `pulsar.client.obs.call("CreateInput", {...})`:

### Browser source (`browser_source`)

```ts
await pulsar.client.obs.call("CreateInput", {
  sceneName: "Default",
  inputName: "Overlay",
  inputKind: "browser_source",
  inputSettings: {
    url: "https://my.cdn/overlay/index.html",
    width: 1920,
    height: 1080,
    fps: 60,
    fps_custom: true,
    css: "body { background: transparent; }",
    shutdown: true,           // suspend audio when source is hidden
    restart_when_active: false,
  },
});

// At runtime, push events / state into the page:
await pulsar.client.obs.call("CallVendorRequest", {
  vendorName: "obs-browser",
  requestType: "emit_event",
  requestData: {
    event_name: "score_update",
    event_data: { home: 3, away: 2 },
  },
});
// In the page:  window.addEventListener("obsSourceCustomEvent", ...)
```

CEF runs the page in its own process (`pulsar-browser-page.exe`),
GPU-composited via libobs. The page can use the full Chromium API
including `OffscreenCanvas`, `WebAudio`, `WebGL2`, `WebTransport`, …

### Native text sources

Two flavours, pick whichever renders better for your locale:

```ts
// gdiplus: Windows-native, fast, sharp
await pulsar.client.obs.call("CreateInput", {
  sceneName: "Default",
  inputName: "Title",
  inputKind: "text_gdiplus_v3",
  inputSettings: {
    text: "LIVE",
    font: { face: "Segoe UI", size: 64, flags: 1 },
    color1: 0xFFFFFFFF,
    outline: true,
    outline_color: 0xFF000000,
  },
});

// freetype2: better non-Latin script support
await pulsar.client.obs.call("CreateInput", {
  sceneName: "Default",
  inputName: "Subtitle",
  inputKind: "text_ft2_source_v2",
  inputSettings: {
    text: "こんにちは",
    font: { face: "Noto Sans CJK JP", size: 48 },
  },
});
```

### VLC media source (`vlc_source`)

```ts
await pulsar.client.obs.call("CreateInput", {
  sceneName: "Default",
  inputName: "Bumper",
  inputKind: "vlc_source",
  inputSettings: {
    playlist: [
      { value: "C:/path/to/intro.mp4", selected: false, hidden: false },
      { value: "C:/path/to/sponsor.mkv", selected: false, hidden: false },
    ],
    loop: true,
    shuffle: false,
    playback_behavior: "stop_restart",
  },
});
```

Useful for codecs ffmpeg's default build doesn't support, or playlist
behaviour the FFmpeg muxer source can't express.

## `spawn()` API

Identical to the light bundle's. See the
[`@clodocapeo/pulsar-bundle` README](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle)
for the full reference; it covers:

- The `SpawnOptions` shape (`binariesPath`, `env`, `readyTimeoutMs`,
  `onLog`)
- The `SpawnedPulsar` handle (`client`, `child`, `port`,
  `libobsVersion`, `runtimeInstanceId`, `runtimeDir`, `shutdown()`)
- Boot environment variables (the full `PULSAR_*` matrix)
- Concurrent runtime isolation and the DirectShow legacy-alias lease
- Lifecycle (boot / shutdown / crash recovery)

Everything below is specific to the full bundle.

### Boot time

The full bundle takes **~1 s longer to boot** than the light variant —
CEF needs to spin up its own subprocess + GPU process before
`obs-browser` reports ready. The 30 s default `readyTimeoutMs` still
has plenty of headroom; bump it on contended CI runners or under
heavy AV scanning.

### CEF subprocess

`pulsar-browser-page.exe` ships in `obs-plugins/64bit/` next to
`libcef.dll` (mandatory — CEF resolves its helper executable
relative to the DLL). It is spawned by libobs via CEF's own
multi-process architecture; you don't manage it from Node.

If you see `pulsar-browser-page.exe` stuck in Task Manager after
`pulsar.exe` has exited, that's a known CEF-side issue (rare, usually
on heavy-load shutdowns). Pulsar's `taskkill` fallback in `shutdown()`
takes care of it; manual cleanup is `taskkill /F /IM pulsar-browser-page.exe`.

## Bundling for distribution

The full bundle is bigger — make sure your packaging tooling honours
the `asarUnpack` glob and the `cwd` constraint.

### electron-builder

```jsonc
{
  "asar": true,
  "asarUnpack": [
    "node_modules/@clodocapeo/pulsar-bundle-full/binaries/**/*"
  ],
  "files": [
    "dist/**/*",
    "node_modules/@clodocapeo/pulsar-bundle-full/**/*"
  ],
  "extraResources": []
}
```

Then in your Electron main process:

```ts
import { app } from "electron";
import { spawn } from "@clodocapeo/pulsar-bundle-full";
import { resolve } from "node:path";

const binariesPath = app.isPackaged
  ? resolve(
      process.resourcesPath,
      "app.asar.unpacked",
      "node_modules",
      "@clodocapeo",
      "pulsar-bundle-full",
      "binaries",
    )
  : undefined;

const pulsar = await spawn({ binariesPath });
```

### Installer size

A typical Electron app with `pulsar-bundle-full` in NSIS comes out
**around 200 MB** installed (your app code + Electron + Pulsar full
bundle). If that matters, ship `pulsar-bundle` (light) and
implement scene composition client-side; if it doesn't, the full
bundle gives you the most expressive scene model.

## CI / offline / mirror

Same env vars as the light bundle:

```bash
# CI: install but skip the 150 MB download
PULSAR_BUNDLE_SKIP_POSTINSTALL=1 npm ci

# Internal mirror
PULSAR_BUNDLE_DOWNLOAD_URL=https://my-mirror.internal/pulsar/v1.0.0-full.zip npm install
```

`PULSAR_BUNDLE_DOWNLOAD_URL` overrides the URL for **this package
only** — the light and full bundles each have their own download URL,
so a `pulsar-bundle` install in the same workspace fetches its own
zip from the default GitHub Releases path.

## Troubleshooting

In addition to everything in the
[light bundle's troubleshooting](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle#troubleshooting):

### Browser source shows a blank / black square

CEF subprocess failed to start. Common causes:

1. **`pulsar-browser-page.exe` missing from `obs-plugins/64bit/`** —
   antivirus may have quarantined it. Restore + whitelist.
2. **GPU disabled in CEF.** On some virtualised / RDP environments
   CEF can't initialise its GPU process. The `obs-browser` plugin
   logs the failure to stderr — capture it via `onLog` and look for
   `gpu_process_host`.
3. **URL not reachable.** Check `pulsar.client.obs.call("GetInputSettings", { inputName })`
   matches what you set, and that the URL responds with `200 OK`
   (CEF won't render a 404 page by default).

### `Helper process exited with code 63`

This is the CEF subprocess crashing on launch. **Almost always**
means `pulsar-browser-page.exe` is in the wrong directory — it must
be in `obs-plugins/64bit/` next to `libcef.dll`, not in
`bin/64bit/`. The full bundle's CMake places it correctly; if you
re-stage the bundle by hand, preserve that layout.

### Audio from a `browser_source` is muffled / out of sync

Browser source audio goes through CEF's audio pipeline + libobs's
WASAPI graph — two A/V resync paths. Mitigations:

- Set `restart_when_active: false` so the browser process keeps
  running across scene changes (avoids re-handshake stalls).
- Pin the page's audio sample rate to 48000 to match libobs's
  audio mix bus.
- For CEF audio + scene-A-to-scene-B transitions, set the source's
  audio sync offset via the v5 `SetInputAudioSyncOffset` request.

### `vlc_source` shows nothing

The bundled libVLC is stripped of optional codec support to keep the
zip size manageable. If your media file plays in standalone VLC but
not in Pulsar, the codec is probably not in the bundle. Workarounds:
re-encode to H.264/AAC, or use the FFmpeg muxer source
(`ffmpeg_source`) which ships with the light bundle anyway.

## Versioning

Tracks `pulsar-client` and `pulsar.exe` in lockstep — `1.0.0` of this
package downloads `pulsar-windows-x64-full-v1.0.0.zip` and depends on
`@clodocapeo/pulsar-client@1.0.0`.

Bump light, full, and client together; npm semver resolution rejects
mixed versions in a workspace.

## Compatibility

| | |
|---|---|
| OS | Windows 10/11 x64 only |
| Node | ≥ 18 |
| Module system | ESM only |
| CEF | bundled — version pinned by upstream `obs-browser` at the recorded SHA |
| Antivirus | Whitelist the `binaries/` directory; CEF helper exes are common false positives |

## Licence

[GPL-2.0-or-later](./LICENSE).

This package bundles `pulsar.exe`, libobs + Pulsar plugins, CEF, and
libVLC. The aggregate is distributed under GPL-2.0-or-later;
individual components retain their upstream notices:

| Component | Licence |
|---|---|
| libobs, obs-studio plugins, Pulsar plugins | GPL-2.0-or-later |
| CEF (Chromium Embedded Framework) | BSD-3-Clause |
| libVLC | LGPL-2.1-or-later |

Source for the GPL components is available at
<https://github.com/ZabLaboratory/Pulsar> at the matching version
tag.

### Bundling Pulsar in a non-GPL application

Same four invariants as the light bundle:

1. **Process boundary.** Always spawn `pulsar.exe` as a separate OS
   process. Never `LoadLibrary` / `dlopen` `pulsar.exe`,
   `libcef.dll`, `pulsar-*.dll`, or any libobs binary.
2. **WebSocket-only IPC.** No FFI, no shared memory, no native
   bindings.
3. **No FFI surface on Pulsar's side.** Don't add
   `__declspec(dllexport)` to any plugin. Pulsar's CI gates this.
4. **No source copy-paste.** Don't include lines copied from
   libobs / obs-websocket / obs-browser / CEF source trees in your
   application.

Read [`LICENSE-INVARIANTS.md`](https://github.com/ZabLaboratory/Pulsar/blob/main/LICENSE-INVARIANTS.md)
on the Pulsar repo for the full contract, then
[`CONSUMER-AUDIT.md`](https://github.com/ZabLaboratory/Pulsar/blob/main/CONSUMER-AUDIT.md)
for the empirical checklist your application's CI must enforce.

If you only need the typed client without any GPL binary, use
[`@clodocapeo/pulsar-client`](https://www.npmjs.com/package/@clodocapeo/pulsar-client)
instead — it's MIT.

For the binary without browser sources / CEF / VLC, use
[`@clodocapeo/pulsar-bundle`](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle).
