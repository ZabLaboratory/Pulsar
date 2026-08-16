// pgm-extractor tests. Two tiers, deliberately separated:
//
//  - "parser" tests feed a captured real ffmpeg stderr transcript (see the
//    literal below -- captured verbatim from a real run during this unit's
//    development, not authored by hand) through parseSceneDetectionOutput.
//    Deterministic, no process spawn.
//  - "real ffmpeg/ffprobe" tests spawn the actual binaries against a real,
//    freshly generated video file. This proves the extractor's command
//    line and parsing work against real bytes and real output -- it does
//    NOT prove anything about a live Pulsar recording of a real antenna
//    feed. See record-session.ts's doc comment for that boundary.

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { extractVisualEvents, parseSceneDetectionOutput, probeFile } from "../src/pgm-extractor.js";

// Captured verbatim from:
//   ffmpeg -i fixture.mp4 -vf "select='gt(scene,0.1)',metadata=print,showinfo" -f null -
// against a real 4s fixture (2s libavfilter testsrc + 2s solid red,
// concatenated), during development of this unit (2026-08-16, ffmpeg
// 8.1.1-full_build, Windows).
const REAL_FFMPEG_STDERR_TRANSCRIPT = `
[Parsed_showinfo_2 @ 000002d062f34c80] config in time_base: 1/10240, frame_rate: 10/1
[Parsed_showinfo_2 @ 000002d062f34c80] config out time_base: 0/0, frame_rate: 0/0
[Parsed_metadata_1 @ 000002d062f34300] lavfi.scene_score=0.673662
[Parsed_showinfo_2 @ 000002d062f34c80] n:   0 pts:  20480 pts_time:2       duration:   1024 duration_time:0.1     fmt:yuv420p cl:left sar:1/1 s:320x240 i:P iskey:0 type:B checksum:D71A5A31 plane_checksum:[EFD7F182 22B45F86 6788091A] mean:[81 90 239] stdev:[0.0 0.0 0.0]
[Parsed_showinfo_2 @ 000002d062f34c80] color_range:unknown color_space:unknown color_primaries:unknown color_trc:unknown
`;

describe("parseSceneDetectionOutput -- parser (real captured ffmpeg transcript)", () => {
  it("pairs a lavfi.scene_score line with the showinfo line that follows it", () => {
    const events = parseSceneDetectionOutput(REAL_FFMPEG_STDERR_TRANSCRIPT, /* recordingStartMs */ 1_000_000);
    expect(events).toHaveLength(1);
    expect(events[0]!.ptsSeconds).toBe(2);
    expect(events[0]!.sceneScore).toBeCloseTo(0.673662, 6);
    expect(events[0]!.atMs).toBe(1_000_000 + 2000);
  });

  it("never confuses a duration_time: field for a pts_time: field", () => {
    // The real transcript's showinfo line ALSO contains "duration_time:0.1"
    // immediately after "pts_time:2" -- if the regex were not anchored to
    // the "pts_time:" literal it would false-match "duration_time:0.1" as
    // a second, later event. Assert there is exactly one.
    const events = parseSceneDetectionOutput(REAL_FFMPEG_STDERR_TRANSCRIPT, 0);
    expect(events).toHaveLength(1);
  });

  it("returns no events for output with no scene-change lines", () => {
    const events = parseSceneDetectionOutput("no matches here\njust noise\n", 0);
    expect(events).toEqual([]);
  });
});

describe("pgm-extractor -- real ffmpeg/ffprobe against a real generated fixture", () => {
  let dir: string;
  let fixturePath: string;

  beforeAll(async () => {
    dir = await mkdtemp(path.join(tmpdir(), "pgm-correlator-fixture-"));
    fixturePath = path.join(dir, "fixture.mp4");
    // 2s of animated testsrc, then a hard cut to 2s of solid red -- one
    // real, unambiguous scene change at t=2s, real encoded bytes.
    await new Promise<void>((resolve, reject) => {
      const child = spawn(
        "ffmpeg",
        [
          "-y",
          "-f",
          "lavfi",
          "-i",
          "testsrc=size=320x240:rate=10:duration=2",
          "-f",
          "lavfi",
          "-i",
          "color=c=red:size=320x240:rate=10:duration=2",
          "-filter_complex",
          "[0:v][1:v]concat=n=2:v=1:a=0[outv]",
          "-map",
          "[outv]",
          "-pix_fmt",
          "yuv420p",
          fixturePath,
        ],
        { windowsHide: true },
      );
      child.once("error", reject);
      child.once("close", (code) => (code === 0 ? resolve() : reject(new Error(`ffmpeg fixture build exited ${code}`))));
    });
  }, 20_000);

  afterAll(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  it("ffprobe reports the real fixture's duration and codec", async () => {
    const { raw, parsed } = await probeFile(fixturePath);
    expect(raw.length).toBeGreaterThan(0);
    const p = parsed as { format: { duration: string }; streams: Array<{ codec_type: string }> };
    expect(Number(p.format.duration)).toBeCloseTo(4, 0);
    expect(p.streams.some((s) => s.codec_type === "video")).toBe(true);
  });

  it("detects the real scene cut at ~t=2s and anchors it to the supplied wall-clock start", async () => {
    const recordingStartMs = 5_000_000;
    const { visualEvents, ffmpegStderrRaw } = await extractVisualEvents(fixturePath, recordingStartMs);

    expect(ffmpegStderrRaw.length).toBeGreaterThan(0);
    expect(visualEvents.length).toBeGreaterThanOrEqual(1);

    const cut = visualEvents.find((e) => Math.abs(e.ptsSeconds - 2) < 0.5);
    expect(cut).toBeDefined();
    expect(cut!.atMs).toBeCloseTo(recordingStartMs + cut!.ptsSeconds * 1000, 0);
    expect(cut!.sceneScore).toBeGreaterThan(0.1);
  });
});
