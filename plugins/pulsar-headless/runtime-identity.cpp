#ifdef _WIN32
// The runtime bootstrap reads a fixed set of environment variables. Keep the
// standalone MSVC probe warning-free without weakening any runtime checks.
#define _CRT_SECURE_NO_WARNINGS
#endif

#include "runtime-identity.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cerrno>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <memory>
#include <mutex>
#include <random>
#include <sstream>
#include <system_error>
#include <unordered_map>
#include <utility>
#include <vector>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#else
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
#endif

namespace pulsar_runtime {
namespace {

namespace fs = std::filesystem;

std::string env_value(const char *name)
{
    const char *value = std::getenv(name);
    return value ? std::string(value) : std::string();
}

bool is_ascii_alnum(char value)
{
    return (value >= 'A' && value <= 'Z') || (value >= 'a' && value <= 'z') ||
           (value >= '0' && value <= '9');
}

fs::path absolute_path(const std::string &raw, const fs::path &base, std::string &error)
{
    if (raw.empty())
        return {};

    std::error_code ec;
    fs::path path(raw);
    if (path.is_relative())
        path = base / path;
    path = fs::absolute(path, ec);
    if (ec) {
        error = "could not resolve path '" + raw + "': " + ec.message();
        return {};
    }
    return path.lexically_normal();
}

fs::path default_state_root()
{
#ifdef _WIN32
    const std::string local = env_value("LOCALAPPDATA");
    if (!local.empty())
        return fs::path(local) / "Pulsar";
#else
    const std::string state = env_value("XDG_STATE_HOME");
    if (!state.empty())
        return fs::path(state) / "Pulsar";
#endif

    std::error_code ec;
    fs::path temp = fs::temp_directory_path(ec);
    if (ec || temp.empty())
        temp = fs::current_path(ec);
    return temp / "Pulsar";
}

std::string now_millis()
{
    const auto now = std::chrono::time_point_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now());
    return std::to_string(now.time_since_epoch().count());
}

std::string process_id()
{
#ifdef _WIN32
    return std::to_string(static_cast<unsigned long>(GetCurrentProcessId()));
#else
    return std::to_string(static_cast<unsigned long>(getpid()));
#endif
}

std::string metadata_value(const std::string &metadata, const char *key)
{
    const std::string prefix = std::string(key) + "=";
    std::size_t begin = 0;
    while (begin < metadata.size()) {
        const std::size_t end = metadata.find('\n', begin);
        const std::size_t length = end == std::string::npos ? metadata.size() - begin : end - begin;
        if (metadata.compare(begin, prefix.size(), prefix) == 0)
            return metadata.substr(begin + prefix.size(), length - prefix.size());
        if (end == std::string::npos)
            break;
        begin = end + 1;
    }
    return {};
}

std::string read_metadata(const fs::path &path)
{
#ifdef _WIN32
    const std::wstring wide_path = path.wstring();
    HANDLE handle = CreateFileW(wide_path.c_str(), GENERIC_READ,
                                FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                                FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle == INVALID_HANDLE_VALUE)
        return {};

    LARGE_INTEGER size{};
    if (!GetFileSizeEx(handle, &size) || size.QuadPart <= 0 || size.QuadPart > (1 << 20)) {
        CloseHandle(handle);
        return {};
    }

    std::string out(static_cast<std::size_t>(size.QuadPart), '\0');
    DWORD read = 0;
    const bool success = ReadFile(handle, out.data(), static_cast<DWORD>(out.size()), &read,
                                  nullptr) != 0;
    CloseHandle(handle);
    if (!success)
        return {};
    out.resize(read);
    return out;
#else
    std::ifstream input(path, std::ios::binary);
    if (!input)
        return {};
    std::ostringstream out;
    out << input.rdbuf();
    return out.str();
#endif
}

std::string stable_hash(std::string_view value)
{
    // FNV-1a is deliberately used instead of std::hash: authority names and
    // metadata paths must remain identical across processes and toolchains.
    std::uint64_t hash = 1469598103934665603ULL;
    for (const unsigned char byte : value) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }

    std::ostringstream out;
    out << std::hex << std::setfill('0') << std::setw(16) << hash;
    return out.str();
}

