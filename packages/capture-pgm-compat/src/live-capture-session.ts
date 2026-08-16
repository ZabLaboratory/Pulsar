import { spawn, type SpawnedPulsar } from "@clodocapeo/pulsar-bundle-full";
import { startTestPageServer, type TestPageServer } from "./test-page-server.js";

export type Scenario = "healthy" | "black" | "frozen";

export interface LiveCaptureRecording {
  scenario: Scenario;
  /** Real .mp4 path written by pulsar.exe's own recorder (RecordNamespace,
   *  the real StartRecord/StopRecord path -- same code #230's
   *  record-session.ts drives). */
  path: string;
}

export interface RunLiveCaptureSessionOptions {
  /** Directory containing bin/64bit/pulsar.exe -- see
   *  @clodocapeo/pulsar-bundle-full's SpawnOptions.binariesPath. */
  pulsarBinariesPath?: string;
  /** How long to let the browser_source settle after (re)pointing it at a
   *  new URL, before recording -- CEF page load + first paint. */
  settleMs?: number;
  /** How long each of the three recordings runs. */
  recordDurationMs?: number;
  readyTimeoutMs?: number;
  onLog?: (stream: "stdout" | "stderr", line: string) => void;
}

export interface LiveCaptureSessionResult {
  recordings: LiveCaptureRecording[];
  libobsVersion: string;
  port: number;
}

const INPUT_NAME = "CapturePgmCompatProbe";
const DEFAULT_SCENE = "Default";
/** Both the boot canvas (via PULSAR_RESOLUTION) and the browser_source
 *  input are sized to this, so the captured/recorded frame IS the probe
 *  page, edge to edge -- no black canvas margin to dilute the measure. */
const PROBE_WIDTH = 320;
const PROBE_HEIGHT = 240;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Drives a REAL full Pulsar (pulsar.exe with obs-browser/CEF) through the
 * three scenarios this unit's oracle needs, entirely inside this process:
 * no Orion, no other repo, no fixture recordings substituted for real ones.
 *
 *  - "healthy": browser_source pointed at a real page with a
 *    requestAnimationFrame draw loop (real spatial AND temporal variance).
 *  - "black": browser_source pointed at a URL the local page server 404s.
 *    @clodocapeo/pulsar-bundle-full's own README documents this as CEF
 *    rendering blank/black -- the exact "faux positif CEF" shape: the
 *    SetInputSettings call itself succeeds (wire says OK).
 *  - "frozen": browser_source pointed at a page that paints real detail
 *    once on load and then never updates (no rAF loop) -- normal spatial
 *    stddev/luma, dead temporal diff.
 *
 * One input is created once, then SetInputSettings swaps its URL between
 * scenarios -- mirroring how a real capture pipeline (e.g.
 * pulsar-scene-source's SetCaptureSource) reuses one managed capture item
 * rather than creating a fresh one per swap.
 */
export async function runLiveCaptureSession(
  opts: RunLiveCaptureSessionOptions = {},
): Promise<LiveCaptureSessionResult> {
  const settleMs = opts.settleMs ?? 1500;
  const recordDurationMs = opts.recordDurationMs ?? 3000;

  const server: TestPageServer = await startTestPageServer();
  try {
    const spawnOpts: Parameters<typeof spawn>[0] = {
      readyTimeoutMs: opts.readyTimeoutMs ?? 30_000,
      // Match the boot canvas to the probe browser_source's own size
      // (see below). Default is 1920x1080 -- leaving that unmatched would
      // measure mostly black canvas margin around a small captured patch,
      // diluting every axis toward "flat" regardless of what the page
      // actually renders. This is not cosmetic: it's what makes the
      // measurement see the capture, not the canvas around it.
      env: { PULSAR_RESOLUTION: `${PROBE_WIDTH}x${PROBE_HEIGHT}` },
    };
    if (opts.pulsarBinariesPath !== undefined) spawnOpts.binariesPath = opts.pulsarBinariesPath;
    if (opts.onLog !== undefined) spawnOpts.onLog = opts.onLog;
    const pulsar: SpawnedPulsar = await spawn(spawnOpts);
    try {
      // Disable the boot-default "PulsarCapture" window_capture item so it
      // can't composite over (or under, depending on z-order) the probe
      // input -- it produces black frames anyway (unbound window) but this
      // removes any ambiguity about which layer a measurement is seeing.
      await disableDefaultCaptureItem(pulsar);

      const recordings: LiveCaptureRecording[] = [];
      const scenarioPaths: Record<Scenario, string> = {
        healthy: "/healthy",
        black: "/missing",
        frozen: "/frozen",
      };

      let inputCreated = false;
      for (const scenario of ["healthy", "black", "frozen"] as const) {
        const url = server.urlFor(scenarioPaths[scenario]);
        if (!inputCreated) {
          await pulsar.client.obs.call(
            "CreateInput" as never,
            {
              sceneName: DEFAULT_SCENE,
              inputName: INPUT_NAME,
              inputKind: "browser_source",
              inputSettings: { url, width: PROBE_WIDTH, height: PROBE_HEIGHT, fps: 30, fps_custom: true },
            } as never,
          );
          inputCreated = true;
        } else {
          await pulsar.client.obs.call(
            "SetInputSettings" as never,
            { inputName: INPUT_NAME, inputSettings: { url }, overlay: true } as never,
          );
        }

        await sleep(settleMs);
        await pulsar.client.record.start();
        await sleep(recordDurationMs);
        const path = await pulsar.client.record.stop();
        recordings.push({ scenario, path });
      }

      return { recordings, libobsVersion: pulsar.libobsVersion, port: pulsar.port };
    } finally {
      await pulsar.shutdown();
    }
  } finally {
    await server.close();
  }
}

interface SceneItem {
  sceneItemId: number;
  sourceName: string;
}

async function disableDefaultCaptureItem(pulsar: SpawnedPulsar): Promise<void> {
  let items: SceneItem[];
  try {
    const resp = await pulsar.client.obs.call(
      "GetSceneItemList" as never,
      { sceneName: DEFAULT_SCENE } as never,
    );
    items = (resp as unknown as { sceneItems: SceneItem[] }).sceneItems;
  } catch {
    // No "Default" scene item list available (unexpected boot shape) --
    // not fatal, the probe input still gets created and this unit's
    // recordings simply carry whatever else is in the scene, which the
    // report must then account for rather than silently trust.
    return;
  }
  const captureItem = items.find((i) => i.sourceName === "PulsarCapture");
  if (!captureItem) return;
  await pulsar.client.obs.call(
    "SetSceneItemEnabled" as never,
    { sceneName: DEFAULT_SCENE, sceneItemId: captureItem.sceneItemId, sceneItemEnabled: false } as never,
  );
}
