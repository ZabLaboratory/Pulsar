import type { PulsarClient } from "@clodocapeo/pulsar-client";

import { buildArtifact, writeArtifact, type WrittenArtifact } from "./artifact.js";
import { correlate, type CorrelateOptions } from "./correlator.js";
import { extractVisualEvents, type PgmExtractorOptions } from "./pgm-extractor.js";
import { OrionObserver, type OrionObserverOptions } from "./orion-observer.js";
import type { CorrelationArtifact, StateEvent } from "./types.js";

export interface RecordCorrelatedSessionOptions {
  /** Already-connected PulsarClient (packages/pulsar-client). */
  pulsar: PulsarClient;
  /** Passive LSDP viewer connection options. Read-only per this unit's
   *  bail: never touches Orion beyond opening the same connection any
   *  show-token holder can already open. */
  orion: OrionObserverOptions;
  outputBaseDir: string;
  sessionId?: string;
  durationMs: number;
  extractor?: PgmExtractorOptions;
  correlate?: CorrelateOptions;
}

export interface RecordCorrelatedSessionResult {
  artifact: CorrelationArtifact;
  paths: WrittenArtifact;
}

/**
 * Ties the three real pieces together for one live session:
 *
 *  1. `pulsar.record.start()/stop()` (pulsar-client `RecordNamespace`) --
 *     a real `StartRecord`/`StopRecord` against a running Pulsar.
 *  2. `OrionObserver` -- a real, passive, read-only LSDP viewer connection.
 *  3. `correlate()` -- pairs (1)'s post-hoc ffprobe/ffmpeg extraction
 *     against (2)'s captured identity stream.
 *
 * This function performs real I/O end to end (WS connect, StartRecord, a
 * live wait, StopRecord, ffprobe/ffmpeg) and has no test that exercises
 * this exact wiring, because doing so requires a live Orion instance
 * emitting a real `correlation_id` -- unavailable in the sandbox this unit
 * was built in (see the PR description / AGENT_CHECKPOINT on
 * ZabLaboratory/Pulsar#230). Each of its three dependencies IS tested in
 * isolation against real transports/binaries where the mandate requires it
 * (orion-observer.test.ts uses a real local WS server; pgm-extractor.test.ts
 * runs real ffmpeg/ffprobe against a real generated fixture file).
 */
export async function recordCorrelatedSession(
  opts: RecordCorrelatedSessionOptions,
): Promise<RecordCorrelatedSessionResult> {
  const sessionId = opts.sessionId ?? new Date().toISOString().replace(/[:.]/g, "-");
  const stateEvents: StateEvent[] = [];

  const observer = new OrionObserver(opts.orion);
  const unsubscribe = observer.onState((e) => stateEvents.push(e));
  await observer.connect();

  // Anchor recordingStartMs to the moment obs-websocket actually confirms
  // STARTED, not to when `record.start()` resolves (which fires on the
  // request ack, ahead of the output actually beginning) -- a closer, but
  // still imperfect, anchor. See KNOWN_DRIFT_SOURCES.clock_skew.
  let recordingStartMs = Date.now();
  const onRecordState = (e: { state: string }) => {
    if (e.state === "STARTED") recordingStartMs = Date.now();
  };
  opts.pulsar.on("recordStateChanged", onRecordState as never);

  await opts.pulsar.record.start();
  await new Promise((resolve) => setTimeout(resolve, opts.durationMs));
  const outputPath = await opts.pulsar.record.stop();

  opts.pulsar.off("recordStateChanged", onRecordState as never);
  observer.close();
  unsubscribe();

  const { visualEvents } = await extractVisualEvents(outputPath, recordingStartMs, opts.extractor);
  const { records, threshold } = correlate(stateEvents, visualEvents, opts.correlate);

  const artifact = buildArtifact({
    sessionId,
    recordingPath: outputPath,
    recordingContainer: outputPath.toLowerCase().endsWith(".mkv") ? "mkv" : "mp4",
    stateEvents,
    visualEvents,
    records,
    threshold,
  });

  const paths = await writeArtifact(opts.outputBaseDir, artifact);
  return { artifact, paths };
}