std::string normalized_path_string(const fs::path &path)
{
    std::error_code ec;
    fs::path absolute = fs::absolute(path, ec);
    if (ec)
        absolute = path;
    return absolute.lexically_normal().generic_string();
}

#ifdef _WIN32

bool file_id_is_nonzero(const FILE_ID_128 &file_id)
{
    for (const BYTE byte : file_id.Identifier) {
        if (byte != 0)
            return true;
    }
    return false;
}

fs::path final_path_from_handle(HANDLE handle, std::string &error)
{
    // VOLUME_NAME_DOS yields a stable \\?\\ DOS/UNC spelling instead of
    // replaying the caller's case, junction, or 8.3 alias.  The handle remains
    // open for the lease lifetime, so the returned path is only used after the
    // identity check and never re-resolved through the original alias.
    DWORD capacity = 512;
    for (;;) {
        std::vector<wchar_t> buffer(capacity);
        const DWORD length = GetFinalPathNameByHandleW(
            handle, buffer.data(), capacity, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
        if (length == 0) {
            error = "runtime_directory_final_path_failed_win32_" +
                    std::to_string(GetLastError());
            return {};
        }
        if (length < capacity)
            return fs::path(std::wstring(buffer.data(), length));

        // Windows extended paths are bounded by 32,767 characters.  Refuse a
        // provider which reports an unrepresentable path instead of falling
        // back to the mutable lexical spelling.
        if (length >= 32767) {
            error = "runtime_directory_final_path_too_long";
            return {};
        }
        capacity = length + 1;
    }
}

std::string physical_directory_key(const fs::path &path, HANDLE &directory_handle,
                                   fs::path &physical_directory, std::string &error)
{
    directory_handle = nullptr;
    physical_directory.clear();
    // Runtime-directory leases are represented by <runtime-dir>/.runtime.lock;
    // the physical identity belongs to the controlled directory, not to the
    // marker file which may not exist yet.
    const fs::path directory_path = path.parent_path();
    if (directory_path.empty()) {
        error = "runtime_directory_parent_missing";
        return {};
    }
    const std::wstring wide_path = directory_path.wstring();

    // Do not request FILE_FLAG_OPEN_REPARSE_POINT: a junction/symlink is
    // intentionally resolved to its target.  A failure to resolve the
    // reparse point or to pass the DACL check is a hard error, never a silent
    // fallback to the lexical path.
    HANDLE handle = CreateFileW(wide_path.c_str(), FILE_READ_ATTRIBUTES,
                                FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                                FILE_FLAG_BACKUP_SEMANTICS, nullptr);
    if (handle == INVALID_HANDLE_VALUE) {
        const DWORD code = GetLastError();
        error = "runtime_directory_open_failed_win32_" + std::to_string(code);
        return {};
    }

    BY_HANDLE_FILE_INFORMATION basic_info{};
    if (!GetFileInformationByHandle(handle, &basic_info)) {
        const DWORD code = GetLastError();
        CloseHandle(handle);
        error = "runtime_directory_attributes_failed_win32_" + std::to_string(code);
        return {};
    }
    if ((basic_info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) == 0) {
        CloseHandle(handle);
        error = "runtime_directory_not_directory";
        return {};
    }

    FILE_ID_INFO file_id_info{};
    const BOOL extended_ok = GetFileInformationByHandleEx(handle, FileIdInfo, &file_id_info,
                                                          sizeof(file_id_info));
    if (extended_ok && file_id_info.VolumeSerialNumber != 0 &&
        file_id_is_nonzero(file_id_info.FileId)) {
        physical_directory = final_path_from_handle(handle, error);
        if (physical_directory.empty()) {
            CloseHandle(handle);
            return {};
        }
        std::ostringstream identity;
        identity << "volume-" << std::hex << std::setfill('0') << std::setw(16)
                 << static_cast<std::uint64_t>(file_id_info.VolumeSerialNumber) << "-file-";
        for (const BYTE byte : file_id_info.FileId.Identifier)
            identity << std::setw(2) << static_cast<unsigned int>(byte);
        directory_handle = handle;
        return identity.str();
    }

    // Older filesystems and some remote providers do not implement FileIdInfo
    // even though the legacy handle information is stable.  Use that API only
    // when it gives a non-zero volume/file identity; otherwise fail closed.
    const DWORD extended_error = extended_ok ? ERROR_INVALID_DATA : GetLastError();
    const std::uint64_t legacy_file_id =
        (static_cast<std::uint64_t>(basic_info.nFileIndexHigh) << 32) |
        static_cast<std::uint64_t>(basic_info.nFileIndexLow);
    if (basic_info.dwVolumeSerialNumber != 0 && legacy_file_id != 0) {
        physical_directory = final_path_from_handle(handle, error);
        if (physical_directory.empty()) {
            CloseHandle(handle);
            return {};
        }
        std::ostringstream identity;
        identity << "volume-" << std::hex << std::setfill('0') << std::setw(8)
                 << static_cast<std::uint64_t>(basic_info.dwVolumeSerialNumber) << "-file-"
                 << std::setw(16) << legacy_file_id;
        directory_handle = handle;
        return identity.str();
    }

    CloseHandle(handle);
    error = "runtime_directory_identity_unavailable_win32_" +
            std::to_string(extended_error);
    return {};
}

#endif

std::string authority_key(const fs::path &path, std::string_view owner,
                          std::string_view resource_kind, std::string &error,
                          void **directory_handle, fs::path &operational_path)
{
    operational_path.clear();
    if (resource_kind == "runtime-instance")
        return "runtime-instance:" + std::string(owner);
    if (resource_kind == "directshow-legacy-alias")
        return "directshow-legacy-alias";
#ifdef _WIN32
    if (resource_kind == "runtime-directory") {
        HANDLE handle = nullptr;
        const std::string physical = physical_directory_key(path, handle, operational_path, error);
        if (physical.empty())
            return {};
        *directory_handle = handle;
        return "runtime-directory-physical:" + physical;
    }
#else
    (void)directory_handle;
#endif
    if (resource_kind == "runtime-directory") {
        operational_path = path.parent_path();
        return "runtime-directory:" + normalized_path_string(path);
    }
    return "path:" + std::string(resource_kind) + ":" + normalized_path_string(path);
}

#ifdef _WIN32

std::uint32_t current_session_id()
{
    DWORD session_id = 0;
    if (!ProcessIdToSessionId(GetCurrentProcessId(), &session_id))
        return 0;
    return static_cast<std::uint32_t>(session_id);
}

std::string authority_name_for(const std::string &key, std::string_view owner,
                               std::string_view resource_kind)
{
    if (resource_kind == "runtime-instance")
        return "Local\\Pulsar.Runtime." + std::string(owner);
    if (resource_kind == "directshow-legacy-alias")
        return "Local\\Pulsar.DirectShowProgramPreview";
    return "Local\\Pulsar.Authority." + stable_hash(key);
}

std::wstring ascii_wide(std::string_view value)
{
    std::wstring wide;
    wide.reserve(value.size());
    for (const unsigned char byte : value)
        wide.push_back(static_cast<wchar_t>(byte));
    return wide;
}

#endif

fs::path canonical_metadata_path(const std::string &key)
{
    fs::path root = default_state_root() / "authorities";
#ifdef _WIN32
    root /= "session-" + std::to_string(current_session_id());
#endif
    return root / (stable_hash(key) + ".lock");
}

std::string lease_metadata(const std::string &owner, const std::string &kind)
{
    std::string out;
    out += "runtime_instance_id=" + owner + "\n";
    out += "resource_kind=" + kind + "\n";
    out += "pid=" + process_id() + "\n";
    out += "heartbeat_unix_ms=" + now_millis() + "\n";
    return out;
}

#ifdef _WIN32

HANDLE as_handle(void *value)
{
    return reinterpret_cast<HANDLE>(value);
}

struct LocalAuthorityOwner {
    DWORD thread_id = 0;
    std::string runtime_id;
};

std::mutex &local_authority_mutex()
{
    static std::mutex mutex;
    return mutex;
}

std::unordered_map<std::string, LocalAuthorityOwner> &local_authority_owners()
{
    static std::unordered_map<std::string, LocalAuthorityOwner> owners;
    return owners;
}

bool locally_reentrant_authority(const std::string &authority_name,
                                 std::string &holder_runtime_id)
{
    const DWORD thread_id = GetCurrentThreadId();
    std::lock_guard<std::mutex> guard(local_authority_mutex());
    const auto it = local_authority_owners().find(authority_name);
    if (it == local_authority_owners().end() || it->second.thread_id != thread_id)
        return false;
    holder_runtime_id = it->second.runtime_id;
    return true;
}

void remember_local_authority(const std::string &authority_name, std::string_view owner_runtime_id)
{
    std::lock_guard<std::mutex> guard(local_authority_mutex());
    local_authority_owners()[authority_name] =
        LocalAuthorityOwner{GetCurrentThreadId(), std::string(owner_runtime_id)};
}

void forget_local_authority(const std::string &authority_name, DWORD thread_id)
{
    std::lock_guard<std::mutex> guard(local_authority_mutex());
    const auto it = local_authority_owners().find(authority_name);
    if (it != local_authority_owners().end() && it->second.thread_id == thread_id)
        local_authority_owners().erase(it);
}

bool write_windows_file(HANDLE handle, const std::string &body)
{
    LARGE_INTEGER zero{};
    if (!SetFilePointerEx(handle, zero, nullptr, FILE_BEGIN))
        return false;
    if (!SetEndOfFile(handle))
        return false;

    DWORD written = 0;
    if (!body.empty() &&
        (!WriteFile(handle, body.data(), static_cast<DWORD>(body.size()), &written, nullptr) ||
         written != body.size()))
        return false;
    return FlushFileBuffers(handle) != 0;
}

#else

bool write_posix_file(int fd, const std::string &body)
{
    if (ftruncate(fd, 0) != 0)
        return false;
    if (lseek(fd, 0, SEEK_SET) < 0)
        return false;

    const char *data = body.data();
    std::size_t left = body.size();
    while (left > 0) {
        const ssize_t count = ::write(fd, data, left);
        if (count < 0) {
            if (errno == EINTR)
                continue;
            return false;
        }
        data += count;
        left -= static_cast<std::size_t>(count);
    }
    return fsync(fd) == 0;
}

#endif

} // namespace

