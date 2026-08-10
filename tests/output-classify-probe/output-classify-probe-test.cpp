// Regression probe for issue #182 / ADR-005 §3.4 -- the closed reason_class
// set behind pulsar:OutputFailed. Pure logic, no libobs/Qt/OBS runtime
// needed (same rationale as tests/ws-debug-probe): what's under test is a
// pure function of (is_local_output, code, last_error).
//
// Each case below also prints the JSON payload pulsar:OutputFailed would
// carry for that scenario (output/phase/code/last_error/reason_class),
// session left empty per the issue's owned scope -- this is the "charges
// JSON par scenario" evidence the issue asks for, captured from a real,
// compiled, executed classification rather than hand-written by hand.

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "pulsar-output-classify.h"

// assert() is a no-op under NDEBUG (RelWithDebInfo CI build): expr is never
// evaluated, so this probe would silently "pass" everything. PULSAR_CHECK
// always evaluates expr and fails hard, independent of NDEBUG (issue #220).
#define PULSAR_CHECK(expr)                                                                  \
	do {                                                                                  \
		if (!(expr)) {                                                               \
			fprintf(stderr, "CHECK FAILED: %s (%s:%d)\n", #expr, __FILE__, __LINE__); \
			exit(EXIT_FAILURE);                                                  \
		}                                                                             \
	} while (0)

using pulsar::classify_output_failure;

static void print_scenario(const char *name, const char *output, const char *phase, int code, const char *last_error)
{
	const char *cls = classify_output_failure(strcmp(output, "virtualcam") == 0, code, last_error);
	printf("{\"scenario\":\"%s\",\"output\":\"%s\",\"phase\":\"%s\",\"code\":%d,"
	       "\"last_error\":\"%s\",\"reason_class\":%s%s%s}\n",
	       name, output, phase, code, last_error ? last_error : "",
	       cls ? "\"" : "", cls ? cls : "null", cls ? "\"" : "");
}

int main()
{
	// RC7 -- a client-requested (or delay-drained graceful) stop is code 0
	// on a network-capable output: no event, on ANY output kind.
	PULSAR_CHECK(classify_output_failure(false, 0, nullptr) == nullptr);
	PULSAR_CHECK(classify_output_failure(true, 0, nullptr) == nullptr);
	print_scenario("client-requested-stop", "stream", "active", 0, nullptr);

	// auth_rejected -- last_error text wins over the numeric code, matching
	// PERSISTENT_RTMP_SIGNATURES in Prism/src/main/broadcast-url.ts.
	PULSAR_CHECK(strcmp(classify_output_failure(false, -2, "Authentication failed"), "auth_rejected") == 0);
	PULSAR_CHECK(strcmp(classify_output_failure(false, -3, "unauthorized"), "auth_rejected") == 0);
	PULSAR_CHECK(strcmp(classify_output_failure(false, -2, "invalid stream key"), "auth_rejected") == 0);
	PULSAR_CHECK(strcmp(classify_output_failure(false, -2, "403 Forbidden"), "auth_rejected") == 0);
	print_scenario("twitch-key-rejected", "stream", "active", -2, "Authentication failed");

	// ingest_unreachable -- OBS_OUTPUT_CONNECT_FAILED, no auth text.
	PULSAR_CHECK(strcmp(classify_output_failure(false, -2, nullptr), "ingest_unreachable") == 0);
	print_scenario("ingest-unreachable", "PulsarDest_abc123", "active", -2, nullptr);

	// ingest_dropped -- OBS_OUTPUT_INVALID_STREAM (connect ok, stream
	// refused) and OBS_OUTPUT_DISCONNECTED (dropped mid-stream) both land
	// here; see the header comment for the -3 judgment call.
	PULSAR_CHECK(strcmp(classify_output_failure(false, -3, nullptr), "ingest_dropped") == 0);
	PULSAR_CHECK(strcmp(classify_output_failure(false, -5, nullptr), "ingest_dropped") == 0);
	print_scenario("ingest-dropped-midstream", "stream", "active", -5, nullptr);

	// encoder_failed -- OBS_OUTPUT_ENCODE_ERROR.
	PULSAR_CHECK(strcmp(classify_output_failure(false, -8, nullptr), "encoder_failed") == 0);
	print_scenario("encoder-failed", "stream", "active", -8, nullptr);

	// config_rejected -- OBS_OUTPUT_BAD_PATH / UNSUPPORTED / HDR_DISABLED,
	// all synchronous, pre-network refusals.
	PULSAR_CHECK(strcmp(classify_output_failure(false, -1, nullptr), "config_rejected") == 0);
	PULSAR_CHECK(strcmp(classify_output_failure(false, -6, nullptr), "config_rejected") == 0);
	PULSAR_CHECK(strcmp(classify_output_failure(false, -9, nullptr), "config_rejected") == 0);
	print_scenario("config-rejected-bad-path", "PulsarDest_abc123", "start", -1, nullptr);

	// disconnected_local -- any non-success code on a local (no
	// ingest/auth surface) output, regardless of what the code/text say.
	PULSAR_CHECK(strcmp(classify_output_failure(true, -8, "should not matter"), "disconnected_local") == 0);
	PULSAR_CHECK(strcmp(classify_output_failure(true, -1, nullptr), "disconnected_local") == 0);
	print_scenario("virtualcam-local-stop", "virtualcam", "active", -4, nullptr);

	// unknown -- explicit, legitimate fallback (R4): OBS_OUTPUT_ERROR
	// (generic) and OBS_OUTPUT_NO_SPACE (a resource condition this closed
	// set has no class for) both land here, last_error joined verbatim.
	PULSAR_CHECK(strcmp(classify_output_failure(false, -4, nullptr), "unknown") == 0);
	PULSAR_CHECK(strcmp(classify_output_failure(false, -7, "No space left on device"), "unknown") == 0);
	print_scenario("unknown-no-space", "PulsarDest_vod01", "active", -7, "No space left on device");

	return EXIT_SUCCESS;
}
