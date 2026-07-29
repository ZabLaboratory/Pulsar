/*
 * pulsar-nv-secure-load.h -- how Pulsar resolves the NVIDIA Maxine SDK.
 *
 * Pulsar issue #167 / Prism ADR 023 Amendment 3 (§A3.1 override, §A3.4
 * invariant). This header is the SINGLE source of truth for three answers:
 *
 *   1. WHICH directory is allowed to provide an NVIDIA SDK DLL or model,
 *   2. WHETHER that directory currently holds a usable SDK (the capability
 *      probe: presence + version + models, all READ, never assumed),
 *   3. HOW a DLL from it is loaded, so that no directory Pulsar did not
 *      designate -- starting with pulsar.exe's own -- enters the search.
 *
 * It has exactly three consumers, and they must never diverge:
 *
 *   - upstream/plugins/nv-filters/, via patches/0003-*.patch. The module
 *     gate (obs_module_load) and both SDK loaders go through here.
 *   - plugins/pulsar-multi-stream/, which publishes the probe result in the
 *     capability manifest (`capabilities.nv_filters`, ADR 027 block 3).
 *   - tests/nv-probe/, the CTest gate that proves the confinement without a
 *     GPU and without the SDK.
 *
 * WHY THIS EXISTS -- the property being defended
 * ----------------------------------------------
 * Upstream's loaders call LoadLibrary() with a BARE NAME under a
 * SetDllDirectory() of a path read from the inherited environment
 * (NVAFX_SDK_DIR, NV_VIDEO_EFFECTS_PATH). Windows' standard search order
 * puts the APPLICATION DIRECTORY first -- ahead of the SetDllDirectory
 * entry -- so an attacker running as the operator drops NVVideoEffects.dll
 * next to pulsar.exe (per-user install: a writable directory) and wins,
 * whatever directory the embedder pinned. nvcuda.dll was worse still: it
 * was loaded by bare name AFTER SetDllDirectoryA(NULL), i.e. pure standard
 * search. The process holds the Twitch stream key.
 *
 * The fix is not "a better directory": it is that the search must contain
 * NOTHING but the designated directory and System32. Hence, everywhere:
 *
 *     LoadLibraryExW(<absolute path>, NULL,
 *                    LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR |
 *                    LOAD_LIBRARY_SEARCH_SYSTEM32)
 *
 * The absolute path alone is NOT enough, and that is the point of the
 * flags: a DLL loaded by absolute path still resolves its OWN imports
 * (CUDA, TensorRT, cuDNN...) through the default search order, which
 * begins at pulsar.exe's directory. LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR
 * confines that transitive resolution to the directory the DLL came from.
 * LOAD_LIBRARY_SEARCH_DEFAULT_DIRS would NOT do -- it re-admits the
 * application directory by definition.
 *
 * WHY THE PROBE GATES THE MODULE
 * ------------------------------
 * The loaders above cannot run if the module never loads. Pulsar therefore
 * refuses obs_module_load() outright when the probe is negative -- which is
 * the common case, since no SDK ships with Pulsar. That makes the ordinary
 * state structurally inert rather than "dependent on a directory", and it
 * is the one layer the embedder cannot provide (Amendment 3 §A3.4 layer i).
 *
 * Header-only, C99, Windows-only. Depends on Win32 + version.lib and
 * NOTHING else -- no libobs, no CRT allocation -- precisely so the CTest
 * gate can exercise it with no OBS process in sight.
 */

#pragma once

#ifndef _WIN32
#error "pulsar-nv-secure-load.h is Windows-only"
#endif

#include <windows.h>
#include <winver.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#ifdef _MSC_VER
/* Header-only by design: each consumer uses a different subset, so C4505
 * ("unreferenced local function has been removed") is expected here and
 * says nothing about the code. Deliberately not push/pop-ed -- MSVC
 * reports C4505 at end of translation unit, so a pop at the bottom of this
 * header would put the pragma back before it could ever apply. */
#pragma warning(disable : 4505)
#endif

