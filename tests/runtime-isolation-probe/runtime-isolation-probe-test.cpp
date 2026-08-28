// Regression gate for issue #243 / ADR-PULSAR-DUAL-LANE-001.
//
// The test deliberately runs the production ExclusiveLease implementation.
// It proves four independent runtime namespaces can be held concurrently,
// that the historical DirectShow alias has exactly one holder, that a second
// claimant is refused deterministically, and that release/reacquisition and
// same-runtime collision recovery do not rely on stale marker text.

#include "runtime-identity.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cwchar>
#include <filesystem>
#include <fstream>
#include <memory>
#include <random>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif

#define PULSAR_CHECK(expr)                                                                  \
    do {                                                                                    \
        if (!(expr)) {                                                                      \
            std::fprintf(stderr, "CHECK FAILED: %s (%s:%d)\n", #expr, __FILE__, __LINE__); \
            std::exit(EXIT_FAILURE);                                                        \
        }                                                                                    \
    } while (0)

namespace fs = std::filesystem;
using pulsar_runtime::ExclusiveLease;

namespace {

fs::path scratch_root()
{
    std::mt19937_64 rng(std::random_device{}());
    const fs::path root = fs::temp_directory_path() /
                          ("pulsar-runtime-isolation-" + std::to_string(rng()));
    std::error_code ec;
    fs::remove_all(root, ec);
    fs::create_directories(root, ec);
    PULSAR_CHECK(!ec);
    return root;
}

#ifdef _WIN32

bool create_directory_alias(const fs::path &link, const fs::path &target,
                            std::string &kind, DWORD &error)
{
    // Junctions do not require the symbolic-link privilege and are therefore
    // the primary adversarial alias used by this probe.  The shell is invoked
    // with only test-generated temporary paths and a hidden window; failure
    // is reported as a typed SKIP rather than treated as a product fallback.
    wchar_t shell_buffer[32768]{};
    const DWORD shell_length =
        GetEnvironmentVariableW(L"ComSpec", shell_buffer, sizeof(shell_buffer) / sizeof(wchar_t));
    const std::wstring shell = shell_length > 0 && shell_length < (sizeof(shell_buffer) / sizeof(wchar_t))
                                   ? std::wstring(shell_buffer, shell_length)
                                   : L"C:\\Windows\\System32\\cmd.exe";
    std::wstring command = L"\"" + shell + L"\" /d /c mklink /J \"" + link.wstring() +
                           L"\" \"" + target.wstring() + L"\" >nul 2>&1";
    std::vector<wchar_t> mutable_command(command.begin(), command.end());
    mutable_command.push_back(L'\0');

    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    if (CreateProcessW(shell.c_str(), mutable_command.data(), nullptr, nullptr, FALSE,
                       CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process)) {
        WaitForSingleObject(process.hProcess, INFINITE);
        DWORD exit_code = ERROR_GEN_FAILURE;
        GetExitCodeProcess(process.hProcess, &exit_code);
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
        if (exit_code == 0) {
            kind = "junction";
            error = ERROR_SUCCESS;
            return true;
        }
        error = exit_code;
    } else {
        error = GetLastError();
    }

    DWORD symlink_flags = SYMBOLIC_LINK_FLAG_DIRECTORY;
#ifdef SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE
    symlink_flags |= SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE;
#endif
    if (CreateSymbolicLinkW(link.wstring().c_str(), target.wstring().c_str(), symlink_flags)) {
        kind = "symlink";
        error = ERROR_SUCCESS;
        return true;
    }
    error = GetLastError();
    return false;
}

std::wstring short_path_for(const fs::path &path)
{
    const std::wstring wide_path = path.wstring();
    DWORD capacity = MAX_PATH;
    for (int attempt = 0; attempt < 4; ++attempt) {
        std::vector<wchar_t> buffer(capacity);
        const DWORD length = GetShortPathNameW(wide_path.c_str(), buffer.data(), capacity);
        if (length == 0)
            return {};
        if (length < capacity)
            return std::wstring(buffer.data(), length);
        capacity = length + 1;
    }
    return {};
}

#endif

void test_four_namespaces_and_same_runtime_collision(const fs::path &root)
{
    std::vector<std::unique_ptr<ExclusiveLease>> locks;
    std::vector<std::unique_ptr<ExclusiveLease>> directory_locks;
    std::vector<fs::path> resource_paths;

    for (int i = 0; i < 4; ++i) {
        const std::string id = "probe-runtime-" + std::to_string(i);
        const fs::path dir = root / id;
        const fs::path lock_path = dir / "instance.lock";
        std::error_code ec;
        fs::create_directories(dir, ec);
        PULSAR_CHECK(!ec);

        auto lock = std::make_unique<ExclusiveLease>();
        PULSAR_CHECK(lock->acquire(lock_path, id, "runtime-instance"));
        PULSAR_CHECK(lock->held());
        locks.push_back(std::move(lock));

        auto directory_lock = std::make_unique<ExclusiveLease>();
        PULSAR_CHECK(directory_lock->acquire(dir / ".runtime.lock", id,
                                             "runtime-directory"));
        PULSAR_CHECK(directory_lock->held());
        directory_locks.push_back(std::move(directory_lock));
        resource_paths.push_back(dir / "obs-websocket" / "config.json");
        resource_paths.push_back(dir / "logs" / "pulsar.log");
        resource_paths.push_back(dir / "recordings");
    }

    // Every named path is rooted under a different runtime directory.  This
    // is the inventory that the real bundle creates for four instances.
    for (std::size_t i = 0; i < resource_paths.size(); ++i)
        for (std::size_t j = i + 1; j < resource_paths.size(); ++j)
            PULSAR_CHECK(resource_paths[i] != resource_paths[j]);

    for (std::size_t i = 0; i < locks.size(); ++i)
        for (std::size_t j = i + 1; j < locks.size(); ++j)
            PULSAR_CHECK(locks[i]->authority_name() != locks[j]->authority_name());
    for (std::size_t i = 0; i < directory_locks.size(); ++i)
        for (std::size_t j = i + 1; j < directory_locks.size(); ++j)
            PULSAR_CHECK(directory_locks[i]->authority_name() != directory_locks[j]->authority_name());

    // A second process using the same instance directory cannot silently
    // share its config/log/recording namespace.
    ExclusiveLease collision;
    PULSAR_CHECK(!collision.acquire(root / "probe-runtime-0" / "instance.lock",
                                    "probe-runtime-0", "runtime-instance"));
    PULSAR_CHECK(collision.result() == pulsar_runtime::LeaseResult::Refused);
    PULSAR_CHECK(collision.reason().find("already_held") == 0);
    PULSAR_CHECK(collision.holder_runtime_id() == "probe-runtime-0");
    PULSAR_CHECK(collision.authority_name() == locks.front()->authority_name());

    std::fprintf(stdout,
                 "runtime-inventory: instances=4 unique_resources=%zu same_instance=refused\n",
                 resource_paths.size());

    for (auto &lock : locks)
        lock->release();
    for (auto &lock : directory_locks)
        lock->release();

    // Recovery after a clean release is explicit and deterministic.
    ExclusiveLease recovered;
    PULSAR_CHECK(recovered.acquire(root / "probe-runtime-0" / "instance.lock",
                                   "probe-runtime-0", "runtime-instance"));
    PULSAR_CHECK(recovered.renew());
    recovered.release();
    PULSAR_CHECK(!recovered.held());
    std::fprintf(stdout, "runtime-recovery: released=1 reacquired=1 renewed=1\n");
}

void test_shared_explicit_directory_collision(const fs::path &root)
{
    const fs::path shared_dir = root / "shared-explicit-runtime";
    std::error_code ec;
    fs::create_directories(shared_dir, ec);
    PULSAR_CHECK(!ec);

    ExclusiveLease first;
    PULSAR_CHECK(first.acquire(shared_dir / ".runtime.lock", "explicit-runtime-a",
                               "runtime-directory"));

    ExclusiveLease second;
    PULSAR_CHECK(!second.acquire(shared_dir / ".runtime.lock", "explicit-runtime-b",
                                 "runtime-directory"));
    PULSAR_CHECK(second.result() == pulsar_runtime::LeaseResult::Refused);
    PULSAR_CHECK(second.reason().find("already_held") == 0);
    PULSAR_CHECK(second.holder_runtime_id() == "explicit-runtime-a");
    std::fprintf(stdout,
                 "runtime-directory-collision: holder=explicit-runtime-a claimant=explicit-runtime-b refusal=%s\n",
                 second.reason().c_str());

    first.release();
    PULSAR_CHECK(second.acquire(shared_dir / ".runtime.lock", "explicit-runtime-b",
                                "runtime-directory"));
    second.release();
    std::fprintf(stdout, "runtime-directory-recovery: release=1 reacquire=1\n");
}

void test_cross_root_authorities(const fs::path &root)
{
    const fs::path root_a = root / "cross-root-a";
    const fs::path root_b = root / "cross-root-b";
    std::error_code ec;
    fs::create_directories(root_a, ec);
    PULSAR_CHECK(!ec);
    fs::create_directories(root_b, ec);
    PULSAR_CHECK(!ec);

    // The runtime ID is the namespace identity, not a property of the
    // caller-selected state root.  Two roots must therefore contend for the
    // same canonical authority.
    ExclusiveLease first;
    PULSAR_CHECK(first.acquire(root_a / "instance.lock", "cross-root-runtime",
                               "runtime-instance"));
    ExclusiveLease second;
    PULSAR_CHECK(!second.acquire(root_b / "instance.lock", "cross-root-runtime",
                                 "runtime-instance"));
    PULSAR_CHECK(second.result() == pulsar_runtime::LeaseResult::Refused);
    PULSAR_CHECK(second.reason().find("already_held") == 0);
    PULSAR_CHECK(second.holder_runtime_id() == "cross-root-runtime");
    PULSAR_CHECK(second.authority_name() == first.authority_name());
    PULSAR_CHECK(second.metadata_path() == first.metadata_path());

#ifdef _WIN32
    PULSAR_CHECK(first.authority_name() == "Local\\Pulsar.Runtime.cross-root-runtime");
    PULSAR_CHECK(first.metadata_path() != root_a / "instance.lock");
    PULSAR_CHECK(first.metadata_path() != root_b / "instance.lock");
#endif

    first.release();
    PULSAR_CHECK(second.acquire(root_b / "instance.lock", "cross-root-runtime",
                                "runtime-instance"));
    second.release();
    std::fprintf(stdout,
                 "cross-root-runtime: same_id_refused=1 authority=%s metadata_shared=1 reacquire=1\n",
                 first.authority_name().c_str());
}

#ifdef _WIN32
void test_runtime_directory_physical_identity(const fs::path &root)
{
    ExclusiveLease missing;
    PULSAR_CHECK(!missing.acquire(root / "missing-runtime" / ".runtime.lock",
                                 "missing-runtime", "runtime-directory"));
    PULSAR_CHECK(missing.result() == pulsar_runtime::LeaseResult::Error);
    PULSAR_CHECK(missing.reason().find("runtime_directory_") == 0);
    std::fprintf(stdout, "runtime-directory-errors: missing_rejected=1 reason=%s\n",
                 missing.reason().c_str());

    const fs::path physical = root / "physical-runtime-with-long-name";
    const fs::path case_alias = root / "PHYSICAL-RUNTIME-WITH-LONG-NAME";
    const fs::path distinct = root / "physical-runtime-distinct";
    std::error_code ec;
    fs::create_directories(physical, ec);
    PULSAR_CHECK(!ec);
    fs::create_directories(distinct, ec);
    PULSAR_CHECK(!ec);

    ExclusiveLease first;
    PULSAR_CHECK(first.acquire(physical / ".runtime.lock", "physical-runtime-a",
                               "runtime-directory"));

    // A different physical directory remains independently usable.
    ExclusiveLease distinct_lease;
    PULSAR_CHECK(distinct_lease.acquire(distinct / ".runtime.lock", "physical-runtime-b",
                                        "runtime-directory"));
    PULSAR_CHECK(distinct_lease.authority_name() != first.authority_name());

    // Windows case folding must not create a second cwd authority.
    ExclusiveLease case_claim;
    PULSAR_CHECK(!case_claim.acquire(case_alias / ".runtime.lock", "physical-runtime-c",
                                     "runtime-directory"));
    PULSAR_CHECK(case_claim.result() == pulsar_runtime::LeaseResult::Refused);
    PULSAR_CHECK(case_claim.reason().find("already_held") == 0);
    PULSAR_CHECK(case_claim.authority_name() == first.authority_name());
    PULSAR_CHECK(case_claim.metadata_path() == first.metadata_path());
    std::fprintf(stdout,
                 "runtime-directory-physical: case_refused=1 distinct_allowed=1 authority=%s\n",
                 first.authority_name().c_str());

    distinct_lease.release();
    first.release();
    PULSAR_CHECK(case_claim.acquire(case_alias / ".runtime.lock", "physical-runtime-c",
                                    "runtime-directory"));
    case_claim.release();

    // A junction/symlink is resolved by the production handle open.  If the
    // runner disallows creating either reparse form, retain a typed SKIP; the
    // production path still fails closed on an unresolvable reparse point.
    const fs::path reparse_alias = root / "physical-runtime-reparse-alias";
    std::string alias_kind;
    DWORD alias_error = ERROR_SUCCESS;
    if (!create_directory_alias(reparse_alias, physical, alias_kind, alias_error)) {
        std::fprintf(stdout,
                     "runtime-directory-reparse: SKIP create_failed_win32=%lu\n",
                     static_cast<unsigned long>(alias_error));
    } else {
        ExclusiveLease reparse_first;
        PULSAR_CHECK(reparse_first.acquire(physical / ".runtime.lock", "reparse-runtime-a",
                                           "runtime-directory"));
        ExclusiveLease reparse_claim;
        PULSAR_CHECK(!reparse_claim.acquire(reparse_alias / ".runtime.lock",
                                            "reparse-runtime-b", "runtime-directory"));
        if (reparse_claim.result() == pulsar_runtime::LeaseResult::Refused) {
            PULSAR_CHECK(reparse_claim.reason().find("already_held") == 0);
            PULSAR_CHECK(reparse_claim.authority_name() == reparse_first.authority_name());
            PULSAR_CHECK(reparse_claim.metadata_path() == reparse_first.metadata_path());
        } else {
            PULSAR_CHECK(reparse_claim.result() == pulsar_runtime::LeaseResult::Error);
            PULSAR_CHECK(reparse_claim.reason().find("runtime_directory_") == 0);
        }
        std::fprintf(stdout, "runtime-directory-reparse: kind=%s result=%s reason=%s\n",
                     alias_kind.c_str(),
                     reparse_claim.result() == pulsar_runtime::LeaseResult::Refused ? "refused"
                                                                                     : "rejected",
                     reparse_claim.reason().c_str());
        reparse_first.release();
        PULSAR_CHECK(reparse_claim.acquire(reparse_alias / ".runtime.lock", "reparse-runtime-b",
                                           "runtime-directory"));
        reparse_claim.release();
        PULSAR_CHECK(RemoveDirectoryW(reparse_alias.wstring().c_str()) != 0);
    }

    // The lease must carry the physical path forward to activation. Retarget
    // the caller-visible junction after acquisition and verify that the
    // handle-derived path and a simulated cwd activation remain on A, while a
    // separate claimant through the retargeted alias sees only B.
    const fs::path retarget_alias = root / "physical-runtime-retarget-alias";
    const fs::path retarget_target = root / "physical-runtime-retarget-target";
    fs::create_directories(retarget_target, ec);
    PULSAR_CHECK(!ec);
    std::string retarget_kind;
    DWORD retarget_error = ERROR_SUCCESS;
    if (!create_directory_alias(retarget_alias, physical, retarget_kind, retarget_error)) {
        std::fprintf(stdout,
                     "runtime-directory-retarget: SKIP initial_create_failed_win32=%lu\n",
                     static_cast<unsigned long>(retarget_error));
    } else {
        bool retarget_alias_present = true;
        ExclusiveLease retarget_lease;
        PULSAR_CHECK(retarget_lease.acquire(retarget_alias / ".runtime.lock",
                                            "retarget-runtime-a", "runtime-directory"));
        PULSAR_CHECK(!retarget_lease.operational_path().empty());
        std::error_code equivalent_error;
        PULSAR_CHECK(fs::equivalent(retarget_lease.operational_path(), physical,
                                    equivalent_error));
        PULSAR_CHECK(!equivalent_error);

        PULSAR_CHECK(RemoveDirectoryW(retarget_alias.wstring().c_str()) != 0);
        retarget_alias_present = false;
        std::string retarget_target_kind;
        DWORD retarget_target_error = ERROR_SUCCESS;
        if (!create_directory_alias(retarget_alias, retarget_target, retarget_target_kind,
                                    retarget_target_error)) {
            std::fprintf(stdout,
                         "runtime-directory-retarget: SKIP retarget_create_failed_win32=%lu\n",
                         static_cast<unsigned long>(retarget_target_error));
        } else {
            retarget_alias_present = true;
            equivalent_error.clear();
            PULSAR_CHECK(fs::equivalent(retarget_lease.operational_path(), physical,
                                        equivalent_error));
            PULSAR_CHECK(!equivalent_error);

            const fs::path original_cwd = fs::current_path();
            std::error_code cwd_error;
            fs::current_path(retarget_lease.operational_path(), cwd_error);
            PULSAR_CHECK(!cwd_error);
            equivalent_error.clear();
            PULSAR_CHECK(fs::equivalent(fs::current_path(), physical, equivalent_error));
            PULSAR_CHECK(!equivalent_error);
            fs::current_path(original_cwd, cwd_error);
            PULSAR_CHECK(!cwd_error);

            ExclusiveLease retarget_target_lease;
            PULSAR_CHECK(retarget_target_lease.acquire(
                retarget_alias / ".runtime.lock", "retarget-runtime-b", "runtime-directory"));
            PULSAR_CHECK(fs::equivalent(retarget_target_lease.operational_path(), retarget_target,
                                        equivalent_error));
            PULSAR_CHECK(!equivalent_error);
            std::fprintf(stdout,
                         "runtime-directory-retarget: initial=%s target=%s operational_a=1 "
                         "cwd_a=1 alias_b_allowed=1\n",
                         retarget_kind.c_str(), retarget_target_kind.c_str());
            retarget_target_lease.release();
        }
        retarget_lease.release();
        if (retarget_alias_present)
            PULSAR_CHECK(RemoveDirectoryW(retarget_alias.wstring().c_str()) != 0);
    }
    fs::remove_all(retarget_target, ec);
    PULSAR_CHECK(!ec);

    // 8.3 names are optional per-volume.  When present, they must identify
    // the same physical directory; otherwise record a typed environmental SKIP.
    const fs::path long_name = root / "runtime-directory-with-eight-dot-three-alias";
    fs::create_directories(long_name, ec);
    PULSAR_CHECK(!ec);
    const std::wstring short_name = short_path_for(long_name);
    if (short_name.empty() || _wcsicmp(short_name.c_str(), long_name.wstring().c_str()) == 0) {
        std::fprintf(stdout, "runtime-directory-8dot3: SKIP unavailable\n");
    } else {
        ExclusiveLease long_claim;
        PULSAR_CHECK(long_claim.acquire(long_name / ".runtime.lock", "short-runtime-a",
                                        "runtime-directory"));
        ExclusiveLease short_claim;
        PULSAR_CHECK(!short_claim.acquire(fs::path(short_name) / ".runtime.lock",
                                          "short-runtime-b", "runtime-directory"));
        PULSAR_CHECK(short_claim.result() == pulsar_runtime::LeaseResult::Refused);
        PULSAR_CHECK(short_claim.reason().find("already_held") == 0);
        PULSAR_CHECK(short_claim.authority_name() == long_claim.authority_name());
        PULSAR_CHECK(short_claim.metadata_path() == long_claim.metadata_path());
        std::fprintf(stdout, "runtime-directory-8dot3: refused=1\n");
        long_claim.release();
        PULSAR_CHECK(short_claim.acquire(fs::path(short_name) / ".runtime.lock",
                                         "short-runtime-b", "runtime-directory"));
        short_claim.release();
    }

    std::error_code cleanup_error;
    fs::remove_all(reparse_alias, cleanup_error);
    fs::remove_all(long_name, cleanup_error);
}
#endif

void test_alias_singleton_and_concurrent_claimants(const fs::path &root)
{
    const fs::path alias_path = root / "leases-a" / "directshow-program-preview.lock";
    const fs::path alias_path_b = root / "leases-b" / "directshow-program-preview.lock";
    std::error_code ec;
    fs::create_directories(alias_path.parent_path(), ec);
    PULSAR_CHECK(!ec);
    fs::create_directories(alias_path_b.parent_path(), ec);
    PULSAR_CHECK(!ec);

    // Exercise the actual resolver inputs as well as the lease primitive:
    // PULSAR_LEGACY_ALIAS_LEASE_ROOT may point at either root, but it must
    // remain diagnostic state and cannot partition the fixed alias authority.
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_RUNTIME_INSTANCE_ID",
                                                        "alias-root-a"));
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_RUNTIME_ROOT",
                                                        (root / "runtime-a").string()));
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_LEGACY_ALIAS_LEASE_ROOT",
                                                        alias_path.parent_path().string()));
    pulsar_runtime::RuntimeIdentity identity_a;
    std::string identity_error;
    PULSAR_CHECK(pulsar_runtime::resolve_identity(identity_a, identity_error));
    PULSAR_CHECK(identity_a.legacy_alias_lease_path == alias_path);

    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_RUNTIME_INSTANCE_ID",
                                                        "alias-root-b"));
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_RUNTIME_ROOT",
                                                        (root / "runtime-b").string()));
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_LEGACY_ALIAS_LEASE_ROOT",
                                                        alias_path_b.parent_path().string()));
    pulsar_runtime::RuntimeIdentity identity_b;
    PULSAR_CHECK(pulsar_runtime::resolve_identity(identity_b, identity_error));
    PULSAR_CHECK(identity_b.legacy_alias_lease_path == alias_path_b);
    PULSAR_CHECK(identity_a.legacy_alias_lease_path != identity_b.legacy_alias_lease_path);

    ExclusiveLease first;
    PULSAR_CHECK(first.acquire(identity_a.legacy_alias_lease_path, "probe-runtime-0",
                               "directshow-legacy-alias"));
    PULSAR_CHECK(first.renew());

