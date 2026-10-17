#include "reset-trace.hpp"
#include "group-composition-world-source.hpp"
#include <graphics/matrix4.h>
#include <obs-module.h>
#include <new>

namespace {
constexpr const char *source_id = "pulsar_composition_world_affine";
constexpr const char *dependency_source_id = "pulsar_composition_dependency";
struct WorldSource {
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  uint64_t trace_id = 0;
#endif
  obs_source_t *owner = nullptr;
  obs_source_t *base = nullptr;
  matrix4 affine{};
  uint32_t width = 0, height = 0;
};
struct DependencySource {
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  uint64_t trace_id = 0;
#endif
  obs_source_t *owner = nullptr;
  obs_source_t *dependency = nullptr;
};

const char *name(void *) { return "Pulsar live affine contribution"; }
void *create(obs_data_t *, obs_source_t *owner)
{
  auto *state = new (std::nothrow) WorldSource;
  if (state) {
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    state->trace_id = pulsar_reset_trace::next_object_id();
    const uint64_t trace_id = state->trace_id;
#else
    constexpr uint64_t trace_id = 0;
#endif
    state->owner = owner;
    matrix4_identity(&state->affine);
    pulsar_reset_trace::event("world-source", trace_id, "create",
                              owner);
  }
  return state;
}
void destroy(void *data)
{
  auto *state = static_cast<WorldSource *>(data);
  if (!state)
    return;
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  const uint64_t trace_id = state->trace_id;
#else
  constexpr uint64_t trace_id = 0;
#endif
  pulsar_reset_trace::event("world-source", trace_id, "destroy-enter",
                            state->owner, state->base);
  if (state->base) {
    pulsar_reset_trace::event("world-source", trace_id,
                              "base-release-begin", state->owner,
                              state->base);
    obs_source_release(state->base);
    pulsar_reset_trace::event("world-source", trace_id, "base-release-end",
                              state->owner);
  }
  pulsar_reset_trace::event("world-source", trace_id, "delete-begin",
                            state->owner);
  delete state;
  pulsar_reset_trace::event("world-source", trace_id, "delete-end", nullptr);
}
uint32_t width(void *data) { return static_cast<WorldSource *>(data)->width; }
uint32_t height(void *data) { return static_cast<WorldSource *>(data)->height; }
void active(void *data, obs_source_enum_proc_t callback, void *param)
{
  auto *state = static_cast<WorldSource *>(data);
  if (state->base)
    callback(state->owner, state->base, param);
}
void render(void *data, gs_effect_t *)
{
  const auto *state = static_cast<WorldSource *>(data);
  if (!state->base)
    return;
  const bool previous_srgb = gs_set_linear_srgb(false);
  gs_matrix_push();
  gs_matrix_mul(&state->affine);
  obs_source_video_render(state->base);
  gs_matrix_pop();
  gs_set_linear_srgb(previous_srgb);
}
const char *dependency_name(void *)
{
  return "Pulsar composition dependency";
}
void *dependency_create(obs_data_t *, obs_source_t *owner)
{
  auto *state = new (std::nothrow) DependencySource;
  if (state) {
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    state->trace_id = pulsar_reset_trace::next_object_id();
    const uint64_t trace_id = state->trace_id;
#else
    constexpr uint64_t trace_id = 0;
#endif
    state->owner = owner;
    pulsar_reset_trace::event("mask-dependency", trace_id, "create",
                              owner);
  }
  return state;
}
void dependency_destroy(void *data)
{
  auto *state = static_cast<DependencySource *>(data);
  if (!state)
    return;
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  const uint64_t trace_id = state->trace_id;
#else
  constexpr uint64_t trace_id = 0;
#endif
  pulsar_reset_trace::event("mask-dependency", trace_id, "destroy-enter",
                            state->owner, state->dependency);
  if (state->dependency) {
    pulsar_reset_trace::event("mask-dependency", trace_id,
                              "dependency-release-begin", state->owner,
                              state->dependency);
    obs_source_release(state->dependency);
    pulsar_reset_trace::event("mask-dependency", trace_id,
                              "dependency-release-end", state->owner);
  }
  pulsar_reset_trace::event("mask-dependency", trace_id, "delete-begin",
                            state->owner);
  delete state;
  pulsar_reset_trace::event("mask-dependency", trace_id, "delete-end",
                            nullptr);
}
uint32_t dependency_width(void *) { return 1; }
uint32_t dependency_height(void *) { return 1; }
void dependency_active(void *data, obs_source_enum_proc_t callback, void *param)
{
  auto *state = static_cast<DependencySource *>(data);
  if (state->dependency)
    callback(state->owner, state->dependency, param);
}
void dependency_render(void *, gs_effect_t *)
{
  // The source exists only to participate in OBS' active-source graph. It
  // emits no pixels; coverage is sampled by the composition filter itself.
}
} // namespace

void register_group_world_source()
{
  obs_source_info info{};
  info.id = source_id;
  info.type = OBS_SOURCE_TYPE_INPUT;
  info.output_flags = OBS_SOURCE_VIDEO | OBS_SOURCE_CUSTOM_DRAW;
  info.get_name = name;
  info.create = create;
  info.destroy = destroy;
  info.get_width = width;
  info.get_height = height;
  info.video_render = render;
  info.enum_active_sources = active;
  info.enum_all_sources = active;
  obs_register_source(&info);

  obs_source_info dependency{};
  dependency.id = dependency_source_id;
  dependency.type = OBS_SOURCE_TYPE_INPUT;
  dependency.output_flags = OBS_SOURCE_VIDEO | OBS_SOURCE_CUSTOM_DRAW;
  dependency.get_name = dependency_name;
  dependency.create = dependency_create;
  dependency.destroy = dependency_destroy;
  dependency.get_width = dependency_width;
  dependency.get_height = dependency_height;
  dependency.video_render = dependency_render;
  dependency.enum_active_sources = dependency_active;
  dependency.enum_all_sources = dependency_active;
  obs_register_source(&dependency);
}

obs_source_t *create_group_world_source(const char *name, obs_source_t *base,
                                       const group_composition::Affine &affine,
                                       uint32_t width, uint32_t height)
{
  if (!base || !width || !height)
    return nullptr;
  obs_source_t *source = obs_source_create_private(source_id, name, nullptr);
  if (!source)
    return nullptr;
  auto *state = static_cast<WorldSource *>(obs_obj_get_data(source));
  if (!state || !(state->base = obs_source_get_ref(base))) {
    obs_source_release(source);
    return nullptr;
  }
  state->width = width;
  state->height = height;
  state->affine.x.x = float(affine.m00);
  state->affine.x.y = float(affine.m10);
  state->affine.y.x = float(affine.m01);
  state->affine.y.y = float(affine.m11);
  state->affine.t.x = float(affine.tx);
  state->affine.t.y = float(affine.ty);
  return source;
}

obs_source_t *create_group_dependency_source(const char *name,
                                              obs_source_t *dependency)
{
  if (!dependency)
    return nullptr;
  obs_source_t *source = obs_source_create_private(dependency_source_id, name,
                                                  nullptr);
  if (!source)
    return nullptr;
  auto *state = static_cast<DependencySource *>(obs_obj_get_data(source));
  if (!state || !(state->dependency = obs_source_get_ref(dependency))) {
    obs_source_release(source);
    return nullptr;
  }
  return source;
}
