#pragma once

// Internal process bridge between the headless frontend stub and the
// pulsar-websocket module.  The websocket module is a DLL and the frontend
// stub is linked into pulsar-headless, so the global libobs procedure handler
// is the narrow ABI-free seam shared by both sides.

#include <obs.h>

namespace pulsar_dual_lane_control {

inline constexpr char kMutationEnterProc[] = "pulsar_dual_lane_mutation_enter";
inline constexpr char kMutationLeaveProc[] = "pulsar_dual_lane_mutation_leave";

// A lease serializes one potentially mutating websocket dispatch with the
// dual-lane Take boundary.  If the bridge is not installed or dual-lane is
// inactive, the caller keeps the legacy behaviour; an active bridge can only
// fail closed.  The proc handler pointer is retained for leave(), because the
// global handler is replaced only during OBS lifecycle teardown, after the
// websocket server has stopped accepting requests.
class MutationLease {
public:
    explicit MutationLease(bool mutating)
    {
        if (!mutating)
            return;

        procHandler_ = obs_get_proc_handler();
        if (!procHandler_)
            return;

        calldata_t cd = {};
        calldata_set_bool(&cd, "mutating", true);
        const bool called = proc_handler_call(procHandler_, kMutationEnterProc, &cd);
        bool available = false;
        bool allowed = true;
        bool held = false;
        if (called) {
            calldata_get_bool(&cd, "available", &available);
            calldata_get_bool(&cd, "allowed", &allowed);
            calldata_get_bool(&cd, "held", &held);
        }
        calldata_free(&cd);

        // An ordinary OBS frontend has no Pulsar bridge.  Preserve its
        // behaviour; the headless dual-lane frontend reports available=true
        // and controls the fail-closed decision itself.
        if (!called || !available)
            return;

        bridgeActive_ = true;
        allowed_ = allowed;
        held_ = held;
    }

    MutationLease(const MutationLease &) = delete;
    MutationLease &operator=(const MutationLease &) = delete;

    ~MutationLease() { release(); }

    bool allowed() const { return allowed_; }

private:
    void release()
    {
        if (!bridgeActive_ || !held_ || !procHandler_)
            return;

        calldata_t cd = {};
        proc_handler_call(procHandler_, kMutationLeaveProc, &cd);
        calldata_free(&cd);
        held_ = false;
    }

    proc_handler_t *procHandler_ = nullptr;
    bool bridgeActive_ = false;
    bool allowed_ = true;
    bool held_ = false;
};

} // namespace pulsar_dual_lane_control
