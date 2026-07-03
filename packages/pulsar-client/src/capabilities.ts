import type { PulsarClient } from "./client.js";
import { capabilitiesFromWire, type WireGetCapabilitiesResponse } from "./wire.js";
import type { PulsarCapabilities } from "./types.js";

export class CapabilitiesNamespace {
  constructor(private readonly client: PulsarClient) {}

  /**
   * Enumerate the encoder families this Pulsar build exposes plus the
   * bitrate windows. Detection is off-air / boot-fixed (ADR 004 §3.3):
   * the reported `activeEncoder` is the family currently bound to the
   * streaming output, and encoder identity cannot change at runtime.
   */
  async get(): Promise<PulsarCapabilities> {
    const resp = await this.client.callVendor<object, WireGetCapabilitiesResponse>("GetCapabilities");
    return capabilitiesFromWire(resp);
  }
}
