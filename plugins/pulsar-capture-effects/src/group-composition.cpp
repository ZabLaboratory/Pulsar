#include "reset-trace.hpp"
#include "group-composition.hpp"
#include "group-composition-plan.hpp"
#include "group-composition-source-validation.hpp"
#include "group-composition-world-source.hpp"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <limits>
#include <obs-module.h>
#include <obs.h>
#include <callback/calldata.h>
#include <callback/signal.h>
#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace {
using namespace group_composition;
using group_composition_detail::SourceRef;
using group_composition_detail::validate_capture_source;
struct OriginalItem {
  int64_t id = 0;
  obs_sceneitem_t *item = nullptr;
  bool visible = false;
};
struct ActiveComposition {
  int protocol_version = 1;
  CoordinateSpace coordinate_space = CoordinateSpace::OwnerLocal;
  obs_sceneitem_t *root = nullptr;
  std::string scene_uuid;
  std::string runtime_instance_id;
  uint64_t generation = 0;
  std::string plan_hash;
  std::vector<int64_t> replaced_scene_item_ids;
  std::vector<OriginalItem> originals;
  std::vector<SourceRef> dependency_sources;
};
struct RemovedSourceEvent {
  SourceRef source;
  std::string uuid;
};
struct ResolvedCapture {
  obs_sceneitem_t *item = nullptr;
  obs_source_t *source = nullptr;
  uint32_t width = 0, height = 0;
  bool visible = false;
};
struct Transform {
  vec2 scale{1, 1};
  float rotation = 0;
  vec2 position{};
  bool empty = false;
};

std::unordered_map<std::string, ActiveComposition> active_compositions;
struct GenerationFence {
  uint64_t generation = 0;
  SourceRef scene_source;

  GenerationFence() = default;
  explicit GenerationFence(SourceRef source)
      : scene_source(std::move(source)) {}
  GenerationFence(const GenerationFence &) = delete;
  GenerationFence &operator=(const GenerationFence &) = delete;
  GenerationFence(GenerationFence &&) noexcept = default;
  GenerationFence &operator=(GenerationFence &&) noexcept = default;
};
std::unordered_map<std::string, GenerationFence> generation_fences;
signal_handler_t *global_signal_handler = nullptr;
std::mutex source_remove_mutex;
std::condition_variable source_remove_idle;
std::unordered_set<RemovedSourceEvent *> pending_source_removals;
bool accepting_source_removals = false;
std::size_t source_removals_in_flight = 0;
uint64_t source_serial = 0;

#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
std::atomic<uint64_t> reset_trace_sequence{0};
uint64_t next_reset_trace_id()
{
  return reset_trace_sequence.fetch_add(1, std::memory_order_relaxed) + 1;
}
void reset_trace(uint64_t trace_id, const char *phase)
{
  blog(LOG_INFO,
       "PULSAR_GROUP_COMPOSITION_RESET trace=%llu tid=%llu phase=%s",
       static_cast<unsigned long long>(trace_id),
       static_cast<unsigned long long>(pulsar_reset_trace::current_thread_id()),
       phase);
}
#else
inline uint64_t next_reset_trace_id() { return 0; }
inline void reset_trace(uint64_t, const char *) {}
#endif

static_assert(std::is_nothrow_swappable_v<ActiveComposition>);

bool to_transform(const Affine &a, Transform &out, obs_data_t *response)
{
  const double det = a.m00 * a.m11 - a.m01 * a.m10;
  if (std::abs(det) < 1e-12) {
    out.empty = true;
    return true;
  }
  if (!validate_group_affine(a, response))
    return false;
  const double sx = std::hypot(a.m00, a.m10);
  const double sy = det / sx;
  out.scale = {float(sx), float(sy)};
  out.rotation = float(std::atan2(a.m10, a.m00) * 180.0 / 3.14159265358979323846);
  out.position = {float(a.tx), float(a.ty)};
  if (!std::isfinite(out.scale.x) || !std::isfinite(out.scale.y) ||
      !std::isfinite(out.rotation) || !std::isfinite(out.position.x) ||
      !std::isfinite(out.position.y)) {
    set_error(response, "invalid-affine", "transform exceeds native float range");
    return false;
  }
  return true;
}

void apply_transform(obs_sceneitem_t *item, const Transform &transform)
{
  obs_transform_info info{};
  info.pos = transform.position;
  info.rot = transform.rotation;
  info.scale = transform.scale;
  info.alignment = OBS_ALIGN_TOP | OBS_ALIGN_LEFT;
  info.bounds_type = OBS_BOUNDS_NONE;
  info.bounds_alignment = OBS_ALIGN_TOP | OBS_ALIGN_LEFT;
  info.bounds = {0, 0};
  info.crop_to_bounds = false;
  obs_sceneitem_set_info2(item, &info);
  obs_sceneitem_force_update_transform(item);
  if (transform.empty)
    obs_sceneitem_set_visible(item, false);
}

std::string unique_name(const char *kind, const std::string &id = {})
{
  std::string name = "Pulsar composition ";
  name += kind;
  name += " ";
  name += std::to_string(++source_serial);
  if (!id.empty()) {
    name += " ";
    name += id.substr(0, 80);
  }
  return name;
}

bool add_bounds_anchor(obs_scene_t *scene, const Rect &bounds,
                       obs_data_t *response)
{
  obs_data_t *settings = obs_data_create();
  obs_data_set_int(settings, "width", static_cast<long long>(std::lround(bounds.width)));
  obs_data_set_int(settings, "height", static_cast<long long>(std::lround(bounds.height)));
  obs_data_set_int(settings, "color", 0);
  const std::string source_name = unique_name("bounds");
  obs_source_t *source = obs_source_create("color_source", source_name.c_str(),
                                           settings, nullptr);
  obs_data_release(settings);
  if (!source) {
    set_error(response, "anchor-create-failed", source_name.c_str());
    return false;
  }
  obs_sceneitem_t *item = obs_scene_add(scene, source);
  obs_source_release(source);
  if (!item) {
    set_error(response, "anchor-add-failed", source_name.c_str());
    return false;
  }
  // The transparent input exists only to keep OBS's native group extent equal
  // to the authored local bounds; it contributes no pixels.
  obs_sceneitem_set_visible(item, true);
  obs_sceneitem_set_blending_method(item, OBS_BLEND_METHOD_SRGB_OFF);
  return true;
}