#ifdef _WIN32
    PULSAR_CHECK(first.authority_name() == "Local\\Pulsar.DirectShowProgramPreview");
#endif

    ExclusiveLease second;
    PULSAR_CHECK(!second.acquire(identity_b.legacy_alias_lease_path, "probe-runtime-1",
                                 "directshow-legacy-alias"));
    PULSAR_CHECK(second.result() == pulsar_runtime::LeaseResult::Refused);
    PULSAR_CHECK(second.reason().find("already_held") == 0);
    PULSAR_CHECK(second.holder_runtime_id() == "probe-runtime-0");
    PULSAR_CHECK(second.authority_name() == first.authority_name());
    PULSAR_CHECK(second.metadata_path() == first.metadata_path());
    std::fprintf(stdout, "legacy-alias: holder=probe-runtime-0 claimant=probe-runtime-1 refusal=%s\n",
                 second.reason().c_str());

#ifdef _WIN32
    // The caller-selected alias path is diagnostic state only.  Replacing
    // that path while the lease is held must not create a second authority.
    {
        std::ofstream diagnostic(alias_path, std::ios::binary | std::ios::trunc);
        PULSAR_CHECK(diagnostic.good());
        diagnostic << "diagnostic-marker\n";
    }
    const fs::path diagnostic_replacement = alias_path.parent_path() /
                                             ("replaced-" +
                                              std::to_string(GetCurrentProcessId()) + ".lock");
    DeleteFileW(diagnostic_replacement.wstring().c_str());
    PULSAR_CHECK(MoveFileExW(alias_path.wstring().c_str(), diagnostic_replacement.wstring().c_str(),
                             MOVEFILE_REPLACE_EXISTING));
    {
        std::ofstream recreated(alias_path, std::ios::binary | std::ios::trunc);
        PULSAR_CHECK(recreated.good());
        recreated << "recreated-diagnostic-marker\n";
    }
    ExclusiveLease replacement_claim;
    PULSAR_CHECK(!replacement_claim.acquire(alias_path, "probe-runtime-2",
                                            "directshow-legacy-alias"));
    PULSAR_CHECK(replacement_claim.result() == pulsar_runtime::LeaseResult::Refused);
    PULSAR_CHECK(replacement_claim.holder_runtime_id() == "probe-runtime-0");
    DeleteFileW(alias_path.wstring().c_str());
    DeleteFileW(diagnostic_replacement.wstring().c_str());

    // The metadata file is observable state, never the authority itself.  It
    // is held without FILE_SHARE_DELETE, so a rename attempt cannot replace
    // the inode under an active lease.  The named mutex remains authoritative
    // even if an external filesystem operation is attempted after release.
    const fs::path replacement =
        first.metadata_path().parent_path() /
        ("replacement-" + std::to_string(GetCurrentProcessId()) + ".lock");
    DeleteFileW(replacement.wstring().c_str());
    const BOOL renamed = MoveFileExW(first.metadata_path().wstring().c_str(),
                                     replacement.wstring().c_str(), MOVEFILE_REPLACE_EXISTING);
    const DWORD rename_error = renamed ? ERROR_SUCCESS : GetLastError();
    if (renamed)
        DeleteFileW(replacement.wstring().c_str());
    PULSAR_CHECK(!renamed);
    const BOOL deleted = DeleteFileW(first.metadata_path().wstring().c_str());
    const DWORD delete_error = deleted ? ERROR_SUCCESS : GetLastError();
    PULSAR_CHECK(!deleted);
    std::fprintf(stdout,
                 "legacy-alias-replacement: diagnostic_recreate_refused=1 "
                 "metadata_rename_blocked=1 metadata_delete_blocked=1 "
                 "rename_error=%lu delete_error=%lu\n",
                 static_cast<unsigned long>(rename_error), static_cast<unsigned long>(delete_error));
