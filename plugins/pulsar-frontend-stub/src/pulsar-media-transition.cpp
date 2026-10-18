#include "pulsar-media-transition.h"
#include <obs.hpp>
#include <array>
#include <algorithm>
#include <cctype>
#include <cstring>
#include <filesystem>
#include <fstream>

namespace pulsar_transition {
using json = nlohmann::json;

MediaTransition::~MediaTransition() { clear(); }

void MediaTransition::clear()
{
    finish();
    if (source_) {
        obs_source_release(source_);
    }
    source_ = nullptr;
    if (media_) obs_source_release(media_);
    media_ = nullptr;
    config_ = {};
}

bool MediaTransition::configure(const json &value, std::string &error)
{
    if (value.is_null()) { clear(); return true; }
    MediaConfig next;
    if (!parse_media_config(value, next, error)) return false;
    std::error_code ec;
    const auto path = std::filesystem::u8path(next.path);
    if (!std::filesystem::is_regular_file(path, ec)) { error = "ASSET_MISSING"; return false; }
    std::ifstream stream(path, std::ios::binary);
    std::array<unsigned char, 12> header{};
    stream.read(reinterpret_cast<char *>(header.data()), header.size());
    const auto length = stream.gcount();
    const bool webm = length >= 4 && header[0] == 0x1a && header[1] == 0x45 && header[2] == 0xdf && header[3] == 0xa3;
    const bool mp4 = length == 12 && std::memcmp(header.data() + 4, "ftyp", 4) == 0;
    auto extension = path.extension().u8string();
    std::transform(extension.begin(), extension.end(), extension.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    if ((extension == ".webm" && !webm) || (extension == ".mp4" && !mp4)) {
        error = "ASSET_INVALID_CONTAINER"; return false;
    }
    OBSDataAutoRelease settings = obs_data_create();
    obs_data_set_string(settings, "path", next.path.c_str());
    obs_data_set_int(settings, "transition_point", next.cut_point_ms);
    obs_data_set_int(settings, "tp_type", 0);
    obs_data_set_bool(settings, "hw_decode", false);
    obs_data_set_bool(settings, "preload", false); // bounded memory; preload the first frame only
    obs_data_set_int(settings, "audio_monitoring", OBS_MONITORING_TYPE_NONE);
    auto *replacement = obs_source_create_private("obs_stinger_transition", "PulsarConfiguredStinger", settings);
    if (!replacement || !obs_get_source_output_flags("ffmpeg_source")) {
        if (replacement) obs_source_release(replacement);
        error = "DECODER_UNAVAILABLE"; return false;
    }
    clear();
    config_ = std::move(next);
    source_ = replacement;
    return true;
}

obs_source_t *MediaTransition::media() const
{
    // Stinger creation schedules its update on the video thread: its media
    // child does not necessarily exist when Configure returns. Observe that
    // child during readiness checks, never wait for graphics under the lane
    // mutex. Once retained, it is stable until the next idle Configure/Clear.
    if (!media_ && source_) obs_source_enum_full_tree(source_, [](obs_source_t *, obs_source_t *child, void *p) {
        if (std::strcmp(obs_source_get_id(child), "ffmpeg_source") == 0)
            *static_cast<obs_source_t **>(p) = obs_source_get_ref(child);
    }, &media_);
    return media_;
}

uint64_t MediaTransition::media_duration_ns() const
{
    if (!media()) return 0;
    calldata_t cd = {};
    proc_handler_call(obs_source_get_proc_handler(media_), "get_duration", &cd);
    const int64_t duration = calldata_int(&cd, "duration");
    calldata_free(&cd);
    return duration > 0 ? static_cast<uint64_t>(duration) : 0;
}

void MediaTransition::preload_tick()
{
    preload_frame(media());
}

void MediaTransition::preload_frame(obs_source_t *media)
{
    // Inactive ffmpeg sources retain a decoded first frame, but expose zero
    // dimensions until that frame has a texture. Upload it on the video thread
    // only: doing this from GetState would invert graphics and lane locks.
    if (media && !obs_source_get_width(media)) {
        calldata_t cd = {};
        proc_handler_call(obs_source_get_proc_handler(media), "preload_first_frame", &cd);
        calldata_free(&cd);
        obs_source_show_preloaded_video(media);
    }
}

obs_source_t *MediaTransition::create_preview_playback() const
{
    if (!media()) return nullptr;
    OBSDataAutoRelease original = obs_source_get_settings(media_);
    OBSDataAutoRelease settings = obs_data_create();
    obs_data_apply(settings, original);
    auto *playback = obs_source_create_private("ffmpeg_source", "PulsarPreviewTransitionVideo", settings);
    if (playback) {
        obs_source_set_volume(playback, config_.volume);
        obs_source_set_muted(playback, true);
    }
    return playback;
}

std::string MediaTransition::readiness_error() const
{
    if (!source_) return "NOT_CONFIGURED";
    std::error_code ec;
    if (!std::filesystem::is_regular_file(std::filesystem::u8path(config_.path), ec)) return "ASSET_MISSING";
    if (!media()) return "MEDIA_NOT_READY";
    if (obs_source_media_get_state(media_) == OBS_MEDIA_STATE_ERROR) return "DECODE_FAILED";
    const auto ns = media_duration_ns();
    if (!ns || !obs_source_get_width(media_) || !obs_source_get_height(media_)) return "MEDIA_NOT_READY";
    // OBS stinger appends 250 ms to finish its media/audio tail. The native
    // controller must honor that fixed duration, not truncate it to a UI guess.
    if (ns + 250000000ULL > 20000000000ULL) return "DURATION_INVALID";
    if (uint64_t{config_.cut_point_ms} * 1000000ULL >= ns) return "CUT_POINT_OUTSIDE_MEDIA";
    return {};
}

json MediaTransition::state() const
{
    const auto error = readiness_error();
    const auto ns = media_duration_ns();
    return {{"configured", source_ != nullptr},
            {"config", source_ ? json{{"path", config_.path}, {"cut_point_ms", config_.cut_point_ms},
                {"volume", config_.volume}, {"muted", config_.muted}} : json::object()},
            {"ready", error.empty()}, {"readiness_error", error},
            {"media_duration_ms", ns / 1000000ULL},
            {"transition_duration_ms", ns ? (ns + 250000000ULL) / 1000000ULL : 0}};
}

obs_source_t *MediaTransition::prepare(bool preview, uint32_t &duration_ms, std::string &error)
{
    error = readiness_error();
    if (!error.empty()) return nullptr;
    duration_ms = static_cast<uint32_t>((media_duration_ns() + 250000000ULL) / 1000000ULL);
    obs_source_set_volume(media_, config_.volume);
    obs_source_set_muted(media_, preview || config_.muted);
    prepared_ = true;
    return source_;
}

void MediaTransition::finish()
{
    // clear() releases A/B but does not notify the stinger implementation.
    // Reset its media lifecycle as well, including after an early abort.
    if (source_ && prepared_) obs_transition_force_stop(source_);
    if (source_) obs_transition_clear(source_);
    prepared_ = false;
}
} // namespace pulsar_transition
