# Embedding Pulsar 3.0.0

This guide applies to Prism and any application that distributes or supervises
Pulsar. It describes the implemented host boundary and lifecycle; it does not
assert that this Pulsar release has already been deployed into any consumer.

See the [architecture](ARCHITECTURE.md) for the internal graph,
[protocol](PROTOCOL.md) for wire fields and
[bundle API](../packages/pulsar-bundle/README.md) for the reference Node launcher.

## 1. Distribution contract

The native runtime is GPL-2.0-or-later; the TypeScript WebSocket client is MIT.
Follow [LICENSE-INVARIANTS.md](../LICENSE-INVARIANTS.md) and
[CONSUMER-AUDIT.md](../CONSUMER-AUDIT.md), preserve notices and provide the
matching source information when distributing the runtime. These are project
distribution requirements, not a universal legal conclusion about a host.

The host's application API is WebSocket. It must not load libobs, Pulsar
plugins or CEF into its own address space as a Pulsar FFI. The native runtime's
internal module calls and private media helpers are not a host extension API.
Ordinary DirectShow video samples are a separate media boundary.

## 2. Choose and verify a runtime

| Distribution | Contents |
|---|---|
| `pulsar-windows-x64-v3.0.0.zip` | Headless runtime, capture, WASAPI, encoders, outputs and Pulsar control components. |
| `pulsar-windows-x64-full-v3.0.0.zip` | Light features plus browser/CEF, native text/VLC modules and the gated nv-filters module. |

Game capture belongs to both variants. NDI is not a standard-bundle promise.
VLC/effect-module presence does not prove the external runtime/SDK and host
capabilities are available. NVIDIA SDK DLLs/models are not bundled.

Preserve the extracted layout:

```text
<pulsar-root>/
  README.txt
  bin/64bit/
    pulsar.exe
    obs.dll
    Qt/runtime/codec DLLs and platform resources
  obs-plugins/64bit/
    obs-websocket.dll
    pulsar-multi-stream.dll
    pulsar-scene-source.dll
    pulsar-browser.dll          (full)
    pulsar-browser-page.exe     (full; next to CEF)
    libcef.dll                  (full)
    other packaged native modules/helpers
  data/
    libobs/
    obs-plugins/
```

Do not flatten this tree, ship only `pulsar.exe`, put native files inside
`app.asar`, or mix binaries from different revisions. The upstream
`obs-browser.dll` must not coexist as a second browser-source loader.

For a release integration:

1. Read the exact tag and matching npm versions.
2. Download the intended ZIP and `prism-pulsar-runtime-manifest.json`.
3. Match the full ZIP's SHA-256 to the release manifest, and inspect the
   archive's version/layout. A hash inside a manifest must be obtained through
   your trusted release-distribution path; it is not independent provenance
   by itself.
4. Stage the verified directory as unpacked application resources.
5. Start that artifact and read its reported version/capabilities.
6. Run the consumer's integration gates against those bytes.

A lockfile bump alone does not prove the installed executable changed.
The bundle downloader can warn and soft-fail; explicitly verify binary
availability before declaring an upgrade complete.

## 3. Preferred Node/Electron launch

```js
import { spawn } from "@clodocapeo/pulsar-bundle-full";

const pulsar = await spawn({
  binariesPath: "C:/MyApp/resources/pulsar",
  readyTimeoutMs: 60_000,
  env: {
    PULSAR_RUNTIME_DIR: "C:/MyApp/state/session-unique-id",
    PULSAR_RECORD_DIR: "C:/MyApp/recordings",
  },
  onPrismLog: (event) => {
    // Send to your bounded application logger.
    console.log(event.code, event.message);
  },
});

try {
  console.log(pulsar.runtimeInstanceId, pulsar.port);
  console.log(await pulsar.client.capabilities.get());
  // Run application work; finalize any active outputs before leaving.
} finally {
  await pulsar.shutdown();
}
```

The paths are examples. Allocate a unique runtime directory per active child
and apply your application's retention policy. In Electron, derive the
unpacked binary location from `process.resourcesPath`.

The handle exposes `client`, `child`, `port`, `libobsVersion`,
`runtimeInstanceId`, `runtimeDir` and idempotent `shutdown()`.
The bundle's `onLog` and `onPrismLog` redact credentials from public log
lines. Reading `child.stdout` directly bypasses those redactions.

### Runtime directory ownership

Without an explicit directory, the bundle creates a temporary private runtime
under the selected runtime root/system temp directory and attempts to remove
it after shutdown or failure. Default recordings/logs inside it are temporary.

An explicit `PULSAR_RUNTIME_DIR` is application-owned and not deleted by the
bundle. Use `PULSAR_RECORD_DIR` and `PULSAR_LOG_DIR` for durable outputs,
or export files before cleanup. Do not reuse one directory for live children.

The executable remains under the bundle's `bin/64bit/`; only process state
uses the private cwd. Moving cwd back beside the executable defeats session
isolation and is not the fix for missing resources.

### Port and identity

