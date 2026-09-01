#pragma once

// Deterministic state machine for transitions composed above the dual-lane
// atomic Cut.  The controller owns no libobs objects and performs no I/O; the
// frontend holds its lane mutex while calling it and uses the returned phase
// to decide which frame-boundary swap to queue.

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>

namespace pulsar_transition {

enum class Kind : uint8_t { Cut, Fade, Stinger };
enum class Phase : uint8_t { Idle, Queued, Running, FinalQueued, Committed, Aborted };

struct Metrics {
    Kind kind = Kind::Cut;
    Phase phase = Phase::Idle;
    uint64_t requested_duration_ms = 0;
    uint64_t actual_duration_ms = 0;
    uint64_t actual_duration_frames = 0;
    uint64_t start_frame_id = 0;
    uint64_t start_pts_ns = 0;
    uint64_t end_frame_id = 0;
    uint64_t end_pts_ns = 0;
    bool fallback_to_cut = false;
    const char *fallback_reason = nullptr;
};

struct AggregateSummary {
    uint64_t count = 0;
    uint64_t duration_p50_ms = 0;
    uint64_t duration_p95_ms = 0;
    uint64_t duration_p99_ms = 0;
    uint64_t frames_p50 = 0;
    uint64_t frames_p95 = 0;
    uint64_t frames_p99 = 0;
};

// Bounded, allocation-free aggregate for the runtime transition probe.  The
// count remains exact; the fixed sample window is deliberately large enough
// for the 100-take acceptance campaign and avoids adding work to the frame
// boundary callback beyond a small copy/sort when a commit is logged.
class Aggregate {
public:
    void record(const Metrics &metrics)
    {
        ++count_;
        if (sample_count_ == kMaxSamples)
            return;
        durations_[sample_count_] = metrics.actual_duration_ms;
        frames_[sample_count_] = metrics.actual_duration_frames;
        ++sample_count_;
    }

    AggregateSummary summary() const
    {
        AggregateSummary out;
        out.count = count_;
        if (sample_count_ == 0)
            return out;

        auto durations = durations_;
        auto frames = frames_;
        std::sort(durations.begin(), durations.begin() + static_cast<std::ptrdiff_t>(sample_count_));
        std::sort(frames.begin(), frames.begin() + static_cast<std::ptrdiff_t>(sample_count_));
        out.duration_p50_ms = percentile(durations);
        out.duration_p95_ms = percentile(durations, 95);
        out.duration_p99_ms = percentile(durations, 99);
        out.frames_p50 = percentile(frames);
        out.frames_p95 = percentile(frames, 95);
        out.frames_p99 = percentile(frames, 99);
        return out;
    }

private:
    static constexpr size_t kMaxSamples = 2048;

    template <typename T>
    T percentile(const std::array<T, kMaxSamples> &values, uint64_t rank = 50) const
    {
        const size_t index = (static_cast<size_t>(rank) * sample_count_ + 99) / 100 - 1;
        return values[index < sample_count_ ? index : sample_count_ - 1];
    }

    uint64_t count_ = 0;
    size_t sample_count_ = 0;
    std::array<uint64_t, kMaxSamples> durations_{};
    std::array<uint64_t, kMaxSamples> frames_{};
};

class Controller {
public:
    bool begin(Kind kind, uint64_t duration_ms, bool available)
    {
        if (phase_ != Phase::Idle && phase_ != Phase::Committed && phase_ != Phase::Aborted)
            return false;
        metrics_ = {};
        metrics_.kind = kind;
        metrics_.requested_duration_ms = duration_ms;
        if (kind == Kind::Cut) {
            phase_ = Phase::Committed;
            metrics_.phase = phase_;
            return true;
        }
        if (!available || duration_ms < 50 || duration_ms > 20000) {
            metrics_.fallback_to_cut = true;
            metrics_.fallback_reason = !available ? "transition_unavailable" : "duration_invalid";
            phase_ = Phase::Committed;
            metrics_.phase = phase_;
            return false;
        }
        phase_ = Phase::Queued;
        metrics_.phase = phase_;
        return true;
    }

