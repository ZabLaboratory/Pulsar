import type { PulsarClient } from "./client.js";
import {
  adaptiveStateFromWire,
  type WireGetAdaptiveStateResponse,
  type WireSetAdaptiveEnabledResponse,
} from "./wire.js";
import type { AdaptiveState } from "./types.js";

export class AdaptiveNamespace {
  constructor(private readonly client: PulsarClient) {}

  /** Snapshot of the bitrate adaptation worker's current state. */
  async getState(): Promise<AdaptiveState> {
    const resp = await this.client.callVendor<object, WireGetAdaptiveStateResponse>("GetAdaptiveState");
    return adaptiveStateFromWire(resp);
  }

  /** Toggle the worker. Disabling pauses sampling; the encoder bitrate is
   *  left at whatever value the worker last applied. Re-enabling resets
   *  stable_ticks to 0 so the loop re-warms before any climb attempt. */
  async setEnabled(enabled: boolean): Promise<boolean> {
    const resp = await this.client.callVendor<{ enabled: boolean }, WireSetAdaptiveEnabledResponse>(
      "SetAdaptiveEnabled",
      { enabled },
    );
    return resp.enabled === true;
  }

  async enable(): Promise<boolean> {
    return this.setEnabled(true);
  }

  async disable(): Promise<boolean> {
    return this.setEnabled(false);
  }
}
