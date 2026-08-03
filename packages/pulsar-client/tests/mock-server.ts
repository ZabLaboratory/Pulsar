// Mock obs-websocket v5 server with a stub of Pulsar's vendor namespace.
//
// Stays intentionally lo-fi: no SHA256 auth challenge (clients connect
// without a password), no event subscription filtering, no RPC version
// negotiation beyond rpcVersion=1. Just enough wire shape that the real
// obs-websocket-js client treats us as a real server.
//
// Vendor state lives in-memory and is reset per server instance, so
// each test gets a clean slate.

import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { decode as msgpackDecode, encode as msgpackEncode } from "@msgpack/msgpack";
import { WebSocketServer, type WebSocket } from "ws";

type Json = Record<string, unknown>;

interface VendorDest {
  id: string;
  name: string;
  kind: string;
  url: string;
  enabled: boolean;
  active: boolean;
}

interface VideoState {
  fps: number;
  width: number;
  height: number;
  video_bitrate: number;
  video_rate_control: string;
  video_keyint_sec: number;
  audio_bitrate: number;
  video_encoder: string;
  video_preset: string;
  video_profile: string;
}

interface AdaptiveState {
  enabled: boolean;
  target_kbps: number;
  current_kbps: number;
  floor_kbps: number;
  stable_ticks: number;
  adjustments_total: number;
  last_delta_total: number;
  last_delta_dropped: number;
  last_drop_ratio: number;
}

interface MockInput {
  name: string;
  kind: string;
  muted: boolean;
  settings: Json;
}

export class MockObsWebSocket {
  readonly httpServer: Server;
  readonly wss: WebSocketServer;
  readonly destinations = new Map<string, VendorDest>();
  readonly clients = new Set<WebSocket>();
  readonly inputs = new Map<string, MockInput>([
    ["Mic/Aux", { name: "Mic/Aux", kind: "wasapi_input_capture", muted: false, settings: { device_id: "default" } }],
  ]);

  video: VideoState = {
    fps: 60,
    width: 1920,
    height: 1080,
    video_bitrate: 6000,
    video_rate_control: "CBR",
    video_keyint_sec: 2,
    audio_bitrate: 160,
    video_encoder: "x264",
    video_preset: "veryfast",
    video_profile: "high",
  };

  /** Encoder families the mock advertises via GetCapabilities. */
  capabilityEncoders: string[] = ["x264", "nvenc"];

  adaptive: AdaptiveState = {
    enabled: true,
    target_kbps: 6000,
    current_kbps: 6000,
    floor_kbps: 1800,
    stable_ticks: 0,
    adjustments_total: 0,
    last_delta_total: 0,
    last_delta_dropped: 0,
    last_drop_ratio: 0,
  };

  /** Playback devices the mock machine enumerates (#173). "default" first,
   *  the way libobs-plus-Pulsar answers: the OS-default follower is a real
   *  choice, not a placeholder. */
  readonly monitoringDevices: Array<{ id: string; name: string }> = [
    { id: "default", name: "Default" },
    { id: "{0.0.0.0}.{abcd}", name: "Headphones (Realtek)" },
    { id: "{0.0.0.0}.{ef01}", name: "Studio Monitors (Focusrite)" },
  ];
  monitoringDeviceId = "default";
  monitoringDeviceName = "Default";

  /** Hooks tests can swap in to override responses. */
  vendorOverride?: (requestType: string, requestData: Json) => Json | undefined;

  private nextId = 1;

  /** Async factory. Drives an explicit http.createServer().listen() so the
   *  port assignment is predictable across platforms (the bare
   *  WebSocketServer({port:0}) variant has been flaky on Windows). */
  static async create(): Promise<MockObsWebSocket> {
    const m = new MockObsWebSocket();
    await new Promise<void>((resolve, reject) => {
      m.httpServer.once("error", reject);
      m.httpServer.listen(0, "127.0.0.1", () => resolve());
    });
    return m;
  }

