import type { PulsarClient } from "./client.js";
import {
  videoPatchResultFromWire,
  videoPatchToWire,
  videoSettingsFromWire,
  type WireGetVideoSettingsResponse,
  type WireSetVideoSettingsResponse,
} from "./wire.js";
import type { VideoSettings, VideoSettingsPatch, VideoSettingsPatchResult } from "./types.js";

export class VideoNamespace {
  constructor(private readonly client: PulsarClient) {}

  /** Snapshot the current encoder + reset_video state. */
  async get(): Promise<VideoSettings> {
    const resp = await this.client.callVendor<object, WireGetVideoSettingsResponse>("GetVideoSettings");
    return videoSettingsFromWire(resp);
  }

  /**
   * Mutate encoder bitrates live. Phase 12a only accepts video_bitrate
   * and (when the audio encoder is idle) audio_bitrate. Setting fps /
   * width / height is rejected by the server with a typed error pointing
   * at the boot env vars (PULSAR_FPS, PULSAR_RESOLUTION).
   */
  async set(patch: VideoSettingsPatch): Promise<VideoSettingsPatchResult> {
    const resp = await this.client.callVendor<
      ReturnType<typeof videoPatchToWire>,
      WireSetVideoSettingsResponse
    >("SetVideoSettings", videoPatchToWire(patch));
    return videoPatchResultFromWire(resp);
  }

  /** Convenience: set just the video bitrate in kbps. */
  async setBitrate(videoKbps: number): Promise<VideoSettingsPatchResult> {
    return this.set({ videoBitrate: videoKbps });
  }
}
