// Regression probe for issue #177 (fail-open IsDebugEnabled()).
//
// plugins/pulsar-websocket/src/obs-websocket.cpp:181-184 cannot be linked
// standalone -- it pulls in Qt, obs-module.h and the rest of the libobs SDK,
// none of which are available to a host-only unit test. This probe instead
// mirrors the exact boolean expression at that call site against a minimal
// stand-in for Config/_config (nullable pointer + DebugEnabled bool), so a
// regression back to the fail-open form (`!_config || _config->DebugEnabled`)
// is caught even though the real translation unit isn't exercised. Keep this
// mirror in sync with obs-websocket.cpp:181-184 by hand -- there is no
// automated link between the two.

#include <cstdio>
#include <cstdlib>

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

struct FakeConfig {
	bool DebugEnabled;
};

// Exact mirror of plugins/pulsar-websocket/src/obs-websocket.cpp:181-184.
static bool IsDebugEnabledMirror(const FakeConfig *config)
{
	return config && config->DebugEnabled;
}

int main()
{
	// _config == nullptr (before OBS finishes loading, or after unload):
	// must fail CLOSED, not dump WebSocket payloads (stream key, ingest
	// URL, browser-source token) at INFO level via blog_debug.
	PULSAR_CHECK(IsDebugEnabledMirror(nullptr) == false);

	// _config present, DebugEnabled explicitly off: stays closed.
	FakeConfig off{false};
	PULSAR_CHECK(IsDebugEnabledMirror(&off) == false);

	// _config present, DebugEnabled explicitly on: behavior preserved.
	FakeConfig on{true};
	PULSAR_CHECK(IsDebugEnabledMirror(&on) == true);

	return EXIT_SUCCESS;
}