  private constructor() {
    this.httpServer = createServer();
    this.wss = new WebSocketServer({
      server: this.httpServer,
      // obs-websocket-js sends Sec-WebSocket-Protocol: obswebsocket.json
      // (or .msgpack) and refuses to send Identify if the server
      // doesn't echo a known subprotocol back. Default ws behaviour is
      // to ignore the header entirely, which leaves the client hanging
      // on Hello forever.
      handleProtocols: (protocols) => {
        const list = Array.from(protocols as Iterable<string>);
        // Both encodings carry the same v5 message shape -- pick what
        // the client offered, prefer msgpack (smaller wire). The
        // selection per-connection is tracked via the subprotocol the
        // ws.WebSocket instance reports back in handleConnection.
        if (list.includes("obswebsocket.msgpack")) return "obswebsocket.msgpack";
        if (list.includes("obswebsocket.json")) return "obswebsocket.json";
        return false;
      },
    });
    this.wss.on("connection", (ws) => this.handleConnection(ws));
  }

  get url(): string {
    const addr = this.httpServer.address() as AddressInfo | null;
    if (!addr) throw new Error("MockObsWebSocket not listening yet");
    return `ws://127.0.0.1:${addr.port}`;
  }

  async close(): Promise<void> {
    for (const c of this.clients) c.terminate();
    await new Promise<void>((resolve) => this.wss.close(() => resolve()));
    await new Promise<void>((resolve) => this.httpServer.close(() => resolve()));
  }

  emitVendorEvent(eventType: string, eventData: Json): void {
    const frame = {
      op: 5,
      d: {
        eventType: "VendorEvent",
        eventIntent: 1,
        eventData: {
          vendorName: "pulsar",
          eventType,
          eventData,
        },
      },
    };
    for (const c of this.clients) {
      if (c.readyState === c.OPEN) sendFrame(c, frame);
    }
  }

  /** Emit a baseline (non-vendor) obs-websocket v5 event, e.g. InputMuteStateChanged. */
  emitEvent(eventType: string, eventData: Json): void {
    const frame = { op: 5, d: { eventType, eventIntent: 1, eventData } };
    for (const c of this.clients) {
      if (c.readyState === c.OPEN) sendFrame(c, frame);
    }
  }

  private handleConnection(ws: WebSocket): void {
    this.clients.add(ws);

    // Send Hello in the encoding negotiated during the WS handshake.
    sendFrame(ws, {
      op: 0,
      d: { obsWebSocketVersion: "5.7.3", rpcVersion: 1 },
    });

    ws.on("message", (raw, isBinary) => {
      const frame = decodeFrame(raw, isBinary);
      if (!frame) return;
      if (frame.op === 1) {
        sendFrame(ws, { op: 2, d: { negotiatedRpcVersion: 1 } });
      } else if (frame.op === 6) {
        const requestType = frame.d["requestType"] as string;
        const requestId = frame.d["requestId"] as string;
        const requestData = (frame.d["requestData"] ?? {}) as Json;
        const responseData = this.handleRequest(requestType, requestData);
        sendFrame(ws, {
          op: 7,
          d: {
            requestType,
            requestId,
            requestStatus: { result: true, code: 100 },
            responseData,
          },
        });
      }
    });

    ws.on("close", () => this.clients.delete(ws));
  }

  private handleRequest(requestType: string, requestData: Json): Json {
    const inputResponse = this.handleInputRequest(requestType, requestData);
    if (inputResponse !== undefined) return inputResponse;

    if (requestType !== "CallVendorRequest") {
      // We don't simulate the v5 baseline here; tests that need
      // baseline calls (StartRecord, etc.) install their own routing.
      return {};
    }

    const vendorReq = requestData["requestType"] as string;
    const vendorData = (requestData["requestData"] ?? {}) as Json;

    if (this.vendorOverride) {
      const override = this.vendorOverride(vendorReq, vendorData);
      if (override !== undefined) return { responseData: override };
    }

    return { responseData: this.handleVendor(vendorReq, vendorData) };
  }

