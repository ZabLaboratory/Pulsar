// snake_case <-> camelCase mapping for vendor payloads.
//
// Pulsar's C++ vendor handlers serialize obs_data_t fields with the
// snake_case names the libobs C API expects. We keep the public TS API
// camelCase to match JS conventions; the mapping is centralized here so
// adding a vendor field touches exactly one place.

import type {
  AdaptiveState,
  AudioCapabilities,
  AudioMonitoringCapability,
  BitrateAdjustedEvent,
  CapabilityRegime,
  CreateDestinationInput,
  Destination,
  DestinationKind,
  EncoderFamilyCapability,
  GraphicsAdapter,
  MonitoringDevice,
  MonitoringDeviceList,
  OutputScale,
  ProgramAudioOutput,
  ProgramAudioPts,
  ProgramAudioRoute,
  ProgramAudioSource,
  ProgramAudioTrack,
  PulsarCapabilities,
  VideoSettings,
  VideoSettingsPatch,
  VideoSettingsPatchResult,
} from "./types.js";

// ---- Wire shapes (what travels on obs-websocket vendor frames) ----------

export interface WireDestination {
  id: string;
  name: string;
  kind: DestinationKind;
  url: string;
  enabled: boolean;
  active: boolean;
}

export interface WireGetDestinationsResponse {
  destinations?: WireDestination[];
  error?: string;
}

export interface WireCreateDestinationRequest {
  name?: string;
  kind: DestinationKind;
  url?: string;
  key?: string;
}

export interface WireCreateDestinationResponse {
  id?: string;
  error?: string;
}

export interface WireRemoveDestinationResponse {
  removed?: boolean;
  error?: string;
}

export interface WireStartDestinationResponse {
  started?: boolean;
  error?: string;
}

export interface WireStopDestinationResponse {
  stopped?: boolean;
  error?: string;
}

export interface WireOkResponse {
  ok?: boolean;
  error?: string;
}

export interface WireGetVideoSettingsResponse {
  fps?: number;
  width?: number;
  height?: number;
  video_bitrate?: number;
  video_rate_control?: string;
  video_keyint_sec?: number;
  audio_bitrate?: number;
  video_encoder?: string;
  video_preset?: string;
  video_profile?: string;
  error?: string;
}

// libobs vendor handlers can only serialise arrays as arrays of objects
// (Utils::Json::ObsDataToJson walks obs_data_array items as obs_data), never
// bare JSON scalar arrays. So Pulsar wraps each list element in a one-field
// { value } object; the client unwraps it back to a flat array here.
export interface WireValueItem<T> {
  value: T;
}

// ADR 027 §3.2 (#141): every manifest entry declares its application regime
// next to its values. Unknown entries and unknown regimes are tolerated -- the
// manifest is additive by contract, so a client older than the server must not
// choke on a block it has never heard of.
export interface WireCapabilityEntry {
  applicability?: string;
  [k: string]: unknown;
}

/** One entry of `capabilities.encoder_families.values` (ADR 027 §3.3 bloc 1,
 *  #142). Every field but `value` is optional: Pulsar omits what that family's
 *  libobs properties do not advertise, and an omission is a positive answer. */
export interface WireEncoderFamily {
  value: string;
  presets?: WireValueItem<string>[];
  profiles?: WireValueItem<string>[];
  rate_controls?: WireValueItem<string>[];
  keyint_sec?: { min?: number; max?: number; step?: number };
  bitrate?: { min?: number; max?: number; step?: number };
}

/** One entry of `capabilities.graphics_adapters.values` (ADR 027 Am.1, #159).
 *  `value` is the adapter name, `index` the `obs_video_info.adapter` number. */
export interface WireGraphicsAdapter {
  value: string;
  index?: number;
}

/** One entry of `capabilities.output_scales.values` (ADR 027 Am.1, #159).
 *  `value` is the `"<W>x<H>"` token; `scale` is omitted when the two axes do
 *  not share one ratio. */
export interface WireOutputScale {
  value: string;
  width?: number;
  height?: number;
  scale?: number;
}

