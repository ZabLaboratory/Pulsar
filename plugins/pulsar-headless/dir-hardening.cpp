#include "dir-hardening.h"

#include <cstdio>
#include <random>
#include <vector>

#if defined(_WIN32)
#include <aclapi.h>
#pragma comment(lib, "advapi32.lib")
#endif

namespace pulsar_dir {

#if defined(_WIN32)

namespace {

// Builds a SECURITY_ATTRIBUTES whose DACL grants GENERIC_ALL to the
// current user only, protected (SE_DACL_PROTECTED) so it is never
// unioned with whatever the parent directory would otherwise inherit
// down. `sd_storage` must outlive every use of the returned `sa`.
bool build_current_user_only_security_attributes(SECURITY_ATTRIBUTES &sa, SECURITY_DESCRIPTOR &sd_storage,
                                                   const std::string &context)
{
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        std::fprintf(stderr, "pulsar-dir-hardening: OpenProcessToken failed (%lu); %s\n", GetLastError(),
                     context.c_str());
        return false;
    }

    DWORD needed = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &needed);
    std::vector<BYTE> buf(needed);
    if (needed == 0 || !GetTokenInformation(token, TokenUser, buf.data(), needed, &needed)) {
        std::fprintf(stderr, "pulsar-dir-hardening: GetTokenInformation failed (%lu); %s\n", GetLastError(),
                     context.c_str());
        CloseHandle(token);
        return false;
    }
    auto *user = reinterpret_cast<TOKEN_USER *>(buf.data());

    EXPLICIT_ACCESSA ea{};
    ea.grfAccessPermissions = GENERIC_ALL;
    ea.grfAccessMode = SET_ACCESS;
    ea.grfInheritance = NO_INHERITANCE;
    ea.Trustee.TrusteeForm = TRUSTEE_IS_SID;
    ea.Trustee.TrusteeType = TRUSTEE_IS_USER;
    ea.Trustee.ptstrName = reinterpret_cast<LPSTR>(user->User.Sid);

    PACL new_dacl = nullptr;
    DWORD rc = SetEntriesInAclA(1, &ea, nullptr, &new_dacl);
    CloseHandle(token);
    if (rc != ERROR_SUCCESS || !new_dacl) {
        std::fprintf(stderr, "pulsar-dir-hardening: SetEntriesInAclA failed (%lu); %s\n", rc, context.c_str());
        return false;
    }

    if (!InitializeSecurityDescriptor(&sd_storage, SECURITY_DESCRIPTOR_REVISION)) {
        std::fprintf(stderr, "pulsar-dir-hardening: InitializeSecurityDescriptor failed (%lu); %s\n",
                     GetLastError(), context.c_str());
        LocalFree(new_dacl);
        return false;
    }
    // bDaclDefaulted=FALSE, and SE_DACL_PROTECTED below, so the DACL is
    // never unioned with an inherited grant from the parent directory.
    if (!SetSecurityDescriptorDacl(&sd_storage, TRUE, new_dacl, FALSE)) {
        std::fprintf(stderr, "pulsar-dir-hardening: SetSecurityDescriptorDacl failed (%lu); %s\n", GetLastError(),
                     context.c_str());
        LocalFree(new_dacl);
        return false;
    }
    if (!SetSecurityDescriptorControl(&sd_storage, SE_DACL_PROTECTED, SE_DACL_PROTECTED)) {
        std::fprintf(stderr, "pulsar-dir-hardening: SetSecurityDescriptorControl failed (%lu); %s\n",
                     GetLastError(), context.c_str());
        LocalFree(new_dacl);
        return false;
    }
    // new_dacl is intentionally never LocalFree'd: it must outlive every
    // use of `sa`/`sd_storage` through CreateFileA/CreateDirectoryA.

    sa.nLength = sizeof(SECURITY_ATTRIBUTES);
    sa.lpSecurityDescriptor = &sd_storage;
    sa.bInheritHandle = FALSE;
    return true;
}

