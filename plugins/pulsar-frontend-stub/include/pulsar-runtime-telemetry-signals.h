#pragma once

#include <cstdint>
#include <cstddef>
#include <string>

namespace pulsar_runtime_telemetry {

enum class Signal : uint8_t {
    Program = 0,
    Preview,
    Raw,
    Borrowed,
    Gpu,
    Queues,
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
    return signal_bit(Signal::Program) | signal_bit(Signal::Preview) | signal_bit(Signal::Raw) |
           signal_bit(Signal::Borrowed) | signal_bit(Signal::Gpu) | signal_bit(Signal::Queues);
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

    bool sawToken = false;
    size_t begin = 0;
    while (begin <= value.size()) {
        const size_t comma = value.find(',', begin);
        const std::string token = trim_ascii(value.substr(begin, comma == std::string::npos ? std::string::npos
                                                                                              : comma - begin));
        if (token.empty()) {
            result.error = "PULSAR_TRACE_SIGNALS contains an empty CSV token";
            return result;
        }
        sawToken = true;
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
            Signal signal = Signal::Count;
            if (token == "program") signal = Signal::Program;
            else if (token == "preview") signal = Signal::Preview;
            else if (token == "raw") signal = Signal::Raw;
            else if (token == "borrowed") signal = Signal::Borrowed;
            else if (token == "gpu") signal = Signal::Gpu;
            else if (token == "queues") signal = Signal::Queues;
            else {
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

    if (!sawToken) {
        result.error = "PULSAR_TRACE_SIGNALS has no tokens";
        return result;
    }
    result.valid = true;
    return result;
}

} // namespace pulsar_runtime_telemetry
