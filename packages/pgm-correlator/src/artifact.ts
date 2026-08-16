import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import type {
  CorrelationArtifact,
  CorrelationRecord,
  DriftSource,
  StateEvent,
  ThresholdDerivation,
  VisualEvent,
} from "./types.js";

/**
 * Known sources of drift between a state event's timestamp and a visual
 * event's timestamp, documented in the artifact itself rather than only in
 * a code comment -- a reader of the JSON should be able to say "this ±X"
 * and know where X comes from without opening this file.
 */
export const KNOWN_DRIFT_SOURCES: DriftSource[] = [
  {
    name: "encoding_latency",
    description:
      "Time between a frame being composited and the encoder emitting its encoded bytes into " +
      "the recorded file. Bounded by the encoder's keyint/GOP structure and, for hardware " +
      "encoders, driver queueing. Not measured directly by this tool -- it is one of the " +
      "components the derived threshold (threshold.derivedThresholdMs) absorbs.",
  },
  {
    name: "rtmp_buffer",
    description:
      "Does not apply to the recordings this tool currently produces: the media source is a " +
      "local StartRecord capture (pulsar-client RecordNamespace), whose muxer writes directly, " +
      "bypassing any RTMP send buffer. Listed because a future artifact built from an RTMP " +
      "destination instead would need to account for it explicitly.",
  },
  {
    name: "clock_skew",
    description:
      "The state-event timestamp (StateEvent.receivedAtMs) is this process' own wall clock at " +
      "WS message receipt. The visual-event timestamp (VisualEvent.atMs) is derived from the " +
      "recording's start anchor plus the frame's PTS, timed by Pulsar's own encoder clock. The " +
      "two clocks are not synchronized against each other. `clockSkewAllowanceMs` bounds how far " +
      "a visual event may appear to precede its causing state event before it is rejected as a " +
      "correlation candidate.",
  },
  {
    name: "scene_detection_sensitivity",
    description:
      "The ffmpeg scene-change score cutoff (pgm-extractor.ts `sceneThreshold`) trades false " +
      "negatives (a real but subtle visual change scored below threshold, surfacing as a spurious " +
      "state_without_visual) against false positives (encoder noise scored as a change). It is a " +
      "property of the visual-event extractor, not of the correlator's derived time threshold, " +
      "and is reported separately in the artifact's `recording` block via the command actually run.",
  },
];

export function buildArtifact(input: {
  sessionId: string;
  recordingPath: string;
  recordingContainer: string;
  stateEvents: StateEvent[];
  visualEvents: VisualEvent[];
  records: CorrelationRecord[];
  threshold: ThresholdDerivation;
}): CorrelationArtifact {
  let matched = 0;
  let stateWithoutVisual = 0;
  let visualWithoutState = 0;
  for (const r of input.records) {
    if (r.category === "matched") matched++;
    else if (r.category === "state_without_visual") stateWithoutVisual++;
    else visualWithoutState++;
  }

  return {
    schemaVersion: 1,
    sessionId: input.sessionId,
    createdAtIso: new Date().toISOString(),
    recording: { path: input.recordingPath, container: input.recordingContainer },
    stateEvents: input.stateEvents,
    visualEvents: input.visualEvents,
    records: input.records,
    threshold: input.threshold,
    driftSources: KNOWN_DRIFT_SOURCES,
    counts: { matched, stateWithoutVisual, visualWithoutState },
  };
}

export interface WrittenArtifact {
  dir: string;
  recordsPath: string;
  summaryPath: string;
}

/**
 * Writes the artifact under `<baseDir>/<sessionId>/`:
 *  - `correlation.jsonl` -- one CorrelationRecord per line, replayable and
 *    greppable without parsing the whole file.
 *  - `summary.json` -- everything else (threshold derivation, drift
 *    sources, counts, state/visual events, recording pointer).
 */
export async function writeArtifact(baseDir: string, artifact: CorrelationArtifact): Promise<WrittenArtifact> {
  const dir = path.join(baseDir, artifact.sessionId);
  await mkdir(dir, { recursive: true });

  const recordsPath = path.join(dir, "correlation.jsonl");
  const lines = artifact.records.map((r) => JSON.stringify(r)).join("\n");
  await writeFile(recordsPath, artifact.records.length ? lines + "\n" : "", "utf8");

  const summaryPath = path.join(dir, "summary.json");
  const { records: _records, ...summary } = artifact;
  await writeFile(summaryPath, JSON.stringify(summary, null, 2) + "\n", "utf8");

  return { dir, recordsPath, summaryPath };
}
