import type { PulsarClient } from "./client.js";

/**
 * Wraps the legacy frontend-stub streaming output ("PulsarStream"
 * rtmp_output). This is the singleton stream output that obeys the v5
 * StartStream / StopStream baseline; Stream Deck, Companion, and
 * Streamer.bot all use this path. Multi-destination control goes
 * through `PulsarClient.destinations` instead.
 *
 * StartStream succeeds on the wire even when no destination URL is
 * configured -- the underlying obs_output_start declines silently. To
 * actually go live through this surface, configure a streaming service
 * via the v5 SetStreamServiceSettings request first, or use
 * `pulsar.destinations.start(id)` from the multi-stream API.
 *
 * TWITCH IS NOT AVAILABLE ON THIS SURFACE. SetStreamServiceSettings
 * refuses `rtmp_common` + `service: "Twitch"` (InvalidRequestField):
 * that service resolves its ingest from a list downloaded at runtime
 * and falls back to the cleartext rtmp://live.twitch.tv/app when the
 * list is absent, which would put the stream key on the wire
 * unencrypted. Use `pulsar.destinations.create({ kind: "twitch", ... })`,
 * whose rtmps:// ingest is pinned at compile time. The same request
 * also requires an rtmp://|rtmps:// server and a non-empty key.
 */
export class StreamNamespace {
  constructor(private readonly client: PulsarClient) {}

  async start(): Promise<void> {
    await this.client.obs.call("StartStream");
  }

  async stop(): Promise<void> {
    await this.client.obs.call("StopStream");
  }

  async isActive(): Promise<boolean> {
    const resp = await this.client.obs.call("GetStreamStatus");
    return Boolean((resp as { outputActive: boolean }).outputActive);
  }
}
