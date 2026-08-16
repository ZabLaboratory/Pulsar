// @clodocapeo/capture-pgm-compat -- proves Pulsar's capture path and its
// recorded PGM are compatible: what a real, rendered source produces is what
// gets recorded, measurably, and a source that reports OK while being
// visually dead (black or frozen -- the "faux positif CEF" risk named in
// ZabLaboratory/Pulsar#231) is caught rather than accepted. See README.md.

export {
  measureFrameHealth,
  type FrameStats,
  type TemporalDiff,
  type HealthMeasurement,
  type MeasureFrameHealthOptions,
} from "./frame-health.js";
export {
  deriveSeparationThreshold,
  passesThreshold,
  type SeparationThreshold,
} from "./threshold.js";
export { startTestPageServer, type TestPageServer } from "./test-page-server.js";
export {
  runLiveCaptureSession,
  type Scenario,
  type LiveCaptureRecording,
  type RunLiveCaptureSessionOptions,
  type LiveCaptureSessionResult,
} from "./live-capture-session.js";
