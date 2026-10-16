#pragma once

#include <cstdint>
#include <obs-data.h>
#include <string>
#include <unordered_map>
#include <vector>

namespace group_composition {

inline constexpr char root_id[] = "prism.native-composition.root.v1";
inline constexpr size_t max_nested_scene_sources = 4096;
inline constexpr size_t max_nested_scene_depth = 64;

struct Affine {
  double m00 = 1, m01 = 0, tx = 0, m10 = 0, m11 = 1, ty = 0;
};
struct Rect {
  double x = 0, y = 0, width = 0, height = 0;
};
struct Crop {
  int left = 0, top = 0, right = 0, bottom = 0;
};
enum class ChildKind { Capture, Group, SolarBand };
enum class CoordinateSpace { OwnerLocal, GlobalSceneTexels };
struct Child {
  ChildKind kind = ChildKind::Capture;
  int64_t scene_item_id = 0;
  std::string group_id;
  int64_t band = -1;
  Affine affine;
  Crop crop;
  Rect rect;
  double local_opacity = 1;
  bool has_visible = false;
  bool visible = true;
};
struct Group {
  std::string id;
  std::string parent_id;
  bool parent_is_null = false;
  int64_t order = 0;
  double opacity = 1;
  bool visible = true;
  std::string blend_mode;
  Rect bounds;
  Affine affine;
  bool mask_is_null = false;
  std::vector<Child> children;
};
struct Plan {
  CoordinateSpace coordinate_space = CoordinateSpace::OwnerLocal;
  std::string scene_name;
  std::string plan_hash;
  std::string runtime_instance_id;
  uint64_t generation = 0;
  std::vector<Group> groups;
  std::vector<int64_t> replaced_scene_item_ids;
  std::unordered_map<std::string, size_t> by_id;
  size_t child_count = 0;
};

void set_error(obs_data_t *response, const char *code,
               const char *detail = nullptr);
bool read_string(obs_data_t *data, const char *key, std::string &out,
                 obs_data_t *response, size_t max_length = 512);
bool read_integer(obs_data_t *data, const char *key, int64_t &out,
                  obs_data_t *response);
bool parse_plan(obs_data_t *request, Plan &plan, obs_data_t *response);
bool validate_group_affine(const Affine &a, obs_data_t *response);
bool validate_world_affine(const Affine &a, obs_data_t *response);

} // namespace group_composition
