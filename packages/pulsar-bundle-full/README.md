# @clodocapeo/pulsar-bundle-full

Full Pulsar bundle for Node consumers — `pulsar.exe` with **browser sources**, **text sources**, and **VLC-backed media sources** included on top of the standard light bundle. Same `spawn()` API as [`@clodocapeo/pulsar-bundle`](../pulsar-bundle).

Use this package when your scenes need any of:

- HTML / CSS / JS overlays (browser_source via CEF)
- Native text overlays (text_gdiplus / text_ft2_source)
- Local video file playback (vlc_source)

If you only need game capture + window/monitor capture + audio + recording + RTMP, the smaller [`@clodocapeo/pulsar-bundle`](../pulsar-bundle) (~40 MB zip) is enough.

## Install

```bash
npm install @clodocapeo/pulsar-bundle-full
```

Windows x64 only — `os: ["win32"]` in `package.json` makes `npm install` skip the binary on every other platform.

A `postinstall` step downloads the matching version's `pulsar-windows-x64-full-vX.Y.Z.zip` from the Pulsar GitHub Releases (~250 MB) and extracts it into `node_modules/@clodocapeo/pulsar-bundle-full/binaries/`. Cached: re-running `npm install` against an unchanged version is a no-op.

The 200 MB inflation over the light bundle is **CEF** (Chromium Embedded Framework, the runtime obs-browser links against). It is what makes browser sources possible.

### Environment overrides

Same as `@clodocapeo/pulsar-bundle`:

```
PULSAR_BUNDLE_SKIP_POSTINSTALL=1   # skip download (CI / offline)
PULSAR_BUNDLE_DOWNLOAD_URL=<url>   # mirror or internal CDN
```

## Usage

```ts
import { spawn } from "@clodocapeo/pulsar-bundle-full";

const pulsar = await spawn();

// HTML overlay via browser_source (the headline feature of -full)
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

// Native text source
await pulsar.client.obs.call("CreateInput", {
  sceneName: "Default",
  inputName: "Title",
  inputKind: "text_gdiplus",
  inputSettings: { text: "LIVE", font: { face: "Segoe UI", size: 64 } },
});

// VLC-backed local video file playback
await pulsar.client.obs.call("CreateInput", {
  sceneName: "Default",
  inputName: "Bumper",
  inputKind: "vlc_source",
  inputSettings: { playlist: ["C:/path/to/clip.mp4"] },
});

await pulsar.shutdown();
```

`spawn()` returns the same handle shape as the light bundle (`{ client, child, port, libobsVersion, shutdown }`). All the higher-level namespaces (`destinations`, `video`, `adaptive`, `record`, `stream`) are inherited from `@clodocapeo/pulsar-client` and work identically.

## Trade-off vs the light bundle

| | `pulsar-bundle` (light) | `pulsar-bundle-full` |
|---|---|---|
| Zip download | ~40 MB | ~250 MB |
| Extracted | ~100 MB | ~600 MB |
| Plugins | game capture, window, monitor, WASAPI, dshow webcam, x264/NVENC/QSV/AMF, ffmpeg, filters, transitions, multi-stream + websocket | + obs-browser (CEF) + obs-text + text-freetype2 + vlc-video |
| Best for | gaming streams, simple capture flows | composed scenes, HTML overlays, multi-source layouts |

Both bundles ship the same set of always-stripped plugins (obs-vst, obs-webrtc, decklink, frontend-tools, obs-libfdk) — those are deliberate omissions, see the repo's main README for the rationale.

## Versioning

Locked to `pulsar-client` and `pulsar.exe` in lockstep. `0.2.0` of this package downloads `pulsar-windows-x64-full-v0.2.0.zip` and depends on `@clodocapeo/pulsar-client@0.2.0`.

## Development

```bash
# from the monorepo root
npm install                 # workspaces resolve, postinstall soft-fails
                            # on offline / unreleased versions
npm run build               # tsc -> dist/
npm run test                # vitest with the same fake-pulsar fixture
                            # as @clodocapeo/pulsar-bundle
```

The vitest suite uses `tests/fake-pulsar.mjs` (a Node script that mimics pulsar.exe's boot) and exercises the same 5 lifecycle tests as `pulsar-bundle`. Browser-source-specific behaviour requires the real `pulsar.exe` with CEF; that's covered in the repo's integration smoke test, not the package's unit suite.

## License

GPL-2.0-or-later. See [`LICENSE`](./LICENSE).

This package bundles `pulsar.exe`, libobs + Pulsar plugins, CEF (browser
sources) and libVLC. The aggregate is distributed under GPL-2.0-or-later;
individual components retain their own upstream notices (BSD for CEF,
LGPL-2.1-or-later for libVLC, GPL-2.0-or-later for libobs and Pulsar).
Source for the GPL components is available at <https://github.com/ZabLaboratory/Pulsar>
at the matching version tag.

If you only need the typed client without any binary, use
[`@clodocapeo/pulsar-client`](https://www.npmjs.com/package/@clodocapeo/pulsar-client)
(MIT). For the binary without browser sources, use
[`@clodocapeo/pulsar-bundle`](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle).
