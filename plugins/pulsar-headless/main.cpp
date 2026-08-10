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
//
// Phase 6: pulsar-frontend-stub::setup() runs from
// pulsar_frontend_finished_loading() (after plugins are loaded) and
// brings up the encode + record pipeline -- x264 + ffmpeg_aac encoders
// attached to the recording output, a window_capture source on the
// Default scene (target via PULSAR_CAPTURE_WINDOW), and a record path
// resolver under <cwd>/recordings (or PULSAR_RECORD_DIR). v5 clients
// can issue StartRecord / StopRecord and receive a faststart MP4 on
// disk.

#include <obs.h>

#include "pulsar-frontend-stub.h"

#include <QtCore/QByteArray>
#include <QtWidgets/QApplication>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <random>
#include <string>
#include <thread>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mmdeviceapi.h>
#include <functiondiscoverykeys_devpkey.h>
#pragma comment(lib, "ole32.lib")
#endif

namespace {

std::atomic<bool> g_running(true);

// Session credentials negotiated at boot, exposed to the parent
// process via the PULSAR_READY stdout sentinel.
int  g_session_port     = 4455;
std::string g_session_password;

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

// /SUBSYSTEM:WINDOWS support -- since pulsar.exe is built without a
// console subsystem (so Windows never allocates a window for it), the
// CRT does NOT auto-wire stdout/stderr to a terminal. Three cases to
// handle at boot, in priority order:
//
//   1. Parent passed pipe/file handles via STARTUPINFO + bInheritHandles
//      (Prism's child_process.spawn with stdio:'pipe', PowerShell's
//      Start-Process -RedirectStandard*, the test probes that consume
//      stdout). GetStdHandle(STD_OUTPUT_HANDLE) returns a valid
//      pipe/file handle, the CRT already wired stdout to it. Nothing
//      to do.
//
//   2. Direct invocation from cmd.exe / PowerShell.exe / Windows
//      Terminal. No handles inherited, but the parent does own a
//      console. AttachConsole(ATTACH_PARENT_PROCESS) attaches us to
//      it, then freopen("CONOUT$") rebinds stdio to that console so
//      printf / fprintf / std::cout reach the operator's terminal.
//
//   3. Spawned with windowsHide:true and stdio:'ignore' (Prism's
//      production case when it does not pipe stdout). GetStdHandle
//      returns INVALID_HANDLE_VALUE / null, AttachConsole fails
//      (parent has no console). Stdout writes silently no-op. Fine
//      -- nobody is reading.
void wire_stdio_for_windows_subsystem()
{
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    if (hOut && hOut != INVALID_HANDLE_VALUE) {
        DWORD type = GetFileType(hOut);
        if (type == FILE_TYPE_DISK || type == FILE_TYPE_PIPE) {
            // Case 1: parent already wired us up. Don't disturb.
            return;
        }
    }

    if (AttachConsole(ATTACH_PARENT_PROCESS)) {
        // Case 2: hooked into the operator's terminal. Rebind C stdio
        // to the attached console. freopen returns nullptr on failure,
        // which we ignore -- if the rebind fails we silently no-op,
        // matching case 3.
        FILE *unused = nullptr;
        freopen_s(&unused, "CONOUT$", "w", stdout);
        freopen_s(&unused, "CONOUT$", "w", stderr);
    }
    // Case 3 falls through here: no console, no rebind, printf goes nowhere.
}
#endif

// Phase 12a: video pipeline parameters are env-overridable at boot.
//   PULSAR_FPS         -- frames-per-second (default 60). Must be an integer
//                         libobs accepts as fps_num/1.
//   PULSAR_RESOLUTION  -- "<W>x<H>" string (default "1920x1080"). Both base
//                         and output resolution are set to this value;
//                         downscaling is delegated to per-encoder scaling
//                         when needed (Phase 12b+).
// Changing these after obs_reset_video has run requires another reset and
// re-attaching encoders, so they are intentionally fixed at boot.
bool reset_video()
{
    obs_video_info ovi = {};
    ovi.graphics_module = "libobs-d3d11.dll";

    int fps = 60;
    if (const char *e = std::getenv("PULSAR_FPS"); e && *e) {
        int v = std::atoi(e);
        if (v == 24 || v == 30 || v == 48 || v == 60 || v == 120)
            fps = v;
        else
            std::fprintf(stderr, "pulsar-headless: PULSAR_FPS=%s rejected; using %d\n", e, fps);
    }
    ovi.fps_num = fps;
    ovi.fps_den = 1;

    int width = 1920, height = 1080;
    if (const char *e = std::getenv("PULSAR_RESOLUTION"); e && *e) {
        int w = 0, h = 0;
        if (std::sscanf(e, "%dx%d", &w, &h) == 2 && w > 0 && h > 0 &&
            w <= 7680 && h <= 4320) {
            width = w; height = h;
        } else {
            std::fprintf(stderr, "pulsar-headless: PULSAR_RESOLUTION=%s rejected; using %dx%d\n",
                         e, width, height);
        }
    }
    ovi.base_width = width;
    ovi.base_height = height;
    ovi.output_width = width;
    ovi.output_height = height;
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
    std::fprintf(stdout, "pulsar-headless: video %dx%d @ %d fps\n", width, height, fps);
    return true;
}

#ifdef _WIN32
// Resolves the CONCRETE endpoint id of the system's current default
// playback device (same COM call libobs's own device-enumeration backend
// uses internally, see audio-monitoring/win32/wasapi-enum-devices.c
// get_default_id()) -- deliberately NOT the "default" sentinel string.
//
// Every WASAPI-backed capture source (mic / desktop-audio-device / a
// captured app's audio -- ZabCapture:* sources all resolve to one of
// these three plugin types) defaults its own `device_id` setting to the
// literal string "default" unless something explicitly overrides it
// (win-wasapi.cpp: obs_data_set_default_string(..., "default")), and all
// three carry OBS_SOURCE_DO_NOT_SELF_MONITOR. That flag's guard
// (audio-monitoring/win32/wasapi-output.c audio_monitor_init) compares
// the SOURCE's own device_id against the bound MONITORING device id by
// exact string match; if both sides are literally "default" it treats
// them as the same physical device and silently disables that source's
// monitor to avoid a feedback loop -- correct instinct for a microphone
// captured then monitored back into a device that could pick it up
// again, a false positive for every other ZabCapture source (capturing
// an app's or the desktop's audio and playing it back in the operator's
// headset is never a feedback loop). No error surfaces either way:
// SetInputAudioMonitorType still succeeds and reads back "monitor" --
// the antenna/stream mix is unaffected (a fully separate code path), only
// the local headphone tap goes silent.
//
// Binding monitoring to the RESOLVED id instead breaks that accidental
// string collision -- a source's own device_id stays "default" (still
// correctly self-monitor-guarded against an actual mic loop), while the
// monitoring device is now a concrete GUID that never matches it. Trade-
// off accepted: this stops dynamically re-tracking the OS default if the
// operator changes it without restarting Pulsar -- resolved once at
// boot, same as the fixed 1080p30 video profile below.
std::string resolve_default_render_device_id()
{
    std::string result;
    HRESULT co_hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    bool co_owned = SUCCEEDED(co_hr);
    if (co_hr != RPC_E_CHANGED_MODE && FAILED(co_hr)) {
        std::fprintf(stderr, "pulsar-headless: CoInitializeEx failed (0x%08lx)\n",
                     static_cast<unsigned long>(co_hr));
        return result;
    }

    IMMDeviceEnumerator *enumerator = nullptr;
    IMMDevice *device = nullptr;
    LPWSTR w_id = nullptr;

    HRESULT hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                                   __uuidof(IMMDeviceEnumerator),
                                   reinterpret_cast<void **>(&enumerator));
    if (SUCCEEDED(hr)) {
        hr = enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &device);
    }
    if (SUCCEEDED(hr)) {
        hr = device->GetId(&w_id);
    }
    if (SUCCEEDED(hr) && w_id) {
        int len = WideCharToMultiByte(CP_UTF8, 0, w_id, -1, nullptr, 0, nullptr, nullptr);
        if (len > 0) {
            std::string utf8(static_cast<size_t>(len) - 1, '\0');
            WideCharToMultiByte(CP_UTF8, 0, w_id, -1, utf8.data(), len, nullptr, nullptr);
            result = std::move(utf8);
        }
    }

    if (w_id)
        CoTaskMemFree(w_id);
    if (device)
        device->Release();
    if (enumerator)
        enumerator->Release();
    if (co_owned)
        CoUninitialize();

    if (result.empty()) {
        std::fprintf(stderr,
                     "pulsar-headless: could not resolve the default playback device id (0x%08lx)\n",
                     static_cast<unsigned long>(hr));
    }
    return result;
}
#endif

