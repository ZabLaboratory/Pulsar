#include "group-composition-world-source.hpp"
#include <graphics/matrix4.h>
#include <obs-module.h>
#include <new>

namespace {
constexpr const char *source_id = "pulsar_composition_world_affine";
struct WorldSource {
  obs_source_t *owner = nullptr;
  obs_source_t *base = nullptr;
  matrix4 affine{};
  uint32_t width = 0, height = 0;
};

const char *name(void *) { return "Pulsar live affine contribution"; }
void *create(obs_data_t *, obs_source_t *owner)
{
  auto *state = new (std::nothrow) WorldSource;
  if (state) {
    state->owner = owner;
    matrix4_identity(&state->affine);
  }
  return state;
}
void destroy(void *data)
{
  auto *state = static_cast<WorldSource *>(data);
  if (state->base)
    obs_source_release(state->base);
  delete state;
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
