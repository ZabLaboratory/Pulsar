// Pulsar -- v5 stream-egress gate (shared predicate).
//
// WHY THIS FILE EXISTS
// --------------------
// Issue #131 bound the frontend stub's `streamService` to `streamOutput`, which
// made the v5 `SetStreamServiceSettings` + `StartStream` path a LIVE egress for
// the first time. Before that binding the path was dead, and #114 relied on that
// deadness: the stub creates its boot placeholder as `rtmp_common` / "Twitch"
// (pulsar-frontend-stub.cpp, setup()), and upstream resolves an rtmp_common
// Twitch service through `update_ingest` (upstream/plugins/rtmp-services/
// rtmp-common.c) -- which falls back to the bundled default
// `rtmp://live.twitch.tv/app` (upstream/plugins/rtmp-services/service-specific/
// twitch.c:45) whenever the ingest list is missing: first run, cold cache, or
// offline. CLEARTEXT. The stream key would travel unencrypted.
//
// The multi-stream twin (`pulsar:StartDestination`) cannot do this: its Twitch
// ingest is a compile-time constant guarded by a `static_assert` on the
// `rtmps://` scheme (pulsar-multi-stream/src/plugin-main.cpp:85-95), and it
// front-loads `is_rtmp_scheme()` + non-empty-key validation before any
// obs_output_* allocation (plugin-main.cpp:100-121).
//
// THE RULE (C1, form (b)): Twitch is barred from the v5 single-stream path.
// Twitch egress goes through `pulsar:StartDestination`, which carries the
// compile-time RTMPS guarantee. The v5 path stays alive for `rtmp_custom` and
// non-Twitch services -- that is the Stream Deck / Companion compatibility #131
// exists for -- but it never resolves an ingest URL out of a downloaded list we
// do not control.
//
// Form (b) was chosen over (a) "refuse any resolved rtmp:// URL" and (c) "patch
// twitch.c in the submodule":
//   - (a) would either break parity with the twin (which deliberately ACCEPTS
//     `rtmp://` for operator-supplied rtmp_custom endpoints, e.g. a LAN relay)
//     or force a second, divergent scheme policy on the same product.
//   - (c) puts a Pulsar security invariant inside a vendored upstream file, i.e.
//     re-litigated at every submodule bump and invisible to anyone reading
//     Pulsar's own sources. The invariant belongs where the egress is decided.
// Form (b) keeps ONE rule -- "Twitch egress is the multi-stream plugin's job" --
// enforced in Pulsar's own code, at both the configuration and start seams.
//
// THE PARITY (C2): the same front-loaded validation the twin applies --
// `rtmp://`/`rtmps://` scheme and non-empty stream key -- is applied here.
// A newly live egress path must not be more permissive than its twin.
//
// Header-only on purpose: pulsar-frontend-stub (static lib) and pulsar-websocket
// (plugin dll) are separate link units and must not diverge on this predicate.

#pragma once

#include <obs.h>
#include <obs.hpp> // OBSDataAutoRelease

#include <cstring>
#include <string>

namespace pulsar {

// One wording for the C1 refusal, shared by the configuration seam
// (SetStreamServiceSettings) and the start seam (obs_frontend_streaming_start)
// so the two can never drift into telling the operator different stories.
//
// SCOPE -- read the wording narrowly: this gate closes the `rtmp_common`/Twitch
// path, i.e. an ingest RESOLVED for us out of a downloaded list that can silently
// degrade to cleartext. It does NOT close the general class "a Twitch key in
// cleartext": `SetStreamServiceSettings{rtmp_custom, server "rtmp://live.twitch.tv/app",
// key ...}` still passes every guard here -- it is not rtmp_common, `rtmp://` is
// accepted by the deliberate parity with `pulsar:StartDestination` (which accepts
// operator-supplied rtmp:// endpoints), and the key is non-empty. That residual is
// the accepted one of ADR 010 section 5: an operator knowingly typing a cleartext
// URL is the assumed rtmp_custom use. Pulsar cannot tell a Twitch key from any
// other key on that generic path; that guard lives in Prism (R1), not here.
inline constexpr const char *kTwitchOnV5Refusal =
	"the Twitch service is not available on the v5 single-stream path: an rtmp_common "
	"Twitch service resolves its ingest from a downloaded list and falls back to the "
	"CLEARTEXT rtmp://live.twitch.tv/app when that list is absent. Stream to Twitch with "
	"the pulsar:StartDestination multi-stream API, which pins an rtmps:// ingest at "
	"compile time.";

// True if url starts with "rtmp://" or "rtmps://".
// Mirrors pulsar-multi-stream/src/plugin-main.cpp `is_rtmp_scheme`.
inline bool IsRtmpScheme(const char *url)
{
	if (!url)
		return false;
	return std::strncmp(url, "rtmp://", 7) == 0 || std::strncmp(url, "rtmps://", 8) == 0;
}

inline bool EqualsIgnoreCase(const char *a, const char *b)
{
	if (!a || !b)
		return false;
	while (*a && *b) {
		char ca = *a++, cb = *b++;
		if (ca >= 'A' && ca <= 'Z')
			ca = static_cast<char>(ca - 'A' + 'a');
		if (cb >= 'A' && cb <= 'Z')
			cb = static_cast<char>(cb - 'A' + 'a');
		if (ca != cb)
			return false;
	}
	return *a == '\0' && *b == '\0';
}

// C1 (b). An `rtmp_common` service whose "service" setting names Twitch is the
// one that resolves through update_ingest -> twitch.c's cleartext default.
// Identified from the SETTINGS, not from the resolved URL: the resolution is
// exactly what we refuse to depend on.
inline bool IsTwitchCommonService(const char *serviceType, obs_data_t *settings)
{
	if (!serviceType || std::strcmp(serviceType, "rtmp_common") != 0)
		return false;
	if (!settings)
		return false;
	return EqualsIgnoreCase(obs_data_get_string(settings, "service"), "Twitch");
}

// The gate. `errOut` always carries a NAMED cause on refusal -- it is what the
// v5 client is answered with (RequestHandler_Config.cpp) and what
// obs_output_set_last_error carries into DescribeOutputRefusal
// (pulsar-frontend-stub.cpp -> OutputEffect.h).
//
// The word "service" appears in every message on purpose: scripts/
// probe-output-effect.py asserts the refusal names the service as the cause.
inline bool ValidateStreamServiceEgress(obs_service_t *service, std::string &errOut)
{
	if (!service) {
		errOut = "no streaming service is configured.";
		return false;
	}

	const char *type = obs_service_get_type(service);
	OBSDataAutoRelease settings = obs_service_get_settings(service);

	if (IsTwitchCommonService(type, settings)) {
		errOut = kTwitchOnV5Refusal;
		return false;
	}

	const char *url = obs_service_get_connect_info(service, OBS_SERVICE_CONNECT_INFO_SERVER_URL);
	if (!IsRtmpScheme(url)) {
		errOut = std::string("the streaming service resolves to ") +
			 (url && *url ? std::string("\"") + url + "\"" : std::string("an empty URL")) +
			 ", which is not an rtmp:// or rtmps:// endpoint.";
		return false;
	}

	const char *key = obs_service_get_connect_info(service, OBS_SERVICE_CONNECT_INFO_STREAM_KEY);
	if (!key || !*key) {
		errOut = "the streaming service has no stream key.";
		return false;
	}

	return true;
}

} // namespace pulsar