/** `capabilities.record_markers` (issue #166, ADR Prism 028 §3.5 B6). Both
 *  booleans are always sent together when the block is present. */
export interface WireRecordMarkers {
  split_file?: boolean;
  add_chapter?: boolean;
}

export interface WireGetCapabilitiesResponse {
  /** Manifest schema version. Absent on a pre-#141 Pulsar. */
  version?: number;
  encoders?: WireValueItem<string>[];
  active_encoder?: string;
  video_bitrate?: { min?: number; max?: number };
  audio_bitrate?: WireValueItem<number>[];
  /** Regime-carrying map, keyed by capability name. Absent on a pre-#141 Pulsar. */
  capabilities?: Record<string, WireCapabilityEntry | undefined>;
  error?: string;
}

export interface WireSetVideoSettingsRequest {
  video_bitrate?: number;
  audio_bitrate?: number;
}

export interface WireSetVideoSettingsResponse {
  changed?: boolean;
  video_bitrate?: number;
  audio_bitrate?: number;
  error?: string;
}

export interface WireGetAdaptiveStateResponse {
  enabled?: boolean;
  target_kbps?: number;
  current_kbps?: number;
  floor_kbps?: number;
  stable_ticks?: number;
  adjustments_total?: number;
  last_delta_total?: number;
  last_delta_dropped?: number;
  last_drop_ratio?: number;
  error?: string;
}

export interface WireSetAdaptiveEnabledRequest {
  enabled: boolean;
}

export interface WireSetAdaptiveEnabledResponse {
  enabled?: boolean;
  error?: string;
}

export interface WireGetMonitoringDeviceListResponse {
  available?: boolean;
  devices?: Array<{ id?: unknown; name?: unknown }>;
  active_device_id?: string;
  active_device_name?: string;
  error?: string;
}

export interface WireSetMonitoringDeviceRequest {
  device_id: string;
}

export interface WireSetMonitoringDeviceResponse {
  changed?: boolean;
  device_id?: string;
  device_name?: string;
  error?: string;
}

export interface WireProgramAudioPts {
  first_ns?: number;
  last_ns?: number;
  samples?: number;
  regressions?: number;
  monotone?: boolean;
  series_ns?: Array<{ pts_ns?: number }>;
}

export interface WireProgramAudioSlot {
  slot?: number;
  track?: number;
  encoder?: string;
}

export interface WireProgramAudioOutput {
  output?: string;
  id?: string;
  name?: string;
  audio_supported?: boolean;
  audio_identity?: string;
  audio_matches_route?: boolean;
  active?: boolean;
  slots?: WireProgramAudioSlot[];
}

export interface WireProgramAudioSource {
  channel?: number;
  identity?: string;
  id?: string;
  name?: string;
}

export interface WireProgramAudioTrack {
  track?: number;
  mixer_index?: number;
  encoder?: string;
  blocks?: number;
  frames?: number;
  first_pts_ns?: number;
  last_pts_ns?: number;
  pts_samples?: number;
  pts_regressions?: number;
  pts_monotone?: boolean;
  pts?: WireProgramAudioPts;
  pts_series_ns?: Array<{ pts_ns?: number }>;
}

export interface WireGetProgramAudioRouteResponse {
  schema_version?: number;
  route_id?: string;
  route_name?: string;
  scope?: string;
  cut_audio_policy?: string;
  audio_identity?: string;
  stable?: boolean;
  preview_audio_supported?: boolean;
  afv_supported?: boolean;
  observed?: boolean;
  outputs?: WireProgramAudioOutput[];
  sources?: WireProgramAudioSource[];
  tracks?: WireProgramAudioTrack[];
  pts_monotone?: boolean;
  pts_samples?: number;
  route_error?: string;
  error?: string;
}

export interface WireBitrateAdjustedEvent {
  bitrate: number;
  target: number;
  floor: number;
  reason: "drops" | "recovery";
  drop_ratio: number;
}

// ---- Mappings ----------------------------------------------------------

export function destinationFromWire(w: WireDestination): Destination {
  return {
    id: w.id,
    name: w.name,
    kind: w.kind,
    url: w.url,
    enabled: w.enabled,
    active: w.active,
  };
}

