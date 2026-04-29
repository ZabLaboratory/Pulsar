// Pulsar headless service entry point.
//
// Phase 4a: real long-running service. Boots libobs with default
// 1080p30 video + 48 kHz stereo audio, loads every plugin libobs
// can find under the rundir layout, then idles waiting for a
// console-control event (Ctrl+C / window close / system shutdown)
// to flip the running flag and trigger graceful obs_shutdown.
//
// Phase 4b/c/d: Qt minimal platform + obs-websocket plugin loaded so
// external clients drive the service over WebSocket on port 4455.
//
// Phase 5: pulsar-frontend-stub installs an obs_frontend_callbacks
// implementation BEFORE obs_load_all_modules so plugins like
// obs-websocket find a populated frontend table when they register
// their event callbacks.

#include <obs.h>

#include "pulsar-frontend-stub.h"

#include <QtCore/QByteArray>
#include <QtWidgets/QApplication>

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
    // Force the offscreen Qt platform so QApplication can construct
    // without a display server / platform plugin DLL. obs-websocket
    // (and other libobs plugins) link against Qt6 and assume a
    // QApplication exists; in headless mode we never show a widget,
    // but the QApplication instance is still required for
    // QObject/QString/QJson machinery to work.
    qputenv("QT_QPA_PLATFORM", QByteArrayLiteral("minimal"));

    QApplication qt_app(argc, argv);

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

    // Install the frontend callback table BEFORE loading plugins so
    // obs-websocket's EventHandler (which calls
    // obs_frontend_add_event_callback inside obs_module_load) finds a
    // valid frontend and its callback registers correctly.
    pulsar_frontend_init();

    load_modules();

    // FINISHED_LOADING is the trigger obs-websocket waits for before
    // accepting requests/events on the wire. Emit it once modules are
    // post-loaded.
    pulsar_frontend_finished_loading();

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

    pulsar_frontend_shutdown();
    obs_shutdown();

    return 0;
}
