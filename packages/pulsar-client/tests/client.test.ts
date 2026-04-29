import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { PulsarClient, PulsarVendorError } from "../src/index.js";
import { MockObsWebSocket } from "./mock-server.js";

describe("PulsarClient", () => {
  let server: MockObsWebSocket;
  let client: PulsarClient;

  beforeEach(async () => {
    server = await MockObsWebSocket.create();
    client = new PulsarClient();
    await client.connect({ url: server.url });
  });

  afterEach(async () => {
    await client.disconnect();
    await server.close();
  });

  describe("destinations", () => {
    it("lists empty initially", async () => {
      expect(await client.destinations.list()).toEqual([]);
    });

    it("create -> list -> start -> stop -> remove round trip", async () => {
      const dest = await client.destinations.create({
        kind: "rtmp_custom",
        url: "rtmp://example.test/live",
        key: "abc",
      });
      expect(dest.id).toMatch(/^mock-/);
      expect(dest.kind).toBe("rtmp_custom");
      expect(dest.active).toBe(false);

      const list = await client.destinations.list();
      expect(list.find((d) => d.id === dest.id)).toBeTruthy();

      expect(await client.destinations.start(dest.id)).toBe(true);
      const afterStart = await client.destinations.list();
      expect(afterStart.find((d) => d.id === dest.id)?.active).toBe(true);

      expect(await client.destinations.stop(dest.id)).toBe(true);
      expect(await client.destinations.remove(dest.id)).toBe(true);
      expect(await client.destinations.list()).toEqual([]);
    });

    it("twitch kind pins server url", async () => {
      const dest = await client.destinations.create({
        kind: "twitch",
        key: "live_dummy_dummy",
      });
      expect(dest.url).toBe("rtmp://live.twitch.tv/app/");
      expect(dest.kind).toBe("twitch");
    });

    it("propagates server validation errors as PulsarVendorError", async () => {
      // Force the mock to reject any kind we send via override.
      server.vendorOverride = (req) => {
        if (req === "CreateDestination") return { error: "test rejection" };
        return undefined;
      };
      await expect(
        client.destinations.create({ kind: "rtmp_custom", url: "rtmp://x", key: "k" }),
      ).rejects.toBeInstanceOf(PulsarVendorError);
    });
  });

  describe("video", () => {
    it("get returns the boot config in camelCase", async () => {
      const v = await client.video.get();
      expect(v).toEqual({
        fps: 60,
        width: 1920,
        height: 1080,
        videoBitrate: 6000,
        videoRateControl: "CBR",
        videoKeyintSec: 2,
        audioBitrate: 160,
      });
    });

    it("setBitrate mutates and is reflected on next get", async () => {
      const r = await client.video.setBitrate(4500);
      expect(r.changed).toBe(true);
      expect(r.videoBitrate).toBe(4500);

      const v = await client.video.get();
      expect(v.videoBitrate).toBe(4500);
    });

    it("server-rejected fps mutation surfaces as PulsarVendorError", async () => {
      // The client's set() doesn't expose fps -- but a low-level callVendor
      // call simulating a misuse should surface the error correctly.
      await expect(
        client.callVendor("SetVideoSettings", { fps: 120 }),
      ).rejects.toBeInstanceOf(PulsarVendorError);
    });
  });

  describe("adaptive", () => {
    it("getState returns camelCase + sane defaults", async () => {
      const s = await client.adaptive.getState();
      expect(s.enabled).toBe(true);
      expect(s.targetKbps).toBe(6000);
      expect(s.floorKbps).toBe(1800);
    });

    it("disable / enable cycles state", async () => {
      expect(await client.adaptive.disable()).toBe(false);
      expect((await client.adaptive.getState()).enabled).toBe(false);
      expect(await client.adaptive.enable()).toBe(true);
      expect((await client.adaptive.getState()).enabled).toBe(true);
    });
  });

  describe("events", () => {
    it("BitrateAdjusted vendor event maps to typed bitrateAdjusted", async () => {
      const received = new Promise<{
        bitrate: number;
        target: number;
        floor: number;
        reason: "drops" | "recovery";
        dropRatio: number;
      }>((resolve) => {
        client.on("bitrateAdjusted", (e) => resolve(e));
      });

      server.emitVendorEvent("BitrateAdjusted", {
        bitrate: 4500,
        target: 6000,
        floor: 1800,
        reason: "drops",
        drop_ratio: 0.027,
      });

      const e = await received;
      expect(e).toEqual({
        bitrate: 4500,
        target: 6000,
        floor: 1800,
        reason: "drops",
        dropRatio: 0.027,
      });
    });
  });

  describe("connection", () => {
    it("isConnected reflects state", async () => {
      expect(client.isConnected()).toBe(true);
      await client.disconnect();
      expect(client.isConnected()).toBe(false);
    });

    it("disconnect is idempotent", async () => {
      await client.disconnect();
      await client.disconnect();
      expect(client.isConnected()).toBe(false);
    });
  });
});
