// Shared types for the PGM correlator (ZabLaboratory/Pulsar#230,
// ADR-BLUE-012 R6 §16.1). Correlation is by TIME, never by pixel: no
// correlation_id is ever recoverable from the recorded frames themselves
// (confirmed impossible short of a Solar burn-in, out of scope here --
// see Orion#334 / the Conduit audit this unit's bail cites).

/** The six optional, non-semantic identity fields Orion's LSDP kit stamps
 *  on a Delta or Snapshot frame (lumencast-go protocol.ProjectionMetadata,
 *  pinned at ZabLaboratory/Orion go.mod `0c7cfc65c694`). All optional on
 *  the wire; a frame carrying none of them carries no correlatable identity. */
export interface ProjectionIdentity {
  schemaVersion?: string;
  sceneDigest?: string;
  runtimeInstanceId?: string;
  target?: string;
  renderRevision?: string;
  correlationId?: string;
}

/** One LSDP frame that carried at least one identity field, as observed by
 *  a passive WS viewer connection. This is the "intention / desired state"
 *  half of the correlation -- never the "what actually got painted" half. */
export interface StateEvent {
  /** Wall-clock ms (this process' clock) at WS message receipt. Not
   *  Orion's own clock -- see `clock_skew` in artifact.ts. */
  receivedAtMs: number;
  frameType: "delta" | "snapshot";
  sequence: number;
  sceneId: string;
  identity: ProjectionIdentity;
}

/** One detected visual change inside a real recorded file, as produced by
 *  ffmpeg's scene-detection filter. This is the "what actually got
 *  painted" half -- the PGM half. It carries no identity: pixels don't. */
export interface VisualEvent {
  /** Wall-clock ms this visual change is estimated to have occurred at:
   *  recordingStartMs + ptsSeconds*1000. Only as accurate as that anchor
   *  (see record-session.ts) plus the encoder's own PTS discipline. */
  atMs: number;
  ptsSeconds: number;
  /** ffmpeg `scene` filter score in [0,1] for the frame that triggered
   *  this event -- higher means "more different from the previous frame". */
  sceneScore: number;
}

/**
 * A state event and a visual event the correlator paired within the
 * derived acceptance threshold.
 */
export interface CorrelationMatch {
  category: "matched";
  state: StateEvent;
  visual: VisualEvent;
  /** visual.atMs - state.receivedAtMs. Can be slightly negative under the
   *  configured clock-skew allowance; never a claim that the visual
   *  preceded its cause by more than that allowance. */
  latencyMs: number;
}

/**
 * A state event with no visual counterpart within the derived threshold.
 * EXPECTED, not necessarily an error: a Delta can patch a leaf that
 * renders to the same pixels (identical value, an off-screen leaf, a
 * non-visual leaf).
 */
export interface CorrelationOrphanState {
  category: "state_without_visual";
  state: StateEvent;
  reason: string;
}

/**
 * A visual change with no state counterpart within the derived threshold.
 * EXPECTED, not necessarily an error: a running animation, a playing video
 * source, or a transition is a real visual change with no corresponding
 * LSDP delta of its own.
 */
export interface CorrelationOrphanVisual {
  category: "visual_without_state";
  visual: VisualEvent;
  reason: string;
}

export type CorrelationRecord = CorrelationMatch | CorrelationOrphanState | CorrelationOrphanVisual;

export interface DriftSource {
  name: string;
  description: string;
}

/** How the correlator's acceptance time-window was arrived at. Never a
 *  bare number without this record: a threshold that hides its own
 *  provenance is exactly what this unit exists to avoid. */
export interface ThresholdDerivation {
  method: string;
  /** Number of candidate pairs the distribution below was computed from. */
  sampleSize: number;
  percentileUsed?: number;
  distributionMs: { min: number; p50: number; p90: number; p95: number; max: number } | null;
  derivedThresholdMs: number;
  fallbackUsed: boolean;
  fallbackReasonIfUsed?: string;
}

export interface CorrelationCounts {
  matched: number;
  stateWithoutVisual: number;
  visualWithoutState: number;
}

export interface CorrelationArtifact {
  schemaVersion: 1;
  sessionId: string;
  createdAtIso: string;
  recording: { path: string; container: string };
  stateEvents: StateEvent[];
  visualEvents: VisualEvent[];
  records: CorrelationRecord[];
  threshold: ThresholdDerivation;
  driftSources: DriftSource[];
  counts: CorrelationCounts;
}
