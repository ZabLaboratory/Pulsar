#pragma once
/* Minimal runtime ABI adapter for NVIDIA Maxine AR 0.8.7.
 * Public API reference: NVIDIA-Maxine/Maxine-AR-SDK/nvar/include/nvAR.h.
 * Included after nvvfx-load.h; reuses its NvCVImage ABI and CUDA interop.
 * No SDK binary, model, path override or second capture producer is shipped.
 */
#include "pulsar-nv-secure-load.h"

typedef struct pulsar_ar_rect { float x, y, width, height; } pulsar_ar_rect;
typedef struct pulsar_ar_boxes {
	pulsar_ar_rect *boxes;
	uint8_t num_boxes;
	uint8_t max_boxes;
} pulsar_ar_boxes;
typedef struct pulsar_ar_point { float x, y; } pulsar_ar_point;
typedef void *pulsar_ar_handle;

struct pulsar_ar_api {
	HMODULE module;
	char dir[MAX_PATH];
	NvCV_Status (*create)(const char *, pulsar_ar_handle *);
	NvCV_Status (*load)(pulsar_ar_handle);
	NvCV_Status (*run)(pulsar_ar_handle);
	NvCV_Status (*destroy)(pulsar_ar_handle);
	NvCV_Status (*set_u32)(pulsar_ar_handle, const char *, unsigned int);
	NvCV_Status (*set_s32)(pulsar_ar_handle, const char *, int);
	NvCV_Status (*set_object)(pulsar_ar_handle, const char *, void *, unsigned long);
	NvCV_Status (*set_string)(pulsar_ar_handle, const char *, const char *);
	NvCV_Status (*set_stream)(pulsar_ar_handle, const char *, CUstream);
	NvCV_Status (*set_f32_array)(pulsar_ar_handle, const char *, float *, int);
};
static struct pulsar_ar_api pulsar_ar;

static void pulsar_ar_unload(void)
{
	if (pulsar_ar.module)
		FreeLibrary(pulsar_ar.module);
	memset(&pulsar_ar, 0, sizeof(pulsar_ar));
}

static bool pulsar_ar_has_model(const char *pattern)
{
	char path[MAX_PATH];
	WIN32_FIND_DATAA item;
	snprintf(path, sizeof(path), "%s\\models\\%s", pulsar_ar.dir, pattern);
	HANDLE search = FindFirstFileA(path, &item);
	if (search == INVALID_HANDLE_VALUE)
		return false;
	bool valid = !(item.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) &&
		(item.nFileSizeHigh || item.nFileSizeLow);
	FindClose(search);
	return valid;
}

static bool pulsar_ar_load(void)
{
	struct pulsar_nv_sdk_probe probe;
	pulsar_nv_probe_ar(&probe);
	if (!probe.usable)
		return false;
	memcpy(pulsar_ar.dir, probe.dir, sizeof(pulsar_ar.dir));
	/* The common confinement helper uses DLL_LOAD_DIR | SYSTEM32 only. */
	pulsar_ar.module = pulsar_nv_load_from_dir(probe.dir, "nvARPose.dll");
	if (!pulsar_ar.module)
		return false;
#define AR_SYMBOL(field, exported) \
	*(FARPROC *)&pulsar_ar.field = GetProcAddress(pulsar_ar.module, exported); \
	if (!pulsar_ar.field) goto fail
	AR_SYMBOL(create, "NvAR_Create");
	AR_SYMBOL(load, "NvAR_Load");
	AR_SYMBOL(run, "NvAR_Run");
	AR_SYMBOL(destroy, "NvAR_Destroy");
	AR_SYMBOL(set_u32, "NvAR_SetU32");
	AR_SYMBOL(set_s32, "NvAR_SetS32");
	AR_SYMBOL(set_object, "NvAR_SetObject");
	AR_SYMBOL(set_string, "NvAR_SetString");
	AR_SYMBOL(set_stream, "NvAR_SetCudaStream");
	AR_SYMBOL(set_f32_array, "NvAR_SetF32Array");
#undef AR_SYMBOL
	return true;
fail:
	pulsar_ar_unload();
	return false;
}
