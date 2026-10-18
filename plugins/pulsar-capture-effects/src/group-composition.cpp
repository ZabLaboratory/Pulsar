#include "reset-trace.hpp"
#include "group-composition.hpp"
#include "capture-effects.hpp"
#include "group-composition-plan.hpp"
#include "group-composition-source-validation.hpp"
#include "group-composition-world-source.hpp"
#include <algorithm>
#include <chrono>
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
#include <cstring>
#include <memory>
#include <mutex>
#include <new>
#include <stdexcept>
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
struct DataValueSnapshot {
  std::string name;
  enum obs_data_type type = OBS_DATA_NULL;
  enum obs_data_number_type number_type = OBS_DATA_NUM_INVALID;
  std::string string_value;
  long long integer_value = 0;
  double double_value = 0;
  bool bool_value = false;
  std::vector<DataValueSnapshot> object_values;
  std::vector<std::vector<DataValueSnapshot>> array_values;
};
struct DataSnapshot {
  std::vector<DataValueSnapshot> values;
  bool valid = false;
};
struct InputBinding {
  int64_t id = 0;
  obs_sceneitem_t *item = nullptr;
  obs_source_t *source = nullptr;
  SourceRef source_ref;
  std::string source_id;
  std::string source_name;
  uint32_t width = 0;
  uint32_t height = 0;
  bool visible = false;
  bool source_enabled = false;
  bool has_source = false;
  obs_scale_type scale_filter = OBS_SCALE_DISABLE;
};
struct DataRef {
  obs_data_t *data = nullptr;
  explicit DataRef(obs_data_t *value = nullptr) : data(value) {}
  DataRef(const DataRef &) = delete;
  DataRef &operator=(const DataRef &) = delete;
  ~DataRef() { obs_data_release(data); }
};
struct DataArrayRef {
  obs_data_array_t *array = nullptr;
  explicit DataArrayRef(obs_data_array_t *value = nullptr) : array(value) {}
  DataArrayRef(const DataArrayRef &) = delete;
  DataArrayRef &operator=(const DataArrayRef &) = delete;
  ~DataArrayRef() { obs_data_array_release(array); }
};

bool capture_data_values(obs_data_t *data,
                         std::vector<DataValueSnapshot> &values)
{
  if (!data)
    return false;
  values.clear();
  struct ItemIterator {
    obs_data_item_t *item = nullptr;
    explicit ItemIterator(obs_data_t *data) : item(obs_data_first(data)) {}
    ~ItemIterator()
    {
      if (item)
        obs_data_item_release(&item);
    }
    bool advance() { return obs_data_item_next(&item); }
  } iterator(data);
  while (iterator.item) {
    obs_data_item_t *item = iterator.item;
    if (obs_data_item_has_user_value(item)) {
      DataValueSnapshot value;
      const char *name = obs_data_item_get_name(item);
      if (!name || !*name)
        return false;
      value.name = name;
      value.type = obs_data_item_gettype(item);
      switch (value.type) {
      case OBS_DATA_STRING: {
        const char *string = obs_data_item_get_string(item);
        if (!string)
          return false;
        value.string_value = string;
        break;
      }
      case OBS_DATA_NUMBER:
        value.number_type = obs_data_item_numtype(item);
        if (value.number_type == OBS_DATA_NUM_INT) {
          value.integer_value = obs_data_item_get_int(item);
        } else if (value.number_type == OBS_DATA_NUM_DOUBLE) {
          value.double_value = obs_data_item_get_double(item);
          if (!std::isfinite(value.double_value))
            return false;
        } else {
          return false;
        }
        break;
      case OBS_DATA_BOOLEAN:
        value.bool_value = obs_data_item_get_bool(item);
        break;
      case OBS_DATA_OBJECT: {
        DataRef object(obs_data_item_get_obj(item));
        if (!object.data)
          return false;
        if (!capture_data_values(object.data, value.object_values))
          return false;
        break;
      }
      case OBS_DATA_ARRAY: {
        DataArrayRef array(obs_data_item_get_array(item));
        if (!array.array)
          return false;
        const size_t count = obs_data_array_count(array.array);
        value.array_values.reserve(count);
        for (size_t index = 0; index < count; ++index) {
          DataRef object(obs_data_array_item(array.array, index));
          if (!object.data) {
            return false;
          }
          std::vector<DataValueSnapshot> object_values;
          if (!capture_data_values(object.data, object_values))
            return false;
          value.array_values.push_back(std::move(object_values));
        }
        break;
      }
      case OBS_DATA_NULL:
      default:
        return false;
      }
      values.push_back(std::move(value));
    }
    if (!iterator.advance())
      break;
  }
  return true;
}

bool data_value_matches(obs_data_item_t *item,
                        const DataValueSnapshot &expected);

bool data_values_match(obs_data_t *data,
                       const std::vector<DataValueSnapshot> &expected)
{
  if (!data)
    return false;
  size_t actual_user_values = 0;
  struct ItemIterator {
    obs_data_item_t *item = nullptr;
    explicit ItemIterator(obs_data_t *data) : item(obs_data_first(data)) {}
    ~ItemIterator()
    {
      if (item)
        obs_data_item_release(&item);
    }
    bool advance() { return obs_data_item_next(&item); }
  } iterator(data);
  while (iterator.item) {
    obs_data_item_t *item = iterator.item;
    if (obs_data_item_has_user_value(item)) {
      ++actual_user_values;
      const char *name = obs_data_item_get_name(item);
      const auto found = name
                             ? std::find_if(expected.begin(), expected.end(),
                                            [name](const DataValueSnapshot &value) {
                                              return value.name == name;
                                            })
                             : expected.end();
      if (found == expected.end() || !data_value_matches(item, *found))
        return false;
    }
    if (!iterator.advance())
      break;
  }
  return actual_user_values == expected.size();
}

bool data_value_matches(obs_data_item_t *item,
                        const DataValueSnapshot &expected)
{
  if (!item || !obs_data_item_has_user_value(item) ||
      obs_data_item_gettype(item) != expected.type)
    return false;
  switch (expected.type) {
  case OBS_DATA_STRING: {
    const char *value = obs_data_item_get_string(item);
    return value && expected.string_value == value;
  }
  case OBS_DATA_NUMBER:
    if (obs_data_item_numtype(item) != expected.number_type)
      return false;
    if (expected.number_type == OBS_DATA_NUM_INT)
      return obs_data_item_get_int(item) == expected.integer_value;
    if (expected.number_type == OBS_DATA_NUM_DOUBLE) {
      const double value = obs_data_item_get_double(item);
      return std::memcmp(&value, &expected.double_value, sizeof(value)) == 0;
    }
    return false;
  case OBS_DATA_BOOLEAN:
    return obs_data_item_get_bool(item) == expected.bool_value;
  case OBS_DATA_OBJECT: {
    DataRef object(obs_data_item_get_obj(item));
    return object.data && data_values_match(object.data, expected.object_values);
  }
  case OBS_DATA_ARRAY: {
    DataArrayRef array(obs_data_item_get_array(item));
    if (!array.array)
      return false;
    bool matches = obs_data_array_count(array.array) ==
                   expected.array_values.size();
    for (size_t index = 0; matches && index < expected.array_values.size(); ++index) {
      DataRef object(obs_data_array_item(array.array, index));
      matches = object.data &&
                data_values_match(object.data, expected.array_values[index]);
    }
    return matches;
  }
  case OBS_DATA_NULL:
  default:
    return false;
  }
}

bool capture_data_snapshot(obs_data_t *data, DataSnapshot &snapshot)
{
  snapshot.values.clear();
  snapshot.valid = capture_data_values(data, snapshot.values);
  return snapshot.valid;
}

bool data_snapshot_matches(obs_data_t *data, const DataSnapshot &snapshot)
{
  return snapshot.valid && data_values_match(data, snapshot.values);
}