#ifndef LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR
#define LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR 0x00000100
#endif
#ifndef LOAD_LIBRARY_SEARCH_SYSTEM32
#define LOAD_LIBRARY_SEARCH_SYSTEM32 0x00000800
#endif
#ifndef LOAD_LIBRARY_SEARCH_DEFAULT_DIRS
/* Named only so the CTest gate can assert it is NOT what we pass: it
 * includes LOAD_LIBRARY_SEARCH_APPLICATION_DIR by definition. */
#define LOAD_LIBRARY_SEARCH_DEFAULT_DIRS 0x00001000
#endif

/* The exact search set Pulsar admits. Written once, asserted by the CTest
 * gate, and named here so a reviewer can check the two flags in one place
 * rather than at four call sites. */
#define PULSAR_NV_LOAD_FLAGS (LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32)

/* Environment variables the embedder pins (Prism side, Amendment 3 §A3.4
 * layer ii). Pulsar READS them and validates; it never writes them, and it
 * never trusts them without the validation below. */
#define PULSAR_NV_AFX_DIR_ENV "NVAFX_SDK_DIR"
#define PULSAR_NV_VFX_DIR_ENV "NV_VIDEO_EFFECTS_PATH"

#define PULSAR_NV_AFX_DLL "NVAudioEffects.dll"
#define PULSAR_NV_VFX_DLL "NVVideoEffects.dll"
#define PULSAR_NV_CVIMAGE_DLL "NVCVImage.dll"
#define PULSAR_NV_CUDA_DLL "nvcuda.dll"

#define PULSAR_NV_MODEL_SUBDIR "models"

/* Minimum SDK versions, packed major<<24|minor<<16|build<<8|revision, the
 * same packing upstream's get_dll_ver() uses. Kept identical to
 * upstream/plugins/nv-filters/nv_sdk_versions.h; the patched loaders carry
 * a #error that fires if the two ever drift. */
#define PULSAR_NV_MIN_VFX_VERSION (0u << 24 | 7u << 16 | 6u << 8 | 0u)
#define PULSAR_NV_MIN_AFX_VERSION (1u << 24 | 6u << 16 | 1u << 8 | 2u)

/* The three AFX model files. set_nv_model() picks ONE of these by effect
 * method -- dereverb, dereverb_denoiser, or (default) denoiser -- so the
 * probe has to account for all three: declaring the SDK usable on the
 * strength of denoiser_48k.trtpkg alone would leave two effects able to
 * hand the SDK a path that resolves to nothing, or to something else.
 * These files are TensorRT packages the SDK DESERIALISES: an uncontrolled
 * model is an execution surface, not a data surface. */
#define PULSAR_NV_AFX_MODEL_COUNT 3
static const char *const pulsar_nv_afx_models[PULSAR_NV_AFX_MODEL_COUNT] = {
	"denoiser_48k.trtpkg",
	"dereverb_48k.trtpkg",
	"dereverb_denoiser_48k.trtpkg",
};

/* ---- one SDK's probe result -------------------------------------------
 *
 * ABSENCE IS AN ANSWER (ADR 027 §3.3 §1). `version` is 0 and
 * `version_readable` false when the version resource cannot be read -- it
 * is NEVER back-filled with a plausible number, and an unreadable version
 * can never satisfy `version_ok`. */
struct pulsar_nv_sdk_probe {
	bool dir_valid;     /* the designated directory passed validation   */
	char dir[MAX_PATH]; /* canonical form of it; "" when !dir_valid     */
	bool dlls_present;  /* every DLL this SDK needs is in `dir`         */
	bool version_readable;
	unsigned int version;     /* 0 when unreadable                     */
	unsigned int min_version; /* what it is measured against           */
	bool version_ok;
	bool models_present; /* AFX: all 3 .trtpkg present. VFX: model dir  */
	bool usable;         /* every line above holds                     */
};

struct pulsar_nv_probe_result {
	struct pulsar_nv_sdk_probe afx;
	struct pulsar_nv_sdk_probe vfx;
};

