# Host-controlled WebM transitions

Owner: `plugins/pulsar-frontend-stub`. The media resource lives in
`pulsar-media-transition.{h,cpp}`, with pure wire validation in
`pulsar-media-transition-config.cpp`; the frontend owns the lane mutex, atomic
frame-boundary routing, admission and lifecycle. The `pulsar-transitions`
obs-websocket vendor and `PulsarClient.transitions` expose the capability to
hosts such as Prism. This increment changes no Prism code and includes no
default media or artwork.

This capability uses Pulsar's existing dual-lane mode
(`PULSAR_DUAL_LANE_ENABLED=1`). Its existing activation and rollback gates
remain authoritative: outside that mode, or after a rollback freeze,
`operational` is false and configuration/switch requests are refused.

## Configuration and readiness

Use `CallVendorRequest` with `vendorName: "pulsar-transitions"`. `GetState`
takes `{}` and returns:

```json
{
  "runtime_instance_id": "<current runtime>",
  "operational": true,
  "configured": false,
  "config": {},
  "ready": false,
  "readiness_error": "NOT_CONFIGURED",
  "busy": false,
  "media_duration_ms": 0,
  "transition_duration_ms": 0,
  "role_map": {"on_air": "A", "preview": "B"},
  "last_switch": {}
}
```

`Configure` takes the current runtime identity and a complete configuration:

```json
{
  "runtime_instance_id": "<current runtime>",
  "config": {
    "path": "D:/media/operator-selected.webm",
    "cut_point_ms": 880,
    "volume": 0.5,
    "muted": false
  }
}
```

The path must be an absolute local `.webm` file. URLs, UNC paths, embedded
NULs, unknown configuration fields, fractional/bool cut points and invalid
gain/mute values are rejected. `cut_point_ms` is an integer in `[1, 19999]`.
`volume` defaults to 1 and is bounded to `[0,1]`; `muted` defaults to false.
No download, asset library, persistence or default file selection is performed
by Pulsar. A host stores its settings and reconfigures after a runtime restart.

Configuration checks the container header and creates a private software
decoder. Its first decoded frame is uploaded on the video thread without
starting playback or sound. Acceptance is **not decoder readiness**. Poll `GetState` until
`ready` is true before taking or switching. Readiness requires decoded video
dimensions and a measured duration, with the cut point inside that duration.
Unsupported/corrupt media stays unready. The total transition is capped at
20 seconds. OBS adds a 250 ms completion tail to the measured media duration;
`transition_duration_ms` reports that effective native duration. The API does
not stretch the animation to an unrelated fade duration.

An author must choose a fully opaque cut point in the actual animation.
Readiness validates decoding/timing, not opacity coverage or artistic quality.
Transparent WebM frames are composited by the existing OBS stinger renderer.
Only the media child's gain/mute is changed; surrounding scene and stream
audio gains are not changed. Preview lane playback mutes the media child;
the next Program/Take playback restores the configured gain/mute.

`Clear` takes `{"runtime_instance_id":"<current runtime>"}` and releases the
private decoder when idle. `Configure` and `Clear` cannot run during a
transition. The legacy fade/cut selection remains the fallback when the host
configuration is cleared. An explicitly supplied legacy `PULSAR_STINGER_ASSET`
is still supported; the former automatic demo-path resolution is removed.
The host API works without either legacy native-transition environment flag.

## Two distinct switch operations

**Take:** the existing `pulsar-scene-switch` Prepare/PreviewReady/Take path uses
the configured stinger automatically. Its role swap, CAS guards, freeze and
terminal `TakeCommitted` event are retained. An unready configured media
resource rejects admission instead of silently performing a hard cut.
The visual cut occurs at `cut_point_ms`; `TakeCommitted` is still the terminal
frame boundary after the transition, not the visual midpoint.

**Same physical lane:** stage an independently ready OBS scene first, then
call `SwitchLane`:

```json
{
  "runtime_instance_id": "<current runtime>",
  "command_id": "switch-unique-001",
  "lane_id": "A",
  "expected_scene_name": "currently selected scene",
  "scene_name": "prepared incoming scene"
}
```

The selected physical lane animates from its current composition to the
incoming one. The other lane, role map, views, video surfaces and output
bindings remain unchanged. The final frame callback installs the new child
in the existing lane root and updates the selected scene and matching
scene-switch revision. It does not perform a Take or increment the role-map
revision. Without a configured stinger this operation is an atomic hard cut.

The destination must be an existing OBS scene, distinct from the other lane's
selected scene and both physical lane roots. A same-scene request returns
`status: "noop"`. The caller stages the complete target graph and establishes
its source readiness before sending this request. Pulsar cannot infer a
scene replacement occurring inside a persistent browser/DOM source: a future
host integration must stage a distinct composition and invoke this API, not
mutate the visible page before asking for a transition. Plain scene selection
and Prepare remain preparation operations; they do not start this effect.