struct FilterSnapshot {
  SourceRef source;
  std::string source_id;
  std::string source_name;
  bool enabled = false;
  DataSnapshot settings;
  FilterSnapshot() = default;
  FilterSnapshot(SourceRef filter, std::string id, std::string name,
                 bool filter_enabled, DataSnapshot filter_settings)
      : source(std::move(filter)), source_id(std::move(id)),
        source_name(std::move(name)), enabled(filter_enabled),
        settings(std::move(filter_settings)) {}
  FilterSnapshot(const FilterSnapshot &) = delete;
  FilterSnapshot &operator=(const FilterSnapshot &) = delete;
  FilterSnapshot(FilterSnapshot &&) noexcept = default;
  FilterSnapshot &operator=(FilterSnapshot &&) noexcept = default;
};
struct SceneItemSnapshot {
  obs_sceneitem_t *item = nullptr;
  int64_t id = 0;
  int order_position = -1;
  SourceRef source;
  std::string source_id;
  std::string source_name;
  uint32_t source_width = 0;
  uint32_t source_height = 0;
  DataSnapshot source_settings;
  bool compare_source_semantics = false;
  bool source_enabled = false;
  bool group = false;
  obs_scene_t *group_scene = nullptr;
  obs_transform_info transform{};
  obs_sceneitem_crop crop{};
  bool visible = false;
  obs_blending_type blending_mode = OBS_BLEND_NORMAL;
  obs_blending_method blending_method = OBS_BLEND_METHOD_DEFAULT;
  obs_scale_type scale_filter = OBS_SCALE_DISABLE;
  DataSnapshot private_settings;
  std::vector<FilterSnapshot> filters;

  SceneItemSnapshot() = default;
  SceneItemSnapshot(const SceneItemSnapshot &) = delete;
  SceneItemSnapshot &operator=(const SceneItemSnapshot &) = delete;
  SceneItemSnapshot(SceneItemSnapshot &&other) noexcept
      : item(std::exchange(other.item, nullptr)), id(other.id),
        order_position(other.order_position), source(std::move(other.source)),
        source_id(std::move(other.source_id)),
        source_name(std::move(other.source_name)),
        source_width(other.source_width), source_height(other.source_height),
        source_settings(std::move(other.source_settings)),
        compare_source_semantics(other.compare_source_semantics),
        source_enabled(other.source_enabled), group(other.group),
        group_scene(other.group_scene), transform(other.transform),
        crop(other.crop), visible(other.visible),
        blending_mode(other.blending_mode), blending_method(other.blending_method),
        scale_filter(other.scale_filter),
        private_settings(std::move(other.private_settings)),
        filters(std::move(other.filters)) {}
  SceneItemSnapshot &operator=(SceneItemSnapshot &&other) noexcept
  {
    if (this == &other)
      return *this;
    if (item)
      obs_sceneitem_release(item);
    item = std::exchange(other.item, nullptr);
    id = other.id;
    order_position = other.order_position;
    source = std::move(other.source);
    source_id = std::move(other.source_id);
    source_name = std::move(other.source_name);
    source_width = other.source_width;
    source_height = other.source_height;
    source_settings = std::move(other.source_settings);
    compare_source_semantics = other.compare_source_semantics;
    source_enabled = other.source_enabled;
    group = other.group;
    group_scene = other.group_scene;
    transform = other.transform;
    crop = other.crop;
    visible = other.visible;
    blending_mode = other.blending_mode;
    blending_method = other.blending_method;
    scale_filter = other.scale_filter;
    private_settings = std::move(other.private_settings);
    filters = std::move(other.filters);
    return *this;
  }
  ~SceneItemSnapshot()
  {
    if (item)
      obs_sceneitem_release(item);
  }
};
struct PrivateSceneSnapshot {
  obs_scene_t *scene = nullptr;
  SourceRef source;
  std::vector<SceneItemSnapshot> items;
};
struct WorldAffineBinding {
  std::string group_id;
  size_t child_index = 0;
  int64_t scene_item_id = 0;
  SourceRef world_source;
  SourceRef crop_scene_source;
  Affine affine;
};
void swap_plan(Plan &left, Plan &right) noexcept
{
  using std::swap;
  swap(left.protocol_version, right.protocol_version);
  swap(left.coordinate_space, right.coordinate_space);
  left.scene_name.swap(right.scene_name);
  left.plan_hash.swap(right.plan_hash);
  left.runtime_instance_id.swap(right.runtime_instance_id);
  swap(left.generation, right.generation);
  left.groups.swap(right.groups);
  left.replaced_scene_item_ids.swap(right.replaced_scene_item_ids);
  left.by_id.swap(right.by_id);
  swap(left.child_count, right.child_count);
}
struct ActiveComposition {
  int protocol_version = 1;
  CoordinateSpace coordinate_space = CoordinateSpace::OwnerLocal;
  obs_sceneitem_t *root = nullptr;
  std::string scene_uuid;
  std::string runtime_instance_id;
  uint64_t generation = 0;
  std::vector<int64_t> replaced_scene_item_ids;
  std::vector<OriginalItem> originals;
  std::vector<SourceRef> dependency_sources;
  Plan plan_snapshot;
  PrivateSceneSnapshot host_scene_snapshot;
  std::vector<PrivateSceneSnapshot> private_scenes;
  std::vector<WorldAffineBinding> world_affines;
  std::vector<GroupWorldSourceTranslationUpdate> geometry_updates;
  std::vector<InputBinding> input_bindings;
  bool geometry_snapshot_ready = false;

