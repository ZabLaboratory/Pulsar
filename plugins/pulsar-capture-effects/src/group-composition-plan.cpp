#include "group-composition-plan.hpp"
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <obs.h>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

namespace group_composition {
namespace {
constexpr const char *protocol_name = "pulsar.group-composition/1";
constexpr size_t max_groups = 256;
constexpr size_t max_children = 2048;
constexpr size_t max_replaced_scene_items = 2048;
constexpr uint32_t max_dimension = 16384;
} // namespace

void set_error(obs_data_t *response, const char *code, const char *detail)
{
  std::string text(code);
  if (detail && *detail) {
    text += ": ";
    text += detail;
  }
  obs_data_set_bool(response, "ok", false);
  obs_data_set_string(response, "error", text.c_str());
}

obs_data_item_t *find_item(obs_data_t *data, const char *key,
                           enum obs_data_type expected, obs_data_t *response)
{
  auto *item = obs_data_item_byname(data, key);
  if (!item || obs_data_item_gettype(item) != expected) {
    if (item)
      obs_data_item_release(&item);
    set_error(response, "invalid-plan", key);
    return nullptr;
  }
  return item;
}

bool read_string(obs_data_t *data, const char *key, std::string &out,
                 obs_data_t *response, size_t max_length)
{
  auto *item = find_item(data, key, OBS_DATA_STRING, response);
  if (!item)
    return false;
  const char *value = obs_data_item_get_string(item);
  out = value ? value : "";
  obs_data_item_release(&item);
  if (out.empty() || out.size() > max_length) {
    set_error(response, "invalid-plan", key);
    return false;
  }
  return true;
}

bool read_number(obs_data_t *data, const char *key, double &out,
                 obs_data_t *response)
{
  auto *item = find_item(data, key, OBS_DATA_NUMBER, response);
  if (!item)
    return false;
  out = obs_data_item_get_double(item);
  obs_data_item_release(&item);
  if (!std::isfinite(out)) {
    set_error(response, "invalid-plan", key);
    return false;
  }
  return true;
}

bool read_optional_boolean(obs_data_t *data, const char *key, bool &out,
                           bool &was_present, obs_data_t *response)
{
  was_present = obs_data_has_user_value(data, key);
  if (!was_present)
    return true;
  auto *item = find_item(data, key, OBS_DATA_BOOLEAN, response);
  if (!item)
    return false;
  out = obs_data_item_get_bool(item);
  obs_data_item_release(&item);
  return true;
}

bool read_integer(obs_data_t *data, const char *key, int64_t &out,
                  obs_data_t *response)
{
  double value = 0;
  if (!read_number(data, key, value, response))
    return false;
  constexpr double max_safe_json_integer = 9007199254740991.0;
  if (std::trunc(value) != value || std::abs(value) > max_safe_json_integer) {
    set_error(response, "invalid-plan", key);
    return false;
  }
  out = static_cast<int64_t>(value);
  return true;
}

bool read_object(obs_data_t *data, const char *key, obs_data_t **out,
                 obs_data_t *response, bool allow_null = false,
                 bool *was_null = nullptr)
{
  auto *item = obs_data_item_byname(data, key);
  if (!item || obs_data_item_gettype(item) != OBS_DATA_OBJECT) {
    if (item)
      obs_data_item_release(&item);
    set_error(response, "invalid-plan", key);
    return false;
  }
  auto *object = obs_data_item_get_obj(item);
  obs_data_item_release(&item);
  if (!object) {
    if (!allow_null) {
      set_error(response, "invalid-plan", key);
      return false;
    }
    if (was_null)
      *was_null = true;
    *out = nullptr;
    return true;
  }
  if (was_null)
    *was_null = false;
  *out = object;
  return true;
}

bool read_array(obs_data_t *data, const char *key, obs_data_array_t **out,
                obs_data_t *response)
{
  auto *item = find_item(data, key, OBS_DATA_ARRAY, response);
  if (!item)
    return false;
  *out = obs_data_item_get_array(item);
  obs_data_item_release(&item);
  if (!*out) {
    set_error(response, "invalid-plan", key);
    return false;
  }
  return true;
}

bool read_parent(obs_data_t *data, Group &group, obs_data_t *response)
{
  auto *item = obs_data_item_byname(data, "parentId");
  if (!item) {
    set_error(response, "invalid-plan", "parentId");
    return false;
  }
  const auto type = obs_data_item_gettype(item);
  if (type == OBS_DATA_OBJECT) {
    auto *object = obs_data_item_get_obj(item);
    if (object)
      obs_data_release(object);
    group.parent_is_null = object == nullptr;
    group.parent_id.clear();
  } else if (type == OBS_DATA_STRING) {
    const char *value = obs_data_item_get_string(item);
    group.parent_id = value ? value : "";
    group.parent_is_null = false;
  } else {
    obs_data_item_release(&item);
    set_error(response, "invalid-plan", "parentId");
    return false;
  }
  obs_data_item_release(&item);
  if (!group.parent_is_null && group.parent_id.empty()) {
    set_error(response, "invalid-plan", "parentId");
    return false;
  }
  return true;
}

bool read_affine(obs_data_t *data, Affine &out, obs_data_t *response)
{
  obs_data_t *object = nullptr;
  if (!read_object(data, "affine", &object, response)) {
    // The OBS websocket JSON bridge converts scalar arrays to an empty OBS
    // array. Keep that old wire form an explicit malformed-affine refusal.
    set_error(response, "invalid-affine", "expected named affine object");
    return false;
  }
  bool valid = read_number(object, "m00", out.m00, response) &&
               read_number(object, "m01", out.m01, response) &&
               read_number(object, "tx", out.tx, response) &&
               read_number(object, "m10", out.m10, response) &&
               read_number(object, "m11", out.m11, response) &&
               read_number(object, "ty", out.ty, response);
  obs_data_release(object);
  if (!valid)
    set_error(response, "invalid-affine", "six finite named fields required");
  return valid;
}

bool read_rect(obs_data_t *data, Rect &out, obs_data_t *response)
{
  obs_data_t *object = nullptr;
  if (!read_object(data, "bounds", &object, response))
    return false;
  bool valid = read_number(object, "x", out.x, response) &&
               read_number(object, "y", out.y, response) &&
               read_number(object, "width", out.width, response) &&
               read_number(object, "height", out.height, response);
  obs_data_release(object);
  if (!valid || out.width <= 0 || out.height <= 0 || out.width > max_dimension ||
      out.height > max_dimension) {
    set_error(response, "invalid-bounds", "positive bounded rectangle required");
    return false;
  }
  if (std::trunc(out.width) != out.width || std::trunc(out.height) != out.height) {
    set_error(response, "invalid-bounds", "native group extents must be whole pixels");
    return false;
  }
  return true;
}

bool read_local_rect(obs_data_t *data, Rect &out, obs_data_t *response)
{
  obs_data_t *object = nullptr;
  if (!read_object(data, "rect", &object, response))
    return false;
  const bool valid = read_number(object, "x", out.x, response) &&
                     read_number(object, "y", out.y, response) &&
                     read_number(object, "width", out.width, response) &&
                     read_number(object, "height", out.height, response);
  obs_data_release(object);
  if (!valid || out.width <= 0 || out.height <= 0 ||
      out.width > max_dimension || out.height > max_dimension ||
      std::abs(out.x) > 32768 || std::abs(out.y) > 32768) {
    set_error(response, "invalid-band-rect",
              "finite local rectangle with positive bounded extents required");
    return false;
  }
  return true;
}

bool read_crop(obs_data_t *data, Crop &crop, obs_data_t *response)
{
  obs_data_t *object = nullptr;
  if (!read_object(data, "crop", &object, response))
    return false;
  int64_t left = 0, top = 0, right = 0, bottom = 0;
  bool valid = read_integer(object, "left", left, response) &&
               read_integer(object, "top", top, response) &&
               read_integer(object, "right", right, response) &&
               read_integer(object, "bottom", bottom, response);
  obs_data_release(object);
  if (!valid || left < 0 || top < 0 || right < 0 || bottom < 0 ||
      left > INT32_MAX || top > INT32_MAX || right > INT32_MAX ||
      bottom > INT32_MAX) {
    set_error(response, "invalid-crop", "nonnegative pixel crop required");
    return false;
  }
  crop = {static_cast<int>(left), static_cast<int>(top),
          static_cast<int>(right), static_cast<int>(bottom)};
  return true;
}

bool read_mask_null(obs_data_t *data, bool &is_null, obs_data_t *response)
{
  obs_data_t *unused = nullptr;
  if (!read_object(data, "mask", &unused, response, true, &is_null))
    return false;
  if (unused)
    obs_data_release(unused);
  if (!is_null) {
    set_error(response, "unsupported-mask",
              "coverage must be materialized before group mutation");
    return false;
  }
  return true;
}

bool identity(const Affine &a);

bool read_child(obs_data_t *data, Child &child, obs_data_t *response)
{
  std::string kind;
  if (!read_string(data, "kind", kind, response, 32))
    return false;
  if (kind == "capture") {
    child.kind = ChildKind::Capture;
    if (!read_optional_boolean(data, "visible", child.visible,
                               child.has_visible, response))
      return false;
    if (!read_integer(data, "sceneItemId", child.scene_item_id, response) ||
        child.scene_item_id <= 0 || !read_affine(data, child.affine, response) ||
        !read_crop(data, child.crop, response)) {
      if (child.scene_item_id <= 0)
        set_error(response, "invalid-capture", "sceneItemId must be positive");
      return false;
    }
    if (obs_data_has_user_value(data, "localOpacity")) {
      if (!read_number(data, "localOpacity", child.local_opacity, response) ||
          child.local_opacity < 0 || child.local_opacity > 1) {
        set_error(response, "invalid-capture", "localOpacity outside 0..1");
        return false;
      }
    }
    return true;
  }
  if (kind == "group") {
    child.kind = ChildKind::Group;
    return read_string(data, "id", child.group_id, response, 256);
  }
  if (kind == "solarBand") {
    child.kind = ChildKind::SolarBand;
    if (!read_integer(data, "band", child.band, response) || child.band < 0) {
      set_error(response, "invalid-solar-band", "band must be a nonnegative integer");
      return false;
    }
    if (!read_integer(data, "sceneItemId", child.scene_item_id, response) ||
        child.scene_item_id <= 0) {
      set_error(response, "invalid-solar-band", "sceneItemId must be positive");
      return false;
    }
    if (!read_affine(data, child.affine, response))
      return false;
    if (!identity(child.affine)) {
      set_error(response, "invalid-solar-band", "band affine must be identity");
      return false;
    }
    if (!read_crop(data, child.crop, response) ||
        !read_local_rect(data, child.rect, response))
      return false;
    child.has_visible = true;
    child.visible = true;
    return true;
  }
  set_error(response, "unsupported-child-kind", kind.c_str());
  return false;
}

bool parse_group(obs_data_t *data, Group &group, obs_data_t *response)
{
  bool visible_present = false;
  if (!read_string(data, "id", group.id, response, 256) ||
      !read_parent(data, group, response) ||
      !read_integer(data, "order", group.order, response) || group.order < 0 ||
      !read_number(data, "opacity", group.opacity, response) ||
      group.opacity < 0 || group.opacity > 1 ||
      !read_optional_boolean(data, "visible", group.visible,
                             visible_present, response) ||
      !read_string(data, "blendMode", group.blend_mode, response, 32) ||
      group.blend_mode != "normal" || !read_rect(data, group.bounds, response) ||
      !read_affine(data, group.affine, response) ||
      !read_mask_null(data, group.mask_is_null, response)) {
    if (group.blend_mode != "normal")
      set_error(response, "unsupported-blend-mode", group.blend_mode.c_str());
    return false;
  }
  if (std::abs(group.bounds.x) > 1e-9 || std::abs(group.bounds.y) > 1e-9) {
    set_error(response, "invalid-bounds", "group bounds must begin at local origin");
    return false;
  }
  obs_data_array_t *children = nullptr;
  if (!read_array(data, "children", &children, response))
    return false;
  const size_t count = obs_data_array_count(children);
  if (count > max_children) {
    obs_data_array_release(children);
    set_error(response, "plan-too-large", "group child limit exceeded");
    return false;
  }
  group.children.reserve(count);
  for (size_t i = 0; i < count; ++i) {
    obs_data_t *child_data = obs_data_array_item(children, i);
    if (!child_data) {
      obs_data_array_release(children);
      set_error(response, "invalid-plan", "children must be objects");
      return false;
    }
    Child child;
    const bool valid = read_child(child_data, child, response);
    obs_data_release(child_data);
    if (!valid) {
      obs_data_array_release(children);
      return false;
    }
    group.children.push_back(std::move(child));
  }
  obs_data_array_release(children);
  return true;
}

bool validate_graph(Plan &plan, obs_data_t *response);

bool parse_plan(obs_data_t *request, Plan &plan, obs_data_t *response)
{
  std::string protocol;
  if (!read_string(request, "protocol", protocol, response, 64) ||
      protocol != protocol_name) {
    set_error(response, "unsupported-protocol", protocol.c_str());
    return false;
  }
  if (!read_string(request, "sceneName", plan.scene_name, response, 512) ||
      !read_string(request, "runtimeInstanceId", plan.runtime_instance_id, response, 256) ||
      !read_string(request, "planHash", plan.plan_hash, response, 128))
    return false;
  const char *runtime_instance = std::getenv("PULSAR_RUNTIME_INSTANCE_ID");
  if (!runtime_instance || !*runtime_instance || plan.runtime_instance_id != runtime_instance) {
    set_error(response, "runtime-instance-mismatch",
              "request must be fenced to this Pulsar process instance");
    return false;
  }
  int64_t generation = 0;
  if (!read_integer(request, "generation", generation, response) || generation < 1) {
    set_error(response, "invalid-generation", "generation must be a positive integer");
    return false;
  }
  plan.generation = static_cast<uint64_t>(generation);

  obs_data_item_t *replaced_item = obs_data_item_byname(request,
                                                        "replacedSceneItems");
  if (replaced_item) {
    if (obs_data_item_gettype(replaced_item) != OBS_DATA_ARRAY) {
      obs_data_item_release(&replaced_item);
      set_error(response, "invalid-replaced-scene-items",
                "replacedSceneItems must be an array");
      return false;
    }
    obs_data_array_t *replaced = obs_data_item_get_array(replaced_item);
    obs_data_item_release(&replaced_item);
    if (!replaced) {
      set_error(response, "invalid-replaced-scene-items",
                "replacedSceneItems must be an array");
      return false;
    }
    const size_t replaced_count = obs_data_array_count(replaced);
    if (replaced_count > max_replaced_scene_items) {
      obs_data_array_release(replaced);
      set_error(response, "plan-too-large",
                "replaced scene item limit exceeded");
      return false;
    }
    std::unordered_set<int64_t> unique_replaced;
    plan.replaced_scene_item_ids.reserve(replaced_count);
    for (size_t i = 0; i < replaced_count; ++i) {
      obs_data_t *entry = obs_data_array_item(replaced, i);
      if (!entry) {
        obs_data_array_release(replaced);
        set_error(response, "invalid-replaced-scene-items",
                  "each replaced item must be an object");
        return false;
      }
      int64_t scene_item_id = 0;
      const bool valid = read_integer(entry, "sceneItemId", scene_item_id,
                                      response) && scene_item_id > 0;
      obs_data_release(entry);
      if (!valid) {
        obs_data_array_release(replaced);
        set_error(response, "invalid-replaced-scene-items",
                  "sceneItemId must be a positive safe integer");
        return false;
      }
      if (!unique_replaced.insert(scene_item_id).second) {
        obs_data_array_release(replaced);
        set_error(response, "duplicate-replaced-scene-item",
                  std::to_string(scene_item_id).c_str());
        return false;
      }
      plan.replaced_scene_item_ids.push_back(scene_item_id);
    }
    obs_data_array_release(replaced);
    std::sort(plan.replaced_scene_item_ids.begin(),
              plan.replaced_scene_item_ids.end());
  }

  obs_data_array_t *groups = nullptr;
  if (!read_array(request, "groups", &groups, response))
    return false;
  const size_t count = obs_data_array_count(groups);
  if (!count || count > max_groups) {
    obs_data_array_release(groups);
    set_error(response, "invalid-group-count", "one root and bounded groups required");
    return false;
  }
  plan.groups.reserve(count);
  for (size_t i = 0; i < count; ++i) {
    obs_data_t *group_data = obs_data_array_item(groups, i);
    if (!group_data) {
      obs_data_array_release(groups);
      set_error(response, "invalid-plan", "groups must be objects");
      return false;
    }
    Group group;
    const bool valid = parse_group(group_data, group, response);
    obs_data_release(group_data);
    if (!valid) {
      obs_data_array_release(groups);
      return false;
    }
    if (plan.by_id.find(group.id) != plan.by_id.end()) {
      obs_data_array_release(groups);
      set_error(response, "duplicate-group-id", group.id.c_str());
      return false;
    }
    plan.by_id.emplace(group.id, plan.groups.size());
    plan.child_count += group.children.size();
    if (plan.child_count > max_children) {
      obs_data_array_release(groups);
      set_error(response, "plan-too-large", "child limit exceeded");
      return false;
    }
    plan.groups.push_back(std::move(group));
  }
  obs_data_array_release(groups);
  return validate_graph(plan, response);
}

bool identity(const Affine &a)
{
  return std::abs(a.m00 - 1) < 1e-9 && std::abs(a.m01) < 1e-9 &&
         std::abs(a.tx) < 1e-9 && std::abs(a.m10) < 1e-9 &&
         std::abs(a.m11 - 1) < 1e-9 && std::abs(a.ty) < 1e-9;
}

bool validate_group_affine(const Affine &a, obs_data_t *response)
{
  const double det = a.m00 * a.m11 - a.m01 * a.m10;
  if (std::abs(det) < 1e-12)
    return true; // A finite singular node is a valid empty visual.
  const double first = std::hypot(a.m00, a.m10);
  const double second = std::hypot(a.m01, a.m11);
  const double dot = a.m00 * a.m01 + a.m10 * a.m11;
  if (first <= 1e-12 || second <= 1e-12 ||
      std::abs(dot) > 1e-6 * first * second) {
    set_error(response, "unsupported-affine",
              "OBS scene transforms cannot represent affine shear");
    return false;
  }
  return true;
}

bool validate_graph(Plan &plan, obs_data_t *response)
{
  auto root_it = plan.by_id.find(root_id);
  if (root_it == plan.by_id.end()) {
    set_error(response, "missing-root", root_id);
    return false;
  }
  const Group &root = plan.groups[root_it->second];
  if (!root.parent_is_null || root.order != 0 || !identity(root.affine) ||
      std::abs(root.opacity - 1) > 1e-9 || root.blend_mode != "normal" ||
      !root.visible || !root.mask_is_null) {
    set_error(response, "invalid-root", "reserved root must be null-parent identity");
    return false;
  }
  std::unordered_set<int64_t> seen_solar_bands;
  for (const auto &group : plan.groups) {
    if (group.id == root_id)
      continue;
    if (group.parent_is_null || plan.by_id.find(group.parent_id) == plan.by_id.end()) {
      set_error(response, "invalid-parent", group.id.c_str());
      return false;
    }
    if (!validate_group_affine(group.affine, response))
      return false;
    if (group.parent_id == group.id) {
      set_error(response, "cyclic-group-reference", group.id.c_str());
      return false;
    }
  }
  for (const auto &group : plan.groups) {
    std::unordered_set<std::string> seen_children;
    for (size_t i = 0; i < group.children.size(); ++i) {
      const auto &child = group.children[i];
      if (child.kind == ChildKind::SolarBand &&
          !seen_solar_bands.insert(child.band).second) {
        set_error(response, "duplicate-solar-band",
                  std::to_string(child.band).c_str());
        return false;
      }
      if (child.kind == ChildKind::SolarBand &&
          (child.rect.x < 0 || child.rect.y < 0 ||
           child.rect.x + child.rect.width > group.bounds.width + 1e-6 ||
           child.rect.y + child.rect.height > group.bounds.height + 1e-6)) {
        set_error(response, "invalid-band-rect",
                  "atlas page rectangle must fit the owning group bounds");
        return false;
      }
      if (child.kind != ChildKind::Group)
        continue;
      auto child_it = plan.by_id.find(child.group_id);
      if (child_it == plan.by_id.end() || !seen_children.insert(child.group_id).second) {
        set_error(response, "invalid-group-child", child.group_id.c_str());
        return false;
      }
      const auto &nested = plan.groups[child_it->second];
      if (nested.parent_is_null || nested.parent_id != group.id ||
          nested.order != static_cast<int64_t>(i)) {
        set_error(response, "group-parent-order-mismatch", nested.id.c_str());
        return false;
      }
    }
  }
  std::unordered_set<std::string> visited;
  std::vector<std::string> pending{root_id};
  while (!pending.empty()) {
    auto id = std::move(pending.back());
    pending.pop_back();
    if (!visited.insert(id).second) {
      set_error(response, "cyclic-group-reference", id.c_str());
      return false;
    }
    const auto &group = plan.groups[plan.by_id.at(id)];
    for (const auto &child : group.children)
      if (child.kind == ChildKind::Group)
        pending.push_back(child.group_id);
  }
  if (visited.size() != plan.groups.size()) {
    set_error(response, "unreachable-group", "all groups must be rooted at synthetic root");
    return false;
  }
  return true;
}


} // namespace group_composition
