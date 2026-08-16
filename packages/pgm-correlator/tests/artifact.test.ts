import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { buildArtifact, KNOWN_DRIFT_SOURCES, writeArtifact } from "../src/artifact.js";
import type { CorrelationRecord, StateEvent, ThresholdDerivation, VisualEvent } from "../src/types.js";

const state: StateEvent = {
  receivedAtMs: 100,
  frameType: "delta",
  sequence: 1,
  sceneId: "scene-x",
  identity: { correlationId: "corr-1" },
};
const visual: VisualEvent = { atMs: 150, ptsSeconds: 0.15, sceneScore: 0.4 };
const threshold: ThresholdDerivation = {
  method: "95th percentile of observed candidate-pair latency",
  sampleSize: 5,
  percentileUsed: 0.95,
  distributionMs: { min: 40, p50: 50, p90: 55, p95: 58, max: 60 },
  derivedThresholdMs: 58,
  fallbackUsed: false,
};

describe("artifact -- built object and its written form", () => {
  it("counts each category exactly once", () => {
    const records: CorrelationRecord[] = [
      { category: "matched", state, visual, latencyMs: 50 },
      { category: "state_without_visual", state, reason: "r1" },
      { category: "visual_without_state", visual, reason: "r2" },
    ];
    const artifact = buildArtifact({
      sessionId: "s1",
      recordingPath: "/tmp/rec.mp4",
      recordingContainer: "mp4",
      stateEvents: [state],
      visualEvents: [visual],
      records,
      threshold,
    });
    expect(artifact.counts).toEqual({ matched: 1, stateWithoutVisual: 1, visualWithoutState: 1 });
    expect(artifact.driftSources).toBe(KNOWN_DRIFT_SOURCES);
    expect(artifact.schemaVersion).toBe(1);
  });

  it("documents all four known drift sources by name", () => {
    const names = KNOWN_DRIFT_SOURCES.map((d) => d.name);
    expect(names).toEqual(
      expect.arrayContaining(["encoding_latency", "rtmp_buffer", "clock_skew", "scene_detection_sensitivity"]),
    );
    for (const d of KNOWN_DRIFT_SOURCES) expect(d.description.length).toBeGreaterThan(20);
  });
});

describe("writeArtifact -- real filesystem round-trip", () => {
  let dir: string;

  beforeEach(async () => {
    dir = await mkdtemp(path.join(tmpdir(), "pgm-correlator-artifact-"));
  });

  afterEach(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  it("writes correlation.jsonl as one record per line and summary.json without the records array", async () => {
    const records: CorrelationRecord[] = [
      { category: "matched", state, visual, latencyMs: 50 },
      { category: "state_without_visual", state, reason: "r1" },
    ];
    const artifact = buildArtifact({
      sessionId: "session-42",
      recordingPath: "/tmp/rec.mp4",
      recordingContainer: "mp4",
      stateEvents: [state],
      visualEvents: [visual],
      records,
      threshold,
    });

    const paths = await writeArtifact(dir, artifact);
    expect(paths.dir).toBe(path.join(dir, "session-42"));

    const jsonlRaw = await readFile(paths.recordsPath, "utf8");
    const lines = jsonlRaw.trim().split("\n");
    expect(lines).toHaveLength(2);
    expect(JSON.parse(lines[0]!)).toEqual(records[0]);
    expect(JSON.parse(lines[1]!)).toEqual(records[1]);

    const summaryRaw = await readFile(paths.summaryPath, "utf8");
    const summary = JSON.parse(summaryRaw) as Record<string, unknown>;
    expect(summary["records"]).toBeUndefined();
    expect(summary["counts"]).toEqual({ matched: 1, stateWithoutVisual: 1, visualWithoutState: 0 });
    expect(summary["threshold"]).toEqual(threshold);
    expect((summary["driftSources"] as unknown[]).length).toBe(KNOWN_DRIFT_SOURCES.length);
  });

  it("writes an empty correlation.jsonl (not a single blank line) when there are no records", async () => {
    const artifact = buildArtifact({
      sessionId: "session-empty",
      recordingPath: "/tmp/rec.mp4",
      recordingContainer: "mp4",
      stateEvents: [],
      visualEvents: [],
      records: [],
      threshold: { ...threshold, sampleSize: 0, distributionMs: null },
    });
    const paths = await writeArtifact(dir, artifact);
    const raw = await readFile(paths.recordsPath, "utf8");
    expect(raw).toBe("");
  });
});
