import { spawn } from "node:child_process";

/** Per-frame measurements from a decoded, downscaled grayscale frame. */
export interface FrameStats {
  index: number;
  /** Mean pixel value across the frame, 0-255 -- "niveaux de luma". */
  meanLuma: number;
  /** Population standard deviation of pixel values WITHIN this single
   *  frame -- "spatial stddev" per live-testing.md's golden rule. Near 0
   *  for a flat/solid-colour frame (black, or any uniform colour); high
   *  for a frame with real visual detail. This is a within-frame measure:
   *  it says nothing about whether the frame differs from its neighbours
   *  (see TemporalDiff for that -- a static-but-detailed frame scores high
   *  here and ~0 there, which is exactly the "frozen" failure mode this
   *  axis alone cannot see). */
  spatialStddev: number;
}

/** Pixel-wise mean absolute difference between two consecutive decoded
 *  frames -- the temporal axis. Computed directly on raw pixel bytes, not
 *  on a derived scalar like meanLuma, so a change that happens to preserve
 *  the average luma is still caught. */
export interface TemporalDiff {
  fromIndex: number;
  toIndex: number;
  meanAbsDiff: number;
}

export interface HealthMeasurement {
  filePath: string;
  width: number;
  height: number;
  frameCount: number;
  frames: FrameStats[];
  temporalDiffs: TemporalDiff[];
  /** Mean of frames[].meanLuma. */
  meanLumaAvg: number;
  /** Mean of frames[].spatialStddev. */
  spatialStddevAvg: number;
  /** Mean of temporalDiffs[].meanAbsDiff. 0 (not NaN) when frameCount < 2,
   *  a caller must check frameCount to distinguish "no motion measured"
   *  from "too few frames to measure motion at all". */
  temporalDiffAvg: number;
  /** Raw ffmpeg stderr, for pasting into a report. */
  ffmpegStderrRaw: string;
}

export interface MeasureFrameHealthOptions {
  ffmpegPath?: string;
  /** Downscale target before measuring. Small on purpose: this measure
   *  cares about "is there real, varying detail", not fine spatial
   *  resolution, and a tiny frame keeps the pure-JS pass over every pixel
   *  of every frame cheap even for a several-second recording. */
  width?: number;
  height?: number;
}

const DEFAULT_WIDTH = 64;
const DEFAULT_HEIGHT = 36;

interface RawCapture {
  stdout: Buffer;
  stderr: string;
  code: number | null;
}

function runRawCapture(cmd: string, args: string[]): Promise<RawCapture> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { windowsHide: true });
    const stdoutChunks: Buffer[] = [];
    let stderr = "";
    child.stdout.on("data", (d: Buffer) => stdoutChunks.push(d));
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    child.once("error", reject);
    child.once("close", (code) => resolve({ stdout: Buffer.concat(stdoutChunks), stderr, code }));
  });
}

/**
 * Decodes a real recorded file to raw grayscale frames (via a real ffmpeg
 * process, not a filter whose exact per-frame semantics this unit would
 * have to take on faith) and computes, per frame, the mean luma and the
 * spatial (within-frame) pixel stddev, plus the temporal (frame-to-frame)
 * pixel-wise mean absolute difference across the sequence.
 *
 * Both axes are required to tell "healthy" apart from either named failure
 * mode: a black/absent source fails spatialStddev (flat) AND meanLuma (low);
 * a frozen-but-detailed source fails ONLY temporalDiff -- it can have a
 * perfectly normal spatialStddev and meanLuma while never changing frame to
 * frame. Neither axis alone is a sufficient health oracle.
 */
export async function measureFrameHealth(
  filePath: string,
  opts: MeasureFrameHealthOptions = {},
): Promise<HealthMeasurement> {
  const ffmpeg = opts.ffmpegPath ?? "ffmpeg";
  const width = opts.width ?? DEFAULT_WIDTH;
  const height = opts.height ?? DEFAULT_HEIGHT;
  const frameSize = width * height;

  const args = [
    "-i",
    filePath,
    "-vf",
    `scale=${width}:${height}:flags=area,format=gray`,
    "-f",
    "rawvideo",
    "-pix_fmt",
    "gray",
    "-",
  ];
  const { stdout, stderr, code } = await runRawCapture(ffmpeg, args);
  if (stdout.length === 0) {
    throw new Error(`ffmpeg exited ${code} producing zero raw bytes for ${filePath}: ${stderr}`);
  }
  if (stdout.length % frameSize !== 0) {
    throw new Error(
      `ffmpeg raw output for ${filePath} is ${stdout.length} bytes, not a multiple of the ` +
        `expected frame size ${frameSize} (${width}x${height}) -- decode likely truncated mid-frame.`,
    );
  }

  const frameBufs: Buffer[] = [];
  for (let offset = 0; offset < stdout.length; offset += frameSize) {
    frameBufs.push(stdout.subarray(offset, offset + frameSize));
  }
  if (frameBufs.length === 0) {
    throw new Error(`ffmpeg produced 0 decodable frames for ${filePath} (raw output ${stdout.length} bytes)`);
  }

  const frames: FrameStats[] = frameBufs.map((buf, index) => {
    let sum = 0;
    for (let p = 0; p < buf.length; p++) sum += buf[p]!;
    const mean = sum / buf.length;
    let sqSum = 0;
    for (let p = 0; p < buf.length; p++) {
      const d = buf[p]! - mean;
      sqSum += d * d;
    }
    const stddev = Math.sqrt(sqSum / buf.length);
    return { index, meanLuma: mean, spatialStddev: stddev };
  });

  const temporalDiffs: TemporalDiff[] = [];
  for (let i = 1; i < frameBufs.length; i++) {
    const prev = frameBufs[i - 1]!;
    const cur = frameBufs[i]!;
    let diffSum = 0;
    for (let p = 0; p < frameSize; p++) diffSum += Math.abs(cur[p]! - prev[p]!);
    temporalDiffs.push({ fromIndex: i - 1, toIndex: i, meanAbsDiff: diffSum / frameSize });
  }

  const meanLumaAvg = average(frames.map((f) => f.meanLuma));
  const spatialStddevAvg = average(frames.map((f) => f.spatialStddev));
  const temporalDiffAvg = temporalDiffs.length > 0 ? average(temporalDiffs.map((t) => t.meanAbsDiff)) : 0;

  return {
    filePath,
    width,
    height,
    frameCount: frames.length,
    frames,
    temporalDiffs,
    meanLumaAvg,
    spatialStddevAvg,
    temporalDiffAvg,
    ffmpegStderrRaw: stderr,
  };
}

function average(values: number[]): number {
  if (values.length === 0) return 0;
  return values.reduce((a, b) => a + b, 0) / values.length;
}