bool add_opacity_filter(obs_sceneitem_t *group, const Rect &bounds,
                        double opacity, obs_data_t *response)
{
  if (opacity >= 1)
    return true;
  obs_source_t *group_source = obs_sceneitem_get_source(group);
  if (!group_source) {
    set_error(response, "group-source-missing");
    return false;
  }
  obs_data_t *settings = obs_data_create();
  obs_data_set_int(settings, "width", static_cast<long long>(std::lround(bounds.width)));
  obs_data_set_int(settings, "height", static_cast<long long>(std::lround(bounds.height)));
  obs_data_set_double(settings, "opacity", opacity);
  obs_data_set_int(settings, "padding", 0);
  obs_data_set_int(settings, "corner_radius", 0);
  obs_data_set_bool(settings, "skip_content_fit", true);
  obs_data_array_t *effects = obs_data_array_create();
  obs_data_set_array(settings, "effects", effects);
  obs_data_array_release(effects);
  const std::string filter_name = unique_name("opacity-filter");
  obs_source_t *filter = obs_source_create("pulsar_capture_effects",
                                           filter_name.c_str(), settings,
                                           nullptr);
  obs_data_release(settings);
  if (!filter) {
    set_error(response, "group-opacity-filter-create-failed");
    return false;
  }
  obs_source_filter_add(group_source, filter);
  obs_source_set_enabled(filter, true);
  obs_source_release(filter);
  return true;
}

bool add_composition_filter(obs_source_t *filtered_source,
                            obs_scene_t *parent_scene, const Rect &viewport,
                            double opacity, int blend_mode, const Mask &mask,
                            const ResolvedCapture *mask_source,
                            obs_data_t *response)
{
  if (mask.is_null && opacity >= 1 && blend_mode == 0)
    return true;
  if (!filtered_source || !parent_scene) {
    set_error(response, "composition-filter-parent-missing");
    return false;
  }
  const char *background_scene_name =
      obs_source_get_name(obs_scene_get_source(parent_scene));
  if (!background_scene_name || !*background_scene_name) {
    set_error(response, "composition-backdrop-scene-missing");
    return false;
  }
  if (!mask.is_null && (!mask_source || !mask_source->source)) {
    set_error(response, "composition-mask-source-missing",
              std::to_string(mask.scene_item_id).c_str());
    return false;
  }

  obs_data_t *settings = obs_data_create();
  obs_data_set_double(settings, "width", viewport.width);
  obs_data_set_double(settings, "height", viewport.height);
  obs_data_set_double(settings, "opacity", opacity);
  obs_data_set_int(settings, "padding", 0);
  obs_data_set_int(settings, "corner_radius", 0);
  obs_data_set_int(settings, "blend_mode", blend_mode);
  obs_data_set_bool(settings, "skip_content_fit", true);
  obs_data_set_string(settings, "background_scene", background_scene_name);
  auto *effects = obs_data_array_create();
  obs_data_set_array(settings, "effects", effects);
  obs_data_array_release(effects);
  if (!mask.is_null) {
    obs_data_set_string(settings, "coverage_source_name",
                        obs_source_get_name(mask_source->source));
    obs_data_set_int(settings, "coverage_scene_item_id",
                     static_cast<long long>(mask.scene_item_id));
    obs_data_set_int(settings, "coverage_crop_left", mask.crop.left);
    obs_data_set_int(settings, "coverage_crop_top", mask.crop.top);
    obs_data_set_int(settings, "coverage_crop_right", mask.crop.right);
    obs_data_set_int(settings, "coverage_crop_bottom", mask.crop.bottom);
    obs_data_set_int(settings, "coverage_rect_x",
                     static_cast<long long>(mask.rect.x));
    obs_data_set_int(settings, "coverage_rect_y",
                     static_cast<long long>(mask.rect.y));
    obs_data_set_int(settings, "coverage_rect_width",
                     static_cast<long long>(mask.rect.width));
    obs_data_set_int(settings, "coverage_rect_height",
                     static_cast<long long>(mask.rect.height));
  }
  const std::string filter_name = unique_name("composition-filter");
  obs_source_t *filter = obs_source_create("pulsar_capture_effects",
                                           filter_name.c_str(), settings,
                                           nullptr);
  obs_data_release(settings);
  if (!filter) {
    set_error(response, "composition-filter-create-failed");
    return false;
  }
  obs_source_filter_add(filtered_source, filter);
  obs_source_set_enabled(filter, true);
  obs_source_release(filter);
  return true;
}

bool apply_crop(obs_sceneitem_t *item, const Crop &crop,
                const ResolvedCapture &resolved, obs_data_t *response)
{
  if ((resolved.width && uint64_t(crop.left) + uint64_t(crop.right) >= resolved.width) ||
      (resolved.height && uint64_t(crop.top) + uint64_t(crop.bottom) >= resolved.height) ||
      ((!resolved.width || !resolved.height) &&
       (crop.left || crop.top || crop.right || crop.bottom))) {
    set_error(response, "invalid-crop", "crop removes source or dimensions are unknown");
    return false;
  }
  obs_sceneitem_crop value{crop.left, crop.top, crop.right, crop.bottom};
  obs_sceneitem_set_crop(item, &value);
  return true;
}

bool add_capture(obs_scene_t *scene, const Child &child,
                 const ResolvedCapture &resolved, obs_data_t *response, bool global_texels = false)
{
  const bool contribution_visible = child.has_visible ? child.visible : resolved.visible;
  Transform transform;
  if (!to_transform(child.affine, transform, response))
    return false;
  if (child.local_opacity >= 1) {
    obs_sceneitem_t *item = obs_scene_add(scene, resolved.source);
    if (!item) {
      set_error(response, "capture-clone-failed");
      return false;
    }
    if (!apply_crop(item, child.crop, resolved, response))
      return false;
    apply_transform(item, transform);
    obs_sceneitem_set_visible(item, contribution_visible && !transform.empty);
    obs_sceneitem_set_blending_mode(item, obs_sceneitem_get_blending_mode(resolved.item));
    obs_sceneitem_set_blending_method(item, global_texels ? OBS_BLEND_METHOD_SRGB_OFF
                                                       : obs_sceneitem_get_blending_method(resolved.item));
    obs_sceneitem_set_scale_filter(item, obs_sceneitem_get_scale_filter(resolved.item));
    return true;
  }

  const double width = resolved.width
                           ? std::max<uint32_t>(1, resolved.width - child.crop.left - child.crop.right)
                           : 1;
  const double height = resolved.height
                            ? std::max<uint32_t>(1, resolved.height - child.crop.top - child.crop.bottom)
                            : 1;
  Rect wrapper_bounds{0, 0, width, height};
  obs_sceneitem_t *wrapper = obs_scene_add_group(scene, unique_name("capture-wrapper").c_str());
  if (!wrapper) {
    set_error(response, "capture-wrapper-create-failed");
    return false;
  }
  obs_sceneitem_set_visible(wrapper, false);
  obs_scene_t *wrapper_scene = obs_sceneitem_group_get_scene(wrapper);
  if (!wrapper_scene || !add_bounds_anchor(wrapper_scene, wrapper_bounds, response))
    return false;
  obs_sceneitem_t *item = obs_scene_add(wrapper_scene, resolved.source);
  if (!item) {
    set_error(response, "capture-clone-failed");
    return false;
  }
  if (!apply_crop(item, child.crop, resolved, response))
    return false;
  Transform identity_transform;
  apply_transform(item, identity_transform);
  obs_sceneitem_set_visible(item, contribution_visible);
  obs_sceneitem_set_blending_mode(item, obs_sceneitem_get_blending_mode(resolved.item));
  obs_sceneitem_set_blending_method(item, global_texels ? OBS_BLEND_METHOD_SRGB_OFF
                                                       : obs_sceneitem_get_blending_method(resolved.item));
  obs_sceneitem_set_scale_filter(item, obs_sceneitem_get_scale_filter(resolved.item));
  obs_sceneitem_set_blending_method(wrapper, OBS_BLEND_METHOD_SRGB_OFF);
  if (!add_opacity_filter(wrapper, wrapper_bounds, child.local_opacity, response))
    return false;
  apply_transform(wrapper, transform);
  obs_sceneitem_set_visible(wrapper, contribution_visible && !transform.empty);
  return true;
}