The bundle ignores an inherited fixed port as a default for another child;
only a per-spawn explicit nonzero `PULSAR_PORT` pins it. Otherwise it asks
the OS for a free loopback port. The later native bind can still race, so the
authenticated connection is the final readiness check.

A runtime ID follows `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. Native identity and
physical-directory leases reject collisions before configuration or media
resources are shared. Runtime identity differs from the log session ID.

## 4. Native/manual hosts

Launch the absolute `bin/64bit/pulsar.exe` path with piped stdout/stderr,
a private runtime directory, validated identity and an appropriate per-child
port. Configuration is through the documented environment, not an invented
`--service --port --config` command-line interface.

The binary uses the Windows GUI subsystem without an OBS UI.
`windowsHide: true` is still a good launcher convention; it is not evidence
that the executable is a console-subsystem program.

The boot output includes:

```text
PULSAR_SESSION <session-id>
PULSAR_READY ws=ws://127.0.0.1:<port> password=<password>
pulsar-headless: libobs <version> ready, idling (Ctrl+C to exit)
```

A manual host can parse the stable READY line, keep its password private and
complete v5 Hello/Identify. Reject startup timeout/exit and reap the owned
child. Do not forward an unredacted READY line to telemetry.

The Node bundle uses the idle marker and then reads the protected
`<runtimeDir>/obs-websocket/config.json` seeded by that same child.
Do not read a stale config in the binary directory or another session.
An empty/unset password requests a generated password, not disabled auth.

The default bind is IPv4 loopback. Do not promise an IPv6 listener unless
the runtime configuration actually selects it. Any deliberate non-loopback
deployment requires separate transport/exposure review; this guide does not
turn a local embedding contract into a supported public service.

## 5. Application state and control

Maintain one authenticated client while production work is active.
A transient WebSocket disconnect does not itself terminate the native process
or clear its outputs. Check its state before deciding to restart; restarting
an on-air engine is an application policy decision.

The runtime does not provide application-level persistence of scene authoring
or destination configuration across process restart. Reconcile the actual
state and restore the intended configuration deliberately.

For dual-lane switching, use `GetState`, Prepare → PreviewReady → Take →
TakeCommitted with matching revisions and command IDs. Do not infer a
completed switch from TakeAccepted. Keep the 4096-outcome session bound in
your restart/recovery design.

## 6. Shutdown and crash recovery

Before terminating a session:

1. Stop/save replay as needed while its source encoders are still active.
2. Stop and finalize singleton recording, stream and registry destinations.
   Await the corresponding effective terminal states and output paths.
3. Copy/retain logs and media that must survive temporary-directory cleanup.
4. Disconnect and stop the native process using the chosen launcher.
5. Verify exit before removing application-owned session state.

Closing a WebSocket is **not** a process-shutdown command. On Windows,
Node's child termination does not prove a graceful console-control callback
or MP4 finalization. The current bundle disconnects, terminates, waits with a
five-second bounded fallback, and attempts generated-directory cleanup.

The native lifecycle harness additionally supports an explicitly inherited
anonymous shutdown event for redirected-stdio execution. Its implementation
and proof live in the native shutdown test/probe and bootstrap. That control
is not currently exposed as a public bundle option; do not claim the bundle
uses it.

If a child/helper remains, identify the exact owned process tree and investigate.
Never kill all `pulsar-browser-page.exe` processes by image name: another
active runtime may own them. Do not promise the current bundle has a
`taskkill /T` descendant cleanup path; its implementation uses child.kill.

## 7. DirectShow compatibility and production returns

ProgramReturn and PreviewReturn provide video-only CPU NV12 samples.
They do not imply Preview audio or audio-follow-video.

The historical camera aliases have one compatibility lease. Use:

- `PULSAR_LEGACY_ALIAS=required` only when this process must own that lease;
- `PULSAR_LEGACY_ALIAS=dedicated` or `off` to avoid claiming it;
- the default for opportunistic legacy compatibility.

A non-holder's DirectShow reader must use the same
`PULSAR_RUNTIME_INSTANCE_ID` and `PULSAR_DIRECTSHOW_LEGACY_ALIAS=0`.
Legacy names are selected only when both namespace variables are absent.
A malformed/empty identity or alias without a valid identity refuses mapping
access rather than silently attaching to another session.

The D3D11 option remains an internal private-helper transport. External
readers do not receive writable control maps or GPU capabilities.
See [transport](runbooks/d3d11-return-transport.md) and
[lease watcher](runbooks/directshow-lease-watcher.md) before changing a reader.

## 8. Consumer acceptance checklist

- Exact runtime/package/tag selected; archive hash and deployed bytes checked.
- Native assets unpacked with one browser loader and complete resource tree.
- Unique runtime directory/identity and authenticated listener observed.
- Capability manifest captured from the deployed runtime.
- Actual sources rendered/captured, not merely created successfully.
- Prepare/Take terminal events and common Program-audio behavior checked.
- Output starts/stops, recording finalization and owned-process exit checked.
- Hardware skips, unsupported Preview audio and capacity limits retained.
- Consumer build/test/deployment proof recorded separately from this release.

The complete [documentation index](README.md) links the operational and
historical material without treating an old incident's commands as current
deployment policy.
