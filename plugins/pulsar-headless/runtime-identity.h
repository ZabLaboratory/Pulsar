// Runtime identity and process-local resource leases for Pulsar.
//
// This translation unit deliberately has no libobs or Qt dependency.  The
// headless executable uses it before obs_startup(), and the same code is built
// by the standalone runtime-isolation probe. Leases are kernel/file locks,
// not best-effort markers: they are released by the operating system when a
// process exits, including an unclean exit.
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

// An exclusive, crash-safe lease over one named resource.  The lock file is
// intentionally retained after a clean release so a subsequent claimant can
// reuse it; the kernel lock itself is the ownership primitive and stale text
// is overwritten only after a new lock has been acquired.
class ExclusiveLease {
public:
    ExclusiveLease() = default;
    ~ExclusiveLease();

    ExclusiveLease(const ExclusiveLease &) = delete;
    ExclusiveLease &operator=(const ExclusiveLease &) = delete;

    ExclusiveLease(ExclusiveLease &&other) noexcept;
    ExclusiveLease &operator=(ExclusiveLease &&other) noexcept;

    // `path` must name a file below a caller-owned lease directory.  The
    // parent directory is created by the caller so path errors remain
    // observable and are never silently redirected elsewhere.
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

private:
    void move_from(ExclusiveLease &&other) noexcept;

    std::filesystem::path path_;
    std::string owner_runtime_id_;
    std::string holder_runtime_id_;
    std::string resource_kind_;
    std::string reason_;
    LeaseResult result_ = LeaseResult::Released;
    bool held_ = false;

#ifdef _WIN32
    void *handle_ = nullptr; // HANDLE, kept opaque in the public header
    void *overlapped_ = nullptr; // OVERLAPPED, owned by the implementation
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
