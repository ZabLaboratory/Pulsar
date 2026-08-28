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
#include <random>
#include <sstream>
#include <system_error>
#include <utility>

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
    std::ifstream input(path, std::ios::binary);
    if (!input)
        return {};
    std::ostringstream out;
    out << input.rdbuf();
    return out.str();
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

// Windows file-region locks also block reads of the locked bytes. Keep the
// metadata at the beginning of the retained lock file and take ownership of
// a byte beyond it so a contending process can still report the holder.
constexpr DWORD kLockOffset = 4096;

HANDLE as_handle(void *value)
{
    return reinterpret_cast<HANDLE>(value);
}

OVERLAPPED *as_overlapped(void *value)
{
    return reinterpret_cast<OVERLAPPED *>(value);
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
    owner_runtime_id_ = std::move(other.owner_runtime_id_);
    holder_runtime_id_ = std::move(other.holder_runtime_id_);
    resource_kind_ = std::move(other.resource_kind_);
    reason_ = std::move(other.reason_);
    result_ = other.result_;
    held_ = other.held_;
#ifdef _WIN32
    handle_ = other.handle_;
    overlapped_ = other.overlapped_;
    other.handle_ = nullptr;
    other.overlapped_ = nullptr;
#else
    fd_ = other.fd_;
    other.fd_ = -1;
#endif
    other.held_ = false;
    other.result_ = LeaseResult::Released;
}

bool ExclusiveLease::acquire(const fs::path &path, std::string_view owner_runtime_id,
                             std::string_view resource_kind)
{
    release();
    path_ = path;
    owner_runtime_id_ = std::string(owner_runtime_id);
    holder_runtime_id_.clear();
    resource_kind_ = std::string(resource_kind);
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

#ifdef _WIN32
    const std::wstring wide_path = path.wstring();
    HANDLE handle = CreateFileW(wide_path.c_str(), GENERIC_READ | GENERIC_WRITE,
                                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle == INVALID_HANDLE_VALUE) {
        result_ = LeaseResult::Error;
        reason_ = "open_failed";
        return false;
    }

    auto overlap = std::make_unique<OVERLAPPED>();
    *overlap = OVERLAPPED{};
    overlap->Offset = kLockOffset;
    if (!LockFileEx(handle, LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0,
                    overlap.get())) {
        const std::string metadata = read_metadata(path);
        holder_runtime_id_ = metadata_value(metadata, "runtime_instance_id");
        reason_ = holder_runtime_id_.empty() ? "already_held" : "already_held_by_" + holder_runtime_id_;
        CloseHandle(handle);
        result_ = LeaseResult::Refused;
        return false;
    }

    handle_ = handle;
    overlapped_ = overlap.release();
    held_ = true;
    holder_runtime_id_ = owner_runtime_id_;
    if (!write_windows_file(handle, lease_metadata(owner_runtime_id_, resource_kind_))) {
        reason_ = "metadata_write_failed";
        release();
        result_ = LeaseResult::Error;
        return false;
    }
#else
    fd_ = ::open(path.c_str(), O_RDWR | O_CREAT, 0600);
    if (fd_ < 0) {
        result_ = LeaseResult::Error;
        reason_ = "open_failed";
        return false;
    }
    if (flock(fd_, LOCK_EX | LOCK_NB) != 0) {
        const std::string metadata = read_metadata(path);
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
    const bool success = write_windows_file(as_handle(handle_), metadata);
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
        if (overlapped_) {
            delete as_overlapped(overlapped_);
            overlapped_ = nullptr;
        }
#else
        if (fd_ >= 0) {
            ::close(fd_);
            fd_ = -1;
        }
#endif
        return;
    }

#ifdef _WIN32
    HANDLE handle = as_handle(handle_);
    OVERLAPPED *overlap = as_overlapped(overlapped_);
    // Clearing metadata before unlocking prevents a future claimant from
    // observing an old owner during the tiny hand-over window.  The new owner
    // still writes fresh metadata only after it owns the kernel lock.
    write_windows_file(handle, std::string());
    if (overlap)
        UnlockFileEx(handle, 0, 1, 0, overlap);
    CloseHandle(handle);
    delete overlap;
    handle_ = nullptr;
    overlapped_ = nullptr;
#else
    write_posix_file(fd_, std::string());
    flock(fd_, LOCK_UN);
    ::close(fd_);
    fd_ = -1;
#endif
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
