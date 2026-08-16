// frame-health + threshold: unit-level proof against REAL ffmpeg-generated
// fixtures (real spawned ffmpeg, real encoded bytes -- same posture as
// pgm-correlator's own pgm-extractor.test.ts), not mocked math. This suite
// runs everywhere ffmpeg is available (it is, on every runner this repo's
// CI already uses for TS packages -- see README.md's CI-gap note) and does
// NOT need Pulsar: it proves the MEASURE itself discriminates the three
// axes it's meant to, using synthetic-but-real content standing in for
// each of #231's named scenarios:
//
//   - "healthy"-shaped: `testsrc` -- a real animated pattern. Non-zero
//     spatialStddev (real per-frame detail) AND non-zero temporalDiff
//     (frame-to-frame motion).
//   - "black"-shaped: `color=c=black` -- a real flat, unchanging frame.
//     ~zero spatialStddev, ~zero meanLuma, ~zero temporalDiff.
//   - "frozen"-shaped: one real frame EXTRACTED from the same `testsrc`
//     stream, then held static for the clip's whole duration. Same order
//     of magnitude of spatial detail as "healthy" (it's a real sample of
//     the same content) by construction, but ~zero temporalDiff -- the
//     case a spatial-only oracle cannot tell apart from "healthy" at all.
//
// The real Pulsar/CEF integration proof (live-capture-compat.test.ts)
// exercises the SAME measureFrameHealth/deriveSeparationThreshold against
// real Pulsar recordings; this suite is what keeps the measure itself
// under CI even where the full integration can't run.

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { measureFrameHealth } from "../src/frame-health.js";
import { deriveSeparationThreshold, passesThreshold } from "../src/threshold.js";

function buildFixture(args: string[], outPath: string): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const child = spawn("ffmpeg", ["-y", ...args, outPath], { windowsHide: true });
    let stderr = "";
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    child.once("error", reject);
    child.once("close", (code) =>
      code === 0 ? resolve() : reject(new Error(`ffmpeg fixture build exited ${code}: ${stderr}`)),
    );
  });
}