ExclusiveLease::~ExclusiveLease()
{
    release();
}

ExclusiveLease::ExclusiveLease(ExclusiveLease &&other) noexcept
{
    move_from(std::move(other));
}

ExclusiveLease &ExclusiveLease::operator=(ExclusiveLease &&other) noexcept
{
    if (this != &other) {
        release();
        move_from(std::move(other));
    }
    return *this;
}

void ExclusiveLease::move_from(ExclusiveLease &&other) noexcept
{
    path_ = std::move(other.path_);
    operational_path_ = std::move(other.operational_path_);
    metadata_path_ = std::move(other.metadata_path_);
    owner_runtime_id_ = std::move(other.owner_runtime_id_);
    holder_runtime_id_ = std::move(other.holder_runtime_id_);
    resource_kind_ = std::move(other.resource_kind_);
    authority_name_ = std::move(other.authority_name_);
    reason_ = std::move(other.reason_);
    result_ = other.result_;
    held_ = other.held_;
#ifdef _WIN32
    authority_handle_ = other.authority_handle_;
    metadata_handle_ = other.metadata_handle_;
    directory_handle_ = other.directory_handle_;
    authority_thread_id_ = other.authority_thread_id_;
    other.authority_handle_ = nullptr;
    other.metadata_handle_ = nullptr;
    other.directory_handle_ = nullptr;
    other.authority_thread_id_ = 0;
#else
    fd_ = other.fd_;
    other.fd_ = -1;
#endif
    other.operational_path_.clear();
    other.held_ = false;
    other.result_ = LeaseResult::Released;
}