One transition is admitted at a time. Scene/input/settings mutations are
frozen from acceptance until the terminal frame callback. An outstanding
v1 preparation must be completed or expire before `SwitchLane` is admitted.
`expected_scene_name` and `runtime_instance_id` reject stale host commands.
Command IDs use 1–128 ASCII alphanumeric/`._:-` characters. Accepted outcomes
are retained for the runtime lifetime up to 1024 commands; capacity exhaustion
fails closed. Replaying an identical ID/payload returns its current outcome;
using an ID with another payload returns `IDEMPOTENCY_CONFLICT`. A retry while
the mutation gate is closed is rejected as `PREVIEW_FROZEN`; use readback.

## Completion, abort and errors

`SwitchLane` initially returns `status: "accepted"` and its runtime, command,
lane and scene identities. Subscribe to vendor event `LaneSwitchCompleted`,
or call `GetResult` with the runtime and command IDs. The terminal result has
`status: "completed" | "aborted" | "failed"`, `frame_id`, `pts_ns`, and the
unchanged `role_map`. Failed replacement carries `failure_reason`. Noop is
already terminal and does not fabricate frame evidence or emit a completion
event. `GetState.last_switch` is a convenience; `GetResult` is the correlated
source when another client may have submitted a later operation.

`Abort` takes the runtime and command IDs, responds `status: "aborting"`, and
restores the outgoing composition at a native frame boundary. Await the
terminal result. It cannot cancel another command or a v1 Take. Use the
existing scene-switch Abort for a Take. GetState/GetResult/Abort are the only
new operations allowed through the pending-mutation gate.

Vendor validation failures return `{ "error": "CODE" }`; transport admission
failures use the existing obs-websocket error envelope. Stable codes include
`REQUEST_INVALID`, `RUNTIME_MISMATCH`, `RUNTIME_UNAVAILABLE`, `CONFIG_INVALID`,
`LOCAL_PATH_REQUIRED`, `WEBM_REQUIRED`, `ASSET_MISSING`,
`ASSET_INVALID_CONTAINER`, `DECODER_UNAVAILABLE`, `MEDIA_NOT_READY`,
`DECODE_FAILED`, `DURATION_INVALID`, `CUT_POINT_INVALID`,
`CUT_POINT_OUTSIDE_MEDIA`, `VOLUME_INVALID`, `MUTED_INVALID`,
`TRANSITION_BUSY`, `SCENE_NOT_FOUND`, `SCENE_MISMATCH`, `LANE_ALIAS`,
`IDEMPOTENCY_CONFLICT`, `COMMAND_CAPACITY_REACHED`, `COMMAND_NOT_FOUND`,
`COMMAND_NOT_PENDING`, `TRANSITION_START_FAILED`, and `ATOMIC_SWAP_REJECTED`.

The libobs data transport does not preserve JSON nulls. Native empty state
uses empty objects/strings; the TypeScript client normalizes absent config,
last switch and readiness error to null. `client.transitions.configure(id,
null)` maps to the explicit Clear request.

## Host client example

```ts
const state = await client.transitions.getState();
await client.transitions.configure(state.runtime_instance_id, {
  path: selectedLocalWebM,
  cutPointMs: selectedOpaquePoint,
  volume: 0.5,
});
// Poll getState until ready, and stage/validate the incoming OBS scene.
client.on("laneSwitchCompleted", (result) => {
  // Match runtime_instance_id AND command_id before updating host UI.
});
await client.transitions.switchLane({
  runtimeInstanceId: state.runtime_instance_id,
  commandId: crypto.randomUUID(),
  lane: state.role_map.on_air,
  expectedSceneName: outgoingSceneName,
  sceneName: incomingSceneName,
});
```

## Validation and ownership

- `tests/pulsar-media-transition`: production parser cases, no capture/stream.
- `tests/pulsar-transition-controller`: existing native lifecycle regression.
- `packages/pulsar-client/tests/transitions.test.ts`: typed request mapping,
  explicit Clear, error propagation and completion events over the OBS client.
- `scripts/probe-configurable-transition.py`: isolated native runtime,
  generated synthetic alpha WebM, same-lane Program and Preview, Program
  recording pixels/audio and partial-alpha blending, Take after Preview,
  busy/stale/alias guards, replacement by a second file, media gain/mute,
  replay after abort and a hard cut after clear. Unready/invalid media cannot
  silently bypass the effect. Generated files are test artifacts, not product assets.

Run the opt-in probe with a dedicated build and explicit output directory:

```powershell
python scripts/probe-configurable-transition.py --exe <isolated-pulsar.exe> --output <repo-evidence-directory>
```

The probe never connects to an existing user runtime, never starts a stream,
and mutes capture inputs before recording its synthetic scenes. Publication,
runtime activation and any future Prism UI integration are separate work.
