#pragma once

// Process-local runtime telemetry bridge shared by the headless frontend stub,
// obs-websocket and the patched win-dshow producer.  The websocket module is a
// DLL and the frontend/virtual-camera code are linked into separate binaries;
// libobs's global procedure handler is therefore the narrow ABI seam.  The
// bridge is deliberately opt-in: without PULSAR_TRACE_PATH and explicit
// transaction metadata, the normal OBS request and Cut behaviour is unchanged
// and no evidence is emitted.

#include <obs.h>

#include <cstdint>
#include <cstring>

#include "pulsar-runtime-telemetry-abi.h"

namespace pulsar_runtime_telemetry {

inline constexpr char kBeginTakeProc[] = "pulsar_runtime_telemetry_begin_take";
inline constexpr char kCancelTakeProc[] = "pulsar_runtime_telemetry_cancel_take";
inline constexpr char kSnapshotFrameProc[] = "pulsar_runtime_telemetry_snapshot_frame";

inline constexpr size_t kIdentifierCapacity = PULSAR_RUNTIME_TELEMETRY_IDENTIFIER_CAPACITY;
using FrameMetadata = pulsar_runtime_frame_metadata;

// Keep the three observations at the proc boundary visible to the websocket
// adapter. A boolean-only helper used to collapse "proc not registered",
// "producer disabled", and "envelope rejected" into the same false value;
// that made a trace with no TakeAccepted event look like a healthy Cut.
struct BeginTakeStatus {
    bool called = false;
    bool available = false;
    bool accepted = false;
};

inline void copy_identifier(char *destination, size_t capacity, const char *value)
{
    if (!destination || capacity == 0)
        return;
    if (!value)
        value = "";
    std::strncpy(destination, value, capacity - 1);
    destination[capacity - 1] = '\0';
}

// Start/replace the metadata context for the next scene-switch ingress.  A
// missing bridge is intentionally reported as false so an ordinary OBS
// frontend continues to execute its legacy path.
inline BeginTakeStatus begin_take_status(const char *command_id, const char *intent_id,
                                         const char *runtime_instance_id, const char *take_command_id,
                                         const char *target_lane_id, const char *target_scene_id,
                                         int64_t freeze_until_monotonic_ns, const char *payload_sha256)
{
    BeginTakeStatus status;
    proc_handler_t *handler = obs_get_proc_handler();
    if (!handler)
        return status;

    calldata_t cd = {};
    calldata_set_string(&cd, "command_id", command_id ? command_id : "");
    calldata_set_string(&cd, "intent_id", intent_id ? intent_id : "");
    calldata_set_string(&cd, "runtime_instance_id", runtime_instance_id ? runtime_instance_id : "");
    calldata_set_string(&cd, "take_command_id", take_command_id ? take_command_id : "");
    calldata_set_string(&cd, "target_lane_id", target_lane_id ? target_lane_id : "");
    calldata_set_string(&cd, "target_scene_id", target_scene_id ? target_scene_id : "");
    calldata_set_int(&cd, "freeze_until_monotonic_ns", freeze_until_monotonic_ns);
    calldata_set_string(&cd, "payload_sha256", payload_sha256 ? payload_sha256 : "");
    status.called = proc_handler_call(handler, kBeginTakeProc, &cd);
    if (status.called) {
        calldata_get_bool(&cd, "available", &status.available);
        calldata_get_bool(&cd, "accepted", &status.accepted);
    }
    calldata_free(&cd);
    return status;
}

inline bool begin_take(const char *command_id, const char *intent_id, const char *runtime_instance_id,
                       const char *take_command_id, const char *target_lane_id, const char *target_scene_id,
                       int64_t freeze_until_monotonic_ns, const char *payload_sha256)
{
    return begin_take_status(command_id, intent_id, runtime_instance_id, take_command_id, target_lane_id,
                             target_scene_id, freeze_until_monotonic_ns, payload_sha256)
        .accepted;
}

// Clear metadata for a request that did not enter the atomic Take path.  The
// producer consumes the pending envelope synchronously when a swap is queued;
// every other request outcome must explicitly retire it so a later scene
// mutation cannot inherit another command's correlation identity.
inline void cancel_take()
{
    proc_handler_t *handler = obs_get_proc_handler();
    if (!handler)
        return;
    calldata_t cd = {};
    proc_handler_call(handler, kCancelTakeProc, &cd);
    calldata_free(&cd);
}

// Read the latest committed frame context into an output-output-safe struct.
// This is used by the patched win-dshow producer once per raw output frame so
// the DirectShow consumer can correlate the actual slot it consumed.
inline bool snapshot_frame(FrameMetadata *metadata)
{
    if (!metadata)
        return false;
    *metadata = {};

    proc_handler_t *handler = obs_get_proc_handler();
    if (!handler)
        return false;

    calldata_t cd = {};
    const bool called = proc_handler_call(handler, kSnapshotFrameProc, &cd);
    if (!called) {
        calldata_free(&cd);
        return false;
    }

    metadata->valid = calldata_bool(&cd, "valid");
    metadata->server_seq = calldata_int(&cd, "server_seq");
    metadata->frame_id = calldata_int(&cd, "frame_id");
    metadata->pts_ns = calldata_int(&cd, "pts_ns");
    metadata->program_revision = calldata_int(&cd, "program_revision");
    metadata->preview_revision = calldata_int(&cd, "preview_revision");
    metadata->role_map_revision = calldata_int(&cd, "role_map_revision");
    copy_identifier(metadata->runtime_instance_id, sizeof(metadata->runtime_instance_id),
                    calldata_string(&cd, "runtime_instance_id"));
    copy_identifier(metadata->command_id, sizeof(metadata->command_id), calldata_string(&cd, "command_id"));
    copy_identifier(metadata->intent_id, sizeof(metadata->intent_id), calldata_string(&cd, "intent_id"));
    copy_identifier(metadata->take_command_id, sizeof(metadata->take_command_id),
                    calldata_string(&cd, "take_command_id"));
    calldata_free(&cd);
    return true;
}

} // namespace pulsar_runtime_telemetry
