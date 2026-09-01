#include "pulsar-transition-controller.h"

#include <cstdio>
#include <cstdlib>
#include <string>

// assert() is a no-op under NDEBUG (RelWithDebInfo CI build). Keep every
// probe assertion active so a failed transition contract cannot pass silently.
#define PULSAR_CHECK(expr)                                                                  \
    do {                                                                                    \
        if (!(expr)) {                                                                      \
            std::fprintf(stderr, "CHECK FAILED: %s (%s:%d)\n", #expr, __FILE__, __LINE__);  \
            std::exit(EXIT_FAILURE);                                                        \
        }                                                                                    \
    } while (0)

using pulsar_transition::Controller;
using pulsar_transition::Kind;
using pulsar_transition::Phase;

int main()
{
    Controller fade;
    PULSAR_CHECK(fade.begin(Kind::Fade, 250, true));
    PULSAR_CHECK(fade.phase() == Phase::Queued);
    PULSAR_CHECK(fade.started(10, 1'000'000'000));
    fade.set_start_monotonic_ns(5'000'000'000);
    PULSAR_CHECK(!fade.deadline_reached(5'249'999'999));
    PULSAR_CHECK(fade.deadline_reached(5'250'000'000));
    PULSAR_CHECK(fade.final_queued());
    PULSAR_CHECK(fade.committed(25, 1'400'000'000, 5'251'000'000));
    PULSAR_CHECK(fade.metrics().actual_duration_ms == 400);
    PULSAR_CHECK(fade.metrics().actual_duration_frames == 15);
    PULSAR_CHECK(fade.metrics().start_frame_id == 10);
    PULSAR_CHECK(fade.metrics().end_frame_id == 25);

    // The runtime campaign reports aggregates from the controller, rather
    // than requiring a post-hoc parser to infer them from one sample. Vary
    // durations and frame counts so each percentile is exercised.
    for (uint64_t i = 0; i < 100; ++i) {
        const uint64_t duration = 10 + i % 10;
        PULSAR_CHECK(fade.begin(Kind::Fade, 300, true));
        PULSAR_CHECK(fade.started(100 + i, 2'000'000'000 + i * 20'000'000));
        PULSAR_CHECK(fade.final_queued());
        PULSAR_CHECK(fade.committed(100 + i + duration,
                                   2'000'000'000 + i * 20'000'000 + duration * 1'000'000,
                                   5'300'000'000 + i * 1'000'000));
    }
    const auto aggregate = fade.aggregate(Kind::Fade);
    PULSAR_CHECK(aggregate.count == 101);
    PULSAR_CHECK(aggregate.duration_p50_ms == 15);
    PULSAR_CHECK(aggregate.duration_p95_ms == 19);
    PULSAR_CHECK(aggregate.duration_p99_ms == 19);
    PULSAR_CHECK(aggregate.frames_p50 == 15);
    PULSAR_CHECK(aggregate.frames_p95 == 19);
    PULSAR_CHECK(aggregate.frames_p99 == 19);

    Controller stinger;
    PULSAR_CHECK(!stinger.begin(Kind::Stinger, 300, false));
    PULSAR_CHECK(stinger.phase() == Phase::Committed);
    PULSAR_CHECK(stinger.metrics().fallback_to_cut);
    PULSAR_CHECK(stinger.metrics().fallback_reason &&
           std::string(stinger.metrics().fallback_reason) == "transition_unavailable");

    Controller interrupted;
    PULSAR_CHECK(interrupted.begin(Kind::Fade, 200, true));
    PULSAR_CHECK(interrupted.started(1, 10));
    PULSAR_CHECK(interrupted.abort("operator"));
    PULSAR_CHECK(interrupted.phase() == Phase::Aborted);
    PULSAR_CHECK(interrupted.metrics().fallback_to_cut);
    PULSAR_CHECK(!interrupted.committed(2, 20, 20));

    Controller queuedAbort;
    PULSAR_CHECK(queuedAbort.begin(Kind::Fade, 100, true));
    PULSAR_CHECK(queuedAbort.abort("queue_cancelled"));
    PULSAR_CHECK(queuedAbort.phase() == Phase::Aborted);

    Controller finalAbort;
    PULSAR_CHECK(finalAbort.begin(Kind::Stinger, 100, true));
    PULSAR_CHECK(finalAbort.started(4, 40));
    PULSAR_CHECK(finalAbort.final_queued());
    PULSAR_CHECK(finalAbort.abort("operator"));
    PULSAR_CHECK(finalAbort.phase() == Phase::Aborted);

    Controller invalidDuration;
    PULSAR_CHECK(!invalidDuration.begin(Kind::Fade, 49, true));
    // The public request validator rejects this before any atomic queue; the
    // controller's direct guard likewise leaves no active transition.
    PULSAR_CHECK(!invalidDuration.active());
    PULSAR_CHECK(invalidDuration.phase() == Phase::Committed);
    PULSAR_CHECK(invalidDuration.metrics().fallback_to_cut);
    PULSAR_CHECK(std::string(invalidDuration.metrics().fallback_reason) == "duration_invalid");
    return 0;
}