export function createDestinationToWire(input: CreateDestinationInput): WireCreateDestinationRequest {
  const w: WireCreateDestinationRequest = { kind: input.kind };
  if (input.name !== undefined) w.name = input.name;
  if (input.url !== undefined) w.url = input.url;
  if (input.key !== undefined) w.key = input.key;
  return w;
}

export function videoSettingsFromWire(w: WireGetVideoSettingsResponse): VideoSettings {
  return {
    fps: w.fps ?? 0,
    width: w.width ?? 0,
    height: w.height ?? 0,
    videoBitrate: w.video_bitrate ?? 0,
    videoRateControl: w.video_rate_control ?? "",
    videoKeyintSec: w.video_keyint_sec ?? 0,
    audioBitrate: w.audio_bitrate ?? 0,
    videoEncoder: w.video_encoder ?? "",
    videoPreset: w.video_preset ?? "",
    videoProfile: w.video_profile ?? "",
  };
}

const CAPABILITY_REGIMES: readonly CapabilityRegime[] = ["live", "boot-fixed", "read-only"];

/** A regime string the server sent that this client does not know is dropped,
 *  never coerced into one of the three we do know. */
function regimeOf(
  w: WireGetCapabilitiesResponse,
  key: string,
): CapabilityRegime | undefined {
  const a = w.capabilities?.[key]?.applicability;
  return CAPABILITY_REGIMES.includes(a as CapabilityRegime) ? (a as CapabilityRegime) : undefined;
}

/** Unwraps the `values: [{value}]` list of an inventory entry (ADR 027 §3.3
 *  block 3). Anything that is not a list of string-valued items yields `[]`:
 *  the consumer keeps its own static list rather than trusting a malformed
 *  block. */
function inventoryOf(w: WireGetCapabilitiesResponse, key: string): string[] {
  const raw = w.capabilities?.[key]?.values;
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const item of raw) {
    const v = (item as { value?: unknown } | null)?.value;
    if (typeof v === "string" && v.length > 0) out.push(v);
  }
  return out;
}

/** Reads a numeric field of a manifest entry, or `undefined` when the entry or
 *  the field is absent / not a number. An unreadable field stays absent -- the
 *  client never substitutes a default for a value the server declined to state. */
function entryNumber(
  w: WireGetCapabilitiesResponse,
  key: string,
  field: string,
): number | undefined {
  const v = w.capabilities?.[key]?.[field];
  return typeof v === "number" ? v : undefined;
}

function entryString(
  w: WireGetCapabilitiesResponse,
  key: string,
  field: string,
): string | undefined {
  const v = w.capabilities?.[key]?.[field];
  return typeof v === "string" && v !== "" ? v : undefined;
}

/**
 * Decodes the audio block (ADR 027 §3.3 bloc 2, #143).
 *
 * The monitoring sub-entry is only produced when the server actually declared
 * it: `available`/`deviceBound` are booleans the server states explicitly, so
 * synthesising `{available:false, deviceBound:false}` for a server that said
 * nothing would turn a silence into an answer -- the exact confusion this block
 * exists to remove. Absent stays absent.
 */
function audioFromWire(w: WireGetCapabilitiesResponse): AudioCapabilities {
  const audio: AudioCapabilities = {};

  const mon = w.capabilities?.audio_monitoring;
  if (mon && typeof mon.available === "boolean" && typeof mon.device_bound === "boolean") {
    const monitoring: AudioMonitoringCapability = {
      available: mon.available,
      deviceBound: mon.device_bound,
    };
    const id = entryString(w, "audio_monitoring", "device_id");
    const name = entryString(w, "audio_monitoring", "device_name");
    if (id) monitoring.deviceId = id;
    if (name) monitoring.deviceName = name;
    // Same rule as the entry itself: a server that said nothing about
    // selectability is left undefined, not turned into a `false` it never sent.
    if (typeof mon.device_selectable === "boolean")
      monitoring.deviceSelectable = mon.device_selectable;
    audio.monitoring = monitoring;
  }

  const count = entryNumber(w, "audio_tracks", "count");
  if (count !== undefined) audio.trackCount = count;
  const bound = entryNumber(w, "audio_tracks", "bound");
  if (bound !== undefined) audio.boundTrackCount = bound;

  const hz = entryNumber(w, "audio_sample_rate", "hz");
  if (hz !== undefined) audio.sampleRateHz = hz;

  const layout = entryString(w, "audio_speaker_layout", "layout");
  if (layout !== undefined) audio.speakerLayout = layout;
  const channels = entryNumber(w, "audio_speaker_layout", "channels");
  if (channels !== undefined) audio.channels = channels;

  return audio;
}

