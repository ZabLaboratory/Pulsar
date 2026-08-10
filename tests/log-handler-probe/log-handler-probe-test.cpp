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
    auto r1 = pulsar_log::redact_patterns(
        "connecting to rtmp://ingest.example/live/sk_live_abc123"); // pragma: allowlist secret
    assert(r1.has_value());
    assert(r1->find("sk_live_abc123") == std::string::npos);
    assert(r1->find("rtmp://[REDACTED]") != std::string::npos);

    auto r2 = pulsar_log::redact_patterns(
        "rtmps://ingest.example/app/other_secret_key"); // pragma: allowlist secret
    assert(r2.has_value());
    assert(r2->find("other_secret_key") == std::string::npos);

    // Sensitive query params, several casings.
    auto r3 = pulsar_log::redact_patterns(
        "GET /source?Token=abc&Auth=def&sig=ghi"); // pragma: allowlist secret
    assert(r3.has_value());
    assert(r3->find("abc") == std::string::npos);
    assert(r3->find("def") == std::string::npos);
    assert(r3->find("ghi") == std::string::npos);

    // token%3D encoded form.
    auto r4 = pulsar_log::redact_patterns(
        "source url ...token%3Dshowtoken123..."); // pragma: allowlist secret
    assert(r4.has_value());
    assert(r4->find("showtoken123") == std::string::npos);

    // key field, different casing of the field name (not the value).
    auto r5 = pulsar_log::redact_patterns(
        R"(destination settings: "Key": "flowkey987")"); // pragma: allowlist secret
    assert(r5.has_value());
    assert(r5->find("flowkey987") == std::string::npos);

    auto r6 = pulsar_log::redact_patterns("server_password=hunter2pw"); // pragma: allowlist secret
    assert(r6.has_value());
    assert(r6->find("hunter2pw") == std::string::npos);
}

// ADR-005 F1 (issue #197): the five forms Bastion's rejeu of the pattern
// layer found leaking -- stream_key=/streamKey (the plain \bkey\b form does
// not break on "_"), a key wrapped in apostrophes, srt:// ingest URLs,
// access_token= (same underscore-boundary gap as stream_key), and
// `Bearer <token>`.
void test_pattern_layer_covers_adr005_f1_forms()
{
    auto r1 = pulsar_log::redact_patterns(
        "stream_key=live_abcDEF123secret"); // pragma: allowlist secret
    assert(r1.has_value());
    assert(r1->find("live_abcDEF123secret") == std::string::npos);
    assert(r1->find("stream_key=[REDACTED]") != std::string::npos);

    auto r2 = pulsar_log::redact_patterns(
        "streamKey=camelCaseSecret999"); // pragma: allowlist secret
    assert(r2.has_value());
    assert(r2->find("camelCaseSecret999") == std::string::npos);

    auto r3 = pulsar_log::redact_patterns(
        "destination created: key='quotedSecretValue42'"); // pragma: allowlist secret
    assert(r3.has_value());
    assert(r3->find("quotedSecretValue42") == std::string::npos);

    auto r4 = pulsar_log::redact_patterns(
        "connecting to srt://ingest.example:9998?streamid=srtSecretPath77"); // pragma: allowlist secret
    assert(r4.has_value());
    assert(r4->find("srtSecretPath77") == std::string::npos);
    assert(r4->find("srt://[REDACTED]") != std::string::npos);

    auto r5 = pulsar_log::redact_patterns(
        "GET /source?access_token=accessTokenSecret55"); // pragma: allowlist secret
    assert(r5.has_value());
    assert(r5->find("accessTokenSecret55") == std::string::npos);

    auto r6 = pulsar_log::redact_patterns(
        "Authorization: Bearer bearerTokenSecret.part-2"); // pragma: allowlist secret
    assert(r6.has_value());
    assert(r6->find("bearerTokenSecret.part-2") == std::string::npos);
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
    // Named once so the literal appears on exactly one line (with its
    // allowlist pragma) instead of being repeated -- and re-flagged --
    // across every assertion below.
    static const std::string kFixtureSecret = "SUPERSECRETKEY99"; // pragma: allowlist secret

    pulsar_log::SecretRegistry registry;
    registry.register_secret(kFixtureSecret);

    // Bare, nude occurrence.
    auto bare = pulsar_log::redact_line("stream key accepted: " + kFixtureSecret, registry);
    assert(bare.has_value());
    assert(bare->find(kFixtureSecret) == std::string::npos);
    assert(bare->find("[REDACTED]") != std::string::npos);

    // Embedded inside an otherwise-unstructured string (not caught by the
    // pattern layer's field/URL forms -- only the registry can catch this).
    auto embedded =
        pulsar_log::redact_line("dump: prefix-" + kFixtureSecret + "-suffix", registry);
    assert(embedded.has_value());
    assert(embedded->find(kFixtureSecret) == std::string::npos);

    // Repeated twice on the same line -- both occurrences must go.
    auto repeated =
        pulsar_log::redact_line(kFixtureSecret + " seen again: " + kFixtureSecret, registry);
    assert(repeated.has_value());
    assert(repeated->find(kFixtureSecret) == std::string::npos);

    // The two layers are exercised separately: pattern layer alone (empty
    // registry) does NOT know about this bare value -- it has no
    // recognizable form -- so it must survive un-redacted at that layer.
    // This is exactly the shape of leak ADR-005 F1 (issue #197) found: a
    // Twitch stream key reaching the log with no recognizable field/URL
    // form around it, and nothing ever calling register_secret() for it.
    const std::string bare_line = "bare value " + kFixtureSecret + " with no field";
    auto pattern_only = pulsar_log::redact_patterns(bare_line);
    assert(pattern_only.has_value());
    assert(pattern_only->find(kFixtureSecret) != std::string::npos);

    // Regression gate for the F1 fix: pulsar-multi-stream now calls
    // pulsar_log_register_secret (cross-DLL proc, main.cpp) the moment it
    // receives a stream key, before that key ever reaches a log line. Once
    // registered -- exactly what `registry` above simulates -- the SAME
    // bare line the pattern layer alone cannot catch MUST be caught by the
    // full pipeline (redact_line = pattern + registry). If the cross-DLL
    // registration wiring regresses (proc removed, call site dropped, or
    // the key is registered too late), this line leaks nude again and this
    // assertion fails.
    auto full_pipeline = pulsar_log::redact_line(bare_line, registry);
    assert(full_pipeline.has_value());
    assert(full_pipeline->find(kFixtureSecret) == std::string::npos);
    assert(full_pipeline->find("[REDACTED]") != std::string::npos);
}

