// Public types for @clodocapeo/pulsar-client.
//
// Pulsar's vendor wire format uses snake_case (legacy of the C++ impl);
// the public API exposes camelCase to match TS/JS convention. The
// translation layer lives in wire.ts.

/** A streaming destination kind supported by pulsar-multi-stream. */
export type DestinationKind = "rtmp_custom" | "vod_local" | "twitch" | "youtube";

/** A single destination as surfaced by GetDestinations. */
export interface Destination {
  id: string;
  name: string;
  kind: DestinationKind;
  /** RTMP server URL (rtmp_custom / twitch / youtube) or file path (vod_local). */
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
  /** RTMP URL (rtmp_custom) or file path (vod_local). Ignored for the named
   *  platform kinds (twitch, youtube) -- the server pins its own ingest URL. */
  url?: string;
  /** Required for rtmp_custom + twitch + youtube. Unused for vod_local. */
  key?: string;
}

/** Encoder family short names Pulsar reports (ADR 004 §3.3). */
export type EncoderFamily = "x264" | "nvenc" | "qsv" | "amf";

/** Snapshot returned by GetVideoSettings. */
export interface VideoSettings {
  fps: number;
  width: number;
  height: number;
  videoBitrate: number;
  videoRateControl: string;
  videoKeyintSec: number;
  audioBitrate: number;
  /** Active encoder family, boot-fixed via PULSAR_VIDEO_ENCODER (ADR 004 §3.4). */
  videoEncoder: string;
  /** Active encoder preset. */
  videoPreset: string;
  /** Active encoder H.264 profile. */
  videoProfile: string;
}

/**
 * Application regime of a manifest entry (Prism ADR 027 §3.2).
 *
 * - `live`        — settable on a running Pulsar.
 * - `boot-fixed`  — fixed at boot (env), refused hot.
 * - `read-only`   — observable, never settable.
 *
 * A capability the manifest does not declare at all is a fourth, distinct
 * answer — an *absence*, not a regime: the consumer keeps its own static
 * assumption instead of deriving one.
 */
export type CapabilityRegime = "live" | "boot-fixed" | "read-only";

/**
 * What one encoder family this build exposes actually offers (ADR 027 §3.3
 * bloc 1). Read from that family's libobs properties, narrowed by Pulsar's own
 * boot policy — never a list this package or Pulsar holds as a literal.
 *
 * Every field but `family` is optional and stays *undefined* when Pulsar
 * declared it absent (the encoder advertises no such property). A family the
 * binary does not register is not in the array at all.
 */
export interface EncoderFamilyCapability {
  /** Whitelisted family short name: "x264" | "nvenc" | "qsv" | "amf". */
  family: string;
  /** Preset values the family's preset knob offers, in libobs' own order. */
  presets?: string[];
  /** H.264 profiles, intersected with what PULSAR_VIDEO_PROFILE accepts. */
  profiles?: string[];
  /** Rate controls, intersected with what PULSAR_VIDEO_RATE_CONTROL accepts. */
  rateControls?: string[];
  /** Keyframe interval window in seconds. */
  keyintSec?: { min: number; max: number; step: number };
  /** This family's own bitrate window in kbps (may differ per family). */
  bitrateKbps?: { min: number; max: number; step: number };
}

/**
 * Headphone-monitoring capability (ADR 027 §3.3 bloc 2).
 *
 * `deviceBound` is the load-bearing field: a Pulsar that exposes no way to bind
 * a monitoring device answers `false` — an explicit, readable "no" rather than
 * a silence a consumer could mistake for "probably fine". It is `false` even
 * when libobs reports its own seeded `"default"` placeholder, because that seed
 * is not a choice anyone made.
 *
 * The regime tells the consumer whether it may offer the setting at all: it is
 * `read-only` on today's Pulsar, which has no monitoring write path.
 */
export interface AudioMonitoringCapability {
  /** Whether this build/platform supports audio monitoring at all. */
  available: boolean;
  /** Whether a monitoring device is genuinely bound. */
  deviceBound: boolean;
  /** libobs device id — present only when `deviceBound`. */
  deviceId?: string;
  /** Human-readable device name — present only when `deviceBound`. */
  deviceName?: string;
  /**
   * Whether the monitoring device can be CHOSEN over the wire (#173). Absent
   * on a pre-#173 Pulsar, which is not a `false`: a consumer must offer the
   * selector only on an explicit `true`.
   */
  deviceSelectable?: boolean;
}