  /** Native (non-vendor) obs-websocket v5 Input* requests used by AudioNamespace. Returns undefined for anything else. */
  private handleInputRequest(requestType: string, data: Json): Json | undefined {
    switch (requestType) {
      case "GetSpecialInputs":
        return { mic1: "Mic/Aux" };

      case "GetInputList":
        return { inputs: Array.from(this.inputs.values()).map((i) => ({ inputName: i.name, inputKind: i.kind })) };

      case "GetInputMute": {
        const input = this.inputs.get(data["inputName"] as string);
        return { inputMuted: input?.muted ?? false };
      }

      case "SetInputMute": {
        const input = this.inputs.get(data["inputName"] as string);
        if (input) input.muted = data["inputMuted"] as boolean;
        return {};
      }

      case "ToggleInputMute": {
        const input = this.inputs.get(data["inputName"] as string);
        if (input) input.muted = !input.muted;
        return { inputMuted: input?.muted ?? false };
      }

      case "GetInputPropertiesListPropertyItems": {
        if (data["propertyName"] !== "device_id") return { propertyItems: [] };
        return {
          propertyItems: [
            { itemName: "Default", itemValue: "default", itemEnabled: true },
            { itemName: "USB Mic", itemValue: "usb-mic-1", itemEnabled: true },
          ],
        };
      }

      case "SetInputSettings": {
        const input = this.inputs.get(data["inputName"] as string);
        if (input) Object.assign(input.settings, data["inputSettings"] as Json);
        return {};
      }

      default:
        return undefined;
    }
  }