bool ExclusiveLease::acquire(const fs::path &path, std::string_view owner_runtime_id,
                             std::string_view resource_kind)
{
    release();
    path_ = path;
    operational_path_.clear();
    metadata_path_.clear();
    owner_runtime_id_ = std::string(owner_runtime_id);
    holder_runtime_id_.clear();
    resource_kind_ = std::string(resource_kind);
    authority_name_.clear();
    reason_.clear();

    if (path_.empty()) {
        result_ = LeaseResult::Error;
        reason_ = "invalid_path";
        return false;
    }
    if (!is_valid_instance_id(owner_runtime_id_)) {
        result_ = LeaseResult::Error;
        reason_ = "invalid_owner";
        return false;
    }

    void *directory_handle = nullptr;
    fs::path operational_path;
    std::string key_error;
    const std::string key = authority_key(path_, owner_runtime_id_, resource_kind_, key_error,
                                          &directory_handle, operational_path);
    if (key.empty()) {
        result_ = LeaseResult::Error;
        reason_ = key_error.empty() ? "authority_key_failed" : key_error;
        return false;
    }
#ifdef _WIN32
    directory_handle_ = directory_handle;
#endif
    operational_path_ = std::move(operational_path);
    metadata_path_ = canonical_metadata_path(key);
#ifdef _WIN32
    authority_name_ = authority_name_for(key, owner_runtime_id_, resource_kind_);
#else
    authority_name_ = key;
#endif

    std::error_code directory_error;
    fs::create_directories(metadata_path_.parent_path(), directory_error);
    if (directory_error) {
        result_ = LeaseResult::Error;
        reason_ = "metadata_directory_failed";
        release();
        return false;
    }

#ifdef _WIN32
    // The Local namespace matches the DirectShow mapping names. The default
    // security descriptor inherits the creator token's DACL: all runtime
    // processes under the same principal/session can contend, while a
    // different principal receives an access error rather than a second lease.
    const std::wstring wide_authority = ascii_wide(authority_name_);
    HANDLE authority = CreateMutexW(nullptr, FALSE, wide_authority.c_str());
    if (!authority) {
        result_ = LeaseResult::Error;
        reason_ = GetLastError() == ERROR_ACCESS_DENIED ? "authority_access_denied"
                                                        : "authority_create_failed";
        release();
        return false;
    }

    // A Windows mutex is recursive for its owning thread.  Keep a small
    // process-local guard so two ExclusiveLease objects on the same thread
    // cannot turn that API detail into a second logical holder.  Other
    // threads and processes still contend through the named mutex below.
    std::string local_holder;
    if (locally_reentrant_authority(authority_name_, local_holder)) {
        holder_runtime_id_ = local_holder;
        CloseHandle(authority);
        result_ = LeaseResult::Refused;
        reason_ = "already_held_by_" + holder_runtime_id_;
        release();
        return false;
    }

    const DWORD wait_result = WaitForSingleObject(authority, 0);
    if (wait_result != WAIT_OBJECT_0 && wait_result != WAIT_ABANDONED) {
        const std::string metadata = read_metadata(metadata_path_);
        holder_runtime_id_ = metadata_value(metadata, "runtime_instance_id");
        if (wait_result == WAIT_TIMEOUT) {
            reason_ = holder_runtime_id_.empty() ? "already_held"
                                                 : "already_held_by_" + holder_runtime_id_;
            CloseHandle(authority);
            result_ = LeaseResult::Refused;
        } else {
            CloseHandle(authority);
            result_ = LeaseResult::Error;
            reason_ = "authority_wait_failed";
        }
        release();
        return false;
    }

    const std::wstring wide_metadata_path = metadata_path_.wstring();
    HANDLE metadata = CreateFileW(wide_metadata_path.c_str(), GENERIC_READ | GENERIC_WRITE,
                                   FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_ALWAYS,
                                   FILE_ATTRIBUTE_NORMAL, nullptr);
    if (metadata == INVALID_HANDLE_VALUE) {
        ReleaseMutex(authority);
        CloseHandle(authority);
        result_ = LeaseResult::Error;
        reason_ = "metadata_open_failed";
        release();
        return false;
    }

    authority_handle_ = authority;
    metadata_handle_ = metadata;
    authority_thread_id_ = GetCurrentThreadId();
    held_ = true;
    holder_runtime_id_ = owner_runtime_id_;
    remember_local_authority(authority_name_, owner_runtime_id_);
    if (wait_result == WAIT_ABANDONED)
        reason_ = "abandoned_recovered";
    if (!write_windows_file(metadata, lease_metadata(owner_runtime_id_, resource_kind_))) {
        reason_ = "metadata_write_failed";
        release();
        result_ = LeaseResult::Error;
        return false;
    }
#else
    fd_ = ::open(metadata_path_.c_str(), O_RDWR | O_CREAT, 0600);
    if (fd_ < 0) {
        result_ = LeaseResult::Error;
        reason_ = "open_failed";
        return false;
    }
    if (flock(fd_, LOCK_EX | LOCK_NB) != 0) {
        const std::string metadata = read_metadata(metadata_path_);
        holder_runtime_id_ = metadata_value(metadata, "runtime_instance_id");
        reason_ = holder_runtime_id_.empty() ? "already_held" : "already_held_by_" + holder_runtime_id_;
        ::close(fd_);
        fd_ = -1;
        result_ = LeaseResult::Refused;
        return false;
    }
    held_ = true;
    holder_runtime_id_ = owner_runtime_id_;
    if (!write_posix_file(fd_, lease_metadata(owner_runtime_id_, resource_kind_))) {
        reason_ = "metadata_write_failed";
        release();
        result_ = LeaseResult::Error;
        return false;
    }
#endif

    result_ = LeaseResult::Acquired;
    return true;
}