bool reset_audio()
{
    obs_audio_info oai = {};
    oai.samples_per_sec = 48000;
    oai.speakers = SPEAKERS_STEREO;

    if (!obs_reset_audio(&oai)) {
        std::fprintf(stderr, "pulsar-headless: obs_reset_audio failed\n");
        return false;
    }

    // Bind the system's DEFAULT playback device as the audio monitoring
    // output, by its RESOLVED CONCRETE id -- never the "default" sentinel
    // string. See resolve_default_render_device_id() above for why: every
    // WASAPI-backed ZabCapture source's own device_id also defaults to
    // that same literal "default", and OBS_SOURCE_DO_NOT_SELF_MONITOR
    // treats an exact string match as "this is the same physical device,
    // refuse to avoid a feedback loop" -- true for a microphone, a false
    // positive for every captured-app/desktop-audio source, and it fails
    // silently (SetInputAudioMonitorType still reports success). Without
    // ANY monitoring device bound at all, libobs accepts
    // SetInputAudioMonitorType too but never routes audio anywhere --
    // hence resolving a real id is required either way, not optional.
    // Falls back to the "default" sentinel (the original, narrower
    // behaviour) only if resolution itself fails, so monitoring still
    // works for at least the mic case rather than not at all.
    if (obs_audio_monitoring_available()) {
        std::string resolved_id;
#ifdef _WIN32
        resolved_id = resolve_default_render_device_id();
#endif
        if (!resolved_id.empty()) {
            obs_set_audio_monitoring_device("Default", resolved_id.c_str());
            std::fprintf(stdout, "pulsar-headless: audio monitoring device bound (resolved default, id=%s)\n",
                         resolved_id.c_str());
        } else {
            obs_set_audio_monitoring_device("Default", "default");
            std::fprintf(stdout,
                         "pulsar-headless: audio monitoring device bound (default sentinel -- id resolution "
                         "failed, ZabCapture sources will self-monitor-guard silent)\n");
        }
    } else {
        std::fprintf(stderr, "pulsar-headless: audio monitoring unavailable on this platform build\n");
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

// V1 session boundary: each spawn gets its own port + password, never
// trusting the persisted config.json. PULSAR_PORT and PULSAR_PASSWORD
// override the defaults; if either is empty, we pick safe values
// (4455 for port, a fresh 22-char URL-safe random string for password).
//
// Then we PRE-WRITE obs-websocket/config.json before obs_load_all_modules
// so the obs-websocket plugin loads our values rather than whatever was
// on disk from a prior session. This makes every boot reproducible from
// the parent process's point of view.
std::string generate_session_password(std::size_t len = 22)
{
    static const char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789-_";
    std::random_device rd;
    std::mt19937_64 rng(((uint64_t)rd() << 32) ^ rd());
    std::uniform_int_distribution<size_t> pick(0, sizeof(alphabet) - 2);
    std::string out;
    out.reserve(len);
    for (std::size_t i = 0; i < len; ++i)
        out.push_back(alphabet[pick(rng)]);
    return out;
}

void seed_websocket_config()
{
    if (const char *e = std::getenv("PULSAR_PORT"); e && *e) {
        int v = std::atoi(e);
        if (v > 0 && v < 65536) {
            g_session_port = v;
        } else {
            std::fprintf(stderr,
                         "pulsar-headless: PULSAR_PORT=%s rejected; using %d\n",
                         e, g_session_port);
        }
    }

    if (const char *e = std::getenv("PULSAR_PASSWORD"); e && *e) {
        g_session_password = e;
    } else {
        g_session_password = generate_session_password();
    }

    // obs-websocket persists its config under <cwd>/obs-websocket/
    // when running headless (no profile path). pulsar.exe is always
    // spawned with cwd=bin/64bit (documented in PRISM-EMBEDDING.md),
    // so that resolves to bin/64bit/obs-websocket/config.json --
    // exactly where the plugin will look during obs_module_load.
    namespace fs = std::filesystem;
    std::error_code ec;
    fs::create_directories("obs-websocket", ec);
    if (ec) {
        std::fprintf(stderr,
                     "pulsar-headless: could not create obs-websocket/ dir: %s\n",
                     ec.message().c_str());
        return;
    }

    std::ofstream out("obs-websocket/config.json", std::ios::binary | std::ios::trunc);
    if (!out) {
        std::fprintf(stderr,
                     "pulsar-headless: could not write obs-websocket/config.json\n");
        return;
    }
    // Hand-rolled JSON -- the only consumer is obs-websocket itself
    // and the password is already constrained to URL-safe charset
    // so no escaping is needed.
    out << "{\n"
        << "  \"alerts_enabled\": false,\n"
        << "  \"auth_required\": true,\n"
        << "  \"first_load\": false,\n"
        << "  \"server_enabled\": true,\n"
        << "  \"server_password\": \"" << g_session_password << "\",\n"
        << "  \"server_port\": " << g_session_port << "\n"
        << "}\n";
}

} // namespace

int main(int argc, char **argv)
{
#ifdef _WIN32
    wire_stdio_for_windows_subsystem();
#endif

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

    // Seed obs-websocket's config.json before plugins load so the
    // pulsar-websocket fork picks up our session port + password
    // rather than the persisted values from a previous run.
    seed_websocket_config();

    load_modules();

    // FINISHED_LOADING is the trigger obs-websocket waits for before
    // accepting requests/events on the wire. Emit it once modules are
    // post-loaded.
    pulsar_frontend_finished_loading();

#ifdef _WIN32
    SetConsoleCtrlHandler(console_ctrl_handler, TRUE);
#endif

    // PULSAR_READY sentinel. The parent process (Prism, CI probes,
    // operators) reads stdout line-by-line, picks up this marker,
    // parses port + password, and uses them to authenticate the
    // obs-websocket session. Documented in docs/PRISM-EMBEDDING.md.
    std::printf("PULSAR_READY ws=ws://127.0.0.1:%d password=%s\n",
                g_session_port, g_session_password.c_str());
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
