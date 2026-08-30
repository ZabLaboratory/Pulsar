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

#include "dir-hardening.h"
#include "log-handler.h"
#include "pulsar-frontend-stub.h"
#include "runtime-identity.h"

#include <QtCore/QByteArray>
#include <QtWidgets/QApplication>

#include <atomic>
#include <chrono>
#include <cctype>
#include <cstdarg>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <memory>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mmdeviceapi.h>
#include <functiondiscoverykeys_devpkey.h>
#pragma comment(lib, "ole32.lib")
#endif

namespace {

std::atomic<bool> g_running(true);

// #243: one process owns one runtime namespace. The identity and cwd locks
// protect config/log/recording and other process-local resources; the third
// lock is the compatibility lease for the historical DirectShow
// Program/Preview aliases. All are kernel-backed and therefore recover when
// the process exits unexpectedly.
struct RuntimeState {
    pulsar_runtime::RuntimeIdentity identity;
    pulsar_runtime::ExclusiveLease instance_lease;
    pulsar_runtime::ExclusiveLease runtime_dir_lease;
    pulsar_runtime::ExclusiveLease legacy_alias_lease;
    bool legacy_alias = false;
    std::string alias_state = "disabled";

    bool initialize();
    bool renew();
    void release();
};

std::string lower_ascii(std::string value)
{
    for (char &c : value)
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return value;
}

bool port_is_valid(const char *value)
{
    if (!value || !*value)
        return false;
    char *end = nullptr;
    const long parsed = std::strtol(value, &end, 10);
    return end && *end == '\0' && parsed > 0 && parsed < 65536;
}

bool RuntimeState::initialize()
{
    std::string error;
    if (!pulsar_runtime::resolve_identity(identity, error)) {
        std::fprintf(stderr, "PULSAR_RUNTIME_ERROR code=invalid_identity reason=%s\n",
                     error.c_str());
        return false;
    }

    std::error_code ec;
    std::filesystem::create_directories(identity.runtime_dir, ec);
    if (ec || !std::filesystem::is_directory(identity.runtime_dir, ec) || ec) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_ERROR code=runtime_dir_unavailable id=%s dir=%s reason=%s\n",
                     identity.instance_id.c_str(), identity.runtime_dir.string().c_str(),
                     ec ? ec.message().c_str() : "not_a_directory");
        return false;
    }
    std::filesystem::create_directories(identity.instance_lease_path.parent_path(), ec);
    if (ec) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_ERROR code=instance_lease_dir_unavailable id=%s dir=%s reason=%s\n",
                     identity.instance_id.c_str(),
                     identity.instance_lease_path.parent_path().string().c_str(),
                     ec.message().c_str());
        return false;
    }
    std::filesystem::create_directories(identity.legacy_alias_lease_path.parent_path(), ec);
    if (ec) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_ERROR code=lease_dir_unavailable id=%s dir=%s reason=%s\n",
                     identity.instance_id.c_str(),
                     identity.legacy_alias_lease_path.parent_path().string().c_str(),
                     ec.message().c_str());
        return false;
    }

    if (!instance_lease.acquire(identity.instance_lease_path, identity.instance_id,
                                "runtime-instance")) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_COLLISION id=%s dir=%s reason=%s owner=%s authority=%s metadata=%s\n",
                     identity.instance_id.c_str(), identity.runtime_dir.string().c_str(),
                     instance_lease.reason().c_str(), instance_lease.holder_runtime_id().c_str(),
                     instance_lease.authority_name().c_str(),
                     instance_lease.metadata_path().string().c_str());
        return false;
    }

    // The identity lease catches reuse of one runtime_instance_id. The
    // directory lease catches the complementary misconfiguration where two
    // different IDs are pointed at one explicit caller-owned cwd. Both are
    // required before any cwd-relative config/log/recording is touched.
    if (!runtime_dir_lease.acquire(identity.runtime_dir_lease_path, identity.instance_id,
                                   "runtime-directory")) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_COLLISION id=%s dir=%s reason=%s owner=%s authority=%s metadata=%s\n",
                     identity.instance_id.c_str(), identity.runtime_dir.string().c_str(),
                     runtime_dir_lease.reason().c_str(),
                     runtime_dir_lease.holder_runtime_id().c_str(),
                     runtime_dir_lease.authority_name().c_str(),
                     runtime_dir_lease.metadata_path().string().c_str());
        release();
        return false;
    }

    // The lease resolves the requested directory through the retained kernel
    // handle. From this point on, every cwd-relative resource must use that
    // handle-derived path; re-resolving identity.runtime_dir could follow a
    // junction which was retargeted after acquisition.
    if (runtime_dir_lease.operational_path().empty()) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_ERROR code=runtime_dir_operational_path_unavailable "
                     "id=%s requested=%s\n",
                     identity.instance_id.c_str(), identity.runtime_dir.string().c_str());
        release();
        return false;
    }
    identity.runtime_dir = runtime_dir_lease.operational_path();

    if (!pulsar_runtime::set_process_environment("PULSAR_RUNTIME_INSTANCE_ID",
                                                 identity.instance_id) ||
        !pulsar_runtime::set_process_environment("PULSAR_RUNTIME_DIR",
                                                 identity.runtime_dir.string())) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_ERROR code=environment_publish_failed id=%s\n",
                     identity.instance_id.c_str());
        release();
        return false;
    }

    // A caller may explicitly disable the historical aliases, or require
    // them. The default is an opportunistic claim: exactly one process gets
    // the fixed names and every other process is made usable with a dedicated
    // mapping rather than silently sharing the first process's queue.
    const std::string alias_policy = lower_ascii(
        std::getenv("PULSAR_LEGACY_ALIAS") ? std::getenv("PULSAR_LEGACY_ALIAS") : "");
    const bool disabled = alias_policy == "0" || alias_policy == "false" ||
                          alias_policy == "off" || alias_policy == "disabled" ||
                          alias_policy == "dedicated";
    const bool required = alias_policy == "required" || alias_policy == "strict";

    if (disabled) {
        alias_state = "disabled";
    } else if (legacy_alias_lease.acquire(identity.legacy_alias_lease_path,
                                          identity.instance_id,
                                          "directshow-legacy-alias")) {
        legacy_alias = true;
        alias_state = "acquired";
    } else {
        alias_state = legacy_alias_lease.result() == pulsar_runtime::LeaseResult::Refused
                          ? "refused"
                          : "error";
        std::fprintf(stderr,
                     "PULSAR_LEGACY_ALIAS lease=%s id=%s path=%s reason=%s owner=%s authority=%s metadata=%s\n",
                     alias_state.c_str(), identity.instance_id.c_str(),
                     identity.legacy_alias_lease_path.string().c_str(),
                     legacy_alias_lease.reason().c_str(),
                     legacy_alias_lease.holder_runtime_id().c_str(),
                     legacy_alias_lease.authority_name().c_str(),
                     legacy_alias_lease.metadata_path().string().c_str());
        if (required) {
            std::fprintf(stderr,
                         "PULSAR_RUNTIME_ERROR code=legacy_alias_required id=%s\n",
                         identity.instance_id.c_str());
            release();
            return false;
        }
    }

    if (!pulsar_runtime::set_process_environment("PULSAR_DIRECTSHOW_LEGACY_ALIAS",
                                                 legacy_alias ? "1" : "0")) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_ERROR code=directshow_policy_publish_failed id=%s\n",
                     identity.instance_id.c_str());
        release();
        return false;
    }

    if (!port_is_valid(std::getenv("PULSAR_PORT"))) {
        const std::uint16_t port = pulsar_runtime::pick_free_loopback_port();
        if (port == 0 || !pulsar_runtime::set_process_environment(
                              "PULSAR_PORT", std::to_string(port))) {
            std::fprintf(stderr,
                         "PULSAR_RUNTIME_ERROR code=port_allocation_failed id=%s\n",
                         identity.instance_id.c_str());
            release();
            return false;
        }
        std::fprintf(stderr, "PULSAR_RUNTIME_PORT id=%s port=%u source=auto\n",
                     identity.instance_id.c_str(), static_cast<unsigned>(port));
    }

    // All cwd-relative resources (the WebSocket config, default recordings,
    // and any legacy path in a plugin that has not yet been namespaced) now
    // resolve below this validated, exclusively held directory.
    std::filesystem::current_path(identity.runtime_dir, ec);
    if (ec) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_ERROR code=chdir_failed id=%s dir=%s reason=%s\n",
                     identity.instance_id.c_str(), identity.runtime_dir.string().c_str(),
                     ec.message().c_str());
        release();
        return false;
    }

    std::fprintf(stderr,
                 "PULSAR_RUNTIME_INSTANCE id=%s dir=%s instance_lock=acquired "
                 "instance_lock_path=%s runtime_dir_lock=acquired runtime_dir_lock_path=%s "
                 "instance_authority=%s instance_metadata=%s runtime_dir_authority=%s "
                 "runtime_dir_metadata=%s legacy_alias=%s alias_lease=%s alias_path=%s "
                 "alias_authority=%s alias_metadata=%s\n",
                 identity.instance_id.c_str(), identity.runtime_dir.string().c_str(),
                 identity.instance_lease_path.string().c_str(),
                 identity.runtime_dir_lease_path.string().c_str(),
                 instance_lease.authority_name().c_str(), instance_lease.metadata_path().string().c_str(),
                 runtime_dir_lease.authority_name().c_str(),
                 runtime_dir_lease.metadata_path().string().c_str(), legacy_alias ? "1" : "0",
                 alias_state.c_str(), identity.legacy_alias_lease_path.string().c_str(),
                 legacy_alias_lease.authority_name().c_str(),
                 legacy_alias_lease.metadata_path().string().c_str());
    if (disabled)
        std::fprintf(stderr, "PULSAR_LEGACY_ALIAS lease=disabled id=%s path=%s authority=%s metadata=%s\n",
                     identity.instance_id.c_str(), identity.legacy_alias_lease_path.string().c_str(),
                     legacy_alias_lease.authority_name().c_str(),
                     legacy_alias_lease.metadata_path().string().c_str());
    else if (legacy_alias)
        std::fprintf(stderr, "PULSAR_LEGACY_ALIAS lease=acquired id=%s path=%s authority=%s metadata=%s\n",
                     identity.instance_id.c_str(), identity.legacy_alias_lease_path.string().c_str(),
                     legacy_alias_lease.authority_name().c_str(),
                     legacy_alias_lease.metadata_path().string().c_str());
    return true;
}

