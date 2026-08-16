import { describe, expect, it } from "vitest";

import { correlate } from "../src/correlator.js";
import type { StateEvent, VisualEvent } from "../src/types.js";

function state(receivedAtMs: number, sequence: number): StateEvent {
  return {
    receivedAtMs,
    frameType: "delta",
    sequence,
    sceneId: "scene-x",
    identity: { correlationId: `corr-${sequence}` },
  };
}

function visual(atMs: number, ptsSeconds: number, sceneScore = 0.5): VisualEvent {
  return { atMs, ptsSeconds, sceneScore };
}

describe("correlate -- three categories, never two", () => {
  it("matches a state event to its nearest visual event within the derived threshold", () => {
    // Six pairs at increasing latency to give the derivation pass a real
    // sample (minSampleForDerivation defaults to 5).
    const latencies = [40, 50, 60, 70, 80, 90];
    const stateEvents = latencies.map((_, i) => state(1000 + i * 1000, i));
    const visualEvents = latencies.map((lat, i) => visual(1000 + i * 1000 + lat, i));

    const { records, threshold } = correlate(stateEvents, visualEvents);

    expect(threshold.fallbackUsed).toBe(false);
    expect(threshold.sampleSize).toBe(6);
    expect(threshold.distributionMs).not.toBeNull();
    expect(threshold.distributionMs!.min).toBe(40);
    expect(threshold.distributionMs!.max).toBe(90);
    // p95 of 6 sorted values [40,50,60,70,80,90]: ceil(0.95*6)=6 -> index 5 -> 90.
    expect(threshold.derivedThresholdMs).toBe(90);

    const matched = records.filter((r) => r.category === "matched");
    expect(matched).toHaveLength(6);
    expect(records.filter((r) => r.category === "state_without_visual")).toHaveLength(0);
    expect(records.filter((r) => r.category === "visual_without_state")).toHaveLength(0);
  });

  it("classifies a state change with no visible effect as state_without_visual, not an error count", () => {
    // 5 real pairs to derive a tight threshold, plus one lone state event
    // far past any of them -- e.g. a delta that patched a non-rendered leaf.
    const stateEvents = [0, 1000, 2000, 3000, 4000].map((t, i) => state(t, i));
    const visualEvents = [0, 1000, 2000, 3000, 4000].map((t, i) => visual(t + 50, i));
    const lonelyState = state(100_000, 99);

    const { records, threshold } = correlate([...stateEvents, lonelyState], visualEvents);

    expect(threshold.derivedThresholdMs).toBeLessThan(1000); // tight, derived from ~50ms pairs
    const orphanStates = records.filter((r) => r.category === "state_without_visual");
    expect(orphanStates).toHaveLength(1);
    expect((orphanStates[0] as { state: StateEvent }).state.sequence).toBe(99);
    expect((orphanStates[0] as { reason: string }).reason).toMatch(/not necessarily a correlation failure/);
  });

  it("classifies a visual change with no preceding state as visual_without_state (e.g. a running animation)", () => {
    const stateEvents = [0, 1000, 2000, 3000, 4000].map((t, i) => state(t, i));
    const visualEvents = [0, 1000, 2000, 3000, 4000].map((t, i) => visual(t + 50, i));
    const lonelyVisual = visual(200_000, 200);

    const { records } = correlate(stateEvents, [...visualEvents, lonelyVisual]);

    const orphanVisuals = records.filter((r) => r.category === "visual_without_state");
    expect(orphanVisuals).toHaveLength(1);
    expect((orphanVisuals[0] as { visual: VisualEvent }).visual.ptsSeconds).toBe(200);
  });

  it("falls back to a documented default threshold and says so when the sample is too small", () => {
    const stateEvents = [state(0, 0), state(1000, 1)];
    const visualEvents = [visual(50, 0), visual(1050, 1)];

    const { threshold } = correlate(stateEvents, visualEvents);

    expect(threshold.fallbackUsed).toBe(true);
    expect(threshold.method).toBe("fallback (insufficient sample)");
    expect(threshold.derivedThresholdMs).toBe(1000); // documented default
    expect(threshold.fallbackReasonIfUsed).toMatch(/stated placeholder, not a measurement/);
    expect(threshold.percentileUsed).toBeUndefined();
  });

  it("accepts a small negative latency (clock skew allowance) but rejects a larger one", () => {
    // 5 pairs with a tiny negative latency (visual apparently 20ms before
    // its state event -- within the default 200ms skew allowance).
    const stateEvents = [0, 1000, 2000, 3000, 4000].map((t, i) => state(t, i));
    const visualEvents = [0, 1000, 2000, 3000, 4000].map((t, i) => visual(t - 20, i));

    const { records } = correlate(stateEvents, visualEvents);
    expect(records.filter((r) => r.category === "matched")).toHaveLength(5);

    // A visual event 500ms BEFORE its nearest state event, well past the
    // 200ms skew allowance, must not match it.
    const skewedVisual = visual(-500, 999);
    const { records: records2 } = correlate([state(0, 0)], [skewedVisual]);
    expect(records2.filter((r) => r.category === "matched")).toHaveLength(0);
    expect(records2.filter((r) => r.category === "visual_without_state")).toHaveLength(1);
  });
});
