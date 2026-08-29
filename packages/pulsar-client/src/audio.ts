import type { PulsarClient } from "./client.js";
import {
  monitoringDeviceListFromWire,
  programAudioRouteFromWire,
  type WireGetMonitoringDeviceListResponse,
  type WireGetProgramAudioRouteResponse,
  type WireSetMonitoringDeviceRequest,
  type WireSetMonitoringDeviceResponse,
} from "./wire.js";
import type {
  AudioDevice,
  AudioInput,
  MonitoringDevice,
  MonitoringDeviceList,
  ProgramAudioRoute,
  SpecialInputs,
} from "./types.js";

/**
 * Mic / audio-input control. Wraps the native obs-websocket v5 Input*
 * requests (no vendor plugin involved) -- GetSpecialInputs to resolve the
 * mic slot name, GetInputList/GetInputMute/SetInputMute/ToggleInputMute
 * for mute state, GetInputPropertiesListPropertyItems("device_id") to
 * enumerate capture devices on a wasapi_input_capture input, and
 * SetInputSettings to switch device.
 *
 * Stream-level by design: mic state lives on the OBS input, not on any
 * scene, so it survives scene switches for free.
 */
export class AudioNamespace {
  constructor(private readonly client: PulsarClient) {}

  /** Names of the Mic/Auxiliary input slots (mic1..mic4) configured in Pulsar. */
  async specialInputs(): Promise<SpecialInputs> {
    const resp = await this.client.call("GetSpecialInputs");
    return resp as unknown as SpecialInputs;
  }

  /** All audio-capable inputs (kind starts with wasapi_input/output_capture, etc). */
  async listInputs(): Promise<AudioInput[]> {
    const resp = await this.client.call("GetInputList");
    const inputs = (resp as unknown as { inputs: Array<Record<string, unknown>> }).inputs ?? [];
    return inputs.map((i) => ({
      name: String(i.inputName ?? ""),
      kind: String(i.inputKind ?? ""),
    }));
  }

  async isMuted(inputName: string): Promise<boolean> {
    const resp = await this.client.call("GetInputMute", { inputName });
    return Boolean((resp as unknown as { inputMuted: boolean }).inputMuted);
  }

  async setMuted(inputName: string, muted: boolean): Promise<void> {
    await this.client.call("SetInputMute", { inputName, inputMuted: muted });
  }

  /** Returns the new mute state after toggling. */
  async toggleMuted(inputName: string): Promise<boolean> {
    const resp = await this.client.call("ToggleInputMute", { inputName });
    return Boolean((resp as unknown as { inputMuted: boolean }).inputMuted);
  }

  /** Enumerate the physical devices selectable on a wasapi_input_capture input. */
  async listDevices(inputName: string): Promise<AudioDevice[]> {
    const resp = await this.client.call("GetInputPropertiesListPropertyItems", {
      inputName,
      propertyName: "device_id",
    } as never);
    const items = (resp as unknown as { propertyItems: Array<Record<string, unknown>> }).propertyItems ?? [];
    return items.map((i) => ({
      id: String(i.itemValue ?? ""),
      name: String(i.itemName ?? ""),
      enabled: i.itemEnabled !== false,
    }));
  }

  /** Switch the capture device of a mic input. Applies on top of existing settings. */
  async setDevice(inputName: string, deviceId: string): Promise<void> {
    await this.client.call("SetInputSettings", {
      inputName,
      inputSettings: { device_id: deviceId },
      overlay: true,
    } as never);
  }

  /**
   * Playback devices monitoring can be routed to, read from the machine at
   * call time (#173). `"default"` follows the OS default device.
   *
   * Offer these in a selector only when the manifest declares
   * `audio.monitoring.deviceSelectable === true`: on a build without a
   * monitoring backend the list is empty and every write refuses.
   */
  async listMonitoringDevices(): Promise<MonitoringDeviceList> {
    const resp = await this.client.callVendor<object, WireGetMonitoringDeviceListResponse>(
      "GetMonitoringDeviceList",
    );
    return monitoringDeviceListFromWire(resp);
  }

  /**
   * Routes monitoring to `deviceId`. Throws `PulsarVendorError` when the id is
   * not one the machine enumerates — the server refuses before writing rather
   * than accepting an id into silence.
   *
   * Returns the device libobs reports in force after the write (read-back).
   */
  async setMonitoringDevice(deviceId: string): Promise<MonitoringDevice> {
    const resp = await this.client.callVendor<
      WireSetMonitoringDeviceRequest,
      WireSetMonitoringDeviceResponse
    >("SetMonitoringDevice", { device_id: deviceId });
    return { id: resp.device_id ?? "", name: resp.device_name ?? "" };
  }

  /**
   * Reads the explicit common Program audio route (#245). The route is
   * independent of the dual-lane video Cut; the snapshot includes output and
   * source identities plus the bounded recent PTS evidence from the actual
   * mixer indexes consumed by the encoders.
   */
  async programRoute(): Promise<ProgramAudioRoute> {
    const resp = await this.client.callVendor<object, WireGetProgramAudioRouteResponse>(
      "GetProgramAudioRoute",
    );
    return programAudioRouteFromWire(resp);
  }
}