  void swap(ActiveComposition &other) noexcept
  {
    using std::swap;
    swap(protocol_version, other.protocol_version);
    swap(coordinate_space, other.coordinate_space);
    swap(root, other.root);
    scene_uuid.swap(other.scene_uuid);
    runtime_instance_id.swap(other.runtime_instance_id);
    swap(generation, other.generation);
    replaced_scene_item_ids.swap(other.replaced_scene_item_ids);
    originals.swap(other.originals);
    dependency_sources.swap(other.dependency_sources);
    swap_plan(plan_snapshot, other.plan_snapshot);
    swap(host_scene_snapshot, other.host_scene_snapshot);
    private_scenes.swap(other.private_scenes);
    world_affines.swap(other.world_affines);
    geometry_updates.swap(other.geometry_updates);
    input_bindings.swap(other.input_bindings);
    swap(geometry_snapshot_ready, other.geometry_snapshot_ready);
  }
  friend void swap(ActiveComposition &left,
                   ActiveComposition &right) noexcept
  {
    left.swap(right);
  }
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
  obs_scale_type scale_filter = OBS_SCALE_DISABLE;
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
using CommitClock = std::chrono::steady_clock;
using CommitTick = CommitClock::time_point;
double elapsed_ms(CommitTick start, CommitTick end)
{
  return std::chrono::duration<double, std::milli>(end - start).count();
}
struct CommitTiming {
  CommitTick handler_started{};
  CommitTick queue_started{};
  CommitTick graphics_started{};
  CommitTick graphics_finished{};
  double parse_ms = 0;
  double resolve_ms = 0;
  double build_ms = 0;
  double input_binding_snapshot_ms = 0;
  double geometry_snapshot_ms = 0;
  double geometry_update_ms = 0;
  double atomic_swap_ms = 0;
  double retirement_ms = 0;
  bool geometry_snapshot_ready = false;
  bool geometry_translation_plan = false;
  bool geometry_input_bindings_match = false;
  bool geometry_prevalidation_success = false;
  uint32_t geometry_prevalidation_failure = 0;
  bool geometry_fast_path = false;
  uint32_t filter_sources_created = 0;
  CaptureShaderCreationTiming shader_creation;
};
thread_local uint32_t *active_filter_creation_count = nullptr;
struct CountFilterCreations {
  uint32_t *previous = nullptr;
  CaptureShaderCreationTiming *previous_shader = nullptr;
  explicit CountFilterCreations(uint32_t *count,
                                CaptureShaderCreationTiming *shader)
      : previous(std::exchange(active_filter_creation_count, count)),
        previous_shader(capture_effects_exchange_shader_creation_timing(shader)) {}
  ~CountFilterCreations() {
    capture_effects_exchange_shader_creation_timing(previous_shader);
    active_filter_creation_count = previous;
  }
};
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

struct FilterCapture {
  std::vector<FilterSnapshot> *filters = nullptr;
  bool failed = false;
};

void capture_filter_callback(obs_source_t *, obs_source_t *filter, void *data)
{
  auto &capture = *static_cast<FilterCapture *>(data);
  if (capture.failed || !filter)
    return;
  try {
    SourceRef retained(obs_source_get_ref(filter));
    DataSnapshot settings;
    DataRef filter_settings(obs_source_get_settings(filter));
    const bool settings_captured =
        filter_settings.data &&
        capture_data_snapshot(filter_settings.data, settings);
    if (!retained || !settings_captured) {
      capture.failed = true;
      return;
    }
    const char *id = obs_source_get_id(filter);
    const char *name = obs_source_get_name(filter);
    if (!id || !name) {
      capture.failed = true;
      return;
    }
    capture.filters->emplace_back(std::move(retained), id, name,
                                  obs_source_enabled(filter),
                                  std::move(settings));
  } catch (...) {
    capture.failed = true;
  }
}

bool capture_source_snapshot(obs_source_t *source,
                             SceneItemSnapshot &snapshot,
                             bool compare_source_semantics)
{
  if (!source)
    return false;
  snapshot.source = SourceRef(obs_source_get_ref(source));
  const char *id = obs_source_get_id(source);
  const char *name = obs_source_get_name(source);
  if (!snapshot.source || !id || !name)
    return false;
  snapshot.source_id = id;
  snapshot.source_name = name;
  snapshot.source_width = obs_source_get_width(source);
  snapshot.source_height = obs_source_get_height(source);
  snapshot.source_enabled = obs_source_enabled(source);
  snapshot.compare_source_semantics = compare_source_semantics;
  if (!compare_source_semantics)
    return true;
  DataRef settings(obs_source_get_settings(source));
  const bool settings_captured =
      settings.data && capture_data_snapshot(settings.data,
                                             snapshot.source_settings);
  if (!settings_captured)
    return false;
  FilterCapture filters{&snapshot.filters};
  obs_source_enum_filters(source, capture_filter_callback, &filters);
  return !filters.failed;
}

bool capture_scene_item_snapshot(obs_sceneitem_t *item,
                                 SceneItemSnapshot &snapshot,
                                 const std::vector<InputBinding> *input_bindings,
                                 bool include_source_semantics,
                                 obs_source_t *only_semantic_source = nullptr)
{
  if (!item)
    return false;
  obs_sceneitem_addref(item);
  snapshot.item = item;
  snapshot.id = obs_sceneitem_get_id(item);
  snapshot.order_position = obs_sceneitem_get_order_position(item);
  obs_transform_info transform{};
  obs_sceneitem_get_info2(item, &transform);
  snapshot.transform = transform;
  obs_sceneitem_get_crop(item, &snapshot.crop);
  snapshot.visible = obs_sceneitem_visible(item);
  snapshot.blending_mode = obs_sceneitem_get_blending_mode(item);
  snapshot.blending_method = obs_sceneitem_get_blending_method(item);
  snapshot.scale_filter = obs_sceneitem_get_scale_filter(item);
  snapshot.group = obs_sceneitem_is_group(item);
  snapshot.group_scene = snapshot.group
                             ? obs_sceneitem_group_get_scene(item)
                             : nullptr;
  DataRef private_settings(obs_sceneitem_get_private_settings(item));
  const bool private_settings_captured =
      private_settings.data &&
      capture_data_snapshot(private_settings.data, snapshot.private_settings);
  if (!private_settings_captured)
    return false;
  obs_source_t *source = obs_sceneitem_get_source(item);
  const bool is_live_input = input_bindings && std::any_of(
      input_bindings->begin(), input_bindings->end(),
      [source](const InputBinding &binding) {
        return source && binding.source_ref.get() == source;
      });
  return capture_source_snapshot(source, snapshot,
                                 include_source_semantics && !is_live_input &&
                                     (!only_semantic_source ||
                                      source == only_semantic_source));
}

struct SceneCapture {
  std::vector<SceneItemSnapshot> *items = nullptr;
  const std::vector<InputBinding> *input_bindings = nullptr;
  obs_source_t *only_semantic_source = nullptr;
  bool include_source_semantics = false;
  bool failed = false;
};

bool capture_scene_item_callback(obs_scene_t *, obs_sceneitem_t *item,
                                 void *data)
{
  auto &capture = *static_cast<SceneCapture *>(data);
  if (capture.failed)
    return false;
  try {
    SceneItemSnapshot snapshot;
    if (!capture_scene_item_snapshot(item, snapshot,
                                     capture.input_bindings,
                                     capture.include_source_semantics,
                                     capture.only_semantic_source)) {
      capture.failed = true;
      return false;
    }
    capture.items->push_back(std::move(snapshot));
    return true;
  } catch (...) {
    capture.failed = true;
    return false;
  }
}

bool capture_scene_snapshot(obs_scene_t *scene,
                            PrivateSceneSnapshot &snapshot,
                            const std::vector<InputBinding> *input_bindings,
                            bool include_source_semantics,
                            obs_source_t *only_semantic_source = nullptr)
{
  if (!scene)
    return false;
  snapshot.scene = scene;
  snapshot.source = SourceRef(obs_source_get_ref(obs_scene_get_source(scene)));
  if (!snapshot.source)
    return false;
  SceneCapture capture{&snapshot.items, input_bindings, only_semantic_source,
                       include_source_semantics};
  obs_scene_enum_items(scene, capture_scene_item_callback, &capture);
  return !capture.failed;
}

struct CapturePrivateSceneContext {
  obs_scene_t *expected_scene = nullptr;
  ActiveComposition *state = nullptr;
  std::vector<obs_scene_t *> *visited = nullptr;
  size_t depth = 0;
  bool succeeded = false;
};

bool capture_private_scene_tree(obs_scene_t *scene, ActiveComposition &state,
                                std::vector<obs_scene_t *> &visited,
                                size_t depth);

void capture_private_scene_callback(void *data, obs_scene_t *scene)
{
  auto &context = *static_cast<CapturePrivateSceneContext *>(data);
  try {
    if (!scene || scene != context.expected_scene || !context.state ||
        !context.visited || context.depth > max_nested_scene_depth ||
        std::find(context.visited->begin(), context.visited->end(), scene) !=
            context.visited->end())
      return;
    context.visited->push_back(scene);
    PrivateSceneSnapshot snapshot;
    if (!capture_scene_snapshot(scene, snapshot,
                                &context.state->input_bindings, true))
      return;
    const size_t index = context.state->private_scenes.size();
    context.state->private_scenes.push_back(std::move(snapshot));
    const auto &items = context.state->private_scenes[index].items;
    for (size_t item_index = 0; item_index < items.size(); ++item_index) {
      const auto &item_snapshot = items[item_index];
      if (item_snapshot.group) {
        if (!item_snapshot.group_scene ||
            !capture_private_scene_tree(item_snapshot.group_scene,
                                        *context.state, *context.visited,
                                        context.depth + 1))
          return;
        continue;
      }
      const auto world = std::find_if(
          context.state->world_affines.begin(),
          context.state->world_affines.end(),
          [&item_snapshot](const WorldAffineBinding &binding) {
            return binding.world_source.get() == item_snapshot.source.get();
          });
      if (world != context.state->world_affines.end()) {
        obs_scene_t *crop_scene =
            obs_scene_from_source(world->crop_scene_source.get());
        if (!crop_scene || !capture_private_scene_tree(
                               crop_scene, *context.state, *context.visited,
                               context.depth + 1))
          return;
      }
    }
    context.succeeded = true;
  } catch (...) {
    context.succeeded = false;
  }
}

bool capture_private_scene_tree(obs_scene_t *scene, ActiveComposition &state,
                                std::vector<obs_scene_t *> &visited,
                                size_t depth)
{
  if (!scene)
    return false;
  CapturePrivateSceneContext context{scene, &state, &visited, depth};
  // OBS group rendering holds the parent scene locks while descending into its
  // child scene. The recursive scene mutexes make this same order safe here.
  obs_scene_atomic_update(scene, capture_private_scene_callback, &context);
  return context.succeeded;
}

bool scene_item_transform_equal(const obs_transform_info &a,
                                const obs_transform_info &b)
{
  return a.pos.x == b.pos.x && a.pos.y == b.pos.y && a.rot == b.rot &&
         a.scale.x == b.scale.x && a.scale.y == b.scale.y &&
         a.alignment == b.alignment && a.bounds_type == b.bounds_type &&
         a.bounds_alignment == b.bounds_alignment &&
         a.bounds.x == b.bounds.x && a.bounds.y == b.bounds.y &&
         a.crop_to_bounds == b.crop_to_bounds;
}

bool scene_item_crop_equal(const obs_sceneitem_crop &a,
                           const obs_sceneitem_crop &b)
{
  return a.left == b.left && a.top == b.top &&
         a.right == b.right && a.bottom == b.bottom;
}

bool source_settings_match(obs_source_t *source,
                           const DataSnapshot &expected)
{
  obs_data_t *settings = source ? obs_source_get_settings(source) : nullptr;
  const bool matches = data_snapshot_matches(settings, expected);
  obs_data_release(settings);
  return matches;
}

bool data_settings_match(obs_data_t *settings, const DataSnapshot &expected)
{
  const bool matches = data_snapshot_matches(settings, expected);
  obs_data_release(settings);
  return matches;
}

struct FilterMatch {
  const std::vector<FilterSnapshot> *expected = nullptr;
  size_t index = 0;
  bool matches = true;
};

void match_filter_callback(obs_source_t *, obs_source_t *filter, void *data)
{
  auto &match = *static_cast<FilterMatch *>(data);
  if (!match.matches)
    return;
  if (!filter || match.index >= match.expected->size()) {
    match.matches = false;
    return;
  }
  const auto &expected = (*match.expected)[match.index];
  const char *id = obs_source_get_id(filter);
  const char *name = obs_source_get_name(filter);
  if (expected.source.get() != filter || !id || !name ||
      expected.source_id != id || expected.source_name != name ||
      expected.enabled != obs_source_enabled(filter) ||
      !source_settings_match(filter, expected.settings)) {
    match.matches = false;
    return;
  }
  ++match.index;
}

bool source_filters_match(obs_source_t *source,
                          const std::vector<FilterSnapshot> &expected)
{
  FilterMatch match{&expected};
  obs_source_enum_filters(source, match_filter_callback, &match);
  return match.matches && match.index == expected.size();
}

struct RootGroupSizeTransition {
  obs_sceneitem_t *item = nullptr;
  obs_source_t *source = nullptr;
  uint32_t width = 0;
  uint32_t height = 0;
  size_t snapshot_index = std::numeric_limits<size_t>::max();
};

bool scene_item_matches_snapshot(obs_scene_t *scene, obs_sceneitem_t *item,
                                 const SceneItemSnapshot &expected,
                                 uint32_t *failure_reason = nullptr,
                                 RootGroupSizeTransition *root_size = nullptr,
                                 size_t snapshot_index = 0)
{
  const auto fail = [failure_reason](uint32_t reason) {
    if (failure_reason)
      *failure_reason = reason;
    return false;
  };
  if (!item || item != expected.item ||
      obs_sceneitem_get_scene(item) != scene ||
      obs_sceneitem_get_id(item) != expected.id ||
      obs_sceneitem_get_order_position(item) != expected.order_position ||
      obs_sceneitem_is_group(item) != expected.group ||
      obs_sceneitem_get_source(item) != expected.source.get())
    return fail(1);
  if (expected.group &&
      obs_sceneitem_group_get_scene(item) != expected.group_scene)
    return fail(2);
  obs_transform_info transform{};
  obs_sceneitem_get_info2(item, &transform);
  obs_sceneitem_crop crop{};
  obs_sceneitem_get_crop(item, &crop);
  if (!scene_item_transform_equal(transform, expected.transform) ||
      !scene_item_crop_equal(crop, expected.crop) ||
      obs_sceneitem_visible(item) != expected.visible ||
      obs_sceneitem_get_blending_mode(item) != expected.blending_mode ||
      obs_sceneitem_get_blending_method(item) != expected.blending_method ||
      obs_sceneitem_get_scale_filter(item) != expected.scale_filter)
    return fail(3);
  if (!data_settings_match(obs_sceneitem_get_private_settings(item),
                           expected.private_settings))
    return fail(4);
  obs_source_t *source = expected.source.get();
  const char *source_id = obs_source_get_id(source);
  const char *source_name = obs_source_get_name(source);
  if (!source_id || !source_name || expected.source_id != source_id ||
      expected.source_name != source_name)
    return fail(5);
  const uint32_t actual_width = obs_source_get_width(source);
  const uint32_t actual_height = obs_source_get_height(source);
  if (expected.source_width != actual_width ||
      expected.source_height != actual_height) {
    const bool initial_root_size_settlement =
        root_size && expected.item == root_size->item &&
        expected.source.get() == root_size->source && expected.group &&
        expected.source_width == 0 && expected.source_height == 0 &&
        actual_width == root_size->width &&
        actual_height == root_size->height &&
        root_size->snapshot_index == std::numeric_limits<size_t>::max();
    if (!initial_root_size_settlement)
      return fail(6);
    root_size->snapshot_index = snapshot_index;
  }
  if (expected.source_enabled != obs_source_enabled(source))
    return fail(7);
  if (expected.compare_source_semantics &&
      (!source_settings_match(source, expected.source_settings) ||
       !source_filters_match(source, expected.filters)))
    return fail(8);
  return true;
}

struct SceneMatch {
  const PrivateSceneSnapshot *expected = nullptr;
  RootGroupSizeTransition *root_size = nullptr;
  size_t index = 0;
  bool matches = true;
  uint32_t failure_reason = 0;
};

bool match_scene_item_callback(obs_scene_t *scene, obs_sceneitem_t *item,
                               void *data)
{
  auto &match = *static_cast<SceneMatch *>(data);
  if (!match.matches)
    return false;
  if (match.index >= match.expected->items.size()) {
    match.matches = false;
    match.failure_reason = 32;
    return false;
  }
  uint32_t item_failure_reason = 0;
  if (!scene_item_matches_snapshot(
          scene, item, match.expected->items[match.index],
          &item_failure_reason, match.root_size, match.index)) {
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    if (item_failure_reason == 6) {
      const auto &expected = match.expected->items[match.index];
      obs_source_t *actual_source = obs_sceneitem_get_source(item);
      blog(LOG_INFO,
           "PULSAR_GROUP_GEOMETRY_SIZE_MISMATCH index=%zu item=%lld source=%s expected=%ux%u actual=%ux%u group=%d",
           match.index, static_cast<long long>(expected.id),
           expected.source_id.c_str(), expected.source_width,
           expected.source_height,
           actual_source ? obs_source_get_width(actual_source) : 0,
           actual_source ? obs_source_get_height(actual_source) : 0,
           expected.group ? 1 : 0);
    }
#endif
    match.matches = false;
    match.failure_reason = static_cast<uint32_t>(100 + match.index * 10) +
                           item_failure_reason;
    return false;
  }
  ++match.index;
  return true;
}

bool scene_snapshot_matches(obs_scene_t *scene,
                            const PrivateSceneSnapshot &expected,
                            uint32_t *failure_reason = nullptr,
                            RootGroupSizeTransition *root_size = nullptr)
{
  if (failure_reason)
    *failure_reason = 0;
  if (!scene || scene != expected.scene ||
      obs_scene_get_source(scene) != expected.source.get()) {
    if (failure_reason)
      *failure_reason = 31;
    return false;
  }
  SceneMatch match{&expected, root_size};
  obs_scene_enum_items(scene, match_scene_item_callback, &match);
  if (!match.matches) {
    if (failure_reason)
      *failure_reason = match.failure_reason ? match.failure_reason : 31;
    return false;
  }
  if (match.index != expected.items.size()) {
    if (failure_reason)
      *failure_reason = 32;
    return false;
  }
  return true;
}

bool make_input_binding(int64_t id, const ResolvedCapture &capture,
                        InputBinding &binding)
{
  if (!capture.item || obs_sceneitem_get_id(capture.item) != id)
    return false;
  obs_source_t *source = obs_sceneitem_get_source(capture.item);
  const char *source_id = source ? obs_source_get_id(source) : nullptr;
  const char *source_name = source ? obs_source_get_name(source) : nullptr;
  if (!source || !source_id || !source_name)
    return false;
  binding.id = id;
  binding.item = capture.item;
  binding.source = source;
  binding.source_ref = SourceRef(obs_source_get_ref(source));
  if (!binding.source_ref)
    return false;
  binding.source_id = source_id;
  binding.source_name = source_name;
  binding.width = obs_source_get_width(source);
  binding.height = obs_source_get_height(source);
  binding.visible = capture.visible;
  binding.source_enabled = obs_source_enabled(source);
  binding.has_source = true;
  binding.scale_filter = obs_sceneitem_get_scale_filter(capture.item);
  return true;
}

void capture_input_bindings(
    const std::unordered_map<int64_t, ResolvedCapture> &captures,
    std::vector<InputBinding> &bindings)
{
  bindings.reserve(captures.size());
  for (const auto &[id, capture] : captures) {
    InputBinding binding;
    if (!make_input_binding(id, capture, binding))
      throw std::runtime_error("composition-input-binding-snapshot-failed");
    bindings.push_back(std::move(binding));
  }
}

bool input_bindings_equal(
    const std::vector<InputBinding> &active,
    const std::unordered_map<int64_t, ResolvedCapture> &captures)
{
  if (active.size() != captures.size())
    return false;
  for (const auto &binding : active) {
    const auto found = captures.find(binding.id);
    if (found == captures.end())
      return false;
    const auto &capture = found->second;
    obs_source_t *source = capture.item
                               ? obs_sceneitem_get_source(capture.item)
                               : nullptr;
    const char *source_id = source ? obs_source_get_id(source) : nullptr;
    const char *source_name = source ? obs_source_get_name(source) : nullptr;
    if (capture.item != binding.item || source != binding.source ||
        !binding.has_source || capture.width != binding.width ||
        capture.height != binding.height || capture.visible != binding.visible ||
        capture.scale_filter != binding.scale_filter ||
        source != binding.source_ref.get() || !source_id || !source_name ||
        binding.source_id != source_id || binding.source_name != source_name ||
        binding.source_enabled != obs_source_enabled(source))
      return false;
  }
  return true;
}

struct CaptureHostGeometryContext {
  ActiveComposition *state = nullptr;
  obs_scene_t *expected_scene = nullptr;
  std::vector<obs_scene_t *> *visited = nullptr;
  bool succeeded = false;
};

bool affine_equal(const Affine &a, const Affine &b);
bool affine_linear_equal(const Affine &a, const Affine &b);

void capture_host_geometry_callback(void *data, obs_scene_t *scene)
{
  auto &context = *static_cast<CaptureHostGeometryContext *>(data);
  try {
    if (!scene || scene != context.expected_scene || !context.state ||
        !context.visited || !context.state->root)
      return;
    if (!capture_scene_snapshot(scene, context.state->host_scene_snapshot,
                                &context.state->input_bindings, true,
                                obs_sceneitem_get_source(context.state->root)))
      return;
    obs_scene_t *root_inner =
        obs_sceneitem_group_get_scene(context.state->root);
    if (!root_inner || !capture_private_scene_tree(
                           root_inner, *context.state, *context.visited, 0))
      return;
    context.succeeded = true;
  } catch (...) {
    context.succeeded = false;
  }
}

bool capture_geometry_snapshots(ActiveComposition &state)
{
  const auto clear_snapshots = [&state]() {
    state.private_scenes.clear();
    state.host_scene_snapshot = PrivateSceneSnapshot();
    state.geometry_snapshot_ready = false;
  };
  try {
    if (!state.root || state.world_affines.empty())
      return false;
    obs_scene_t *host_scene = obs_sceneitem_get_scene(state.root);
    if (!host_scene)
      return false;
    size_t nested_group_refs = 0;
    size_t capture_children = 0;
    for (const auto &group : state.plan_snapshot.groups) {
      for (const auto &child : group.children) {
        if (child.kind == ChildKind::Group)
          ++nested_group_refs;
        else if (child.kind == ChildKind::Capture)
          ++capture_children;
      }
    }
    const size_t minimum_scenes = state.world_affines.size() +
                                  state.plan_snapshot.groups.size();
    const size_t maximum_scenes = minimum_scenes + nested_group_refs +
                                  capture_children;
    state.private_scenes.clear();
    state.host_scene_snapshot = PrivateSceneSnapshot();
    state.private_scenes.reserve(maximum_scenes);
    std::vector<obs_scene_t *> visited;
    visited.reserve(maximum_scenes + 1);
    visited.push_back(host_scene);
    CaptureHostGeometryContext context{&state, host_scene, &visited};
    // Capture one coherent snapshot while holding the same parent-to-child
    // recursive scene lock chain used by OBS when it renders nested groups.
    obs_scene_atomic_update(host_scene, capture_host_geometry_callback,
                            &context);
    if (!context.succeeded || state.private_scenes.size() < minimum_scenes ||
        state.private_scenes.size() > maximum_scenes) {
      clear_snapshots();
      return false;
    }
    for (const auto &binding : state.world_affines) {
      const auto group_it = state.plan_snapshot.by_id.find(binding.group_id);
      if (group_it == state.plan_snapshot.by_id.end()) {
        clear_snapshots();
        return false;
      }
      const auto &group = state.plan_snapshot.groups[group_it->second];
      if (binding.child_index >= group.children.size()) {
        clear_snapshots();
        return false;
      }
      const auto &child = group.children[binding.child_index];
      if (child.kind != ChildKind::Capture ||
          child.scene_item_id != binding.scene_item_id ||
          !affine_equal(child.affine, binding.affine) ||
          !group_world_source_matches(binding.world_source.get(),
                                      binding.crop_scene_source.get(),
                                      binding.affine))
      {
        clear_snapshots();
        return false;
      }
      size_t world_items = 0;
      size_t crop_scenes = 0;
      for (const auto &snapshot : state.private_scenes) {
        if (snapshot.source.get() == binding.crop_scene_source.get())
          ++crop_scenes;
        for (const auto &item : snapshot.items)
          if (item.source.get() == binding.world_source.get())
            ++world_items;
      }
      if (world_items != 1 || crop_scenes != 1) {
        clear_snapshots();
        return false;
      }
    }
    return !state.private_scenes.empty();
  } catch (...) {
    clear_snapshots();
    return false;
  }
}

bool scene_bindings_match(obs_scene_t *scene,
                          const ActiveComposition &active,
                          uint32_t &failure_reason,
                          RootGroupSizeTransition &root_size)
{
  failure_reason = 0;
  const auto root_it = active.plan_snapshot.by_id.find(root_id);
  if (root_it == active.plan_snapshot.by_id.end()) {
    failure_reason = 33;
    return false;
  }
  const Rect &root_bounds =
      active.plan_snapshot.groups[root_it->second].bounds;
  if (std::isfinite(root_bounds.width) && std::isfinite(root_bounds.height) &&
      root_bounds.width > 0 && root_bounds.height > 0 &&
      root_bounds.width <= std::numeric_limits<uint32_t>::max() &&
      root_bounds.height <= std::numeric_limits<uint32_t>::max() &&
      std::floor(root_bounds.width) == root_bounds.width &&
      std::floor(root_bounds.height) == root_bounds.height) {
    root_size.item = active.root;
    root_size.source = obs_sceneitem_get_source(active.root);
    root_size.width = static_cast<uint32_t>(root_bounds.width);
    root_size.height = static_cast<uint32_t>(root_bounds.height);
  }
  uint32_t host_snapshot_failure = 0;
  if (!scene_snapshot_matches(scene, active.host_scene_snapshot,
                              &host_snapshot_failure, &root_size)) {
    failure_reason = host_snapshot_failure ? host_snapshot_failure : 31;
    return false;
  }
  for (size_t index = 0; index < active.input_bindings.size(); ++index) {
    const auto &binding = active.input_bindings[index];
    obs_sceneitem_t *item =
        obs_scene_find_sceneitem_by_id(scene, binding.id);
    const char *source_id =
        binding.source_ref ? obs_source_get_id(binding.source_ref.get())
                           : nullptr;
    const char *source_name =
        binding.source_ref ? obs_source_get_name(binding.source_ref.get())
                           : nullptr;
    if (item != binding.item || !source_id || !source_name ||
        obs_sceneitem_get_source(item) != binding.source_ref.get() ||
        binding.source_id != source_id || binding.source_name != source_name ||
        binding.width != obs_source_get_width(binding.source_ref.get()) ||
        binding.height != obs_source_get_height(binding.source_ref.get()) ||
        binding.source_enabled != obs_source_enabled(binding.source_ref.get())) {
      failure_reason = 32;
      return false;
    }
  }
  return true;
}

bool build_geometry_translation_updates(
    const ActiveComposition &active, const Plan &next,
    std::vector<GroupWorldSourceTranslationUpdate> &updates)
{
  size_t capture_count = 0;
  for (const auto &group : next.groups)
    capture_count += static_cast<size_t>(std::count_if(
        group.children.begin(), group.children.end(), [](const Child &child) {
          return child.kind == ChildKind::Capture;
        }));
  if (capture_count != active.world_affines.size())
    return false;
  updates.clear();
  updates.reserve(active.world_affines.size());
  for (const auto &binding : active.world_affines) {
    const auto old_group_it = active.plan_snapshot.by_id.find(binding.group_id);
    const auto next_group_it = next.by_id.find(binding.group_id);
    if (old_group_it == active.plan_snapshot.by_id.end() ||
        next_group_it == next.by_id.end())
      return false;
    const auto &old_group = active.plan_snapshot.groups[old_group_it->second];
    const auto &next_group = next.groups[next_group_it->second];
    if (binding.child_index >= old_group.children.size() ||
        binding.child_index >= next_group.children.size())
      return false;
    const auto &old_child = old_group.children[binding.child_index];
    const auto &next_child = next_group.children[binding.child_index];
    if (old_child.kind != ChildKind::Capture ||
        next_child.kind != ChildKind::Capture ||
        old_child.scene_item_id != binding.scene_item_id ||
        next_child.scene_item_id != binding.scene_item_id ||
        !affine_equal(old_child.affine, binding.affine) ||
        !affine_linear_equal(old_child.affine, next_child.affine) ||
        !group_world_source_matches(binding.world_source.get(),
                                    binding.crop_scene_source.get(),
                                    binding.affine))
      return false;
    updates.push_back({binding.world_source.get(),
                       binding.crop_scene_source.get(), binding.affine,
                       next_child.affine});
  }
  return !updates.empty();
}

struct RetainedGeometryTransaction {
  ActiveComposition *active = nullptr;
  const std::vector<GroupWorldSourceTranslationUpdate> *updates = nullptr;
  RootGroupSizeTransition root_size;
  size_t next_scene_index = 0;
  bool valid = true;
  bool applied = false;
  uint32_t failure_reason = 0;
};

void update_retained_geometry_callback(void *data, obs_scene_t *scene)
{
  auto &transaction = *static_cast<RetainedGeometryTransaction *>(data);
  if (!transaction.valid || transaction.applied || !transaction.active ||
      !transaction.updates)
    return;

  auto &active = *transaction.active;
  if (transaction.next_scene_index == 0) {
    uint32_t failure_reason = 0;
    if (!scene_bindings_match(scene, active, failure_reason,
                              transaction.root_size)) {
      transaction.valid = false;
      transaction.failure_reason = failure_reason ? failure_reason : 3;
      return;
    }
  } else {
    const size_t private_index = transaction.next_scene_index - 1;
    if (private_index >= active.private_scenes.size() ||
        !scene_snapshot_matches(
            scene, active.private_scenes[private_index])) {
      transaction.valid = false;
      transaction.failure_reason = 4;
      return;
    }
  }
  ++transaction.next_scene_index;
  if (transaction.next_scene_index <= active.private_scenes.size()) {
    obs_scene_t *nested = active.private_scenes[
        transaction.next_scene_index - 1].scene;
    if (!nested) {
      transaction.valid = false;
      transaction.failure_reason = 5;
      return;
    }
    // Keep each parent lock held as the next child scene is acquired. This
    // follows OBS's parent-to-child render traversal and prevents a frame from
    // observing only part of the world-source matrix batch.
    obs_scene_atomic_update(nested, update_retained_geometry_callback,
                            &transaction);
    return;
  }
  if (transaction.next_scene_index != active.private_scenes.size() + 1) {
    transaction.valid = false;
    transaction.failure_reason = 7;
    return;
  }
  SceneItemSnapshot *root_size_snapshot = nullptr;
  if (transaction.root_size.snapshot_index !=
      std::numeric_limits<size_t>::max()) {
    auto &items = active.host_scene_snapshot.items;
    const size_t index = transaction.root_size.snapshot_index;
    if (index >= items.size() || items[index].item != transaction.root_size.item ||
        items[index].source.get() != transaction.root_size.source ||
        items[index].source_width != 0 || items[index].source_height != 0 ||
        obs_source_get_width(transaction.root_size.source) !=
            transaction.root_size.width ||
        obs_source_get_height(transaction.root_size.source) !=
            transaction.root_size.height) {
      transaction.valid = false;
      transaction.failure_reason = 34;
      return;
    }
    root_size_snapshot = &items[index];
  }
  transaction.applied = group_world_source_update_translations(
      transaction.updates->data(), transaction.updates->size());
  if (!transaction.applied) {
    transaction.valid = false;
    transaction.failure_reason = 6;
  } else if (root_size_snapshot) {
    root_size_snapshot->source_width = transaction.root_size.width;
    root_size_snapshot->source_height = transaction.root_size.height;
  }
}

bool try_retained_geometry_update(
    ActiveComposition &active, const Plan &next,
    std::vector<GroupWorldSourceTranslationUpdate> &updates,
    uint32_t &failure_reason)
{
  failure_reason = 0;
  if (!active.geometry_snapshot_ready) {
    failure_reason = 1;
    return false;
  }
  if (!build_geometry_translation_updates(active, next, updates)) {
    failure_reason = 2;
    return false;
  }
  RetainedGeometryTransaction transaction;
  transaction.active = &active;
  transaction.updates = &updates;
  obs_scene_atomic_update(active.host_scene_snapshot.scene,
                          update_retained_geometry_callback, &transaction);
  if (!transaction.valid || !transaction.applied) {
    failure_reason = transaction.failure_reason
                         ? transaction.failure_reason
                         : 8;
    return false;
  }
  return true;
}

bool affine_equal(const Affine &a, const Affine &b)
{
  return a.m00 == b.m00 && a.m01 == b.m01 && a.tx == b.tx &&
         a.m10 == b.m10 && a.m11 == b.m11 && a.ty == b.ty;
}

bool affine_linear_equal(const Affine &a, const Affine &b)
{
  return a.m00 == b.m00 && a.m01 == b.m01 &&
         a.m10 == b.m10 && a.m11 == b.m11;
}

bool rect_equal(const Rect &a, const Rect &b)
{
  return a.x == b.x && a.y == b.y && a.width == b.width &&
         a.height == b.height;
}

bool crop_equal(const Crop &a, const Crop &b)
{
  return a.left == b.left && a.top == b.top &&
         a.right == b.right && a.bottom == b.bottom;
}

bool mask_equal(const Mask &a, const Mask &b)
{
  return a.scene_item_id == b.scene_item_id && crop_equal(a.crop, b.crop) &&
         rect_equal(a.rect, b.rect) && a.is_null == b.is_null;
}

bool child_equal_except_capture_translation(const Child &a, const Child &b,
                                             bool &translation_changed)
{
  if (a.kind != b.kind || a.scene_item_id != b.scene_item_id ||
      a.group_id != b.group_id || a.band != b.band ||
      a.local_opacity != b.local_opacity || a.blend_mode != b.blend_mode ||
      a.blend_mode_id != b.blend_mode_id || !crop_equal(a.crop, b.crop) ||
      !rect_equal(a.rect, b.rect) || !mask_equal(a.mask, b.mask) ||
      a.has_visible != b.has_visible || a.visible != b.visible)
    return false;
  if (a.kind == ChildKind::Capture) {
    if (!affine_linear_equal(a.affine, b.affine))
      return false;
    translation_changed = translation_changed || a.affine.tx != b.affine.tx ||
                          a.affine.ty != b.affine.ty;
    return true;
  }
  return affine_equal(a.affine, b.affine);
}

bool group_equal_except_capture_translation(const Group &a, const Group &b,
                                            bool &translation_changed)
{
  if (a.id != b.id || a.parent_id != b.parent_id ||
      a.parent_is_null != b.parent_is_null || a.order != b.order ||
      a.opacity != b.opacity || a.visible != b.visible ||
      a.blend_mode != b.blend_mode || a.blend_mode_id != b.blend_mode_id ||
      !rect_equal(a.bounds, b.bounds) || !affine_equal(a.affine, b.affine) ||
      a.mask_is_null != b.mask_is_null || !mask_equal(a.mask, b.mask) ||
      a.children.size() != b.children.size())
    return false;
  for (size_t index = 0; index < a.children.size(); ++index)
    if (!child_equal_except_capture_translation(a.children[index], b.children[index],
                                                translation_changed))
      return false;
  return true;
}

bool plans_equal_except_capture_translation(const Plan &active,
                                            const Plan &next)
{
  if (active.protocol_version != 2 || next.protocol_version != 2 ||
      active.coordinate_space != CoordinateSpace::GlobalSceneTexels ||
      next.coordinate_space != CoordinateSpace::GlobalSceneTexels ||
      active.coordinate_space != next.coordinate_space ||
      active.scene_name != next.scene_name ||
      active.runtime_instance_id != next.runtime_instance_id ||
      active.replaced_scene_item_ids != next.replaced_scene_item_ids ||
      active.child_count != next.child_count ||
      active.groups.size() != next.groups.size() ||
      active.by_id != next.by_id || active.generation >= next.generation ||
      active.plan_hash == next.plan_hash)
    return false;
  bool translation_changed = false;
  for (size_t index = 0; index < active.groups.size(); ++index)
    if (!group_equal_except_capture_translation(active.groups[index],
                                                next.groups[index],
                                                translation_changed))
      return false;
  return translation_changed;
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
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  if (active_filter_creation_count)
    ++*active_filter_creation_count;
#endif
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
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  if (active_filter_creation_count)
    ++*active_filter_creation_count;
#endif
  if (!capture_effects_bind_background_parent(
          filter, obs_scene_get_source(parent_scene))) {
    obs_source_release(filter);
    set_error(response, "composition-filter-parent-bind-failed");
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
                       bool version_two, const Group &group,
                       size_t child_index,
                       std::vector<WorldAffineBinding> &world_affines,
                       obs_data_t *response)
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
  const bool added = version_two
                         ? add_global_capture_contribution(scene, world.get(),
                                                           world_child,
                                                           contribution, viewport,
                                                           mask_source, response)
                         : add_capture(scene, world_child, contribution,
                                       response, true);
  if (!added)
    return false;
  WorldAffineBinding binding;
  binding.group_id = group.id;
  binding.child_index = child_index;
  binding.scene_item_id = child.scene_item_id;
  binding.world_source = std::move(world);
  binding.crop_scene_source = std::move(crop_source);
  binding.affine = child.affine;
  world_affines.push_back(std::move(binding));
  return true;
}

bool build_group(Plan &plan, const Group &group, obs_scene_t *parent_scene,
                 const std::unordered_map<int64_t, ResolvedCapture> &captures,
                 obs_sceneitem_t **out,
                 std::vector<WorldAffineBinding> &world_affines,
                 obs_data_t *response)
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