bool RuntimeState::renew()
{
    bool ok = instance_lease.renew();
    ok = runtime_dir_lease.renew() && ok;
    if (legacy_alias)
        ok = legacy_alias_lease.renew() && ok;
    return ok;
}

void RuntimeState::release()
{
    if (legacy_alias_lease.held()) {
        std::fprintf(stderr, "PULSAR_LEGACY_ALIAS lease=released id=%s path=%s authority=%s metadata=%s\n",
                     identity.instance_id.c_str(), identity.legacy_alias_lease_path.string().c_str(),
                     legacy_alias_lease.authority_name().c_str(),
                     legacy_alias_lease.metadata_path().string().c_str());
        legacy_alias_lease.release();
    }
    if (runtime_dir_lease.held()) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_INSTANCE runtime_dir_lease=released id=%s dir=%s authority=%s metadata=%s\n",
                     identity.instance_id.c_str(), identity.runtime_dir.string().c_str(),
                     runtime_dir_lease.authority_name().c_str(),
                     runtime_dir_lease.metadata_path().string().c_str());
        runtime_dir_lease.release();
    }
    if (instance_lease.held()) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_INSTANCE lease=released id=%s dir=%s authority=%s metadata=%s\n",
                     identity.instance_id.c_str(), identity.runtime_dir.string().c_str(),
                     instance_lease.authority_name().c_str(),
                     instance_lease.metadata_path().string().c_str());
        instance_lease.release();
    }
}

// Session credentials negotiated at boot, exposed to the parent
// process via the PULSAR_READY stdout sentinel.
int  g_session_port     = 4455;
std::string g_session_password;

// ADR-005 §3.1-§3.2: the log handler's registry-layer state and file sink.
// `g_log_session_id` is the reserved `session` field of the log line
// gabarit (ADR §3.3). Resolved once in main(), before
// install_pulsar_log_handler() runs, so every line -- startup included --
// carries it; never reassigned afterwards.
pulsar_log::SecretRegistry g_secret_registry;
std::unique_ptr<pulsar_log::LogFileSink> g_log_sink;
std::mutex g_log_mutex;
std::string g_log_session_id;

