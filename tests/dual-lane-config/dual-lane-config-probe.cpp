#include "pulsar-dual-lane-config.h"

#include <array>
#include <cassert>
#include <cstdint>

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
        assert(result.valid == test.valid);
        assert(result.present == test.present);
        assert(result.takes == test.takes);
    }
    return 0;
}