#endif

    first.release();
    PULSAR_CHECK(second.acquire(identity_b.legacy_alias_lease_path, "probe-runtime-1",
                                "directshow-legacy-alias"));
    second.release();
    std::fprintf(stdout, "legacy-alias-recovery: release=1 reacquire=1\n");

    // Four independent runtime locks race for one alias lock.  Exactly one
    // obtains the compatibility namespace, and every non-holder remains
    // usable through its already-isolated instance namespace.
    std::atomic<int> ready{0};
    std::atomic<bool> start{false};
    std::atomic<int> alias_holders{0};
    std::atomic<int> alias_refusals{0};
    std::vector<std::thread> workers;
    for (int i = 0; i < 4; ++i) {
        workers.emplace_back([&, i] {
            const std::string id = "concurrent-runtime-" + std::to_string(i);
            const fs::path dir = root / id;
            std::error_code worker_ec;
            fs::create_directories(dir, worker_ec);
            PULSAR_CHECK(!worker_ec);
            ExclusiveLease runtime;
            PULSAR_CHECK(runtime.acquire(dir / "instance.lock", id, "runtime-instance"));

            ready.fetch_add(1, std::memory_order_release);
            while (!start.load(std::memory_order_acquire))
                std::this_thread::yield();

            ExclusiveLease alias;
            if (alias.acquire(alias_path_b, id, "directshow-legacy-alias")) {
                alias_holders.fetch_add(1, std::memory_order_relaxed);
                std::this_thread::sleep_for(std::chrono::milliseconds(30));
                alias.release();
            } else {
                PULSAR_CHECK(alias.result() == pulsar_runtime::LeaseResult::Refused);
                alias_refusals.fetch_add(1, std::memory_order_relaxed);
            }
            runtime.release();
        });
    }
    while (ready.load(std::memory_order_acquire) != 4)
        std::this_thread::yield();
    start.store(true, std::memory_order_release);
    for (auto &worker : workers)
        worker.join();

    PULSAR_CHECK(alias_holders.load() == 1);
    PULSAR_CHECK(alias_refusals.load() == 3);
    std::fprintf(stdout, "legacy-alias-concurrency: claimants=4 holders=%d refusals=%d\n",
                 alias_holders.load(), alias_refusals.load());
}