bool ExclusiveLease::renew()
{
    if (!held_)
        return false;

    const std::string metadata = lease_metadata(owner_runtime_id_, resource_kind_);
#ifdef _WIN32
    const bool success = write_windows_file(as_handle(metadata_handle_), metadata);
#else
    const bool success = write_posix_file(fd_, metadata);
#endif
    if (!success)
        reason_ = "metadata_renew_failed";
    return success;
}

void ExclusiveLease::release()
{
    if (!held_) {
#ifdef _WIN32
        if (metadata_handle_) {
            CloseHandle(as_handle(metadata_handle_));
            metadata_handle_ = nullptr;
        }
        if (directory_handle_) {
            CloseHandle(as_handle(directory_handle_));
            directory_handle_ = nullptr;
        }
        if (authority_handle_) {
            CloseHandle(as_handle(authority_handle_));
            authority_handle_ = nullptr;
        }
        authority_thread_id_ = 0;
#else
        if (fd_ >= 0) {
            ::close(fd_);
            fd_ = -1;
        }
#endif
        operational_path_.clear();
        return;
    }

#ifdef _WIN32
    HANDLE authority = as_handle(authority_handle_);
    HANDLE metadata = as_handle(metadata_handle_);
    // Clearing metadata before releasing the named mutex prevents a future
    // claimant from observing an old owner during the hand-over window. The
    // metadata handle deliberately does not share DELETE: rename/delete is
    // blocked while held, and even if a stale file is replaced after release,
    // the named mutex remains the sole authority.
    write_windows_file(metadata, std::string());
    CloseHandle(metadata);
    metadata_handle_ = nullptr;
    const BOOL released = ReleaseMutex(authority);
    if (released)
        forget_local_authority(authority_name_, authority_thread_id_);
    CloseHandle(authority);
    authority_handle_ = nullptr;
    authority_thread_id_ = 0;
    if (directory_handle_) {
        CloseHandle(as_handle(directory_handle_));
        directory_handle_ = nullptr;
    }
#else
    write_posix_file(fd_, std::string());
    flock(fd_, LOCK_UN);
    ::close(fd_);
    fd_ = -1;
#endif
    operational_path_.clear();
    held_ = false;
    result_ = LeaseResult::Released;
}