void test_registry_rejects_short_dedups_and_caps()
{
    pulsar_log::SecretRegistry registry;

    // Below the credential floor: never enters the registry, so it must
    // never get redacted out of a line -- this is what keeps a
    // validation-rejected/garbage `key` from polluting every future line.
    static const std::string kShort = "short7c"; // pragma: allowlist secret, 7 chars
    registry.register_secret(kShort);
    auto short_line = pulsar_log::redact_line("value=" + kShort + " end", registry);
    assert(short_line.has_value());
    assert(short_line->find(kShort) != std::string::npos);

    // Re-registering the same value (e.g. CreateDestination called twice
    // for the same destination/key) must not grow the registry: register
    // it 300 times, then register 300 distinct secrets on top -- if dedup
    // were missing, the repeated one alone would already have evicted
    // itself out of the (small) cap.
    static const std::string kRepeated = "REPEATEDSTREAMKEY01"; // pragma: allowlist secret
    for (int i = 0; i < 300; ++i)
        registry.register_secret(kRepeated);
    auto repeated_line = pulsar_log::redact_line("k=" + kRepeated, registry);
    assert(repeated_line.has_value());
    assert(repeated_line->find(kRepeated) == std::string::npos);

    // Cap with FIFO eviction: push well past the registry's cap with
    // distinct secrets. The earliest one registered (before the dedup
    // block above even ran) must have aged out; the most recent one must
    // still be present and redacted.
    static const std::string kOldest = "OLDESTEVICTEDSECRET03"; // pragma: allowlist secret
    pulsar_log::SecretRegistry cap_registry;
    cap_registry.register_secret(kOldest);
    std::string last_secret;
    for (int i = 0; i < 300; ++i) {
        last_secret = "DISTINCTSTREAMKEYNUMBER" + std::to_string(i) + "PAD"; // pragma: allowlist secret
        cap_registry.register_secret(last_secret);
    }

    auto oldest_line = pulsar_log::redact_line("k=" + kOldest, cap_registry);
    assert(oldest_line.has_value());
    assert(oldest_line->find(kOldest) != std::string::npos); // evicted, no longer redacted

    auto newest_line = pulsar_log::redact_line("k=" + last_secret, cap_registry);
    assert(newest_line.has_value());
    assert(newest_line->find(last_secret) == std::string::npos);
}

