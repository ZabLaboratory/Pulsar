// Public types for @zablaboratory/pulsar-client.
//
// Pulsar's vendor wire format uses snake_case (legacy of the C++ impl);
// the public API exposes camelCase to match TS/JS convention. The
// translation layer lives in wire.ts.

/** A streaming destination kind supported by pulsar-multi-stream. */
export type DestinationKind = "rtmp_custom" | "vod_local" | "twitch";

/** A single destination as surfaced by GetDestinations. */
export interface Destination {
  id: string;
  name: string;
  kind: DestinationKind;
  /** RTMP server URL (rtmp_custom / twitch) or file path (vod_local). */
  url: string;
  /** Last user intent (Start/Stop request). */
  enabled: boolean;
  /** True iff the underlying obs_output_t is currently active. */
  active: boolean;
}

/** Inputs for CreateDestination. */
export interface CreateDestinationInput {
  /** Display name. Defaults to the generated id when omitted. */
  name?: string;
  kind: DestinationKind;
  /** RTMP URL (rtmp_custom) or file path (vod_local). Ignored for twitch
   *  -- the server pins its own ingest URL. */
  url?: string;
  /** Required for rtmp_custom + twitch. Unused for vod_local. */
  key?: string;
}

/** Snapshot returned by GetVideoSettings. */
export interface VideoSettings {
  fps: number;
  width: number;
  height: number;
  videoBitrate: number;
  videoRateControl: string;
  videoKeyintSec: number;
  audioBitrate: number;
}

/** Mutations accepted by SetVideoSettings. fps/width/height changes are
 *  rejected at the server -- they require boot-time env vars. */
export interface VideoSettingsPatch {
  videoBitrate?: number;
  audioBitrate?: number;
}

/** Result of SetVideoSettings. */
export interface VideoSettingsPatchResult {
  changed: boolean;
  videoBitrate?: number;
  audioBitrate?: number;
}

/** Snapshot returned by GetAdaptiveState. */
export interface AdaptiveState {
  enabled: boolean;
  /** Bitrate the loop tries to maintain (latched at first sample). */
  targetKbps: number;
  /** Encoder's current configured bitrate (may differ from target after a down-adjust). */
  currentKbps: number;
  /** Lower bound the loop will not drop below (30% of target by default). */
  floorKbps: number;
  /** Consecutive ticks without drops since last adjust. >= 15 triggers a recovery climb. */
  stableTicks: number;
  /** Cumulative number of bitrate adjustments since boot. */
  adjustmentsTotal: number;
  /** Frames produced by all active outputs during the last sample window. */
  lastDeltaTotal: number;
  /** Frames dropped by all active outputs during the last sample window. */
  lastDeltaDropped: number;
  /** Drop ratio = lastDeltaDropped / max(1, lastDeltaTotal). */
  lastDropRatio: number;
}

/** Recording / streaming output state as broadcast by obs-websocket. */
export type OutputState =
  | "STARTING"
  | "STARTED"
  | "STOPPING"
  | "STOPPED"
  | "PAUSED"
  | "RESUMED"
  | "RECONNECTING"
  | "RECONNECTED";

/** RecordStateChanged event payload. */
export interface RecordStateChangedEvent {
  state: OutputState;
  /** File path on disk -- only set when state is STOPPED. */
  outputPath?: string;
}

/** StreamStateChanged event payload. */
export interface StreamStateChangedEvent {
  state: OutputState;
}

/** StudioModeStateChanged event payload. */
export interface StudioModeStateChangedEvent {
  enabled: boolean;
}

/** pulsar:BitrateAdjusted vendor event payload. */
export interface BitrateAdjustedEvent {
  bitrate: number;
  target: number;
  floor: number;
  reason: "drops" | "recovery";
  dropRatio: number;
}

/** Connection options for PulsarClient.connect. */
export interface ConnectOptions {
  /** Defaults to "ws://127.0.0.1:4455". */
  url?: string;
  /** obs-websocket auth password. Read it from
   *  <pulsar-bin>/obs-websocket/config.json (server_password field). */
  password?: string;
  /** Bitmask of obs-websocket EventSubscription flags. Defaults to 0x7FF
   *  (all baseline event categories). */
  eventSubscriptions?: number;
}

/** Mapping of typed event names to their payloads. */
export interface PulsarEventMap {
  bitrateAdjusted: BitrateAdjustedEvent;
  recordStateChanged: RecordStateChangedEvent;
  streamStateChanged: StreamStateChangedEvent;
  studioModeStateChanged: StudioModeStateChangedEvent;
  connectionClosed: { code: number; reason: string };
}

export type PulsarEventName = keyof PulsarEventMap;