  for (size_t child_index = 0; child_index < group.children.size();
       ++child_index) {
    const auto &child = group.children[child_index];
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
                                      plan.protocol_version == 2, group,
                                      child_index, world_affines, response)
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
                       &nested, world_affines, response))
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
  state.host_scene_snapshot = PrivateSceneSnapshot();
  state.private_scenes.clear();
  state.world_affines.clear();
  state.input_bindings.clear();
  state.plan_snapshot = Plan();
  state.geometry_snapshot_ready = false;
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
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  CommitTiming *timing = nullptr;
#endif
  ~CandidateGuard()
  {
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    const auto retirement_started = CommitClock::now();
#endif
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
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    if (timing)
      timing->retirement_ms = elapsed_ms(retirement_started, CommitClock::now());
#endif
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
    resolved.scale_filter = obs_sceneitem_get_scale_filter(item);
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
    resolved.source = obs_sceneitem_get_source(item);
    if (resolved.source) {
      resolved.width = obs_source_get_width(resolved.source);
      resolved.height = obs_source_get_height(resolved.source);
    }
    resolved.visible = obs_sceneitem_visible(item);
    resolved.scale_filter = obs_sceneitem_get_scale_filter(item);
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
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  CommitTiming timing;
#endif
};

void commit_graphics(void *data)
{
  auto &commit = *static_cast<Commit *>(data);
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  commit.timing.graphics_started = CommitClock::now();
  struct FinishTiming {
    CommitTiming &timing;
    ~FinishTiming() { timing.graphics_finished = CommitClock::now(); }
  } finish_timing{commit.timing};
#endif
  try {
    Plan plan;
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    auto phase_started = CommitClock::now();
#endif
    const bool parsed = parse_plan(commit.request, plan, commit.response);
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    commit.timing.parse_ms = elapsed_ms(phase_started, CommitClock::now());
#endif
    if (!parsed)
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
          plan.plan_hash == previous->plan_snapshot.plan_hash &&
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
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    candidate.timing = &commit.timing;
#endif
    candidate.state.protocol_version = plan.protocol_version;
    candidate.state.coordinate_space = plan.coordinate_space;
    candidate.state.scene_uuid = scene_uuid;
    candidate.state.runtime_instance_id = plan.runtime_instance_id;
    candidate.state.generation = plan.generation;
    candidate.state.replaced_scene_item_ids = plan.replaced_scene_item_ids;
    std::unordered_map<int64_t, ResolvedCapture> captures;
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    phase_started = CommitClock::now();
#endif
    const bool resolved = resolve_captures(plan, scene, source, previous,
                                           candidate.state.dependency_sources,
                                           captures, commit.response);
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    commit.timing.resolve_ms = elapsed_ms(phase_started, CommitClock::now());
#endif
    if (!resolved) {
      return;
    }
    const bool geometry_translation_plan =
        previous && previous->generation == last &&
        previous->scene_uuid == scene_uuid &&
        plans_equal_except_capture_translation(previous->plan_snapshot, plan);
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    commit.timing.geometry_snapshot_ready =
        previous && previous->geometry_snapshot_ready;
    commit.timing.geometry_translation_plan = geometry_translation_plan;
#endif
    if (geometry_translation_plan && previous->geometry_snapshot_ready) {
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
      const auto geometry_started = CommitClock::now();
#endif
      const bool bindings_match =
          input_bindings_equal(previous->input_bindings, captures);
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
      commit.timing.geometry_input_bindings_match = bindings_match;
#endif
      uint32_t geometry_failure_reason = 0;
      const bool retained_updated =
          bindings_match && try_retained_geometry_update(
                                *previous, plan, previous->geometry_updates,
                                geometry_failure_reason);
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
      commit.timing.geometry_prevalidation_success = retained_updated;
      commit.timing.geometry_prevalidation_failure =
          retained_updated ? 0 : (bindings_match ? geometry_failure_reason : 9);
#endif
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
      commit.timing.geometry_update_ms =
          elapsed_ms(geometry_started, CommitClock::now());
#endif
      if (retained_updated) {
        const uint64_t next_generation = plan.generation;
        obs_data_set_bool(commit.response, "ok", true);
        obs_data_set_int(commit.response, "generation",
                         static_cast<long long>(next_generation));
        obs_data_set_string(commit.response, "planHash", plan.plan_hash.c_str());
        for (size_t index = 0; index < previous->world_affines.size(); ++index)
          previous->world_affines[index].affine =
              previous->geometry_updates[index].replacement;
        // Keep the validated plan, including its hash, as the single active
        // metadata authority; the local plan then owns the old snapshot.
        swap_plan(previous->plan_snapshot, plan);
        previous->generation = next_generation;
        fence_it->second.generation = next_generation;
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
        commit.timing.geometry_fast_path = true;
#endif
        return;
      }
    }
    candidate.state.plan_snapshot = plan;
    if (plan.protocol_version == 2 &&
        plan.coordinate_space == CoordinateSpace::GlobalSceneTexels) {
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
      phase_started = CommitClock::now();
#endif
      try {
        capture_input_bindings(captures, candidate.state.input_bindings);
      } catch (...) {
        candidate.state.input_bindings.clear();
      }
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
      commit.timing.input_binding_snapshot_ms =
          elapsed_ms(phase_started, CommitClock::now());
#endif
    }
    if (!copy_original_visibility(plan, scene, previous, candidate.state,
                                  captures, commit.response))
      return;
    const Group &root = plan.groups[plan.by_id.at(root_id)];
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    phase_started = CommitClock::now();
#endif
    bool built = false;
    {
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
      CountFilterCreations count_filters(&commit.timing.filter_sources_created,
                                         &commit.timing.shader_creation);
#endif
      built = build_group(plan, root, scene, captures, &candidate.state.root,
                          candidate.state.world_affines, commit.response);
    }
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    commit.timing.build_ms = elapsed_ms(phase_started, CommitClock::now());
#endif
    if (!built || !candidate.state.root) {
      if (!obs_data_has_user_value(commit.response, "error"))
        set_error(commit.response, "candidate-build-failed");
      return;
    }
    candidate.state.geometry_updates.reserve(
        candidate.state.world_affines.size());

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
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    phase_started = CommitClock::now();
#endif
    obs_scene_atomic_update(scene, swap_visibility, &swap);
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
    commit.timing.atomic_swap_ms = elapsed_ms(phase_started, CommitClock::now());
#endif
    slot->swap(candidate.state);
    fence_it->second.generation = plan.generation;
    slot_reservation.committed = true;
    fence_reservation.committed = true;
    if (candidate.state.root) {
      remove_root(candidate.state.root, candidate.state.generation);
      candidate.state.root = nullptr;
    }
    if (!slot->input_bindings.empty() && slot->protocol_version == 2 &&
        slot->coordinate_space == CoordinateSpace::GlobalSceneTexels) {
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
      phase_started = CommitClock::now();
#endif
      slot->geometry_snapshot_ready = capture_geometry_snapshots(*slot);
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
      commit.timing.geometry_snapshot_ms =
          elapsed_ms(phase_started, CommitClock::now());
#endif
    }
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
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  commit.reset_trace_id = next_reset_trace_id();
  commit.timing.handler_started = CommitClock::now();
  commit.timing.queue_started = CommitClock::now();
#endif
  obs_queue_task(OBS_TASK_GRAPHICS, commit_graphics, &commit, true);
#if defined(PULSAR_GROUP_COMPOSITION_RESET_TRACE)
  const auto handler_finished = CommitClock::now();
  auto *timing = obs_data_create();
  obs_data_set_int(timing, "traceId", static_cast<long long>(commit.reset_trace_id));
  obs_data_set_double(timing, "queueWaitMs", elapsed_ms(
      commit.timing.queue_started, commit.timing.graphics_started));
  obs_data_set_double(timing, "graphicsCallbackMs", elapsed_ms(
      commit.timing.graphics_started, commit.timing.graphics_finished));
  obs_data_set_double(timing, "parseMs", commit.timing.parse_ms);
  obs_data_set_double(timing, "resolveCapturesMs", commit.timing.resolve_ms);
  obs_data_set_double(timing, "buildGroupMs", commit.timing.build_ms);
  obs_data_set_double(timing, "inputBindingSnapshotMs",
                      commit.timing.input_binding_snapshot_ms);
  obs_data_set_double(timing, "geometrySnapshotMs",
                      commit.timing.geometry_snapshot_ms);
  obs_data_set_double(timing, "geometryUpdateMs",
                      commit.timing.geometry_update_ms);
  obs_data_set_bool(timing, "geometrySnapshotReady",
                    commit.timing.geometry_snapshot_ready);
  obs_data_set_bool(timing, "geometryTranslationPlan",
                    commit.timing.geometry_translation_plan);
  obs_data_set_bool(timing, "geometryInputBindingsMatch",
                    commit.timing.geometry_input_bindings_match);
  obs_data_set_bool(timing, "geometryPrevalidationSuccess",
                    commit.timing.geometry_prevalidation_success);
  obs_data_set_int(timing, "geometryPrevalidationFailure",
                   commit.timing.geometry_prevalidation_failure);
  obs_data_set_bool(timing, "geometryFastPath",
                    commit.timing.geometry_fast_path);
  obs_data_set_double(timing, "atomicSwapMs", commit.timing.atomic_swap_ms);
  obs_data_set_double(timing, "retirePreviousMs", commit.timing.retirement_ms);
  obs_data_set_double(timing, "handlerTotalMs", elapsed_ms(
      commit.timing.handler_started, handler_finished));
  obs_data_set_int(timing, "filterSourcesCreated",
                   commit.timing.filter_sources_created);
  obs_data_set_double(timing, "shaderCreateMs",
                      commit.timing.shader_creation.milliseconds);
  obs_data_set_int(timing, "shaderCreateCalls",
                   commit.timing.shader_creation.calls);
  obs_data_set_obj(response, "debugCommitTiming", timing);
  obs_data_release(timing);
#endif
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
