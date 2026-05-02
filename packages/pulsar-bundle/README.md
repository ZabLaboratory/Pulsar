# @clodocapeo/pulsar-bundle

[![npm](https://img.shields.io/npm/v/%40clodocapeo%2Fpulsar-bundle?logo=npm&color=cb3837)](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle)
[![Licence GPL-2.0-or-later](https://img.shields.io/badge/licence-GPL--2.0--or--later-blue)](./LICENSE)
[![Platform](https://img.shields.io/badge/platform-windows--x64-0078d4)](https://github.com/ZabLaboratory/Pulsar/releases)
[![Node ≥ 18](https://img.shields.io/badge/node-%E2%89%A518-339933)](https://nodejs.org)

The **light** Pulsar bundle: ships `pulsar.exe` + libobs runtime +
encoders + capture plugins, and exposes a Node `spawn()` API that
returns a connected, typed
[`PulsarClient`](https://www.npmjs.com/package/@clodocapeo/pulsar-client).

For HTML overlays / browser sources / native game capture / VLC media
sources, install [`@clodocapeo/pulsar-bundle-full`](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle-full)
instead — same `spawn()` API, larger payload (CEF runtime).

## Table of contents

- [What's in the box](#whats-in-the-box)
- [Install](#install)
- [Quick start](#quick-start)
- [`spawn()` API](#spawn-api)
- [Spawn options](#spawn-options)
- [Boot environment variables](#boot-environment-variables)
- [Lifecycle](#lifecycle)
- [Talking to the running pulsar](#talking-to-the-running-pulsar)
- [Bundling for distribution](#bundling-for-distribution)
- [CI / offline / mirror](#ci--offline--mirror)
- [Troubleshooting](#troubleshooting)
- [Versioning](#versioning)
- [Compatibility](#compatibility)
- [Licence](#licence)

## What's in the box

| | Light bundle | Full bundle |
|---|---|---|
| Zip download | ~40 MB | ~150 MB |
| Extracted | ~100 MB | ~370 MB |
| `pulsar.exe` + libobs runtime | ✅ | ✅ |
| Encoders (x264, NVENC, QSV, AMF, AAC, FFmpeg muxer) | ✅ | ✅ |
| Capture (window, monitor, game-via-DLL-injection, dshow webcam) | ✅ | ✅ |
| WASAPI audio (mic / desktop / per-process) | ✅ | ✅ |
| Multi-destination (Twitch / RTMP / VOD MP4) | ✅ | ✅ |
| Adaptive bitrate worker | ✅ | ✅ |
| obs-websocket (v5 + `pulsar:*` vendor) | ✅ | ✅ |
| `obs-browser` (CEF, HTML overlays, JS scenes) | ❌ | ✅ |
| `obs-text` (native text sources) | ❌ | ✅ |
| `vlc-video` (libVLC media playback) | ❌ | ✅ |

**Use this package** if you only need streaming + recording + window /
monitor / game capture + WASAPI audio. The 40 MB postinstall is one
quarter of the full bundle.

**Use the full bundle** if you need any of: HTML overlays, browser-based
scene composition, native text sources, VLC-backed playlists.

The two packages are interchangeable — same `spawn()` shape, same
client surface. Switching is a `package.json` rename and a one-line
import change; no code change.

## Install

```bash
npm install @clodocapeo/pulsar-bundle
```

The `os: ["win32"]` + `cpu: ["x64"]` fields make `npm install` skip the
package on every other platform without erroring out — safe to list as
a dependency in a cross-platform repo.

A `postinstall` step downloads
`pulsar-windows-x64-v<VERSION>.zip` from the matching Pulsar GitHub
Release and extracts it to
`node_modules/@clodocapeo/pulsar-bundle/binaries/`. The download is
cached: re-running `npm install` on an unchanged version is a no-op
(checked via `binaries/.version-stamp`).

If the download fails (network error, unpublished version, 404), the
postinstall **soft-fails** with a warning — `npm install` completes,
the package installs, but `spawn()` will throw a clear error pointing
at the missing `pulsar.exe`. This is intentional: a CI matrix that
never spawns pulsar shouldn't blow up just because the binary couldn't
be fetched.

## Quick start

```ts
import { spawn } from "@clodocapeo/pulsar-bundle";

const pulsar = await spawn({
  env: {
    PULSAR_FPS: "60",
    PULSAR_VIDEO_BITRATE: "6000",
    PULSAR_CAPTURE_WINDOW: "Untitled - Notepad:Notepad:notepad.exe",
  },
  onLog: (stream, line) => {
    if (line.includes("error") || line.includes("warn")) {
      console.log(`[pulsar/${stream}] ${line}`);
    }
  },
});

console.log(`pulsar booted: libobs ${pulsar.libobsVersion}, ws :${pulsar.port}`);

// Multi-destination
const dest = await pulsar.client.destinations.create({
  kind: "twitch",
  key: process.env.TWITCH_KEY!,
});
await pulsar.client.destinations.start(dest.id);

// Watch the adaptive worker
pulsar.client.on("bitrateAdjusted", (e) =>
  console.log(`bitrate -> ${e.bitrate} kbps (${e.reason})`),
);

// ... your application's broadcast workflow ...

await pulsar.client.destinations.stop(dest.id);
await pulsar.client.destinations.remove(dest.id);
await pulsar.shutdown();
```

Run with:

```bash
TWITCH_KEY=live_xxx node app.mjs
```

That's it. `spawn()` returns a connected, typed client. The full
client surface (destinations / video / adaptive / record / stream / v5
baseline / typed events / errors) is documented in
[`@clodocapeo/pulsar-client`'s README](https://www.npmjs.com/package/@clodocapeo/pulsar-client) —
this package re-exports every symbol so you don't need a second
dependency line.

## `spawn()` API

```ts
import { spawn } from "@clodocapeo/pulsar-bundle";

function spawn(options?: SpawnOptions): Promise<SpawnedPulsar>;

interface SpawnedPulsar {
  /** Connected PulsarClient ready for v5 + vendor calls. */
  client: PulsarClient;

  /** Underlying ChildProcess. Most callers should use shutdown()
   *  instead of touching this directly, but it's exposed for
   *  advanced use cases (sending custom signals, reading stdio). */
  child: ChildProcess;

  /** WebSocket port the obs-websocket server bound to. */
  port: number;

  /** libobs version string parsed from the boot log
   *  (e.g. "32.1.2-1-g8c23ba721-pulsar"). */
  libobsVersion: string;

  /** Disconnect the WS client and terminate pulsar.exe. Resolves once
   *  the process has exited. Idempotent — call as many times as you
   *  like, only the first one does work. */
  shutdown(): Promise<void>;
}
```

The promise resolves once **both** of these have happened:

1. `pulsar.exe` printed `pulsar-headless: libobs <version> ready, idling`
   on stdout.
2. The bundled `PulsarClient` connected to the WebSocket on the
   session-random port and completed the v5 Identify handshake.

If either step fails (boot timeout, WS connect timeout, auth
rejection), `spawn()` rejects with a typed error and the child process
is killed.

## Spawn options

```ts
interface SpawnOptions {
  /** Override the directory containing bin/64bit/pulsar.exe.
   *  Default: <package>/binaries (populated by postinstall). */
  binariesPath?: string;

  /** Extra env vars for pulsar.exe. See "Boot environment variables"
   *  below. */
  env?: Record<string, string>;

  /** How long to wait for "ready, idling" on stdout. Default 30 s. */
  readyTimeoutMs?: number;

  /** Optional log forwarder. Receives one stdout/stderr line at a time.
   *  Useful for piping into your application's log aggregator. */
  onLog?: (stream: "stdout" | "stderr", line: string) => void;
}
```

### `binariesPath`

By default, `spawn()` looks under
`<this package>/binaries/bin/64bit/pulsar.exe`. Override when:

- **Monorepo dev**: point at your local
  `upstream/build_x64/rundir/RelWithDebInfo/` to spawn the binary you
  just built without round-tripping through the postinstall download.
- **Custom packaging**: when packaging your application with
  electron-builder / pkg / oxc-pack, you may stage the binary
  somewhere else and pass the path explicitly.

```ts
import { resolve } from "node:path";
import { app } from "electron";

const binariesPath = app.isPackaged
  ? resolve(process.resourcesPath, "pulsar")          // app.asar.unpacked
  : resolve(__dirname, "../../upstream/build_x64/rundir/RelWithDebInfo");

const pulsar = await spawn({ binariesPath });
```

### `env`

Merged on top of `process.env` for the child process. You can override
boot-time configuration (FPS, resolution, bitrates, capture target,
record dir) without touching the parent process's env.

The two env vars you almost always want to set:

```ts
import { randomBytes } from "node:crypto";

const sessionPassword = randomBytes(16).toString("base64url");
const sessionPort     = await pickFreePort(); // see PRISM-EMBEDDING.md

const pulsar = await spawn({
  env: {
    PULSAR_PORT:     String(sessionPort),
    PULSAR_PASSWORD: sessionPassword,
  },
});
```

If you don't pin them, Pulsar generates a fresh random password each
session and uses port 4455 by default. The password ends up in
`<binaries>/bin/64bit/obs-websocket/config.json`, which `spawn()`
reads after the boot marker.

### `readyTimeoutMs`

A clean Pulsar boot reaches the ready marker in:

- ~3 s on a warm cache (libobs already loaded once this Windows session)
- ~6 s on a cold start (first spawn after reboot)

The 30 s default leaves headroom for slow disks / antivirus scans /
loaded systems. Bump it to 60 s on contended CI runners.

### `onLog`

Receives one line per `\n` boundary on stdout / stderr — line endings
are stripped, so don't append `\n` when forwarding. Useful for piping
the libobs / plugin boot log into your application's structured logger.

```ts
const pulsar = await spawn({
  onLog: (stream, line) => myLogger[stream === "stdout" ? "info" : "warn"]("pulsar", line),
});
```

The `onLog` callback fires for **every** line — including the
`PULSAR_READY ` sentinel — even before `spawn()` resolves.

## Boot environment variables

All `PULSAR_*` env vars recognised by `pulsar.exe` are passed through
via `opts.env`.

| Var | Type | Default | Purpose |
|---|---|---|---|
| `PULSAR_PORT` | port | `4455` | obs-websocket port. Pin to avoid collisions with stock OBS / a second Pulsar instance. |
| `PULSAR_PASSWORD` | string | random 22-char URL-safe | obs-websocket auth password. Pin via `randomBytes(...)` per session. |
| `PULSAR_FPS` | int | `60` | Output frame rate. Common: 24 / 30 / 48 / 60 / 120. |
| `PULSAR_RESOLUTION` | `<W>x<H>` | `1920x1080` | Output canvas size. Up to 8K. |
| `PULSAR_VIDEO_BITRATE` | kbps | `6000` | x264 / NVENC bitrate. Range 200..50000. |
| `PULSAR_AUDIO_BITRATE` | kbps | `160` | AAC bitrate. Range 32..512. |
| `PULSAR_CAPTURE_WINDOW` | `<title>:<class>:<exe>` | unset (no window source) | Window descriptor for `window_capture`. Find it via `obs.call("GetSourceFilterList")` after a one-shot enumerate, or with [Spy++](https://learn.microsoft.com/en-us/visualstudio/debugger/introducing-spy-increment) for a manual lookup. |
| `PULSAR_RECORD_DIR` | path | `<cwd>/recordings/` | Output dir for the singleton recorder + the auto-named MP4. |
| `PULSAR_DESKTOP_AUDIO_DEVICE_ID` | device id | system default | Pin desktop loopback device. |
| `PULSAR_MIC_DEVICE_ID` | device id | system default | Pin mic device. |
| `PULSAR_PROCESS_AUDIO_NAME` | exe name (`chrome.exe`, etc.) | unset (off) | Per-process loopback target. |
| `PULSAR_ADAPTIVE_BITRATE` | `on` / `off` | `on` | Disable the adaptive worker if you want a fully manual bitrate. |

Anything else you set on `opts.env` is passed through to the child
process unchanged (e.g. `OBS_LOG_LEVEL=DEBUG` for verbose libobs
logging).

## Lifecycle

```ts
const pulsar = await spawn();          // (1) boot + connect
// pulsar.client is connected and ready

// ... application work ...

await pulsar.shutdown();               // (2) clean shutdown
```

### Boot (`spawn()`)

1. Resolve the binary path (default = bundled, or `binariesPath`).
2. `child_process.spawn(exe, [], { cwd, env, stdio, windowsHide: true })`.
   - `cwd` is set to `<binariesPath>/bin/64bit` (mandatory — libobs
     resolves data paths relative to the working directory).
   - `windowsHide: true` is set even though `pulsar.exe` is built
     `/SUBSYSTEM:WINDOWS` (no console alloc). It costs nothing and
     makes intent explicit on older Windows / certain antivirus
     drivers.
3. Stream stdout / stderr into a line reader; forward to `onLog`.
4. Watch for `pulsar-headless: libobs <ver> ready, idling`.
5. Read `<cwd>/obs-websocket/config.json` to recover the seeded
   `server_port` + `server_password`.
6. Construct a `PulsarClient`, connect to `ws://127.0.0.1:<port>`
   with the recovered password, complete the v5 Identify handshake.
7. Resolve `{ client, child, port, libobsVersion, shutdown }`.

### Shutdown (`shutdown()`)

1. `client.disconnect()` — sends a clean WebSocket close frame.
2. If the child is still alive, `child.kill()` (SIGTERM-equivalent on
   Windows, which `pulsar-headless` translates to a graceful
   `obs_shutdown` via the console-control handler).
3. Wait up to 5 s for the child to exit.
4. If still alive, `child.kill("SIGKILL")` as a safety net.

The shutdown promise is cached — call it as many times as you like;
only the first call does work. Multiple subscribers all receive the
same eventual resolution.

### Crash recovery

`spawn()` does **not** auto-restart on child crash. Listen for
`pulsar.client.on("connectionClosed", ...)` and decide your policy:

```ts
let pulsar = await spawn();

pulsar.client.on("connectionClosed", async (e) => {
  if (shuttingDown) return;            // expected during your own shutdown
  console.error(`pulsar disconnect (code=${e.code}): respawning...`);
  await pulsar.shutdown().catch(() => {});
  pulsar = await spawn(/* same opts */);
  // Re-issue any state your app depends on (destinations, scenes, ...)
});
```

## Talking to the running pulsar

`pulsar.client` is a fully-typed `PulsarClient`. The full surface — six
namespaces (`destinations`, `video`, `adaptive`, `record`, `stream`,
plus the v5 baseline passthrough on `pulsar.client.obs`), typed events,
typed errors — is documented in the
[`@clodocapeo/pulsar-client` README](https://www.npmjs.com/package/@clodocapeo/pulsar-client).

This package **re-exports every symbol** from `pulsar-client`, so a
single import line gets you both the spawn API and the client types:

```ts
import {
  spawn,
  PulsarClient,                 // re-export
  PulsarVendorError,            // re-export
  PulsarNotConnectedError,      // re-export
  type Destination,             // re-export
  type CreateDestinationInput,  // re-export
  type AdaptiveState,           // re-export
  // ... and so on
} from "@clodocapeo/pulsar-bundle";
```

## Bundling for distribution

When you package your application (electron-builder, pkg, oxc-pack,
nexe, …), the bundled `pulsar.exe` + DLLs need to ship as
**unpacked resources** — `app.asar` and most other archive formats
break the relative-path lookups libobs uses to resolve its plugin
DLLs and effect files.

### electron-builder

```jsonc
// electron-builder.json (excerpt)
{
  "asar": true,
  "asarUnpack": [
    "node_modules/@clodocapeo/pulsar-bundle/binaries/**/*"
  ],
  "files": [
    "dist/**/*",
    "node_modules/@clodocapeo/pulsar-bundle/**/*"
  ]
}
```

Then in your Electron main process:

```ts
import { app } from "electron";
import { spawn } from "@clodocapeo/pulsar-bundle";
import { resolve } from "node:path";

const binariesPath = app.isPackaged
  ? resolve(process.resourcesPath, "app.asar.unpacked", "node_modules", "@clodocapeo", "pulsar-bundle", "binaries")
  : undefined;  // dev: use the postinstall'd binaries

const pulsar = await spawn({ binariesPath });
```

### pkg / nexe

These bundle a Node runtime + your code into a single executable. The
`pulsar.exe` payload **cannot** live inside that bundle — ship it
alongside as a sidecar resource:

```
my-app.exe                   # pkg-bundled Node + your code
resources/
└── pulsar/                  # extracted from pulsar-bundle's binaries/
    └── bin/64bit/pulsar.exe
    └── obs-plugins/64bit/...
    └── data/...
```

Then `spawn({ binariesPath: resolve(__dirname, "resources/pulsar") })`.

## CI / offline / mirror

Three env vars control postinstall behaviour:

| Var | Effect |
|---|---|
| `PULSAR_BUNDLE_SKIP_POSTINSTALL=1` | Skip the binary download entirely. Useful for `npm install` in a CI matrix that never spawns pulsar (lint-only, type-check-only jobs), or for offline builds with a vendored copy. |
| `PULSAR_BUNDLE_DOWNLOAD_URL=<url>` | Override the download URL. Use for an internal mirror, a private CDN, or a pre-signed S3 URL. The downloaded zip must be the matching `pulsar-windows-x64-v<VERSION>.zip` shape. |

```bash
# CI: install but skip the 40 MB download
PULSAR_BUNDLE_SKIP_POSTINSTALL=1 npm ci

# Internal mirror
PULSAR_BUNDLE_DOWNLOAD_URL=https://my-mirror.internal/pulsar/v1.0.0.zip npm install
```

If your CI installs on Linux / macOS to lint a cross-platform repo,
the `os: ["win32"]` field already prevents the postinstall from
running there. If you need to force-install on a non-target platform
anyway, pass `--force` to npm — the postinstall detects the platform
mismatch and exits cleanly.

## Troubleshooting

### `pulsar.exe did not signal ready within 30000ms`

Most common causes, in order:

1. **Antivirus quarantine.** A freshly-extracted `pulsar.exe` can
   trigger heuristics on Defender / corporate AV. Whitelist the
   `binaries/` directory or pre-extract before the first run.
2. **Wrong `cwd`** (only possible with manual `binariesPath`). The
   built-in `spawn()` always sets `cwd` correctly; if you reach
   inside `child` to reuse the binary, make sure you `cwd` to the
   `bin/64bit/` directory.
3. **Port conflict.** Stock OBS Studio with the obs-websocket plugin
   also defaults to 4455. Pin a free port via `PULSAR_PORT` or use
   the `pickFreePort()` pattern.
4. **Loaded system / cold cache.** Bump `readyTimeoutMs` to 60_000.

The `onLog` callback receives every boot line — capture them and look
at the last few lines before the timeout to see where pulsar got stuck.

### `pulsar.exe not found at <path>`

The postinstall didn't fetch the binary (network failure, unpublished
version). Re-run `npm install` with network access, or set
`binariesPath` to a local checkout
(`upstream/build_x64/rundir/RelWithDebInfo/`).

### `obs-websocket config not found at <path> (boot incomplete?)`

Pulsar reached the ready marker but didn't write its config.json.
Almost always means the obs-websocket plugin failed to load — check
the boot log for `Failed to load plugin obs-websocket.dll`. Usually a
missing dependency in the bundle (Qt6Core.dll absent, an antivirus
removed a DLL, etc.).

### Auth rejected on connect

The seeded password didn't match what obs-websocket persisted. Two
known causes:

1. A stale `obs-websocket/config.json` from a prior run — Pulsar
   rewrites it before plugin load, but on a corrupted filesystem you
   might see drift. Delete `binaries/bin/64bit/obs-websocket/config.json`
   and re-spawn.
2. You set `PULSAR_PASSWORD=""` (empty string) on `opts.env`. Pulsar
   treats empty as "generate a random password" and the bundle then
   reads what was generated — your `""` is ignored. Either pass a
   non-empty value or leave the var unset.

### `pulsar.exe exited prematurely (code=N, signal=...)`

Look at the boot log captured via `onLog`. The most common patterns:

- `code=-1073740940` (`0xC0000374`) — heap corruption. File a bug
  with the boot log.
- `code=3221225477` (`0xC0000005`) — access violation. Same — file a bug.
- `code=1` with `Failed to find file 'default.effect'` — `cwd` is
  wrong. Don't override `binariesPath` to point at a directory that
  doesn't have `bin/64bit/` + `data/` + `obs-plugins/64bit/`.

## Versioning

Tracks `pulsar-client` and `pulsar.exe` in lockstep. `1.0.0` of this
package downloads `pulsar-windows-x64-v1.0.0.zip` and depends on
`@clodocapeo/pulsar-client@1.0.0`.

The matching GitHub Release must exist for postinstall to succeed.
When upgrading, bump all three packages together — npm semver
resolution will reject mixed versions.

## Compatibility

| | |
|---|---|
| OS | Windows 10/11 x64 only |
| Node | ≥ 18 |
| Module system | ESM only (`"type": "module"`) |
| TypeScript | ≥ 5.0 — strict mode supported |
| Antivirus | Whitelist the `binaries/` directory if you see boot timeouts |

## Licence

[GPL-2.0-or-later](./LICENSE).

This package bundles `pulsar.exe` and its DLLs (libobs + Pulsar
plugins), all of which are GPL-2.0-or-later. The aggregate distributed
by this npm package is therefore covered by the GPL.

Source for the bundled binaries is available at
<https://github.com/ZabLaboratory/Pulsar> at the matching version tag.

### Bundling Pulsar in a non-GPL application

The process boundary keeps your application's licence under
**mere aggregation** — the GPL does not propagate. Four invariants must
be honoured:

1. **Process boundary.** Always spawn `pulsar.exe` as a separate OS
   process. Never `LoadLibrary` / `dlopen` it.
2. **WebSocket-only IPC.** No FFI, no shared memory, no native bindings.
3. **No FFI surface on Pulsar's side.** Don't add `__declspec(dllexport)`
   to any plugin. Pulsar's CI gates this.
4. **No source copy-paste.** Don't include lines copied from the libobs
   / obs-websocket / obs-browser source trees in your application.

Read [`LICENSE-INVARIANTS.md`](https://github.com/ZabLaboratory/Pulsar/blob/main/LICENSE-INVARIANTS.md)
on the Pulsar repo for the full contract, then
[`CONSUMER-AUDIT.md`](https://github.com/ZabLaboratory/Pulsar/blob/main/CONSUMER-AUDIT.md)
for the empirical checklist your application's CI must enforce.

If you only need the typed client without any GPL binary (e.g. you
talk to a Pulsar already running elsewhere), use
[`@clodocapeo/pulsar-client`](https://www.npmjs.com/package/@clodocapeo/pulsar-client)
instead — it's MIT.
