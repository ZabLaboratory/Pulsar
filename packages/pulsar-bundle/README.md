# @zablaboratory/pulsar-bundle

Bundles `pulsar.exe` and exposes a Node `spawn()` API. Pairs with [`@zablaboratory/pulsar-client`](../pulsar-client).

This package is **Windows x64 only**. The `os` / `cpu` fields in `package.json` make `npm install` skip it on every other platform without erroring out.

## Install

```bash
npm install @zablaboratory/pulsar-bundle
```

A `postinstall` step downloads the matching version's
`pulsar-windows-x64-v<VERSION>.zip` from the Pulsar GitHub Releases and
extracts it to `node_modules/@zablaboratory/pulsar-bundle/binaries/`.
The download is cached: re-running `npm install` against an unchanged
version is a no-op.

### Skip the binary download

```
PULSAR_BUNDLE_SKIP_POSTINSTALL=1 npm install   # CI-friendly, install-only
PULSAR_BUNDLE_DOWNLOAD_URL=<url> npm install   # mirror or internal CDN
```

If the GitHub Release isn't available (unpublished version, network failure), the postinstall logs a warning and exits cleanly. `spawn()` will then throw a clear error pointing at the missing `pulsar.exe`.

## Usage

```ts
import { spawn } from "@zablaboratory/pulsar-bundle";

const pulsar = await spawn({
  env: {
    PULSAR_CAPTURE_WINDOW: "Untitled - Notepad:Notepad:notepad.exe",
    PULSAR_VIDEO_BITRATE: "4500",
  },
  onLog: (stream, line) => console.log(`[${stream}] ${line}`),
});

const dest = await pulsar.client.destinations.create({
  kind: "twitch",
  key: process.env.TWITCH_KEY!,
});
await pulsar.client.destinations.start(dest.id);

pulsar.client.on("bitrateAdjusted", (e) =>
  console.log(`bitrate -> ${e.bitrate} kbps (${e.reason})`));

// ... operator workflow ...

await pulsar.shutdown();
```

`spawn()` resolves once `pulsar.exe` has printed its `ready, idling` boot marker AND the package's bundled `PulsarClient` has connected to the WebSocket on the session-random port. The handle exposes:

- `client` — connected `PulsarClient` (re-exported from `@zablaboratory/pulsar-client`)
- `port` — the loopback WebSocket port pulsar bound to
- `libobsVersion` — version string parsed from the boot log
- `child` — underlying `ChildProcess` (advanced use only)
- `shutdown()` — disconnect the client + terminate the child gracefully (idempotent)

## Environment variables

All pulsar.exe-recognised env vars are passed through via `opts.env`:

| Var | Purpose |
|---|---|
| `PULSAR_FPS` | 24 / 30 / 48 / 60 / 120 |
| `PULSAR_RESOLUTION` | `<W>x<H>` up to 8K |
| `PULSAR_VIDEO_BITRATE` | 200..50000 kbps |
| `PULSAR_AUDIO_BITRATE` | 32..512 kbps |
| `PULSAR_CAPTURE_WINDOW` | window descriptor `<title>:<class>:<exe>` |
| `PULSAR_RECORD_DIR` | output dir for the singleton recorder |
| `PULSAR_DESKTOP_AUDIO_DEVICE_ID` | pin desktop loopback device |
| `PULSAR_MIC_DEVICE_ID` | pin mic device |
| `PULSAR_PROCESS_AUDIO_NAME` | exe name for per-process loopback |
| `PULSAR_ADAPTIVE_BITRATE` | `off` to disable the adaptive worker |

## Versioning

Tracks `pulsar-client` and `pulsar.exe` in lockstep. `0.1.0` of this package downloads `pulsar-windows-x64-v0.1.0.zip`.

## Development

```bash
# from the monorepo root
npm install              # builds the workspace, runs postinstall (soft-fails when offline)
npm run build            # tsc -> dist/ for both packages
npm run test             # vitest in both packages

# from this package
npm run test:watch       # vitest in watch mode against the fake-pulsar fixture
```

Tests don't need a real `pulsar.exe`: a `tests/fake-pulsar.mjs` Node script mimics the boot marker + obs-websocket v5 handshake so the spawn lifecycle can be exercised without the C++ binary. `spawn()`'s `launchCommand` option (marked `@internal`) is the escape hatch the tests use; it is not part of the public API.

## License

MIT.
