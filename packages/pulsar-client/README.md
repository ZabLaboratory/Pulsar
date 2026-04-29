# @clodocapeo/pulsar-client

Typed TypeScript client for [Pulsar](https://github.com/ZabLaboratory/Pulsar), the headless broadcast engine bundled in Prism.

This package is the **client side only** — it speaks the obs-websocket v5 protocol plus Pulsar's `pulsar:*` vendor namespace, but does not embed `pulsar.exe`. To launch Pulsar from Node use [`@clodocapeo/pulsar-bundle`](../pulsar-bundle) (Phase 13b).

## Install

```bash
npm install @clodocapeo/pulsar-client
```

## Usage

```ts
import { PulsarClient } from "@clodocapeo/pulsar-client";

const pulsar = new PulsarClient();
await pulsar.connect({
  url: "ws://127.0.0.1:4455",
  password: process.env.PULSAR_WS_PASSWORD,
});

// --- Multi-stream destinations ----------------------------------------
const twitch = await pulsar.destinations.create({
  name: "Main",
  kind: "twitch",
  key: process.env.TWITCH_KEY!,
});
await pulsar.destinations.start(twitch.id);

const vod = await pulsar.destinations.create({
  kind: "vod_local",
  url: "C:/recordings/my-stream.mp4",
});
await pulsar.destinations.start(vod.id);

// --- Live bitrate mutation --------------------------------------------
await pulsar.video.setBitrate(4500); // kbps

// --- Adaptive bitrate worker ------------------------------------------
const state = await pulsar.adaptive.getState();
console.log(`bitrate ${state.currentKbps}/${state.targetKbps}`);

pulsar.on("bitrateAdjusted", (e) => {
  console.log(`bitrate -> ${e.bitrate} kbps (${e.reason}, ratio=${e.dropRatio.toFixed(4)})`);
});

// --- Legacy frontend-stub record (singleton MP4) ----------------------
await pulsar.record.start();
await new Promise((r) => setTimeout(r, 3000));
const path = await pulsar.record.stop(); // resolves with the .mp4 path

// --- v5 baseline passthrough ------------------------------------------
const ver = await pulsar.obs.call("GetVersion");
console.log(ver.obsVersion);

await pulsar.disconnect();
```

## Surface

### Namespaces

| Namespace | Vendor requests it wraps |
|---|---|
| `pulsar.destinations` | `GetDestinations`, `CreateDestination`, `RemoveDestination`, `StartDestination`, `StopDestination`, `StartAllDestinations`, `StopAllDestinations` |
| `pulsar.video` | `GetVideoSettings`, `SetVideoSettings` |
| `pulsar.adaptive` | `GetAdaptiveState`, `SetAdaptiveEnabled` |
| `pulsar.record` | v5 `StartRecord` / `StopRecord` / `PauseRecord` / `ResumeRecord` / `GetRecordStatus` (no vendor extension) |
| `pulsar.stream` | v5 `StartStream` / `StopStream` / `GetStreamStatus` (no vendor extension) |

### Events

```ts
pulsar.on("bitrateAdjusted", (e) => { /* { bitrate, target, floor, reason, dropRatio } */ });
pulsar.on("recordStateChanged", (e) => { /* { state, outputPath? } */ });
pulsar.on("streamStateChanged", (e) => { /* { state } */ });
pulsar.on("studioModeStateChanged", (e) => { /* { enabled } */ });
pulsar.on("connectionClosed", (e) => { /* { code, reason } */ });
```

### Errors

- `PulsarVendorError` — server returned a typed `error` field on a vendor request (validation failure, unsupported kind, etc.).
- `PulsarNotConnectedError` — method called before `connect()`.

Other errors (network failure, auth rejection) bubble up from the underlying `obs-websocket-js` exceptions.

### Wire format

Pulsar's vendor handlers use snake_case on the wire; this client maps to camelCase in the public surface. The mapping is in `src/wire.ts` and is the only place to touch when the server adds a field.

## Versioning

This package's version tracks Pulsar's `VERSION` file. `0.1.0` matches `pulsar.exe` v0.1.0.

A new `pulsar:*` request or event is a **minor** bump. Removing or changing the shape of an existing one is a **major** bump.

## Development

```bash
cd packages/pulsar-client
npm install
npm test       # vitest with mock obs-websocket server
npm run build  # tsc -> dist/
```

## License

MIT. See [`LICENSE`](./LICENSE).

This wrapper contains no libobs code and links nothing GPL — it speaks
obs-websocket v5 over a WebSocket. The `pulsar.exe` engine it talks to is
distributed separately under GPL-2.0-or-later, but the process boundary
keeps the licences disjoint (mere aggregation, not derivative work).