/* ---- path validation --------------------------------------------------
 *
 * A designated directory is admitted only if it is an absolute LOCAL
 * drive-letter path that exists and is a directory. Rejected on purpose:
 *
 *   - empty / oversized values (an inherited variable can be anything),
 *   - UNC paths (\\host\share) -- a remote, attacker-reachable provider of
 *     both DLLs and TensorRT models,
 *   - relative paths, which would resolve against the process CWD,
 *   - any "." or ".." segment: the pinned value must designate its target
 *     literally, so that what a reviewer reads is what gets loaded.
 *
 * Forward slashes are normalised to backslashes before the checks (Win32
 * accepts both, so refusing them would only push the difference out of
 * sight). GetFullPathNameA then canonicalises, and the result is
 * re-validated -- canonicalisation must not be able to produce something
 * the first pass would have refused. */
static bool pulsar_nv_dir_shape_ok(const char *p)
{
	size_t i;
	size_t len;

	if (!p)
		return false;
	len = strlen(p);
	if (len < 3 || len >= MAX_PATH)
		return false;

	/* absolute, drive-letter, local */
	if (!((p[0] >= 'A' && p[0] <= 'Z') || (p[0] >= 'a' && p[0] <= 'z')))
		return false;
	if (p[1] != ':' || p[2] != '\\')
		return false;

	/* no traversal or self segments, anywhere */
	for (i = 2; i < len; i++) {
		if (p[i] != '\\')
			continue;
		if (p[i + 1] == '.') {
			char c = p[i + 2];
			if (c == '\\' || c == '\0')
				return false; /* "\.\" or trailing "\." */
			if (c == '.' && (p[i + 3] == '\\' || p[i + 3] == '\0'))
				return false; /* "\..\" or trailing "\.." */
		}
	}
	return true;
}

static bool pulsar_nv_validate_dir(const char *raw, char *out, size_t out_len)
{
	char norm[MAX_PATH];
	char full[MAX_PATH];
	DWORD attrs;
	DWORD n;
	size_t len;
	size_t i;

	if (out && out_len)
		out[0] = '\0';
	if (!raw || !out || out_len < MAX_PATH)
		return false;

	len = strlen(raw);
	if (len == 0 || len >= MAX_PATH)
		return false;

	memcpy(norm, raw, len + 1);
	for (i = 0; i < len; i++) {
		if (norm[i] == '/')
			norm[i] = '\\';
	}
	/* trailing separators carry no meaning and would break the joins */
	while (len > 3 && norm[len - 1] == '\\')
		norm[--len] = '\0';

	if (!pulsar_nv_dir_shape_ok(norm))
		return false;

	n = GetFullPathNameA(norm, MAX_PATH, full, NULL);
	if (n == 0 || n >= MAX_PATH)
		return false;
	len = strlen(full);
	while (len > 3 && full[len - 1] == '\\')
		full[--len] = '\0';
	if (!pulsar_nv_dir_shape_ok(full))
		return false;
	if (strcmp(full, norm) != 0)
		return false; /* the pinned value was not already canonical */

	attrs = GetFileAttributesA(full);
	if (attrs == INVALID_FILE_ATTRIBUTES)
		return false;
	if (!(attrs & FILE_ATTRIBUTE_DIRECTORY))
		return false;

	memcpy(out, full, len + 1);
	return true;
}

/* Joins a validated directory and a leaf into a wide absolute path. The
 * leaf is a compile-time constant at every call site; the separator check
 * is there so that stays true. */
static bool pulsar_nv_join_w(const char *dir, const char *sub, const char *leaf, wchar_t *out, size_t out_len)
{
	char joined[MAX_PATH * 2];
	int written;

	if (!dir || !leaf || !out || out_len == 0)
		return false;
	if (strchr(leaf, '\\') || strchr(leaf, '/'))
		return false;

	if (sub && *sub)
		written = snprintf(joined, sizeof(joined), "%s\\%s\\%s", dir, sub, leaf);
	else
		written = snprintf(joined, sizeof(joined), "%s\\%s", dir, leaf);

	if (written <= 0 || (size_t)written >= MAX_PATH)
		return false;
	if (MultiByteToWideChar(CP_ACP, 0, joined, -1, out, (int)out_len) == 0)
		return false;
	return true;
}