bool is_valid_instance_id(std::string_view value)
{
    if (value.empty() || value.size() > 64 || value == "." || value == "..")
        return false;
    if (!is_ascii_alnum(value.front()))
        return false;
    for (const char c : value) {
        if (!(is_ascii_alnum(c) || c == '-' || c == '_' || c == '.'))
            return false;
    }
    return true;
}

std::string generate_instance_id()
{
    std::array<std::uint64_t, 2> random{};
    std::random_device device;
    random[0] = (static_cast<std::uint64_t>(device()) << 32) ^ device();
    random[1] = (static_cast<std::uint64_t>(device()) << 32) ^ device();

    std::ostringstream out;
    out << "p" << std::hex << std::setfill('0') << std::setw(16) << random[0]
        << std::setw(16) << random[1];
    return out.str();
}

bool resolve_identity(RuntimeIdentity &identity, std::string &error)
{
    error.clear();
    const std::string requested_id = env_value("PULSAR_RUNTIME_INSTANCE_ID");
    identity.instance_id = requested_id.empty() ? generate_instance_id() : requested_id;
    if (!is_valid_instance_id(identity.instance_id)) {
        error = "PULSAR_RUNTIME_INSTANCE_ID must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}";
        return false;
    }

    const fs::path cwd = fs::current_path();
    const std::string requested_root = env_value("PULSAR_RUNTIME_ROOT");
    if (!requested_root.empty()) {
        identity.state_root = absolute_path(requested_root, cwd, error);
        if (identity.state_root.empty())
            return false;
    } else {
        identity.state_root = default_state_root();
    }
    {
        std::error_code ec;
        identity.state_root = fs::absolute(identity.state_root, ec);
        if (ec) {
            error = "could not resolve default runtime root: " + ec.message();
            return false;
        }
        identity.state_root = identity.state_root.lexically_normal();
    }

    const std::string requested_dir = env_value("PULSAR_RUNTIME_DIR");
    if (!requested_dir.empty()) {
        identity.runtime_dir = absolute_path(requested_dir, cwd, error);
        if (identity.runtime_dir.empty())
            return false;
    } else {
        identity.runtime_dir = identity.state_root / "runtimes" / identity.instance_id;
    }

    // Keep the identity lock in the shared state root, not only inside an
    // explicitly supplied runtime directory. Otherwise two callers could
    // accidentally reuse one runtime_instance_id with different cwd paths
    // and still derive the same DirectShow mapping names.
    identity.instance_lease_path =
        identity.state_root / "instances" / identity.instance_id / "instance.lock";
    identity.runtime_dir_lease_path = identity.runtime_dir / ".runtime.lock";

    std::string lease_root = env_value("PULSAR_LEGACY_ALIAS_LEASE_ROOT");
    fs::path lease_path;
    if (!lease_root.empty()) {
        lease_path = absolute_path(lease_root, cwd, error);
        if (lease_path.empty())
            return false;
    } else {
        lease_path = identity.state_root / "leases";
    }
    identity.legacy_alias_lease_path = lease_path / "directshow-program-preview.lock";
    return true;
}