void test_redact_line_abandons_when_pattern_layer_fails()
{
    pulsar_log::SecretRegistry registry;
    registry.register_secret("irrelevant"); // pragma: allowlist secret
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

// RC14: counters concord with an independent count, and a bounded ring
// holds only WARN/ERROR lines, most-recent last.
void test_diagnostics_ring_counts_and_ring_content()
{
    pulsar_log::DiagnosticsRing ring(/*capacity=*/3);

    ring.record(pulsar_log::Level::Info, "info line 1");
    ring.record(pulsar_log::Level::Warn, "warn line 1");
    ring.record(pulsar_log::Level::Error, "error line 1");
    ring.record(pulsar_log::Level::Debug, "debug line 1");
    ring.record(pulsar_log::Level::Info, "info line 2");

    assert(ring.count(pulsar_log::Level::Info) == 2);
    assert(ring.count(pulsar_log::Level::Warn) == 1);
    assert(ring.count(pulsar_log::Level::Error) == 1);
    assert(ring.count(pulsar_log::Level::Debug) == 1);

    // Only the two Warn/Error lines are in the ring -- Info/Debug never
    // enter it despite being counted.
    auto lines = ring.last_warn_error_lines(10);
    assert(lines.size() == 2);
    assert(lines[0] == "warn line 1");
    assert(lines[1] == "error line 1");
}

// The ring itself never grows past its configured capacity -- oldest
// entries are evicted first, so a caller always sees the MOST RECENT tail.
void test_diagnostics_ring_bounded_eviction()
{
    pulsar_log::DiagnosticsRing ring(/*capacity=*/2);

    ring.record(pulsar_log::Level::Warn, "warn 1");
    ring.record(pulsar_log::Level::Warn, "warn 2");
    ring.record(pulsar_log::Level::Warn, "warn 3");

    auto lines = ring.last_warn_error_lines(10);
    assert(lines.size() == 2);
    assert(lines[0] == "warn 2");
    assert(lines[1] == "warn 3");
}

// ADR §3.6.1: N is capped server-side no matter what the caller asks for,
// and independently clamped to however many lines the ring actually holds.
void test_diagnostics_ring_n_clamped_to_server_cap_and_ring_size()
{
    pulsar_log::DiagnosticsRing ring(/*capacity=*/pulsar_log::DiagnosticsRing::kServerMaxLines + 50);

    for (int i = 0; i < 5; ++i)
        ring.record(pulsar_log::Level::Error, "line " + std::to_string(i));

    // Asking for more than exist returns only what exists.
    assert(ring.last_warn_error_lines(1000).size() == 5);

    for (int i = 5; i < static_cast<int>(pulsar_log::DiagnosticsRing::kServerMaxLines) + 20; ++i)
        ring.record(pulsar_log::Level::Error, "line " + std::to_string(i));

    // Even though the ring's own capacity is larger, and the caller asks for
    // more still, the response never exceeds the server cap.
    auto lines = ring.last_warn_error_lines(pulsar_log::DiagnosticsRing::kServerMaxLines + 1000);
    assert(lines.size() == pulsar_log::DiagnosticsRing::kServerMaxLines);
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
    test_pattern_layer_covers_adr005_f1_forms();
    test_pattern_layer_leaves_ordinary_text_alone();
    test_pattern_layer_abandons_oversized_line();
    test_registry_layer_bare_embedded_repeated();
    test_registry_rejects_short_dedups_and_caps();
    test_redact_line_abandons_when_pattern_layer_fails();
    test_rotation_stays_under_max_files();
    test_age_purge_fires_under_size_and_count_bounds();
    test_diagnostics_ring_counts_and_ring_content();
    test_diagnostics_ring_bounded_eviction();
    test_diagnostics_ring_n_clamped_to_server_cap_and_ring_size();
    test_acl_restricted_at_creation_and_widened_dir_refused();
    test_unwritable_directory_degrades_with_named_error();

    std::fprintf(stdout, "log-handler-probe-test: all assertions passed\n");
    return EXIT_SUCCESS;
}