bool add_global_capture_contribution(obs_scene_t *scene, obs_source_t *source,
                                    const Child &child,
                                    const ResolvedCapture &resolved,
                                    const Rect &viewport,
                                    const ResolvedCapture *mask_source,
                                    obs_data_t *response)
{
  const bool visible = child.has_visible ? child.visible : resolved.visible;
  const bool needs_filter = child.local_opacity < 1 ||
                            child.blend_mode_id != 0 || !child.mask.is_null;
  obs_scene_t *target_scene = scene;
  obs_sceneitem_t *outer = nullptr;
  if (needs_filter) {
    outer = obs_scene_add_group(scene, unique_name("capture-composition").c_str());
    if (!outer) {
      set_error(response, "capture-composition-wrapper-create-failed");
      return false;
    }
    obs_sceneitem_set_visible(outer, false);
    target_scene = obs_sceneitem_group_get_scene(outer);
    if (!target_scene || !add_bounds_anchor(target_scene, viewport, response))
      return false;
  }
  obs_sceneitem_t *item = obs_scene_add(target_scene, source);
  if (!item) {
    set_error(response, "capture-clone-failed");
    return false;
  }
  Transform identity_transform;
  apply_transform(item, identity_transform);
  obs_sceneitem_set_visible(item, needs_filter ? true : visible);
  // V2 blendMode is explicit and applies to this occurrence. Never inherit the
  // physical scene item's blend a second time after the isolated CSS pass.
  obs_sceneitem_set_blending_mode(item, OBS_BLEND_NORMAL);
  obs_sceneitem_set_blending_method(item, OBS_BLEND_METHOD_SRGB_OFF);
  obs_sceneitem_set_scale_filter(item, obs_sceneitem_get_scale_filter(resolved.item));
  if (!needs_filter)
    return true;

  auto *wrapper_source = obs_sceneitem_get_source(outer);
  if (!add_composition_filter(wrapper_source, scene, viewport,
                              child.local_opacity, child.blend_mode_id,
                              child.mask, mask_source, response))
    return false;
  obs_sceneitem_set_blending_method(outer, OBS_BLEND_METHOD_SRGB_OFF);
  obs_sceneitem_set_blending_mode(outer, OBS_BLEND_NORMAL);
  Transform identity_wrapper;
  apply_transform(outer, identity_wrapper);
  obs_sceneitem_set_visible(outer, visible);
  return true;
}

bool add_mask_dependencies(
    const Plan &plan, obs_scene_t *root_scene,
    const std::unordered_map<int64_t, ResolvedCapture> &captures,
    obs_data_t *response)
{
  std::unordered_set<int64_t> mask_scene_items;
  for (const auto &group : plan.groups) {
    if (!group.mask_is_null)
      mask_scene_items.insert(group.mask.scene_item_id);
    for (const auto &child : group.children)
      if (!child.mask.is_null)
        mask_scene_items.insert(child.mask.scene_item_id);
  }
  std::vector<int64_t> ordered_mask_scene_items(mask_scene_items.begin(),
                                                mask_scene_items.end());
  std::sort(ordered_mask_scene_items.begin(), ordered_mask_scene_items.end());
  for (const int64_t scene_item_id : ordered_mask_scene_items) {
    const auto it = captures.find(scene_item_id);
    if (it == captures.end() || !it->second.source) {
      set_error(response, "composition-mask-source-missing",
                std::to_string(scene_item_id).c_str());
      return false;
    }
    SourceRef dependency(create_group_dependency_source(
        unique_name("mask-dependency", std::to_string(scene_item_id)).c_str(),
        it->second.source));
    if (!dependency) {
      set_error(response, "composition-mask-dependency-create-failed",
                std::to_string(scene_item_id).c_str());
      return false;
    }
    obs_sceneitem_t *item = obs_scene_add(root_scene, dependency.get());
    if (!item) {
      set_error(response, "composition-mask-dependency-add-failed",
                std::to_string(scene_item_id).c_str());
      return false;
    }
    // This private custom-draw source emits no pixels. Keeping its item visible
    // lets the root scene's native active-source walk retain the atlas even
    // though the original authored scene item is hidden by the composition.
    obs_sceneitem_set_visible(item, true);
  }
  return true;
}

bool add_world_capture(obs_scene_t *scene, const Child &child,
                       const ResolvedCapture &resolved, const Rect &viewport,
                       const ResolvedCapture *mask_source,
                       bool version_two, obs_data_t *response)
{
  // Crop in source space before the full source-to-scene affine. A private
  // scene reuses libobs' crop/filter/scale-filter path, without mutating the
  // physical input or decomposing a shear into lossy rotation/scale fields.
  obs_scene_t *cropped = obs_scene_create_private(unique_name("crop-scene").c_str());
  if (!cropped) {
    set_error(response, "capture-crop-scene-create-failed");
    return false;
  }
  SourceRef crop_source(obs_source_get_ref(obs_scene_get_source(cropped)));
  obs_scene_release(cropped);
  if (!crop_source) {
    set_error(response, "capture-crop-scene-not-retainable");
    return false;
  }
  obs_sceneitem_t *item = obs_scene_add(cropped, resolved.source);
  if (!item || !apply_crop(item, child.crop, resolved, response)) {
    if (!item) set_error(response, "capture-crop-item-create-failed");
    return false;
  }
  Transform identity;
  apply_transform(item, identity);
  obs_sceneitem_set_visible(item, true);
  obs_sceneitem_set_blending_mode(item, OBS_BLEND_NORMAL);
  obs_sceneitem_set_blending_method(item, OBS_BLEND_METHOD_SRGB_OFF);
  obs_sceneitem_set_scale_filter(item, obs_sceneitem_get_scale_filter(resolved.item));
  SourceRef world(create_group_world_source(unique_name("world-capture").c_str(), crop_source.get(), child.affine,
                                             uint32_t(viewport.width),
                                             uint32_t(viewport.height)));
  if (!world) {
    set_error(response, "capture-world-source-create-failed");
    return false;
  }
  ResolvedCapture contribution = resolved;
  contribution.source = world.get();
  contribution.width = uint32_t(viewport.width);
  contribution.height = uint32_t(viewport.height);
  Child world_child = child;
  world_child.affine = {};
  world_child.crop = {};
  if (version_two)
    return add_global_capture_contribution(scene, world.get(), world_child,
                                           contribution, viewport, mask_source,
                                           response);
  return add_capture(scene, world_child, contribution, response, true);
}

