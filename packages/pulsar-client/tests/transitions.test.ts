import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PulsarClient, PulsarVendorError } from "../src/index.js";
import { MockObsWebSocket } from "./mock-server.js";

describe("host-controlled transitions", () => {
  let server: MockObsWebSocket;
  let client: PulsarClient;
  beforeEach(async () => {
    server = await MockObsWebSocket.create();
    client = new PulsarClient();
    await client.connect({ url: server.url });
  });
  afterEach(async () => { await client.disconnect(); await server.close(); vi.restoreAllMocks(); });

  it("reads empty state without configuring or loading a default asset", async () => {
    const call = vi.spyOn(client.obs, "call").mockResolvedValue({ responseData: { configured: false, config: {}, last_switch: {}, readiness_error: "", ready: false } } as never);
    expect(await client.transitions.getState()).toMatchObject({ configured: false, config: null, last_switch: null, readiness_error: null, ready: false });
    expect(call).toHaveBeenCalledTimes(1);
    expect(call).toHaveBeenCalledWith("CallVendorRequest", {
      vendorName: "pulsar-transitions", requestType: "GetState", requestData: {},
    });
  });

  it("carries the runtime guard, custom file, cut point and media gain", async () => {
    const call = vi.spyOn(client.obs, "call").mockResolvedValue({ responseData: { ready: false } } as never);
    await client.transitions.configure("runtime-1", { path: "D:/effects/custom.webm", cutPointMs: 880, volume: 0.4, muted: true });
    expect(call).toHaveBeenLastCalledWith("CallVendorRequest", {
      vendorName: "pulsar-transitions", requestType: "Configure",
      requestData: { runtime_instance_id: "runtime-1", config: { path: "D:/effects/custom.webm", cut_point_ms: 880, volume: 0.4, muted: true } },
    });
    await client.transitions.configure("runtime-1", null);
    expect(call).toHaveBeenLastCalledWith("CallVendorRequest", expect.objectContaining({ requestType: "Clear", requestData: { runtime_instance_id: "runtime-1" } }));
  });

  it("does not turn same-lane acceptance into a Take or completion", async () => {
    const call = vi.spyOn(client.obs, "call").mockResolvedValue({ responseData: { command_id: "switch-1", status: "accepted" } } as never);
    const result = await client.transitions.switchLane({ runtimeInstanceId: "runtime-1", commandId: "switch-1", lane: "A", sceneName: "next", expectedSceneName: "old" });
    expect(result.status).toBe("accepted");
    expect(call).toHaveBeenCalledTimes(1);
    expect(call).toHaveBeenCalledWith("CallVendorRequest", {
      vendorName: "pulsar-transitions", requestType: "SwitchLane",
      requestData: { runtime_instance_id: "runtime-1", command_id: "switch-1", lane_id: "A", scene_name: "next", expected_scene_name: "old" },
    });
  });

  it("reads a correlated terminal result and aborts only a named command", async () => {
    const call = vi.spyOn(client.obs, "call").mockResolvedValue({ responseData: { status: "aborted", command_id: "switch-1" } } as never);
    expect((await client.transitions.getResult("runtime-1", "switch-1")).status).toBe("aborted");
    expect(call).toHaveBeenLastCalledWith("CallVendorRequest", expect.objectContaining({ requestType: "GetResult", requestData: { runtime_instance_id: "runtime-1", command_id: "switch-1" } }));
    await client.transitions.abort("runtime-1", "switch-1");
    expect(call).toHaveBeenLastCalledWith("CallVendorRequest", expect.objectContaining({ requestType: "Abort", requestData: { runtime_instance_id: "runtime-1", command_id: "switch-1" } }));
  });

  it.each(["MEDIA_NOT_READY", "CUT_POINT_OUTSIDE_MEDIA", "SCENE_MISMATCH", "RUNTIME_MISMATCH", "TRANSITION_BUSY"])("surfaces %s without falling back to a hard cut", async (error) => {
    const call = vi.spyOn(client.obs, "call").mockResolvedValue({ responseData: { error } } as never);
    await expect(client.transitions.getState()).rejects.toBeInstanceOf(PulsarVendorError);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it("routes the native completion event without consuming another vendor's event", async () => {
    const event = { runtime_instance_id: "runtime-1", command_id: "switch-1", lane_id: "A", scene_name: "next", status: "completed", frame_id: 70, pts_ns: 2000 };
    const received = new Promise<unknown>((resolve) => client.on("laneSwitchCompleted", resolve));
    server.emitEvent("VendorEvent", { vendorName: "pulsar-transitions", eventType: "LaneSwitchCompleted", eventData: event });
    expect(await received).toEqual(event);
  });
});
