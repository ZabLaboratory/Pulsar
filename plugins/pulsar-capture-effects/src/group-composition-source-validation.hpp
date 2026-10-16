#pragma once

#include <obs.h>
#include <obs-data.h>
#include <unordered_set>
#include <vector>

namespace group_composition_detail {

class SourceRef {
public:
  obs_source_t *value = nullptr;

  SourceRef() = default;
  explicit SourceRef(obs_source_t *source) : value(source) {}
  SourceRef(const SourceRef &) = delete;
  SourceRef &operator=(const SourceRef &) = delete;
  SourceRef(SourceRef &&other) noexcept : value(other.value)
  {
    other.value = nullptr;
  }
  SourceRef &operator=(SourceRef &&other) noexcept
  {
    if (this == &other)
      return *this;
    if (value)
      obs_source_release(value);
    value = other.value;
    other.value = nullptr;
    return *this;
  }
  ~SourceRef()
  {
    if (value)
      obs_source_release(value);
  }

  explicit operator bool() const { return value != nullptr; }
  obs_source_t *get() const { return value; }
};

bool validate_capture_source(obs_source_t *source, obs_source_t *target,
                             std::unordered_set<obs_source_t *> &dependency_set,
                             std::vector<SourceRef> &dependencies,
                             obs_data_t *response);

} // namespace group_composition_detail