bool build_group(Plan &plan, const Group &group, obs_scene_t *parent_scene,
                 const std::unordered_map<int64_t, ResolvedCapture> &captures,
                 obs_sceneitem_t **out, obs_data_t *response)
{
  Transform transform;
  const bool world = plan.coordinate_space == CoordinateSpace::GlobalSceneTexels;
  const Rect &bounds = world ? plan.groups[plan.by_id.at(root_id)].bounds : group.bounds;
  if (!world && !to_transform(group.affine, transform, response))
    return false;
  obs_sceneitem_t *item = obs_scene_add_group(parent_scene,
                                              unique_name("group", group.id).c_str());
  if (!item) {
    set_error(response, "group-create-failed", group.id.c_str());
    return false;
  }
  obs_sceneitem_set_visible(item, false);
  if (group.id == root_id) {
    obs_sceneitem_addref(item); // Candidate/root ownership across the swap.
    *out = item;
  }
  obs_scene_t *inner = obs_sceneitem_group_get_scene(item);
  if (!inner || !add_bounds_anchor(inner, bounds, response))
    return false;

  for (const auto &child : group.children) {
    if (child.kind == ChildKind::Capture) {
      const auto it = captures.find(child.scene_item_id);
      const auto mask_it = child.mask.is_null
                               ? captures.end()
                               : captures.find(child.mask.scene_item_id);
      if (it == captures.end() ||
          (plan.protocol_version == 2 && !child.mask.is_null &&
           mask_it == captures.end()) ||
          !(world ? add_world_capture(inner, child, it->second, bounds,
                                      mask_it == captures.end() ? nullptr : &mask_it->second,
                                      plan.protocol_version == 2, response)
                  : add_capture(inner, child, it->second, response)))
        return false;
    } else if (child.kind == ChildKind::Group) {
      const auto group_it = plan.by_id.find(child.group_id);
      if (group_it == plan.by_id.end()) {
        set_error(response, "missing-group", child.group_id.c_str());
        return false;
      }
      obs_sceneitem_t *nested = nullptr;
      if (!build_group(plan, plan.groups[group_it->second], inner, captures,
                       &nested, response))
        return false;
    } else {
      const auto it = captures.find(child.scene_item_id);
      if (it == captures.end() || !it->second.width || !it->second.height) {
        set_error(response, "solar-band-source-unready",
                  std::to_string(child.band).c_str());
        return false;
      }
      const double cropped_width =
          it->second.width - child.crop.left - child.crop.right;
      const double cropped_height =
          it->second.height - child.crop.top - child.crop.bottom;
      if (cropped_width <= 0 || cropped_height <= 0) {
        set_error(response, "invalid-solar-band-crop",
                  std::to_string(child.band).c_str());
        return false;
      }
      if (world &&
          (child.rect.width != cropped_width || child.rect.height != cropped_height ||
           std::trunc(child.rect.x) != child.rect.x ||
           std::trunc(child.rect.y) != child.rect.y ||
           child.rect.x < 0 || child.rect.y < 0 ||
           child.rect.x + child.rect.width > bounds.width ||
           child.rect.y + child.rect.height > bounds.height)) {
        set_error(response, "global-band-resampling",
                  "global texels require exact integer placement without scaling");
        return false;
      }
      Child page = child;
      page.affine = {child.rect.width / cropped_width, 0, child.rect.x,
                     0, child.rect.height / cropped_height, child.rect.y};
      page.local_opacity = 1;
      page.has_visible = true;
      page.visible = true;
      if (!add_capture(inner, page, it->second, response, world))
        return false;
    }
  }
  if (group.id == root_id && plan.protocol_version == 2 &&
      !add_mask_dependencies(plan, inner, captures, response))
    return false;
  if (plan.protocol_version == 2) {
    const auto mask_it = group.mask_is_null
                             ? captures.end()
                             : captures.find(group.mask.scene_item_id);
    if (!group.mask_is_null && mask_it == captures.end()) {
      set_error(response, "composition-mask-source-missing",
                std::to_string(group.mask.scene_item_id).c_str());
      return false;
    }
    if (!add_composition_filter(obs_sceneitem_get_source(item), parent_scene,
                                bounds, group.opacity, group.blend_mode_id,
                                group.mask,
                                mask_it == captures.end() ? nullptr : &mask_it->second,
                                response))
      return false;
  } else if (group.id != root_id &&
             !add_opacity_filter(item, bounds, group.opacity, response)) {
    return false;
  }
  obs_sceneitem_set_blending_mode(item, OBS_BLEND_NORMAL);
  obs_sceneitem_set_blending_method(item, OBS_BLEND_METHOD_SRGB_OFF);
  apply_transform(item, transform);
  if (group.id != root_id)
    obs_sceneitem_set_visible(item, group.visible && !transform.empty);
  return true;
}

void release_originals(std::vector<OriginalItem> &items)
{
  for (auto &item : items)
    if (item.item)
      obs_sceneitem_release(item.item);
  items.clear();
}

void release_state(ActiveComposition &state)
{
  if (state.root)
    obs_sceneitem_release(state.root);
  release_originals(state.originals);
  state.dependency_sources.clear();
  state.root = nullptr;
}

struct SwapVisibility {
  obs_sceneitem_t *new_root = nullptr;
  obs_sceneitem_t *old_root = nullptr;
  bool new_root_visible = true;
  std::vector<OriginalItem> *old_originals = nullptr;
  std::vector<OriginalItem> *new_originals = nullptr;
};

bool contains_item(const std::vector<OriginalItem> &items, int64_t id,
                   obs_sceneitem_t *item)
{
  return std::any_of(items.begin(), items.end(), [=](const OriginalItem &entry) {
    return entry.id == id && entry.item == item;
  });
}

void swap_visibility(void *data, obs_scene_t *scene)
{
  auto &swap = *static_cast<SwapVisibility *>(data);
  if (swap.old_originals) {
    for (const auto &original : *swap.old_originals) {
      if (contains_item(*swap.new_originals, original.id, original.item))
        continue;
      if (obs_scene_find_sceneitem_by_id(scene, original.id) == original.item)
        obs_sceneitem_set_visible(original.item, original.visible);
    }
  }
  for (const auto &original : *swap.new_originals)
    if (obs_scene_find_sceneitem_by_id(scene, original.id) == original.item)
      obs_sceneitem_set_visible(original.item, false);
  if (swap.old_root)
    obs_sceneitem_set_visible(swap.old_root, false);
  obs_sceneitem_set_visible(swap.new_root, swap.new_root_visible);
}

