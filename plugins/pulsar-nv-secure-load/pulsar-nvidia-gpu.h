#pragma once
/* Read-only NVML sampling of physical GPUs, not per-effect attribution.
 * ABI: https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html
 * This driver library is loaded from System32 only and is never bundled. */
static void pulsar_nvidia_gpu_usage(obs_data_t *result)
{
    obs_data_set_bool(result, "available", false);
    obs_data_set_string(result, "scope", "whole-device");
    obs_data_set_string(result, "reason", "driver-unavailable");
#ifdef _WIN32
    HMODULE driver = LoadLibraryExW(L"nvml.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!driver) return;
    using Device = void *;
    struct Utilization { unsigned int gpu, memory; };
    struct Memory { unsigned long long total, free, used; };
    auto init = reinterpret_cast<int (*)(void)>(GetProcAddress(driver, "nvmlInit_v2"));
    auto shutdown = reinterpret_cast<int (*)(void)>(GetProcAddress(driver, "nvmlShutdown"));
    auto count_fn = reinterpret_cast<int (*)(unsigned int *)>(GetProcAddress(driver, "nvmlDeviceGetCount_v2"));
    auto handle_fn = reinterpret_cast<int (*)(unsigned int, Device *)>(GetProcAddress(driver, "nvmlDeviceGetHandleByIndex_v2"));
    auto name_fn = reinterpret_cast<int (*)(Device, char *, unsigned int)>(GetProcAddress(driver, "nvmlDeviceGetName"));
    auto uuid_fn = reinterpret_cast<int (*)(Device, char *, unsigned int)>(GetProcAddress(driver, "nvmlDeviceGetUUID"));
    auto utilization_fn = reinterpret_cast<int (*)(Device, Utilization *)>(GetProcAddress(driver, "nvmlDeviceGetUtilizationRates"));
    auto memory_fn = reinterpret_cast<int (*)(Device, Memory *)>(GetProcAddress(driver, "nvmlDeviceGetMemoryInfo"));
    if (!init || !shutdown || !count_fn || !handle_fn || !name_fn || !uuid_fn || !utilization_fn || !memory_fn) {
        obs_data_set_string(result, "reason", "driver-api-unavailable");
        FreeLibrary(driver);
        return;
    }
    const int status = init();
    if (status != 0) {
        obs_data_set_string(result, "reason", "driver-query-failed");
        obs_data_set_int(result, "nativeStatus", status);
        FreeLibrary(driver);
        return;
    }
    unsigned int count = 0;
    const int count_status = count_fn(&count);
    OBSDataArrayAutoRelease devices = obs_data_array_create();
    if (count_status == 0 && count <= 32) {
        for (unsigned int index = 0; index < count; index++) {
            Device device = nullptr;
            if (handle_fn(index, &device) != 0) continue;
            char name[128] = {}, uuid[128] = {};
            if (name_fn(device, name, sizeof(name)) != 0 || uuid_fn(device, uuid, sizeof(uuid)) != 0) continue;
            OBSDataAutoRelease entry = obs_data_create();
            obs_data_set_int(entry, "index", index);
            obs_data_set_string(entry, "name", name);
            obs_data_set_string(entry, "uuid", uuid);
            Utilization utilization = {};
            if (utilization_fn(device, &utilization) == 0 && utilization.gpu <= 100 && utilization.memory <= 100) {
                obs_data_set_int(entry, "gpuPercent", utilization.gpu);
                obs_data_set_int(entry, "memoryPercent", utilization.memory);
            }
            Memory memory = {};
            if (memory_fn(device, &memory) == 0) {
                obs_data_set_int(entry, "usedMemoryBytes", static_cast<long long>(memory.used));
                obs_data_set_int(entry, "totalMemoryBytes", static_cast<long long>(memory.total));
            }
            obs_data_array_push_back(devices, entry);
        }
    }
    const bool available = obs_data_array_count(devices) > 0;
    obs_data_set_array(result, "devices", devices);
    obs_data_set_bool(result, "available", available);
    obs_data_set_string(result, "reason", available ? "available" : "driver-query-failed");
    shutdown(); // Balance only this request's reference-counted initialization.
    FreeLibrary(driver);
#endif
}
