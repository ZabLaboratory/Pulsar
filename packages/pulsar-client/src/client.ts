import OBSWebSocket from "obs-websocket-js";

import { TypedEventEmitter } from "./events.js";
import { PulsarNotConnectedError, PulsarVendorError } from "./errors.js";
import { DestinationsNamespace } from "./destinations.js";
import { VideoNamespace } from "./video.js";
import { CapabilitiesNamespace } from "./capabilities.js";
import { AdaptiveNamespace } from "./adaptive.js";
import { RecordNamespace } from "./record.js";
import { StreamNamespace } from "./stream.js";
import { AudioNamespace } from "./audio.js";
import { bitrateAdjustedFromWire, type WireBitrateAdjustedEvent } from "./wire.js";
import type {
  ConnectOptions,
  InputMuteStateChangedEvent,
  RecordStateChangedEvent,
  StreamStateChangedEvent,
  StudioModeStateChangedEvent,
} from "./types.js";

const VENDOR = "pulsar";

/**
 * Typed client for Pulsar.
 *
 * Wraps obs-websocket-js v5: callers can still reach the full v5 surface
 * via `client.obs.call(...)` (e.g. for `GetVersion`, `GetSceneList`),
 * while `client.destinations`, `client.video`, `client.adaptive`,
 * `client.record` and `client.stream` provide ergonomic typed wrappers
 * over Pulsar's vendor namespace and the legacy frontend-stub
 * stream/record APIs.
 *
 * @example
 *   const pulsar = new PulsarClient();
 *   await pulsar.connect({ url: "ws://127.0.0.1:4455", password: "..." });
 *
 *   const dest = await pulsar.destinations.create({
 *     kind: "twitch",
 *     key: process.env.TWITCH_KEY!,
 *   });
 *   await pulsar.destinations.start(dest.id);
 *
 *   pulsar.on("bitrateAdjusted", (e) => {
 *     console.log(`bitrate ${e.bitrate} kbps (${e.reason})`);
 *   });
 *
 *   await pulsar.record.start();
 *   await new Promise(r => setTimeout(r, 3000));
 *   const path = await pulsar.record.stop();
 *
 *   await pulsar.disconnect();
 */
export class PulsarClient extends TypedEventEmitter {
  /** Underlying obs-websocket-js client. Use it for v5 baseline calls. */
  public readonly obs: OBSWebSocket;
  public readonly destinations: DestinationsNamespace;
  public readonly video: VideoNamespace;
  public readonly capabilities: CapabilitiesNamespace;
  public readonly adaptive: AdaptiveNamespace;
  public readonly record: RecordNamespace;
  public readonly stream: StreamNamespace;
  public readonly audio: AudioNamespace;

  private connected = false;

  constructor() {
    super();
    this.obs = new OBSWebSocket();
    this.destinations = new DestinationsNamespace(this);
    this.video = new VideoNamespace(this);
    this.capabilities = new CapabilitiesNamespace(this);
    this.adaptive = new AdaptiveNamespace(this);
    this.record = new RecordNamespace(this);
    this.stream = new StreamNamespace(this);
    this.audio = new AudioNamespace(this);

    this.obs.on("ConnectionClosed", (info) => {
      this.connected = false;
      this.emit("connectionClosed", {
        code: info.code,
        reason: info.message ?? "",
      });
    });

    // Translate legacy obs-websocket events into our typed surface.
    this.obs.on("RecordStateChanged", (data) => {
      const evt: RecordStateChangedEvent = { state: extractOutputState(data.outputState) };
      if (typeof data.outputPath === "string" && data.outputPath !== "") {
        evt.outputPath = data.outputPath;
      }
      this.emit("recordStateChanged", evt);
    });
    this.obs.on("StreamStateChanged", (data) => {
      const evt: StreamStateChangedEvent = { state: extractOutputState(data.outputState) };
      this.emit("streamStateChanged", evt);
    });
    this.obs.on("StudioModeStateChanged", (data) => {
      const evt: StudioModeStateChangedEvent = { enabled: data.studioModeEnabled };
      this.emit("studioModeStateChanged", evt);
    });
    this.obs.on("InputMuteStateChanged", (data) => {
      const evt: InputMuteStateChangedEvent = {
        inputName: data.inputName,
        inputMuted: data.inputMuted,
      };
      this.emit("inputMuteStateChanged", evt);
    });

    // Pulsar vendor events -- VendorEvent payload includes vendorName +
    // eventType + eventData. We dispatch on eventType under our namespace.
    this.obs.on("VendorEvent", (data) => {
      if (data.vendorName !== VENDOR) return;
      switch (data.eventType) {
        case "BitrateAdjusted": {
          const wire = data.eventData as unknown as WireBitrateAdjustedEvent;
          this.emit("bitrateAdjusted", bitrateAdjustedFromWire(wire));
          break;
        }
        // Future Pulsar events land in additional cases here.
      }
    });
  }

  /** Connect to a running Pulsar (or any obs-websocket v5 server). */
  async connect(options: ConnectOptions = {}): Promise<void> {
    const url = options.url ?? "ws://127.0.0.1:4455";
    const subs = options.eventSubscriptions ?? 0x7ff;
    await this.obs.connect(url, options.password, {
      eventSubscriptions: subs,
      rpcVersion: 1,
    });
    this.connected = true;
  }

  async disconnect(): Promise<void> {
    if (!this.connected) return;
    await this.obs.disconnect();
    this.connected = false;
  }

  isConnected(): boolean {
    return this.connected;
  }

  /**
   * Internal: invoke a vendor request under the "pulsar" namespace.
   *
   * Throws PulsarNotConnectedError if disconnected, PulsarVendorError if
   * the server response includes an `error` field, and lets the
   * underlying obs-websocket-js exceptions bubble up otherwise.
   */
  async callVendor<TReq extends object, TRes extends { error?: string }>(
    requestType: string,
    requestData?: TReq,
  ): Promise<TRes> {
    if (!this.connected) throw new PulsarNotConnectedError();

    const callPayload: { vendorName: string; requestType: string; requestData?: TReq } = {
      vendorName: VENDOR,
      requestType,
    };
    if (requestData !== undefined) {
      callPayload.requestData = requestData;
    }
    const resp = await this.obs.call("CallVendorRequest", callPayload as never);
    const inner = ((resp as unknown as { responseData?: TRes }).responseData ?? {}) as TRes;
    if (typeof inner.error === "string" && inner.error.length > 0) {
      throw new PulsarVendorError(requestType, inner.error);
    }
    return inner;
  }
}

/** Strip the "OBS_WEBSOCKET_OUTPUT_" prefix obs-websocket adds to state strings. */
function extractOutputState(raw: string): RecordStateChangedEvent["state"] {
  const prefix = "OBS_WEBSOCKET_OUTPUT_";
  return (raw.startsWith(prefix) ? raw.slice(prefix.length) : raw) as RecordStateChangedEvent["state"];
}
