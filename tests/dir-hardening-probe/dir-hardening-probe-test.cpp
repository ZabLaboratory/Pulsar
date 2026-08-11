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
//
// A2.RC2 (ADR-005 Amendment 2, issue #213): the reparse-point case
// (gesture 2) hard-fails -- instead of silently skipping -- when
// `mklink /J` is unavailable, and prints explicit "reparse"/"mklink"
// diagnostics on every run so the CI log always shows whether the case
// was actually exercised. Gestures 3 (different-owner refusal) and 4
// (post-rename re-verification) each gained a real negative case,
// simulated via SetNamedSecurityInfoA reassigning ownership to the
// well-known SYSTEM SID (SeRestorePrivilege permitting) -- no second
// Windows account needed. If SeRestorePrivilege is not held by the
// runner's token, that negative case is documented as NOT RUN with an
// explicit reason printed to stdout, never silently passed.

#include "dir-hardening.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <random>
#include <string>
#include <vector>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <aclapi.h>
#include <sddl.h>
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

// #226: A2.RC2 (#213) made the mklink-unavailable case (gesture 2) a
// hard failure instead of a silent skip, but left gestures 3 and 4's
// SeRestorePrivilege-gated NOT RUN paths (below) merely printing to
// stdout and returning -- main() kept printing "all assertions passed"
// unchanged regardless. A runner that loses SeRestorePrivilege would
// silently stop exercising the different-owner and post-rename-
// substitution refusals while CI stayed green. Every NOT RUN path
// records itself here so main() can fail closed on a non-empty list.
std::vector<std::string> g_not_run;

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

// Best-effort: reassigns the owner of `path` to the well-known SYSTEM
// SID, so a negative test can observe a directory/file owned by a
// principal other than our own token WITHOUT needing a second real
// Windows account. Reassigning ownership to an arbitrary SID (one not
// already carrying the SE_GROUP_OWNER attribute in our own token)
// requires SeRestorePrivilege; on an unprivileged runner this fails,
// and the caller must treat that as a documented "cannot be exercised
// here", not a silent pass. `out_reason` is always set on failure.
bool try_reassign_owner_to_system(const std::string &path, std::string &out_reason)
{
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &token)) {
        out_reason = "OpenProcessToken(TOKEN_ADJUST_PRIVILEGES) failed (" + std::to_string(GetLastError()) + ")";
        return false;
    }

    LUID luid{};
    if (!LookupPrivilegeValueA(nullptr, SE_RESTORE_NAME, &luid)) {
        out_reason = "LookupPrivilegeValueA(SeRestorePrivilege) failed (" + std::to_string(GetLastError()) + ")";
        CloseHandle(token);
        return false;
    }

    TOKEN_PRIVILEGES tp{};
    tp.PrivilegeCount = 1;
    tp.Privileges[0].Luid = luid;
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;
    AdjustTokenPrivileges(token, FALSE, &tp, sizeof(tp), nullptr, nullptr);
    DWORD adjust_err = GetLastError();
    CloseHandle(token);
    if (adjust_err == ERROR_NOT_ALL_ASSIGNED) {
        out_reason = "SeRestorePrivilege is not held by this runner's token (ERROR_NOT_ALL_ASSIGNED) -- "
                     "reassigning ownership to a different principal requires it in the absence of a second "
                     "real Windows account";
        return false;
    }

    BYTE sid_buf[68]; // SECURITY_MAX_SID_SIZE
    DWORD sid_size = sizeof(sid_buf);
    if (!CreateWellKnownSid(WinLocalSystemSid, nullptr, sid_buf, &sid_size)) {
        out_reason = "CreateWellKnownSid(WinLocalSystemSid) failed (" + std::to_string(GetLastError()) + ")";
        return false;
    }

    DWORD rc = SetNamedSecurityInfoA(const_cast<char *>(path.c_str()), SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION,
                                      reinterpret_cast<PSID>(sid_buf), nullptr, nullptr, nullptr);
    if (rc != ERROR_SUCCESS) {
        out_reason = "SetNamedSecurityInfoA(owner=SYSTEM, " + path + ") failed (" + std::to_string(rc) + ")";
        return false;
    }
    return true;
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
//
// A2.RC2 (#213): if `mklink /J` cannot plant the junction on this
// runner, this is now a HARD FAILURE (explicit stderr diagnostic +
// non-zero exit), never a silent skip that still reports "all
// assertions passed" -- the CI log must show, via "reparse"/"mklink"
// tokens, whether this case was actually exercised.
void test_reparse_point_is_refused()
{
    fs::path root = make_scratch_root("reparse");
    fs::path target = root / "junction-target";
    { std::error_code create_ec; fs::create_directories(target, create_ec); }
    fs::path junction = root / "obs-websocket";
    std::string junctions = junction.string();
    std::string targets = target.string();

    std::string cmd = "cmd /c mklink /J \"" + junctions + "\" \"" + targets + "\" >nul 2>&1";
    int rc = std::system(cmd.c_str());
    bool junction_created = (rc == 0) && fs::exists(junction);
    std::fprintf(stdout,
                 "dir-hardening-probe-test: reparse-point gesture: mklink /J rc=%d junction_exists=%d\n", rc,
                 junction_created ? 1 : 0);

    if (!junction_created) {
        std::fprintf(stderr,
                     "dir-hardening-probe-test: FATAL -- mklink /J failed to plant a reparse point on this "
                     "runner (rc=%d); the reparse-point refusal case (gesture 2, #201 N1) cannot be exercised "
                     "here, and per A2.RC2 (#213) this is a hard failure, not a silent skip\n",
                     rc);
        { std::error_code cleanup_ec; fs::remove_all(root, cleanup_ec); }
        std::exit(EXIT_FAILURE);
    }

    PULSAR_CHECK(!pulsar_dir::create_directory_hardened(junctions, "test"));
    std::fprintf(stdout, "dir-hardening-probe-test: reparse-point gesture: junction refusal confirmed\n");

    { std::error_code cleanup_ec; fs::remove_all(root, cleanup_ec); }
}

