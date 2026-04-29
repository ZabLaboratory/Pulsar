// Pulsar headless service entry point.
//
// Phase 3b: minimum viable proof of headless libobs. Calls
// obs_startup, prints the OBS version (which carries our -pulsar
// suffix from patch 0001), then obs_shutdown and exits.
//
// Phase 4+ extends this with: video/audio reset, plugin loading,
// signal-driven shutdown loop, websocket server, etc.

#include <obs.h>

#include <cstdio>

int main(int argc, char **argv)
{
    (void)argc;
    (void)argv;

    if (!obs_startup("en-US", nullptr, nullptr)) {
        std::fprintf(stderr, "pulsar-headless: obs_startup failed\n");
        return 1;
    }

    std::printf("pulsar-headless: libobs %s initialised\n",
                obs_get_version_string());

    obs_shutdown();

    return 0;
}