/**
 * Decodes `capabilities.graphics_adapters.values` (ADR 027 Am.1, #159).
 *
 * An item without a readable name *and* a numeric index is dropped rather than
 * half-decoded: an adapter whose index the client had to invent could not be
 * matched against `activeGraphicsAdapter`, which is the only thing this list is
 * good for.
 */
function graphicsAdaptersFromWire(w: WireGetCapabilitiesResponse): GraphicsAdapter[] {
  const raw = w.capabilities?.["graphics_adapters"]?.["values"];
  if (!Array.isArray(raw)) return [];

  const out: GraphicsAdapter[] = [];
  for (const item of raw as WireGraphicsAdapter[]) {
    if (!item || typeof item.value !== "string" || item.value === "") continue;
    if (typeof item.index !== "number") continue;
    out.push({ name: item.value, index: item.index });
  }
  return out;
}

/**
 * Decodes `capabilities.output_scales.values` (ADR 027 Am.1, #159).
 *
 * `width`/`height` are the load-bearing pair; an item missing either is dropped
 * rather than reconstructed from the `"<W>x<H>"` token, which is a label, not a
 * second source of truth. `scale` stays absent when the server omitted it.
 */
function outputScalesFromWire(w: WireGetCapabilitiesResponse): OutputScale[] {
  const raw = w.capabilities?.["output_scales"]?.["values"];
  if (!Array.isArray(raw)) return [];

  const out: OutputScale[] = [];
  for (const item of raw as WireOutputScale[]) {
    if (!item || typeof item.width !== "number" || typeof item.height !== "number") continue;
    const scale: OutputScale = { width: item.width, height: item.height };
    if (typeof item.scale === "number") scale.scale = item.scale;
    out.push(scale);
  }
  return out;
}

/** Decodes `capabilities.output_scales.canvas`. Both dimensions or nothing —
 *  a half-read canvas would silently rescale everything measured against it. */
function canvasFromWire(
  w: WireGetCapabilitiesResponse,
): { width: number; height: number } | undefined {
  const raw = w.capabilities?.["output_scales"]?.["canvas"] as
    | { width?: unknown; height?: unknown }
    | undefined;
  if (!raw || typeof raw.width !== "number" || typeof raw.height !== "number") return undefined;
  return { width: raw.width, height: raw.height };
}

/**
 * Decodes `capabilities.record_markers` (issue #166, ADR Prism 028 §3.5 B6).
 *
 * Both booleans or nothing: a server that sent only one is treated as absent
 * rather than half-read, exactly like colorimetry above -- the two flags
 * describe the same recording output and a partial pair cannot be trusted.
 */
function recordMarkersFromWire(
  w: WireGetCapabilitiesResponse,
): { splitFile: boolean; addChapter: boolean } | undefined {
  const raw = w.capabilities?.["record_markers"] as WireRecordMarkers | undefined;
  if (!raw || typeof raw.split_file !== "boolean" || typeof raw.add_chapter !== "boolean")
    return undefined;
  return { splitFile: raw.split_file, addChapter: raw.add_chapter };
}

