#pragma once

#include <obs.h>

// Bind the containing scene while its caller owns a source reference. The
// filter retains only a weak reference; rendering never searches by name.
bool capture_effects_bind_background_parent(obs_source_t *filter,
                                            obs_source_t *parent);
