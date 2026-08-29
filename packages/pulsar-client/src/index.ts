// @clodocapeo/pulsar-client
//
// Typed TypeScript client for Pulsar -- the headless broadcast engine.
// See README.md for a full example.

export { PulsarClient } from "./client.js";
export {
  PulsarVendorError,
  PulsarNotConnectedError,
  PulsarRuntimeError,
  type PulsarPrismErrorEnvelope,
} from "./errors.js";
export type { PulsarPrismLogEvent } from "./types.js";
export { TypedEventEmitter } from "./events.js";
export type {
  AdaptiveState,
  AudioCapabilities,
  AudioDevice,
  AudioInput,
  AudioMonitoringCapability,
  BitrateAdjustedEvent,
  // Exported alongside the audio block: PulsarCapabilities.regimes is typed with
  // it, so a consumer could not name the type it already receives (#141 gap).
  CapabilityRegime,
  ConnectOptions,
  CreateDestinationInput,
  Destination,
  DestinationKind,
  EncoderFamily,
  // Exported alongside the adapters/scales block (#159): both are reachable
  // from PulsarCapabilities, so a consumer must be able to name them.
  GraphicsAdapter,
  InputMuteStateChangedEvent,
  // Exported alongside the monitoring selector (#173): both are returned by
  // audio.listMonitoringDevices / setMonitoringDevice.
  MonitoringDevice,
  MonitoringDeviceList,
  OutputScale,
  ProgramAudioOutput,
  ProgramAudioPts,
  ProgramAudioRoute,
  ProgramAudioSource,
  ProgramAudioTrack,
  OutputState,
  PulsarCapabilities,
  PulsarEventMap,
  PulsarEventName,
  RecordStateChangedEvent,
  SpecialInputs,
  StreamStateChangedEvent,
  StudioModeStateChangedEvent,
  VideoSettings,
  VideoSettingsPatch,
  VideoSettingsPatchResult,
} from "./types.js";