/** One playback device Pulsar can route monitoring to (#173). */
export interface MonitoringDevice {
  /** libobs device id. `"default"` follows the OS default device. */
  id: string;
  name: string;
}

/** Answer of `GetMonitoringDeviceList` (#173). */
export interface MonitoringDeviceList {
  /** Whether this build supports audio monitoring at all. */
  available: boolean;
  /** Playback devices enumerated at call time; empty when unavailable. */
  devices: MonitoringDevice[];
  /** Device currently in force — absent when none is bound. */
  activeDeviceId?: string;
  activeDeviceName?: string;
}

/**
 * Audio block of the manifest (ADR 027 §3.3 bloc 2). Every field is read from
 * libobs; a field the server could not read is `undefined` (declared absent),
 * never a fallback constant.
 */
export interface AudioCapabilities {
  monitoring?: AudioMonitoringCapability;
  /** Audio tracks (mixer slots) the running libobs supports. */
  trackCount?: number;
  /** Tracks actually bound to the streaming output. Absent off-air. */
  boundTrackCount?: number;
  /** Output sample rate, in Hz. */
  sampleRateHz?: number;
  /** Speaker layout: "mono" | "stereo" | "2.1" | "4.0" | "4.1" | "5.1" | "7.1". */
  speakerLayout?: string;
  /** Channel count implied by the layout. */
  channels?: number;
}

/**
 * One graphics adapter the running libobs enumerates (ADR 027 Amendment 1).
 *
 * `index` is the number `obs_video_info.adapter` is expressed in, so
 * `activeGraphicsAdapter` can be matched against this list instead of being
 * assumed to be `0` — which is the decree this block exists to remove.
 */
export interface GraphicsAdapter {
  /** Adapter name as the graphics subsystem reports it. */
  name: string;
  /** Adapter index, in `obs_video_info.adapter` numbering. */
  index: number;
}

/**
 * One output resolution this Pulsar admits for its current canvas
 * (ADR 027 Amendment 1).
 *
 * The list is what the binary can actually establish, not a ladder of
 * downscale factors: a Pulsar with no downscale path admits exactly its canvas
 * resolution, and says so rather than letting the consumer guess.
 */
export interface OutputScale {
  width: number;
  height: number;
  /** Ratio to the canvas. Absent when the two axes do not share one ratio. */
  scale?: number;
}