describe("measureFrameHealth -- real ffmpeg fixtures for the three named scenarios", () => {
  let dir: string;
  let healthyPath: string;
  let blackPath: string;
  let frozenPath: string;

  beforeAll(async () => {
    dir = await mkdtemp(path.join(tmpdir(), "capture-pgm-compat-fixture-"));
    healthyPath = path.join(dir, "healthy.mp4");
    blackPath = path.join(dir, "black.mp4");
    frozenPath = path.join(dir, "frozen.mp4");
    const framePng = path.join(dir, "frozen-frame.png");

    await buildFixture(
      ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2", "-pix_fmt", "yuv420p"],
      healthyPath,
    );
    await buildFixture(
      ["-f", "lavfi", "-i", "color=c=black:size=320x240:rate=10:duration=2", "-pix_fmt", "yuv420p"],
      blackPath,
    );
    // "frozen": extract ONE real frame from the exact same testsrc stream,
    // then hold it static for 2s. By construction this has the same order
    // of magnitude of spatial detail as "healthy" -- proving the spatial
    // axis genuinely cannot discriminate this case, not by picking an
    // unrelated low-detail static fixture that would discriminate by
    // accident. This is the ffmpeg-fixture analogue of the "frozen"
    // browser page (real paint once, then no further updates).
    await buildFixture(["-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-frames:v", "1"], framePng);
    await buildFixture(["-loop", "1", "-i", framePng, "-t", "2", "-r", "10", "-pix_fmt", "yuv420p"], frozenPath);
  }, 30_000);

  afterAll(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  it("scores the animated fixture with non-trivial spatial AND temporal variance", async () => {
    const m = await measureFrameHealth(healthyPath);
    expect(m.frameCount).toBeGreaterThan(1);
    expect(m.spatialStddevAvg).toBeGreaterThan(5);
    expect(m.temporalDiffAvg).toBeGreaterThan(1);
  });

  it("scores the black fixture as flat on every axis (spatial, luma, temporal)", async () => {
    const m = await measureFrameHealth(blackPath);
    expect(m.spatialStddevAvg).toBeLessThan(2);
    expect(m.meanLumaAvg).toBeLessThan(5);
    expect(m.temporalDiffAvg).toBeLessThan(1);
  });

  it("scores the static-but-detailed fixture as spatially healthy but temporally dead -- the case a spatial-only oracle would miss", async () => {
    const m = await measureFrameHealth(frozenPath);
    expect(m.spatialStddevAvg).toBeGreaterThan(5);
    expect(m.temporalDiffAvg).toBeLessThan(1);
  });

  it("a combined spatial+temporal oracle accepts the animated fixture and rejects BOTH degraded fixtures, from thresholds derived from the actual observed separation", async () => {
    const healthy = await measureFrameHealth(healthyPath);
    const black = await measureFrameHealth(blackPath);
    const frozen = await measureFrameHealth(frozenPath);

    // Each axis's threshold is derived against ONLY the degraded scenario
    // it exists to catch -- spatial vs "black" (the scenario that is
    // actually spatially degraded), temporal vs BOTH "black" and "frozen"
    // (both are temporally dead). Pooling "frozen" into the spatial
    // population would be wrong: frozen is spatially healthy BY
    // CONSTRUCTION (a real frame of the same content) and pollutes that
    // axis's threshold toward the healthy sample itself -- exactly the
    // false-negative-on-healthy failure mode a naive combined threshold
    // would produce. See threshold.ts's SeparationThreshold docstring.
    const spatialThreshold = deriveSeparationThreshold([healthy.spatialStddevAvg], [black.spatialStddevAvg]);
    expect(spatialThreshold.separated).toBe(true);
    expect(passesThreshold(healthy.spatialStddevAvg, spatialThreshold)).toBe(true);
    expect(passesThreshold(black.spatialStddevAvg, spatialThreshold)).toBe(false);
    // frozen legitimately passes the spatial axis alone -- that's the
    // whole point: it is NOT spatially degraded, only temporally.
    expect(passesThreshold(frozen.spatialStddevAvg, spatialThreshold)).toBe(true);

    const temporalThreshold = deriveSeparationThreshold(
      [healthy.temporalDiffAvg],
      [black.temporalDiffAvg, frozen.temporalDiffAvg],
    );
    expect(temporalThreshold.separated).toBe(true);
    expect(passesThreshold(healthy.temporalDiffAvg, temporalThreshold)).toBe(true);
    expect(passesThreshold(black.temporalDiffAvg, temporalThreshold)).toBe(false);
    expect(passesThreshold(frozen.temporalDiffAvg, temporalThreshold)).toBe(false);

    // The combined oracle (both axes must pass) is what #231's checkpoint
    // review required: spatial alone would wrongly accept "frozen";
    // temporal alone can't see "black" is ALSO spatially flat. Together
    // they reject both degraded scenarios and accept only the real one.
    const isHealthy = (m: { spatialStddevAvg: number; temporalDiffAvg: number }) =>
      passesThreshold(m.spatialStddevAvg, spatialThreshold) && passesThreshold(m.temporalDiffAvg, temporalThreshold);
    expect(isHealthy(healthy)).toBe(true);
    expect(isHealthy(black)).toBe(false);
    expect(isHealthy(frozen)).toBe(false);
  });
});

describe("deriveSeparationThreshold", () => {
  it("reports separated=false, not a forced threshold, when populations overlap", () => {
    const t = deriveSeparationThreshold([10, 12], [8, 20]);
    expect(t.separated).toBe(false);
    expect(t.degradedMax).toBe(20);
    expect(t.healthyMin).toBe(10);
  });

  it("throws rather than silently deriving from an empty population", () => {
    expect(() => deriveSeparationThreshold([], [1])).toThrow();
    expect(() => deriveSeparationThreshold([1], [])).toThrow();
  });
});
