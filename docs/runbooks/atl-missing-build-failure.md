# Runbook — Build failure: ATL headers missing (C1083)

**Applies to:** local Windows build without the VS2022 "C++ ATL" workload.
**Fixed by:** commit `d634d35` (PR #43), now integrated in the pinned upstream,
plus `scripts/build-win.ps1`.

---

## Symptom

`cmake --build` fails on one or more of:

```
error C1083: Cannot open include file: 'atlbase.h': No such file or directory
error C1083: Cannot open include file: 'atlcomcli.h': No such file or directory
error C1083: Cannot open include file: 'atlstr.h': No such file or directory
```

Faulting plugins: `obs-qsv11`, `win-dshow`, `virtualcam-module` (a sub-target of `win-dshow`).
The error appears during the CMake build step, not during configure.

---

## Diagnostic

ATL headers live under the MSVC toolset, not the Windows SDK:

```
<VS root>\VC\Tools\MSVC\<version>\atlmfc\include\
```

Check whether the directory exists and contains the three headers:

```powershell
$vs = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" `
      -products '*' -property installationPath | Select-Object -First 1
$msvcRoot = Join-Path $vs "VC\Tools\MSVC"
Get-ChildItem $msvcRoot | ForEach-Object {
    $atl = Join-Path $_.FullName "atlmfc\include\atlbase.h"
    [PSCustomObject]@{ Toolset = $_.Name; AtlPresent = (Test-Path $atl) }
}
```

If every row shows `AtlPresent = False`, ATL is not installed. This is the root cause.

`scripts/build-win.ps1` runs `Test-AtlAvailable` automatically and emits:

```
WARNING: ATL not found -> skipping qsv11/virtualcam/win-dshow ; headless browser_source path unaffected
```

If you see that warning and the build succeeded anyway, the gate fired correctly — you are in the OFF branch (see below). If you see the warning and the build *still* fails with C1083, the gate was not applied — confirm you are running `scripts/build-win.ps1` and not invoking CMake directly.

---

## Root cause

The MSVC "C++ ATL" component (`Microsoft.VisualStudio.Component.VC.ATL`) is **not** part of the default "Desktop development with C++" workload install. It is an optional component. The CI runner (`windows-2022`) has it; a freshly installed VS2022 Build Tools box typically does not.

`obs-qsv11`, `win-dshow`, and `win-dshow`'s `virtualcam-module` include ATL headers unconditionally in upstream. Installing ATL requires an elevated UAC prompt (VS installer) that cannot be satisfied in a non-interactive build context.

---

## Fix — the `PULSAR_HAVE_ATL` gate (commit `d634d35`)

The fix is already in place. Two artifacts implement it:

**Pinned `upstream/plugins/CMakeLists.txt`**
Wraps the three plugin registrations in `plugins/CMakeLists.txt` behind
`if(PULSAR_HAVE_ATL) ... else()`. The `else()` branch registers each plugin
as a disabled stub via `target_disable()` so CMake reports them in
`OBS_MODULES_DISABLED` instead of failing configure. The CMake option
defaults to `ON`. The former `patches/0002-*` file was removed after this
change became part of the signed upstream revision.

**`scripts/build-win.ps1` — `Test-AtlAvailable` function**
Runs before the configure step. Uses `vswhere` to enumerate installed VS
instances, then checks each MSVC toolset's `atlmfc\include\` for all three
headers. Falls back to a fixed list of common VS install paths if `vswhere`
is unavailable. Outcome:

| Detection result | Flag injected | Effect |
|---|---|---|
| ATL headers found | `-DPULSAR_HAVE_ATL=ON` (or none — same as default) | All three plugins build normally |
| ATL headers absent | `-DPULSAR_HAVE_ATL=OFF` | Three plugins registered as disabled stubs; build continues |

Because the option defaults `ON`, CI builds (`windows-2022`) never receive the
flag and build everything exactly as upstream — no coverage regression.

---

## Verification (after the gate fires)

After a local build with ATL absent:

1. `scripts/build-win.ps1` exits 0.
2. `upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/` contains
   `pulsar-browser.dll` and the encoder/capture plugins but **not**
   `obs-qsv11.dll`, `win-dshow.dll`, or `win-dshow-virtualcam.dll`.
3. `pulsar.exe` starts and prints the `PULSAR_READY` sentinel.
4. The offline probe suite passes — `obs-qsv11`, `win-dshow`, and
   `virtualcam-module` are not exercised by any probe (none are on the
   headless `browser_source` → x264/nvenc → CEF path).

---

## Rollback

To revert to unconditional build of all plugins (equivalent to upstream
before this fix), pass the flag explicitly:

```powershell
.\scripts\build-win.ps1 -CMakeArgs @("-DPULSAR_HAVE_ATL=ON")
```

This forces the ON branch regardless of detection. If ATL is genuinely
absent the build will fail with C1083 — which is the correct signal that
the toolchain is incomplete for a full build.

Alternatively, remove the `-DPULSAR_HAVE_ATL=OFF` injection from
`Test-AtlAvailable` in `scripts/build-win.ps1` to restore the old
unconditional behavior (not recommended — reintroduces the original breakage).

---

## Restoring local ↔ CI parity (optional)

To build all three plugins locally — matching CI exactly — install the ATL component:

**Via VS Installer (GUI):**
Open "Visual Studio Installer" → Modify your VS2022 Build Tools installation →
Individual components → search "ATL" → check
"C++ ATL for latest v143 build tools (x86 & x64)" → Modify.

**Via winget / `vs_buildtools.exe` (elevated PowerShell):**

```powershell
winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Component.VC.ATL --quiet --wait"
```

After install, `Test-AtlAvailable` will return `$true` on the next build and the three plugins will compile. No code change needed.

---

## Local ↔ CI asymmetry note

| | Local (ATL absent) | CI (`windows-2022`) |
|---|---|---|
| `obs-qsv11` | Disabled stub | Built |
| `win-dshow` | Disabled stub | Built |
| `virtualcam-module` | Disabled stub | Built |
| `pulsar.exe` | Built, fully functional | Built, fully functional |
| Probe suite | Passes (those plugins untested) | Passes |
| Headless live path | Unaffected | Unaffected |

The asymmetry is intentional and safe for Pulsar's use case. The three
skipped plugins are capture/encode peripherals not required by the headless
`browser_source` path. If a future feature requires QSV hardware encode or
DirectShow capture in local development, install ATL (see above).