/** Capabilities snapshot returned by GetCapabilities (ADR 004 §3.3, ADR 027 §3.2). */
export interface PulsarCapabilities {
  /** Manifest schema version. `0` when talking to a pre-#141 Pulsar. */
  version: number;
  /** Encoder families this build exposes (always contains "x264"). */
  encoders: string[];
  /** Encoder family currently bound to the streaming output. */
  activeEncoder: string;
  /** Inclusive video bitrate window, in kbps. `{min:0,max:0}` when the manifest
   *  declares it absent — the encoder did not advertise a readable range. */
  videoBitrateKbps: { min: number; max: number };
  /** Discrete audio bitrate ladder, in kbps. Empty when declared absent. */
  audioBitrateKbps: number[];
  /**
   * Filter types registered in this binary (ADR 027 §3.3 block 3).
   *
   * **Presence, not permission.** This is the list of filters that *exist*; it
   * says nothing about which of them may be configured, nor within which
   * bounds. Those stay owned by the consumer's own closed whitelist
   * (ADR 023 §3.3). Empty when the manifest declares no inventory — the
   * consumer then keeps its own static list.
   */
  filters: string[];
  /** Input (source) kinds this binary can instantiate. Presence only. */
  sourceKinds: string[];
  /** Destination kinds this binary can serve. Presence only: a kind the
   *  consumer does not know stays ignorable, it is never routed. */
  destinationKinds: string[];
  /** Effective video colorimetry (ADR 027 §3.3 block 4), pinned at
   *  `obs_reset_video`. Read-only: no request and no env var selects another.
   *  `undefined` when the manifest declares it absent. */
  colorimetry?: {
    /** Compact colourspace token, e.g. `"709"` / `"601"` / `"srgb"`. */
    colorSpace: string;
    /** libobs range name, e.g. `"Partial"` / `"Full"`. */
    range: string;
    /** libobs pixel format name, e.g. `"NV12"`. */
    format: string;
  };
  /** Per-family encoder detail (ADR 027 §3.3 bloc 1). Empty when the manifest
   *  carries no encoder block — a pre-#142 Pulsar, whose absence leaves the
   *  consumer's static assumptions intact. */
  encoderFamilies: EncoderFamilyCapability[];
  /** Audio block (ADR 027 §3.3 bloc 2). Empty object on a Pulsar that predates
   *  it — its fields are then all absent, so no consumer reads a "no" that was
   *  never said. */
  audio: AudioCapabilities;
  /** Graphics adapters libobs enumerates (ADR 027 Am.1). Empty when the
   *  manifest declares none — the consumer keeps its own assumption rather
   *  than reading "this machine has no GPU". */
  graphicsAdapters: GraphicsAdapter[];
  /** Index of the adapter actually in use. `undefined` when not declared —
   *  never defaulted to 0. */
  activeGraphicsAdapter?: number;
  /** Canvas resolution the admitted output scales are relative to.
   *  `undefined` when the manifest declares no scale block. */
  canvas?: { width: number; height: number };
  /** Output resolutions admitted for that canvas (ADR 027 Am.1). Empty when
   *  the manifest declares none. */
  outputScales: OutputScale[];
  /** Recording container in effect (issue #166). `"mp4"` or `"mkv"`, chosen
   *  once at spawn by `PULSAR_RECORD_CONTAINER`. `undefined` on a Pulsar that
   *  predates the block. */
  recordContainer?: string;
  /** Containers this build admits for `recordContainer` (issue #166). Empty
   *  when the manifest declares no inventory. */
  recordContainers: string[];
  /** Recording marker support actually available on the bound recording
   *  output (issue #166, ADR Prism 028 §3.5 B6). `undefined` on a Pulsar that
   *  predates the block — never synthesised as `{false, false}`. */
  recordMarkers?: {
    /** Whether `SplitRecordFile` can succeed. */
    splitFile: boolean;
    /** Whether `CreateRecordChapter` can succeed. */
    addChapter: boolean;
  };
  /** Regime per entry. A key is present only if the manifest declared it, so
   *  `undefined` means "not declared", never "live". */
  regimes: {
    encoders?: CapabilityRegime;
    activeEncoder?: CapabilityRegime;
    videoBitrateKbps?: CapabilityRegime;
    audioBitrateKbps?: CapabilityRegime;
    filters?: CapabilityRegime;
    sourceKinds?: CapabilityRegime;
    destinationKinds?: CapabilityRegime;
    colorimetry?: CapabilityRegime;
    encoderFamilies?: CapabilityRegime;
    audioMonitoring?: CapabilityRegime;
    audioTracks?: CapabilityRegime;
    audioSampleRate?: CapabilityRegime;
    audioSpeakerLayout?: CapabilityRegime;
    graphicsAdapters?: CapabilityRegime;
    outputScales?: CapabilityRegime;
    recordContainer?: CapabilityRegime;
    recordMarkers?: CapabilityRegime;
  };
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

/** GetSpecialInputs response -- names of the mic/desktop audio slots. */
export interface SpecialInputs {
  desktop1?: string;
  desktop2?: string;
  mic1?: string;
  mic2?: string;
  mic3?: string;
  mic4?: string;
}

/** A single entry from GetInputList. */
export interface AudioInput {
  name: string;
  kind: string;
}

/** A single capture device from GetInputPropertiesListPropertyItems("device_id"). */
export interface AudioDevice {
  id: string;
  name: string;
  enabled: boolean;
}

/** InputMuteStateChanged event payload. */
export interface InputMuteStateChangedEvent {
  inputName: string;
  inputMuted: boolean;
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

export interface PulsarPrismLogEvent {
  schemaVersion: 1;
  severity: "error" | "warning" | "info" | "debug";
  domain: "scene" | "broadcast" | "service" | "system" | "operator";
  source: string;
  code: string;
  message: string;
  context: Record<string, unknown>;
  details: Record<string, unknown>;
  requestId?: string;
}

/** Mapping of typed event names to their payloads. */
export interface PulsarEventMap {
  prismLog: PulsarPrismLogEvent;
  bitrateAdjusted: BitrateAdjustedEvent;
  recordStateChanged: RecordStateChangedEvent;
  streamStateChanged: StreamStateChangedEvent;
  studioModeStateChanged: StudioModeStateChangedEvent;
  inputMuteStateChanged: InputMuteStateChangedEvent;
  connectionClosed: { code: number; reason: string };
}

export type PulsarEventName = keyof PulsarEventMap;