export function capabilitiesFromWire(w: WireGetCapabilitiesResponse): PulsarCapabilities {
  // Regimes are only populated for entries the manifest actually declares. A
  // pre-#141 Pulsar sends no `capabilities` block at all: every regime is then
  // `undefined` and the consumer keeps its own static assumption -- an absent
  // block leaves the static bound intact (ADR 027 §3.3).
  const regimes: PulsarCapabilities["regimes"] = {};
  const encodersRegime = regimeOf(w, "encoders");
  if (encodersRegime) regimes.encoders = encodersRegime;
  const activeRegime = regimeOf(w, "active_encoder");
  if (activeRegime) regimes.activeEncoder = activeRegime;
  const videoRegime = regimeOf(w, "video_bitrate");
  if (videoRegime) regimes.videoBitrateKbps = videoRegime;
  const audioRegime = regimeOf(w, "audio_bitrate");
  if (audioRegime) regimes.audioBitrateKbps = audioRegime;
  const filtersRegime = regimeOf(w, "filters");
  if (filtersRegime) regimes.filters = filtersRegime;
  const sourceKindsRegime = regimeOf(w, "source_kinds");
  if (sourceKindsRegime) regimes.sourceKinds = sourceKindsRegime;
  const destinationKindsRegime = regimeOf(w, "destination_kinds");
  if (destinationKindsRegime) regimes.destinationKinds = destinationKindsRegime;
  const colorimetryRegime = regimeOf(w, "video_colorimetry");
  if (colorimetryRegime) regimes.colorimetry = colorimetryRegime;
  const familiesRegime = regimeOf(w, "encoder_families");
  if (familiesRegime) regimes.encoderFamilies = familiesRegime;
  const monitoringRegime = regimeOf(w, "audio_monitoring");
  if (monitoringRegime) regimes.audioMonitoring = monitoringRegime;
  const tracksRegime = regimeOf(w, "audio_tracks");
  if (tracksRegime) regimes.audioTracks = tracksRegime;
  const sampleRateRegime = regimeOf(w, "audio_sample_rate");
  if (sampleRateRegime) regimes.audioSampleRate = sampleRateRegime;
  const speakersRegime = regimeOf(w, "audio_speaker_layout");
  if (speakersRegime) regimes.audioSpeakerLayout = speakersRegime;
  const adaptersRegime = regimeOf(w, "graphics_adapters");
  if (adaptersRegime) regimes.graphicsAdapters = adaptersRegime;
  const scalesRegime = regimeOf(w, "output_scales");
  if (scalesRegime) regimes.outputScales = scalesRegime;
  const recordContainerRegime = regimeOf(w, "record_container");
  if (recordContainerRegime) regimes.recordContainer = recordContainerRegime;
  const recordMarkersRegime = regimeOf(w, "record_markers");
  if (recordMarkersRegime) regimes.recordMarkers = recordMarkersRegime;

  // Colorimetry is reported only when the three fields are there; a partial
  // entry is treated as absent rather than half-read.
  const colorSpace = entryString(w, "video_colorimetry", "value");
  const range = entryString(w, "video_colorimetry", "range");
  const format = entryString(w, "video_colorimetry", "format");

  const caps: PulsarCapabilities = {
    version: w.version ?? 0,
    encoders: (w.encoders ?? []).map((e) => e.value),
    activeEncoder: w.active_encoder ?? "",
    videoBitrateKbps: {
      min: w.video_bitrate?.min ?? 0,
      max: w.video_bitrate?.max ?? 0,
    },
    audioBitrateKbps: (w.audio_bitrate ?? []).map((e) => e.value),
    filters: inventoryOf(w, "filters"),
    sourceKinds: inventoryOf(w, "source_kinds"),
    destinationKinds: inventoryOf(w, "destination_kinds"),
    encoderFamilies: encoderFamiliesFromWire(w),
    audio: audioFromWire(w),
    graphicsAdapters: graphicsAdaptersFromWire(w),
    outputScales: outputScalesFromWire(w),
    recordContainers: inventoryOf(w, "record_container"),
    regimes,
  };
  if (colorSpace && range && format) caps.colorimetry = { colorSpace, range, format };
  const activeAdapter = entryNumber(w, "graphics_adapters", "active_index");
  if (activeAdapter !== undefined) caps.activeGraphicsAdapter = activeAdapter;
  const canvas = canvasFromWire(w);
  if (canvas) caps.canvas = canvas;
  const recordContainer = entryString(w, "record_container", "value");
  if (recordContainer) caps.recordContainer = recordContainer;
  const recordMarkers = recordMarkersFromWire(w);
  if (recordMarkers) caps.recordMarkers = recordMarkers;
  return caps;
}