void remove_root(obs_sceneitem_t *root, uint64_t generation)
{
  if (!root)
    return;
  pulsar_reset_trace::event("composition-retirement", generation,
                            "sceneitem-remove-begin", nullptr, root);
  obs_sceneitem_remove(root);
  pulsar_reset_trace::event("composition-retirement", generation,
                            "sceneitem-remove-end", nullptr, root);
  pulsar_reset_trace::event("composition-retirement", generation,
                            "sceneitem-release-begin", nullptr, root);
  obs_sceneitem_release(root);
  pulsar_reset_trace::event("composition-retirement", generation,
                            "sceneitem-release-end", nullptr, root);
}

struct CandidateGuard {
  ActiveComposition state;
  ~CandidateGuard()
  {
    pulsar_reset_trace::event("composition-retirement", state.generation,
                              "candidate-guard-begin", nullptr, state.root,
                              state.dependency_sources.size());
    if (state.root)
      remove_root(state.root, state.generation);
    pulsar_reset_trace::event("composition-retirement", state.generation,
                              "original-item-release-begin", nullptr,
                              state.root, state.originals.size());
    release_originals(state.originals);
    pulsar_reset_trace::event("composition-retirement", state.generation,
                              "original-item-release-end", nullptr,
                              state.root);
    pulsar_reset_trace::event("composition-retirement", state.generation,
                              "dependency-release-begin", nullptr,
                              state.root, state.dependency_sources.size());
    state.dependency_sources.clear();
    pulsar_reset_trace::event("composition-retirement", state.generation,
                              "dependency-release-end", nullptr, state.root);
    pulsar_reset_trace::event("composition-retirement", state.generation,
                              "candidate-guard-end", nullptr, state.root);
  }
};

struct ActiveSlotReservation {
  const std::string *key = nullptr;
  bool inserted = false;
  bool committed = false;
  ~ActiveSlotReservation()
  {
    if (inserted && !committed && key)
      active_compositions.erase(*key);
  }
};

struct GenerationFenceReservation {
  const std::string *key = nullptr;
  bool inserted = false;
  bool committed = false;
  ~GenerationFenceReservation()
  {
    if (inserted && !committed && key)
      generation_fences.erase(*key);
  }
};

bool resolve_captures(Plan &plan, obs_scene_t *scene,
                      obs_source_t *target_source,
                      const ActiveComposition *previous,
                      std::vector<SourceRef> &dependencies,
                      std::unordered_map<int64_t, ResolvedCapture> &captures,
                      obs_data_t *response)
{
  std::unordered_set<obs_source_t *> dependency_set;
  std::unordered_set<int64_t> referenced_scene_items;
  for (const auto &group : plan.groups) {
    if (!group.mask_is_null)
      referenced_scene_items.insert(group.mask.scene_item_id);
    for (const auto &child : group.children) {
      if (child.kind == ChildKind::Capture || child.kind == ChildKind::SolarBand)
        referenced_scene_items.insert(child.scene_item_id);
      if (plan.protocol_version == 2 && !child.mask.is_null)
        referenced_scene_items.insert(child.mask.scene_item_id);
    }
  }
  for (const int64_t scene_item_id : referenced_scene_items) {
    if (captures.count(scene_item_id))
      continue;
    obs_sceneitem_t *item = obs_scene_find_sceneitem_by_id(scene, scene_item_id);
    if (!item || obs_sceneitem_get_scene(item) != scene ||
        obs_sceneitem_is_group(item)) {
      set_error(response, "capture-item-not-direct",
                std::to_string(scene_item_id).c_str());
      return false;
    }
    obs_source_t *source = obs_sceneitem_get_source(item);
    if (!validate_capture_source(source, target_source, dependency_set,
                                 dependencies, response)) {
      if (!obs_data_has_user_value(response, "error"))
        set_error(response, "capture-source-invalid",
                  std::to_string(scene_item_id).c_str());
      return false;
    }
    ResolvedCapture resolved;
    resolved.item = item;
    resolved.source = source;
    resolved.width = obs_source_get_width(source);
    resolved.height = obs_source_get_height(source);
    resolved.visible = obs_sceneitem_visible(item);
    if (previous) {
      auto old = std::find_if(previous->originals.begin(), previous->originals.end(),
                              [=](const OriginalItem &original) {
                                return original.id == scene_item_id &&
                                       original.item == item;
                              });
      if (old != previous->originals.end())
        resolved.visible = old->visible;
    }
    captures.emplace(scene_item_id, resolved);
  }
  for (const int64_t scene_item_id : plan.replaced_scene_item_ids) {
    if (captures.count(scene_item_id))
      continue;
    obs_sceneitem_t *item = obs_scene_find_sceneitem_by_id(scene, scene_item_id);
    if (!item || obs_sceneitem_get_scene(item) != scene ||
        obs_sceneitem_is_group(item)) {
      set_error(response, "replaced-scene-item-not-direct",
                std::to_string(scene_item_id).c_str());
      return false;
    }
    ResolvedCapture resolved;
    resolved.item = item;
    resolved.visible = obs_sceneitem_visible(item);
    if (previous) {
      auto old = std::find_if(previous->originals.begin(), previous->originals.end(),
                              [=](const OriginalItem &original) {
                                return original.id == scene_item_id &&
                                       original.item == item;
                              });
      if (old != previous->originals.end())
        resolved.visible = old->visible;
    }
    captures.emplace(scene_item_id, resolved);
  }
  for (const auto &group : plan.groups) {
    for (const auto &child : group.children) {
      if (child.kind != ChildKind::Capture && child.kind != ChildKind::SolarBand)
        continue;
      const auto &capture = captures.at(child.scene_item_id);
      if ((capture.width && uint64_t(child.crop.left) + child.crop.right >= capture.width) ||
          (capture.height && uint64_t(child.crop.top) + child.crop.bottom >= capture.height) ||
          ((!capture.width || !capture.height) &&
           (child.crop.left || child.crop.top || child.crop.right || child.crop.bottom))) {
        set_error(response, "invalid-crop", "crop removes source or dimensions are unknown");
        return false;
      }
      Transform transform;
      if (!(plan.coordinate_space == CoordinateSpace::GlobalSceneTexels
                ? validate_world_affine(child.affine, response)
                : to_transform(child.affine, transform, response)))
        return false;
    }
  }
  auto validate_mask = [&](const Mask &mask) {
    if (mask.is_null)
      return true;
    const auto it = captures.find(mask.scene_item_id);
    if (it == captures.end() || !it->second.width || !it->second.height) {
      set_error(response, "mask-source-unready",
                std::to_string(mask.scene_item_id).c_str());
      return false;
    }
    const auto &source = it->second;
    if (uint64_t(mask.crop.left) + mask.crop.right >= source.width ||
        uint64_t(mask.crop.top) + mask.crop.bottom >= source.height) {
      set_error(response, "invalid-mask-crop",
                std::to_string(mask.scene_item_id).c_str());
      return false;
    }
    const uint32_t cropped_width = source.width - mask.crop.left - mask.crop.right;
    const uint32_t cropped_height = source.height - mask.crop.top - mask.crop.bottom;
    if (mask.rect.width != cropped_width || mask.rect.height != cropped_height) {
      set_error(response, "invalid-mask-rect",
                "mask crop and global rect must have exact one-to-one dimensions");
      return false;
    }
    return true;
  };
  if (plan.protocol_version == 2) {
    for (const auto &group : plan.groups) {
      if (!group.mask_is_null && !validate_mask(group.mask))
        return false;
      for (const auto &child : group.children)
        if (!child.mask.is_null && !validate_mask(child.mask))
          return false;
    }
  }
  return true;
}

