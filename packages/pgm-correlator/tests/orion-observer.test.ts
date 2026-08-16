// Parser test (mock transport, NOT a PGM proof). Exercises OrionObserver's
// LSDP envelope parsing/filtering against a real local WebSocket server --
// it proves the parser reads the wire schema correctly, nothing about a
// live Pulsar recording or a real Orion correlation_id.

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { OrionObserver } from "../src/orion-observer.js";
import type { StateEvent } from "../src/types.js";
import { MockLsdpServer } from "./lsdp-mock-server.js";

async function waitForClient(server: MockLsdpServer): Promise<void> {
  const start = Date.now();
  while (server.clients.size === 0) {
    if (Date.now() - start > 2000) throw new Error("no client connected within 2s");
    await new Promise((r) => setTimeout(r, 5));
  }
}

describe("OrionObserver -- LSDP envelope parsing (mock transport, not a PGM proof)", () => {
  let server: MockLsdpServer;
  let observer: OrionObserver | undefined;

  beforeEach(async () => {
    server = await MockLsdpServer.create();
  });

  afterEach(async () => {
    observer?.close();
    await server.close();
  });

  it("emits a StateEvent for a delta carrying identity fields, with wire snake_case mapped to camelCase", async () => {
    const events: StateEvent[] = [];
    observer = new OrionObserver({ url: server.url, now: () => 12345 });
    observer.onState((e) => events.push(e));
    await observer.connect();
    await waitForClient(server);

    server.broadcast({
      type: "delta",
      v: 1,
      seq: 7,
      scene_id: "scene-abc",
      schema_version: "1.1",
      scene_digest: "sha256:deadbeef",
      runtime_instance_id: "runtime-1",
      target: "solar",
      render_revision: "rev-3",
      correlation_id: "corr-xyz",
      patches: [],
    });

    await new Promise((r) => setTimeout(r, 20));

    expect(events).toHaveLength(1);
    expect(events[0]).toEqual({
      receivedAtMs: 12345,
      frameType: "delta",
      sequence: 7,
      sceneId: "scene-abc",
      identity: {
        schemaVersion: "1.1",
        sceneDigest: "sha256:deadbeef",
        runtimeInstanceId: "runtime-1",
        target: "solar",
        renderRevision: "rev-3",
        correlationId: "corr-xyz",
      },
    });
  });

  it("emits a StateEvent for a snapshot carrying identity fields (post-lumencast-go 0c7cfc6)", async () => {
    const events: StateEvent[] = [];
    observer = new OrionObserver({ url: server.url, now: () => 999 });
    observer.onState((e) => events.push(e));
    await observer.connect();
    await waitForClient(server);

    server.broadcast({
      type: "snapshot",
      v: 1,
      seq: 1,
      scene_id: "scene-abc",
      scene_version: "v1",
      state: {},
      scene_digest: "sha256:cafef00d",
      correlation_id: "corr-resumed",
    });

    await new Promise((r) => setTimeout(r, 20));

    expect(events).toHaveLength(1);
    expect(events[0]!.frameType).toBe("snapshot");
    expect(events[0]!.identity.correlationId).toBe("corr-resumed");
    expect(events[0]!.identity.sceneDigest).toBe("sha256:cafef00d");
  });

  it("drops a delta with no identity fields at all -- nothing to correlate (mirror.go parity)", async () => {
    const events: StateEvent[] = [];
    observer = new OrionObserver({ url: server.url });
    observer.onState((e) => events.push(e));
    await observer.connect();
    await waitForClient(server);

    server.broadcast({ type: "delta", v: 1, seq: 1, scene_id: "scene-abc", patches: [] });
    server.broadcast({
      type: "delta",
      v: 1,
      seq: 2,
      scene_id: "scene-abc",
      correlation_id: "corr-only-this-one",
      patches: [],
    });

    await new Promise((r) => setTimeout(r, 20));

    expect(events).toHaveLength(1);
    expect(events[0]!.sequence).toBe(2);
  });

  it("ignores non-delta/snapshot frame types and malformed (non-JSON) frames without throwing", async () => {
    const events: StateEvent[] = [];
    observer = new OrionObserver({ url: server.url });
    observer.onState((e) => events.push(e));
    await observer.connect();
    await waitForClient(server);

    server.broadcastRaw("not json at all");
    server.broadcast({ type: "scene_changed", v: 1, from_scene_id: "a", to_scene_id: "b" });
    server.broadcast({ type: "error", v: 1, code: "SOME_ERROR", message: "x", recoverable: true });
    server.broadcast({ type: "delta", v: 1, seq: 3, scene_id: "s", correlation_id: "corr-9", patches: [] });

    await new Promise((r) => setTimeout(r, 20));

    expect(events).toHaveLength(1);
    expect(events[0]!.identity.correlationId).toBe("corr-9");
  });
});
