#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace pulsar_runtime_telemetry {

// The first six values are the established runtime telemetry selector.  The
// remaining values are opt-in stage observations added by issue #253.
enum class Signal : uint8_t {
    Program = 0,
    Preview,
    Raw,
    Borrowed,
    Gpu,
    Queues,
    EncoderFrameReady,
    ProgramReturnReadback,
    EncodeCallbackEnqueue,
    OutputMuxEnqueue,
    SocketSend,
    Count,
};

struct SignalSelection {
    uint32_t mask = 0;
    bool valid = false;
    std::string error;

    bool enabled(Signal signal) const
    {
        return valid && (mask & (uint32_t{1} << static_cast<unsigned>(signal))) != 0;
    }
};

constexpr uint32_t signal_bit(Signal signal)
{
    return uint32_t{1} << static_cast<unsigned>(signal);
}

constexpr uint32_t all_signal_mask()
{
    return (uint32_t{1} << static_cast<unsigned>(Signal::Count)) - 1;
}

inline const std::array<const char *, static_cast<size_t>(Signal::Count)> &signal_names()
{
    static constexpr std::array<const char *, static_cast<size_t>(Signal::Count)> names = {
        "program", "preview", "raw", "borrowed", "gpu", "queues", "encoder_frame_ready",
        "program_return_readback", "encode_callback_enqueue", "output_mux_enqueue", "socket_send",
    };
    return names;
}

inline Signal signal_from_name(const std::string &name)
{
    const auto &names = signal_names();
    for (size_t index = 0; index < names.size(); ++index) {
        if (name == names[index])
            return static_cast<Signal>(index);
    }
    return Signal::Count;
}

inline std::string trim_ascii(const std::string &value)
{
    size_t first = 0;
    size_t last = value.size();
    while (first < last && (value[first] == ' ' || value[first] == '\t'))
        ++first;
    while (last > first && (value[last - 1] == ' ' || value[last - 1] == '\t'))
        --last;
    return value.substr(first, last - first);
}

inline SignalSelection parse_signal_selection(const char *raw)
{
    SignalSelection result;
    if (!raw) {
        result.mask = all_signal_mask();
        result.valid = true;
        return result;
    }

    const std::string value(raw);
    if (value.empty()) {
        result.error = "PULSAR_TRACE_SIGNALS is empty; use all, none, or a CSV signal set";
        return result;
    }

    size_t begin = 0;
    while (begin <= value.size()) {
        const size_t comma = value.find(',', begin);
        const std::string token = trim_ascii(value.substr(begin, comma == std::string::npos ? std::string::npos
                                                                                              : comma - begin));
        if (token.empty()) {
            result.error = "PULSAR_TRACE_SIGNALS contains an empty CSV token";
            return result;
        }
        if (token == "all") {
            if (value.find(',') != std::string::npos) {
                result.error = "all must be the only PULSAR_TRACE_SIGNALS token";
                return result;
            }
            result.mask = all_signal_mask();
        } else if (token == "none") {
            if (value.find(',') != std::string::npos) {
                result.error = "none must be the only PULSAR_TRACE_SIGNALS token";
                return result;
            }
            result.mask = 0;
        } else {
            const Signal signal = signal_from_name(token);
            if (signal == Signal::Count) {
                result.error = "unknown PULSAR_TRACE_SIGNALS token: " + token;
                return result;
            }
            const uint32_t bit = signal_bit(signal);
            if ((result.mask & bit) != 0) {
                result.error = "duplicate PULSAR_TRACE_SIGNALS token: " + token;
                return result;
            }
            result.mask |= bit;
        }
        if (comma == std::string::npos)
            break;
        begin = comma + 1;
    }
    result.valid = true;
    return result;
}

inline std::vector<std::string> selected_signal_names(uint32_t mask)
{
    std::vector<std::string> selected;
    const auto &names = signal_names();
    for (size_t index = 0; index < names.size(); ++index) {
        if ((mask & signal_bit(static_cast<Signal>(index))) != 0)
            selected.emplace_back(names[index]);
    }
    return selected;
}

} // namespace pulsar_runtime_telemetry
