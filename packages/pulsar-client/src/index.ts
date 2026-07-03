// @clodocapeo/pulsar-client
//
// Typed TypeScript client for Pulsar -- the headless broadcast engine.
// See README.md for a full example.

export { PulsarClient } from "./client.js";
export { PulsarVendorError, PulsarNotConnectedError } from "./errors.js";
export { TypedEventEmitter } from "./events.js";
export type {
  AdaptiveState,
  AudioDevice,
  AudioInput,
  BitrateAdjustedEvent,
  ConnectOptions,
  CreateDestinationInput,
  Destination,
  DestinationKind,
  EncoderFamily,
  InputMuteStateChangedEvent,
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
