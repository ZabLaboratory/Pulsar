import type { PulsarClient } from "./client.js";
import type { OutputState } from "./types.js";

/**
 * Wraps the legacy frontend-stub recording output (frontend-stub's
 * "PulsarRecord" ffmpeg_muxer). This is the singleton, env-driven
 * recording used for "always-on local capture" -- distinct from the
 * multi-stream destinations API where vod_local destinations are also
 * MP4 files but client-named.
 *
 * The path is auto-resolved to <recordDir>/pulsar-<YYYYMMDD-HHMMSS>.mp4
 * by the server. recordDir defaults to <cwd>/recordings; override at
 * boot via PULSAR_RECORD_DIR.
 */
export class RecordNamespace {
  constructor(private readonly client: PulsarClient) {}

  async start(): Promise<void> {
    await this.client.obs.call("StartRecord");
  }

  /**
   * Stop and resolve with the resulting file path. Blocks until the
   * RecordStateChanged event reports STOPPED (with outputPath).
   */
  async stop(timeoutMs = 10_000): Promise<string> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.client.off("recordStateChanged", listener);
        reject(new Error(`RecordNamespace.stop timed out after ${timeoutMs}ms`));
      }, timeoutMs);

      const listener = (e: { state: OutputState; outputPath?: string }) => {
        if (e.state !== "STOPPED") return;
        clearTimeout(timer);
        this.client.off("recordStateChanged", listener);
        if (e.outputPath) resolve(e.outputPath);
        else reject(new Error("RecordStateChanged STOPPED without outputPath"));
      };
      this.client.on("recordStateChanged", listener);

      this.client.obs.call("StopRecord").catch((err) => {
        clearTimeout(timer);
        this.client.off("recordStateChanged", listener);
        reject(err);
      });
    });
  }

  async pause(): Promise<void> {
    await this.client.obs.call("PauseRecord");
  }

  async resume(): Promise<void> {
    await this.client.obs.call("ResumeRecord");
  }

  async isActive(): Promise<boolean> {
    const resp = await this.client.obs.call("GetRecordStatus");
    return Boolean((resp as { outputActive: boolean }).outputActive);
  }
}
