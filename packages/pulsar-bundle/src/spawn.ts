import { spawn as childSpawn, type ChildProcess } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { PulsarClient } from "@clodocapeo/pulsar-client";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/** Default location of the bundled binaries inside this package. */
const DEFAULT_BINARIES = resolve(__dirname, "..", "binaries");

export interface SpawnOptions {
  /** Override the directory containing bin/64bit/pulsar.exe.
   *  Default: <package>/binaries (populated by postinstall). */
  binariesPath?: string;

  /** Extra env vars for pulsar.exe. Common ones:
   *    PULSAR_CAPTURE_WINDOW   "<title>:<class>:<exe>" for window_capture
   *    PULSAR_FPS              30 / 60 / etc.
   *    PULSAR_VIDEO_BITRATE    kbps
   *    PULSAR_AUDIO_BITRATE    kbps
   *    PULSAR_RECORD_DIR       output dir for the singleton recorder
   *    PULSAR_PROCESS_AUDIO_NAME  exe name for per-process audio
   *    PULSAR_ADAPTIVE_BITRATE off  to disable the worker
   */
  env?: Record<string, string>;

  /** How long to wait for "ready, idling" on stdout. Default 30 s. */
  readyTimeoutMs?: number;

  /** Optional log forwarder. Receives one stdout/stderr line at a time. */
  onLog?: (stream: "stdout" | "stderr", line: string) => void;

  /** Test-only escape hatch: spawn a different executable instead of
   *  the bundled pulsar.exe. The launched process must still print the
   *  ready marker on stdout and create obs-websocket/config.json under
   *  the cwd. Used by the package's own vitest suite -- not a public
   *  API for normal callers.
   *  @internal */
  launchCommand?: { exe: string; args?: string[] };
}

export interface SpawnedPulsar {
  /** Connected PulsarClient ready for vendor calls. */
  client: PulsarClient;

  /** Underlying child process. Most callers should use shutdown() instead
   *  of touching this directly, but it's exposed for advanced use cases
   *  (sending custom signals, reading stdio, etc.). */
  child: ChildProcess;

  /** WebSocket port the obs-websocket server is bound to. */
  port: number;

  /** libobs version string parsed from the boot log
   *  (e.g. "32.1.2-1-g8c23ba721-pulsar"). */
  libobsVersion: string;

  /** Disconnect the WS client and terminate pulsar.exe. Resolves once
   *  the process has exited. Idempotent. */
  shutdown(): Promise<void>;
}

interface ObsWebSocketConfig {
  server_password?: string;
  server_port?: number;
}

const READY_MARKER = "pulsar-headless: libobs ";
const IDLE_MARKER = "ready, idling";

/**
 * Spawn pulsar.exe and return a connected PulsarClient.
 *
 * The child process is started with its working directory set to the
 * binary's location so that the loader resolves obs.dll and the libobs
 * plugin scanner finds obs-plugins/64bit/ via the relative path
 * convention add_default_module_paths uses.
 *
 * @example
 *   const pulsar = await spawn({
 *     env: { PULSAR_CAPTURE_WINDOW: "Untitled - Notepad:Notepad:notepad.exe" },
 *   });
 *   await pulsar.client.destinations.create({ kind: "twitch", key });
 *   await pulsar.shutdown();
 */
