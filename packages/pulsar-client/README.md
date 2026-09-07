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
  - [`capabilities` namespace](#capabilities-namespace)
  - [Deterministic scene switching](#deterministic-scene-switching)
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
under an authorized supervisor and want to talk to it. Pulsar binds loopback
by default; remote/native-container deployment is not enabled by installing
this client.

Typical scenarios:

- A browser tool / Electron renderer that drives a Pulsar instance
  spawned by another process.
- A CLI utility that probes broadcast state, mutates bitrate, or
  toggles destinations.
- A test harness that talks to a fake / mocked v5 server.
- Anything that doesn't need the native postinstall download from the
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

This connects to an **already running, authenticated** runtime. The host supplies
the actual per-session URL/password; the client does not start Pulsar.

```js
import { PulsarClient } from "@clodocapeo/pulsar-client";

const url = process.env.PULSAR_WS_URL;
const password = process.env.PULSAR_WS_PASSWORD;
if (!url || !password) throw new Error("Set this session's URL and password");

const pulsar = new PulsarClient();
try {
  await pulsar.connect({ url, password });
  console.log(await pulsar.video.get());
  console.log(await pulsar.capabilities.get());
} finally {
  await pulsar.disconnect();
}
```

`PULSAR_WS_URL` and `PULSAR_WS_PASSWORD` here are host application variables,
not native bootstrap option names. Native hosts obtain credentials from READY;
the bundle uses its child's idle marker and private config. Never read another
session's config or log the password.

For an owned runtime, use a bundle's `spawn()`; the returned client is already
connected. Disconnecting a client does not stop the native process or its outputs.

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
  readonly capabilities:  CapabilitiesNamespace;

  // Lifecycle
  connect(opts?: ConnectOptions): Promise<void>;
  disconnect(): Promise<void>;
  isConnected(): boolean;

  // Typed events — see "Events" below
  on<E extends PulsarEventName>(event: E, listener: (e: PulsarEventMap[E]) => void): this;
  off<E extends PulsarEventName>(event: E, listener: (e: PulsarEventMap[E]) => void): this;

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
   *  or supplied by the owning bundle's private runtime configuration. */
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

Low-level escape hatch for the fixed **`pulsar`** vendor. It does not select
`pulsar-scene` or `pulsar-scene-switch`; use `obs.call("CallVendorRequest", ... )`
with an explicit vendor for those namespaces.

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
  kind: "rtmp_custom" | "vod_local" | "twitch" | "youtube";
  url: string;             // server-pinned for twitch/youtube
  enabled: boolean;        // last user intent
  active: boolean;         // obs_output_active(d.output)
}
```

```ts
interface CreateDestinationInput {
  kind: "rtmp_custom" | "vod_local" | "twitch" | "youtube";
  name?: string;           // defaults to the generated id
  url?: string;            // RTMP URL (rtmp_custom) or file path (vod_local)
                           // ignored for twitch/youtube (server pins the ingest)
  key?: string;            // required for remote kinds; unused for vod_local
}
```

#### Examples

```ts
// Twitch: server pins its TLS ingest URL
const twitch = await pulsar.destinations.create({
  name: "Twitch",
  kind: "twitch",
  key: process.env.TWITCH_KEY!,
});
console.log(twitch.url);
// → rtmps://ingest.global-contribute.live-video.net/app/

// Custom RTMP: e.g. a co-host's private ingest
const rtmp = await pulsar.destinations.create({
  name: "Co-host",
  kind: "rtmp_custom",
  url: "rtmps://my.private.cdn/live",
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

The path is auto-resolved to `<recordDir>/pulsar-<YYYYMMDD-HHMMSS>.<ext>`
by the server. MP4 is default; `PULSAR_RECORD_CONTAINER=mkv` selects MKV at
boot. `recordDir` defaults to the private runtime's recordings directory;
use `PULSAR_RECORD_DIR` for persistent storage.

```ts
class RecordNamespace {
  start(): Promise<void>;
  stop(timeoutMs?: number): Promise<string>;   // resolves with the actual output path
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

> Configure a service before starting the singleton output. Current Pulsar
> verifies effective output state and reports declined/no-effect attempts;
> the old “success with no stream” behavior is not the current contract.
> Use v5 `SetStreamServiceSettings` for a supported service, **or**
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
uses v5 `Input*` requests for input control and the `pulsar` vendor for
monitoring-device and common Program-route readback.

```ts
class AudioNamespace {
  specialInputs(): Promise<SpecialInputs>;              // mic1..mic4 slot names
  listInputs(): Promise<AudioInput[]>;
  isMuted(inputName: string): Promise<boolean>;
  setMuted(inputName: string, muted: boolean): Promise<void>;
  toggleMuted(inputName: string): Promise<boolean>;      // returns new state
  listDevices(inputName: string): Promise<AudioDevice[]>; // wasapi device_id list
  setDevice(inputName: string, deviceId: string): Promise<void>;
  listMonitoringDevices(): Promise<MonitoringDeviceList>;
  setMonitoringDevice(deviceId: string): Promise<MonitoringDevice>;
  programRoute(): Promise<ProgramAudioRoute>;              // common r2 Program route + PTS evidence
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

Mute changes from any connected control client broadcast as the
typed `inputMuteStateChanged` event — see "Events" below.

`programRoute()` reads the explicit `program-common` / `ProgramAudio` route.
Its `audioIdentity` and output read-back fields are stable across video Cuts;
its `sources` entries are audio-capable source channels (channel 0, the mutable
Program video root, is excluded), and `tracks[].pts` reports actual
encoder-fed audio PTS. r2 explicitly reports
`previewAudioSupported=false` and `afvSupported=false`: Preview audio/AFV is
not inferred from the selected video scene.

### `capabilities` namespace

```ts
const capabilities = await pulsar.capabilities.get();
```

The response is a typed `PulsarCapabilities` manifest. It describes the running
engine's encoder families, audio, input/filter/transition inventories,
graphics adapters, output scales and mutation regimes. Presence is not
permission to mutate every setting and is not proof that a selected device
works. Read back the actual runtime state after a write.

Monitoring-device controls should be offered only when the manifest reports
the selectable capability. `audio.listInputs()` currently maps the baseline
input list; it does not itself filter every non-audio kind. Validate input
capability before presenting it as an audio device.

### Deterministic scene switching

There is no typed `sceneSwitch` namespace in 3.0.0. Use the explicit vendor:

```js
const state = await pulsar.obs.call("CallVendorRequest", {
  vendorName: "pulsar-scene-switch",
  requestType: "GetState",
  requestData: {},
});
console.log(state.responseData);
```

Prepare/Take/Abort require the complete
[scene-switch v1 envelope](../../scripts/contracts/scene_switch_v1/README.md).
Observe PreviewReady and TakeCommitted separately from request acceptance,
respect revisions and retain command IDs for idempotent retries.
The runtime's 4096-outcome cache refuses new IDs at capacity rather than
evicting known outcomes. Raw `VendorEvent` listeners must filter vendor and
event type before interpreting a payload.

### v5 baseline passthrough

Use `pulsar.obs.call(...)` for baseline v5 requests. Availability and headless
refusals are defined by the running server and Pulsar's protocol, not by the
client having a method name:

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
| `prismLog` | `PulsarPrismLogEvent`: structured severity/domain/source/code/message/context/details. |

`OutputState = "STARTING" | "STARTED" | "STOPPING" | "STOPPED" | "PAUSED" | "RESUMED" | "RECONNECTING" | "RECONNECTED"`

## Errors

Three exported custom error classes plus underlying obs-websocket-js / OS
errors. The runtime class is also used by the bundle launcher.

```ts
import { PulsarNotConnectedError, PulsarVendorError } from "@clodocapeo/pulsar-client";
```

| Class | When |
|---|---|
| `PulsarRuntimeError` | Structured process/bootstrap error from the bundle, with stable code/envelope. |
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

Public types are exported from the package root; the selection below is not
an exhaustive list. [src/index.ts](src/index.ts) is the complete export surface:

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

A host should distinguish intentional shutdown from unexpected disconnection,
serialize reconnect attempts and use bounded backoff. A lost WebSocket does
not prove the native runtime died: it may still be on air. Reconnect and inspect
state before deciding to replace the process.

For a real restart, finalize outputs where possible, retain files, stop only
the owned runtime, attach handlers to the replacement client and reconstruct
the intended state. The client does not automatically restore destinations,
scenes, pending Takes or command-cache history. Avoid an unguarded respawn
handler that restarts again when its own shutdown closes the connection.

## Wire format

Pulsar's vendor handlers serialize `obs_data_t` fields with the
**snake_case** names the libobs C API expects. This client maps to
**camelCase** at its boundary (`src/wire.ts`). The mapping is the only
place to touch when the server adds a field.

If you call `pulsar.callVendor(...)` directly, you see snake_case in
both the request and response payloads.

## Versioning

Tracks Pulsar's [`VERSION` file](https://github.com/ZabLaboratory/Pulsar/blob/main/VERSION)
in lockstep — this package's `3.0.0` matches Pulsar `3.0.0`.

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

The client can connect to stock OBS's v5 server for supported baseline calls.
Pulsar-specific vendors are absent there, and unknown-vendor failures may be
underlying protocol errors rather than a vendor response with an `error` field.
Do not promise identical headless/runtime behavior across servers.

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
is distributed separately under GPL-2.0-or-later. Host distribution must
respect the project's documented constraints and component notices; this
README is not a blanket legal determination about an arbitrary integration.

If you bundle `pulsar.exe` alongside this client (via
`@clodocapeo/pulsar-bundle` or your own packaging), read
[`LICENSE-INVARIANTS.md`](https://github.com/ZabLaboratory/Pulsar/blob/main/LICENSE-INVARIANTS.md)
on the Pulsar repo first — there are four non-negotiable invariants
your application must evaluate and honour when distributing the runtime.
