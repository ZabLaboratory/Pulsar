// ADR-005 (docs/adr/005-go-live-failure-diagnosability.md) §3.1-§3.2:
// durable, typed, redacted log handler.
//
// This header and its .cpp intentionally never include obs.h or Qt. The
// formatting/redaction logic and the rotating file sink are host-buildable,
// so tests/log-handler-probe links the REAL translation unit -- not a mirror
// of it, the way tests/nv-probe already builds its own subject standalone.
// Only main.cpp (which already depends on obs.h) wires
// `pulsar_log::install_pulsar_log_handler()` to `base_set_log_handler` and
// translates libobs's LOG_* ints into `pulsar_log::Level`.
#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <fstream>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace pulsar_log {

enum class Level { Error, Warn, Info, Debug };

const char *level_name(Level level);

// Splits a "[pulsar-foo] rest of message" prefix off `message`, returning
// the tag without brackets ("pulsar-foo") in the return value and the
// message with that prefix (plus one following space, if present) stripped
// into `out_message`. Returns "libobs" and leaves `message` untouched in
// `out_message` when no bracket prefix is present.
std::string derive_subsystem(const std::string &message, std::string &out_message);

// "<ISO8601 UTC> <LEVEL> <session> <subsystem> | <message>". `session` is
// the correlation id resolved once at boot (ADR-005 §3.3, main.cpp's
// `g_log_session_id`); an empty string remains valid for any other caller
// (e.g. tests) that has none to give.
std::string format_line(Level level, const std::string &session, const std::string &subsystem,
                         const std::string &message);

// -- Redaction -------------------------------------------------------------
//
// Two layers, both required (ADR §3.2). Neither subsumes the other.

// Registry of exact secret values, populated by the component that
// receives them (e.g. main.cpp registers the WebSocket session password it
// generates at boot). Thread-safe; safe to call from the log callback.
class SecretRegistry {
public:
    void register_secret(std::string value);
    std::string redact(const std::string &line) const;

private:
    mutable std::mutex mutex_;
    std::deque<std::string> secrets_;
};

// Pattern layer: the five classes from §3.2 -- stream key field, full
// ingest URL, WebSocket password, show token (raw and `token%3D`-encoded),
// and sensitive query params (token/key/password/auth/sig) -- plus the five
// forms added by ADR-005 F1 (issue #197): stream_key=/streamKey, a key
// wrapped in apostrophes, srt:// ingest URLs, access_token=, and
// `Bearer <token>`. Active without any configuration loaded. Returns
// std::nullopt only when the redaction
// pass itself could not be trusted (e.g. a line past the safety length
// cap, where the risk of pathological regex behaviour is judged higher
// than the value of the line) -- callers MUST drop the line rather than
// write it raw (ADR §3.2, "posture d'échec, non négociable").
std::optional<std::string> redact_patterns(const std::string &line);

// Applies the pattern layer then the registry layer. std::nullopt means
// "abandon this line, do not write it anywhere".
std::optional<std::string> redact_line(const std::string &line, const SecretRegistry &registry);

// -- Rotating, ACL'd, durable file sink -------------------------------------

struct RotationConfig {
    std::size_t max_files = 10;
    std::uint64_t max_bytes = 16ull * 1024 * 1024;
    int max_age_days = 7;
    bool file_enabled = true; // PULSAR_LOG_FILE=off
};

RotationConfig rotation_config_from_env();

// Default log directory, honouring PULSAR_LOG_DIR. Never resolves under
// %APPDATA% (roaming) nor next to the executable (ADR §3.1 "Emplacement").
std::string default_log_dir();

class LogFileSink {
public:
    // Creates (or adopts) `dir`, purges files older than
    // `config.max_age_days`, enforces the count/size retention bound, then
    // opens the active log file for append. On any failure (PULSAR_LOG_FILE
    // =off, unwritable dir, an existing dir with a wider ACL than the
    // current user, disk full, ...) `opened()` is false and `error()` names
    // path+cause. Never throws, never retries.
    LogFileSink(std::string dir, RotationConfig config);

    bool opened() const { return file_.is_open(); }
    const std::string &error() const { return error_; }
    const std::string &path() const { return current_path_; }

    // Appends `line` (without a trailing newline -- one is added here) and
    // rotates first if the write would exceed max_bytes.
    void write_line(const std::string &line);

    // Writes `final_line` as the last line, then closes the file. No-op if
    // not opened. Used by the §3.6.2 kill switch to record the stop itself
    // before closing.
    void close(const std::string &final_line);

private:
    void rotate_if_needed(std::size_t incoming_bytes);
    void purge_expired_by_age();
    void enforce_retention();

    std::string dir_;
    RotationConfig config_;
    std::string current_path_;
    std::string error_;
    std::ofstream file_;
    std::uint64_t current_bytes_ = 0;
};

// -- Diagnostics (ADR-005 §3.6.1) -------------------------------------------
//
// Per-level counters since construction, plus a bounded ring of the most
// recent WARN/ERROR lines -- fed the SAME already-formatted, already-redacted
// line the handler writes to the file (never a re-read of it), so the
// extraction request has content to serve without ever opening a path.
// Host-buildable like the rest of this file; exercised directly by
// tests/log-handler-probe.
class DiagnosticsRing {
public:
    // Hard ceiling on how many lines a single extraction request can ever
    // receive, independent of how large the ring is configured to hold --
    // ADR §3.6.1: "N est plafonne cote serveur quelle que soit la demande."
    static constexpr std::size_t kServerMaxLines = 200;

    explicit DiagnosticsRing(std::size_t capacity = kServerMaxLines);

    // Increments the counter for `level` unconditionally, then -- for
    // Warn/Error only -- pushes `redacted_line` into the ring, evicting the
    // oldest entry past `capacity`.
    void record(Level level, const std::string &redacted_line);

    // Counter for `level` since construction.
    std::uint64_t count(Level level) const;

    // Up to `n` most recent WARN/ERROR lines, oldest first, clamped to
    // min(n, kServerMaxLines, lines currently held).
    std::vector<std::string> last_warn_error_lines(std::size_t n) const;

private:
    mutable std::mutex mutex_;
    std::size_t capacity_;
    std::deque<std::string> ring_;
    std::uint64_t counts_[4] = {0, 0, 0, 0}; // indexed by static_cast<int>(Level)
};

// True when `dir` (already existing) grants access to any principal other
// than the current user -- Everyone, Authenticated Users, BUILTIN\Users, or
// any other specific account/group. Windows-only ACL check; always false on
// other platforms (Pulsar targets Windows).
bool directory_is_more_permissive_than_current_user(const std::string &dir);

// Creates `dir` (and parents) with a DACL restricted to the current user
// only, explicitly set at creation (never inherited). Returns false on
// failure. Windows-only ACL; other platforms just create the directory.
bool create_directory_with_current_user_acl(const std::string &dir);

} // namespace pulsar_log
