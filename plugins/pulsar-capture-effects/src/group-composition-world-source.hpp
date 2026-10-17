#pragma once

#include "group-composition-plan.hpp"
#include <obs.h>

// Private live source, owned by the admitted composition. It applies a full
// source-to-scene affine once; it owns no physical device or retained frame.
void register_group_world_source();
obs_source_t *create_group_world_source(const char *name, obs_source_t *base,
                                       const group_composition::Affine &affine,
                                       uint32_t width, uint32_t height);
// Private no-paint scene child that keeps a resolved atlas source active for
// as long as the admitted composition containing it is active.
obs_source_t *create_group_dependency_source(const char *name,
                                              obs_source_t *dependency);
