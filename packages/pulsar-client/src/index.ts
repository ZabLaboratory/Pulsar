// @zablaboratory/pulsar-client
//
// Typed TypeScript client for Pulsar -- the headless broadcast engine.
// See README.md for a full example.

export { PulsarClient } from "./client.js";
export { PulsarVendorError, PulsarNotConnectedError } from "./errors.js";
export { TypedEventEmitter } from "./events.js";
export type {
  AdaptiveState,
  BitrateAdjustedEvent,
  ConnectOptions,
  CreateDestinationInput,
  Destination,
  DestinationKind,
  OutputState,
  PulsarEventMap,
  PulsarEventName,
  RecordStateChangedEvent,
  StreamStateChangedEvent,
  StudioModeStateChangedEvent,
  VideoSettings,
  VideoSettingsPatch,
  VideoSettingsPatchResult,
} from "./types.js";
