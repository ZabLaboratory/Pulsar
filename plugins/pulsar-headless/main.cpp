// Pulsar headless service entry point.
//
// Phase 4a: real long-running service. Boots libobs with default
// 1080p30 video + 48 kHz stereo audio, loads every plugin libobs
// can find under the rundir layout, then idles waiting for a
// console-control event (Ctrl+C / window close / system shutdown)
// to flip the running flag and trigger graceful obs_shutdown.
//
// Phase 4b will add the websocket plugin so external clients can
// drive this service. Phase 4c validates the protocol round-trip.

#include <obs.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <thread>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif

namespace {

std::atomic<bool> g_running(true);

#ifdef _WIN32
BOOL WINAPI console_ctrl_handler(DWORD ctrl_type)
{
    switch (ctrl_type) {
    case CTRL_C_EVENT:
    case CTRL_BREAK_EVENT:
    case CTRL_CLOSE_EVENT:
    case CTRL_LOGOFF_EVENT:
    case CTRL_SHUTDOWN_EVENT:
        g_running.store(false, std::memory_order_release);
        return TRUE;
    default:
        return FALSE;
    }
}
#endif

bool reset_video()
{
    obs_video_info ovi = {};
    ovi.graphics_module = "libobs-d3d11.dll";
    ovi.fps_num = 30;
    ovi.fps_den = 1;
    ovi.base_width = 1920;
    ovi.base_height = 1080;
    ovi.output_width = 1920;
    ovi.output_height = 1080;
    ovi.output_format = VIDEO_FORMAT_NV12;
    ovi.colorspace = VIDEO_CS_DEFAULT;
    ovi.range = VIDEO_RANGE_DEFAULT;
    ovi.adapter = 0;
    ovi.gpu_conversion = true;
    ovi.scale_type = OBS_SCALE_BICUBIC;

    int result = obs_reset_video(&ovi);
    if (result != OBS_VIDEO_SUCCESS) {
        std::fprintf(stderr,
                     "pulsar-headless: obs_reset_video failed (%d)\n",
                     result);
        return false;
    }
    return true;
}

bool reset_audio()
{
    obs_audio_info oai = {};
    oai.samples_per_sec = 48000;
    oai.speakers = SPEAKERS_STEREO;

    if (!obs_reset_audio(&oai)) {
        std::fprintf(stderr, "pulsar-headless: obs_reset_audio failed\n");
        return false;
    }
    return true;
}

void load_modules()
{
    // libobs's `add_default_module_paths()` (obs-windows.c:43) already
    // registers ../../obs-plugins/64bit/ and ../../data/obs-plugins/
    // %module%/ relative to the running binary on Windows. Calling
    // `obs_add_module_path` ourselves with the same paths would
    // register a second entry, double-loading every module and
    // emitting "obs_register_*: id 'X' already exists! Duplicate
    // library?" warnings. Trust the defaults.
    obs_load_all_modules();
    obs_post_load_modules();
}

} // namespace

int main(int argc, char **argv)
{
    (void)argc;
    (void)argv;

    if (!obs_startup("en-US", nullptr, nullptr)) {
        std::fprintf(stderr, "pulsar-headless: obs_startup failed\n");
        return 1;
    }

    if (!reset_video()) {
        obs_shutdown();
        return 1;
    }

    if (!reset_audio()) {
        obs_shutdown();
        return 1;
    }

    load_modules();

#ifdef _WIN32
    SetConsoleCtrlHandler(console_ctrl_handler, TRUE);
#endif

    std::printf("pulsar-headless: libobs %s ready, idling (Ctrl+C to exit)\n",
                obs_get_version_string());
    std::fflush(stdout);

    while (g_running.load(std::memory_order_acquire)) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    std::printf("pulsar-headless: shutting down\n");
    std::fflush(stdout);

    obs_shutdown();

    return 0;
}