bool set_process_environment(const char *name, const std::string &value)
{
    if (!name || !*name)
        return false;
#ifdef _WIN32
    return _putenv_s(name, value.c_str()) == 0;
#else
    return ::setenv(name, value.c_str(), 1) == 0;
#endif
}

std::uint16_t pick_free_loopback_port()
{
#ifdef _WIN32
    static bool winsock_ready = false;
    if (!winsock_ready) {
        WSADATA data{};
        if (WSAStartup(MAKEWORD(2, 2), &data) != 0)
            return 0;
        winsock_ready = true;
    }

    SOCKET socket_handle = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (socket_handle == INVALID_SOCKET)
        return 0;
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons(0);
    if (bind(socket_handle, reinterpret_cast<const sockaddr *>(&address), sizeof(address)) != 0) {
        closesocket(socket_handle);
        return 0;
    }
    int length = sizeof(address);
    if (getsockname(socket_handle, reinterpret_cast<sockaddr *>(&address), &length) != 0) {
        closesocket(socket_handle);
        return 0;
    }
    const std::uint16_t port = ntohs(address.sin_port);
    closesocket(socket_handle);
    return port;
#else
    const int socket_handle = socket(AF_INET, SOCK_STREAM, 0);
    if (socket_handle < 0)
        return 0;
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons(0);
    if (bind(socket_handle, reinterpret_cast<const sockaddr *>(&address), sizeof(address)) != 0) {
        ::close(socket_handle);
        return 0;
    }
    socklen_t length = sizeof(address);
    if (getsockname(socket_handle, reinterpret_cast<sockaddr *>(&address), &length) != 0) {
        ::close(socket_handle);
        return 0;
    }
    const std::uint16_t port = ntohs(address.sin_port);
    ::close(socket_handle);
    return port;
#endif
}

} // namespace pulsar_runtime
