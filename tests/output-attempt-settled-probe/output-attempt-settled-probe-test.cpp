// Regression probe for issue #186 / ADR-005 §3.5 -- pulsar:OutputAttemptSettled.
// Pure logic, no libobs/Qt/OBS runtime needed: the state machine that decides
// WHEN a verdict is built (attempt_went_active tracking in plugin-main.cpp's
// DestinationRegistry and pulsar-frontend-stub.cpp's PulsarFrontendAPI) needs
// real obs_output_t signal_handler wiring and is exercised by the CTest
// integration suite (scripts/probe-multi-stream.py) instead. What IS a pure
// function of its inputs, and is what this probe pins, is WHAT that verdict
// contains once "outcome" is known: reason_class is a closed-set classification
// on failure and, this issue's own contract, an ABSENT key -- not null, not a
// placeholder character -- on success.
//
// Each case also prints the JSON payload pulsar:OutputAttemptSettled would
// carry, session left empty per the issue's owned scope, mirroring
// tests/output-classify-probe's evidence pattern.

#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "pulsar-output-classify.h"

using pulsar::classify_output_failure;

// Mirrors emit_output_attempt_settled's (plugin-main.cpp) and
// EmitOutputAttemptSettledViaGlobalProc's (pulsar-frontend-stub.cpp) contract:
// a `live` verdict never carries reason_class, a `failed` verdict always does
// (a non-zero code always classifies -- classify_output_failure only returns
// nullptr for code == 0, and this helper is never called with live=false and
// code == 0 by either caller: see their "code != 0" gates).
static const char *attempt_reason_class(bool live, bool is_local_output, int code, const char *last_error)
{
	if (live)
		return nullptr; // ABSENT key, not printed at all below
	const char *cls = classify_output_failure(is_local_output, code, last_error);
	return cls ? cls : "unknown";
}

// Prints reason_class only when non-null -- the same "key simply never set"
// contract obs_data_set_string's conditional call implements in production.
static void print_scenario(const char *name, const char *output, const char *destination, long long attempt,
			    bool live, int code, const char *last_error, long long duration_ms)
{
	const char *cls = attempt_reason_class(live, strcmp(output, "virtualcam") == 0, code, last_error);
	printf("{\"scenario\":\"%s\",\"output\":\"%s\",\"destination\":\"%s\",\"attempt\":%lld,"
	       "\"outcome\":\"%s\"", name, output, destination, attempt, live ? "live" : "failed");
	if (cls)
		printf(",\"reason_class\":\"%s\"", cls);
	printf(",\"code\":%d,\"last_error\":\"%s\",\"duration_ms\":%lld,\"session\":\"\"}\n", code,
	       last_error ? last_error : "", duration_ms);
}

int main()
{
	// A success verdict NEVER carries reason_class, on any output kind,
	// regardless of what code/last_error a caller might (incorrectly)
	// pass alongside live=true -- the omission is unconditional on `live`.
	assert(attempt_reason_class(/*live=*/true, false, 0, nullptr) == nullptr);
	assert(attempt_reason_class(/*live=*/true, true, -8, "should not matter") == nullptr);
	print_scenario("stream-attempt-live", "stream", "stream", 1, true, 0, nullptr, 842);
	print_scenario("destination-attempt-live", "PulsarDest_abc123", "abc123", 1, true, 0, nullptr, 1203);

	// RC6, scenario 1 -- invalid stream key: outcome=failed, reason_class=auth_rejected.
	assert(strcmp(attempt_reason_class(false, false, -2, "invalid stream key"), "auth_rejected") == 0);
	print_scenario("rc6-invalid-key", "stream", "stream", 1, false, -2, "invalid stream key", 640);

	// RC6, scenario 2 -- ingest unreachable: outcome=failed, reason_class=ingest_unreachable.
	assert(strcmp(attempt_reason_class(false, false, -2, nullptr), "ingest_unreachable") == 0);
	print_scenario("rc6-ingest-unreachable", "PulsarDest_vod01", "vod01", 2, false, -2, nullptr, 5012);

	// A synchronous decline (obs_output_start returns false before any
	// network attempt) is the OBS_OUTPUT_ERROR (-4) sentinel this issue's
	// callers use uniformly; duration_ms is ~0 since no async wait ever
	// started.
	assert(strcmp(attempt_reason_class(false, false, -4, "rtmp_custom: url must be rtmp:// or rtmps://"),
		      "unknown") == 0);
	print_scenario("sync-decline-bad-config", "PulsarDest_xyz789", "xyz789", 1, false, -4,
			"rtmp_custom: url must be rtmp:// or rtmps://", 0);

	// virtualcam is local -- any non-zero code, any text, classifies
	// disconnected_local (mirrors tests/output-classify-probe's own case).
	assert(strcmp(attempt_reason_class(false, true, -1, nullptr), "disconnected_local") == 0);
	print_scenario("vcam-attempt-failed", "virtualcam", "virtualcam", 1, false, -1, nullptr, 12);

	return EXIT_SUCCESS;
}