#ifdef _WIN32
void test_abandoned_authority_recovery(const fs::path &root)
{
    constexpr wchar_t authority_name[] = L"Local\\Pulsar.Runtime.probe-abandoned";
    HANDLE ready = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    PULSAR_CHECK(ready != nullptr);

    std::atomic<HANDLE> raw_handle{nullptr};
    std::atomic<HANDLE> keeper_handle{nullptr};
    std::atomic<bool> worker_ok{false};
    std::thread orphan([&] {
        HANDLE raw = CreateMutexW(nullptr, FALSE, authority_name);
        if (!raw) {
            SetEvent(ready);
            return;
        }
        raw_handle.store(raw, std::memory_order_release);
        const DWORD wait_result = WaitForSingleObject(raw, 0);
        if (wait_result != WAIT_OBJECT_0 && wait_result != WAIT_ABANDONED) {
            SetEvent(ready);
            CloseHandle(raw);
            raw_handle.store(nullptr, std::memory_order_release);
            return;
        }
        worker_ok.store(true, std::memory_order_release);
        SetEvent(ready);

        // The duplicated handle in the parent keeps the named object alive
        // while this owning thread exits without ReleaseMutex.  The next
        // claimant must observe WAIT_ABANDONED and recover the authority.
        while (keeper_handle.load(std::memory_order_acquire) == nullptr)
            std::this_thread::yield();
        CloseHandle(raw);
    });

    PULSAR_CHECK(WaitForSingleObject(ready, INFINITE) == WAIT_OBJECT_0);
    PULSAR_CHECK(worker_ok.load(std::memory_order_acquire));
    HANDLE raw = raw_handle.load(std::memory_order_acquire);
    PULSAR_CHECK(raw != nullptr);
    HANDLE keeper = nullptr;
    PULSAR_CHECK(DuplicateHandle(GetCurrentProcess(), raw, GetCurrentProcess(), &keeper, 0, FALSE,
                                 DUPLICATE_SAME_ACCESS));
    keeper_handle.store(keeper, std::memory_order_release);
    orphan.join();

    ExclusiveLease recovered;
    PULSAR_CHECK(recovered.acquire(root / "abandoned" / "instance.lock", "probe-abandoned",
                                   "runtime-instance"));
    PULSAR_CHECK(recovered.held());
    PULSAR_CHECK(recovered.reason() == "abandoned_recovered");
    PULSAR_CHECK(recovered.renew());
    recovered.release();
    CloseHandle(keeper);
    CloseHandle(ready);
    std::fprintf(stdout, "abandoned-recovery: kernel_mutex=1 recovered=1 renewed=1\n");
}
#endif