static bool pulsar_nv_file_exists(const char *dir, const char *sub, const char *leaf)
{
	wchar_t path[MAX_PATH];
	DWORD attrs;

	if (!pulsar_nv_join_w(dir, sub, leaf, path, MAX_PATH))
		return false;
	attrs = GetFileAttributesW(path);
	return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

static bool pulsar_nv_subdir_exists(const char *dir, const char *sub)
{
	wchar_t path[MAX_PATH];
	DWORD attrs;

	if (!pulsar_nv_join_w(dir, NULL, sub, path, MAX_PATH))
		return false;
	attrs = GetFileAttributesW(path);
	return attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY);
}

/* ---- version, read off the file, never off a load ---------------------
 *
 * GetFileVersionInfo* parses the resource of the file NAMED BY AN ABSOLUTE
 * PATH; it maps the image as data and executes none of it. That matters:
 * the probe must be able to answer "is this SDK good enough" WITHOUT
 * having loaded anything, otherwise the gate would run after the very
 * thing it gates.
 *
 * Returns true and writes the packed version on success; returns false and
 * leaves *out at 0 when the resource is missing or malformed. A false here
 * means ABSENT -- callers must not treat it as "probably fine". */
static bool pulsar_nv_read_version(const char *dir, const char *leaf, unsigned int *out)
{
	wchar_t path[MAX_PATH];
	DWORD handle = 0;
	DWORD size;
	void *block;
	VS_FIXEDFILEINFO *info = NULL;
	UINT info_len = 0;
	bool ok = false;

	if (out)
		*out = 0;
	if (!out || !pulsar_nv_join_w(dir, NULL, leaf, path, MAX_PATH))
		return false;

	size = GetFileVersionInfoSizeW(path, &handle);
	if (size == 0)
		return false;

	block = HeapAlloc(GetProcessHeap(), 0, size);
	if (!block)
		return false;

	if (GetFileVersionInfoW(path, handle, size, block) &&
	    VerQueryValueW(block, L"\\", (LPVOID *)&info, &info_len) && info && info_len >= sizeof(VS_FIXEDFILEINFO) &&
	    info->dwSignature == 0xFEEF04BDu) {
		*out = ((info->dwFileVersionMS >> 16) & 0xffu) << 24 | ((info->dwFileVersionMS >> 0) & 0xffu) << 16 |
		       ((info->dwFileVersionLS >> 16) & 0xffu) << 8 | ((info->dwFileVersionLS >> 0) & 0xffu);
		ok = true;
	}

	HeapFree(GetProcessHeap(), 0, block);
	return ok;
}

/* ---- the loads --------------------------------------------------------
 *
 * `dir` must already have come out of pulsar_nv_validate_dir(). The
 * existence check before the call is not politeness: it keeps a missing
 * file from turning into a search, which is exactly the failure mode being
 * removed. */
static HMODULE pulsar_nv_load_from_dir(const char *dir, const char *leaf)
{
	wchar_t path[MAX_PATH];

	if (!pulsar_nv_join_w(dir, NULL, leaf, path, MAX_PATH))
		return NULL;
	if (GetFileAttributesW(path) == INVALID_FILE_ATTRIBUTES)
		return NULL;

	return LoadLibraryExW(path, NULL, PULSAR_NV_LOAD_FLAGS);
}

/* nvcuda.dll is installed by the display driver into System32 and is NOT
 * part of the Maxine redistributable, so its designated directory is the
 * system directory -- never the SDK directory, and never (as upstream had
 * it) the standard search order with SetDllDirectory already reset to
 * NULL, which is the single most exposed load of the three. */
static HMODULE pulsar_nv_load_from_system32(const char *leaf)
{
	char sysdir[MAX_PATH];
	UINT n = GetSystemDirectoryA(sysdir, MAX_PATH);

	if (n == 0 || n >= MAX_PATH)
		return NULL;
	return pulsar_nv_load_from_dir(sysdir, leaf);
}

/* ---- the probe --------------------------------------------------------
 *
 * Reads only. Installs nothing, downloads nothing, loads nothing. */
