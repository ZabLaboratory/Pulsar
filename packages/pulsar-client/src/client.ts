import OBSWebSocket, { type OBSRequestTypes } from "obs-websocket-js";

import { TypedEventEmitter } from "./events.js";
import { PulsarNotConnectedError, PulsarVendorError, type PulsarPrismErrorEnvelope } from "./errors.js";
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
    this.emitPrism({
      severity: "debug",
      domain: "service",
      source: "pulsar.client",
      code: "ACTION_STARTED",
      message: "Pulsar connection started",
      context: { action: "connect" },
      details: { host: maskHost(url) },
    });
    const subs = options.eventSubscriptions ?? 0x7ff;
    try {
      await this.obs.connect(url, options.password, {
        eventSubscriptions: subs,
        rpcVersion: 1,
      });
      this.connected = true;
      this.emitPrism({
        severity: "info",
        domain: "service",
        source: "pulsar.client",
        code: "SERVICE_READY",
        message: "Pulsar is ready",
        context: { action: "connect" },
        details: { host: maskHost(url) },
      });
    } catch (error) {
      this.emitPrism({
        severity: "error",
        domain: "service",
        source: "pulsar.client",
        code: "SERVICE_UNAVAILABLE",
        message: "Pulsar connection failed",
        context: { action: "connect" },
        details: { errorType: error instanceof Error ? error.name : typeof error },
      });
      throw error;
    }
  }

  async disconnect(): Promise<void> {
    if (!this.connected) {
      this.emitPrism({
        severity: "info",
        domain: "service",
        source: "pulsar.client",
        code: "ACTION_NOOP",
        message: "Pulsar was already disconnected",
        context: { action: "disconnect" },
        details: {},
      });
      return;
    }
    try {
      await this.obs.disconnect();
    } catch (error) {
      this.emitPrism({
        severity: "error",
        domain: "service",
        source: "pulsar.client",
        code: "ACTION_FAILED",
        message: "Pulsar disconnect failed",
        context: { action: "disconnect" },
        details: { errorType: error instanceof Error ? error.name : typeof error },
      });
      throw error;
    }
    this.connected = false;
    this.emitPrism({
      severity: "info",
      domain: "service",
      source: "pulsar.client",
      code: "SERVICE_STOPPED",
      message: "Pulsar disconnected",
      context: { action: "disconnect" },
      details: {},
    });
  }

  /** Call a standard obs-websocket request with the same lifecycle logging as
   * the Pulsar vendor namespace. Typed namespaces use this seam so a failed
   * OBS action cannot disappear as an unstructured rejected promise. */
  async call<TResponse = unknown>(requestType: string, requestData?: object): Promise<TResponse> {
    if (!this.connected) {
      const error = new PulsarNotConnectedError();
      this.emitPrism(error.prism);
      throw error;
    }
    this.emitPrism({
      severity: "debug",
      domain: "broadcast",
      source: "pulsar.obs",
      code: "ACTION_STARTED",
      message: "Pulsar OBS action started",
      context: { action: requestType },
      details: {},
    });
    try {
      const response = (await this.obs.call(requestType as keyof OBSRequestTypes, requestData as never)) as TResponse;
      this.emitPrism({
        severity: "info",
        domain: "broadcast",
        source: "pulsar.obs",
        code: "ACTION_SUCCEEDED",
        message: "Pulsar OBS action completed",
        context: { action: requestType },
        details: {},
      });
      return response;
    } catch (error) {
      this.emitPrism({
        severity: "error",
        domain: "broadcast",
        source: "pulsar.obs",
        code: "ACTION_FAILED",
        message: "Pulsar OBS action failed",
        context: { action: requestType },
        details: { errorType: error instanceof Error ? error.name : typeof error },
      });
      throw error;
    }
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
    if (!this.connected) {
      const error = new PulsarNotConnectedError();
      this.emitPrism(error.prism);
      throw error;
    }

    this.emitPrism({
      severity: "debug",
      domain: "broadcast",
      source: "pulsar.vendor",
      code: "ACTION_STARTED",
      message: "Pulsar vendor action started",
      context: { action: requestType },
      details: {},
    });

    const callPayload: { vendorName: string; requestType: string; requestData?: TReq } = {
      vendorName: VENDOR,
      requestType,
    };
    if (requestData !== undefined) {
      callPayload.requestData = requestData;
    }
    let resp: unknown;
    try {
      resp = await this.obs.call("CallVendorRequest", callPayload as never);
    } catch (error) {
      this.emitPrism({
        severity: "error",
        domain: "broadcast",
        source: "pulsar.vendor",
        code: "ACTION_FAILED",
        message: "Pulsar vendor action failed",
        context: { action: requestType },
        details: { errorType: error instanceof Error ? error.name : typeof error },
      });
      throw error;
    }
    const inner = ((resp as unknown as { responseData?: TRes }).responseData ?? {}) as TRes;
    if (typeof inner.error === "string" && inner.error.length > 0) {
      const raw = inner as TRes & Record<string, unknown>;
      const envelope: Partial<PulsarPrismErrorEnvelope> = {};
      if (typeof raw.code === "string") envelope.code = raw.code;
      if (typeof raw.message === "string") envelope.message = raw.message;
      if (raw.severity === "error" || raw.severity === "warning") envelope.severity = raw.severity;
      if (isPrismDomain(raw.domain)) envelope.domain = raw.domain;
      if (typeof raw.source === "string") envelope.source = raw.source;
      if (isRecord(raw.context)) envelope.context = raw.context;
      if (isRecord(raw.details)) envelope.details = raw.details;
      if (typeof raw.requestId === "string") envelope.requestId = raw.requestId;
      const error = new PulsarVendorError(requestType, inner.error, envelope);
      this.emitPrism(error.prism);
      throw error;
    }
    this.emitPrism({
      severity: "info",
      domain: "broadcast",
      source: "pulsar.vendor",
      code: "ACTION_SUCCEEDED",
      message: "Pulsar vendor action completed",
      context: { action: requestType },
      details: {},
    });
    return inner;
  }

  reportPrism(event: Omit<import("./types.js").PulsarPrismLogEvent, "schemaVersion">): void {
    this.emit("prismLog", { schemaVersion: 1, ...event });
  }

  private emitPrism(event: Omit<import("./types.js").PulsarPrismLogEvent, "schemaVersion">): void {
    this.reportPrism(event);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isPrismDomain(value: unknown): value is PulsarPrismErrorEnvelope["domain"] {
  return (
    value === "scene" ||
    value === "broadcast" ||
    value === "service" ||
    value === "system" ||
    value === "operator"
  );
}

function maskHost(value: string): string {
  try {
    const url = new URL(value);
    return `${url.protocol}//${url.hostname}${url.port ? `:${url.port}` : ""}`;
  } catch {
    return "invalid-url";
  }
}

/** Strip the "OBS_WEBSOCKET_OUTPUT_" prefix obs-websocket adds to state strings. */
function extractOutputState(raw: string): RecordStateChangedEvent["state"] {
  const prefix = "OBS_WEBSOCKET_OUTPUT_";
  return (raw.startsWith(prefix) ? raw.slice(prefix.length) : raw) as RecordStateChangedEvent["state"];
}