void test_identity_validation_and_port()
{
    PULSAR_CHECK(pulsar_runtime::is_valid_instance_id("A-0._ok"));
    PULSAR_CHECK(!pulsar_runtime::is_valid_instance_id(""));
    PULSAR_CHECK(!pulsar_runtime::is_valid_instance_id("../escape"));
    PULSAR_CHECK(!pulsar_runtime::is_valid_instance_id("bad/id"));
    PULSAR_CHECK(!pulsar_runtime::is_valid_instance_id(".hidden"));

    ExclusiveLease invalid;
    PULSAR_CHECK(!invalid.acquire(fs::temp_directory_path() / "pulsar-invalid.lock",
                                  "../invalid", "runtime-instance"));
    PULSAR_CHECK(invalid.result() == pulsar_runtime::LeaseResult::Error);
    PULSAR_CHECK(invalid.reason() == "invalid_owner");

    const std::uint16_t port = pulsar_runtime::pick_free_loopback_port();
    PULSAR_CHECK(port != 0);
    std::fprintf(stdout, "identity-validation: valid=1 invalid=5 free_loopback_port=%u\n",
                 static_cast<unsigned>(port));
}

void test_identity_resolution(const fs::path &root)
{
    const fs::path requested_runtime_dir = root / "custom-runtime";
    const fs::path requested_lease_root = root / "custom-leases";
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_RUNTIME_INSTANCE_ID",
                                                        "resolved-runtime"));
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_RUNTIME_ROOT",
                                                        root.string()));
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_RUNTIME_DIR",
                                                        requested_runtime_dir.string()));
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_LEGACY_ALIAS_LEASE_ROOT",
                                                        requested_lease_root.string()));

    pulsar_runtime::RuntimeIdentity identity;
    std::string error;
    PULSAR_CHECK(pulsar_runtime::resolve_identity(identity, error));
    PULSAR_CHECK(identity.instance_id == "resolved-runtime");
    PULSAR_CHECK(identity.runtime_dir == requested_runtime_dir);
    PULSAR_CHECK(identity.instance_lease_path ==
                 root / "instances" / "resolved-runtime" / "instance.lock");
    PULSAR_CHECK(identity.runtime_dir_lease_path == requested_runtime_dir / ".runtime.lock");
    PULSAR_CHECK(identity.legacy_alias_lease_path ==
                 requested_lease_root / "directshow-program-preview.lock");
    std::fprintf(stdout, "identity-resolution: explicit_dir=1 shared_instance_lease=1 alias_root=1\n");
}

} // namespace

int main()
{
    const fs::path root = scratch_root();
    test_identity_validation_and_port();
    test_identity_resolution(root);
    test_four_namespaces_and_same_runtime_collision(root);
    test_shared_explicit_directory_collision(root);
    test_cross_root_authorities(root);
#ifdef _WIN32
    test_runtime_directory_physical_identity(root);
#endif
    test_alias_singleton_and_concurrent_claimants(root);
#ifdef _WIN32
    test_abandoned_authority_recovery(root);
#endif

    std::error_code ec;
    fs::remove_all(root, ec);
    PULSAR_CHECK(!ec);
    std::fprintf(stdout, "runtime-isolation-probe: PASS cleanup=1\n");
    return EXIT_SUCCESS;
}
