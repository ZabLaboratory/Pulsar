// Regression gate for issue #201 (ADR-005 §5 R10, Amendment 1).
//
// Links the REAL plugins/pulsar-headless/dir-hardening.cpp translation
// unit -- not a mirror -- because that file deliberately never includes
// obs.h / Qt (see dir-hardening.h's header comment), so it is
// host-buildable like tests/log-handler-probe already is for its own
// subject.
//
// A1.RC1 (ADR-005 Amendment 1): exercises the four named gestures --
// atomic creation with a protected SECURITY_ATTRIBUTES DACL,
// FILE_FLAG_OPEN_REPARSE_POINT + reparse-point refusal, owner
// verification, and post-rename re-verification by handle -- plus N2
// (no close/reopen TOCTOU on the temp file handle) and N3 (orphan
// sweep).

#include "dir-hardening.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <random>
#include <string>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <aclapi.h>
#endif

// assert() is a no-op under NDEBUG (RelWithDebInfo CI build): expr is never
// evaluated, so this probe would silently "pass" everything. PULSAR_CHECK
// always evaluates expr and fails hard, independent of NDEBUG (issue #220).
#define PULSAR_CHECK(expr)                                                                  \
    do {                                                                                    \
        if (!(expr)) {                                                                      \
            std::fprintf(stderr, "CHECK FAILED: %s (%s:%d)\n", #expr, __FILE__, __LINE__);  \
            std::exit(EXIT_FAILURE);                                                        \
        }                                                                                    \
    } while (0)

namespace fs = std::filesystem;

#ifdef _WIN32

namespace {

fs::path make_scratch_root(const char *label)
{
    std::mt19937_64 rng(std::random_device{}());
    fs::path p = fs::temp_directory_path() /
                 ("pulsar-dir-hardening-test-" + std::string(label) + "-" + std::to_string(rng()));
    std::error_code ec;
    fs::remove_all(p, ec);
    fs::create_directories(p, ec);
    PULSAR_CHECK(!ec);
    return p;
}

// 1st + 3rd named gestures: fresh creation carries a protected,
// current-user-only DACL applied atomically at CreateDirectoryA time.
void test_fresh_directory_gets_protected_single_ace_dacl()
{
    fs::path root = make_scratch_root("fresh");
    fs::path dir = root / "obs-websocket";
    std::string dirs = dir.string();

    PULSAR_CHECK(pulsar_dir::create_directory_hardened(dirs, "test"));

    HANDLE h = CreateFileA(dirs.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                            FILE_FLAG_BACKUP_SEMANTICS, nullptr);
    PULSAR_CHECK(h != INVALID_HANDLE_VALUE);
    PACL dacl = nullptr;
    PSECURITY_DESCRIPTOR sd = nullptr;
    DWORD rc = GetSecurityInfo(h, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, nullptr, nullptr, &dacl, nullptr, &sd);
    PULSAR_CHECK(rc == ERROR_SUCCESS && dacl != nullptr);
    // Exactly one ACE: the current user only, never unioned with an
    // inherited grant (SE_DACL_PROTECTED at creation time).
    PULSAR_CHECK(dacl->AceCount == 1);
    if (sd)
        LocalFree(sd);
    CloseHandle(h);

    { std::error_code cleanup_ec; fs::remove_all(root, cleanup_ec); }
}

// Re-running against our own directory from a "prior boot"
// (ERROR_ALREADY_EXISTS path) self-heals rather than refusing.
void test_reboot_against_own_directory_succeeds()
{
    fs::path root = make_scratch_root("reboot");
    fs::path dir = root / "obs-websocket";
    std::string dirs = dir.string();

    PULSAR_CHECK(pulsar_dir::create_directory_hardened(dirs, "boot-1"));
    PULSAR_CHECK(pulsar_dir::create_directory_hardened(dirs, "boot-2"));

    { std::error_code cleanup_ec; fs::remove_all(root, cleanup_ec); }
}

// 2nd named gesture: a junction planted where the directory should be
// is opened as the reparse point itself (FILE_FLAG_OPEN_REPARSE_POINT)
// and refused, never transparently followed.
void test_reparse_point_is_refused()
{
    fs::path root = make_scratch_root("reparse");
    fs::path target = root / "junction-target";
    { std::error_code create_ec; fs::create_directories(target, create_ec); }
    fs::path junction = root / "obs-websocket";
    std::string junctions = junction.string();
    std::string targets = target.string();

    std::string cmd = "cmd /c mklink /J \"" + junctions + "\" \"" + targets + "\" >nul 2>&1";
    if (std::system(cmd.c_str()) != 0 || !fs::exists(junction)) {
        std::fprintf(stdout, "dir-hardening-probe-test: mklink /J unavailable, skipping reparse-point case\n");
        { std::error_code cleanup_ec; fs::remove_all(root, cleanup_ec); }
        return;
    }

    PULSAR_CHECK(!pulsar_dir::create_directory_hardened(junctions, "test"));

    { std::error_code cleanup_ec; fs::remove_all(root, cleanup_ec); }
}

// N2: the handle returned by create_protected_temp_file is the SAME
// handle CREATE_NEW opened -- writable directly, no reopen -- and the
// file it names exists with exactly the bytes written through it.
void test_temp_file_handle_is_writable_without_reopen()
{
    fs::path root = make_scratch_root("n2");
    std::string dirs = root.string();

    std::string tmp_path;
    HANDLE h = INVALID_HANDLE_VALUE;
    PULSAR_CHECK(pulsar_dir::create_protected_temp_file(dirs, "test", tmp_path, h));
    PULSAR_CHECK(h != INVALID_HANDLE_VALUE);
    PULSAR_CHECK(!tmp_path.empty());

    const char *payload = "{\"server_password\":\"probe-secret\"}";
    DWORD written = 0;
    BOOL ok = WriteFile(h, payload, static_cast<DWORD>(std::strlen(payload)), &written, nullptr);
    CloseHandle(h);
    PULSAR_CHECK(ok && written == std::strlen(payload));

    std::ifstream in(tmp_path, std::ios::binary);
    std::string content((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    PULSAR_CHECK(content == payload);

    { std::error_code cleanup_ec; fs::remove_all(root, cleanup_ec); }
}

// 4th named gesture: post-rename, the published path is re-verified by
// handle as owned by the current token.
void test_verify_owned_by_current_token_on_our_own_file()
{
    fs::path root = make_scratch_root("verify");
    std::string dirs = root.string();
    fs::path config = root / "config.json";
    std::string configs = config.string();

    std::string tmp_path;
    HANDLE h = INVALID_HANDLE_VALUE;
    PULSAR_CHECK(pulsar_dir::create_protected_temp_file(dirs, "test", tmp_path, h));
    const char *payload = "{}";
    DWORD written = 0;
    WriteFile(h, payload, static_cast<DWORD>(std::strlen(payload)), &written, nullptr);
    CloseHandle(h);
    PULSAR_CHECK(MoveFileExA(tmp_path.c_str(), configs.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH));

    PULSAR_CHECK(pulsar_dir::verify_owned_by_current_token(configs, "test"));

    { std::error_code cleanup_ec; fs::remove_all(root, cleanup_ec); }
}

// N3: config.*.tmp orphans are swept before a new one is allocated; a
// directory with no orphans is left untouched (no crash, no-op).
void test_sweep_removes_orphans_and_is_idempotent()
{
    fs::path root = make_scratch_root("n3");
    std::string dirs = root.string();

    fs::path orphan1 = root / "config.orphanaaaaaa.tmp";
    fs::path orphan2 = root / "config.orphanbbbbbb.tmp";
    { std::ofstream(orphan1) << "leftover-password-1"; }
    { std::ofstream(orphan2) << "leftover-password-2"; }
    PULSAR_CHECK(fs::exists(orphan1) && fs::exists(orphan2));

    pulsar_dir::sweep_orphaned_temp_files(dirs, "test");
    PULSAR_CHECK(!fs::exists(orphan1) && !fs::exists(orphan2));

    // Idempotent: no orphans left, must not throw/crash.
    pulsar_dir::sweep_orphaned_temp_files(dirs, "test-empty");

    { std::error_code cleanup_ec; fs::remove_all(root, cleanup_ec); }
}

} // namespace

#endif // _WIN32

int main()
{
#ifdef _WIN32
    test_fresh_directory_gets_protected_single_ace_dacl();
    test_reboot_against_own_directory_succeeds();
    test_reparse_point_is_refused();
    test_temp_file_handle_is_writable_without_reopen();
    test_verify_owned_by_current_token_on_our_own_file();
    test_sweep_removes_orphans_and_is_idempotent();

    std::fprintf(stdout, "dir-hardening-probe-test: all assertions passed\n");
#else
    std::fprintf(stdout, "dir-hardening-probe-test: Windows-only, nothing to exercise on this platform\n");
#endif
    return EXIT_SUCCESS;
}
