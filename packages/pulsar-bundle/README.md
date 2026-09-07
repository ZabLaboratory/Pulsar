# @clodocapeo/pulsar-bundle

[![npm](https://img.shields.io/npm/v/%40clodocapeo%2Fpulsar-bundle)](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle)

The **light Windows x64 runtime bundle** and Node launcher for Pulsar 3.0.0.
It downloads the matching native ZIP and returns a connected
`PulsarClient` through `spawn()`. The package re-exports the client API.

Use [pulsar-bundle-full](../pulsar-bundle-full/README.md) for CEF/browser,
native text and VLC modules. Game/window/display capture belong to both
variants. Use [pulsar-client](../pulsar-client/README.md) when another process
already owns the runtime.

## Install

```powershell
npm install @clodocapeo/pulsar-bundle@3.0.0
```

Native support is Windows x64; Node 18+ and ESM are required.
A direct required dependency on an unsupported platform can produce npm
`EBADPLATFORM`; `os`/`cpu` fields are not a universal silent-skip promise.
Cross-platform hosts can use an optional dependency or deliberately install
tooling with `--force` without attempting native execution.

Postinstall downloads
`pulsar-windows-x64-v3.0.0.zip` from the matching GitHub release and extracts
it under `binaries/`. A `.version-stamp` avoids a repeat download for the
same version. The stamp is a cache marker, not a cryptographic integrity proof.

Network/HTTP failure can **soft-fail with a warning**. npm success does not
prove a binary was installed. `spawn()` reports a missing executable through
`PulsarRuntimeError`. Extraction/filesystem failures can still fail installation.

The downloader can remove its old binaries before fetching a changed version.
Do not use a release-preparation install as the only copy of a working native
runtime. Use `PULSAR_BUNDLE_SKIP_POSTINSTALL=1` for source-only work.

## Distribution contents

| Feature | Light | Full |
|---|---|---|
| Headless engine, patched libobs, WebSocket and production controller | Yes | Yes |
| Window/display/game/DirectShow capture modules | Yes, subject to build/device capability | Yes, same caveat |
| WASAPI, H.264 encoder families, recording/replay/RTMP | Yes | Yes |
| Hot Preview/Program lanes and video-only returns | Yes | Yes |
| Browser/CEF | No | Yes |
| Native text / VLC module | No | Yes |
| Gated nv-filters module | No | Yes; SDK/models not bundled |

Consult the actual release assets for sizes, not historical estimates.
Third-party module presence is not proof of hardware/SDK/runtime availability.
NDI, arbitrary OBS plugins and Preview audio/AFV are not bundle promises.

## First run

Save as `hello-pulsar.mjs`:

```js
import { spawn } from "@clodocapeo/pulsar-bundle";

const pulsar = await spawn({ readyTimeoutMs: 60_000 });
try {
  console.log({
    runtimeInstanceId: pulsar.runtimeInstanceId,
    runtimeDir: pulsar.runtimeDir,
    port: pulsar.port,
    video: await pulsar.client.video.get(),
  });
} finally {
  await pulsar.shutdown();
}
```

```powershell
node hello-pulsar.mjs
```

This example does not start a broadcast or recording. In a production
application, finalize outputs and retain needed files before shutdown.

## Public API

```ts
interface SpawnOptions {
  binariesPath?: string;
  env?: Record<string, string>;
  readyTimeoutMs?: number;
  onLog?: (stream: "stdout" | "stderr", line: string) => void;
  onPrismLog?: (event: PulsarPrismLogEvent) => void;
}

interface SpawnedPulsar {
  client: PulsarClient;
  child: ChildProcess;
  port: number;
  libobsVersion: string;
  runtimeInstanceId: string;
  runtimeDir: string;
  shutdown(): Promise<void>;
}

function spawn(options?: SpawnOptions): Promise<SpawnedPulsar>;
```

`launchCommand` in the source type is an internal fake-child test hook,
not a normal alternative-runtime API.

### Binary location

`binariesPath` is the root containing `bin/64bit/pulsar.exe`, not the
executable path or `bin/64bit` directory. Default: this package's `binaries/`.

For local source work:

```js
const pulsar = await spawn({
  binariesPath: "D:/path/to/Pulsar/upstream/build_x64/rundir/RelWithDebInfo",
});
```

The executable and modules remain there, but cwd/config/logs/default
recordings use a private session directory. Preserve the complete resource tree.

`PULSAR_BUNDLE_FULL_BINARIES_PATH` belongs to the capture test harness.
It is **not** a generic `spawn()` option or override; normal hosts pass
`binariesPath`.

### Environment and runtime ownership

The child inherits the process environment plus per-call `env`, with
explicit runtime identity/directory and per-child port behavior.

| Variable | Behavior |
|---|---|
| `PULSAR_RUNTIME_INSTANCE_ID` | Per-call ID, or generated `node-...`; validated against `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. |
| `PULSAR_RUNTIME_DIR` | Explicit directory is caller-owned; otherwise a private temporary directory is generated. |
| `PULSAR_RUNTIME_ROOT` | Parent for generated directories; default system temp. |
| `PULSAR_PORT` | Per-call nonzero value pins the port; omitted/empty/0 allocates a free loopback port. An inherited fixed port is not reused as the default. |
| `PULSAR_PASSWORD` | Optional per-session password; absent/empty asks native bootstrap to generate one. |
| `PULSAR_RECORD_DIR` | Persistent destination for recorder/replay; default is under the runtime directory. |
| `PULSAR_LOG_DIR` | Explicit log destination; otherwise runtime logs are session-local. |
| `PULSAR_LEGACY_ALIAS` | `required`, `dedicated`, `off`, or default opportunistic DirectShow compatibility policy. |

Generated directories are removed on exit/shutdown/failure on a best-effort
basis. Files retained by antivirus/CEF may delay cleanup. Explicit directories
are never removed by this helper. Export media/logs before generated-directory
cleanup, or choose persistent destinations from the start.

Native startup independently rejects runtime-ID and physical-directory
collisions. Distinct path spellings do not authorize concurrent use of the
same directory.

### Media configuration

The [protocol environment reference](../../docs/PROTOCOL.md#environment-variables-pulsar_)
is authoritative. Common settings include:

- resolution/fps and encoder family at boot;
- video bitrate live, audio bitrate only while affected encoders are idle;
- optional capture-window descriptor;
- opt-in microphone/process audio, audio track count and output track lists;
- MP4/MKV record container and replay time/memory bounds;
- optional transitions and explicitly experimental performance switches.

The native engine defaults to 1080p60 x264, 6000 kbps video and 160 kbps AAC.
Unset microphone ID means no microphone source. Preview audio/AFV is unsupported.
An unavailable hardware encoder falls back with a warning; read the result.

## Readiness and credentials

The helper:

1. Resolves/creates the per-child namespace and binary path.
2. Spawns with private cwd, piped stdio and `windowsHide: true`.
3. Waits for the native `pulsar-headless: libobs <version> ready, idling`
   marker (default timeout 30 seconds).
4. Reads the seeded `<runtimeDir>/obs-websocket/config.json`.
5. Validates the port and connects the client with the seeded password.
6. Returns only after authenticated v5 connection.

Manual hosts can instead parse the separate `PULSAR_READY` sentinel.
The bundle currently reads config after the idle marker; it does not use the
sentinel password parser. Never read another session's config or persist raw
READY output.

`onLog` receives bounded redacted text. `onPrismLog` receives structured
runtime/client observations. Raw access to `child.stdout` bypasses this
redaction boundary. Readiness does not prove a source has rendered or an output
is live.

## Using the client

All [client namespaces](../pulsar-client/README.md) are available:
destinations, capabilities, video, adaptive, record, stream, audio and raw
v5 passthrough. For example, with an existing handle:

```js
const state = await pulsar.client.obs.call("CallVendorRequest", {
  vendorName: "pulsar-scene-switch",
  requestType: "GetState",
  requestData: {},
});
console.log(state.responseData);
```

Use the complete scene-switch envelope and revision rules for Prepare/Take.
Do not invent top-level v5 Take requests or assume a typed scene-switch
namespace exists in this SDK.

## Lifecycle and restart policy

`shutdown()` is idempotent and shares one promise. It disconnects the client,
terminates the owned child, waits with a five-second force fallback, and
attempts generated-directory cleanup.

**On Windows, child termination is not proof of graceful libobs shutdown.**
Stop/save replay, finalize recording/stream/destinations and retain files
first. Closing only the WebSocket does not stop the native engine.
The native anonymous-event shutdown harness is a different control path,
not currently a public bundle option.

The helper does not automatically restart, reconstruct scenes or persist
destinations. A connection drop can leave the engine on air; inspect its state
before replacing it. A host restart loop must distinguish intentional shutdown,
bound retries/backoff, attach handlers to each new client and restore only
the intended state. Avoid an unguarded recursive respawn handler.

The helper does not promise process-tree `taskkill`. If a helper remains,
identify the exact owned process; never kill all CEF helper processes by name
when several runtimes may be active.

## DirectShow readers

One runtime can own the legacy aliases. Others use dedicated mappings.
A dedicated reader uses the same `PULSAR_RUNTIME_INSTANCE_ID` and
`PULSAR_DIRECTSHOW_LEGACY_ALIAS=0`. Invalid/ambiguous namespace selection
refuses rather than selecting another session.

The public Program/Preview samples remain CPU NV12 even with the optional
private D3D11 helper. See [embedding](../../docs/PRISM-EMBEDDING.md).

## Electron and offline distribution

Keep native binaries unpacked. A typical electron-builder excerpt is:

```json
{
  "asar": true,
  "asarUnpack": [
    "node_modules/@clodocapeo/pulsar-bundle/binaries/**/*"
  ]
}
```

Resolve the installed path explicitly:

```ts
import { app } from "electron";
import { resolve } from "node:path";
import { spawn } from "@clodocapeo/pulsar-bundle";

const binariesPath = app.isPackaged
  ? resolve(process.resourcesPath, "app.asar.unpacked", "node_modules",
      "@clodocapeo", "pulsar-bundle", "binaries")
  : undefined;
const pulsar = await spawn({ binariesPath });
```

Verify the final installer actually includes the dependency/native tree.
For single-file Node packagers, ship Pulsar as an unpacked sidecar and pass
that location; it cannot execute from an embedded virtual filesystem.

## Download controls

| Variable | Effect |
|---|---|
| `PULSAR_BUNDLE_SKIP_POSTINSTALL=1` | Skip native download for source-only/offline tooling. |
| `PULSAR_BUNDLE_DOWNLOAD_URL` | Replace the release URL; supply a trusted matching light ZIP. |

```powershell
$env:PULSAR_BUNDLE_SKIP_POSTINSTALL = "1"
npm ci
```

The same download override name is read by **both** bundle packages.
Do not install light and full together under one variant-specific mirror URL.
Choose an explicit matching payload or separate installation environments.

For controlled distribution, verify the release manifest/hash externally;
postinstall's version stamp is not that verification.

## Errors and troubleshooting

| Signal | Meaning / next check |
|---|---|
| `PULSAR_BINARY_UNAVAILABLE` | Missing executable: check download warning and binary root. |
| `PULSAR_RUNTIME_ID_INVALID` | Invalid per-call identity before launch. |
| `PULSAR_READY_TIMEOUT` | Read the first native boot failure, namespace collision, listener or module error. |
| `PULSAR_CONFIG_MISSING` / `PULSAR_CONFIG_INVALID` | Idle marker did not yield usable per-session config. |
| `PULSAR_PROCESS_ERROR` / `PULSAR_PROCESS_EXITED` | Process launch/exit observation; inspect bounded native logs. |
| Authentication/connect failure | Check listener/config/version consistency; empty password requests generation, not no-auth mode. |
| Missing `default.effect` | Check executable-relative data layout and matching native stack, not a shared cwd workaround. |

If a file is quarantined, verify its release provenance and follow local
security policy; do not blanket-disable protection. A timeout alone does not
establish antivirus as the cause.

## Versioning and license

3.0.0 downloads the v3.0.0 light ZIP and pins pulsar-client 3.0.0.
Keep all deployed runtime/client/bundle artifacts matched. npm can install
multiple versions; it does not universally reject a mixed runtime setup.

The bundle/native runtime is [GPL-2.0-or-later](LICENSE); the re-exported
client retains its MIT license. Preserve component notices and matching source
links. Read the [distribution constraints](../../LICENSE-INVARIANTS.md) and
[consumer audit](../../CONSUMER-AUDIT.md); process separation is not a blanket
legal guarantee for arbitrary host integrations.
