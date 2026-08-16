import { spawn } from "node:child_process";

import type { VisualEvent } from "./types.js";

export interface PgmExtractorOptions {
  ffmpegPath?: string;
  ffprobePath?: string;
  /** ffmpeg `scene` filter score cutoff in [0,1] above which a frame-to-
   *  frame transition is treated as a visual change. Empirically checked
   *  against a real synthetic fixture (see tests/pgm-extractor.test.ts):
   *  0.1 correctly isolated a hard cut from a slowly-animating `testsrc`
   *  without false positives on this one fixture. It has NOT been tuned
   *  against real Blue-rendered content -- that requires the live
   *  environment this unit's report declares unavailable here. Treat as a
   *  documented starting point, not a validated constant. */
  sceneThreshold?: number;
}

const DEFAULT_SCENE_THRESHOLD = 0.1;

interface CaptureResult {
  stdout: string;
  stderr: string;
  code: number | null;
}

function runCapture(cmd: string, args: string[]): Promise<CaptureResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { windowsHide: true });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    child.once("error", reject);
    child.once("close", (code) => resolve({ stdout, stderr, code }));
  });
}

/** Runs `ffprobe -show_format -show_streams` and returns both the raw text
 *  (paste this verbatim into a report -- it's the standard of proof this
 *  unit's bail requires) and the parsed JSON. */
export async function probeFile(
  filePath: string,
  opts: PgmExtractorOptions = {},
): Promise<{ raw: string; parsed: unknown }> {
  const ffprobe = opts.ffprobePath ?? "ffprobe";
  const { stdout, stderr, code } = await runCapture(ffprobe, [
    "-v",
    "quiet",
    "-print_format",
    "json",
    "-show_format",
    "-show_streams",
    filePath,
  ]);
  if (code !== 0) throw new Error(`ffprobe exited ${code}: ${stderr}`);
  return { raw: stdout, parsed: JSON.parse(stdout) as unknown };
}

const SCENE_SCORE_RE = /lavfi\.scene_score=([0-9.]+)/;
const PTS_TIME_RE = /\bpts_time:([0-9.]+)/;

/** Parses the interleaved `metadata=print` + `showinfo` lines ffmpeg writes
 *  to stderr for the filter chain below: one `lavfi.scene_score=` line
 *  immediately followed by one `[Parsed_showinfo...] ... pts_time:...`
 *  line, per passing frame (verified against a real ffmpeg build --
 *  tests/pgm-extractor.test.ts pastes the actual output). */
export function parseSceneDetectionOutput(stderr: string, recordingStartMs: number): VisualEvent[] {
  const events: VisualEvent[] = [];
  let pendingScore: number | undefined;
  for (const line of stderr.split(/\r?\n/)) {
    const scoreMatch = SCENE_SCORE_RE.exec(line);
    if (scoreMatch) {
      pendingScore = Number(scoreMatch[1]);
      continue;
    }
    const ptsMatch = PTS_TIME_RE.exec(line);
    if (ptsMatch && pendingScore !== undefined) {
      const ptsSeconds = Number(ptsMatch[1]);
      events.push({
        atMs: recordingStartMs + ptsSeconds * 1000,
        ptsSeconds,
        sceneScore: pendingScore,
      });
      pendingScore = undefined;
    }
  }
  return events;
}

export interface ExtractResult {
  visualEvents: VisualEvent[];
  /** Full ffmpeg stderr, for pasting into a report. */
  ffmpegStderrRaw: string;
}

/**
 * Extracts visual scene-change timestamps from a real recorded file.
 * `recordingStartMs` anchors the file's PTS=0 to wall-clock -- this
 * function never guesses it: the caller supplies it (record-session.ts
 * takes it from the moment it observed Pulsar's `recordStateChanged`
 * STARTED event).
 */
export async function extractVisualEvents(
  filePath: string,
  recordingStartMs: number,
  opts: PgmExtractorOptions = {},
): Promise<ExtractResult> {
  const ffmpeg = opts.ffmpegPath ?? "ffmpeg";
  const threshold = opts.sceneThreshold ?? DEFAULT_SCENE_THRESHOLD;
  const args = [
    "-i",
    filePath,
    "-vf",
    `select='gt(scene,${threshold})',metadata=print,showinfo`,
    "-f",
    "null",
    "-",
  ];
  const { stderr } = await runCapture(ffmpeg, args);
  return {
    visualEvents: parseSceneDetectionOutput(stderr, recordingStartMs),
    ffmpegStderrRaw: stderr,
  };
}