// Applies a protected, current-user-only DACL to an already-open
// handle. Handle-based (SetSecurityInfo), not path-based
// (SetNamedSecurityInfoA), so a reparse point/symlink swapped in
// between resolution and the ACL write cannot steer the DACL onto a
// different object.
bool harden_handle_dacl(HANDLE handle, const std::string &context)
{
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        std::fprintf(stderr, "pulsar-dir-hardening: OpenProcessToken failed (%lu); %s\n", GetLastError(),
                     context.c_str());
        return false;
    }
    DWORD needed = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &needed);
    std::vector<BYTE> buf(needed);
    bool have_user = needed != 0 && GetTokenInformation(token, TokenUser, buf.data(), needed, &needed);
    CloseHandle(token);
    if (!have_user) {
        std::fprintf(stderr, "pulsar-dir-hardening: GetTokenInformation failed; %s\n", context.c_str());
        return false;
    }
    auto *user = reinterpret_cast<TOKEN_USER *>(buf.data());

    EXPLICIT_ACCESSA ea{};
    ea.grfAccessPermissions = GENERIC_ALL;
    ea.grfAccessMode = SET_ACCESS;
    ea.grfInheritance = NO_INHERITANCE;
    ea.Trustee.TrusteeForm = TRUSTEE_IS_SID;
    ea.Trustee.TrusteeType = TRUSTEE_IS_USER;
    ea.Trustee.ptstrName = reinterpret_cast<LPSTR>(user->User.Sid);

    PACL new_dacl = nullptr;
    DWORD rc = SetEntriesInAclA(1, &ea, nullptr, &new_dacl);
    if (rc != ERROR_SUCCESS || !new_dacl) {
        std::fprintf(stderr, "pulsar-dir-hardening: SetEntriesInAclA failed (%lu); %s\n", rc, context.c_str());
        return false;
    }

    // PROTECTED_DACL_SECURITY_INFORMATION: never union with whatever the
    // parent directory would otherwise inherit down.
    rc = SetSecurityInfo(handle, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                          nullptr, nullptr, new_dacl, nullptr);
    LocalFree(new_dacl);
    if (rc != ERROR_SUCCESS) {
        std::fprintf(stderr, "pulsar-dir-hardening: SetSecurityInfo failed (%lu); %s\n", rc, context.c_str());
        return false;
    }
    return true;
}

} // namespace

// Compares the object's OWNER (not its DACL) against TokenOwner -- the
// SID Windows assigns by default as OWNER to any object WE create --
// rather than TokenUser. Under a full admin token the owner Windows
// assigns to something we just created is BUILTIN\Administrators, not
// our individual TokenUser, so comparing against TokenUser would flag
// our own output as hostile (the exact regression a prior revision hit
// on config.json itself, #191/#194). Comparing against TokenOwner is
// correct for both the unprivileged and the elevated case, and --
// unlike a DACL, which anyone holding WRITE_DAC can always rewrite --
// ownership is exactly the property an attacker who pre-created the
// object keeps permanently no matter how many times the DACL is
// re-hardened, which is the residual risk this closes (R10).
bool verify_owned_by_current_token(const std::string &path, const std::string &context)
{
    HANDLE h = CreateFileA(path.c_str(), GENERIC_READ | WRITE_DAC, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                            FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        std::fprintf(stderr, "pulsar-dir-hardening: CreateFileA(%s) for owner verification failed (%lu); %s\n",
                     path.c_str(), GetLastError(), context.c_str());
        return false;
    }

    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        std::fprintf(stderr, "pulsar-dir-hardening: OpenProcessToken failed (%lu); %s\n", GetLastError(),
                     context.c_str());
        CloseHandle(h);
        return false;
    }
    DWORD needed = 0;
    GetTokenInformation(token, TokenOwner, nullptr, 0, &needed);
    std::vector<BYTE> buf(needed);
    bool have_owner = needed != 0 && GetTokenInformation(token, TokenOwner, buf.data(), needed, &needed);
    CloseHandle(token);
    if (!have_owner) {
        std::fprintf(stderr, "pulsar-dir-hardening: GetTokenInformation(TokenOwner) failed (%lu); %s\n",
                     GetLastError(), context.c_str());
        CloseHandle(h);
        return false;
    }
    auto *token_owner = reinterpret_cast<TOKEN_OWNER *>(buf.data());

    PSID file_owner = nullptr;
    PSECURITY_DESCRIPTOR sd = nullptr;
    DWORD rc = GetSecurityInfo(h, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION, &file_owner, nullptr, nullptr,
                                nullptr, &sd);
    CloseHandle(h);
    if (rc != ERROR_SUCCESS || !file_owner) {
        std::fprintf(stderr, "pulsar-dir-hardening: GetSecurityInfo(owner) failed (%lu); %s\n", rc,
                     context.c_str());
        if (sd)
            LocalFree(sd);
        return false;
    }
    bool same = EqualSid(token_owner->Owner, file_owner) == TRUE;
    LocalFree(sd);
    return same;
}

