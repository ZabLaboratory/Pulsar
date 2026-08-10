#include "log-handler.h"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <regex>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <aclapi.h>
#endif

namespace pulsar_log {

namespace {

namespace fs = std::filesystem;

// A line whose redaction we won't attempt: real log lines are printf-style
// libobs messages, capped well under this by main.cpp's own vsnprintf
// buffer. Anything past this is treated as an untrusted/pathological input
// rather than risk catastrophic-backtracking cost in the pattern layer on
// it -- abandoning the line is the documented failure posture (§3.2).
constexpr std::size_t kMaxRedactableLineBytes = 32 * 1024;

std::string iso8601_utc_now()
{
    using namespace std::chrono;
    const auto now = system_clock::now();
    const std::time_t t = system_clock::to_time_t(now);
    std::tm tm_utc{};
#ifdef _WIN32
    gmtime_s(&tm_utc, &t);
#else
    gmtime_r(&t, &tm_utc);
#endif
    char buf[32];
    std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm_utc);
    return std::string(buf);
}

std::uint64_t env_u64(const char *name, std::uint64_t fallback)
{
    if (const char *e = std::getenv(name); e && *e) {
        char *end = nullptr;
        unsigned long long v = std::strtoull(e, &end, 10);
        if (end != e && v > 0)
            return v;
    }
    return fallback;
}

int env_int(const char *name, int fallback)
{
    if (const char *e = std::getenv(name); e && *e) {
        char *end = nullptr;
        long v = std::strtol(e, &end, 10);
        if (end != e && v > 0)
            return static_cast<int>(v);
    }
    return fallback;
}

bool is_rotated_pulsar_log(const fs::path &p)
{
    const std::string name = p.filename().string();
    return name.rfind("pulsar", 0) == 0 && p.extension() == ".log" && name != "pulsar.log";
}

bool is_any_pulsar_log(const fs::path &p)
{
    const std::string name = p.filename().string();
    return name.rfind("pulsar", 0) == 0 && p.extension() == ".log";
}

} // namespace

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

const char *level_name(Level level)
{
    switch (level) {
    case Level::Error:
        return "ERROR";
    case Level::Warn:
        return "WARN";
    case Level::Info:
        return "INFO";
    case Level::Debug:
        return "DEBUG";
    }
    return "INFO";
}

std::string derive_subsystem(const std::string &message, std::string &out_message)
{
    if (!message.empty() && message.front() == '[') {
        const std::size_t close = message.find(']');
        if (close != std::string::npos && close > 1) {
            std::string tag = message.substr(1, close - 1);
            std::size_t rest = close + 1;
            if (rest < message.size() && message[rest] == ' ')
                ++rest;
            out_message = message.substr(rest);
            return tag;
        }
    }
    out_message = message;
    return "libobs";
}

std::string format_line(Level level, const std::string &session, const std::string &subsystem,
                         const std::string &message)
{
    std::string out;
    out.reserve(message.size() + subsystem.size() + session.size() + 48);
    out += iso8601_utc_now();
    out += ' ';
    out += level_name(level);
    out += ' ';
    out += session; // reserved field, may be empty
    out += ' ';
    out += subsystem;
    out += " | ";
    out += message;
    return out;
}

// ---------------------------------------------------------------------------
// Redaction
// ---------------------------------------------------------------------------

void SecretRegistry::register_secret(std::string value)
{
    if (value.empty())
        return;
    std::lock_guard<std::mutex> lock(mutex_);
    secrets_.push_back(std::move(value));
}

std::string SecretRegistry::redact(const std::string &line) const
{
    std::vector<std::string> ordered;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        ordered = secrets_;
    }
    // Longest-first: a secret that happens to be a substring of another
    // registered secret must not be partially redacted by the shorter one
    // first, which would leave a fragment of the longer one exposed.
    std::sort(ordered.begin(), ordered.end(),
              [](const std::string &a, const std::string &b) { return a.size() > b.size(); });

    std::string out = line;
    for (const auto &secret : ordered) {
        if (secret.empty())
            continue;
        std::size_t pos = 0;
        while ((pos = out.find(secret, pos)) != std::string::npos) {
            out.replace(pos, secret.size(), "[REDACTED]");
            pos += 10; // length of "[REDACTED]"
        }
    }
    return out;
}