static void pulsar_nv_probe_afx(struct pulsar_nv_sdk_probe *p)
{
	char raw[MAX_PATH];
	DWORD n;
	int i;

	memset(p, 0, sizeof(*p));
	p->min_version = PULSAR_NV_MIN_AFX_VERSION;

	n = GetEnvironmentVariableA(PULSAR_NV_AFX_DIR_ENV, raw, MAX_PATH);
	if (n == 0 || n >= MAX_PATH)
		return;
	if (!pulsar_nv_validate_dir(raw, p->dir, MAX_PATH))
		return;
	p->dir_valid = true;

	p->dlls_present = pulsar_nv_file_exists(p->dir, NULL, PULSAR_NV_AFX_DLL);

	p->models_present = true;
	for (i = 0; i < PULSAR_NV_AFX_MODEL_COUNT; i++) {
		if (!pulsar_nv_file_exists(p->dir, PULSAR_NV_MODEL_SUBDIR, pulsar_nv_afx_models[i]))
			p->models_present = false;
	}

	p->version_readable = pulsar_nv_read_version(p->dir, PULSAR_NV_AFX_DLL, &p->version);
	p->version_ok = p->version_readable && p->version >= p->min_version;

	p->usable = p->dir_valid && p->dlls_present && p->models_present && p->version_ok;
}

static void pulsar_nv_probe_vfx(struct pulsar_nv_sdk_probe *p)
{
	char raw[MAX_PATH];
	char fallback[MAX_PATH];
	const char *candidate = raw;
	DWORD n;

	memset(p, 0, sizeof(*p));
	p->min_version = PULSAR_NV_MIN_VFX_VERSION;

	n = GetEnvironmentVariableA(PULSAR_NV_VFX_DIR_ENV, raw, MAX_PATH);
	if (n == 0 || n >= MAX_PATH) {
		/* Upstream's fallback, kept because dropping it would disable
		 * a default install for no security gain: %ProgramFiles% is
		 * not operator-writable, and the value still goes through the
		 * same validation as a pinned one. */
		char progfiles[MAX_PATH];
		DWORD pn = GetEnvironmentVariableA("ProgramFiles", progfiles, MAX_PATH);
		if (pn == 0 || pn >= MAX_PATH)
			return;
		if (snprintf(fallback, sizeof(fallback), "%s\\NVIDIA Corporation\\NVIDIA Video Effects", progfiles) <=
		    0)
			return;
		candidate = fallback;
	}

	if (!pulsar_nv_validate_dir(candidate, p->dir, MAX_PATH))
		return;
	p->dir_valid = true;

	p->dlls_present = pulsar_nv_file_exists(p->dir, NULL, PULSAR_NV_VFX_DLL) &&
			  pulsar_nv_file_exists(p->dir, NULL, PULSAR_NV_CVIMAGE_DLL);

	/* VFX takes a model DIRECTORY (NVVFX_MODEL_DIRECTORY), not files, so
	 * the checkable fact is that the directory exists inside the
	 * validated root -- which is also what stops the SDK being handed a
	 * path that resolves outside it. */
	p->models_present = pulsar_nv_subdir_exists(p->dir, PULSAR_NV_MODEL_SUBDIR);

	p->version_readable = pulsar_nv_read_version(p->dir, PULSAR_NV_VFX_DLL, &p->version);
	p->version_ok = p->version_readable && p->version >= p->min_version;

	p->usable = p->dir_valid && p->dlls_present && p->models_present && p->version_ok;
}

static void pulsar_nv_probe(struct pulsar_nv_probe_result *out)
{
	if (!out)
		return;
	pulsar_nv_probe_afx(&out->afx);
	pulsar_nv_probe_vfx(&out->vfx);
}

/* The module gate. False here means obs_module_load() refuses, so neither
 * SDK loader ever executes -- the point of Amendment 3 §A3.4 layer (i). */
static bool pulsar_nv_module_should_load(const struct pulsar_nv_probe_result *p)
{
	return p && (p->afx.usable || p->vfx.usable);
}
