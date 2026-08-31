#pragma once

#include <cstdint>

namespace pulsar_dual_lane_config {

struct RollbackAfterTakes {
    uint64_t takes = 0;
    bool valid = false;
    bool present = false;
};

// Parse the rollback drill trigger without locale-dependent conversion.
// Unset is valid and means "no drill"; every explicitly present value must
// be ASCII digits representing 1..100000.  Keeping this helper header-only
// gives the production boot path and its native truth-table probe one exact
// parser, without widening the scene-switch wire contract.
inline RollbackAfterTakes parse_rollback_after_takes(const char *value)
{
    if (!value)
        return {0, true, false};
    if (!*value)
        return {0, false, true};

    uint64_t parsed = 0;
    for (const char *p = value; *p; ++p) {
        const unsigned char digit = static_cast<unsigned char>(*p);
        if (digit < static_cast<unsigned char>('0') || digit > static_cast<unsigned char>('9'))
            return {0, false, true};
        const uint64_t digitValue = static_cast<uint64_t>(digit - static_cast<unsigned char>('0'));
        if (parsed > 100000ULL / 10ULL ||
            (parsed == 100000ULL / 10ULL && digitValue > 0))
            return {0, false, true};
        parsed = parsed * 10ULL + digitValue;
    }
    return {parsed, parsed >= 1 && parsed <= 100000ULL, true};
}

} // namespace pulsar_dual_lane_config
