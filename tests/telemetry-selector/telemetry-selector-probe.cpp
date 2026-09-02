#include "pulsar-runtime-telemetry-signals.h"

#include <array>
#include <cstdio>
#include <cstdlib>

#define PULSAR_CHECK(expression)                                                        \
    do {                                                                                \
        if (!(expression)) {                                                            \
            std::fprintf(stderr, "CHECK FAILED: %s (%s:%d)\n", #expression, __FILE__, \
                         __LINE__);                                                     \
            return EXIT_FAILURE;                                                        \
        }                                                                               \
    } while (0)

using pulsar_runtime_telemetry::Signal;
using pulsar_runtime_telemetry::all_signal_mask;
using pulsar_runtime_telemetry::parse_signal_selection;
using pulsar_runtime_telemetry::signal_bit;

struct Case {
    const char *value;
    bool valid;
    uint32_t mask;
};

int main()
{
    const std::array<Case, 16> cases = {{
        {nullptr, true, all_signal_mask()},
        {"all", true, all_signal_mask()},
        {"none", true, 0},
        {"program", true, signal_bit(Signal::Program)},
        {"preview,raw", true, signal_bit(Signal::Preview) | signal_bit(Signal::Raw)},
        {"borrowed,gpu,queues", true, signal_bit(Signal::Borrowed) | signal_bit(Signal::Gpu) |
                                      signal_bit(Signal::Queues)},
        {" program , raw ", true, signal_bit(Signal::Program) | signal_bit(Signal::Raw)},
        {"", false, 0},
        {",raw", false, 0},
        {"raw,", false, 0},
        {"ALL", false, 0},
        {"all,raw", false, 0},
        {"none,raw", false, 0},
        {"raw,raw", false, 0},
        {"unknown", false, 0},
        {"program\nraw", false, 0},
    }};

    for (const Case &test : cases) {
        const auto result = parse_signal_selection(test.value);
        PULSAR_CHECK(result.valid == test.valid);
        if (result.valid)
            PULSAR_CHECK(result.mask == test.mask);
        else
            PULSAR_CHECK(!result.error.empty());
    }

    const auto none = parse_signal_selection("none");
    PULSAR_CHECK(!none.enabled(Signal::Program));
    PULSAR_CHECK(!none.enabled(Signal::Queues));
    const auto selected = parse_signal_selection("program,borrowed");
    PULSAR_CHECK(selected.enabled(Signal::Program));
    PULSAR_CHECK(!selected.enabled(Signal::Preview));
    PULSAR_CHECK(selected.enabled(Signal::Borrowed));
    return EXIT_SUCCESS;
}