// ADR-005 §3.6.1: counters + bounded WARN/ERROR ring, fed the same
// redacted line written to the file below -- never a re-read of it.
pulsar_log::DiagnosticsRing g_log_diagnostics;

// base_set_log_handler callback. Formats, redacts (both layers), then
// writes the SAME redacted line to stderr and to the rotating file --
// never the raw one, on either destination (ADR §3.2 "posture d'échec,
// non négociable": a line whose redaction can't be trusted is abandoned
// entirely, not just kept off the file).
void pulsar_log_handler(int log_level, const char *format, va_list args, void * /*param*/)
{
    char buf[4096];
    va_list args_copy;
    va_copy(args_copy, args);
    int written = std::vsnprintf(buf, sizeof(buf), format, args_copy);
    va_end(args_copy);
    std::string message = (written > 0)
        ? std::string(buf, static_cast<std::size_t>(written) < sizeof(buf)
                               ? static_cast<std::size_t>(written)
                               : sizeof(buf) - 1)
        : std::string();

    pulsar_log::Level level;
    switch (log_level) {
    case LOG_ERROR:
        level = pulsar_log::Level::Error;
        break;
    case LOG_WARNING:
        level = pulsar_log::Level::Warn;
        break;
    case LOG_DEBUG:
        level = pulsar_log::Level::Debug;
        break;
    case LOG_INFO:
    default:
        level = pulsar_log::Level::Info;
        break;
    }

    std::string trimmed_message;
    std::string subsystem = pulsar_log::derive_subsystem(message, trimmed_message);
    std::string line =
        pulsar_log::format_line(level, g_log_session_id, subsystem, trimmed_message);

    std::lock_guard<std::mutex> lock(g_log_mutex);
    auto redacted = pulsar_log::redact_line(line, g_secret_registry);
    if (!redacted)
        return; // abandon: neither destination gets an unverified line

    std::fprintf(stderr, "%s\n", redacted->c_str());
    std::fflush(stderr);

    // ADR-005 §3.6.1: recorded for EVERY line reaching this point -- the
    // same set the file below receives when it is opened -- so the
    // extraction request's counters concord with an independent count of
    // the file (RC14).
    g_log_diagnostics.record(level, *redacted);

    if (g_log_sink && g_log_sink->opened())
        g_log_sink->write_line(*redacted);
}

// ADR-005 §3.6: bridges pulsar-headless's process-local diagnostics state
// (this .exe's own translation unit -- g_log_sink / g_log_diagnostics are
// not symbols any plugin DLL can link against) to the vendor requests
// registered by pulsar-multi-stream, a separate module loaded into the same
// process. Same pattern obs-websocket itself uses for its own cross-module
// handle (WebSocketApi's "obs_websocket_api_get_ph" on the global proc
// handler): any module, DLL or host exe, can reach a proc registered here.
//
// §3.6.1: `in int max_lines` is the caller's request; the response is
// always clamped to pulsar_log::DiagnosticsRing::kServerMaxLines
// server-side, never just to what was asked. `lines` transfers ownership
// of a fresh obs_data_array_t* to the caller (obs_data_array_release()
// once done), one object per line under the "line" key -- the array itself
// carries WARN/ERROR content only, per the ring's own contract.
void pulsar_log_get_diagnostics_cb(void * /*priv_data*/, calldata_t *cd)
{
    long long requested = calldata_int(cd, "max_lines");
    if (requested < 0)
        requested = 0;

    std::string path;
    bool path_known = false;
    {
        std::lock_guard<std::mutex> lock(g_log_mutex);
        if (g_log_sink && g_log_sink->opened()) {
            path = g_log_sink->path();
            path_known = true;
        }
    }

    calldata_set_string(cd, "path", path.c_str());
    calldata_set_bool(cd, "path_known", path_known);
    calldata_set_int(cd, "errors", static_cast<long long>(g_log_diagnostics.count(pulsar_log::Level::Error)));
    calldata_set_int(cd, "warnings", static_cast<long long>(g_log_diagnostics.count(pulsar_log::Level::Warn)));
    calldata_set_int(cd, "infos", static_cast<long long>(g_log_diagnostics.count(pulsar_log::Level::Info)));
    calldata_set_int(cd, "debugs", static_cast<long long>(g_log_diagnostics.count(pulsar_log::Level::Debug)));

    obs_data_array_t *lines = obs_data_array_create();
    for (const auto &line : g_log_diagnostics.last_warn_error_lines(static_cast<std::size_t>(requested))) {
        obs_data_t *item = obs_data_create();
        obs_data_set_string(item, "line", line.c_str());
        obs_data_array_push_back(lines, item);
        obs_data_release(item);
    }
    calldata_set_ptr(cd, "lines", lines);

    calldata_set_bool(cd, "success", true);
}

// ADR-005 §3.6.2: the kill switch. Deliberately asymmetric -- stops file
// writes, never reopens them; a second call is a no-op that reports
// `already_stopped`, never an error, and never resurrects the file. Takes
// no path (there is nothing to parameterise: it acts on the one sink this
// process already owns). The stop itself is journalled as the last line
// via LogFileSink::close(), which writes-then-closes -- exactly the
// primitive #183 built this kill switch for.
void pulsar_log_stop_file_write_cb(void * /*priv_data*/, calldata_t *cd)
{
    std::lock_guard<std::mutex> lock(g_log_mutex);

    if (!g_log_sink || !g_log_sink->opened()) {
        calldata_set_bool(cd, "stopped", false);
        calldata_set_bool(cd, "already_stopped", true);
        calldata_set_string(cd, "path", g_log_sink ? g_log_sink->path().c_str() : "");
        calldata_set_bool(cd, "success", true);
        return;
    }

    const std::string path = g_log_sink->path();
    std::string stopLine = pulsar_log::format_line(
        pulsar_log::Level::Warn, g_log_session_id, "pulsar-headless",
        "log file write stopped via kill-switch request; a restart is required to resume");
    auto redacted = pulsar_log::redact_line(stopLine, g_secret_registry);
    // ADR §3.2 posture d'échec: a line whose redaction can't be trusted is
    // abandoned, same as the normal handler (:119-120) -- never fall back
    // to the unredacted line. The sink still needs closing either way, so
    // the fallback is a fixed, static, redaction-free sentinel rather than
    // the (unverified) formatted stopLine.
    const std::string finalLine = redacted ? *redacted
                                            : "WARN pulsar-headless log file write stopped "
                                              "via kill-switch request (redaction unavailable, "
                                              "message omitted)";

    g_log_sink->close(finalLine);
    g_log_diagnostics.record(pulsar_log::Level::Warn, finalLine);

    calldata_set_bool(cd, "stopped", true);
    calldata_set_bool(cd, "already_stopped", false);
    calldata_set_string(cd, "path", path.c_str());
    calldata_set_bool(cd, "success", true);
}

