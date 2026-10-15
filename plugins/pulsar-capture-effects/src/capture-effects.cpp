#include "effect-source.hpp"
#include "group-composition.hpp"
#include "obs-websocket-api.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <graphics/vec2.h>
#include <graphics/vec4.h>
#include <mutex>
#include <obs-module.h>
#include <string>
#include <unordered_map>
#include <vector>

OBS_DECLARE_MODULE()
OBS_MODULE_USE_DEFAULT_LOCALE("pulsar-capture-effects", "en-US")
MODULE_EXPORT const char *obs_module_description(void) {
  return "Scene-owned GPU capture effects";
}

namespace {
struct Stage {
  int kind = 0, noise_type = 0;
  float clip = 1;
  float radius = 0, spread = 0, density = 0, refraction = 0, depth = 0,
        angle = 0, intensity = 0, dispersion = 0, splay = 0;
  vec2 offset{}, grain{4, 4};
  vec4 tint{0, 0, 0, 1}, secondary{1, 1, 1, 1};
};
struct Effect {
  obs_source_t *context = nullptr;
  gs_effect_t *shader = nullptr;
  gs_texrender_t *input = nullptr, *passes[2]{}, *background = nullptr;
  obs_scene_t *background_scene = nullptr;
  std::vector<obs_sceneitem_t *> background_items;
  std::vector<Stage> stages;
  std::string parent;
  std::mutex mutex;
  uint32_t width = 1920, height = 1080;
  std::atomic<uint32_t> output_width{1}, output_height{1};
  float padding = 0, corner = 0, opacity = 1, rotation = 0;
  vec2 size{}, position{};
  bool needs_background = false;
  int blend_mode = 0;
  vec4 crop_percent{};
  vec2 content_position{0.5f, 0.5f};
  vec2 content_origin{}, content_size{};
  uint32_t source_width = 0, source_height = 0;
  bool clip_effects = false;
  bool skip_content_fit = false;
  int ancestor_count = 0;
  std::array<vec4, 32> ancestor_x{}, ancestor_y{}, ancestor_size{};
  float source_zoom = 1;
};
std::mutex registry_mutex;
std::unordered_map<obs_source_t *, Effect *> registry;
float number(obs_data_t *s, const char *key, float lo, float hi,
             float fallback = 0) {
  if (!obs_data_has_user_value(s, key))
    return fallback;
  const double v = obs_data_get_double(s, key);
  return std::isfinite(v) ? float(std::clamp(v, double(lo), double(hi)))
                          : fallback;
}
vec4 color(obs_data_t *s, const char *key, vec4 fallback) {
  obs_data_t *c = obs_data_get_obj(s, key);
  if (!c)
    return fallback;
  vec4 value{number(c, "r", 0, 1), number(c, "g", 0, 1), number(c, "b", 0, 1),
             number(c, "a", 0, 1, 1)};
  obs_data_release(c);
  return value;
}
void update(void *data, obs_data_t *settings) {
  auto *f = static_cast<Effect *>(data);
  std::lock_guard<std::mutex> lock(f->mutex);
  f->size = {number(settings, "width", 1, 16384, 1),
             number(settings, "height", 1, 16384, 1)};
  f->position = {number(settings, "x", -32768, 32768),
                 number(settings, "y", -32768, 32768)};
  f->corner = number(settings, "corner_radius", 0, 8192);
  f->opacity = number(settings, "opacity", 0, 1, 1);
  f->padding = number(settings, "padding", 0, 512);
  f->rotation = number(settings, "rotation", -360, 360);
  f->crop_percent = {number(settings, "crop_left", 0, 99), number(settings, "crop_top", 0, 99),
                     number(settings, "crop_right", 0, 99), number(settings, "crop_bottom", 0, 99)};
  if (f->crop_percent.x + f->crop_percent.z > 99) {
    const float factor = 99 / (f->crop_percent.x + f->crop_percent.z);
    f->crop_percent.x *= factor; f->crop_percent.z *= factor;
  }
  if (f->crop_percent.y + f->crop_percent.w > 99) {
    const float factor = 99 / (f->crop_percent.y + f->crop_percent.w);
    f->crop_percent.y *= factor; f->crop_percent.w *= factor;
  }
  f->content_position = {number(settings, "content_x", 0, 1, 0.5f), number(settings, "content_y", 0, 1, 0.5f)};
  f->content_origin = {number(settings, "content_box_x", -32768, 32768), number(settings, "content_box_y", -32768, 32768)};
  f->content_size = {number(settings, "content_box_width", 1, 16384, f->size.x), number(settings, "content_box_height", 1, 16384, f->size.y)};
  f->clip_effects = obs_data_get_bool(settings, "clip_effects");
  f->skip_content_fit = obs_data_get_bool(settings, "skip_content_fit");
  auto *clips = obs_data_get_array(settings, "ancestor_clips");
  f->ancestor_count = clips ? int(std::min(size_t(32), obs_data_array_count(clips))) : 0;
  for (int i = 0; i < f->ancestor_count; ++i) {
    auto *clip = obs_data_array_item(clips, size_t(i));
    f->ancestor_x[i] = {number(clip, "xx", -32768, 32768, 1), number(clip, "xy", -32768, 32768), number(clip, "tx", -32768, 32768), 0};
    f->ancestor_y[i] = {number(clip, "yx", -32768, 32768), number(clip, "yy", -32768, 32768, 1), number(clip, "ty", -32768, 32768), 0};
    f->ancestor_size[i] = {number(clip, "width", 0, 16384), number(clip, "height", 0, 16384), number(clip, "radius", 0, 8192), 0};
    obs_data_release(clip);
  }
  obs_data_array_release(clips);
  f->source_zoom = number(settings, "source_zoom", 0.1f, 10, 1);
  f->source_width = f->source_height = 0;
  f->output_width = uint32_t(f->size.x) + uint32_t(f->padding) * 2;
  f->output_height = uint32_t(f->size.y) + uint32_t(f->padding) * 2;
  f->parent = obs_data_get_string(settings, "background_scene");
  f->stages.clear();
  f->needs_background = false;
  f->blend_mode = int(number(settings, "blend_mode", 0, 16));
  auto *array = obs_data_get_array(settings, "effects");
  for (size_t i = 0;
       array && i < std::min(size_t(32), obs_data_array_count(array)); ++i) {
    auto *s = obs_data_array_item(array, i);
    Stage e;
    e.kind = int(number(s, "kind", 1, 7));
    e.radius = number(s, "radius", 0, 100);
    e.spread = number(s, "spread", -1000, 1000);
    e.offset = {number(s, "x", -4096, 4096), number(s, "y", -4096, 4096)};
    e.tint = color(s, "color", e.tint);
    e.secondary = color(s, "secondary", e.secondary);
    e.density = number(s, "density", 0, 1, 0.5f);
    e.noise_type = int(number(s, "noise_type", 0, 2));
    e.clip = number(s, "clip_to_shape", 0, 1, 1);
    e.grain = {number(s, "grain_x", 0.25f, 2000, 4),
               number(s, "grain_y", 0.25f, 2000, 4)};
    e.refraction = number(s, "refraction", 0, 1);
    e.depth = number(s, "depth", 0, 1);
    e.angle = number(s, "angle", 0, 6.28319f);
    e.intensity = number(s, "intensity", 0, 1);
    e.dispersion = number(s, "dispersion", 0, 1);
    e.splay = number(s, "splay", 0, 1);
    f->needs_background |= e.kind == 4 || e.kind == 7;
    f->stages.push_back(e);
    obs_data_release(s);
  }
  obs_data_array_release(array);
  if (f->blend_mode) {
    Stage blend;
    blend.kind = 8;
    f->stages.push_back(blend);
    f->needs_background = true;
  }
}
const char *name(void *) { return "Pulsar Capture Effects"; }
void destroy(void *data) {
  auto *f = static_cast<Effect *>(data);
  if (!f)
    return;
  {
    std::lock_guard<std::mutex> lock(registry_mutex);
    registry.erase(f->context);
  }
  if (f->background_scene)
    obs_scene_release(f->background_scene);
  obs_enter_graphics();
  gs_effect_destroy(f->shader);
  gs_texrender_destroy(f->input);
  gs_texrender_destroy(f->background);
  for (auto *pass : f->passes)
    gs_texrender_destroy(pass);
  obs_leave_graphics();
  delete f;
}
void *create(obs_data_t *settings, obs_source_t *source) {
  auto *f = new Effect;
  f->context = source;
  char *error = nullptr;
  obs_enter_graphics();
  f->shader = gs_effect_create(capture_effect_source,
                               "pulsar-capture-effects.effect", &error);
  f->input = gs_texrender_create(GS_RGBA, GS_ZS_NONE);
  f->background = gs_texrender_create(GS_RGBA, GS_ZS_NONE);
  for (auto &pass : f->passes)
    pass = gs_texrender_create(GS_RGBA, GS_ZS_NONE);
  obs_leave_graphics();
  if (!f->shader) {
    blog(LOG_ERROR, "[capture-effects] shader: %s", error ? error : "unknown");
    bfree(error);
    destroy(f);
    return nullptr;
  }
  bfree(error);
  f->background_scene = obs_scene_create_private("Pulsar capture backdrop");
  update(f, settings);
  {
    std::lock_guard<std::mutex> lock(registry_mutex);
    registry[source] = f;
  }
  return f;
}
struct BackdropBuild {
  Effect *filter;
  size_t index = 0;
};
bool mirror_item(obs_scene_t *, obs_sceneitem_t *item, void *data) {
  auto &b = *static_cast<BackdropBuild *>(data);
  auto *f = b.filter;
  auto *source = obs_sceneitem_get_source(item);
  if (source == obs_filter_get_parent(f->context))
    return false;
  if (!obs_sceneitem_visible(item))
    return true;
  obs_sceneitem_t *clone = nullptr;
  if (b.index < f->background_items.size())
    clone = f->background_items[b.index];
  if (clone && obs_sceneitem_get_source(clone) != source) {
    obs_sceneitem_remove(clone);
    clone = nullptr;
  }
  if (!clone) {
    clone = obs_scene_add(f->background_scene, source);
    if (b.index < f->background_items.size())
      f->background_items[b.index] = clone;
    else
      f->background_items.push_back(clone);
  }
  if (!clone)
    return true;
  obs_sceneitem_force_update_transform(item);
  obs_transform_info info{};
  obs_sceneitem_get_info2(item, &info);
  obs_sceneitem_set_info2(clone, &info);
  obs_sceneitem_crop crop{};
  obs_sceneitem_get_crop(item, &crop);
  obs_sceneitem_set_crop(clone, &crop);
  obs_sceneitem_set_blending_mode(clone, obs_sceneitem_get_blending_mode(item));
  obs_sceneitem_set_blending_method(clone,
                                    obs_sceneitem_get_blending_method(item));
  obs_sceneitem_force_update_transform(clone);
  ++b.index;
  return true;
}
void uniform(Effect *f, const char *key, float v) {
  gs_effect_set_float(gs_effect_get_param_by_name(f->shader, key), v);
}
void vector2(Effect *f, const char *key, vec2 v) {
  gs_effect_set_vec2(gs_effect_get_param_by_name(f->shader, key), &v);
}
void vector4(Effect *f, const char *key, vec4 v) {
  gs_effect_set_vec4(gs_effect_get_param_by_name(f->shader, key), &v);
}
bool begin(gs_texrender_t *target, uint32_t w, uint32_t h) {
  gs_texrender_reset(target);
  if (!gs_texrender_begin(target, w, h))
    return false;
  vec4 clear{};
  gs_clear(GS_CLEAR_COLOR, &clear, 0, 0);
  gs_ortho(0, float(w), 0, float(h), -100, 100);
  return true;
}
void render(void *data, gs_effect_t *) {
  auto *f = static_cast<Effect *>(data);
  std::lock_guard<std::mutex> lock(f->mutex);
  auto *target = obs_filter_get_target(f->context);
  f->width = obs_source_get_base_width(obs_filter_get_parent(f->context));
  f->height = obs_source_get_base_height(obs_filter_get_parent(f->context));
  if (!target || !f->width || !f->height) {
    obs_source_skip_video_filter(f->context);
    return;
  }
  gs_blend_state_push();
  gs_blend_function(GS_BLEND_ONE, GS_BLEND_INVSRCALPHA);
  if (f->needs_background) {
    auto *parent = obs_get_source_by_name(f->parent.c_str());
    auto *scene = parent ? obs_scene_from_source(parent) : nullptr;
    BackdropBuild build{f};
    if (scene)
      obs_scene_enum_items(scene, mirror_item, &build);
    while (f->background_items.size() > build.index) {
      obs_sceneitem_remove(f->background_items.back());
      f->background_items.pop_back();
    }
    if (begin(f->background, f->width, f->height)) {
      obs_source_video_render(obs_scene_get_source(f->background_scene));
      gs_texrender_end(f->background);
    }
    obs_source_release(parent);
  }
  if (!begin(f->input, f->width, f->height)) {
    gs_blend_state_pop();
    return;
  }
  if (!obs_source_process_filter_begin(f->context, GS_RGBA,
                                       OBS_NO_DIRECT_RENDERING)) {
    gs_texrender_end(f->input);
    gs_blend_state_pop();
    return;
  }
  obs_source_process_filter_end(
      f->context, obs_get_base_effect(OBS_EFFECT_DEFAULT), f->width, f->height);
  gs_texrender_end(f->input);
  auto *texture = gs_texrender_get_texture(f->input);
  const uint32_t w = uint32_t(f->size.x) + uint32_t(f->padding) * 2,
                 h = uint32_t(f->size.y) + uint32_t(f->padding) * 2;
  std::vector<Stage> passes{Stage{}};
  for (auto e : f->stages) {
    if (e.kind == 3) {
      e.kind = 31;
      passes.push_back(e);
      e.kind = 32;
    }
    passes.push_back(e);
  }
  if (f->clip_effects) { Stage final_clip; final_clip.kind = 9; passes.push_back(final_clip); }
  int index = 0;
  for (auto e : passes) {
    auto *out = f->passes[index % 2];
    if (!begin(out, w, h))
      break;
    vector2(f, "dimensions", {float(w), float(h)});
    vector2(f, "capture_size", f->size);
    vector2(f, "capture_position", f->position);
    vector2(f, "canvas_size", {float(f->width), float(f->height)});
    uniform(f, "padding", f->padding);
    uniform(f, "corner_radius", f->corner);
    uniform(f, "capture_opacity", f->opacity);
    uniform(f, "capture_rotation", f->rotation * 0.01745329252f);
    // Clip only final paint, in its existing pass. Ancestor depth never creates
    // extra textures or GPU passes, and shadows remain unclipped between stages.
    gs_effect_set_int(gs_effect_get_param_by_name(f->shader, "ancestor_count"), index + 1 == int(passes.size()) ? f->ancestor_count : 0);
    gs_effect_set_val(gs_effect_get_param_by_name(f->shader, "ancestor_x"), f->ancestor_x.data(), sizeof(f->ancestor_x));
    gs_effect_set_val(gs_effect_get_param_by_name(f->shader, "ancestor_y"), f->ancestor_y.data(), sizeof(f->ancestor_y));
    gs_effect_set_val(gs_effect_get_param_by_name(f->shader, "ancestor_size"), f->ancestor_size.data(), sizeof(f->ancestor_size));
    if (f->needs_background)
      gs_effect_set_texture(gs_effect_get_param_by_name(f->shader, "backdrop"),
                            gs_texrender_get_texture(f->background));
    gs_effect_set_texture(gs_effect_get_param_by_name(f->shader, "image"),
                          texture);
    vector2(f, "source_dimensions",
            index == 0 ? vec2{float(f->width), float(f->height)}
                       : vec2{float(w), float(h)});
    gs_effect_set_int(gs_effect_get_param_by_name(f->shader, "kind"), e.kind);
    gs_effect_set_int(gs_effect_get_param_by_name(f->shader, "blend_mode"), f->blend_mode);
    uniform(f, "radius", e.radius);
    uniform(f, "spread", e.spread);
    vector2(f, "offset", e.offset);
    vector4(f, "tint", e.tint);
    vector4(f, "secondary", e.secondary);
    uniform(f, "density", e.density);
    vector2(f, "grain_size", e.grain);
    gs_effect_set_int(gs_effect_get_param_by_name(f->shader, "noise_type"),
                      e.noise_type);
    uniform(f, "clip_to_shape", e.clip);
    uniform(f, "refraction", e.refraction);
    uniform(f, "depth", e.depth);
    uniform(f, "angle", e.angle);
    uniform(f, "intensity", e.intensity);
    uniform(f, "dispersion", e.dispersion);
    uniform(f, "splay", e.splay);
    while (gs_effect_loop(f->shader, "Draw"))
      gs_draw_sprite(texture, 0, w, h);
    gs_texrender_end(out);
    texture = gs_texrender_get_texture(out);
    ++index;
  }
  auto *effect = obs_get_base_effect(OBS_EFFECT_DEFAULT);
  gs_effect_set_texture(gs_effect_get_param_by_name(effect, "image"), texture);
  while (gs_effect_loop(effect, "Draw"))
    gs_draw_sprite(texture, 0, w, h);
  gs_blend_state_pop();
}
uint32_t width(void *data) {
  return static_cast<Effect *>(data)->output_width.load();
}
uint32_t height(void *data) {
  return static_cast<Effect *>(data)->output_height.load();
}

// A websocket settings write to a video filter is deferred by libobs. Admit the
// filter and both scene-item transforms on the graphics thread before a frame.
// Existing public atomic scene and vendor APIs are reused; no alternate
// renderer.
// Recompute against the actual input dimensions, including first-frame admission
// and resolution changes. The authored crop remains independent of the box.
void fit_content(Effect *effect, obs_sceneitem_t *inner) {
  auto *input = obs_sceneitem_get_source(inner);
  const auto sw = obs_source_get_width(input), sh = obs_source_get_height(input);
  if (!sw || !sh || (sw == effect->source_width && sh == effect->source_height)) return;
  effect->source_width = sw; effect->source_height = sh;
  obs_sceneitem_crop crop{};
  crop.left = int(std::floor(sw * effect->crop_percent.x / 100));
  crop.top = int(std::floor(sh * effect->crop_percent.y / 100));
  crop.right = std::min(int(sw) - crop.left - 1, int(std::floor(sw * effect->crop_percent.z / 100)));
  crop.bottom = std::min(int(sh) - crop.top - 1, int(std::floor(sh * effect->crop_percent.w / 100)));
  const float cw = float(sw - crop.left - crop.right), ch = float(sh - crop.top - crop.bottom);
  obs_transform_info info{};
  obs_sceneitem_get_info2(inner, &info);
  info.bounds = {effect->content_size.x * effect->source_zoom, effect->content_size.y * effect->source_zoom};
  const float scale = info.bounds_type == OBS_BOUNDS_SCALE_OUTER
    ? std::max(info.bounds.x / cw, info.bounds.y / ch)
    : std::min(info.bounds.x / cw, info.bounds.y / ch);
  const float paint_w = info.bounds_type == OBS_BOUNDS_STRETCH ? info.bounds.x : cw * scale;
  const float paint_h = info.bounds_type == OBS_BOUNDS_STRETCH ? info.bounds.y : ch * scale;
  info.pos = {effect->content_origin.x + (effect->content_size.x - paint_w) * effect->content_position.x - (info.bounds.x - paint_w) / 2,
              effect->content_origin.y + (effect->content_size.y - paint_h) * effect->content_position.y - (info.bounds.y - paint_h) / 2};
  info.rot = 0; info.alignment = OBS_ALIGN_TOP | OBS_ALIGN_LEFT;
  info.crop_to_bounds = false; // The filter clips the content before effects.
  obs_sceneitem_defer_update_begin(inner);
  obs_sceneitem_set_crop(inner, &crop);
  obs_sceneitem_set_info2(inner, &info);
  obs_sceneitem_defer_update_end(inner);
  obs_sceneitem_force_update_transform(inner);
}
void tick(void *data, float) {
  auto *effect = static_cast<Effect *>(data);
  if (effect->skip_content_fit)
    return;
  std::lock_guard<std::mutex> lock(effect->mutex);
  auto *parent = obs_filter_get_parent(effect->context);
  auto *scene = parent ? obs_scene_from_source(parent) : nullptr;
  if (scene) obs_scene_enum_items(scene, [](obs_scene_t *, obs_sceneitem_t *item, void *state) {
    fit_content(static_cast<Effect *>(state), item); return false;
  }, effect);
}
struct Commit {
  obs_data_t *request, *response;
};
void admit(void *data, obs_scene_t *scene) {
  auto &c = *static_cast<Commit *>(data);
  auto *r = c.request;
  const char *layer_name = obs_data_get_string(r, "layer");
  const char *input_name = obs_data_get_string(r, "inputName");
  auto *outer =
      obs_scene_find_sceneitem_by_id(scene, obs_data_get_int(r, "itemId"));
  if (!outer || std::string(obs_source_get_name(
                    obs_sceneitem_get_source(outer))) != layer_name) {
    obs_data_set_string(c.response, "error",
                        "Capture appearance item replaced");
    return;
  }
  auto *layer = obs_sceneitem_get_source(outer);
  auto *inner_scene = obs_scene_from_source(layer);
  auto *inner = inner_scene ? obs_scene_find_sceneitem_by_id(
                                  inner_scene, obs_data_get_int(r, "innerId"))
                            : nullptr;
  if (!inner || std::string(obs_source_get_name(
                    obs_sceneitem_get_source(inner))) != input_name) {
    obs_data_set_string(c.response, "error",
                        "Capture appearance source replaced");
    return;
  }
  obs_data_t *settings = obs_data_get_obj(r, "settings");
  if (!settings) {
    obs_data_set_string(c.response, "error",
                        "Capture appearance settings missing");
    return;
  }
  auto *filter = obs_source_get_filter_by_name(layer, "prism/capture-effects");
  if (filter &&
      std::string(obs_source_get_id(filter)) != "pulsar_capture_effects") {
    obs_source_release(filter);
    obs_data_release(settings);
    obs_data_set_string(c.response, "error",
                        "Capture effects filter name collision");
    return;
  }
  // Only the two retired internal filters can be replaced. Never remove an
  // operator filter or a colliding registration, even from an authored layer.
  const char *names[] = {"prism/color_filter", "prism/capture-rounded"};
  const char *ids[] = {"color_filter_v2", "mask_filter_v2"};
  obs_source_t *retired[2]{};
  for (int i = 0; i < 2; ++i) {
    retired[i] = obs_source_get_filter_by_name(layer, names[i]);
    if (retired[i] && std::string(obs_source_get_id(retired[i])) != ids[i]) {
      for (auto *f : retired)
        obs_source_release(f);
      obs_source_release(filter);
      obs_data_release(settings);
      obs_data_set_string(c.response, "error",
                          "Capture appearance filter name collision");
      return;
    }
  }
  if (!filter) {
    filter = obs_source_create("pulsar_capture_effects",
                               "prism/capture-effects", settings, nullptr);
    if (filter)
      obs_source_filter_add(layer, filter);
  }
  Effect *effect = nullptr;
  {
    std::lock_guard<std::mutex> lock(registry_mutex);
    auto it = registry.find(filter);
    if (it != registry.end())
      effect = it->second;
  }
  if (!effect) {
    for (auto *f : retired)
      obs_source_release(f);
    obs_source_release(filter);
    obs_data_release(settings);
    obs_data_set_string(c.response, "error",
                        "Capture effects filter unavailable");
    return;
  }
  obs_source_update(filter, settings);
  update(effect,
         settings); // synchronous admission; next tick repeats the same state
  obs_source_set_enabled(filter, true);
  for (auto *f : retired) {
    if (f)
      obs_source_filter_remove(layer, f);
    obs_source_release(f);
  }
  obs_transform_info info{};
  obs_sceneitem_get_info2(inner, &info);
  info.pos = {0, 0};
  info.bounds = effect->size;
  info.bounds_alignment = 0;
  const std::string fit = obs_data_get_string(r, "boundsType");
  info.bounds_type = fit == "OBS_BOUNDS_STRETCH"       ? OBS_BOUNDS_STRETCH
                     : fit == "OBS_BOUNDS_SCALE_OUTER" ? OBS_BOUNDS_SCALE_OUTER
                                                       : OBS_BOUNDS_SCALE_INNER;
  obs_sceneitem_set_info2(inner, &info);
  fit_content(effect, inner);
  obs_sceneitem_get_info2(outer, &info);
  info.pos = effect->rotation != 0
                 ? vec2{effect->position.x + effect->size.x / 2,
                        effect->position.y + effect->size.y / 2}
                 : vec2{effect->position.x - effect->padding,
                        effect->position.y - effect->padding};
  info.rot = effect->rotation;
  info.scale = {1, 1};
  info.alignment = effect->rotation != 0 ? 0 : OBS_ALIGN_TOP | OBS_ALIGN_LEFT;
  info.bounds_type = OBS_BOUNDS_NONE;
  obs_sceneitem_crop crop{};
  obs_sceneitem_defer_update_begin(outer);
  obs_sceneitem_set_crop(outer, &crop);
  obs_sceneitem_set_info2(outer, &info);
  obs_sceneitem_defer_update_end(outer);
  obs_sceneitem_force_update_transform(outer);
  const std::string mode = obs_data_get_string(r, "blendMode");
  const auto blend = mode == "OBS_BLEND_MULTIPLY"  ? OBS_BLEND_MULTIPLY
                     : mode == "OBS_BLEND_SCREEN"  ? OBS_BLEND_SCREEN
                     : mode == "OBS_BLEND_DARKEN"  ? OBS_BLEND_DARKEN
                     : mode == "OBS_BLEND_LIGHTEN" ? OBS_BLEND_LIGHTEN
                                                   : OBS_BLEND_NORMAL;
  obs_sceneitem_set_blending_mode(outer, blend);
  // Our filter emits an encoded-channel, premultiplied contribution, like
  // the Solar object compositor. Gamma decoding it again corrupts opacity.
  obs_sceneitem_set_blending_method(outer, OBS_BLEND_METHOD_SRGB_OFF);
  auto *direct = obs_scene_find_source(scene, input_name);
  if (direct && direct != outer) {
    obs_sceneitem_set_order_position(outer,
                                     obs_sceneitem_get_order_position(direct));
    obs_sceneitem_remove(direct);
  }
  obs_sceneitem_set_visible(outer, obs_data_get_bool(r, "visible"));
  obs_data_set_bool(c.response, "ok", true);
  obs_source_release(filter);
  obs_data_release(settings);
}
void commit_graphics(void *data) {
  auto &c = *static_cast<Commit *>(data);
  const std::string layer = obs_data_get_string(c.request, "layer");
  const std::string input = obs_data_get_string(c.request, "inputName");
  if (input.rfind("ZabCapture:", 0) != 0 ||
      layer.rfind(input + ":appearance:", 0) != 0) {
    obs_data_set_string(c.response, "error",
                        "Capture appearance target invalid");
    return;
  }
  auto *source =
      obs_get_source_by_name(obs_data_get_string(c.request, "sceneName"));
  auto *scene = source ? obs_scene_from_source(source) : nullptr;
  if (scene)
    obs_scene_atomic_update(scene, admit, &c);
  else
    obs_data_set_string(c.response, "error",
                        "Capture appearance scene missing");
  obs_source_release(source);
}
void commit_request(obs_data_t *request, obs_data_t *response, void *) {
  Commit c{request, response};
  obs_queue_task(OBS_TASK_GRAPHICS, commit_graphics, &c, true);
}
void blend_graphics(void *data) {
  auto &c = *static_cast<Commit *>(data);
  const std::string scene_name = obs_data_get_string(c.request, "sceneName");
  const std::string layer_name = obs_data_get_string(c.request, "layer");
  if (layer_name.rfind("ZabSolarBlend:" + scene_name + ":", 0) != 0) {
    obs_data_set_string(c.response, "error", "Object blend target invalid");
    return;
  }
  auto *source = obs_get_source_by_name(scene_name.c_str());
  auto *scene = source ? obs_scene_from_source(source) : nullptr;
  auto *item = scene ? obs_scene_find_source(scene, layer_name.c_str()) : nullptr;
  if (item) {
    // CSS blending uses encoded channels. Do not gamma-decode the already
    // premultiplied source contribution when adding it to the existing scene.
    obs_sceneitem_set_blending_method(item, OBS_BLEND_METHOD_SRGB_OFF);
    obs_sceneitem_set_blending_mode(item, OBS_BLEND_NORMAL);
    obs_data_set_bool(c.response, "ok", true);
  } else obs_data_set_string(c.response, "error", "Object blend item missing");
  obs_source_release(source);
}
void blend_request(obs_data_t *request, obs_data_t *response, void *) {
  Commit c{request, response};
  obs_queue_task(OBS_TASK_GRAPHICS, blend_graphics, &c, true);
}
} // namespace
bool obs_module_load(void) {
  if (!group_composition_load())
    return false;
  blog(LOG_INFO, "PULSAR_GROUP_COMPOSITION event=module_load");
  obs_source_info info{};
  info.id = "pulsar_capture_effects";
  info.type = OBS_SOURCE_TYPE_FILTER;
  info.output_flags = OBS_SOURCE_VIDEO;
  info.get_name = name;
  info.create = create;
  info.destroy = destroy;
  info.update = update;
  info.video_render = render;
  info.video_tick = tick;
  info.get_width = width;
  info.get_height = height;
  obs_register_source(&info);
  info.id = "pulsar_object_blend";
  obs_register_source(&info);
  return true;
}

void obs_module_post_load(void) {
  auto vendor = obs_websocket_register_vendor("pulsar-capture-effects");
  if (vendor)
    obs_websocket_vendor_register_request(vendor, "CommitAppearance",
                                          commit_request, nullptr);
  if (vendor)
    obs_websocket_vendor_register_request(vendor, "CommitObjectBlend", blend_request, nullptr);
  if (vendor)
    obs_websocket_vendor_register_request(vendor, "CommitGroupComposition",
                                          group_composition_request, nullptr);
  if (vendor)
    obs_websocket_vendor_register_request(vendor, "ResetGroupComposition",
                                          reset_group_composition_request, nullptr);
}

void obs_module_unload(void) { group_composition_unload(); }
