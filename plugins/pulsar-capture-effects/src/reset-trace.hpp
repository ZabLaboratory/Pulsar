#pragma once

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

#include <atomic>
#include <cstdint>
#include <obs-module.h>

#if !defined(_WIN32)
#include <functional>
#include <thread>
#endif

namespace pulsar_reset_trace {

inline uint64_t next_object_id()
{
  static std::atomic<uint64_t> sequence{0};
  return sequence.fetch_add(1, std::memory_order_relaxed) + 1;
}

inline uint64_t current_thread_id()
{
#if defined(_WIN32)
  return static_cast<uint64_t>(GetCurrentThreadId());
#else
  return static_cast<uint64_t>(std::hash<std::thread::id>{}(
      std::this_thread::get_id()));
#endif
}

inline void event(const char *component, uint64_t object_id, const char *phase,
                  const void *source, const void *resource = nullptr,
                  uint64_t value = 0)
{
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  blog(LOG_INFO,
       "PULSAR_NATIVE_RESET_TRACE component=%s id=%llu tid=%llu phase=%s "
       "source=%p resource=%p value=%llu",
       component, static_cast<unsigned long long>(object_id),
       static_cast<unsigned long long>(current_thread_id()), phase, source,
       resource, static_cast<unsigned long long>(value));
#else
  (void)component;
  (void)object_id;
  (void)phase;
  (void)source;
  (void)resource;
  (void)value;
#endif
}

} // namespace pulsar_reset_trace
