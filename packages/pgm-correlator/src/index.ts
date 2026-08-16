export { OrionObserver, type OrionObserverOptions } from "./orion-observer.js";
export {
  extractVisualEvents,
  parseSceneDetectionOutput,
  probeFile,
  type ExtractResult,
  type PgmExtractorOptions,
} from "./pgm-extractor.js";
export { correlate, type CorrelateOptions } from "./correlator.js";
export { buildArtifact, writeArtifact, KNOWN_DRIFT_SOURCES, type WrittenArtifact } from "./artifact.js";
export { recordCorrelatedSession, type RecordCorrelatedSessionOptions, type RecordCorrelatedSessionResult } from "./record-session.js";
export type {
  CorrelationArtifact,
  CorrelationCounts,
  CorrelationMatch,
  CorrelationOrphanState,
  CorrelationOrphanVisual,
  CorrelationRecord,
  DriftSource,
  ProjectionIdentity,
  StateEvent,
  ThresholdDerivation,
  VisualEvent,
} from "./types.js";
