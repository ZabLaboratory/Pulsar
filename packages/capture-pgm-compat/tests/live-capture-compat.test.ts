// The real integration proof: a REAL full Pulsar (pulsar.exe + CEF) is
// spawned, a REAL browser_source captures a REAL rendered page, a REAL
// x264 recording is produced for each of the three named scenarios
// (healthy / black / frozen), and the result is measured with the SAME
// measureFrameHealth used by frame-health.test.ts's ffmpeg-fixture proof.
// It also cross-checks pgm-correlator's (#230) extractVisualEvents verdict
// against these same three real recordings -- concordance is the
// capture<->PGM compatibility proof this unit exists to produce; a
// divergence is a real #230 finding to report, not something this unit
// corrects (#230 is merged, out of this unit's repo-scoped authority).
//
// Opt-in only -- see README.md "CI gap" for why: no CI job in this repo
// runs on a Windows runner capable of executing a CEF binary, and adding
// one is Keeper's call, not Probe's. Enable locally with:
//
//   PULSAR_LIVE_CAPTURE_COMPAT=1 npm test -w @clodocapeo/capture-pgm-compat
//
// Optionally override the binaries directory (useful when this package's
// own postinstall hasn't downloaded the ~150MB full bundle, e.g. a
// worktree checkout that reuses an already-downloaded sibling checkout):
//
//   PULSAR_BUNDLE_FULL_BINARIES_PATH=D:/path/to/pulsar-bundle-full/binaries

import { describe, expect, it } from "vitest";
import { extractVisualEvents } from "@clodocapeo/pgm-correlator";

import { runLiveCaptureSession, type Scenario } from "../src/live-capture-session.js";
import { measureFrameHealth, type HealthMeasurement } from "../src/frame-health.js";
import { deriveSeparationThreshold, passesThreshold } from "../src/threshold.js";

const ENABLED = process.env.PULSAR_LIVE_CAPTURE_COMPAT === "1";

describe.skipIf(!ENABLED)("real Pulsar/CEF capture <-> PGM compatibility (opt-in, PULSAR_LIVE_CAPTURE_COMPAT=1)", () => {
  it(
    "records healthy/black/frozen for real, measures them, and cross-checks pgm-extractor's visual-presence verdict",
    async () => {
      const binariesPath = process.env.PULSAR_BUNDLE_FULL_BINARIES_PATH;
      const sessionOpts = {
        recordDurationMs: 3000,
        settleMs: 1500,
        onLog: (stream: "stdout" | "stderr", line: string) => {
          // eslint-disable-next-line no-console
          console.log(`[pulsar:${stream}] ${line}`);
        },
        ...(binariesPath !== undefined ? { pulsarBinariesPath: binariesPath } : {}),
      };

      const session = await runLiveCaptureSession(sessionOpts);
      // eslint-disable-next-line no-console
      console.log("LIVE_CAPTURE_SESSION", JSON.stringify(session, null, 2));

      const measurements = new Map<Scenario, HealthMeasurement>();
      const visualEventCounts = new Map<Scenario, number>();

      for (const rec of session.recordings) {
        const m = await measureFrameHealth(rec.path);
        measurements.set(rec.scenario, m);
        // eslint-disable-next-line no-console
        console.log(
          `FRAME_HEALTH scenario=${rec.scenario} path=${rec.path} frameCount=${m.frameCount} ` +
            `meanLumaAvg=${m.meanLumaAvg.toFixed(3)} spatialStddevAvg=${m.spatialStddevAvg.toFixed(3)} ` +
            `temporalDiffAvg=${m.temporalDiffAvg.toFixed(3)}`,
        );

        // recordingStartMs=0 -- only relative ptsSeconds matter for this
        // presence check, not wall-clock anchoring (no Orion state stream
        // in this unit's scope; see #230's record-session.ts for the real
        // anchor derivation this unit deliberately doesn't reimplement).
        const { visualEvents, ffmpegStderrRaw } = await extractVisualEvents(rec.path, 0);
        visualEventCounts.set(rec.scenario, visualEvents.length);
        // eslint-disable-next-line no-console
        console.log(
          `PGM_EXTRACTOR scenario=${rec.scenario} visualEventCount=${visualEvents.length}\n` +
            `--- ffmpeg stderr (real, verbatim) ---\n${ffmpegStderrRaw}\n--- end ---`,
        );
      }

      const healthy = measurements.get("healthy")!;
      const black = measurements.get("black")!;
      const frozen = measurements.get("frozen")!;
      expect(healthy).toBeDefined();
      expect(black).toBeDefined();
      expect(frozen).toBeDefined();

      // --- Axis 1: spatial+temporal health oracle, thresholds derived
      // from the actual separation observed in THIS real session (not the
      // ffmpeg-fixture numbers from frame-health.test.ts -- CEF's real
      // encode has its own noise floor). Each axis is validated against
      // only the scenario it exists to catch -- spatial vs "black" (the
      // scenario that IS spatially degraded), temporal vs "black"+"frozen"
      // (both are temporally dead); pooling "frozen" into the spatial
      // population would pull that threshold toward the healthy sample
      // itself, since a frozen source is spatially healthy by definition.
      // See threshold.ts's SeparationThreshold docstring. ---
      const spatialThreshold = deriveSeparationThreshold([healthy.spatialStddevAvg], [black.spatialStddevAvg]);
      const temporalThreshold = deriveSeparationThreshold(
        [healthy.temporalDiffAvg],
        [black.temporalDiffAvg, frozen.temporalDiffAvg],
      );
      // eslint-disable-next-line no-console
      console.log("SPATIAL_THRESHOLD", JSON.stringify(spatialThreshold, null, 2));
      // eslint-disable-next-line no-console
      console.log("TEMPORAL_THRESHOLD", JSON.stringify(temporalThreshold, null, 2));

      const isHealthy = (m: HealthMeasurement) =>
        passesThreshold(m.spatialStddevAvg, spatialThreshold) && passesThreshold(m.temporalDiffAvg, temporalThreshold);

      expect(isHealthy(healthy)).toBe(true);
      expect(isHealthy(black)).toBe(false);
      expect(isHealthy(frozen)).toBe(false);

      // --- Axis 2: capture <-> PGM compatibility via #230's own verdict.
      // A steady-state (post-settle) recording of a genuinely static scene
      // (black OR frozen) SHOULD show ~0 scene-change events; the animated
      // healthy scene SHOULD show real ones. If this diverges -- in
      // particular if pgm-extractor claims visual presence on black or
      // frozen -- that is a real #230 finding, reported as-is (see
      // README.md), not weakened here to force a pass. ---
      expect(visualEventCounts.get("healthy")).toBeGreaterThan(0);
      expect(visualEventCounts.get("black")).toBe(0);
      expect(visualEventCounts.get("frozen")).toBe(0);
    },
    120_000,
  );
});
