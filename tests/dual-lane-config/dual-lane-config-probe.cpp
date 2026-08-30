#include "pulsar-dual-lane-config.h"

#include <array>
#include <cstdio>
#include <cstdint>
#include <cstdlib>

#define PULSAR_CHECK(expression)                                                        \
    do {                                                                                \
        if (!(expression)) {                                                            \
            std::fprintf(stderr, "CHECK FAILED: %s (%s:%d)\n", #expression, __FILE__, \
                         __LINE__);                                                     \
            return EXIT_FAILURE;                                                        \
        }                                                                               \
    } while (0)

namespace {

struct Case {
    const char *value;
    bool valid;
    bool present;
    uint64_t takes;
};

} // namespace

int main()
{
    constexpr std::array<Case, 15> cases = {{
        {nullptr, true, false, 0},
        {"", false, true, 0},
        {"1", true, true, 1},
        {"0001", true, true, 1},
        {"99999", true, true, 99999},
        {"100000", true, true, 100000},
        {"0", false, true, 0},
        {"0000", false, true, 0},
        {"100001", false, true, 0},
        {"18446744073709551615", false, true, 0},
        {" 1", false, true, 0},
        {"1 ", false, true, 0},
        {"+1", false, true, 0},
        {"-1", false, true, 0},
        {"1.0", false, true, 0},
    }};

    for (const Case &test : cases) {
        const auto result = pulsar_dual_lane_config::parse_rollback_after_takes(test.value);
        PULSAR_CHECK(result.valid == test.valid);
        PULSAR_CHECK(result.present == test.present);
        PULSAR_CHECK(result.takes == test.takes);
    }
    return 0;
}
