#pragma once

#include <cstdint>

namespace pulsar_runtime {

// Main-thread, process-level ownership, matching libobs's explicit lifecycle.
// Both calls are idempotent. The return value is a Windows HRESULT (0 elsewhere).
std::uint32_t startup_realtime_work_queue();

// Call only after obs_shutdown has drained source callbacks. On unsafe teardown
// aborts, retain the platform until process exit instead of stopping live work.
std::uint32_t shutdown_realtime_work_queue();

} // namespace pulsar_runtime