  private handleVendor(requestType: string, data: Json): Json {
    switch (requestType) {
      case "GetDestinations":
        return { destinations: Array.from(this.destinations.values()) };

      case "CreateDestination": {
        const kind = data["kind"] as string | undefined;
        if (
          kind !== "rtmp_custom" &&
          kind !== "vod_local" &&
          kind !== "twitch" &&
          kind !== "youtube"
        ) {
          return {
            error: "kind must be 'rtmp_custom', 'vod_local', 'twitch', or 'youtube'",
          };
        }
        const id = `mock-${this.nextId++}`;
        const url = (data["url"] as string | undefined) ?? "";
        // Mirrors pinned_ingest_url() in plugin-main.cpp: for a named platform
        // the caller's url is never read, the server's own ingest is stored.
        const pinned: Record<string, string> = {
          twitch: "rtmps://ingest.global-contribute.live-video.net/app/",
          youtube: "rtmps://a.rtmps.youtube.com:443/live2",
        };
        const dest: VendorDest = {
          id,
          name: (data["name"] as string | undefined) ?? id,
          kind,
          url: pinned[kind] ?? url,
          enabled: false,
          active: false,
        };
        this.destinations.set(id, dest);
        return { id };
      }

      case "RemoveDestination": {
        const id = data["id"] as string | undefined;
        if (!id) return { removed: false, error: "id required" };
        return { removed: this.destinations.delete(id) };
      }

      case "StartDestination": {
        const id = data["id"] as string | undefined;
        if (!id) return { started: false, error: "id required" };
        const d = this.destinations.get(id);
        if (!d) return { started: false, error: "no such destination" };
        d.enabled = true;
        d.active = true;
        return { started: true };
      }

      case "StopDestination": {
        const id = data["id"] as string | undefined;
        if (!id) return { stopped: false };
        const d = this.destinations.get(id);
        if (!d) return { stopped: false };
        d.enabled = false;
        d.active = false;
        return { stopped: true };
      }

      case "StartAllDestinations":
        for (const d of this.destinations.values()) {
          d.enabled = true;
          d.active = true;
        }
        return { ok: true };

      case "StopAllDestinations":
        for (const d of this.destinations.values()) {
          d.enabled = false;
          d.active = false;
        }
        return { ok: true };

      case "GetVideoSettings":
        return { ...this.video };

      case "GetCapabilities": {
        const audioLadder = [64, 96, 128, 160, 192, 224, 256, 320];
        return {
          version: 1,
          encoders: this.capabilityEncoders.map((value) => ({ value })),
          active_encoder: this.video.video_encoder,
          video_bitrate: { min: 200, max: 50000 },
          audio_bitrate: audioLadder.map((value) => ({ value })),
          // ADR 027 §3.2 (#141): values + regime, side by side.
          capabilities: {
            encoders: {
              applicability: "boot-fixed",
              values: this.capabilityEncoders.map((value) => ({ value })),
            },
            active_encoder: { applicability: "boot-fixed", value: this.video.video_encoder },
            video_bitrate: { applicability: "live", min: 200, max: 50000, step: 50 },
            audio_bitrate: {
              applicability: "live",
              min: 64,
              max: 320,
              step: 32,
              values: audioLadder.map((value) => ({ value })),
            },
            // ADR 027 §3.3 blocks 3 + 4 (#144): presence-only inventories and
            // the effective colorimetry. No filter property bound appears here
            // -- that is the point of the block.
            filters: {
              applicability: "live",
              values: [{ value: "color_filter_v2" }, { value: "noise_suppress_filter_v2" }],
            },
            source_kinds: {
              applicability: "live",
              values: [{ value: "dshow_input" }, { value: "window_capture" }],
            },
            destination_kinds: {
              applicability: "live",
              values: [
                { value: "rtmp_custom" },
                { value: "vod_local" },
                { value: "twitch" },
                { value: "youtube" },
              ],
            },
            video_colorimetry: {
              applicability: "read-only",
              value: "709",
              range: "Partial",
              format: "NV12",
            },
            // ADR 027 §3.3 bloc 1 (#142): per-family detail, boot-fixed. Only
            // the families this "build" enumerates appear, and nvenc omits the
            // keyint window to stand for a value libobs did not advertise.
            encoder_families: {
              applicability: "boot-fixed",
              values: this.capabilityEncoders.map((family) =>
                family === "x264"
                  ? {
                      value: "x264",
                      presets: ["ultrafast", "veryfast", "medium"].map((value) => ({ value })),
                      profiles: ["baseline", "main", "high"].map((value) => ({ value })),
                      rate_controls: ["CBR", "VBR"].map((value) => ({ value })),
                      keyint_sec: { min: 0, max: 20, step: 1 },
                      bitrate: { min: 200, max: 50000, step: 50 },
                    }
                  : {
                      value: family,
                      presets: ["p1", "p5", "p7"].map((value) => ({ value })),
                      profiles: ["main", "high"].map((value) => ({ value })),
                      rate_controls: ["CBR", "CQP", "VBR"].map((value) => ({ value })),
                      bitrate: { min: 200, max: 50000, step: 50 },
                    },
              ),
            },
            // ADR 027 §3.3 bloc 2 (#143), updated by #173. Mirrors what a real
            // headless Pulsar answers today: pulsar-headless binds
            // "Default"/"default" at boot, and the vendor pair
            // GetMonitoringDeviceList / SetMonitoringDevice lets an operator
            // choose another one hot -- hence `live` and `device_selectable`.
            audio_monitoring: {
              applicability: "live",
              available: true,
              device_bound: true,
              device_id: this.monitoringDeviceId,
              device_name: this.monitoringDeviceName,
              device_selectable: true,
            },
            audio_tracks: { applicability: "read-only", count: 6, bound: 1 },
            audio_sample_rate: { applicability: "read-only", hz: 48000 },
            audio_speaker_layout: {
              applicability: "read-only",
              layout: "stereo",
              channels: 2,
            },
            // ADR 027 Amendment 1 (#159). Adapters are read-only (nothing
            // selects one), scales boot-fixed (PULSAR_RESOLUTION selects the
            // resolution at spawn, SetVideoSettings refuses it hot). The
            // single admitted scale mirrors a real headless Pulsar, whose
            // canvas IS its output -- no downscale path exists.
            graphics_adapters: {
              applicability: "read-only",
              active_index: 0,
              values: [
                { value: "NVIDIA GeForce RTX 4070", index: 0 },
                { value: "Intel(R) UHD Graphics 770", index: 1 },
              ],
            },
            output_scales: {
              applicability: "boot-fixed",
              canvas: { width: 1920, height: 1080 },
              values: [{ value: "1920x1080", width: 1920, height: 1080, scale: 1 }],
            },
          },
        };
      }

      case "SetVideoSettings": {
        if ("fps" in data || "width" in data || "height" in data) {
          return { error: "fps / width / height pinned at boot" };
        }
        if ("video_encoder" in data || "video_preset" in data || "video_profile" in data) {
          return { error: "video_encoder / video_preset / video_profile pinned at boot" };
        }
        let changed = false;
        const out: Json = {};
        if (typeof data["video_bitrate"] === "number") {
          this.video.video_bitrate = data["video_bitrate"];
          out["video_bitrate"] = data["video_bitrate"];
          changed = true;
        }
        if (typeof data["audio_bitrate"] === "number") {
          this.video.audio_bitrate = data["audio_bitrate"];
          out["audio_bitrate"] = data["audio_bitrate"];
          changed = true;
        }
        out["changed"] = changed;
        return out;
      }

      case "GetAdaptiveState":
        return { ...this.adaptive };

      case "SetAdaptiveEnabled": {
        if (typeof data["enabled"] !== "boolean") {
          return { error: "enabled (bool) required" };
        }
        this.adaptive.enabled = data["enabled"];
        if (data["enabled"]) this.adaptive.stable_ticks = 0;
        return { enabled: this.adaptive.enabled };
      }

      case "GetMonitoringDeviceList":
        return {
          available: true,
          devices: this.monitoringDevices.map((d) => ({ ...d })),
          active_device_id: this.monitoringDeviceId,
          active_device_name: this.monitoringDeviceName,
        };

      case "SetMonitoringDevice": {
        const id = data["device_id"] as string | undefined;
        if (!id) return { error: "device_id required" };
        // Same refusal the server performs: an id the machine does not
        // enumerate is named, never stored into silence.
        const dev = this.monitoringDevices.find((d) => d.id === id);
        if (!dev) {
          return {
            error: `no such monitoring device: '${id}' is not among the playback devices this machine enumerates`,
          };
        }
        this.monitoringDeviceId = dev.id;
        this.monitoringDeviceName = dev.name;
        return { changed: true, device_id: dev.id, device_name: dev.name };
      }

      default:
        return { error: `unknown vendor request: ${requestType}` };
    }
  }
}

