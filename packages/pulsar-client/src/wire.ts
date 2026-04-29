// snake_case <-> camelCase mapping for vendor payloads.
//
// Pulsar's C++ vendor handlers serialize obs_data_t fields with the
// snake_case names the libobs C API expects. We keep the public TS API
// camelCase to match JS conventions; the mapping is centralized here so
// adding a vendor field touches exactly one place.

import type {
  AdaptiveState,
  BitrateAdjustedEvent,
  CreateDestinationInput,
  Destination,
  DestinationKind,
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
  };
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

export function bitrateAdjustedFromWire(w: WireBitrateAdjustedEvent): BitrateAdjustedEvent {
  return {
    bitrate: w.bitrate,
    target: w.target,
    floor: w.floor,
    reason: w.reason,
    dropRatio: w.drop_ratio,
  };
}