/** Unwraps `capabilities.encoder_families.values`. A field Pulsar omitted stays
 *  omitted here -- it is never defaulted to a plausible value, so the consumer
 *  can tell "this family has no readable window" from "the window is 0". */
function encoderFamiliesFromWire(w: WireGetCapabilitiesResponse): EncoderFamilyCapability[] {
  const raw = w.capabilities?.["encoder_families"]?.["values"];
  if (!Array.isArray(raw)) return [];

  const out: EncoderFamilyCapability[] = [];
  for (const item of raw as WireEncoderFamily[]) {
    if (!item || typeof item.value !== "string") continue;
    const fam: EncoderFamilyCapability = { family: item.value };
    if (Array.isArray(item.presets)) fam.presets = item.presets.map((e) => e.value);
    if (Array.isArray(item.profiles)) fam.profiles = item.profiles.map((e) => e.value);
    if (Array.isArray(item.rate_controls))
      fam.rateControls = item.rate_controls.map((e) => e.value);
    if (item.keyint_sec) fam.keyintSec = windowFromWire(item.keyint_sec);
    if (item.bitrate) fam.bitrateKbps = windowFromWire(item.bitrate);
    out.push(fam);
  }
  return out;
}

function windowFromWire(w: { min?: number; max?: number; step?: number }): {
  min: number;
  max: number;
  step: number;
} {
  return { min: w.min ?? 0, max: w.max ?? 0, step: w.step ?? 1 };
}

export function videoPatchToWire(p: VideoSettingsPatch): WireSetVideoSettingsRequest {
  const w: WireSetVideoSettingsRequest = {};
  if (p.videoBitrate !== undefined) w.video_bitrate = p.videoBitrate;
  if (p.audioBitrate !== undefined) w.audio_bitrate = p.audioBitrate;
  return w;
}

export function videoPatchResultFromWire(w: WireSetVideoSettingsResponse): VideoSettingsPatchResult {
  const r: VideoSettingsPatchResult = { changed: w.changed ?? false };
  if (w.video_bitrate !== undefined) r.videoBitrate = w.video_bitrate;
  if (w.audio_bitrate !== undefined) r.audioBitrate = w.audio_bitrate;
  return r;
}

export function adaptiveStateFromWire(w: WireGetAdaptiveStateResponse): AdaptiveState {
  return {
    enabled: w.enabled ?? false,
    targetKbps: w.target_kbps ?? 0,
    currentKbps: w.current_kbps ?? 0,
    floorKbps: w.floor_kbps ?? 0,
    stableTicks: w.stable_ticks ?? 0,
    adjustmentsTotal: w.adjustments_total ?? 0,
    lastDeltaTotal: w.last_delta_total ?? 0,
    lastDeltaDropped: w.last_delta_dropped ?? 0,
    lastDropRatio: w.last_drop_ratio ?? 0,
  };
}

/**
 * Decodes `GetMonitoringDeviceList` (#173). An item without a usable id is
 * dropped rather than half-decoded: an id the client had to invent is one
 * `SetMonitoringDevice` would refuse, so offering it in a selector would show
 * a device that cannot be chosen.
 */
export function monitoringDeviceListFromWire(
  w: WireGetMonitoringDeviceListResponse,
): MonitoringDeviceList {
  const devices: MonitoringDevice[] = [];
  for (const item of Array.isArray(w.devices) ? w.devices : []) {
    if (!item || typeof item.id !== "string" || item.id === "") continue;
    devices.push({ id: item.id, name: typeof item.name === "string" ? item.name : "" });
  }
  const list: MonitoringDeviceList = { available: w.available === true, devices };
  if (w.active_device_id) list.activeDeviceId = w.active_device_id;
  if (w.active_device_name) list.activeDeviceName = w.active_device_name;
  return list;
}