    void queued() { phase_ = Phase::Queued; metrics_.phase = phase_; }

    bool started(uint64_t frame_id, uint64_t pts_ns)
    {
        if (phase_ != Phase::Queued)
            return false;
        phase_ = Phase::Running;
        metrics_.phase = phase_;
        metrics_.start_frame_id = frame_id;
        metrics_.start_pts_ns = pts_ns;
        return true;
    }

    bool deadline_reached(uint64_t monotonic_ns) const
    {
        return phase_ == Phase::Running && start_monotonic_ns_ != 0 &&
               monotonic_ns >= start_monotonic_ns_ + metrics_.requested_duration_ms * 1000000ULL;
    }

    void set_start_monotonic_ns(uint64_t value) { start_monotonic_ns_ = value; }

    bool final_queued()
    {
        if (phase_ != Phase::Running)
            return false;
        phase_ = Phase::FinalQueued;
        metrics_.phase = phase_;
        return true;
    }

    void final_queue_failed()
    {
        if (phase_ == Phase::FinalQueued) {
            phase_ = Phase::Running;
            metrics_.phase = phase_;
        }
    }

    bool committed(uint64_t frame_id, uint64_t pts_ns, uint64_t monotonic_ns)
    {
        if (phase_ != Phase::FinalQueued && phase_ != Phase::Queued)
            return false;
        phase_ = Phase::Committed;
        metrics_.phase = phase_;
        metrics_.end_frame_id = frame_id;
        metrics_.end_pts_ns = pts_ns;
        if (frame_id >= metrics_.start_frame_id)
            metrics_.actual_duration_frames = frame_id - metrics_.start_frame_id;
        if (pts_ns >= metrics_.start_pts_ns)
            metrics_.actual_duration_ms = (pts_ns - metrics_.start_pts_ns) / 1000000ULL;
        if (start_monotonic_ns_ != 0 && monotonic_ns >= start_monotonic_ns_)
            metrics_.actual_duration_ms =
                metrics_.actual_duration_ms != 0 ? metrics_.actual_duration_ms
                                                  : (monotonic_ns - start_monotonic_ns_) / 1000000ULL;
        aggregates_[static_cast<size_t>(metrics_.kind)].record(metrics_);
        return true;
    }

    bool abort(const char *reason)
    {
        if (phase_ != Phase::Queued && phase_ != Phase::Running && phase_ != Phase::FinalQueued)
            return false;
        phase_ = Phase::Aborted;
        metrics_.phase = phase_;
        metrics_.fallback_to_cut = true;
        metrics_.fallback_reason = reason ? reason : "interrupted";
        return true;
    }

    void set_fallback_reason(const char *reason)
    {
        if (reason) {
            metrics_.fallback_to_cut = true;
            metrics_.fallback_reason = reason;
        }
    }

    Phase phase() const { return phase_; }
    const Metrics &metrics() const { return metrics_; }
    AggregateSummary aggregate(Kind kind) const
    {
        return aggregates_[static_cast<size_t>(kind)].summary();
    }
    bool active() const
    {
        return phase_ == Phase::Queued || phase_ == Phase::Running || phase_ == Phase::FinalQueued;
    }

private:
    Phase phase_ = Phase::Idle;
    uint64_t start_monotonic_ns_ = 0;
    Metrics metrics_;
    std::array<Aggregate, 3> aggregates_;
};

inline const char *kind_name(Kind kind)
{
    switch (kind) {
    case Kind::Fade: return "fade";
    case Kind::Stinger: return "stinger";
    default: return "cut";
    }
}

inline const char *phase_name(Phase phase)
{
    switch (phase) {
    case Phase::Queued: return "queued";
    case Phase::Running: return "running";
    case Phase::FinalQueued: return "final_queued";
    case Phase::Committed: return "committed";
    case Phase::Aborted: return "aborted";
    default: return "idle";
    }
}

} // namespace pulsar_transition
