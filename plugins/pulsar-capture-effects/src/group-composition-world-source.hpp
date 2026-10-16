#pragma once

#include "group-composition-plan.hpp"
#include <obs.h>

// Private live source, owned by the admitted composition. It applies a full
// source-to-scene affine once; it owns no physical device or retained frame.
void register_group_world_source();
obs_source_t *create_group_world_source(const char *name, obs_source_t *base,
                                       const group_composition::Affine &affine,
                                       uint32_t width, uint32_t height);