function programAudioPtsFromWire(
  wire: WireProgramAudioPts | undefined,
  track?: WireProgramAudioTrack,
): ProgramAudioPts {
  const pts = wire ?? {};
  const series = Array.isArray(track?.pts_series_ns)
    ? track.pts_series_ns
    : Array.isArray(pts.series_ns)
      ? pts.series_ns
      : [];
  return {
    firstNs: track?.first_pts_ns ?? pts.first_ns ?? 0,
    lastNs: track?.last_pts_ns ?? pts.last_ns ?? 0,
    samples: track?.pts_samples ?? pts.samples ?? 0,
    regressions: track?.pts_regressions ?? pts.regressions ?? 0,
    monotone: track?.pts_monotone ?? pts.monotone ?? false,
    seriesNs: series
      .filter((item): item is { pts_ns: number } => typeof item?.pts_ns === "number")
      .map((item) => item.pts_ns),
  };
}

/** Decodes the explicit common Program audio route (#245). */
export function programAudioRouteFromWire(
  w: WireGetProgramAudioRouteResponse,
): ProgramAudioRoute {
  const outputs: ProgramAudioOutput[] = (Array.isArray(w.outputs) ? w.outputs : []).map((item) => ({
    output: item.output ?? "",
    id: item.id ?? "",
    name: item.name ?? "",
    audioSupported: item.audio_supported === true,
    audioIdentity: item.audio_identity ?? "",
    audioMatchesRoute: item.audio_matches_route === true,
    active: item.active === true,
    slots: (Array.isArray(item.slots) ? item.slots : [])
      .filter(
        (slot): slot is { slot: number; track: number; encoder: string } =>
          typeof slot?.slot === "number" &&
          typeof slot.track === "number" &&
          typeof slot.encoder === "string",
      )
      .map((slot) => ({ slot: slot.slot, track: slot.track, encoder: slot.encoder })),
  }));

  const sources: ProgramAudioSource[] = (Array.isArray(w.sources) ? w.sources : [])
    .filter(
      (item): item is { channel: number; identity: string; id: string; name: string } =>
        typeof item?.channel === "number" &&
        typeof item.identity === "string" &&
        typeof item.id === "string" &&
        typeof item.name === "string",
    )
    .map((item) => ({
      channel: item.channel,
      identity: item.identity,
      id: item.id,
      name: item.name,
    }));

  const tracks: ProgramAudioTrack[] = (Array.isArray(w.tracks) ? w.tracks : []).map((item) => {
    const pts = programAudioPtsFromWire(item.pts, item);
    return {
      track: item.track ?? 0,
      mixerIndex: item.mixer_index ?? 0,
      encoder: item.encoder ?? "",
      blocks: item.blocks ?? 0,
      frames: item.frames ?? 0,
      firstPtsNs: pts.firstNs,
      lastPtsNs: pts.lastNs,
      ptsSamples: pts.samples,
      ptsRegressions: pts.regressions,
      ptsMonotone: pts.monotone,
      pts,
      ptsSeriesNs: pts.seriesNs,
    };
  });

  const route: ProgramAudioRoute = {
    schemaVersion: w.schema_version ?? 0,
    routeId: w.route_id ?? "",
    routeName: w.route_name ?? "",
    scope: w.scope ?? "",
    cutAudioPolicy: w.cut_audio_policy ?? "",
    audioIdentity: w.audio_identity ?? "",
    stable: w.stable === true,
    previewAudioSupported: w.preview_audio_supported === true,
    afvSupported: w.afv_supported === true,
    observed: w.observed === true,
    outputs,
    sources,
    tracks,
    ptsMonotone: w.pts_monotone === true,
    ptsSamples: w.pts_samples ?? 0,
  };
  if (w.route_error) route.routeError = w.route_error;
  return route;
}

export function bitrateAdjustedFromWire(w: WireBitrateAdjustedEvent): BitrateAdjustedEvent {
  return {
    bitrate: w.bitrate,
    target: w.target,
    floor: w.floor,
    reason: w.reason,
    dropRatio: w.drop_ratio,
  };
}
