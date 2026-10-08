#pragma once

#include <obs.h>
#include <nlohmann/json.hpp>
#include <string>

namespace pulsar_transition {

struct MediaConfig {
    std::string path;
    uint32_t cut_point_ms = 0;
    float volume = 1.0f;
    bool muted = false;
};

// Pure wire validation; file/decoder checks
// belong to MediaTransition. No filename or artwork is built into the feature.
bool parse_media_config(const nlohmann::json &value, MediaConfig &out, std::string &error);

// Called only under the frontend lane mutex. Owns a private OBS stinger and
// retains its private media child. Never scales the surrounding scene audio.
class MediaTransition {
public:
    ~MediaTransition();
    MediaTransition() = default;
    MediaTransition(const MediaTransition &) = delete;
    MediaTransition &operator=(const MediaTransition &) = delete;

    bool configure(const nlohmann::json &value, std::string &error);
    void clear();
    bool configured() const { return source_ != nullptr; }
    nlohmann::json state() const;
    void preload_tick(); // video thread only; never starts playback or audio
    obs_source_t *prepare(bool preview, uint32_t &duration_ms, std::string &error);
    void finish();

private:
    obs_source_t *media() const;
    uint64_t media_duration_ns() const;
    std::string readiness_error() const;
    MediaConfig config_;
    obs_source_t *source_ = nullptr;
    mutable obs_source_t *media_ = nullptr; // retained after the deferred source update
    bool prepared_ = false;
};

} // namespace pulsar_transition
