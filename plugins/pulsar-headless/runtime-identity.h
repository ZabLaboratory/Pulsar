// Runtime identity and process-local resource leases for Pulsar.
//
// This translation unit deliberately has no libobs or Qt dependency.  The
// headless executable uses it before obs_startup(), and the same code is built
// by the standalone runtime-isolation probe. On Windows, ownership is held by
// named Local-session mutexes; runtime-directory leases additionally retain a
// no-delete-share directory handle whose volume/file identity is used in the
// authority key. On POSIX, the fallback is an advisory file lock. In both
// cases the lease is kernel-backed rather than a best-effort marker and is
// recovered by the operating system after an unclean exit.
#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <string_view>

namespace pulsar_runtime {

struct RuntimeIdentity {
    std::string instance_id;
    std::filesystem::path runtime_dir;
    std::filesystem::path state_root;
    std::filesystem::path instance_lease_path;
    std::filesystem::path runtime_dir_lease_path;
    std::filesystem::path legacy_alias_lease_path;
};

enum class LeaseResult {
    Acquired,
    Refused,
    Error,
    Released,
};

// An exclusive, crash-safe lease over one named resource.  The metadata file
// is intentionally retained after a clean release so a subsequent claimant
// can reuse it; on Windows it is observability only and the named mutex is the
// ownership primitive. Stale text is overwritten only after a new lock has
// been acquired.
class ExclusiveLease {
public:
    ExclusiveLease() = default;
    ~ExclusiveLease();

    ExclusiveLease(const ExclusiveLease &) = delete;
    ExclusiveLease &operator=(const ExclusiveLease &) = delete;

    ExclusiveLease(ExclusiveLease &&other) noexcept;
    ExclusiveLease &operator=(ExclusiveLease &&other) noexcept;

    // `path` is retained as the caller-visible metadata path.  On Windows,
    // ownership is held by a canonical Local-session named mutex derived from
    // `resource_kind` and the validated owner/path, so replacing this file
    // cannot create a second authority.  On POSIX the canonical lock file is
    // used as the kernel advisory-lock fallback.
    bool acquire(const std::filesystem::path &path, std::string_view owner_runtime_id,
                 std::string_view resource_kind);

    // Refreshes the owner metadata while retaining the kernel lock.  Failure
    // is reported to the caller; the lock is deliberately not dropped, so a
    // transient metadata write cannot create a silent second owner.
    bool renew();

    void release();

    bool held() const { return held_; }
    LeaseResult result() const { return result_; }
    const std::string &reason() const { return reason_; }
    // The attempted owner remains available through owner_runtime_id().  On
    // a deterministic refusal, holder_runtime_id() is populated from the
    // lock metadata so callers can explain which runtime currently owns the
    // singleton without guessing from a stale marker.
    const std::string &owner_runtime_id() const { return owner_runtime_id_; }
    const std::string &holder_runtime_id() const { return holder_runtime_id_; }
    const std::filesystem::path &path() const { return path_; }
    // For a runtime-directory lease this is the path resolved from the
    // retained directory handle, not the caller's reparse/case/8.3 spelling.
    // Callers must use it for operational cwd-relative resources after the
    // lease is acquired. Other lease kinds expose an empty path.
    const std::filesystem::path &operational_path() const { return operational_path_; }
    const std::filesystem::path &metadata_path() const { return metadata_path_; }
    const std::string &authority_name() const { return authority_name_; }

private:
    void move_from(ExclusiveLease &&other) noexcept;

    std::filesystem::path path_;
    std::filesystem::path operational_path_;
    std::filesystem::path metadata_path_;
    std::string owner_runtime_id_;
    std::string holder_runtime_id_;
    std::string resource_kind_;
    std::string authority_name_;
    std::string reason_;
    LeaseResult result_ = LeaseResult::Released;
    bool held_ = false;

#ifdef _WIN32
    void *authority_handle_ = nullptr; // HANDLE, kept opaque in the public header
    void *metadata_handle_ = nullptr; // HANDLE, kept opaque in the public header
    void *directory_handle_ = nullptr; // HANDLE, retained to protect the physical cwd
    std::uint32_t authority_thread_id_ = 0;
#else
    int fd_ = -1;
#endif
};

bool is_valid_instance_id(std::string_view value);
std::string generate_instance_id();

// Resolves all names which must be stable and unique for one process.  It
// does not create directories and it never changes the process environment;
// callers perform those operations only after validating the returned value.
bool resolve_identity(RuntimeIdentity &identity, std::string &error);

// Portable environment setter used by the headless bootstrap.  It is kept
// here so tests can exercise the same failure-aware path without Qt/libobs.
bool set_process_environment(const char *name, const std::string &value);

// Returns an unused loopback TCP port, or zero when the operating system could
// not provide one.  The caller still owns the usual bind race and must treat
// WebSocket bind failure as authoritative.
std::uint16_t pick_free_loopback_port();

} // namespace pulsar_runtime
