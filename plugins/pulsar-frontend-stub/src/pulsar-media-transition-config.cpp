#include "pulsar-media-transition.h"
#include <algorithm>
#include <cctype>
#include <cmath>
#include <filesystem>

namespace pulsar_transition {
using json = nlohmann::json;

bool parse_media_config(const json &value, MediaConfig &out, std::string &error)
{
    if (!value.is_object()) { error = "CONFIG_INVALID"; return false; }
    for (auto it = value.begin(); it != value.end(); ++it)
        if (it.key() != "path" && it.key() != "cut_point_ms" && it.key() != "volume" && it.key() != "muted") {
            error = "CONFIG_INVALID"; return false;
        }
    if (!value.contains("path") || !value["path"].is_string() ||
        !value.contains("cut_point_ms") || !value["cut_point_ms"].is_number_integer()) {
        error = "CONFIG_INVALID"; return false;
    }
    const auto path = value["path"].get<std::string>();
    const auto point = value["cut_point_ms"].get<double>();
    if (path.empty() || path.size() > 32767 || path.find('\0') != std::string::npos ||
        path.find("://") != std::string::npos || !std::filesystem::u8path(path).is_absolute() ||
        path.rfind("\\\\", 0) == 0 || path.rfind("//", 0) == 0) {
        error = "LOCAL_PATH_REQUIRED"; return false;
    }
    auto extension = std::filesystem::u8path(path).extension().u8string();
    std::transform(extension.begin(), extension.end(), extension.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    if (extension != ".webm") { error = "WEBM_REQUIRED"; return false; }
    if (point < 1 || point >= 20000) { error = "CUT_POINT_INVALID"; return false; }
    if (value.contains("volume") && (!value["volume"].is_number() ||
        !std::isfinite(value["volume"].get<double>()) || value["volume"].get<double>() < 0 ||
        value["volume"].get<double>() > 1)) { error = "VOLUME_INVALID"; return false; }
    if (value.contains("muted") && !value["muted"].is_boolean()) { error = "MUTED_INVALID"; return false; }
    out = {path, static_cast<uint32_t>(point), value.value("volume", 1.0f), value.value("muted", false)};
    return true;
}

} // namespace pulsar_transition