bool create_directory_hardened(const std::string &dir, const std::string &context)
{
    SECURITY_ATTRIBUTES sa{};
    SECURITY_DESCRIPTOR sd_storage{};
    if (!build_current_user_only_security_attributes(sa, sd_storage, context))
        return false;

    if (!CreateDirectoryA(dir.c_str(), &sa)) {
        DWORD err = GetLastError();
        if (err != ERROR_ALREADY_EXISTS) {
            std::fprintf(stderr, "pulsar-dir-hardening: CreateDirectoryA(%s) failed (%lu); %s\n", dir.c_str(), err,
                         context.c_str());
            return false;
        }
        // Falls through: `dir` preexists (ours from a prior boot, or
        // planted by another local account before we ever ran) -- open
        // and verify it below rather than trust it.
    }

    // FILE_FLAG_OPEN_REPARSE_POINT: open the reparse point itself rather
    // than following it. Without this flag, a junction/symlink planted
    // in place of `dir` would be transparently followed to whatever
    // target an attacker chose.
    HANDLE h = CreateFileA(dir.c_str(), GENERIC_READ | WRITE_DAC, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        std::fprintf(stderr, "pulsar-dir-hardening: CreateFileA(%s) for verification failed (%lu); %s\n",
                     dir.c_str(), GetLastError(), context.c_str());
        return false;
    }

    BY_HANDLE_FILE_INFORMATION info{};
    if (!GetFileInformationByHandle(h, &info)) {
        std::fprintf(stderr, "pulsar-dir-hardening: GetFileInformationByHandle(%s) failed (%lu); %s\n",
                     dir.c_str(), GetLastError(), context.c_str());
        CloseHandle(h);
        return false;
    }
    if (info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) {
        std::fprintf(stderr,
                     "pulsar-dir-hardening: %s is a reparse point (junction/symlink); refusing (fail closed, "
                     "#201 N1); %s\n",
                     dir.c_str(), context.c_str());
        CloseHandle(h);
        return false;
    }
    if (!(info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) {
        std::fprintf(stderr, "pulsar-dir-hardening: %s exists but is not a directory; refusing (fail closed); %s\n",
                     dir.c_str(), context.c_str());
        CloseHandle(h);
        return false;
    }

    // Ownership check by handle (3rd named gesture) -- see
    // verify_owned_by_current_token's comment for TokenOwner vs
    // TokenUser. Duplicated inline here (not delegated to that
    // function) because it already has an open, verified-non-reparse
    // handle in hand; re-opening by path would reintroduce exactly the
    // TOCTOU this function exists to close.
    HANDLE token = nullptr;
    bool owned = false;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        DWORD needed = 0;
        GetTokenInformation(token, TokenOwner, nullptr, 0, &needed);
        std::vector<BYTE> buf(needed);
        if (needed != 0 && GetTokenInformation(token, TokenOwner, buf.data(), needed, &needed)) {
            auto *token_owner = reinterpret_cast<TOKEN_OWNER *>(buf.data());
            PSID file_owner = nullptr;
            PSECURITY_DESCRIPTOR sd = nullptr;
            DWORD rc = GetSecurityInfo(h, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION, &file_owner, nullptr,
                                        nullptr, nullptr, &sd);
            if (rc == ERROR_SUCCESS && file_owner) {
                owned = EqualSid(token_owner->Owner, file_owner) == TRUE;
            } else {
                std::fprintf(stderr, "pulsar-dir-hardening: GetSecurityInfo(owner, %s) failed (%lu); %s\n",
                             dir.c_str(), rc, context.c_str());
            }
            if (sd)
                LocalFree(sd);
        } else {
            std::fprintf(stderr, "pulsar-dir-hardening: GetTokenInformation(TokenOwner) failed (%lu); %s\n",
                         GetLastError(), context.c_str());
        }
        CloseHandle(token);
    } else {
        std::fprintf(stderr, "pulsar-dir-hardening: OpenProcessToken failed (%lu); %s\n", GetLastError(),
                     context.c_str());
    }
    if (!owned) {
        std::fprintf(stderr,
                     "pulsar-dir-hardening: %s is owned by another account; refusing to trust or re-harden it "
                     "(fail closed, #201 N1); %s\n",
                     dir.c_str(), context.c_str());
        CloseHandle(h);
        return false;
    }

    // Self-healing: unconditionally re-apply the protected DACL by
    // handle, whether `dir` was just created (already carries it, this
    // is a no-op) or is our own directory surviving from a prior boot
    // (heals any drift).
    bool ok = harden_handle_dacl(h, context);
    CloseHandle(h);
    return ok;
}

