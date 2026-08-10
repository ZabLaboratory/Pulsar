// pulsar-output-classify -- header-only, no DLL of its own.
//
// Single source of truth for the closed `reason_class` set of ADR-005 §3.4
// (`pulsar:OutputFailed`). Header-only for the same reason as
// pulsar-nv-secure-load: its two consumers live in two different binaries --
// pulsar-frontend-stub (a static lib linked into pulsar-headless.exe, no
// obs-websocket-api dependency) and pulsar-multi-stream (the DLL that owns
// the "pulsar" obs-websocket vendor) -- and an INTERFACE target is the only
// way to give both an identical classifier without either depending on the
// other's translation unit. R4 (ADR-005 §5) is exactly the risk this file
// exists to close: two hand-copies of this logic would drift, and a drifted
// class is worse than none -- it reintroduces the log-scraping the ADR is
// meant to end.
//
// The mapping from (is_local_output, code, last_error) to a class is a
// deliberate reading of libobs' obs-outputs/rtmp-stream.c signal_stop paths
// (see docs/adr/005-go-live-failure-diagnosability.md §3.4 and the
// correspondence table in the issue #182 PR description) -- not a guarantee
// upstream never changes. `unknown` is the explicit, legitimate fallback:
// approximating a class here is the failure mode R4 warns against.

#pragma once

#include <cctype>
#include <cstring>

namespace pulsar {

// Case-insensitive substring search. last_error strings observed here come
// from libobs / librtmp / ffmpeg, all ASCII -- no need for anything more.
inline bool output_classify_contains_ci(const char *haystack, const char *needle)
{
	if (!haystack || !needle || !*needle)
		return false;
	size_t nlen = std::strlen(needle);
	for (const char *p = haystack; *p; ++p) {
		size_t i = 0;
		while (i < nlen && p[i] && std::tolower(static_cast<unsigned char>(p[i])) ==
					       std::tolower(static_cast<unsigned char>(needle[i])))
			++i;
		if (i == nlen)
			return true;
	}
	return false;
}

// Returns nullptr when the stop is NOT a failure -- OBS_OUTPUT_SUCCESS on a
// network-capable output is a client-requested or delay-drained graceful
// stop (ADR-005 §3.4: "quitte l'état actif autrement que sur demande"), so no
// pulsar:OutputFailed is emitted for it (RC7).
//
// is_local_output distinguishes outputs with no ingest/auth/network surface
// at all (virtualcam) from network-capable ones (RTMP stream, RTMP
// destinations). A local output can only fail for a reason local to this
// machine, so any non-success code on it is `disconnected_local` --
// unambiguous, because there is no ingest for it to have rejected or
// dropped.
inline const char *classify_output_failure(bool is_local_output, int code, const char *last_error)
{
	if (code == 0 /* OBS_OUTPUT_SUCCESS */)
		return nullptr;

	if (is_local_output)
		return "disconnected_local";

	// last_error text wins over the numeric code for auth: Twitch (and the
	// same signatures Prism/src/main/broadcast-url.ts already scans for)
	// accepts the RTMP connection, then rejects the stream key at the
	// application layer -- the code alone (-2 or -3 depending on which
	// leg failed) does not distinguish that from a transient ingest
	// flap, but the text always does.
	if (output_classify_contains_ci(last_error, "unauthor") ||
	    output_classify_contains_ci(last_error, "invalid key") ||
	    output_classify_contains_ci(last_error, "invalid stream key") ||
	    output_classify_contains_ci(last_error, "authenticat") ||
	    output_classify_contains_ci(last_error, "403"))
		return "auth_rejected";

	switch (code) {
	case -1: // OBS_OUTPUT_BAD_PATH -- rejected before any network attempt.
		return "config_rejected";
	case -2: // OBS_OUTPUT_CONNECT_FAILED -- no connection ever established.
		return "ingest_unreachable";
	case -3: // OBS_OUTPUT_INVALID_STREAM -- RTMP connect succeeded, the
		 // server then refused createStream/publish before any frame
		 // flowed. Established-then-refused reads closer to
		 // ingest_dropped than to ingest_unreachable in this closed
		 // set; flagged as a judgment call in the PR, not a certainty.
		return "ingest_dropped";
	case -5: // OBS_OUTPUT_DISCONNECTED -- established, then lost mid-stream
		 // (obs-output.c's reconnect path sets exactly this code).
		return "ingest_dropped";
	case -6: // OBS_OUTPUT_UNSUPPORTED
	case -9: // OBS_OUTPUT_HDR_DISABLED
		return "config_rejected";
	case -8: // OBS_OUTPUT_ENCODE_ERROR
		return "encoder_failed";
	default:
		// -4 (OBS_OUTPUT_ERROR, generic), -7 (OBS_OUTPUT_NO_SPACE, a
		// resource condition this closed set has no class for) and
		// anything else land here on purpose -- see the file header.
		return "unknown";
	}
}

} // namespace pulsar
