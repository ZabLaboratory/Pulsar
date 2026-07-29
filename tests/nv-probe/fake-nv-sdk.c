/*
 * Stand-in for a first-level NVIDIA SDK DLL (NVVideoEffects.dll,
 * NVCVImage.dll, NVAudioEffects.dll).
 *
 * Two copies are built with different markers and different FILEVERSION
 * resources:
 *
 *   "good" -- marker 1, version 1.6.1.2 (clears both SDK minima), goes in
 *             the directory Pulsar designates.
 *   "evil" -- marker 2, version 0.0.0.1 (clears neither), goes next to the
 *             test executable, in the attacker's role.
 *
 * The DLL statically imports pulsar_nv_fake_dep_marker() from
 * PulsarNvFakeDep.dll so that loading it forces Windows to resolve one
 * import BY NAME -- the transitive step. pulsar_nv_fake_dep_marker_via()
 * then reports which copy of the dependency won.
 *
 * Static CRT: see fake-nv-dep.c.
 */

#include <windows.h>

#ifndef PULSAR_NV_FAKE_MARKER
#error "PULSAR_NV_FAKE_MARKER must be defined by the build"
#endif

__declspec(dllimport) int pulsar_nv_fake_dep_marker(void);

__declspec(dllexport) int pulsar_nv_fake_marker(void)
{
	return PULSAR_NV_FAKE_MARKER;
}

__declspec(dllexport) int pulsar_nv_fake_dep_marker_via(void)
{
	return pulsar_nv_fake_dep_marker();
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
	UNREFERENCED_PARAMETER(inst);
	UNREFERENCED_PARAMETER(reason);
	UNREFERENCED_PARAMETER(reserved);
	return TRUE;
}