bool create_protected_temp_file(const std::string &dir, const std::string &context, std::string &out_path,
                                 HANDLE &out_handle)
{
    SECURITY_ATTRIBUTES sa{};
    SECURITY_DESCRIPTOR sd_storage{};
    if (!build_current_user_only_security_attributes(sa, sd_storage, context))
        return false;

    std::random_device rd;
    std::mt19937_64 rng(((uint64_t)rd() << 32) ^ rd());
    static const char alphabet[] = "abcdefghijklmnopqrstuvwxyz0123456789";
    std::uniform_int_distribution<std::size_t> pick(0, sizeof(alphabet) - 2);

    // CREATE_NEW fails closed on any collision (including a squatted
    // name); a handful of random-name retries absorbs an honest
    // same-process re-run without ever falling back to opening or
    // truncating a file we did not just create ourselves.
    for (int attempt = 0; attempt < 8; ++attempt) {
        std::string suffix;
        for (int i = 0; i < 12; ++i)
            suffix.push_back(alphabet[pick(rng)]);
        std::string candidate = dir + "/config." + suffix + ".tmp";

        HANDLE h = CreateFileA(candidate.c_str(), GENERIC_WRITE, 0, &sa, CREATE_NEW, FILE_ATTRIBUTE_NORMAL,
                                nullptr);
        if (h == INVALID_HANDLE_VALUE) {
            if (GetLastError() == ERROR_FILE_EXISTS)
                continue; // collision/squat on this name -- try another
            std::fprintf(stderr, "pulsar-dir-hardening: CreateFileA(%s) failed (%lu); %s\n", candidate.c_str(),
                         GetLastError(), context.c_str());
            return false;
        }
        // N2: the CREATE_NEW handle is returned open, not closed and
        // reopened with OPEN_EXISTING -- no TOCTOU window between
        // creation and the caller's write.
        out_path = candidate;
        out_handle = h;
        return true;
    }
    std::fprintf(stderr,
                 "pulsar-dir-hardening: could not allocate a fresh temp file under %s after 8 attempts; "
                 "refusing (fail closed); %s\n",
                 dir.c_str(), context.c_str());
    return false;
}

void sweep_orphaned_temp_files(const std::string &dir, const std::string &context)
{
    std::string pattern = dir + "/config.*.tmp";
    WIN32_FIND_DATAA fd{};
    HANDLE find_handle = FindFirstFileA(pattern.c_str(), &fd);
    if (find_handle == INVALID_HANDLE_VALUE)
        return; // nothing to sweep, or dir not readable -- non-fatal

    do {
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
            continue;
        std::string victim = dir + "/" + fd.cFileName;
        if (!DeleteFileA(victim.c_str())) {
            std::fprintf(stderr, "pulsar-dir-hardening: could not sweep orphaned temp file %s (%lu); %s\n",
                         victim.c_str(), GetLastError(), context.c_str());
        }
    } while (FindNextFileA(find_handle, &fd));
    FindClose(find_handle);
}

#endif // _WIN32

} // namespace pulsar_dir