export async function spawn(opts: SpawnOptions = {}): Promise<SpawnedPulsar> {
  const binariesPath = opts.binariesPath ?? DEFAULT_BINARIES;
  const cwd = join(binariesPath, "bin", "64bit");
  const configPath = join(cwd, "obs-websocket", "config.json");

  // Resolve the executable. Default = bundled pulsar.exe; tests can pass
  // launchCommand to swap in a fake.
  let exe: string;
  let args: string[];
  if (opts.launchCommand) {
    exe = opts.launchCommand.exe;
    args = opts.launchCommand.args ?? [];
  } else {
    exe = join(cwd, "pulsar.exe");
    args = [];
    if (!existsSync(exe)) {
      throw new Error(
        `pulsar.exe not found at ${exe}. ` +
          `Run \`npm install\` to trigger the postinstall download, ` +
          `or set binariesPath to a local checkout.`,
      );
    }
  }

  const child = childSpawn(exe, args, {
    cwd,
    env: { ...process.env, ...opts.env },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  const readyTimeout = opts.readyTimeoutMs ?? 30_000;

  let libobsVersion = "";
  let resolveReady!: () => void;
  let rejectReady!: (err: Error) => void;
  const ready = new Promise<void>((res, rej) => {
    resolveReady = res;
    rejectReady = rej;
  });

  const watchdog = setTimeout(() => {
    rejectReady(new Error(`pulsar.exe did not signal ready within ${readyTimeout}ms`));
  }, readyTimeout);

  const handleLine = (stream: "stdout" | "stderr", line: string) => {
    if (line.length === 0) return;
    if (opts.onLog) opts.onLog(stream, line);
    // pulsar-headless prints exactly:
    //   "pulsar-headless: libobs <version> ready, idling (Ctrl+C to exit)"
    // when boot is complete.
    if (stream === "stdout" && line.includes(IDLE_MARKER) && line.includes(READY_MARKER)) {
      const slice = line.slice(line.indexOf(READY_MARKER) + READY_MARKER.length);
      libobsVersion = slice.split(" ", 1)[0] ?? "";
      clearTimeout(watchdog);
      resolveReady();
    }
  };

  attachLineReader(child, "stdout", (l) => handleLine("stdout", l));
  attachLineReader(child, "stderr", (l) => handleLine("stderr", l));

  child.on("exit", (code, signal) => {
    clearTimeout(watchdog);
    rejectReady(new Error(`pulsar.exe exited prematurely (code=${code}, signal=${signal})`));
  });

  await ready;

  // obs-websocket writes its config.json on first run -- after the
  // ready marker, the file is guaranteed to exist with port + password.
  if (!existsSync(configPath)) {
    child.kill();
    throw new Error(`obs-websocket config not found at ${configPath} (boot incomplete?)`);
  }
  const config = JSON.parse(readFileSync(configPath, "utf8")) as ObsWebSocketConfig;
  const port = config.server_port ?? 4455;
  const password = config.server_password;

  const client = new PulsarClient();
  await client.connect({
    url: `ws://127.0.0.1:${port}`,
    ...(password !== undefined ? { password } : {}),
  });

  let shutdownPromise: Promise<void> | null = null;
  const shutdown = async (): Promise<void> => {
    if (shutdownPromise) return shutdownPromise;
    shutdownPromise = (async () => {
      try {
        await client.disconnect();
      } catch {
        // Already closed -- swallow.
      }
      if (child.exitCode === null && child.signalCode === null) {
        child.kill();
        await new Promise<void>((res) => {
          if (child.exitCode !== null || child.signalCode !== null) return res();
          child.once("exit", () => res());
          // Safety net: a stuck process gets a 5 s grace period before
          // force-kill so a pending muxer trailer write can land.
          setTimeout(() => {
            if (child.exitCode === null && child.signalCode === null) {
              child.kill("SIGKILL");
            }
          }, 5_000);
        });
      }
    })();
    return shutdownPromise;
  };

  return { client, child, port, libobsVersion, shutdown };
}

/** Reads `child[stream]` line-by-line and dispatches each completed line. */
function attachLineReader(
  child: ChildProcess,
  stream: "stdout" | "stderr",
  onLine: (line: string) => void,
): void {
  const out = child[stream];
  if (!out) return;
  let buf = "";
  out.setEncoding("utf8");
  out.on("data", (chunk: string) => {
    buf += chunk;
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).replace(/\r$/, "");
      buf = buf.slice(idx + 1);
      onLine(line);
    }
  });
  out.on("end", () => {
    if (buf.length > 0) onLine(buf.replace(/\r$/, ""));
  });
}