// ---- Per-connection encoding helpers --------------------------------------
//
// obs-websocket-js v5 negotiates either obswebsocket.json (text frames) or
// obswebsocket.msgpack (binary frames) at WS handshake. The mock honours
// whichever the client picked by inspecting ws.protocol after the upgrade.

type Frame = { op: number; d: Json };

function isMsgpack(ws: WebSocket): boolean {
  return (ws as unknown as { protocol?: string }).protocol === "obswebsocket.msgpack";
}

function sendFrame(ws: WebSocket, frame: Frame): void {
  if (isMsgpack(ws)) {
    const buf = msgpackEncode(frame);
    // ws Buffer signature accepts Uint8Array directly -- cast keeps the
    // underlying typed-array reference without an extra copy.
    ws.send(Buffer.from(buf.buffer, buf.byteOffset, buf.byteLength));
  } else {
    ws.send(JSON.stringify(frame));
  }
}

function decodeFrame(raw: Buffer | ArrayBuffer | Buffer[], isBinary: boolean): Frame | undefined {
  try {
    if (isBinary) {
      const buf = Array.isArray(raw) ? Buffer.concat(raw) : Buffer.from(raw as Buffer);
      return msgpackDecode(buf) as Frame;
    }
    const str = Array.isArray(raw) ? Buffer.concat(raw).toString() : raw.toString();
    return JSON.parse(str) as Frame;
  } catch {
    return undefined;
  }
}

