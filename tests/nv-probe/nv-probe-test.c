/*
 * nv-probe-test -- the CI gate for issue #167 / Prism ADR 023 Amendment 3.
 *
 * WHAT THIS PROVES, AND WHAT IT DELIBERATELY DOES NOT
 * ===================================================
 *
 * Criterion 3 of #167 ("first level", [CI]): a DLL with the SDK's name,
 * dropped in the APPLICATION DIRECTORY, is not loaded. Provable with no
 * GPU and no SDK, because it is a property of the loader flags, not of
 * NVIDIA code -- so it gates the merge.
 *
 * The application directory here is this executable's own directory. That
 * is not an approximation of "next to pulsar.exe": LOAD_LIBRARY_SEARCH_*
 * is evaluated against the directory of the RUNNING IMAGE, so for this
 * process that directory plays exactly the role pulsar.exe's plays in
 * production. The test therefore writes its own homonyms into its build
 * output directory, and removes them again.
 *
 * Criterion 3 bis ("transitive dependency", [PREUVE MANUELLE]) is NOT
 * claimed here. What is exercised below is the transitive resolution of a
 * FAKE dependency -- enough to prove LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR is
 * in force and does what it is there for, and nothing more. The real
 * criterion asks whether the genuine NVIDIA DLL pulls the genuine CUDA /
 * TensorRT runtime from a confined search, and that requires the DLL to
 * load at all, i.e. a machine with a valid SDK. On a machine without one,
 * that test must report NOT EXERCISED -- never green. This binary
 * therefore prints an explicit NOT EXERCISED line for it, so the state is
 * visible in the CTest log rather than silently absent.
 *
 * Also covered: criterion 2 (presence AND versions, read off the system;
 * an unreadable version declared absent) and criterion 4 / 4 ter (a
 * negative probe means the module does not load; the three .trtpkg model
 * files are part of what makes it negative).
 */

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pulsar-nv-secure-load.h"

static int g_failures = 0;
static int g_checks = 0;

static void check(bool cond, const char *what)
{
	g_checks++;
	if (cond) {
		printf("  ok   %s\n", what);
	} else {
		printf("  FAIL %s\n", what);
		g_failures++;
	}
}

static void section(const char *name)
{
	printf("\n== %s\n", name);
}

/* ---- fixture plumbing ------------------------------------------------- */

static char g_exe_dir[MAX_PATH];
static char g_tmp_root[MAX_PATH];
static char g_sdk_dir[MAX_PATH];  /* complete, valid, recent fake SDK      */
static char g_old_dir[MAX_PATH];  /* same, but the DLL is version 0.0.0.1  */
static char g_junk_dir[MAX_PATH]; /* DLL present, version resource absent  */

static const char *g_good_dll = NULL;
static const char *g_evil_dll = NULL;
static const char *g_good_dep = NULL;
static const char *g_evil_dep = NULL;

static void die(const char *msg)
{
	fprintf(stderr, "nv-probe-test: fixture error: %s (GetLastError=%lu)\n", msg, GetLastError());
	exit(2);
}

static void join(char *out, const char *a, const char *b)
{
	if (snprintf(out, MAX_PATH, "%s\\%s", a, b) >= MAX_PATH)
		die("path too long");
}

static void make_dir(const char *p)
{
	if (!CreateDirectoryA(p, NULL) && GetLastError() != ERROR_ALREADY_EXISTS)
		die(p);
}

static void copy_to(const char *src, const char *dir, const char *leaf)
{
	char dst[MAX_PATH];
	join(dst, dir, leaf);
	if (!CopyFileA(src, dst, FALSE))
		die(dst);
}

