/*
 * Stand-in for a TRANSITIVE dependency of an NVIDIA SDK DLL -- the role
 * played in reality by the CUDA runtime, cuDNN or TensorRT.
 *
 * Two copies of this are built, with different markers, and both are named
 * PulsarNvFakeDep.dll: one goes in the designated directory, one next to
 * the test executable (which is that process's "application directory",
 * the same role pulsar.exe's directory plays in production). Whichever
 * marker comes back says which directory Windows resolved the import from
 * -- and that is the whole point of LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR, the
 * flag that governs imports rather than the first-level load.
 *
 * Built with the static CRT on purpose: a vcruntime140.dll dependency
 * would drag a third search into a test about search order.
 */

#include <windows.h>

#ifndef PULSAR_NV_FAKE_DEP_MARKER
#error "PULSAR_NV_FAKE_DEP_MARKER must be defined by the build"
#endif

__declspec(dllexport) int pulsar_nv_fake_dep_marker(void)
{
	return PULSAR_NV_FAKE_DEP_MARKER;
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
	UNREFERENCED_PARAMETER(inst);
	UNREFERENCED_PARAMETER(reason);
	UNREFERENCED_PARAMETER(reserved);
	return TRUE;
}
