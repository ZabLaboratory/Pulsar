// @clodocapeo/pulsar-client
//
// Typed TypeScript client for Pulsar -- the headless broadcast engine.
// See README.md for a full example.

export { PulsarClient } from "./client.js";
export { PulsarVendorError, PulsarNotConnectedError } from "./errors.js";
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
  OutputScale,
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