static void write_file(const char *dir, const char *leaf, const char *content)
{
	char dst[MAX_PATH];
	HANDLE h;
	DWORD written = 0;

	join(dst, dir, leaf);
	h = CreateFileA(dst, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
	if (h == INVALID_HANDLE_VALUE)
		die(dst);
	if (!WriteFile(h, content, (DWORD)strlen(content), &written, NULL))
		die(dst);
	CloseHandle(h);
}

static void remove_file(const char *dir, const char *leaf)
{
	char dst[MAX_PATH];
	join(dst, dir, leaf);
	DeleteFileA(dst);
}

/* Copies the AFX fixture set into `dir`: the DLL under test plus the three
 * TensorRT model packages, which the probe requires as a set. */
static void seed_afx(const char *dir, const char *dll_src, bool with_models)
{
	char models[MAX_PATH];
	int i;

	copy_to(dll_src, dir, PULSAR_NV_AFX_DLL);
	join(models, dir, PULSAR_NV_MODEL_SUBDIR);
	make_dir(models);
	if (!with_models)
		return;
	for (i = 0; i < PULSAR_NV_AFX_MODEL_COUNT; i++)
		write_file(models, pulsar_nv_afx_models[i], "not a real trtpkg");
}

static void seed_vfx(const char *dir, const char *dll_src)
{
	char models[MAX_PATH];

	copy_to(dll_src, dir, PULSAR_NV_VFX_DLL);
	copy_to(dll_src, dir, PULSAR_NV_CVIMAGE_DLL);
	join(models, dir, PULSAR_NV_MODEL_SUBDIR);
	make_dir(models);
}

static void build_fixtures(void)
{
	char *sep;
	DWORD n;

	n = GetModuleFileNameA(NULL, g_exe_dir, MAX_PATH);
	if (n == 0 || n >= MAX_PATH)
		die("GetModuleFileNameA");
	sep = strrchr(g_exe_dir, '\\');
	if (!sep)
		die("exe path has no directory");
	*sep = '\0';

	n = GetTempPathA(MAX_PATH, g_tmp_root);
	if (n == 0 || n >= MAX_PATH)
		die("GetTempPathA");
	if (n > 0 && g_tmp_root[n - 1] == '\\')
		g_tmp_root[n - 1] = '\0';
	if (snprintf(g_tmp_root, MAX_PATH, "%s\\pulsar-nv-probe-%lu", g_tmp_root, GetCurrentProcessId()) >= MAX_PATH)
		die("temp path too long");
	make_dir(g_tmp_root);

	join(g_sdk_dir, g_tmp_root, "sdk");
	join(g_old_dir, g_tmp_root, "old");
	join(g_junk_dir, g_tmp_root, "junk");
	make_dir(g_sdk_dir);
	make_dir(g_old_dir);
	make_dir(g_junk_dir);

	/* The designated directory: everything present and recent enough. */
	seed_afx(g_sdk_dir, g_good_dll, true);
	seed_vfx(g_sdk_dir, g_good_dll);
	copy_to(g_good_dep, g_sdk_dir, "PulsarNvFakeDep.dll");

	/* Same shape, but the DLL is below both minima. */
	seed_afx(g_old_dir, g_evil_dll, true);
	seed_vfx(g_old_dir, g_evil_dll);
	copy_to(g_evil_dep, g_old_dir, "PulsarNvFakeDep.dll");

	/* A file with the right name and no version resource at all. */
	seed_afx(g_junk_dir, g_good_dll, true);
	remove_file(g_junk_dir, PULSAR_NV_AFX_DLL);
	write_file(g_junk_dir, PULSAR_NV_AFX_DLL, "MZ but not really");

	/* THE ATTACKER'S DROP: homonyms in the application directory. Both
	 * the first-level DLL and the transitive dependency, because the
	 * search order gives that directory priority for both. */
	copy_to(g_evil_dll, g_exe_dir, PULSAR_NV_VFX_DLL);
	copy_to(g_evil_dep, g_exe_dir, "PulsarNvFakeDep.dll");
}

static void remove_tree(const char *dir)
{
	char pattern[MAX_PATH];
	char child[MAX_PATH];
	WIN32_FIND_DATAA fd;
	HANDLE h;

	join(pattern, dir, "*");
	h = FindFirstFileA(pattern, &fd);
	if (h == INVALID_HANDLE_VALUE)
		return;
	do {
		if (strcmp(fd.cFileName, ".") == 0 || strcmp(fd.cFileName, "..") == 0)
			continue;
		join(child, dir, fd.cFileName);
		if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
			remove_tree(child);
		else
			DeleteFileA(child);
	} while (FindNextFileA(h, &fd));
	FindClose(h);
	RemoveDirectoryA(dir);
}

static void drop_fixtures(void)
{
	/* The homonyms are removed even on failure: leaving a file called
	 * NVVideoEffects.dll in a build output directory would be a nasty
	 * thing to hand to the next run. */
	remove_file(g_exe_dir, PULSAR_NV_VFX_DLL);
	remove_file(g_exe_dir, "PulsarNvFakeDep.dll");
	remove_tree(g_tmp_root);
}

/* ---- helpers ---------------------------------------------------------- */

typedef int(__cdecl *marker_fn)(void);

static int call_marker(HMODULE h, const char *sym)
{
	marker_fn fn = (marker_fn)(void *)GetProcAddress(h, sym);
	return fn ? fn() : -1;
}

static bool module_path_is_under(HMODULE h, const char *dir)
{
	char path[MAX_PATH];
	size_t len = strlen(dir);

	if (GetModuleFileNameA(h, path, MAX_PATH) == 0)
		return false;
	return _strnicmp(path, dir, len) == 0 && path[len] == '\\';
}

/* ---- the tests -------------------------------------------------------- */

static void test_dir_validation(void)
{
	char out[MAX_PATH];

	section("directory validation -- what may designate an SDK");

	check(pulsar_nv_validate_dir(g_sdk_dir, out, MAX_PATH) && strcmp(out, g_sdk_dir) == 0,
	      "an existing absolute local directory is accepted");
	check(!pulsar_nv_validate_dir("", out, MAX_PATH), "empty is rejected");
	check(!pulsar_nv_validate_dir("relative\\path", out, MAX_PATH),
	      "a relative path is rejected (it would resolve against the CWD)");
	check(!pulsar_nv_validate_dir("\\\\server\\share\\sdk", out, MAX_PATH),
	      "a UNC path is rejected (remote provider of DLLs and models)");
	check(!pulsar_nv_validate_dir("C:\\Windows\\..\\Windows\\System32", out, MAX_PATH),
	      "a '..' segment is rejected even when it resolves somewhere real");
	check(!pulsar_nv_validate_dir("C:\\Windows\\.\\System32", out, MAX_PATH), "a '.' segment is rejected");
	check(!pulsar_nv_validate_dir("C:\\pulsar-nv-does-not-exist-167", out, MAX_PATH),
	      "a non-existent directory is rejected");
	check(pulsar_nv_validate_dir("C:\\Windows\\System32", out, MAX_PATH) &&
		      _stricmp(out, "C:\\Windows\\System32") == 0,
	      "a well-formed system directory is accepted unchanged");
}

/* Criterion 3, first level. */
static void test_application_directory_is_not_searched(void)
{
	HMODULE h;

	section("criterion 3 [CI] -- the application directory is not in the search");

	{
		char homonym[MAX_PATH];
		join(homonym, g_exe_dir, PULSAR_NV_VFX_DLL);
		check(GetFileAttributesA(homonym) != INVALID_FILE_ATTRIBUTES,
		      "fixture: a homonym NVVideoEffects.dll IS sitting next to this executable");
	}

	h = pulsar_nv_load_from_dir(g_sdk_dir, PULSAR_NV_VFX_DLL);
	check(h != NULL, "the DLL loads from the designated directory");
	if (h) {
		check(call_marker(h, "pulsar_nv_fake_marker") == 1,
		      "the DESIGNATED copy is the one loaded (marker 1), not the application-directory homonym (2)");
		check(module_path_is_under(h, g_sdk_dir),
		      "the resolved module path lies under the designated directory");
		check(!module_path_is_under(h, g_exe_dir), "the resolved module path is NOT the application directory");

		/* The flag under test. Without LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR
		 * this import would come from the application directory even
		 * though the DLL itself did not -- which is the hole Bastion
		 * asked to have closed explicitly. */
		check(call_marker(h, "pulsar_nv_fake_dep_marker_via") == 11,
		      "the DLL's OWN import also resolves from the designated directory (LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR)");
		FreeLibrary(h);
	}

	/* No designated directory means no load -- not a fallback search that
	 * would find the homonym. */
	h = pulsar_nv_load_from_dir("C:\\pulsar-nv-does-not-exist-167", PULSAR_NV_VFX_DLL);
	check(h == NULL, "an absent designated directory yields NO load at all (no fallback to the application dir)");
	if (h)
		FreeLibrary(h);

	check((PULSAR_NV_LOAD_FLAGS & LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR) != 0,
	      "LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR is part of the load flags");
	check((PULSAR_NV_LOAD_FLAGS & LOAD_LIBRARY_SEARCH_DEFAULT_DIRS) != LOAD_LIBRARY_SEARCH_DEFAULT_DIRS,
	      "LOAD_LIBRARY_SEARCH_DEFAULT_DIRS is NOT used (it would re-admit the application directory)");
}

/* Criterion 3 bis -- reported, never claimed. */
static void report_transitive_criterion(void)
{
	struct pulsar_nv_probe_result probe;

	section("criterion 3 bis [PREUVE MANUELLE] -- real CUDA/TensorRT transitive load");

	SetEnvironmentVariableA(PULSAR_NV_AFX_DIR_ENV, NULL);
	SetEnvironmentVariableA(PULSAR_NV_VFX_DIR_ENV, NULL);
	pulsar_nv_probe(&probe);

	if (probe.afx.usable || probe.vfx.usable) {
		printf("  NOTE  a real SDK appears to be installed on this machine; criterion 3 bis is still\n"
		       "        NOT exercised by this binary -- it needs a run against the genuine NVIDIA DLL,\n"
		       "        reported by hand on the PR with machine and SDK version.\n");
	} else {
		printf("  NOT EXERCISED  no NVIDIA SDK on this machine, so the genuine first-level DLL never\n"
		       "        loads and the genuine transitive search never happens. This is NOT a pass.\n"
		       "        The fake-dependency check above proves the FLAG is in force; it does not\n"
		       "        prove the real CUDA/TensorRT chain. See docs/runbooks/nv-filters-rollback.md.\n");
	}
}

/* Criterion 2: presence and versions, read off the system. */
static void test_probe_versions(void)
{
	struct pulsar_nv_sdk_probe p;

	section("criterion 2 [CI] -- presence and version, read; unreadable means absent");

	SetEnvironmentVariableA(PULSAR_NV_AFX_DIR_ENV, g_sdk_dir);
	pulsar_nv_probe_afx(&p);
	check(p.dir_valid && p.dlls_present, "a complete fixture SDK is seen as present");
	check(p.version_readable && p.version == PULSAR_NV_MIN_AFX_VERSION,
	      "the version is READ off the file (1.6.1.2), not assumed");
	check(p.version_ok && p.usable, "meeting the minimum makes it usable");

	SetEnvironmentVariableA(PULSAR_NV_AFX_DIR_ENV, g_old_dir);
	pulsar_nv_probe_afx(&p);
	check(p.dlls_present, "an outdated SDK is still seen as PRESENT");
	check(p.version_readable && p.version < p.min_version, "its version reads back below the minimum");
	check(!p.version_ok && !p.usable, "below the minimum is not usable");

	SetEnvironmentVariableA(PULSAR_NV_AFX_DIR_ENV, g_junk_dir);
	pulsar_nv_probe_afx(&p);
	check(p.dlls_present, "a file with the right name is present...");
	check(!p.version_readable && p.version == 0, "...but an unreadable version is reported ABSENT, as 0");
	check(!p.version_ok && !p.usable, "an unreadable version is never assumed sufficient");

	SetEnvironmentVariableA(PULSAR_NV_AFX_DIR_ENV, "C:\\pulsar-nv-does-not-exist-167");
	pulsar_nv_probe_afx(&p);
	check(!p.dir_valid && !p.dlls_present && !p.usable && p.dir[0] == '\0',
	      "an invalid designated directory yields a wholly negative probe");

	SetEnvironmentVariableA(PULSAR_NV_AFX_DIR_ENV, NULL);
	pulsar_nv_probe_afx(&p);
	check(!p.dir_valid && !p.usable, "no designated directory at all yields a negative probe");
}

/* Criterion 4 ter: the three model files are part of the probe. */
static void test_probe_models(void)
{
	struct pulsar_nv_sdk_probe p;
	char models[MAX_PATH];
	int i;

	section("criterion 4 ter [CI] -- the three .trtpkg models are checked, all of them");

	join(models, g_sdk_dir, PULSAR_NV_MODEL_SUBDIR);
	SetEnvironmentVariableA(PULSAR_NV_AFX_DIR_ENV, g_sdk_dir);

	pulsar_nv_probe_afx(&p);
	check(p.models_present && p.usable, "all three models present: usable");

	for (i = 0; i < PULSAR_NV_AFX_MODEL_COUNT; i++) {
		char what[256];
		remove_file(models, pulsar_nv_afx_models[i]);
		pulsar_nv_probe_afx(&p);
		snprintf(what, sizeof(what), "removing %s alone makes the probe negative", pulsar_nv_afx_models[i]);
		check(!p.models_present && !p.usable, what);
		write_file(models, pulsar_nv_afx_models[i], "not a real trtpkg");
	}

	pulsar_nv_probe_afx(&p);
	check(p.models_present && p.usable, "restoring them restores the probe");

	SetEnvironmentVariableA(PULSAR_NV_AFX_DIR_ENV, NULL);
}

/* Criterion 4: a negative probe means the module does not load. */
static void test_module_gate(void)
{
	struct pulsar_nv_probe_result probe;

	section("criterion 4 [CI] -- negative probe means the module is not loaded");

	SetEnvironmentVariableA(PULSAR_NV_AFX_DIR_ENV, NULL);
	SetEnvironmentVariableA(PULSAR_NV_VFX_DIR_ENV, NULL);
	/* Neutralise the %ProgramFiles% fallback for the VFX arm, so this
	 * asserts the gate rather than the absence of an install. */
	SetEnvironmentVariableA(PULSAR_NV_VFX_DIR_ENV, "C:\\pulsar-nv-does-not-exist-167");
	pulsar_nv_probe(&probe);
	check(!probe.afx.usable && !probe.vfx.usable, "no SDK: both arms negative");
	check(!pulsar_nv_module_should_load(&probe), "no SDK: obs_module_load() refuses, so no loader ever runs");

	SetEnvironmentVariableA(PULSAR_NV_VFX_DIR_ENV, g_old_dir);
	pulsar_nv_probe(&probe);
	check(!probe.vfx.usable && !pulsar_nv_module_should_load(&probe),
	      "SDK present but below the minimum: still refused");

	SetEnvironmentVariableA(PULSAR_NV_VFX_DIR_ENV, g_sdk_dir);
	pulsar_nv_probe(&probe);
	check(probe.vfx.usable && pulsar_nv_module_should_load(&probe),
	      "a valid VFX SDK alone is enough to admit the module");

	SetEnvironmentVariableA(PULSAR_NV_VFX_DIR_ENV, NULL);
}

int main(int argc, char **argv)
{
	if (argc != 5) {
		fprintf(stderr, "usage: %s <good-sdk.dll> <evil-sdk.dll> <good-dep.dll> <evil-dep.dll>\n", argv[0]);
		return 2;
	}
	g_good_dll = argv[1];
	g_evil_dll = argv[2];
	g_good_dep = argv[3];
	g_evil_dep = argv[4];

	printf("nv-probe-test -- Pulsar #167 / Prism ADR 023 Amendment 3\n");

	build_fixtures();

	test_dir_validation();
	test_application_directory_is_not_searched();
	test_probe_versions();
	test_probe_models();
	test_module_gate();
	report_transitive_criterion();

	drop_fixtures();

	printf("\n%d checks, %d failures\n", g_checks, g_failures);
	return g_failures == 0 ? 0 : 1;
}