std::optional<std::string> redact_patterns(const std::string &line)
{
    if (line.size() > kMaxRedactableLineBytes)
        return std::nullopt;

    try {
        static const std::vector<std::pair<std::regex, std::string>> rules = {
            // Full ingest URL: scheme kept for readability, everything else
            // (host, path, stream key) redacted as one block -- RTMP carries
            // the key in the path/query, not a separate field.
            {std::regex(R"((rtmps?://)[^\s"'<>]+)", std::regex::icase), "$1[REDACTED]"},
            // token%3D<value> -- percent-encoded token seen inside a
            // recomposed query string (e.g. a browser-source URL).
            {std::regex(R"(token%3[dD][^\s&"'<>]*)", std::regex::icase), "token%3D[REDACTED]"},
            // Stream key field: key=<value> / "key": "<value>" / key: <value>.
            {std::regex(R"(("?\bkey"?\s*[:=]\s*"?)([^\s"'&,<>]+))", std::regex::icase),
             "$1[REDACTED]"},
            // WebSocket password: server_password and any bare password field.
            {std::regex(R"(("?\b(?:server_)?password"?\s*[:=]\s*"?)([^\s"'&,<>]+))",
                        std::regex::icase),
             "$1[REDACTED]"},
            // Show token, raw form (field or query).
            {std::regex(R"(("?\btoken"?\s*[:=]\s*"?)([^\s"'&,<>]+))", std::regex::icase),
             "$1[REDACTED]"},
            // Remaining sensitive query params not already covered above.
            {std::regex(R"([?&](auth|sig)=([^&\s"'<>]+))", std::regex::icase), "&$1=[REDACTED]"},
        };

        std::string out = line;
        for (const auto &rule : rules)
            out = std::regex_replace(out, rule.first, rule.second);
        return out;
    } catch (const std::exception &) {
        return std::nullopt;
    }
}

std::optional<std::string> redact_line(const std::string &line, const SecretRegistry &registry)
{
    auto patterned = redact_patterns(line);
    if (!patterned)
        return std::nullopt;
    return registry.redact(*patterned);
}

// ---------------------------------------------------------------------------
// Environment / defaults
// ---------------------------------------------------------------------------

RotationConfig rotation_config_from_env()
{
    RotationConfig cfg;
    cfg.max_files = static_cast<std::size_t>(env_u64("PULSAR_LOG_MAX_FILES", cfg.max_files));
    cfg.max_bytes = env_u64("PULSAR_LOG_MAX_BYTES", cfg.max_bytes);
    cfg.max_age_days = env_int("PULSAR_LOG_MAX_AGE_DAYS", cfg.max_age_days);
    if (const char *e = std::getenv("PULSAR_LOG_FILE"); e && *e) {
        std::string v(e);
        for (auto &c : v)
            c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        cfg.file_enabled = (v != "off" && v != "0" && v != "false");
    }
    return cfg;
}

std::string default_log_dir()
{
    if (const char *e = std::getenv("PULSAR_LOG_DIR"); e && *e)
        return e;
#ifdef _WIN32
    if (const char *local = std::getenv("LOCALAPPDATA"); local && *local) {
        fs::path p(local);
        p /= "Pulsar";
        p /= "logs";
        return p.string();
    }
#endif
    // LOCALAPPDATA unset is not expected on a real Windows session; degrade
    // to a relative path rather than %APPDATA% or the executable's own
    // directory, both explicitly excluded by §3.1. Open failure from here
    // is a legitimate stderr-only degrade, not a crash.
    return "Pulsar-logs";
}

// ---------------------------------------------------------------------------
// ACL
// ---------------------------------------------------------------------------

#ifdef _WIN32
namespace {

bool get_current_user_sid(std::vector<BYTE> &sid_buffer, PSID &sid_out)
{
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token))
        return false;
    DWORD needed = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &needed);
    if (needed == 0) {
        CloseHandle(token);
        return false;
    }
    sid_buffer.resize(needed);
    const bool ok = GetTokenInformation(token, TokenUser, sid_buffer.data(), needed, &needed) != 0;
    CloseHandle(token);
    if (!ok)
        return false;
    sid_out = reinterpret_cast<PTOKEN_USER>(sid_buffer.data())->User.Sid;
    return true;
}

bool sid_is_well_known_broad(PSID sid)
{
    static const WELL_KNOWN_SID_TYPE broad[] = {
        WinWorldSid,             // Everyone
        WinAuthenticatedUserSid, // NT AUTHORITY\Authenticated Users
        WinBuiltinUsersSid,      // BUILTIN\Users
    };
    BYTE buf[SECURITY_MAX_SID_SIZE];
    for (auto type : broad) {
        DWORD sz = sizeof(buf);
        if (CreateWellKnownSid(type, nullptr, buf, &sz) &&
            EqualSid(sid, reinterpret_cast<PSID>(buf)))
            return true;
    }
    return false;
}

} // namespace

bool create_directory_with_current_user_acl(const std::string &dir)
{
    std::error_code ec;
    fs::create_directories(dir, ec);
    if (ec)
        return false;

    std::vector<BYTE> sid_buf;
    PSID user_sid = nullptr;
    if (!get_current_user_sid(sid_buf, user_sid))
        return false;

    EXPLICIT_ACCESSA ea{};
    ea.grfAccessPermissions = GENERIC_ALL;
    ea.grfAccessMode = SET_ACCESS;
    ea.grfInheritance = NO_INHERITANCE;
    ea.Trustee.TrusteeForm = TRUSTEE_IS_SID;
    ea.Trustee.TrusteeType = TRUSTEE_IS_USER;
    ea.Trustee.ptstrName = reinterpret_cast<LPSTR>(user_sid);

    PACL acl = nullptr;
    if (SetEntriesInAclA(1, &ea, nullptr, &acl) != ERROR_SUCCESS)
        return false;

    const DWORD res = SetNamedSecurityInfoA(
        const_cast<LPSTR>(dir.c_str()), SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION, nullptr, nullptr, acl,
        nullptr);
    LocalFree(acl);
    return res == ERROR_SUCCESS;
}

bool directory_is_more_permissive_than_current_user(const std::string &dir)
{
    PACL dacl = nullptr;
    PSECURITY_DESCRIPTOR sd = nullptr;
    const DWORD res =
        GetNamedSecurityInfoA(const_cast<LPSTR>(dir.c_str()), SE_FILE_OBJECT,
                               DACL_SECURITY_INFORMATION, nullptr, nullptr, &dacl, nullptr, &sd);
    if (res != ERROR_SUCCESS || dacl == nullptr) {
        if (sd)
            LocalFree(sd);
        // Unreadable ACL: treat as unknown-permissive rather than assume
        // safety -- refusing to write is the safe failure direction here.
        return true;
    }

    std::vector<BYTE> sid_buf;
    PSID user_sid = nullptr;
    const bool have_user = get_current_user_sid(sid_buf, user_sid);

    bool permissive = false;
    for (WORD i = 0; i < dacl->AceCount; ++i) {
        LPVOID ace = nullptr;
        if (!GetAce(dacl, i, &ace))
            continue;
        const auto *header = reinterpret_cast<ACE_HEADER *>(ace);
        if (header->AceType != ACCESS_ALLOWED_ACE_TYPE)
            continue;
        auto *allowed = reinterpret_cast<ACCESS_ALLOWED_ACE *>(ace);
        PSID ace_sid = reinterpret_cast<PSID>(&allowed->SidStart);
        if (sid_is_well_known_broad(ace_sid)) {
            permissive = true;
            break;
        }
        if (have_user && !EqualSid(ace_sid, user_sid)) {
            permissive = true;
            break;
        }
    }
    LocalFree(sd);
    return permissive;
}
#else  // !_WIN32
bool create_directory_with_current_user_acl(const std::string &dir)
{
    std::error_code ec;
    fs::create_directories(dir, ec);
    return !ec;
}

bool directory_is_more_permissive_than_current_user(const std::string &)
{
    return false;
}
#endif // _WIN32

// ---------------------------------------------------------------------------
// LogFileSink
// ---------------------------------------------------------------------------

LogFileSink::LogFileSink(std::string dir, RotationConfig config)
    : dir_(std::move(dir)), config_(config)
{
    if (!config_.file_enabled) {
        error_ = "PULSAR_LOG_FILE=off";
        return;
    }

    std::error_code exists_ec;
    const bool existed = fs::exists(dir_, exists_ec);

    if (!existed) {
        if (!create_directory_with_current_user_acl(dir_)) {
            error_ = "could not create log directory with a restricted ACL: " + dir_;
            return;
        }
    } else if (directory_is_more_permissive_than_current_user(dir_)) {
        error_ = "log directory ACL is broader than the current user, refusing to write: " + dir_;
        return;
    }

    purge_expired_by_age();
    enforce_retention();

    current_path_ = (fs::path(dir_) / "pulsar.log").string();
    file_.open(current_path_, std::ios::binary | std::ios::app);
    if (!file_.is_open()) {
        error_ = "could not open log file for append: " + current_path_;
        return;
    }

    std::error_code size_ec;
    const auto size = fs::file_size(current_path_, size_ec);
    current_bytes_ = size_ec ? 0 : static_cast<std::uint64_t>(size);
}

void LogFileSink::write_line(const std::string &line)
{
    if (!opened())
        return;
    std::string record = line;
    record += '\n';
    rotate_if_needed(record.size());
    file_ << record;
    file_.flush();
    current_bytes_ += record.size();
}

void LogFileSink::close(const std::string &final_line)
{
    if (!opened())
        return;
    write_line(final_line);
    file_.close();
}

void LogFileSink::rotate_if_needed(std::size_t incoming_bytes)
{
    if (current_bytes_ == 0 || current_bytes_ + incoming_bytes <= config_.max_bytes)
        return;

    file_.close();

    const std::string rotated_name = "pulsar." + iso8601_utc_now() + ".log";
    // Colons are not valid in a Windows filename; iso8601_utc_now() only
    // produces them between hour/minute/second, so scrub them here rather
    // than change the shared timestamp format used in log lines themselves.
    std::string safe_name = rotated_name;
    std::replace(safe_name.begin(), safe_name.end(), ':', '-');

    std::error_code rename_ec;
    fs::rename(current_path_, fs::path(dir_) / safe_name, rename_ec);

    enforce_retention();

    file_.open(current_path_, std::ios::binary | std::ios::trunc);
    current_bytes_ = 0;
}

void LogFileSink::purge_expired_by_age()
{
    if (config_.max_age_days <= 0)
        return;

    const auto cutoff = std::chrono::system_clock::now() -
                         std::chrono::hours(24 * static_cast<long long>(config_.max_age_days));

    std::error_code it_ec;
    for (const auto &entry : fs::directory_iterator(dir_, it_ec)) {
        if (it_ec)
            break;
        if (!entry.is_regular_file() || !is_any_pulsar_log(entry.path()))
            continue;

        std::error_code mt_ec;
        const auto ftime = fs::last_write_time(entry.path(), mt_ec);
        if (mt_ec)
            continue;

        // Pre-C++20 file_time_type -> system_clock::time_point conversion.
        const auto sctp = std::chrono::time_point_cast<std::chrono::system_clock::duration>(
            ftime - decltype(ftime)::clock::now() + std::chrono::system_clock::now());
        if (sctp < cutoff) {
            std::error_code rm_ec;
            fs::remove(entry.path(), rm_ec);
        }
    }
}

void LogFileSink::enforce_retention()
{
    if (config_.max_files == 0)
        return;

    std::vector<fs::directory_entry> rotated;
    std::error_code it_ec;
    for (const auto &entry : fs::directory_iterator(dir_, it_ec)) {
        if (it_ec)
            break;
        if (entry.is_regular_file() && is_rotated_pulsar_log(entry.path()))
            rotated.push_back(entry);
    }

    // The active pulsar.log counts toward max_files too.
    if (rotated.size() + 1 <= config_.max_files)
        return;

    std::sort(rotated.begin(), rotated.end(), [](const auto &a, const auto &b) {
        std::error_code ea, eb;
        return fs::last_write_time(a.path(), ea) < fs::last_write_time(b.path(), eb);
    });

    std::size_t to_delete = rotated.size() + 1 - config_.max_files;
    for (std::size_t i = 0; i < to_delete && i < rotated.size(); ++i) {
        std::error_code rm_ec;
        fs::remove(rotated[i].path(), rm_ec);
    }
}

} // namespace pulsar_log
