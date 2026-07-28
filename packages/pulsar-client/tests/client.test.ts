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
      expect(dest.url).toBe("rtmps://ingest.global-contribute.live-video.net/app/");
      expect(dest.kind).toBe("twitch");
    });

    it("twitch pinned url is TLS, never cleartext rtmp", async () => {
      const dest = await client.destinations.create({
        kind: "twitch",
        key: "live_dummy_dummy",
      });
      expect(dest.url.startsWith("rtmps://")).toBe(true);
      expect(dest.url.startsWith("rtmp://")).toBe(false);
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
        videoEncoder: "x264",
        videoPreset: "veryfast",
        videoProfile: "high",
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

    it("rejects live encoder / preset / profile mutation (boot-fixed, ADR 004 §3.4)", async () => {
      await expect(
        client.callVendor("SetVideoSettings", { video_encoder: "nvenc" }),
      ).rejects.toBeInstanceOf(PulsarVendorError);
      await expect(
        client.callVendor("SetVideoSettings", { video_preset: "p1" }),
      ).rejects.toBeInstanceOf(PulsarVendorError);
      await expect(
        client.callVendor("SetVideoSettings", { video_profile: "main" }),
      ).rejects.toBeInstanceOf(PulsarVendorError);
      // bitrate mutation still works alongside the new rejections.
      const r = await client.video.setBitrate(3000);
      expect(r.changed).toBe(true);
    });
  });

  describe("capabilities", () => {
    it("get unwraps the wire {value} lists into flat typed arrays", async () => {
      const caps = await client.capabilities.get();
      expect(caps).toEqual({
        version: 1,
        encoders: ["x264", "nvenc"],
        activeEncoder: "x264",
        videoBitrateKbps: { min: 200, max: 50000 },
        audioBitrateKbps: [64, 96, 128, 160, 192, 224, 256, 320],
        filters: ["color_filter_v2", "noise_suppress_filter_v2"],
        sourceKinds: ["dshow_input", "window_capture"],
        destinationKinds: ["rtmp_custom", "vod_local", "twitch"],
        colorimetry: { colorSpace: "709", range: "Partial", format: "NV12" },
        audio: {
          monitoring: { available: true, deviceBound: false },
          trackCount: 6,
          boundTrackCount: 1,
          sampleRateHz: 48000,
          speakerLayout: "stereo",
          channels: 2,
        },
        regimes: {
          encoders: "boot-fixed",
          activeEncoder: "boot-fixed",
          videoBitrateKbps: "live",
          audioBitrateKbps: "live",
          filters: "live",
          sourceKinds: "live",
          destinationKinds: "live",
          colorimetry: "read-only",
          audioMonitoring: "read-only",
          audioTracks: "read-only",
          audioSampleRate: "read-only",
          audioSpeakerLayout: "read-only",
        },
      });
    });

    // ---- ADR 027 §3.2 / issue #141: manifest shape ------------------------

    it("carries a version and a regime for every declared entry", async () => {
      const caps = await client.capabilities.get();
      expect(caps.version).toBeGreaterThanOrEqual(1);
      for (const regime of Object.values(caps.regimes)) {
        expect(["live", "boot-fixed", "read-only"]).toContain(regime);
      }
      // The entries Pulsar declares today all carry one.
      expect(Object.keys(caps.regimes).sort()).toEqual([
        "activeEncoder",
        "audioBitrateKbps",
        "audioMonitoring",
        "audioSampleRate",
        "audioSpeakerLayout",
        "audioTracks",
        "colorimetry",
        "destinationKinds",
        "encoders",
        "filters",
        "sourceKinds",
        "videoBitrateKbps",
      ]);
    });

    // ---- ADR 027 §3.3 bloc 2 / issue #143: audio block --------------------

    it("declares monitoring availability and an unbound device as an explicit no", async () => {
      // Criteria 1 & 2: the "no" must be readable, not inferred from a silence.
      const caps = await client.capabilities.get();
      expect(caps.audio.monitoring).toEqual({ available: true, deviceBound: false });
      expect(caps.audio.monitoring?.deviceId).toBeUndefined();
      expect(caps.audio.monitoring?.deviceName).toBeUndefined();
    });

    it("does not declare monitoring live while Pulsar has no write path", async () => {
      // Criterion 4: `live` requires the write AND the read-back to be really
      // supported hot. Nothing in Pulsar calls obs_set_audio_monitoring_device.
      const caps = await client.capabilities.get();
      expect(caps.regimes.audioMonitoring).toBe("read-only");
      expect(caps.regimes.audioMonitoring).not.toBe("live");
    });

    it("surfaces the bound monitoring device when there is one", async () => {
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 1,
              capabilities: {
                audio_monitoring: {
                  applicability: "read-only",
                  available: true,
                  device_bound: true,
                  device_id: "{0.0.0.0}.{abcd}",
                  device_name: "Headphones (Realtek)",
                },
              },
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.audio.monitoring).toEqual({
        available: true,
        deviceBound: true,
        deviceId: "{0.0.0.0}.{abcd}",
        deviceName: "Headphones (Realtek)",
      });
    });

    it("reports monitoring unavailable without inventing a device", async () => {
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 1,
              capabilities: {
                audio_monitoring: {
                  applicability: "read-only",
                  available: false,
                  device_bound: false,
                },
              },
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.audio.monitoring).toEqual({ available: false, deviceBound: false });
    });

    it("leaves the audio block absent rather than answering for a silent server", async () => {
      // A pre-#143 Pulsar says nothing about audio. `undefined` must NOT be
      // decoded as "monitoring unavailable" -- an absence is not a "no".
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 1,
              encoders: [{ value: "x264" }],
              active_encoder: "x264",
              capabilities: { active_encoder: { applicability: "boot-fixed", value: "x264" } },
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.audio).toEqual({});
      expect(caps.audio.monitoring).toBeUndefined();
      expect(caps.regimes.audioMonitoring).toBeUndefined();
    });

    it("omits audio fields the server declared absent, never defaulting them", async () => {
      // Criterion 3: read from libobs or absent. Off-air there is no bound
      // track count, and an unknown speaker layout is omitted outright.
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 1,
              capabilities: {
                audio_tracks: { applicability: "read-only", count: 6 },
                audio_sample_rate: { applicability: "read-only", hz: 44100 },
              },
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.audio.trackCount).toBe(6);
      expect(caps.audio.boundTrackCount).toBeUndefined();
      expect(caps.audio.sampleRateHz).toBe(44100);
      expect(caps.audio.speakerLayout).toBeUndefined();
      expect(caps.audio.channels).toBeUndefined();
    });

    it("ignores a malformed monitoring entry instead of half-decoding it", async () => {
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 1,
              capabilities: {
                // `available` is not a boolean: the entry states nothing usable.
                audio_monitoring: { applicability: "read-only", available: "yes" },
              },
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.audio.monitoring).toBeUndefined();
      // The regime was still declared and is still reported.
      expect(caps.regimes.audioMonitoring).toBe("read-only");
    });

    it("ignores an unknown block and an unknown entry without erroring", async () => {
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 99,
              encoders: [{ value: "x264" }],
              active_encoder: "x264",
              video_bitrate: { min: 200, max: 50000 },
              audio_bitrate: [{ value: 128 }],
              capabilities: {
                video_bitrate: { applicability: "live", min: 200, max: 50000 },
                // a block this client version has never heard of
                colorimetry: { applicability: "read-only", spaces: [{ value: "sRGB" }] },
              },
              // a top-level block this client version has never heard of
              inventories: { filters: [{ value: "color_filter_v2" }] },
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.version).toBe(99);
      expect(caps.regimes.videoBitrateKbps).toBe("live");
      expect(caps.encoders).toEqual(["x264"]);
    });

    it("stays backward compatible with a pre-#141 payload (no version, no regimes)", async () => {
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              encoders: [{ value: "x264" }],
              active_encoder: "x264",
              video_bitrate: { min: 200, max: 50000 },
              audio_bitrate: [{ value: 128 }],
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.version).toBe(0);
      expect(caps.regimes).toEqual({});
      expect(caps.videoBitrateKbps).toEqual({ min: 200, max: 50000 });
    });

    it("declares a capability absent rather than inventing a bound", async () => {
      // Pulsar omits the entry when libobs exposes no readable window
      // (ADR 027 §3.2, "absence positive"). The client must not fabricate one,
      // and must not report a regime for something that was never declared.
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 1,
              encoders: [{ value: "x264" }],
              active_encoder: "x264",
              capabilities: {
                encoders: { applicability: "boot-fixed", values: [{ value: "x264" }] },
                active_encoder: { applicability: "boot-fixed", value: "x264" },
              },
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.videoBitrateKbps).toEqual({ min: 0, max: 0 });
      expect(caps.audioBitrateKbps).toEqual([]);
      expect(caps.regimes.videoBitrateKbps).toBeUndefined();
      expect(caps.regimes.audioBitrateKbps).toBeUndefined();
    });

    it("drops a regime string it does not know instead of coercing it", async () => {
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 1,
              video_bitrate: { min: 200, max: 50000 },
              capabilities: { video_bitrate: { applicability: "sometimes", min: 200, max: 50000 } },
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.regimes.videoBitrateKbps).toBeUndefined();
    });

    // ---- ADR 027 §3.3 blocks 3 + 4 / issue #144: inventories + colorimetry --

    it("unwraps the presence-only inventories with their regime", async () => {
      const caps = await client.capabilities.get();
      expect(caps.filters).toEqual(["color_filter_v2", "noise_suppress_filter_v2"]);
      expect(caps.sourceKinds).toEqual(["dshow_input", "window_capture"]);
      expect(caps.destinationKinds).toEqual(["rtmp_custom", "vod_local", "twitch"]);
      expect(caps.regimes.filters).toBe("live");
      expect(caps.regimes.sourceKinds).toBe("live");
      expect(caps.regimes.destinationKinds).toBe("live");
    });

    it("keeps the filter inventory a bare presence list, never a bound", async () => {
      // ADR 027 §3.1 / ADR 023 §3.3: the manifest says WHICH filters exist. Any
      // bound a server tried to smuggle beside them is not surfaced by the
      // typed shape -- the whitelist stays the only source of what is settable.
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 1,
              capabilities: {
                filters: {
                  applicability: "live",
                  values: [{ value: "color_filter_v2", min: 0, max: 100 }],
                  bounds: { opacity: { min: 0, max: 100 } },
                },
              },
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.filters).toEqual(["color_filter_v2"]);
      expect(Object.keys(caps)).not.toContain("bounds");
      expect(caps.filters.every((f) => typeof f === "string")).toBe(true);
    });

    it("leaves the consumer's static list intact when an inventory is absent", async () => {
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? { version: 1, capabilities: { encoders: { applicability: "boot-fixed" } } }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.filters).toEqual([]);
      expect(caps.sourceKinds).toEqual([]);
      expect(caps.destinationKinds).toEqual([]);
      expect(caps.regimes.filters).toBeUndefined();
      expect(caps.regimes.destinationKinds).toBeUndefined();
    });

    it("ignores a destination kind it does not know instead of routing it", async () => {
      // ADR 010's discriminated union is untouched by the manifest: an unknown
      // kind travels as a plain string and is the consumer's to drop.
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 1,
              capabilities: {
                destination_kinds: {
                  applicability: "live",
                  values: [{ value: "twitch" }, { value: "srt_relay_from_the_future" }],
                },
              },
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.destinationKinds).toContain("srt_relay_from_the_future");
      const known = ["rtmp_custom", "vod_local", "twitch"];
      expect(caps.destinationKinds.filter((k) => known.includes(k))).toEqual(["twitch"]);
    });

    it("reports colorimetry read-only, and drops a partial entry", async () => {
      const caps = await client.capabilities.get();
      expect(caps.colorimetry).toEqual({ colorSpace: "709", range: "Partial", format: "NV12" });
      expect(caps.regimes.colorimetry).toBe("read-only");

      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 1,
              capabilities: { video_colorimetry: { applicability: "read-only", value: "709" } },
            }
          : undefined;
      const partial = await client.capabilities.get();
      expect(partial.colorimetry).toBeUndefined();
    });

    it("drops malformed inventory items rather than half-reading them", async () => {
      server.vendorOverride = (req) =>
        req === "GetCapabilities"
          ? {
              version: 1,
              capabilities: {
                filters: {
                  applicability: "live",
                  values: [{ value: "color_filter_v2" }, { value: 42 }, { value: "" }, null],
                },
                source_kinds: { applicability: "live", values: "not-an-array" },
              },
            }
          : undefined;
      const caps = await client.capabilities.get();
      expect(caps.filters).toEqual(["color_filter_v2"]);
      expect(caps.sourceKinds).toEqual([]);
    });

    it("always advertises at least x264", async () => {
      const caps = await client.capabilities.get();
      expect(caps.encoders).toContain("x264");
    });

    it("surfaces a server error field as PulsarVendorError", async () => {
      server.vendorOverride = (req) =>
        req === "GetCapabilities" ? { error: "detection failed" } : undefined;
      await expect(client.capabilities.get()).rejects.toBeInstanceOf(PulsarVendorError);
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

  describe("audio", () => {
    it("specialInputs resolves the mic slot name", async () => {
      const s = await client.audio.specialInputs();
      expect(s.mic1).toBe("Mic/Aux");
    });

    it("listInputs surfaces name + kind", async () => {
      const inputs = await client.audio.listInputs();
      expect(inputs).toEqual([{ name: "Mic/Aux", kind: "wasapi_input_capture" }]);
    });

    it("mute / unmute / toggle round trip", async () => {
      expect(await client.audio.isMuted("Mic/Aux")).toBe(false);

      await client.audio.setMuted("Mic/Aux", true);
      expect(await client.audio.isMuted("Mic/Aux")).toBe(true);

      expect(await client.audio.toggleMuted("Mic/Aux")).toBe(false);
      expect(await client.audio.isMuted("Mic/Aux")).toBe(false);
    });

    it("listDevices returns the wasapi device_id list property", async () => {
      const devices = await client.audio.listDevices("Mic/Aux");
      expect(devices).toEqual([
        { id: "default", name: "Default", enabled: true },
        { id: "usb-mic-1", name: "USB Mic", enabled: true },
      ]);
    });

    it("setDevice applies device_id on top of existing settings", async () => {
      await client.audio.setDevice("Mic/Aux", "usb-mic-1");
      expect(server.inputs.get("Mic/Aux")?.settings["device_id"]).toBe("usb-mic-1");
    });

    it("InputMuteStateChanged event maps to typed inputMuteStateChanged", async () => {
      const received = new Promise<{ inputName: string; inputMuted: boolean }>((resolve) => {
        client.on("inputMuteStateChanged", (e) => resolve(e));
      });

      server.emitEvent("InputMuteStateChanged", { inputName: "Mic/Aux", inputMuted: true });

      expect(await received).toEqual({ inputName: "Mic/Aux", inputMuted: true });
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
