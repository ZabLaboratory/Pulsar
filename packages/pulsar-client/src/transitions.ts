import type { PulsarClient } from "./client.js";
import { PulsarVendorError } from "./errors.js";

export type TransitionLane = "A" | "B";

export interface StingerConfig {
  /** Absolute local WebM path on the machine running Pulsar. No URL/download. */
  path: string;
  /** Author-chosen fully opaque point in the animation, in milliseconds. */
  cutPointMs: number;
  /** Media sound only; 0..1. Scene, microphone and music gains are unchanged. */
  volume?: number;
  muted?: boolean;
}

export interface LaneSwitchResult {
  runtime_instance_id: string;
  command_id: string;
  lane_id: TransitionLane;
  scene_name: string;
  status: "accepted" | "completed" | "aborted" | "failed" | "noop";
  frame_id?: number;
  pts_ns?: number;
  failure_reason?: string;
  role_map?: { on_air: TransitionLane; preview: TransitionLane };
}

export interface TransitionState {
  runtime_instance_id: string;
  operational: boolean;
  busy: boolean;
  configured: boolean;
  config: { path: string; cut_point_ms: number; volume: number; muted: boolean } | null;
  ready: boolean;
  readiness_error: string | null;
  media_duration_ms: number;
  /** Measured media duration plus OBS's fixed 250 ms completion tail. */
  transition_duration_ms: number;
  role_map: { on_air: TransitionLane; preview: TransitionLane };
  last_switch: LaneSwitchResult | null;
}

export interface SwitchLaneRequest {
  runtimeInstanceId: string;
  commandId: string;
  lane: TransitionLane;
  sceneName: string;
  expectedSceneName: string;
}

/** Runtime-local transition capability. Persistence and media selection belong
 * to the host. Configure once, wait for ready, then use Prepare/Take or switchLane.
 * Acceptance is not completion: observe laneSwitchCompleted or getResult.
 */
export class TransitionsNamespace {
  constructor(private readonly client: PulsarClient) {}

  getState(): Promise<TransitionState> {
    return this.request<TransitionState>("GetState", {}).then(normalizeState);
  }

  configure(runtimeInstanceId: string, config: StingerConfig | null): Promise<TransitionState> {
    if (config === null) return this.request<TransitionState>("Clear", { runtime_instance_id: runtimeInstanceId }).then(normalizeState);
    return this.request<TransitionState>("Configure", {
      runtime_instance_id: runtimeInstanceId,
      config: {
        path: config.path,
        cut_point_ms: config.cutPointMs,
        volume: config.volume ?? 1,
        muted: config.muted ?? false,
      },
    }).then(normalizeState);
  }

  switchLane(request: SwitchLaneRequest): Promise<LaneSwitchResult> {
    return this.request("SwitchLane", {
      runtime_instance_id: request.runtimeInstanceId,
      command_id: request.commandId,
      lane_id: request.lane,
      scene_name: request.sceneName,
      expected_scene_name: request.expectedSceneName,
    });
  }

  getResult(runtimeInstanceId: string, commandId: string): Promise<LaneSwitchResult> {
    return this.request("GetResult", { runtime_instance_id: runtimeInstanceId, command_id: commandId });
  }

  abort(runtimeInstanceId: string, commandId: string): Promise<{ command_id: string; status: "aborting" }> {
    return this.request("Abort", { runtime_instance_id: runtimeInstanceId, command_id: commandId });
  }

  private async request<T>(requestType: string, requestData: Record<string, unknown>): Promise<T> {
    const response = await this.client.call<{ responseData?: T & { error?: string } }>("CallVendorRequest", {
      vendorName: "pulsar-transitions", requestType, requestData,
    });
    const data = response.responseData;
    if (!data || typeof data !== "object") throw new PulsarVendorError(requestType, "RESPONSE_INVALID");
    if (data.error) throw new PulsarVendorError(requestType, data.error);
    return data;
  }
}

// obs_data's transport has no JSON null: empty objects/strings express the
// absent state on the wire. Present explicit nulls to host UI consumers.
function normalizeState(state: TransitionState): TransitionState {
  return { ...state, config: state.config?.path ? state.config : null,
    readiness_error: state.readiness_error || null,
    last_switch: state.last_switch?.command_id ? state.last_switch : null };
}
