#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <unknwn.h>
#include <rtworkq.h>
#include <mferror.h>

#include "realtime-work-queue.h"

#include <cstdio>
#include <cstdlib>

static void require(bool condition, const char *message)
{
    if (!condition) {
        std::fprintf(stderr, "FAIL: %s\n", message);
        std::exit(1);
    }
}

static void expect_capture_queue(bool running)
{
    DWORD task = 0, queue = 0;
    const HRESULT result = RtwqLockSharedWorkQueue(L"Capture", 0, &task, &queue);
    if (SUCCEEDED(result))
        require(SUCCEEDED(RtwqUnlockWorkQueue(queue)), "release Capture queue");
    if (running)
        require(SUCCEEDED(result), "Capture queue usable while host owns RTWQ");
    else
        require(result == MF_E_SHUTDOWN, "loaded DLL without startup reproduces 0xC00D3E85");
}

int main()
{
    // Linking RTWorkQ loads the DLL, just as another media dependency can do
    // in Pulsar. This must not be mistaken for initializing the platform.
    expect_capture_queue(false);
    std::puts("PASS: reproduced uninitialized Capture queue (0xC00D3E85)");

    for (int cycle = 0; cycle < 3; ++cycle) {
        require(pulsar_runtime::startup_realtime_work_queue() == 0, "host startup");
        require(pulsar_runtime::startup_realtime_work_queue() == 0, "idempotent startup");
        for (int source = 0; source < 12; ++source)
            expect_capture_queue(true);
        require(pulsar_runtime::shutdown_realtime_work_queue() == 0, "host shutdown");
        expect_capture_queue(false);
        require(pulsar_runtime::shutdown_realtime_work_queue() == 0, "idempotent shutdown");
    }
    std::puts("PASS: three host lifecycles and 36 Capture queue recreations");

    // Our matching shutdown must leave a separate dependency's reference live.
    require(SUCCEEDED(RtwqStartup()), "independent owner startup");
    require(pulsar_runtime::startup_realtime_work_queue() == 0, "coexisting host startup");
    require(pulsar_runtime::shutdown_realtime_work_queue() == 0, "release host reference only");
    require(pulsar_runtime::shutdown_realtime_work_queue() == 0, "no duplicate platform shutdown");
    expect_capture_queue(true);
    require(SUCCEEDED(RtwqShutdown()), "independent owner shutdown");
    expect_capture_queue(false);
    std::puts("PASS: independent platform owner preserved; no startup reference leaked");
    return 0;
}
