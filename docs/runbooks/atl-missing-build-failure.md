# Build failure: missing ATL headers

Applies to local Windows builds reporting C1083 for `atlbase.h`,
`atlcomcli.h` or `atlstr.h`. The ATL gate is already integrated in the
pinned OBS fork; it is not a missing patch to reintroduce.

## Diagnose

ATL headers belong to the MSVC toolset, typically:

```text
<VS installation>/VC/Tools/MSVC/<toolset>/atlmfc/include/
```

Check the actual toolset selected by CMake, not merely whether another VS
installation has ATL. `scripts/build-win.ps1` uses `Test-AtlAvailable` and
injects `PULSAR_HAVE_ATL=OFF` when it cannot find the required headers.
The pinned plugin CMake logic then disables the ATL-dependent registrations.

Inspect the configure summary, `CMakeCache.txt` and the first compiler error.
A successful reduced build does not mean all release capabilities are present.

## Capability consequences

Without ATL, QSV/DirectShow-related modules can be omitted. The CEF/x264
headless path can still build, but dual-lane DirectShow return qualification
and a complete release cannot be inferred from that reduced configuration.
A Full browser build does not itself install ATL.

Current native tests include return behavior. Do not reuse the historical
claim that no probe exercises these modules, or treat hardware/absent-module
skips as proof of the omitted path.

## Restore the toolchain

Use Visual Studio Installer to modify the selected VS2022/Build Tools
installation and add the C++ ATL component for its v143 toolset. This is a
machine-level installation requiring the operator's authority.

Re-run the normal configure/build after installation:

```powershell
.\\scripts\\build-win.ps1 -Full
```

Verify the configure summary and expected module inventory, then run the
native/DirectShow checks described in [development](../DEVELOPMENT.md).
Use a clean rebuild only when the cache/artifact state actually requires it.

`build-win.ps1` has no `-CMakeArgs` parameter. Do not paste the obsolete
override command from older versions of this runbook. Do not remove the
detection gate or force a missing toolchain to appear complete.

## Release parity

Release CI must build the intended module set and package both variants.
Record the actual toolchain and checks: a historical hosted-runner image
containing ATL is not proof that a future image or local installation does.
