#pragma once

// C-compatible names/layout used by the patched win-dshow producer.  Keep
// this header free of obs.hpp/nlohmann/C++ namespace constructs: virtualcam.c
// is compiled as C and reaches the frontend through libobs's proc handler.

#include <stdint.h>

#define PULSAR_RUNTIME_TELEMETRY_BEGIN_PROC "pulsar_runtime_telemetry_begin_take"
#define PULSAR_RUNTIME_TELEMETRY_CANCEL_PROC "pulsar_runtime_telemetry_cancel_take"
#define PULSAR_RUNTIME_TELEMETRY_SNAPSHOT_PROC "pulsar_runtime_telemetry_snapshot_frame"
#define PULSAR_RUNTIME_TELEMETRY_IDENTIFIER_CAPACITY 129

struct pulsar_runtime_frame_metadata {
	uint32_t valid;
	uint64_t server_seq;
	uint64_t frame_id;
	uint64_t pts_ns;
	uint64_t program_revision;
	uint64_t preview_revision;
	uint64_t role_map_revision;
	char runtime_instance_id[PULSAR_RUNTIME_TELEMETRY_IDENTIFIER_CAPACITY];
	char command_id[PULSAR_RUNTIME_TELEMETRY_IDENTIFIER_CAPACITY];
	char intent_id[PULSAR_RUNTIME_TELEMETRY_IDENTIFIER_CAPACITY];
	char take_command_id[PULSAR_RUNTIME_TELEMETRY_IDENTIFIER_CAPACITY];
};