// 3rd named gesture, NEGATIVE case (#213): a preexisting obs-websocket/
// directory owned by a DIFFERENT principal than our own token is
// refused outright by create_directory_hardened, never silently
// trusted or re-hardened. Simulated by reassigning ownership to the
// well-known SYSTEM SID; if the runner's token lacks SeRestorePrivilege
// this is documented as NOT RUN rather than skipped in silence.
void test_directory_owned_by_other_principal_is_refused()
{
    fs::path root = make_scratch_root("otherowner");
    fs::path dir = root / "obs-websocket";
    std::string dirs = dir.string();
    { std::error_code create_ec; fs::create_directories(dir, create_ec); PULSAR_CHECK(!create_ec); }

    std::string reason;
    if (!try_reassign_owner_to_system(dirs, reason)) {
        g_not_run.push_back("gesture-3:different-owner-refusal");
        std::fprintf(stdout,
                     "dir-hardening-probe-test: NOT RUN -- different-owner refusal case (gesture 3, #201 N1, "
                     "A2.RC2 #213) cannot be exercised on this runner: %s\n",
                     reason.c_str());
        { std::error_code cleanup_ec; fs::remove_all(root, cleanup_ec); }
        return;
    }

    PULSAR_CHECK(!pulsar_dir::create_directory_hardened(dirs, "test"));
    std::fprintf(stdout, "dir-hardening-probe-test: different-owner gesture (3) refusal confirmed\n");

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

// 4th named gesture, NEGATIVE case (#213): a config.json substituted
// for one owned by a different principal between MoveFileExA
// publishing it and the caller reading it back is DETECTED by
// verify_owned_by_current_token -- the exact defense-in-depth window
// that gesture exists to close. Substitution is simulated by
// reassigning ownership of the just-published file to the well-known
// SYSTEM SID; if the runner's token lacks SeRestorePrivilege this is
// documented as NOT RUN rather than skipped in silence.
void test_verify_detects_post_rename_substitution()
{
    fs::path root = make_scratch_root("substitution");
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

    std::string reason;
    if (!try_reassign_owner_to_system(configs, reason)) {
        g_not_run.push_back("gesture-4:post-rename-substitution-detection");
        std::fprintf(stdout,
                     "dir-hardening-probe-test: NOT RUN -- post-rename substitution detection case (gesture 4, "
                     "#201 N1, A2.RC2 #213) cannot be exercised on this runner: %s\n",
                     reason.c_str());
        { std::error_code cleanup_ec; fs::remove_all(root, cleanup_ec); }
        return;
    }

    PULSAR_CHECK(!pulsar_dir::verify_owned_by_current_token(configs, "test"));
    std::fprintf(stdout, "dir-hardening-probe-test: post-rename substitution gesture (4) detection confirmed\n");

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
    test_directory_owned_by_other_principal_is_refused();
    test_temp_file_handle_is_writable_without_reopen();
    test_verify_owned_by_current_token_on_our_own_file();
    test_verify_detects_post_rename_substitution();
    test_sweep_removes_orphans_and_is_idempotent();

    // #226: a NOT RUN gesture (SeRestorePrivilege missing on this
    // runner) must turn this test RED, not leave "all assertions
    // passed" printing unchanged while a security assertion silently
    // never executed -- the same failure mode A2.RC2 (#213) already
    // closed for the mklink-unavailable case, extended to gestures 3/4.
    if (!g_not_run.empty()) {
        for (const auto &name : g_not_run)
            std::fprintf(stderr, "dir-hardening-probe-test: WARN gesture NOT RUN: %s\n", name.c_str());
        std::fprintf(stderr,
                     "dir-hardening-probe-test: FAIL -- %zu named gesture(s) could not be exercised on this "
                     "runner; a security assertion that silently does not run must not report success\n",
                     g_not_run.size());
        return EXIT_FAILURE;
    }

    std::fprintf(stdout, "dir-hardening-probe-test: all assertions passed\n");
#else
    std::fprintf(stdout, "dir-hardening-probe-test: Windows-only, nothing to exercise on this platform\n");
#endif
    return EXIT_SUCCESS;
}