bool copy_original_visibility(const Plan &plan, obs_scene_t *scene,
                              const ActiveComposition *previous,
                              ActiveComposition &candidate,
                              const std::unordered_map<int64_t, ResolvedCapture> &captures,
                              obs_data_t *response)
{
  candidate.originals.reserve(captures.size());
  for (const auto &[id, resolved] : captures) {
    bool visible = resolved.visible;
    if (previous) {
      auto old = std::find_if(previous->originals.begin(), previous->originals.end(),
                              [=](const OriginalItem &item) {
                                return item.id == id && item.item == resolved.item;
                              });
      if (old != previous->originals.end())
        visible = old->visible;
    }
    candidate.originals.push_back({id, resolved.item, visible});
    obs_sceneitem_addref(resolved.item);
  }
  (void)plan;
  (void)scene;
  (void)response;
  return true;
}

struct Commit {
  obs_data_t *request = nullptr;
  obs_data_t *response = nullptr;
  uint64_t reset_trace_id = 0;
};

void commit_graphics(void *data)
{
  auto &commit = *static_cast<Commit *>(data);
  try {
    Plan plan;
    if (!parse_plan(commit.request, plan, commit.response))
      return;
    SourceRef source_handle(obs_get_source_by_name(plan.scene_name.c_str()));
    auto *source = source_handle.get();
    obs_scene_t *scene = source ? obs_scene_from_source(source) : nullptr;
    if (!scene) {
      set_error(commit.response, "scene-not-found", plan.scene_name.c_str());
      return;
    }
    const char *scene_uuid_value = obs_source_get_uuid(source);
    const char *runtime_instance = std::getenv("PULSAR_RUNTIME_INSTANCE_ID");
    if (!scene_uuid_value || !runtime_instance ||
        plan.runtime_instance_id != runtime_instance) {
      set_error(commit.response, "runtime-instance-mismatch",
                "request must target this Pulsar runtime instance");
      return;
    }
    const std::string scene_uuid(scene_uuid_value);
    auto fence_it = generation_fences.find(scene_uuid);
    const uint64_t last = fence_it == generation_fences.end()
                              ? 0
                              : fence_it->second.generation;
    const auto active_it = active_compositions.find(scene_uuid);
    ActiveComposition *previous = active_it == active_compositions.end()
                                     ? nullptr
                                     : &active_it->second;
    if (fence_it != generation_fences.end() &&
        fence_it->second.scene_source.get() != source) {
      set_error(commit.response, "scene-identity-mismatch", plan.scene_name.c_str());
      return;
    }
    if (plan.generation <= last) {
      if (previous && plan.generation == previous->generation &&
          plan.plan_hash == previous->plan_hash &&
          plan.replaced_scene_item_ids == previous->replaced_scene_item_ids &&
          plan.coordinate_space == previous->coordinate_space &&
          plan.protocol_version == previous->protocol_version &&
          plan.runtime_instance_id == previous->runtime_instance_id) {
        obs_data_set_bool(commit.response, "ok", true);
        obs_data_set_bool(commit.response, "idempotent", true);
        obs_data_set_int(commit.response, "generation",
                         static_cast<long long>(plan.generation));
      } else {
        set_error(commit.response, "stale-generation", plan.scene_name.c_str());
      }
      return;
    }
    if (previous && plan.runtime_instance_id != previous->runtime_instance_id) {
      set_error(commit.response, "runtime-instance-mismatch",
                "reset the prior runtime graph before changing its fence");
      return;
    }
    if (previous &&
        obs_scene_find_sceneitem_by_id(scene, obs_sceneitem_get_id(previous->root)) !=
            previous->root) {
      set_error(commit.response, "active-root-replaced", plan.scene_name.c_str());
      return;
    }

    CandidateGuard candidate;
    candidate.state.protocol_version = plan.protocol_version;
    candidate.state.coordinate_space = plan.coordinate_space;
    candidate.state.scene_uuid = scene_uuid;
    candidate.state.runtime_instance_id = plan.runtime_instance_id;
    candidate.state.generation = plan.generation;
    candidate.state.plan_hash = plan.plan_hash;
    candidate.state.replaced_scene_item_ids = plan.replaced_scene_item_ids;
    std::unordered_map<int64_t, ResolvedCapture> captures;
    if (!resolve_captures(plan, scene, source, previous,
                          candidate.state.dependency_sources, captures,
                          commit.response)) {
      return;
    }
    if (!copy_original_visibility(plan, scene, previous, candidate.state,
                                  captures, commit.response))
      return;
    const Group &root = plan.groups[plan.by_id.at(root_id)];
    if (!build_group(plan, root, scene, captures, &candidate.state.root,
                     commit.response) || !candidate.state.root) {
      if (!obs_data_has_user_value(commit.response, "error"))
        set_error(commit.response, "candidate-build-failed");
      return;
    }

    auto [slot_it, slot_inserted] = active_compositions.try_emplace(scene_uuid);
    ActiveSlotReservation slot_reservation{&scene_uuid, slot_inserted, false};
    ActiveComposition *slot = &slot_it->second;
    previous = slot_inserted ? nullptr : slot;
    if (!previous && slot->root) {
      set_error(commit.response, "active-state-inconsistent", plan.scene_name.c_str());
      return;
    }
    if (previous &&
        (previous->generation != last || previous->scene_uuid != scene_uuid)) {
      set_error(commit.response, "active-state-inconsistent", plan.scene_name.c_str());
      return;
    }

    GenerationFenceReservation fence_reservation{&scene_uuid, false, false};
    if (fence_it == generation_fences.end()) {
      SourceRef scene_source_ref(obs_source_get_ref(source));
      if (!scene_source_ref) {
        set_error(commit.response, "scene-not-retainable", plan.scene_name.c_str());
        return;
      }
      auto insertion = generation_fences.try_emplace(scene_uuid,
                                                      std::move(scene_source_ref));
      fence_it = insertion.first;
      fence_reservation.inserted = insertion.second;
    }
    if (fence_it->second.scene_source.get() != source) {
      set_error(commit.response, "scene-identity-mismatch", plan.scene_name.c_str());
      return;
    }

    SwapVisibility swap{candidate.state.root, previous ? previous->root : nullptr,
                        plan.groups[plan.by_id.at(root_id)].visible,
                        previous ? &previous->originals : nullptr,
                        &candidate.state.originals};
    obs_scene_atomic_update(scene, swap_visibility, &swap);
    std::swap(*slot, candidate.state);
    fence_it->second.generation = plan.generation;
    slot_reservation.committed = true;
    fence_reservation.committed = true;
    obs_data_set_bool(commit.response, "ok", true);
    obs_data_set_int(commit.response, "generation",
                     static_cast<long long>(plan.generation));
    obs_data_set_string(commit.response, "planHash", plan.plan_hash.c_str());
  } catch (const std::exception &error) {
    set_error(commit.response, "composition-exception", error.what());
  } catch (...) {
    set_error(commit.response, "composition-exception", "unknown");
  }
}

