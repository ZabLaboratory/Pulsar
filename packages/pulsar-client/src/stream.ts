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
 * THE `rtmp_common` SERVICE TYPE IS NOT AVAILABLE ON THIS SURFACE.
 * SetStreamServiceSettings refuses it outright (InvalidRequestField),
 * whatever platform the settings name (#135): an rtmp_common service
 * resolves its ingest from a service list downloaded at runtime -- which
 * carries cleartext rtmp:// entries, and which falls back to the
 * cleartext rtmp://live.twitch.tv/app for Twitch when the list is
 * absent -- so the stream key could end up on the wire unencrypted.
 * Push an `rtmp_custom` service with an explicit server instead, or, for
 * Twitch, use `pulsar.destinations.create({ kind: "twitch", ... })`,
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
