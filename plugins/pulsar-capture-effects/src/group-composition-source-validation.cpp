#include "group-composition-source-validation.hpp"
#include "group-composition-plan.hpp"

#include <callback/calldata.h>
#include <algorithm>
#include <memory>

namespace group_composition_detail {
namespace {

struct SceneSourceCheck {
  obs_source_t *target = nullptr;
  std::unordered_set<obs_source_t *> visiting;
  std::unordered_set<obs_source_t *> visited;
  std::unordered_set<obs_source_t *> *dependency_set = nullptr;
  std::vector<SourceRef> *dependencies = nullptr;
  size_t inspected = 0;
  const char *failure = nullptr;
};

struct SceneSourceWalk {
  SceneSourceCheck *check = nullptr;
  size_t depth = 0;
  bool ok = true;
};

bool inspect_scene_source(obs_source_t *source, SceneSourceCheck &check,
                          size_t depth)
{
  if (!source)
    return false;
  if (source == check.target) {
    check.failure = "capture-source-target-cycle";
    return false;
  }
  if (check.visiting.count(source)) {
    check.failure = "capture-source-cycle";
    return false;
  }
  if (check.visited.count(source))
    return true;
  if (check.dependency_set->count(source))
    return true;
  if (depth > group_composition::max_nested_scene_depth ||
      ++check.inspected > group_composition::max_nested_scene_sources) {
    check.failure = "capture-source-graph-too-large";
    return false;
  }
  check.visiting.insert(source);
  if (check.dependency_set->insert(source).second) {
    SourceRef source_ref(obs_source_get_ref(source));
    if (!source_ref) {
      check.failure = "capture-source-invalid";
      return false;
    }
    check.dependencies->push_back(std::move(source_ref));
  }
  if (!obs_source_is_scene(source)) {
    check.visiting.erase(source);
    check.visited.insert(source);
    return true;
  }
  obs_scene_t *scene = obs_scene_from_source(source);
  if (!scene) {
    check.failure = "capture-source-invalid-scene";
    return false;
  }
  SceneSourceWalk walk{&check, depth, true};
  obs_scene_enum_items(scene, [](obs_scene_t *, obs_sceneitem_t *item, void *param) {
    auto &state = *static_cast<SceneSourceWalk *>(param);
    if (obs_sceneitem_is_group(item)) {
      state.check->failure = "capture-source-group-unsupported";
      state.ok = false;
      return false;
    }
    obs_source_t *child = obs_sceneitem_get_source(item);
    if (!child || obs_source_is_group(child)) {
      state.check->failure = "capture-source-group-unsupported";
      state.ok = false;
      return false;
    }
    if (obs_source_is_scene(child) &&
        !inspect_scene_source(child, *state.check, state.depth + 1)) {
      state.ok = false;
      return false;
    }
    return true;
  }, &walk);
  check.visiting.erase(source);
  if (!walk.ok) {
    check.visiting.erase(source);
    return false;
  }
  check.visited.insert(source);
  return true;
}

} // namespace

bool validate_capture_source(obs_source_t *source, obs_source_t *target,
                             std::unordered_set<obs_source_t *> &dependency_set,
                             std::vector<SourceRef> &dependencies,
                             obs_data_t *response)
{
  if (!source || obs_source_removed(source) ||
      !(obs_source_get_output_flags(source) & OBS_SOURCE_VIDEO) ||
      obs_source_is_group(source)) {
    group_composition::set_error(response, "capture-source-invalid");
    return false;
  }
  SceneSourceCheck check;
  check.target = target;
  check.dependency_set = &dependency_set;
  check.dependencies = &dependencies;
  if (!inspect_scene_source(source, check, 0)) {
    group_composition::set_error(response,
                                 check.failure ? check.failure
                                               : "capture-source-invalid");
    return false;
  }
  return true;
}

} // namespace group_composition_detail