// ADR-005 §3.3: bridges `g_log_session_id` -- resolved once at boot, in this
// .exe's own translation unit -- to pulsar-multi-stream, which has no
// compile-time link to it and needs the same value to stamp its `pulsar:*`
// vendor events. Same global proc handler bridge as the diagnostics procs
// above.
void pulsar_log_get_session_id_cb(void * /*priv_data*/, calldata_t *cd)
{
    calldata_set_string(cd, "session", g_log_session_id.c_str());
    calldata_set_bool(cd, "success", true);
}

// ADR-005 F1 (issue #197): registers `value` with THIS process's own
// g_secret_registry -- the same registry pulsar_log_handler (:121) and the
// kill-switch's own final line (:210) already consult on every write.
// Exposed on the global proc handler for the identical reason
// pulsar_log_get_diagnostics is (see install_pulsar_log_procs below):
// g_secret_registry is a static of this .exe's translation unit, unreachable
// from a plugin DLL (pulsar-multi-stream) by any compile-time link. Before
// this proc existed there was no way for the Twitch stream key
// pulsar-multi-stream receives to ever reach this registry.
void pulsar_log_register_secret_cb(void * /*priv_data*/, calldata_t *cd)
{
    const char *value = calldata_string(cd, "value");
    if (value && *value)
        g_secret_registry.register_secret(value);
    calldata_set_bool(cd, "success", true);
}

// Installs base_set_log_handler and opens the rotating file sink. Must run
// BEFORE obs_startup (ADR §3.1) so every libobs blog() call, including
// ones emitted during startup itself, is captured. A file-open failure
// degrades to stderr-only, logged as its own ERROR line on stderr -- boot
// continues either way.
void install_pulsar_log_handler()
{
    g_log_sink = std::make_unique<pulsar_log::LogFileSink>(pulsar_log::default_log_dir(),
                                                             pulsar_log::rotation_config_from_env());
    if (!g_log_sink->opened()) {
        std::fprintf(stderr, "ERROR pulsar-headless: log file unavailable (%s)\n",
                     g_log_sink->error().c_str());
    }
    base_set_log_handler(pulsar_log_handler, nullptr);
}

// ADR-005 §3.6: publish the diagnostic surface's two procs on the GLOBAL obs
// proc handler -- reachable from pulsar-multi-stream (a plugin DLL, no
// compile-time link to this .exe) the same way obs-websocket's own API
// handle is (see obs-websocket.cpp's "pulsar_websocket_is_loopback_only"
// registration for the mirrored pattern on that side).
//
// Must run AFTER obs_startup(): obs_get_proc_handler() returns the global
// `obs->procs` handle, which obs_startup() allocates (and, if called again,
// replaces) -- calling it beforehand dereferences a still-NULL `obs` and
// crashes on every boot. Called from main() once obs_startup() has
// succeeded (around pulsar_frontend_init() time).
void install_pulsar_log_procs()
{
    proc_handler_t *global_ph = obs_get_proc_handler();
    proc_handler_add(global_ph,
                     "bool pulsar_log_get_diagnostics(in int max_lines, out string path, "
                     "out bool path_known, out int errors, out int warnings, out int infos, "
                     "out int debugs, out ptr lines)",
                     &pulsar_log_get_diagnostics_cb, nullptr);
    proc_handler_add(global_ph,
                     "bool pulsar_log_stop_file_write(out bool stopped, out bool already_stopped, "
                     "out string path)",
                     &pulsar_log_stop_file_write_cb, nullptr);
    proc_handler_add(global_ph,
                     "bool pulsar_log_get_session_id(out string session)",
                     &pulsar_log_get_session_id_cb, nullptr);
    proc_handler_add(global_ph, "bool pulsar_log_register_secret(in string value)",
                     &pulsar_log_register_secret_cb, nullptr);
}

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

HANDLE g_shutdown_event = nullptr;

// The parent creates an anonymous manual-reset event and passes this one
// inheritable handle through STARTUPINFOEX's handle list. The numeric value
// never enters logs or the evidence artifact; only the explicit opt-in
// presence and lifecycle state are observable.
bool adopt_shutdown_event_from_environment(const std::string &instance_id)
{
    const char *raw = std::getenv("PULSAR_SHUTDOWN_EVENT_HANDLE");
    if (!raw) {
        std::fprintf(stderr,
                     "PULSAR_SHUTDOWN_CONTROL event=absent id=%s mechanism=console_compat\n",
                     instance_id.c_str());
        return true;
    }
    if (!*raw) {
        std::fprintf(stderr,
                     "PULSAR_SHUTDOWN_CONTROL event=invalid id=%s reason=empty_handle\n",
                     instance_id.c_str());
        return false;
    }

    for (const char *cursor = raw; *cursor; ++cursor) {
        if (*cursor < '0' || *cursor > '9') {
            std::fprintf(stderr,
                         "PULSAR_SHUTDOWN_CONTROL event=invalid id=%s reason=handle_syntax\n",
                         instance_id.c_str());
            return false;
        }
    }
    errno = 0;
    char *end = nullptr;
    const unsigned long long parsed = std::strtoull(raw, &end, 10);
    if (errno == ERANGE || end == raw || *end != '\0' || parsed == 0 ||
        parsed > static_cast<unsigned long long>(UINTPTR_MAX)) {
        std::fprintf(stderr,
                     "PULSAR_SHUTDOWN_CONTROL event=invalid id=%s reason=handle_range\n",
                     instance_id.c_str());
        return false;
    }

    HANDLE candidate = reinterpret_cast<HANDLE>(static_cast<std::uintptr_t>(parsed));
    DWORD handle_flags = 0;
    if (!GetHandleInformation(candidate, &handle_flags) ||
        WaitForSingleObject(candidate, 0) == WAIT_FAILED) {
        std::fprintf(stderr,
                     "PULSAR_SHUTDOWN_CONTROL event=invalid id=%s reason=closed_handle\n",
                     instance_id.c_str());
        return false;
    }
    // Do not let this control capability leak into any later child process
    // (CEF, FFmpeg, or a plugin) that might use inheritable handles.
    if (!SetHandleInformation(candidate, HANDLE_FLAG_INHERIT, 0)) {
        std::fprintf(stderr,
                     "PULSAR_SHUTDOWN_CONTROL event=invalid id=%s reason=inherit_clear\n",
                     instance_id.c_str());
        return false;
    }
    g_shutdown_event = candidate;
    std::fprintf(stderr,
                 "PULSAR_SHUTDOWN_CONTROL event=ready id=%s mechanism=inherited_event\n",
                 instance_id.c_str());
    return true;
}

