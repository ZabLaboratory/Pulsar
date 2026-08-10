// Regression gate for issue #183 (ADR-005 §3.1-§3.2 log handler).
//
// Links the REAL plugins/pulsar-headless/log-handler.cpp translation unit
// -- not a mirror -- because that file deliberately never includes obs.h /
// Qt (see log-handler.h's header comment), so it is host-buildable like
// tests/nv-probe and tests/ws-debug-probe already are for their own
// subjects.
//
// RC9: the redactor's two layers (pattern, registry) exercised separately,
// over a corpus of forms: value registered nude, embedded in a URL,
// repeated in the line, different field-name casing, an rtmp(s):// URL
// carrying an UNregistered secret, and a redaction-failure case proving
// the line is abandoned rather than written raw.
// RC1: format_line matches the §3.1 gabarit.
// RC8/RC19: rotation stays under max_files, age-based purge fires even
// under the size/count bounds.
// RC21/RC22: ACL posted at creation is restricted to the current user; a
// directory an operator widened is refused; an unwritable directory
// degrades to an explicit error rather than throwing.

#include "log-handler.h"

#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <random>
#include <regex>
#include <sstream>
#include <string>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <aclapi.h>
#endif

namespace fs = std::filesystem;

namespace {

// Creates the directory with the SAME restricted ACL LogFileSink itself
// would set on a fresh directory -- so a test that needs the directory to
// pre-exist (e.g. to seed a stale file before construction) doesn't
// accidentally fail the "existing dir, ACL too wide" refusal path just
// because the OS temp folder's own default ACL is broader than one user.
fs::path make_temp_dir(const char *label)
{
    std::mt19937_64 rng(std::random_device{}());
    fs::path p = fs::temp_directory_path() /
                 ("pulsar-log-handler-test-" + std::string(label) + "-" +
                  std::to_string(rng()));
    bool ok = pulsar_log::create_directory_with_current_user_acl(p.string());
    assert(ok);
    return p;
}

std::string read_file(const fs::path &p)
{
    std::ifstream in(p, std::ios::binary);
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

#ifdef _WIN32
// Overwrites `dir`'s DACL with a single Everyone-allow ACE, simulating an
// operator having widened PULSAR_LOG_DIR's permissions.
void grant_everyone_access(const fs::path &dir)
{
    BYTE world_sid_buf[SECURITY_MAX_SID_SIZE];
    DWORD sz = sizeof(world_sid_buf);
    bool ok = CreateWellKnownSid(WinWorldSid, nullptr, world_sid_buf, &sz) != 0;
    assert(ok);

    EXPLICIT_ACCESSA ea{};
    ea.grfAccessPermissions = GENERIC_ALL;
    ea.grfAccessMode = SET_ACCESS;
    ea.grfInheritance = NO_INHERITANCE;
    ea.Trustee.TrusteeForm = TRUSTEE_IS_SID;
    ea.Trustee.TrusteeType = TRUSTEE_IS_WELL_KNOWN_GROUP;
    ea.Trustee.ptstrName = reinterpret_cast<LPSTR>(world_sid_buf);

    PACL acl = nullptr;
    DWORD res = SetEntriesInAclA(1, &ea, nullptr, &acl);
    assert(res == ERROR_SUCCESS);

    std::string path_str = dir.string();
    res = SetNamedSecurityInfoA(const_cast<LPSTR>(path_str.c_str()), SE_FILE_OBJECT,
                                 DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                                 nullptr, nullptr, acl, nullptr);
    LocalFree(acl);
    assert(res == ERROR_SUCCESS);
}
#endif

void test_format_line_matches_gabarit()
{
    std::string line =
        pulsar_log::format_line(pulsar_log::Level::Warn, "", "pulsar-headless", "hello world");

    // <ISO8601 UTC> <LEVEL> <session> <subsystem> | <message>
    static const std::regex gabarit(
        R"(^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z WARN  pulsar-headless \| hello world$)");
    assert(std::regex_match(line, gabarit));

    // Non-empty session field lands between the two single spaces too.
    std::string with_session =
        pulsar_log::format_line(pulsar_log::Level::Error, "sess-abc", "libobs", "boom");
    static const std::regex gabarit_session(
        R"(^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ERROR sess-abc libobs \| boom$)");
    assert(std::regex_match(with_session, gabarit_session));
}

void test_derive_subsystem()
{
    std::string rest;
    std::string tag = pulsar_log::derive_subsystem("[pulsar-multi-stream] adaptive armed", rest);
    assert(tag == "pulsar-multi-stream");
    assert(rest == "adaptive armed");

    std::string rest2;
    std::string tag2 = pulsar_log::derive_subsystem("no bracket prefix here", rest2);
    assert(tag2 == "libobs");
    assert(rest2 == "no bracket prefix here");
}

void test_pattern_layer_url_and_query_params_unregistered()
{
    // rtmp:// / rtmps:// URLs, INCLUDING the path/key, redacted even when
    // the value was never registered -- the pattern layer stands alone.
    auto r1 = pulsar_log::redact_patterns("connecting to rtmp://ingest.example/live/sk_live_abc123");
    assert(r1.has_value());
    assert(r1->find("sk_live_abc123") == std::string::npos);
    assert(r1->find("rtmp://[REDACTED]") != std::string::npos);

    auto r2 = pulsar_log::redact_patterns("rtmps://ingest.example/app/other_secret_key");
    assert(r2.has_value());
    assert(r2->find("other_secret_key") == std::string::npos);

    // Sensitive query params, several casings.
    auto r3 = pulsar_log::redact_patterns("GET /source?Token=abc&Auth=def&sig=ghi");
    assert(r3.has_value());
    assert(r3->find("abc") == std::string::npos);
    assert(r3->find("def") == std::string::npos);
    assert(r3->find("ghi") == std::string::npos);

    // token%3D encoded form.
    auto r4 = pulsar_log::redact_patterns("source url ...token%3Dshowtoken123...");
    assert(r4.has_value());
    assert(r4->find("showtoken123") == std::string::npos);

    // key field, different casing of the field name (not the value).
    auto r5 = pulsar_log::redact_patterns(R"(destination settings: "Key": "flowkey987")");
    assert(r5.has_value());
    assert(r5->find("flowkey987") == std::string::npos);

    auto r6 = pulsar_log::redact_patterns("server_password=hunter2pw");
    assert(r6.has_value());
    assert(r6->find("hunter2pw") == std::string::npos);
}

void test_pattern_layer_leaves_ordinary_text_alone()
{
    auto r = pulsar_log::redact_patterns("pulsar-headless: video 1920x1080 @ 60 fps");
    assert(r.has_value());
    assert(*r == "pulsar-headless: video 1920x1080 @ 60 fps");
}

void test_pattern_layer_abandons_oversized_line()
{
    // Deterministic trigger for the documented failure posture: a line
    // past the safety cap is dropped rather than risk pathological regex
    // cost on untrusted input, and it must never be written raw.
    std::string huge(64 * 1024, 'a');
    auto r = pulsar_log::redact_patterns(huge);
    assert(!r.has_value());
}

void test_registry_layer_bare_embedded_repeated()
{
    pulsar_log::SecretRegistry registry;
    registry.register_secret("SUPERSECRETKEY99");

    // Bare, nude occurrence.
    auto bare = pulsar_log::redact_line("stream key accepted: SUPERSECRETKEY99", registry);
    assert(bare.has_value());
    assert(bare->find("SUPERSECRETKEY99") == std::string::npos);
    assert(bare->find("[REDACTED]") != std::string::npos);

    // Embedded inside an otherwise-unstructured string (not caught by the
    // pattern layer's field/URL forms -- only the registry can catch this).
    auto embedded =
        pulsar_log::redact_line("dump: prefix-SUPERSECRETKEY99-suffix", registry);
    assert(embedded.has_value());
    assert(embedded->find("SUPERSECRETKEY99") == std::string::npos);

    // Repeated twice on the same line -- both occurrences must go.
    auto repeated = pulsar_log::redact_line(
        "SUPERSECRETKEY99 seen again: SUPERSECRETKEY99", registry);
    assert(repeated.has_value());
    assert(repeated->find("SUPERSECRETKEY99") == std::string::npos);

    // The two layers are exercised separately: pattern layer alone (empty
    // registry) does NOT know about this bare value -- it has no
    // recognizable form -- so it must survive un-redacted at that layer.
    auto pattern_only = pulsar_log::redact_patterns("bare value SUPERSECRETKEY99 with no field");
    assert(pattern_only.has_value());
    assert(pattern_only->find("SUPERSECRETKEY99") != std::string::npos);
}

void test_redact_line_abandons_when_pattern_layer_fails()
{
    pulsar_log::SecretRegistry registry;
    registry.register_secret("irrelevant");
    std::string huge(64 * 1024, 'x');
    auto r = pulsar_log::redact_line(huge, registry);
    assert(!r.has_value());
}

void test_rotation_stays_under_max_files()
{
    fs::path dir = make_temp_dir("rotation");
    pulsar_log::RotationConfig cfg;
    cfg.max_files = 3;
    cfg.max_bytes = 200; // tiny, forces rotation almost every write
    cfg.max_age_days = 7;

    {
        pulsar_log::LogFileSink sink(dir.string(), cfg);
        assert(sink.opened());
        for (int i = 0; i < 30; ++i)
            sink.write_line("line " + std::to_string(i) + " padding padding padding");
    }

    std::size_t log_file_count = 0;
    std::error_code ec;
    for (const auto &entry : fs::directory_iterator(dir, ec)) {
        if (entry.path().extension() == ".log")
            ++log_file_count;
    }
    assert(log_file_count <= cfg.max_files);
    assert(log_file_count >= 1);

    fs::remove_all(dir);
}

void test_age_purge_fires_under_size_and_count_bounds()
{
    fs::path dir = make_temp_dir("age-purge");
    pulsar_log::RotationConfig cfg;
    cfg.max_files = 10;             // not exceeded
    cfg.max_bytes = 16ull * 1024 * 1024; // not exceeded
    cfg.max_age_days = 7;

    fs::path stale = dir / "pulsar.20200101T000000Z.log";
    {
        std::ofstream out(stale, std::ios::binary);
        out << "old line\n";
    }
    auto old_time = fs::file_time_type::clock::now() - std::chrono::hours(24 * 30);
    std::error_code time_ec;
    fs::last_write_time(stale, old_time, time_ec);
    assert(!time_ec);

    {
        pulsar_log::LogFileSink sink(dir.string(), cfg);
        assert(sink.opened());
    }

    assert(!fs::exists(stale));
    fs::remove_all(dir);
}

void test_acl_restricted_at_creation_and_widened_dir_refused()
{
#ifdef _WIN32
    fs::path parent = make_temp_dir("acl-parent");
    fs::path fresh = parent / "restricted";

    pulsar_log::RotationConfig cfg;
    {
        pulsar_log::LogFileSink sink(fresh.string(), cfg);
        assert(sink.opened());
    }
    assert(!pulsar_log::directory_is_more_permissive_than_current_user(fresh.string()));

    grant_everyone_access(fresh);
    assert(pulsar_log::directory_is_more_permissive_than_current_user(fresh.string()));

    // A LogFileSink pointed at the now-widened directory must refuse to
    // write and report why, rather than open the file anyway.
    pulsar_log::LogFileSink widened(fresh.string(), cfg);
    assert(!widened.opened());
    assert(!widened.error().empty());

    fs::remove_all(parent);
#endif
}

void test_unwritable_directory_degrades_with_named_error()
{
    fs::path parent = make_temp_dir("unwritable");
    fs::path blocked = parent / "blocked";
    {
        // Occupy the path with a regular FILE so it can never be adopted
        // as a log directory -- deterministic "not writable as a
        // directory" trigger, independent of ACL/OS permission games.
        std::ofstream out(blocked, std::ios::binary);
        out << "not a directory";
    }

    pulsar_log::RotationConfig cfg;
    pulsar_log::LogFileSink sink(blocked.string(), cfg);
    assert(!sink.opened());
    assert(sink.error().find(blocked.string()) != std::string::npos);

    fs::remove_all(parent);
}

} // namespace

int main()
{
    test_format_line_matches_gabarit();
    test_derive_subsystem();
    test_pattern_layer_url_and_query_params_unregistered();
    test_pattern_layer_leaves_ordinary_text_alone();
    test_pattern_layer_abandons_oversized_line();
    test_registry_layer_bare_embedded_repeated();
    test_redact_line_abandons_when_pattern_layer_fails();
    test_rotation_stays_under_max_files();
    test_age_purge_fires_under_size_and_count_bounds();
    test_acl_restricted_at_creation_and_widened_dir_refused();
    test_unwritable_directory_degrades_with_named_error();

    std::fprintf(stdout, "log-handler-probe-test: all assertions passed\n");
    return EXIT_SUCCESS;
}
