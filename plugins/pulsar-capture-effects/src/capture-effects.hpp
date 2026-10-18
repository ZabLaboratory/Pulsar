#pragma once

#include <obs.h>

#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
#include <cstdint>

struct CaptureShaderCreationTiming {
  double milliseconds = 0;
  uint32_t calls = 0;
};

// The active group-build callback owns this collector on its graphics thread.
CaptureShaderCreationTiming *capture_effects_exchange_shader_creation_timing(
    CaptureShaderCreationTiming *timing);
#endif

// Bind the containing scene while its caller owns a source reference. The
// filter retains only a weak reference; rendering never searches by name.
bool capture_effects_bind_background_parent(obs_source_t *filter,
                                            obs_source_t *parent);
