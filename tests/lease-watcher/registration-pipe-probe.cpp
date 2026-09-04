// #254 native regression gate for the producer-owned registration pipe.
// This deliberately exercises the Win32 call shape used by patch 0040:
// FILE_FLAG_FIRST_PIPE_INSTANCE belongs to openMode, while pipeMode remains
// the byte/nowait mode. Both return lanes must create a valid first instance.

#include <windows.h>

#include <cstdio>

namespace {

bool check_lane(const wchar_t *lane)
{
    wchar_t name[128] = {};
    _snwprintf_s(name, _countof(name), _TRUNCATE, L"\\\\.\\pipe\\Pulsar254Registration.%ls.%lu",
                 lane, static_cast<unsigned long>(GetCurrentProcessId()));

    SetLastError(ERROR_SUCCESS);
    HANDLE pipe = CreateNamedPipeW(
        name,
        PIPE_ACCESS_INBOUND | FILE_FLAG_FIRST_PIPE_INSTANCE,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_NOWAIT,
        1, 0, 0, 0, nullptr);
    const DWORD error = GetLastError();
    if (pipe == INVALID_HANDLE_VALUE || error != ERROR_SUCCESS) {
        std::fprintf(stderr, "%ls: CreateNamedPipeW failed: handle=%p GetLastError=%lu\n", lane,
                     static_cast<void *>(pipe), static_cast<unsigned long>(error));
        if (pipe != INVALID_HANDLE_VALUE)
            CloseHandle(pipe);
        return false;
    }

    const bool closed = CloseHandle(pipe) != FALSE;
    if (!closed)
        std::fprintf(stderr, "%ls: CloseHandle failed: GetLastError=%lu\n", lane,
                     static_cast<unsigned long>(GetLastError()));
    return closed;
}

} // namespace

int main()
{
    return check_lane(L"ProgramReturn") && check_lane(L"PreviewReturn") ? 0 : 1;
}
