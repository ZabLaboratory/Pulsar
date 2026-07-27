# @clodocapeo/pulsar-client

[![npm](https://img.shields.io/npm/v/%40clodocapeo%2Fpulsar-client?logo=npm&color=cb3837)](https://www.npmjs.com/package/@clodocapeo/pulsar-client)
[![Licence MIT](https://img.shields.io/badge/licence-MIT-blue)](./LICENSE)
[![Node ≥ 18](https://img.shields.io/badge/node-%E2%89%A518-339933)](https://nodejs.org)

Typed TypeScript client for [Pulsar](https://github.com/ZabLaboratory/Pulsar),
the headless broadcast engine forked from OBS Studio. Speaks
**obs-websocket v5** + Pulsar's `pulsar:*` vendor namespace.

This package is the **client side only** — it wraps obs-websocket-js,
adds typed namespaces over the vendor extensions, and exposes a
strongly-typed event surface. It does **not** ship the `pulsar.exe`
binary; for that, install [`@clodocapeo/pulsar-bundle`](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle)
(or [`@clodocapeo/pulsar-bundle-full`](https://www.npmjs.com/package/@clodocapeo/pulsar-bundle-full)
for browser sources / CEF / VLC support), which re-exports this entire
client surface and adds a `spawn()` API.

## Table of contents

- [When to use it](#when-to-use-it)
- [Install](#install)
- [Quick start](#quick-start)
- [API reference](#api-reference)
  - [`PulsarClient`](#pulsarclient)
  - [`destinations` namespace](#destinations-namespace)
  - [`video` namespace](#video-namespace)
  - [`adaptive` namespace](#adaptive-namespace)
  - [`record` namespace](#record-namespace)
  - [`stream` namespace](#stream-namespace)
  - [`audio` namespace](#audio-namespace)
  - [v5 baseline passthrough](#v5-baseline-passthrough)
- [Events](#events)
- [Errors](#errors)
- [Types](#types)
- [Reconnect strategy](#reconnect-strategy)
- [Wire format](#wire-format)
- [Versioning](#versioning)
- [Compatibility](#compatibility)
- [Development](#development)
- [Licence](#licence)

## When to use it

Use this package when you already have a Pulsar (or any obs-websocket
v5 server, including OBS Studio with the obs-websocket plugin) running
somewhere — locally, on another machine over LAN, in a container —
and just want to talk to it.

Typical scenarios:

- A browser tool / Electron renderer that drives a Pulsar instance
  spawned by another process.
- A CLI utility that probes broadcast state, mutates bitrate, or
  toggles destinations.
- A test harness that talks to a fake / mocked v5 server.
- Anything that doesn't want the ~40 MB postinstall download from the
  `pulsar-bundle*` packages.

If you want Node to spawn pulsar.exe and own its lifecycle, use
`@clodocapeo/pulsar-bundle` instead — it depends on this package and
re-exports everything below, plus a `spawn()` API.

## Install

```bash
npm install @clodocapeo/pulsar-client
```

ESM-only, Node ≥ 18. Tree-shakeable. No native dependencies (the
underlying `obs-websocket-js` uses the platform `WebSocket` /
`ws` package).

## Quick start

```ts
import { PulsarClient } from "@clodocapeo/pulsar-client";

const pulsar = new PulsarClient();

await pulsar.connect({
  url: "ws://127.0.0.1:4455",
  password: process.env.PULSAR_WS_PASSWORD,
});

// 1. Multi-destination: encode once, fan out to N outputs.
const twitch = await pulsar.destinations.create({
  name: "Main",
  kind: "twitch",
  key: process.env.TWITCH_KEY!,
});
await pulsar.destinations.start(twitch.id);

// 2. React to bitrate adaptation events.
pulsar.on("bitrateAdjusted", (e) => {
  console.log(`bitrate -> ${e.bitrate} kbps (${e.reason}, drop_ratio=${e.dropRatio.toFixed(4)})`);
});

// 3. Mutate the encoder live.
await pulsar.video.setBitrate(4500);

// 4. Singleton local recording (independent of destinations).
await pulsar.record.start();
await new Promise((r) => setTimeout(r, 5_000));
const path = await pulsar.record.stop();
console.log(`recorded to ${path}`);

// 5. v5 baseline passthrough — anything obs-websocket v5 supports.
const ver = await pulsar.obs.call("GetVersion");
console.log(`obs ${ver.obsVersion}, ws ${ver.obsWebSocketVersion}`);

await pulsar.disconnect();
```

The credentials come from the `PULSAR_READY` sentinel that pulsar.exe
prints on stdout at boot — see
[`docs/PRISM-EMBEDDING.md`](https://github.com/ZabLaboratory/Pulsar/blob/main/docs/PRISM-EMBEDDING.md)
for the full handshake. If you used `@clodocapeo/pulsar-bundle`'s
`spawn()`, the bundle has already parsed those for you.

## API reference

### `PulsarClient`

```ts
class PulsarClient extends TypedEventEmitter {
  // Public namespaces
  readonly obs:           OBSWebSocket;             // raw obs-websocket-js
  readonly destinations:  DestinationsNamespace;
  readonly video:         VideoNamespace;
  readonly adaptive:      AdaptiveNamespace;
  readonly record:        RecordNamespace;
  readonly stream:        StreamNamespace;
  readonly audio:         AudioNamespace;

  // Lifecycle
  connect(opts?: ConnectOptions): Promise<void>;
  disconnect(): Promise<void>;
  isConnected(): boolean;

  // Typed events — see "Events" below
  on(event: PulsarEventName, listener: (e: PulsarEventMap[E]) => void): this;
  off(event: PulsarEventName, listener: (e: PulsarEventMap[E]) => void): this;

  // Escape hatch for vendor calls not yet typed
  callVendor<TReq, TRes extends { error?: string }>(
    requestType: string,
    requestData?: TReq,
  ): Promise<TRes>;
}
```

#### `connect(options?)`

```ts
interface ConnectOptions {
  /** Defaults to "ws://127.0.0.1:4455". */
  url?: string;
  /** obs-websocket auth password. Read from PULSAR_READY sentinel
   *  or from <pulsar-bin>/obs-websocket/config.json (server_password). */
  password?: string;
  /** Bitmask of obs-websocket EventSubscription flags. Defaults to
   *  0x7FF (all baseline event categories). */
  eventSubscriptions?: number;
}
```

Throws on auth failure / network error / already-connected. Use the
underlying `obs-websocket-js` exception types if you need to branch
on the failure mode.

#### `disconnect()`

Idempotent. Resolves once the WebSocket close frame has landed.

#### `isConnected()`

Snapshot. Updates on `connect()` resolve and on the
`ConnectionClosed` v5 event.

#### `callVendor(requestType, requestData?)`

Low-level escape hatch — use this when a new `pulsar:*` request lands
upstream but a typed wrapper hasn't been added to this package yet.

```ts
const resp = await pulsar.callVendor<{ id: string }, { started?: boolean }>(
  "StartDestination",
  { id: "abcd-1234" },
);
console.log(resp.started); // true
```

Throws `PulsarNotConnectedError` if disconnected, `PulsarVendorError`
if the server returns a non-empty `error` field, and lets the
underlying obs-websocket-js exceptions bubble up otherwise.

### `destinations` namespace

Multi-destination first-class. One encoder pair fans out to N outputs.
Each destination is identified by a server-generated UUID.

```ts
class DestinationsNamespace {
  list(): Promise<Destination[]>;
  create(input: CreateDestinationInput): Promise<Destination>;
  remove(id: string): Promise<boolean>;
  start(id: string): Promise<boolean>;
  stop(id: string): Promise<boolean>;
  startAll(): Promise<void>;
  stopAll(): Promise<void>;
}
```

```ts
interface Destination {
  id: string;
  name: string;
  kind: "rtmp_custom" | "vod_local" | "twitch";
  url: string;             // server-pinned for twitch
  enabled: boolean;        // last user intent
  active: boolean;         // obs_output_active(d.output)
}
```

```ts
interface CreateDestinationInput {
  kind: "rtmp_custom" | "vod_local" | "twitch";
  name?: string;           // defaults to the generated id
  url?: string;            // RTMP URL (rtmp_custom) or file path (vod_local)
                           // ignored for twitch (server picks the ingest)
  key?: string;            // required for rtmp_custom + twitch, unused for vod_local
}
```

#### Examples

```ts
// Twitch: server pins the closest ingest URL
const twitch = await pulsar.destinations.create({
  name: "Twitch",
  kind: "twitch",
  key: process.env.TWITCH_KEY!,
});
console.log(twitch.url);
// → rtmp://<region>.contribute.live-video.net/app/

// Custom RTMP: e.g. a co-host's private ingest
const rtmp = await pulsar.destinations.create({
  name: "Co-host",
  kind: "rtmp_custom",
  url: "rtmp://my.private.cdn/live",
  key: "stream-key",
});

// Local MP4 archive (concurrent with streaming)
const archive = await pulsar.destinations.create({
  name: "VOD",
  kind: "vod_local",
  url: "C:/recordings/my-stream.mp4",
});

// Start all three with one call (encode once, fan out)
await pulsar.destinations.startAll();

// Or one at a time — start() returns false if RTMP refused
const started = await pulsar.destinations.start(twitch.id);
if (!started) console.error("Twitch refused the connection");

// Always remove on shutdown — the server gracefully stops first
await pulsar.destinations.remove(twitch.id);
await pulsar.destinations.remove(rtmp.id);
await pulsar.destinations.remove(archive.id);
```

### `video` namespace

Snapshot + live-mutate the encoder configuration.

```ts
class VideoNamespace {
  get(): Promise<VideoSettings>;
  set(patch: VideoSettingsPatch): Promise<VideoSettingsPatchResult>;
  setBitrate(videoKbps: number): Promise<VideoSettingsPatchResult>; // shortcut
}
```

```ts
interface VideoSettings {
  fps: number;
  width: number;
  height: number;
  videoBitrate: number;        // kbps
  videoRateControl: string;    // e.g. "CBR"
  videoKeyintSec: number;
  audioBitrate: number;        // kbps
}

interface VideoSettingsPatch {
  videoBitrate?: number;
  audioBitrate?: number;
}

interface VideoSettingsPatchResult {
  changed: boolean;
  videoBitrate?: number;
  audioBitrate?: number;
}
```

**Limitations**

- `fps`, `width`, `height` are **not** mutable at runtime — they
  require a libobs `obs_reset_video()` which would interrupt every
  active output. Set them via `PULSAR_FPS` and `PULSAR_RESOLUTION`
  env vars at spawn instead. Trying to set them via this API returns
  a typed `PulsarVendorError`.
- `audioBitrate` is only applied while the audio encoder is idle (no
  active stream / record). The server returns `changed: false` if it
  could not apply the value.

### `adaptive` namespace

Snapshot + control the adaptive bitrate worker. The worker samples
`obs_output_get_frames_dropped` every 2 s, scales bitrate within
`[floor, target]`, and emits `pulsar:BitrateAdjusted` events.

```ts
class AdaptiveNamespace {
  getState(): Promise<AdaptiveState>;
  setEnabled(enabled: boolean): Promise<boolean>;
  enable(): Promise<boolean>;   // shortcut
  disable(): Promise<boolean>;  // shortcut
}
```

```ts
interface AdaptiveState {
  enabled: boolean;
  targetKbps: number;        // bitrate the loop tries to maintain
  currentKbps: number;       // encoder's currently configured bitrate
  floorKbps: number;         // 30% of target by default; never drops below
  stableTicks: number;       // ticks without drops since last adjust
  adjustmentsTotal: number;  // cumulative adjustments since boot
  lastDeltaTotal: number;    // frames produced last sample window
  lastDeltaDropped: number;  // frames dropped last sample window
  lastDropRatio: number;     // lastDeltaDropped / max(1, lastDeltaTotal)
}
```

Disabling pauses the sampling loop; the encoder bitrate stays at
whatever value the worker last applied. Re-enabling resets
`stableTicks` to 0 so the loop re-warms before any climb attempt.

### `record` namespace

Wraps the **legacy frontend-stub recording output** — the singleton,
env-driven recorder pulsar-frontend-stub creates at boot. Distinct
from the multi-destination API where `vod_local` destinations are
also MP4 files but client-named.

The path is auto-resolved to `<recordDir>/pulsar-<YYYYMMDD-HHMMSS>.mp4`
by the server. `recordDir` defaults to `<cwd>/recordings/`; override
at spawn via `PULSAR_RECORD_DIR`.

```ts
class RecordNamespace {
  start(): Promise<void>;
  stop(timeoutMs?: number): Promise<string>;   // resolves with the .mp4 path
  pause(): Promise<void>;
  resume(): Promise<void>;
  isActive(): Promise<boolean>;
}
```

`stop()` waits for the `RecordStateChanged` event reporting
`STOPPED` (with `outputPath`) before resolving. Default timeout is
10 s — adjust if your trailer-write needs longer (high-bitrate, slow
disk).

### `stream` namespace

Wraps the **legacy frontend-stub streaming output** — the singleton
`PulsarStream` rtmp_output that obeys the v5 `StartStream` /
`StopStream` baseline. Stream Deck, Companion, and Streamer.bot all
use this path.

```ts
class StreamNamespace {
  start(): Promise<void>;
  stop(): Promise<void>;
  isActive(): Promise<boolean>;
}
```

> ⚠️ **`StartStream` succeeds on the wire even when no destination URL
> is configured** — the underlying `obs_output_start` declines
> silently. To actually go live through this surface, configure a
> service via the v5 `SetStreamServiceSettings` request first, **or**
> use `pulsar.destinations.create({ kind: "twitch", … })` +
> `pulsar.destinations.start(id)` instead. The multi-destination API
> is the recommended path.
>
> ⛔ **Twitch is refused on this surface.**
> `SetStreamServiceSettings` with `rtmp_common` + `service: "Twitch"`
> answers `InvalidRequestField` (400): that service resolves its ingest
> from a list downloaded at runtime and falls back to the **cleartext**
> `rtmp://live.twitch.tv/app` when the list is absent, which would send
> the stream key unencrypted. Use
> `pulsar.destinations.create({ kind: "twitch", … })` — its `rtmps://`
> ingest is pinned at compile time. The same request also requires an
> `rtmp://`/`rtmps://` server and a non-empty key.

### `audio` namespace

Mic / audio-input control. **Stream-level, not scene-level**: mute
state and device selection live on the OBS input itself, so they
survive scene switches for free — no vendor plugin involved, this
wraps the native obs-websocket v5 `Input*` requests directly.

```ts
class AudioNamespace {
  specialInputs(): Promise<SpecialInputs>;              // mic1..mic4 slot names
  listInputs(): Promise<AudioInput[]>;
  isMuted(inputName: string): Promise<boolean>;
  setMuted(inputName: string, muted: boolean): Promise<void>;
  toggleMuted(inputName: string): Promise<boolean>;      // returns new state
  listDevices(inputName: string): Promise<AudioDevice[]>; // wasapi device_id list
  setDevice(inputName: string, deviceId: string): Promise<void>;
}
```

```ts
// Resolve the mic slot, then flip mute + pick a device from the cockpit
const { mic1 } = await pulsar.audio.specialInputs();
if (mic1) {
  await pulsar.audio.setMuted(mic1, true);

  const devices = await pulsar.audio.listDevices(mic1);
  const usb = devices.find((d) => d.name.includes("USB"));
  if (usb) await pulsar.audio.setDevice(mic1, usb.id);
}
```

Mute changes (from any client, including the OBS UI) broadcast as the
typed `inputMuteStateChanged` event — see "Events" below.

### v5 baseline passthrough

Anything obs-websocket v5 supports is reachable via `pulsar.obs.call(...)`:

```ts
// Scene CRUD
await pulsar.obs.call("CreateScene", { sceneName: "Live" });
const scenes = await pulsar.obs.call("GetSceneList");

// Source / input CRUD
await pulsar.obs.call("CreateInput", {
  sceneName: "Live",
  inputName: "Webcam",
  inputKind: "dshow_input",
  inputSettings: { device_id: "..." },
});

// Audio
await pulsar.obs.call("SetInputVolume", {
  inputName: "Mic/Aux",
  inputVolumeDb: -3,
});

// Filters
await pulsar.obs.call("CreateSourceFilter", {
  sourceName: "Webcam",
  filterName: "Color Correction",
  filterKind: "color_filter_v2",
});

// Stats
const stats = await pulsar.obs.call("GetStats");
console.log(stats.cpuUsage, stats.activeFps, stats.outputSkippedFrames);
```

The full v5 reference: <https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md>

## Events

The client emits typed events translated from both the v5 baseline and
the `pulsar:*` vendor namespace.

```ts
pulsar.on("bitrateAdjusted", (e) => {
  // Pulsar vendor event — the adaptive worker just changed bitrate.
  console.log(e.bitrate, e.target, e.floor, e.reason, e.dropRatio);
});

pulsar.on("recordStateChanged", (e) => {
  // v5 baseline event.
  // e.state ∈ STARTING / STARTED / STOPPING / STOPPED / PAUSED / RESUMED / RECONNECTING / RECONNECTED
  if (e.state === "STOPPED" && e.outputPath) console.log("recorded:", e.outputPath);
});

pulsar.on("streamStateChanged", (e) => {
  console.log(e.state);
});

pulsar.on("studioModeStateChanged", (e) => {
  console.log(e.enabled);
});

pulsar.on("inputMuteStateChanged", (e) => {
  // v5 baseline event -- fires for any input, not just the mic; filter by e.inputName.
  console.log(e.inputName, e.inputMuted);
});

pulsar.on("connectionClosed", (e) => {
  console.log(`connection closed: code=${e.code} reason=${e.reason}`);
});
```

For events not yet wrapped by this client, listen on the underlying
obs-websocket-js client directly: `pulsar.obs.on("InputCreated", …)`.

### Event payload shapes

| Event | Payload |
|---|---|
| `bitrateAdjusted` | `{ bitrate: number, target: number, floor: number, reason: "drops" \| "recovery", dropRatio: number }` |
| `recordStateChanged` | `{ state: OutputState, outputPath?: string }` |
| `streamStateChanged` | `{ state: OutputState }` |
| `studioModeStateChanged` | `{ enabled: boolean }` |
| `inputMuteStateChanged` | `{ inputName: string, inputMuted: boolean }` |
| `connectionClosed` | `{ code: number, reason: string }` |

`OutputState = "STARTING" | "STARTED" | "STOPPING" | "STOPPED" | "PAUSED" | "RESUMED" | "RECONNECTING" | "RECONNECTED"`

## Errors

Two custom error classes plus whatever obs-websocket-js / the OS
network stack throws.

```ts
import { PulsarNotConnectedError, PulsarVendorError } from "@clodocapeo/pulsar-client";
```

| Class | When |
|---|---|
| `PulsarNotConnectedError` | Method called before `connect()` resolves. |
| `PulsarVendorError` | Server returned a typed `error` field on a vendor request (validation failure, unsupported kind, etc.). Carries `requestType` + `message`. |

```ts
try {
  await pulsar.destinations.create({
    kind: "twitch",
    key: "",      // empty key is rejected server-side
  });
} catch (err) {
  if (err instanceof PulsarVendorError) {
    console.error(`vendor request "${err.requestType}" failed: ${err.message}`);
  } else {
    throw err;
  }
}
```

Other failures (network drop, auth rejection, malformed payload) bubble
up as the underlying `obs-websocket-js` exceptions — see its
[error reference](https://github.com/obs-websocket-community-projects/obs-websocket-js#errors).

## Types

Every public type is exported from the package root:

```ts
import type {
  AdaptiveState,
  AudioDevice,
  AudioInput,
  BitrateAdjustedEvent,
  ConnectOptions,
  CreateDestinationInput,
  Destination,
  DestinationKind,
  InputMuteStateChangedEvent,
  OutputState,
  PulsarEventMap,
  PulsarEventName,
  RecordStateChangedEvent,
  SpecialInputs,
  StreamStateChangedEvent,
  StudioModeStateChangedEvent,
  VideoSettings,
  VideoSettingsPatch,
  VideoSettingsPatchResult,
} from "@clodocapeo/pulsar-client";
```

## Reconnect strategy

This client does **not** auto-reconnect. The `connectionClosed` event
fires on every disconnect (clean or abrupt); your application decides
whether to reconnect, with what delay, and with what backoff.

A reasonable pattern when bundling pulsar-bundle:

```ts
import { spawn } from "@clodocapeo/pulsar-bundle";

let pulsar = await spawn();
pulsar.client.on("connectionClosed", async (e) => {
  console.warn(`pulsar disconnect (code=${e.code}): respawning...`);
  await pulsar.shutdown().catch(() => {});
  pulsar = await spawn();
  // Re-issue any state your app depends on (destinations, scenes, ...).
});
```

If you're talking to an external Pulsar / OBS instance instead, just
re-call `connect()` with exponential backoff on `connectionClosed`.

## Wire format

Pulsar's vendor handlers serialize `obs_data_t` fields with the
**snake_case** names the libobs C API expects. This client maps to
**camelCase** at its boundary (`src/wire.ts`). The mapping is the only
place to touch when the server adds a field.

If you call `pulsar.callVendor(...)` directly, you see snake_case in
both the request and response payloads.

## Versioning

Tracks Pulsar's [`VERSION` file](https://github.com/ZabLaboratory/Pulsar/blob/main/VERSION)
in lockstep — `1.0.0` of this package matches `pulsar.exe` `1.0.0`.

- **Patch** — bug fixes, no surface change.
- **Minor** — new typed wrapper over an additive `pulsar:*` request
  or event. Existing code keeps compiling.
- **Major** — rename / removal of an existing `pulsar:*` surface, or
  a breaking change in the embedding contract.

## Compatibility

| | |
|---|---|
| Node | ≥ 18 |
| Module system | ESM only (`"type": "module"`) |
| Browser | Yes (with a bundler that resolves `obs-websocket-js`'s WebSocket import) |
| TypeScript | ≥ 5.0 — strict mode supported |
| obs-websocket | v5 (handles the v5.0–v5.7 wire format range) |

The same client talks to **OBS Studio with the obs-websocket plugin**
just as well as to Pulsar — every v5 baseline call works, only the
`pulsar:*` namespace is missing on stock OBS (vendor calls return an
unknown-vendor error, which surfaces as `PulsarVendorError`).

## Development

```bash
git clone https://github.com/ZabLaboratory/Pulsar
cd Pulsar/packages/pulsar-client
npm install
npm test            # vitest with mocked obs-websocket server
npm test -- --watch
npm run build       # tsc -> dist/
npm run lint        # tsc --noEmit
```

The test suite uses vitest with a mock obs-websocket server — no
real `pulsar.exe` needed. The fixtures live under `tests/`.

## Licence

[MIT](./LICENSE).

This wrapper contains no libobs code and links nothing GPL — it speaks
obs-websocket v5 over a WebSocket. The `pulsar.exe` engine it talks to
is distributed separately under GPL-2.0-or-later, but the **process
boundary** keeps the licences disjoint (mere aggregation, not derivative
work).

If you bundle `pulsar.exe` alongside this client (via
`@clodocapeo/pulsar-bundle` or your own packaging), read
[`LICENSE-INVARIANTS.md`](https://github.com/ZabLaboratory/Pulsar/blob/main/LICENSE-INVARIANTS.md)
on the Pulsar repo first — there are four non-negotiable invariants
your application must honour to keep its own licence under the
process boundary.