void commit_request(obs_data_t *request, obs_data_t *response, void *)
{
  Commit commit{request, response};
  obs_queue_task(OBS_TASK_GRAPHICS, commit_graphics, &commit, true);
}

struct Reset {
  ActiveComposition *active = nullptr;
  obs_data_t *response = nullptr;
};

void reset_graphics(void *data)
{
  auto &request = *static_cast<Commit *>(data);
  reset_trace(request.reset_trace_id, "graphics_enter");
  std::string scene_name;
  reset_trace(request.reset_trace_id, "scene_name_read_begin");
  if (!read_string(request.request, "sceneName", scene_name, request.response, 512)) {
    reset_trace(request.reset_trace_id, "scene_name_read_rejected");
    return;
  }
  reset_trace(request.reset_trace_id, "scene_name_read_end");
  reset_trace(request.reset_trace_id, "scene_lookup_begin");
  SourceRef source_handle(obs_get_source_by_name(scene_name.c_str()));
  obs_source_t *source = source_handle.get();
  obs_scene_t *scene = source ? obs_scene_from_source(source) : nullptr;
  const char *scene_uuid_value = source ? obs_source_get_uuid(source) : nullptr;
  reset_trace(request.reset_trace_id, "scene_lookup_end");
  if (!scene || !scene_uuid_value) {
    set_error(request.response, "scene-not-found", scene_name.c_str());
    reset_trace(request.reset_trace_id, "scene_not_found");
    return;
  }
  const std::string scene_uuid(scene_uuid_value);
  reset_trace(request.reset_trace_id, "active_map_lookup_begin");
  auto found = active_compositions.find(scene_uuid);
  reset_trace(request.reset_trace_id, "active_map_lookup_end");
  if (found == active_compositions.end()) {
    obs_data_set_bool(request.response, "ok", true);
    obs_data_set_bool(request.response, "reset", false);
    reset_trace(request.reset_trace_id, "no_active_composition");
    return;
  }
  int64_t generation = 0;
  if (!read_integer(request.request, "generation", generation, request.response) ||
      generation < 1) {
    set_error(request.response, "invalid-generation", "reset requires active generation");
    reset_trace(request.reset_trace_id, "generation_rejected");
    return;
  }
  std::string runtime_instance_id;
  if (!read_string(request.request, "runtimeInstanceId", runtime_instance_id,
                   request.response, 256)) {
    reset_trace(request.reset_trace_id, "runtime_instance_read_rejected");
    return;
  }
  auto &state = found->second;
  const char *runtime_instance = std::getenv("PULSAR_RUNTIME_INSTANCE_ID");
  if (!runtime_instance || runtime_instance_id != runtime_instance ||
      runtime_instance_id != state.runtime_instance_id) {
    set_error(request.response, "runtime-instance-mismatch", scene_name.c_str());
    reset_trace(request.reset_trace_id, "runtime_instance_rejected");
    return;
  }
  reset_trace(request.reset_trace_id, "runtime_instance_valid");
  if (static_cast<uint64_t>(generation) != state.generation) {
    set_error(request.response, "stale-generation", scene_name.c_str());
    reset_trace(request.reset_trace_id, "generation_mismatch");
    return;
  }
  reset_trace(request.reset_trace_id, "generation_valid");
  if (obs_scene_find_sceneitem_by_id(scene, obs_sceneitem_get_id(state.root)) != state.root) {
    set_error(request.response, "active-root-replaced", scene_name.c_str());
    reset_trace(request.reset_trace_id, "active_root_mismatch");
    return;
  }
  reset_trace(request.reset_trace_id, "active_root_valid");
  struct ResetVisibility {
    ActiveComposition *state;
  } visibility{&state};
  auto callback = [](void *param, obs_scene_t *current_scene) {
    auto &v = *static_cast<ResetVisibility *>(param);
    if (obs_scene_find_sceneitem_by_id(current_scene,
                                       obs_sceneitem_get_id(v.state->root)) == v.state->root)
      obs_sceneitem_set_visible(v.state->root, false);
    for (const auto &original : v.state->originals)
      if (obs_scene_find_sceneitem_by_id(current_scene, original.id) == original.item)
        obs_sceneitem_set_visible(original.item, original.visible);
  };
  reset_trace(request.reset_trace_id, "atomic_update_begin");
  obs_scene_atomic_update(scene, callback, &visibility);
  reset_trace(request.reset_trace_id, "atomic_update_end");
  reset_trace(request.reset_trace_id, "sceneitem_remove_begin");
  obs_sceneitem_remove(state.root);
  reset_trace(request.reset_trace_id, "sceneitem_remove_end");
  reset_trace(request.reset_trace_id, "release_state_begin");
  release_state(state);
  reset_trace(request.reset_trace_id, "release_state_end");
  reset_trace(request.reset_trace_id, "active_map_erase_begin");
  active_compositions.erase(found);
  reset_trace(request.reset_trace_id, "active_map_erase_end");
  obs_data_set_bool(request.response, "ok", true);
  obs_data_set_bool(request.response, "reset", true);
  obs_data_set_int(request.response, "generation", generation);
  reset_trace(request.reset_trace_id, "graphics_exit_success");
}

void reset_request(obs_data_t *request, obs_data_t *response, void *)
{
  Commit commit{request, response};
  commit.reset_trace_id = next_reset_trace_id();
  reset_trace(commit.reset_trace_id, "request_enter");
  reset_trace(commit.reset_trace_id, "queue_wait_begin");
  obs_queue_task(OBS_TASK_GRAPHICS, reset_graphics, &commit, true);
  reset_trace(commit.reset_trace_id, "queue_wait_end");
}

void cleanup_composition(obs_scene_t *scene, ActiveComposition &state)
{
  if (!scene && state.root)
    scene = obs_sceneitem_get_scene(state.root);
  if (scene) {
    struct Cleanup {
      ActiveComposition *state;
    } cleanup{&state};
    auto callback = [](void *param, obs_scene_t *current_scene) {
      auto &c = *static_cast<Cleanup *>(param);
      if (c.state->root &&
          obs_scene_find_sceneitem_by_id(current_scene,
                                         obs_sceneitem_get_id(c.state->root)) == c.state->root)
        obs_sceneitem_set_visible(c.state->root, false);
      for (const auto &original : c.state->originals)
        if (obs_scene_find_sceneitem_by_id(current_scene, original.id) == original.item)
          obs_sceneitem_set_visible(original.item, original.visible);
    };
    obs_scene_atomic_update(scene, callback, &cleanup);
    if (state.root &&
        obs_scene_find_sceneitem_by_id(scene, obs_sceneitem_get_id(state.root)) ==
            state.root) {
      obs_sceneitem_remove(state.root);
    }
  }
  release_state(state);
}

