import type { PulsarClient } from "./client.js";
import {
  createDestinationToWire,
  destinationFromWire,
  type WireCreateDestinationResponse,
  type WireGetDestinationsResponse,
  type WireOkResponse,
  type WireRemoveDestinationResponse,
  type WireStartDestinationResponse,
  type WireStopDestinationResponse,
} from "./wire.js";
import type { CreateDestinationInput, Destination } from "./types.js";

export class DestinationsNamespace {
  constructor(private readonly client: PulsarClient) {}

  /** List every registered destination. */
  async list(): Promise<Destination[]> {
    const resp = await this.client.callVendor<object, WireGetDestinationsResponse>("GetDestinations");
    return (resp.destinations ?? []).map(destinationFromWire);
  }

  /**
   * Create a new destination. Returns the just-created entry (mostly for
   * its `id`). Throws PulsarVendorError on validation failure
   * (bad URL scheme, missing key, unknown kind, etc.).
   */
  async create(input: CreateDestinationInput): Promise<Destination> {
    const resp = await this.client.callVendor<
      ReturnType<typeof createDestinationToWire>,
      WireCreateDestinationResponse
    >("CreateDestination", createDestinationToWire(input));
    if (!resp.id) {
      // Server didn't error out but also didn't return an id -- defensive.
      throw new Error("CreateDestination returned no id");
    }
    // The list-after-create race-window is irrelevant here because the
    // server returns synchronously after registry.create completes.
    const all = await this.list();
    const created = all.find((d) => d.id === resp.id);
    if (!created) {
      throw new Error(`CreateDestination returned id ${resp.id} but it's not in GetDestinations`);
    }
    return created;
  }

  /** Remove a destination. The server gracefully stops it first if active. */
  async remove(id: string): Promise<boolean> {
    const resp = await this.client.callVendor<{ id: string }, WireRemoveDestinationResponse>(
      "RemoveDestination",
      { id },
    );
    return resp.removed === true;
  }

  /** Start a destination -- create the obs_output_t lazily, attach shared
   *  encoders, call obs_output_start. Returns false if RTMP refused. */
  async start(id: string): Promise<boolean> {
    const resp = await this.client.callVendor<{ id: string }, WireStartDestinationResponse>(
      "StartDestination",
      { id },
    );
    return resp.started === true;
  }

  /** Stop a destination -- obs_output_stop, async muxer trailer write. */
  async stop(id: string): Promise<boolean> {
    const resp = await this.client.callVendor<{ id: string }, WireStopDestinationResponse>(
      "StopDestination",
      { id },
    );
    return resp.stopped === true;
  }

  /** Start every destination concurrently. Failed starts are logged
   *  server-side; this returns once the registry has tried them all. */
  async startAll(): Promise<void> {
    await this.client.callVendor<object, WireOkResponse>("StartAllDestinations");
  }

  async stopAll(): Promise<void> {
    await this.client.callVendor<object, WireOkResponse>("StopAllDestinations");
  }
}
