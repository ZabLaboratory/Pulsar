#include "realtime-work-queue.h"

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

namespace {
using PlatformCall = HRESULT(STDAPICALLTYPE *)();
HMODULE module = nullptr;
PlatformCall platform_shutdown = nullptr;
} // namespace
#endif

namespace pulsar_runtime {

std::uint32_t startup_realtime_work_queue()
{
#ifdef _WIN32
    if (module)
        return 0;

    HMODULE candidate = LoadLibraryExW(L"RTWorkQ.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!candidate)
        return static_cast<std::uint32_t>(HRESULT_FROM_WIN32(GetLastError()));

    auto startup = reinterpret_cast<PlatformCall>(GetProcAddress(candidate, "RtwqStartup"));
    auto shutdown = reinterpret_cast<PlatformCall>(GetProcAddress(candidate, "RtwqShutdown"));
    if (!startup || !shutdown) {
        FreeLibrary(candidate);
        return static_cast<std::uint32_t>(HRESULT_FROM_WIN32(ERROR_PROC_NOT_FOUND));
    }

    const HRESULT result = startup();
    if (FAILED(result)) {
        FreeLibrary(candidate);
        return static_cast<std::uint32_t>(result);
    }

    module = candidate;
    platform_shutdown = shutdown;
#endif
    return 0;
}

std::uint32_t shutdown_realtime_work_queue()
{
#ifdef _WIN32
    if (!module)
        return 0;

    const HRESULT result = platform_shutdown();
    FreeLibrary(module);
    module = nullptr;
    platform_shutdown = nullptr;
    return static_cast<std::uint32_t>(result);
#else
    return 0;
#endif
}

} // namespace pulsar_runtime
