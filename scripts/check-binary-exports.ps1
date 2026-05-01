# check-binary-exports.ps1 -- LICENSE-INVARIANTS.md (#3) binary gate.
#
# Scans every Pulsar-owned binary in the rundir and asserts the
# Windows export table conforms to the invariant:
#
#   pulsar.exe                 -> table MUST be empty
#   pulsar-browser-page.exe    -> table MUST be empty
#   pulsar-*.dll, obs-websocket.dll
#                              -> table MUST contain ONLY the OBS module
#                                 ABI symbols (obs_module_*) -- libobs
#                                 calls them via GetProcAddress, they are
#                                 unavoidable. Any *other* exported
#                                 symbol is an FFI surface that violates
#                                 the process-boundary contract.
#
# Called from build.yml, release.yml, live-test.yml. `dumpbin` is on PATH
# after ilammy/msvc-dev-cmd@v1.
#
# Exits 0 on success, 1 on any breach. Prints every violating symbol so
# the failure log is actionable.

param(
    [string] $RundirRoot = "upstream/build_x64/rundir/RelWithDebInfo"
)

$ErrorActionPreference = 'Stop'

# Symbols that libobs requires every plugin DLL to export. Source:
# upstream/libobs/obs-module.h -- the union of OBS_DECLARE_MODULE +
# OBS_MODULE_USE_DEFAULT_LOCALE + OBS_MODULE_AUTHOR macros that every
# upstream / Pulsar plugin uses. Every name here is a libobs API
# contract surface, NOT an FFI an external consumer could legitimately
# bind to -- libobs reaches them via GetProcAddress.
$ObsModuleAbi = @(
    'obs_current_module',
    'obs_module_set_pointer',
    'obs_module_ver',
    'obs_module_load',
    'obs_module_unload',
    'obs_module_post_load',
    'obs_module_set_locale',
    'obs_module_free_locale',
    'obs_module_name',
    'obs_module_description',
    'obs_module_author',
    'obs_module_get_string'
)

function Get-ExportedSymbols {
    param([string] $Path)
    $raw = & dumpbin /exports $Path 2>&1
    # dumpbin format:
    #   ordinal hint RVA      name
    #         1    0 00001000 obs_module_load
    # Match the data rows only (skip headers, summary, blank lines).
    $names = @()
    foreach ($line in $raw) {
        if ($line -match '^\s+\d+\s+[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+(\S+)') {
            $names += $matches[1]
        }
    }
    return ,$names
}

$violations = @()

# 1. pulsar.exe  -- must export nothing.
$pulsar = Get-ChildItem -Recurse -Filter "pulsar.exe" -Path $RundirRoot -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pulsar) {
    Write-Error "pulsar.exe not found under $RundirRoot -- build did not produce the expected artefact."
    exit 1
}
$names = Get-ExportedSymbols -Path $pulsar.FullName
if ($names.Count -gt 0) {
    foreach ($n in $names) {
        $violations += "pulsar.exe exports unauthorised symbol: $n"
    }
} else {
    Write-Host "OK  pulsar.exe                  (0 exports)"
}

# 2. pulsar-browser-page.exe -- must export nothing.
$helper = Get-ChildItem -Recurse -Filter "pulsar-browser-page.exe" -Path $RundirRoot -ErrorAction SilentlyContinue | Select-Object -First 1
if ($helper) {
    $names = Get-ExportedSymbols -Path $helper.FullName
    if ($names.Count -gt 0) {
        foreach ($n in $names) {
            $violations += "pulsar-browser-page.exe exports unauthorised symbol: $n"
        }
    } else {
        Write-Host "OK  pulsar-browser-page.exe     (0 exports)"
    }
} else {
    Write-Host "skip pulsar-browser-page.exe    (not built; -Full required)"
}

# 3. Pulsar plugin DLLs -- only OBS module ABI symbols allowed.
$pluginDir = Join-Path $RundirRoot 'obs-plugins/64bit'
$plugins = @()
foreach ($pat in @('pulsar-*.dll', 'obs-websocket.dll')) {
    $plugins += Get-ChildItem -Path $pluginDir -Filter $pat -ErrorAction SilentlyContinue
}
foreach ($dll in $plugins) {
    $names = Get-ExportedSymbols -Path $dll.FullName
    $extra = $names | Where-Object { $_ -notin $ObsModuleAbi }
    if ($extra.Count -gt 0) {
        foreach ($n in $extra) {
            $violations += "$($dll.Name) exports unauthorised symbol: $n"
        }
    } else {
        Write-Host "OK  $($dll.Name) ($($names.Count) ABI symbol(s))"
    }
}

if ($violations.Count -gt 0) {
    Write-Host ""
    Write-Error "LICENSE-INVARIANTS.md (#3) violation -- $($violations.Count) unauthorised export(s):"
    foreach ($v in $violations) { Write-Host "  - $v" }
    exit 1
}

Write-Host ""
Write-Host "::notice::All Pulsar binaries conform to LICENSE-INVARIANTS.md (#3) -- only OBS module ABI symbols exported."