void cleanup_removed_source_graphics(void *data)
{
  auto *raw_event = static_cast<RemovedSourceEvent *>(data);
  {
    std::lock_guard<std::mutex> lock(source_remove_mutex);
    pending_source_removals.erase(raw_event);
  }
  std::unique_ptr<RemovedSourceEvent> event(raw_event);
  obs_source_t *removed_source = event->source.get();
  for (auto it = active_compositions.begin(); it != active_compositions.end();) {
    auto &state = it->second;
    const bool target_removed = state.scene_uuid == event->uuid;
    const bool dependency_removed = std::any_of(
        state.dependency_sources.begin(), state.dependency_sources.end(),
        [removed_source](const SourceRef &source) {
          return source.get() == removed_source;
        });
    if (!target_removed && !dependency_removed) {
      ++it;
      continue;
    }
    cleanup_composition(state.root ? obs_sceneitem_get_scene(state.root) : nullptr,
                        state);
    it = active_compositions.erase(it);
  }
  auto fence = generation_fences.find(event->uuid);
  if (fence != generation_fences.end() &&
      fence->second.scene_source.get() == removed_source)
    generation_fences.erase(fence);
}

void source_removed_signal(void *, calldata_t *data)
{
  {
    std::lock_guard<std::mutex> lock(source_remove_mutex);
    if (!accepting_source_removals)
      return;
    ++source_removals_in_flight;
  }
  struct InFlightRemoval {
    ~InFlightRemoval()
    {
      std::lock_guard<std::mutex> lock(source_remove_mutex);
      --source_removals_in_flight;
      if (source_removals_in_flight == 0)
        source_remove_idle.notify_all();
    }
  } in_flight;
  auto *source = static_cast<obs_source_t *>(calldata_ptr(data, "source"));
  if (!source)
    return;
  SourceRef source_ref(obs_source_get_ref(source));
  if (!source_ref)
    return;
  std::unique_ptr<RemovedSourceEvent> event(new (std::nothrow) RemovedSourceEvent());
  if (!event)
    return;
    event->source = std::move(source_ref);
    try {
      const char *uuid = obs_source_get_uuid(source);
      event->uuid = uuid ? uuid : "";
      {
        std::lock_guard<std::mutex> lock(source_remove_mutex);
        pending_source_removals.insert(event.get());
      }
      // obs_queue_task can invoke this inline when source_remove is emitted from
      // the graphics task. Do not hold source_remove_mutex across that call:
      // the resulting cleanup can release sources and re-enter this signal.
      obs_queue_task(OBS_TASK_GRAPHICS, cleanup_removed_source_graphics,
                     event.get(), false);
      event.release();
    } catch (...) {
      if (event) {
        std::lock_guard<std::mutex> lock(source_remove_mutex);
        pending_source_removals.erase(event.get());
      }
      blog(LOG_WARNING, "[pulsar-capture-effects] unable to queue source cleanup");
    }
}

void cleanup_all_graphics(void *)
{
  for (auto &entry : active_compositions)
    cleanup_composition(nullptr, entry.second);
  active_compositions.clear();
  generation_fences.clear();
}
} // namespace

void group_composition_request(obs_data_t *request, obs_data_t *response,
                               void *)
{
  Commit commit{request, response};
  obs_queue_task(OBS_TASK_GRAPHICS, commit_graphics, &commit, true);
}

void reset_group_composition_request(obs_data_t *request, obs_data_t *response,
                                     void *)
{
  Commit commit{request, response};
  commit.reset_trace_id = next_reset_trace_id();
  reset_trace(commit.reset_trace_id, "request_enter");
  reset_trace(commit.reset_trace_id, "queue_wait_begin");
  obs_queue_task(OBS_TASK_GRAPHICS, reset_graphics, &commit, true);
  reset_trace(commit.reset_trace_id, "queue_wait_end");
}

void group_composition_unload()
{
  signal_handler_t *handler = nullptr;
  {
    std::lock_guard<std::mutex> lock(source_remove_mutex);
    accepting_source_removals = false;
    handler = global_signal_handler;
    global_signal_handler = nullptr;
  }
  if (handler)
    signal_handler_disconnect(handler, "source_remove", source_removed_signal,
                              nullptr);
  {
    std::unique_lock<std::mutex> lock(source_remove_mutex);
    source_remove_idle.wait(lock, [] { return source_removals_in_flight == 0; });
  }
  std::size_t pending_before = 0;
  {
    std::lock_guard<std::mutex> lock(source_remove_mutex);
    pending_before = pending_source_removals.size();
  }
  std::size_t pending_released = 0;
  // obs_shutdown joins the graphics thread before unloading modules. Any
  // removal task still queued at that boundary can no longer run, so take
  // ownership of and release its payload here rather than leave source refs
  // orphaned in libobs' stopped graphics queue.
  obs_enter_graphics();
  for (;;) {
    RemovedSourceEvent *event = nullptr;
    {
      std::lock_guard<std::mutex> lock(source_remove_mutex);
      if (pending_source_removals.empty())
        break;
      auto first = pending_source_removals.begin();
      event = *first;
      pending_source_removals.erase(first);
    }
    delete event;
    ++pending_released;
  }
  // libobs stops and joins its graphics thread before calling module unloads
  // from obs_shutdown(). A synchronous graphics task here would wait forever.
  // At this boundary source-removal callbacks are disconnected and drained,
  // and no graphics worker remains; release the retained scene references
  // under the still-live graphics context before libobs frees it.
  const auto active_count = active_compositions.size();
  const auto fence_count = generation_fences.size();
  cleanup_all_graphics(nullptr);
  obs_leave_graphics();
  blog(LOG_INFO,
       "PULSAR_GROUP_COMPOSITION event=module_unload_cleanup_complete "
       "active_before=%llu fences_before=%llu pending_before=%llu "
       "pending_released=%llu",
       static_cast<unsigned long long>(active_count),
       static_cast<unsigned long long>(fence_count),
       static_cast<unsigned long long>(pending_before),
       static_cast<unsigned long long>(pending_released));
}

bool group_composition_load()
{
  std::lock_guard<std::mutex> lock(source_remove_mutex);
  if (global_signal_handler)
    return true;
  register_group_world_source();
  global_signal_handler = obs_get_signal_handler();
  if (!global_signal_handler)
    return false;
  signal_handler_connect(global_signal_handler, "source_remove",
                         source_removed_signal, nullptr);
  accepting_source_removals = true;
  return true;
}
