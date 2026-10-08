#include "pulsar-media-transition.h"
#include <filesystem>
#include <iostream>
#include <stdexcept>

int main()
{
    using nlohmann::json;
    const auto path = (std::filesystem::temp_directory_path() / "custom.webm").u8string();
    const json good = {{"path", path}, {"cut_point_ms", 880}, {"volume", 0.4}, {"muted", true}};
    pulsar_transition::MediaConfig config;
    std::string error;
    if (!pulsar_transition::parse_media_config(good, config, error) || config.cut_point_ms != 880 || !config.muted)
        throw std::runtime_error("valid host configuration rejected");
    int rejected = 0;
    const auto reject = [&](json candidate) {
        if (pulsar_transition::parse_media_config(candidate, config, error) || error.empty())
            throw std::runtime_error("invalid host configuration accepted: " + candidate.dump());
        ++rejected;
    };
    for (auto value : {json(-1), json(0), json(20000), json(0.5), json(true), json("880"), json(nullptr)}) {
        auto candidate = good; candidate["cut_point_ms"] = value; reject(candidate);
    }
    for (auto value : {json(-0.1), json(1.1), json(true), json("0.5"), json(nullptr)}) {
        auto candidate = good; candidate["volume"] = value; reject(candidate);
    }
    for (auto value : {json("https://example.org/file.webm"), json("relative.webm"), json(""),
                       json("\\\\server\\share\\file.webm"), json(path + ".mp4"), json(path + std::string(1, '\0'))}) {
        auto candidate = good; candidate["path"] = value; reject(candidate);
    }
    auto candidate = good; candidate["muted"] = 1; reject(candidate);
    candidate = good; candidate["arbitrary"] = true; reject(candidate);
    reject(json::array()); reject(nullptr);
    std::cout << "media configuration: valid custom WebM accepted; " << rejected << " invalid cases rejected\n";
}