void close_shutdown_event()
{
    if (g_shutdown_event) {
        CloseHandle(g_shutdown_event);
        g_shutdown_event = nullptr;
    }
}

bool shutdown_event_requested(const std::string &instance_id)
{
    if (!g_shutdown_event)
        return false;
    // Bound the wait so a signaled event interrupts the idle loop promptly,
    // while still allowing the existing lease-renewal cadence to run. A
    // failed wait is fail-closed: the process must not continue operating
    // when its explicit shutdown control has become invalid.
    const DWORD result = WaitForSingleObject(g_shutdown_event, 100);
    if (result == WAIT_OBJECT_0) {
        std::fprintf(stderr,
                     "PULSAR_SHUTDOWN_CONTROL event=signaled id=%s mechanism=inherited_event\n",
                     instance_id.c_str());
        return true;
    }
    if (result == WAIT_FAILED) {
        std::fprintf(stderr,
                     "PULSAR_SHUTDOWN_CONTROL event=wait_failed id=%s error=%lu\n",
                     instance_id.c_str(), static_cast<unsigned long>(GetLastError()));
        return true;
    }
    return false;
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
            blog(LOG_WARNING, "[pulsar-headless] PULSAR_FPS=%s rejected; using %d", e, fps);
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
            blog(LOG_WARNING, "[pulsar-headless] PULSAR_RESOLUTION=%s rejected; using %dx%d",
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
        blog(LOG_ERROR, "[pulsar-headless] obs_reset_video failed (%d)", result);
        return false;
    }
    blog(LOG_INFO, "[pulsar-headless] video %dx%d @ %d fps", width, height, fps);
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
        blog(LOG_ERROR, "[pulsar-headless] CoInitializeEx failed (0x%08lx)",
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
        blog(LOG_WARNING, "[pulsar-headless] could not resolve the default playback device id (0x%08lx)",
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
        blog(LOG_ERROR, "[pulsar-headless] obs_reset_audio failed");
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
            blog(LOG_INFO, "[pulsar-headless] audio monitoring device bound (resolved default, id=%s)",
                 resolved_id.c_str());
        } else {
            obs_set_audio_monitoring_device("Default", "default");
            blog(LOG_INFO,
                 "[pulsar-headless] audio monitoring device bound (default sentinel -- id resolution "
                 "failed, ZabCapture sources will self-monitor-guard silent)");
        }
    } else {
        blog(LOG_WARNING, "[pulsar-headless] audio monitoring unavailable on this platform build");
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

// A successful obs_module_load() is not enough for the embedding contract:
// obs-websocket may reject its persistent-data directory, or its listen()
// call may fail while libobs continues loading optional modules.  The host
// must never publish PULSAR_READY for such a process.  The websocket plugin
// exposes this state through the global proc handler so the check remains
// independent of its private C++ object and still distinguishes a bind
// failure from an ordinary optional-module warning.
bool websocket_server_ready(std::string &reason)
{
    if (!obs_get_module("obs-websocket")) {
        reason = "obs_websocket_module_not_loaded";
        return false;
    }

    proc_handler_t *global_ph = obs_get_proc_handler();
    if (!global_ph) {
        reason = "obs_websocket_proc_handler_unavailable";
        return false;
    }

    calldata_t cd;
    calldata_init(&cd);
    const bool called = proc_handler_call(global_ph, "pulsar_websocket_is_listening", &cd);
    const bool success = calldata_bool(&cd, "success");
    const bool listening = calldata_bool(&cd, "listening");
    calldata_free(&cd);
    if (!called || !success) {
        reason = "obs_websocket_listening_state_unavailable";
        return false;
    }
    if (!listening) {
        reason = "obs_websocket_bind_failed";
        return false;
    }
    return true;
}

// Quiesce the WebSocket admission/dispatch boundary before any frontend or
// browser callback teardown. The websocket plugin owns the handler leases and
// returns a bounded, explicit drain ACK; this host only consumes that contract
// through the global proc handler.
bool websocket_pre_shutdown_ready(std::string &reason)
{
    proc_handler_t *global_ph = obs_get_proc_handler();
    if (!global_ph) {
        reason = "obs_websocket_proc_handler_unavailable";
        return false;
    }

    calldata_t cd;
    calldata_init(&cd);
    calldata_set_int(&cd, "timeout_ms", 5000);
    const bool called = proc_handler_call(global_ph, "pulsar_websocket_pre_shutdown", &cd);
    const bool success = calldata_bool(&cd, "success");
    const long long activeHandlers = calldata_int(&cd, "active_handlers");
    const long long sessions = calldata_int(&cd, "sessions");
    const char *phaseValue = calldata_string(&cd, "phase");
    const std::string phase = phaseValue ? phaseValue : "unknown";
    calldata_free(&cd);
    if (!called) {
        reason = "obs_websocket_quiesce_proc_unavailable";
        return false;
    }
    if (!success || activeHandlers != 0 || sessions != 0) {
        reason = "obs_websocket_quiesce_failed phase=" + phase +
                 " active_handlers=" + std::to_string(activeHandlers) +
                 " sessions=" + std::to_string(sessions);
        return false;
    }
    return true;
}

// A failed websocket quiesce is a process-integrity failure, not a recoverable
// module error.  Returning through main would run the DLL/static destructors;
// WebSocketServer::~WebSocketServer() calls the unbounded Stop(), which can
// retain admitted worker threads after the host has already released its
// runtime/alias leases.  Emit a direct, credential-free and flushed marker,
// then bypass C++/Qt/libobs teardown entirely.  On Windows, _Exit ultimately
// permits DLL_PROCESS_DETACH and a plugin's static destructors to run, so use
// TerminateProcess for the stronger no-destructor boundary.  Kernel-backed
// leases and handles are released by the operating system only once this
// process exits, so no successor can overlap a still-live failed runtime.
[[noreturn]] void fail_closed_websocket_quiesce(const std::string &reason)
{
    std::fprintf(stderr,
                 "PULSAR_WEBSOCKET_QUIESCE event=fail_closed_exit action=process_exit reason=%s\n",
                 reason.c_str());
    std::fflush(stderr);
    std::fflush(stdout);
#ifdef _WIN32
    if (!TerminateProcess(GetCurrentProcess(), 1)) {
        std::fprintf(stderr,
                     "PULSAR_WEBSOCKET_QUIESCE event=terminate_process_failed error=%lu fallback=exit\n",
                     static_cast<unsigned long>(GetLastError()));
        std::fflush(stderr);
    }
#endif
    std::_Exit(1);
}

// Browser CEF/audio teardown must complete before obs_shutdown() stops the
// process-wide libobs audio bus.  The browser plugin owns the barrier and
// exposes it as a private global proc so the host does not link against a DLL
// implementation detail.
bool browser_pre_shutdown_ready(std::string &reason)
{
    proc_handler_t *global_ph = obs_get_proc_handler();
    if (!global_ph) {
        reason = "browser_pre_shutdown_proc_handler_unavailable";
        return false;
    }

    calldata_t cd;
    calldata_init(&cd);
    const bool called = proc_handler_call(global_ph, "pulsar_browser_pre_shutdown", &cd);
    const bool success = calldata_bool(&cd, "success");
    calldata_free(&cd);
    if (!called) {
        reason = "browser_pre_shutdown_proc_unavailable";
        return false;
    }
    if (!success) {
        reason = "browser_pre_shutdown_barrier_failed";
        return false;
    }
    return true;
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

// ADR-005 §3.3: session correlation id. `PULSAR_SESSION_ID`, if set by the
// parent process, is reused verbatim -- letting a supervisor correlate a
// group of processes under one id it already knows. Otherwise generated
// fresh from the same alphabet as the WebSocket password above (no new
// dependency), so it is never empty and differs between two boots.
std::string resolve_session_id()
{
    if (const char *e = std::getenv("PULSAR_SESSION_ID"); e && *e)
        return e;
    return generate_session_password(16);
}

// V2 fail-closed history (#181 veto, Bastion 2026-08-10): every path
// below used to be `void` and just `return;` on failure -- but the
// caller (main(), below) pressed on into obs_load_all_modules()
// regardless. If an attacker had pre-created obs-websocket/config.json
// with permissive content (e.g. "auth_required": false) BEFORE Pulsar
// booted, every fail-closed branch here (refusing to touch that file)
// let the plugin load it completely unauthenticated -- a session
// read-leak (the pre-#194 behavior) turned into a full RPC takeover
// (scenes/outputs/browser-source URLs, no password required). This
// function now returns bool, and main() aborts BEFORE loading any
// plugin -- never printing PULSAR_READY -- on any false.
bool seed_websocket_config()
{
    if (const char *e = std::getenv("PULSAR_PORT"); e && *e) {
        int v = std::atoi(e);
        if (v > 0 && v < 65536) {
            g_session_port = v;
        } else {
            blog(LOG_WARNING, "[pulsar-headless] PULSAR_PORT=%s rejected; using %d",
                 e, g_session_port);
        }
    }

    if (const char *e = std::getenv("PULSAR_PASSWORD"); e && *e) {
        g_session_password = e;
    } else {
        g_session_password = generate_session_password();
    }
    // ADR-005 §3.2 registry layer: this is the one secret main.cpp itself
    // creates, registered the moment it exists so the log handler's
    // registry-layer redaction covers it even if it later surfaces bare
    // (with no recognizable field/URL form) via a debug dump.
    g_secret_registry.register_secret(g_session_password);

    // obs-websocket persists its config under <module_config_path>/
    // obs-websocket/ when running headless. #243 changes both the process cwd
    // and obs_startup(module_config_path) to the validated runtime directory,
    // so this resolves to <runtime>/obs-websocket/config.json and cannot be
    // shared by concurrent instances.
    const char *dir = "obs-websocket";
    const char *path = "obs-websocket/config.json";

    // Hand-rolled JSON -- the only consumer is obs-websocket itself
    // and the password is already constrained to URL-safe charset
    // so no escaping is needed.
    std::string body;
    body += "{\n";
    body += "  \"alerts_enabled\": false,\n";
    body += "  \"auth_required\": true,\n";
    body += "  \"first_load\": false,\n";
    body += "  \"server_enabled\": true,\n";
    body += "  \"server_password\": \"" + g_session_password + "\",\n";
    body += "  \"server_port\": " + std::to_string(g_session_port) + "\n";
    body += "}\n";

#ifdef _WIN32
    // #201 N1 (three of four named gestures) / ADR-005 §5 R10: atomic
    // creation with a protected DACL, reparse-point refusal, and
    // handle-based owner verification -- see dir-hardening.h/.cpp.
    if (!pulsar_dir::create_directory_hardened(dir, dir)) {
        std::fprintf(stderr,
                     "pulsar-headless: could not create/verify a protected, "
                     "owned %s; refusing to boot (fail closed, #201 N1)\n",
                     dir);
        return false;
    }

    // #201 N3: sweep before allocating a new temp file, not after --
    // an orphan from a prior crashed boot never lingers past this one.
    pulsar_dir::sweep_orphaned_temp_files(dir, dir);

    std::string tmp_path;
    HANDLE h = INVALID_HANDLE_VALUE;
    // #201 N2: the returned handle is the one CREATE_NEW just opened --
    // no close-then-OPEN_EXISTING-reopen TOCTOU window before the write.
    if (!pulsar_dir::create_protected_temp_file(dir, path, tmp_path, h))
        return false; // create_protected_temp_file already logged why

    DWORD written = 0;
    BOOL wrote = WriteFile(h, body.data(), static_cast<DWORD>(body.size()), &written, nullptr);
    FlushFileBuffers(h);
    CloseHandle(h);
    if (!wrote || written != body.size()) {
        std::fprintf(stderr,
                     "pulsar-headless: could not write %s (partial write); "
                     "refusing to publish it (fail closed, #181 V7)\n",
                     tmp_path.c_str());
        DeleteFileA(tmp_path.c_str());
        return false;
    }

    // Atomic publish: rename onto the real path, replacing whatever
    // is there (first boot: nothing; reseed: our own prior file).
    // The renamed file keeps ITS OWN security descriptor -- the one
    // we just built with our own protected DACL -- regardless of
    // what the file it replaces was owned by or who wrote it. No
    // ownership comparison is ever made (closes #181 V1).
    if (!MoveFileExA(tmp_path.c_str(), path,
                      MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        std::fprintf(stderr,
                     "pulsar-headless: MoveFileExA(%s -> %s) failed (%lu); "
                     "refusing to publish the session password (fail closed)\n",
                     tmp_path.c_str(), path, GetLastError());
        DeleteFileA(tmp_path.c_str());
        return false;
    }

    // #201 N1, 4th named gesture ("re-vérification par handle ... après
    // le rename"): re-open the published config.json by handle and
    // re-verify its owner is us. Defense in depth against a substitution
    // not anticipated above -- the temp file was already ours (created
    // under our protected SECURITY_ATTRIBUTES, N1/N2), so this is
    // expected to be a no-op on every honest boot; a mismatch means
    // something intervened between the rename and this check, and we
    // fail closed rather than let obs-websocket load an unverified file.
    if (!pulsar_dir::verify_owned_by_current_token(path, path)) {
        std::fprintf(stderr,
                     "pulsar-headless: %s is not owned by the current token "
                     "after rename; refusing to trust it (fail closed, #201 N1 "
                     "4th gesture)\n",
                     path);
        return false;
    }
    return true;
#else
    namespace fs = std::filesystem;
    std::error_code ec;
    fs::create_directories(dir, ec);
    if (ec) {
        std::fprintf(stderr,
                     "pulsar-headless: could not create %s dir: %s\n",
                     dir, ec.message().c_str());
        return false;
    }
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    if (!out) {
        std::fprintf(stderr, "pulsar-headless: could not write %s\n", path);
        return false;
    }
    out << body;
    return static_cast<bool>(out);
#endif
}

} // namespace

int main(int argc, char **argv)
{
#ifdef _WIN32
    wire_stdio_for_windows_subsystem();
#endif

    // #243: resolve and lock the complete process namespace before Qt/libobs
    // can create any config, log, recording, socket or DirectShow resource.
    // This also publishes the validated runtime identity to the upstream
    // win-dshow patch through the process environment.
    auto runtime_state = std::make_unique<RuntimeState>();
    if (!runtime_state->initialize())
        return 1;

#ifdef _WIN32
    // CTRL_BREAK_EVENT requires a console shared with the target. A
    // /SUBSYSTEM:WINDOWS child whose stdout is a redirected pipe has no such
    // console, so an explicitly inherited anonymous event is the graceful
    // control primitive. Invalid opt-in handles fail closed; absent opt-in
    // retains the console-control compatibility path.
    if (!adopt_shutdown_event_from_environment(runtime_state->identity.instance_id)) {
        runtime_state->release();
        return 1;
    }
#endif

    // Force the offscreen Qt platform so QApplication can construct
    // without a display server / platform plugin DLL. obs-websocket
    // (and other libobs plugins) link against Qt6 and assume a
    // QApplication exists; in headless mode we never show a widget,
    // but the QApplication instance is still required for
    // QObject/QString/QJson machinery to work.
    qputenv("QT_QPA_PLATFORM", QByteArrayLiteral("minimal"));

    QApplication qt_app(argc, argv);

    // ADR-005 §3.3: resolved before install_pulsar_log_handler() so every
    // log line from here on -- startup included -- already carries it.
    g_log_session_id = resolve_session_id();

    // ADR-005 §3.1: installed before obs_startup so every blog() call from
    // here on -- including ones libobs itself emits during startup -- is
    // captured by our durable, redacted log handler.
    install_pulsar_log_handler();

    const std::string module_config_path = runtime_state->identity.runtime_dir.string();
    if (!obs_startup("en-US", module_config_path.c_str(), nullptr)) {
        blog(LOG_ERROR, "[pulsar-headless] obs_startup failed");
        return 1;
    }

    blog(LOG_INFO,
         "[pulsar-runtime] instance_id=%s runtime_dir=%s legacy_alias=%s alias_lease=%s "
         "instance_authority=%s runtime_dir_authority=%s alias_authority=%s",
         runtime_state->identity.instance_id.c_str(),
         runtime_state->identity.runtime_dir.string().c_str(),
         runtime_state->legacy_alias ? "1" : "0", runtime_state->alias_state.c_str(),
         runtime_state->instance_lease.authority_name().c_str(),
         runtime_state->runtime_dir_lease.authority_name().c_str(),
         runtime_state->legacy_alias_lease.authority_name().c_str());

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

    // ADR-005 §3.6: register the diagnostic procs now that obs_startup()
    // has produced the real global proc handler (obs_get_proc_handler()
    // before obs_startup() dereferences a still-NULL `obs`).
    install_pulsar_log_procs();

    // Seed obs-websocket's config.json before plugins load so the
    // pulsar-websocket fork picks up our session port + password
    // rather than the persisted values from a previous run.
    //
    // A false return means the session password could not be
    // published under a verified, protected DACL -- e.g. a hostile
    // pre-created config.json we refused to touch, or a directory
    // whose ACL we could not harden. Loading obs-websocket in that
    // state risks either the plugin falling back to on-disk content
    // we do not control, or authenticating with a password nobody
    // else could actually be handed -- both a security posture we
    // will not boot into silently. Abort BEFORE any plugin loads and
    // BEFORE the PULSAR_READY sentinel, never continuing degraded
    // (#181 V2/F2).
    if (!seed_websocket_config()) {
        std::fprintf(stderr,
                     "pulsar-headless: could not seed a trustworthy "
                     "obs-websocket/config.json; refusing to load plugins or "
                     "report ready (fail closed, #181 V2)\n");
        // #212: pulsar_frontend_init() already ran above, so the frontend
        // must be torn down before obs_shutdown() -- same pairing as the
        // normal exit path below. Skipping this call leaves libobs
        // shutdown tearing down objects the frontend still references,
        // producing a reproducible STATUS_ACCESS_VIOLATION instead of a
        // clean fail-closed exit.
        pulsar_frontend_shutdown();
        obs_shutdown();
        return 1;
    }

    load_modules();

    std::string websocket_error;
    if (!websocket_server_ready(websocket_error)) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_ERROR code=websocket_not_ready id=%s reason=%s\n",
                     runtime_state->identity.instance_id.c_str(), websocket_error.c_str());
        std::string websocket_shutdown_error;
        if (!websocket_pre_shutdown_ready(websocket_shutdown_error)) {
            std::fprintf(stderr,
                         "PULSAR_RUNTIME_ERROR code=websocket_pre_shutdown_failed reason=%s\n",
                         websocket_shutdown_error.c_str());
            fail_closed_websocket_quiesce(websocket_shutdown_error);
        }
        // The frontend callback table was installed before module loading;
        // fence CEF before handing that table back, then pair its teardown
        // with obs_shutdown on this fail-closed path too.
        std::string browser_shutdown_error;
        if (!browser_pre_shutdown_ready(browser_shutdown_error)) {
            std::fprintf(stderr,
                         "PULSAR_RUNTIME_ERROR code=browser_pre_shutdown_failed reason=%s\n",
                         browser_shutdown_error.c_str());
            // Do not enter obs_shutdown after the browser fence failed: that
            // path stops libobs audio while a CEF callback may still be live.
            runtime_state->release();
#ifdef _WIN32
            close_shutdown_event();
#endif
            return 1;
        }
        pulsar_frontend_shutdown();
        if (!pulsar_frontend_cleanup_succeeded()) {
            std::fprintf(stderr,
                         "PULSAR_RUNTIME_ERROR code=frontend_source_cleanup_failed\n");
            runtime_state->release();
#ifdef _WIN32
            close_shutdown_event();
#endif
            return 1;
        }
        obs_shutdown();
        return 1;
    }

    // FINISHED_LOADING is the trigger obs-websocket waits for before
    // accepting requests/events on the wire. Emit it once modules are
    // post-loaded.
    pulsar_frontend_finished_loading();

#ifdef _WIN32
    SetConsoleCtrlHandler(console_ctrl_handler, TRUE);
#endif

    // ADR-005 §3.3: the session correlation line, on its own, BEFORE the
    // sentinel below -- never merged into it, never after it. The sentinel
    // stays byte-for-byte unchanged (D5): twenty probes and spawn.ts anchor
    // it, and both tolerate an intercalary line they don't recognise
    // (probes loop past a non-match, spawn.ts only ever inspects the idle
    // line further down, never this one).
    std::printf("PULSAR_SESSION %s\n", g_log_session_id.c_str());

    // PULSAR_READY sentinel. The parent process (Prism, CI probes,
    // operators) reads stdout line-by-line, picks up this marker,
    // parses port + password, and uses them to authenticate the
    // obs-websocket session. Documented in docs/PRISM-EMBEDDING.md.
    std::printf("PULSAR_READY ws=ws://127.0.0.1:%d password=%s\n",
                g_session_port, g_session_password.c_str());
    std::printf("pulsar-headless: libobs %s ready, idling (Ctrl+C to exit)\n",
                obs_get_version_string());
    std::fflush(stdout);

    auto next_lease_renew = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (g_running.load(std::memory_order_acquire)) {
#ifdef _WIN32
        if (shutdown_event_requested(runtime_state->identity.instance_id))
            g_running.store(false, std::memory_order_release);
#endif
        if (!g_running.load(std::memory_order_acquire))
            break;
        if (std::chrono::steady_clock::now() >= next_lease_renew) {
            if (!runtime_state->renew())
                blog(LOG_ERROR, "[pulsar-runtime] lease metadata renewal failed; "
                                "kernel ownership retained and no alias takeover is allowed");
            next_lease_renew = std::chrono::steady_clock::now() + std::chrono::seconds(5);
        }
#ifdef _WIN32
        if (!g_shutdown_event)
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
#else
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
#endif
    }

    // blog() is intentionally retained for the structured logger, but its
    // INFO sink is not mirrored to a redirected stdout/stderr pipe on every
    // Windows configuration. Emit one credential-free, flushed lifecycle
    // marker on the same graceful path so pipe consumers can prove teardown.
    std::fprintf(stderr, "[pulsar-headless] shutting down\n");
    std::fflush(stderr);
    blog(LOG_INFO, "[pulsar-headless] shutting down");

    std::string websocket_shutdown_error;
    if (!websocket_pre_shutdown_ready(websocket_shutdown_error)) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_ERROR code=websocket_pre_shutdown_failed reason=%s\n",
                     websocket_shutdown_error.c_str());
        fail_closed_websocket_quiesce(websocket_shutdown_error);
    }

    std::string browser_shutdown_error;
    if (!browser_pre_shutdown_ready(browser_shutdown_error)) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_ERROR code=browser_pre_shutdown_failed reason=%s\n",
                     browser_shutdown_error.c_str());
        // A failed or missing browser proc is deliberately fail-closed.  The
        // host must not call obs_shutdown and tear down libobs audio under a
        // still-live CEF callback; process exit performs the final cleanup.
        runtime_state->release();
#ifdef _WIN32
        close_shutdown_event();
#endif
        return 1;
    }
    pulsar_frontend_shutdown();
    if (!pulsar_frontend_cleanup_succeeded()) {
        std::fprintf(stderr,
                     "PULSAR_RUNTIME_ERROR code=frontend_source_cleanup_failed\n");
        runtime_state->release();
#ifdef _WIN32
        close_shutdown_event();
#endif
        return 1;
    }
    obs_shutdown();

    runtime_state->release();
#ifdef _WIN32
    close_shutdown_event();
#endif
    return 0;
}
