# build-win.ps1 — Windows build pipeline for Pulsar (Phase 1).
#
# Phase 1 strategy: invoke obs-studio's own CMake preset (windows-x64)
# against the vendored upstream/ submodule. The preset auto-fetches
# obs-deps prebuilt + Qt6 + CEF from obs-deps releases.
#
# We do NOT yet apply Pulsar patches or build Pulsar plugins — Phase 1
# proves the toolchain integration end-to-end. Patches/plugins land
# in Phase 2+.
#
# Output: upstream/build_x64/RelWithDebInfo/obs64.exe (full GUI obs-studio).

param(
    [ValidateSet('configure', 'build', 'all')]
    [string] $Stage = 'all',
    [switch] $CI
)

# Windows PowerShell 5.1 wraps native command stderr lines as
# ErrorRecords. With $ErrorActionPreference = 'Stop', a single cmake
# AUTHOR_WARNING (e.g. FindDetours emitting "Failed to find detours
# version.") aborts the script. Use 'Continue' and rely on the
# explicit $LASTEXITCODE check after each native invocation.
$ErrorActionPreference = 'Continue'

$root = Resolve-Path "$PSScriptRoot\.."
$upstream = Join-Path $root 'upstream'
$preset = if ($CI) { 'windows-ci-x64' } else { 'windows-x64' }

if (-not (Test-Path (Join-Path $upstream 'CMakePresets.json'))) {
    throw "upstream/ submodule not initialised. Run: git submodule update --init --recursive"
}

# Verify nested submodules of upstream/ are present. obs-studio
# vendors libdshowcapture, obs-browser, and obs-websocket as
# submodules — `git submodule add` only fetches the immediate parent,
# so a fresh clone without --recurse-submodules misses them and
# CMake fails on missing source files (e.g. dshowcapture.hpp).
$nestedProbes = @(
    'deps/libdshowcapture/src/dshowcapture.hpp',
    'plugins/obs-browser/CMakeLists.txt',
    'plugins/obs-websocket/CMakeLists.txt'
)
$missingNested = $false
foreach ($probe in $nestedProbes) {
    if (-not (Test-Path (Join-Path $upstream $probe))) { $missingNested = $true; break }
}
if ($missingNested) {
    Write-Host "Initialising upstream's nested submodules..."
    Push-Location $upstream
    try {
        & git submodule update --init --recursive
        if ($LASTEXITCODE -ne 0) { throw "Nested submodule init failed" }
    } finally {
        Pop-Location
    }
}

# Locate cmake — prefer the one we install at D:\DevTools\CMake\bin\cmake.exe
# so this script works without PATH being set.
$cmake = $null
$cmakeCandidates = @(
    'D:\DevTools\CMake\bin\cmake.exe',
    'C:\Program Files\CMake\bin\cmake.exe',
    'cmake'
)
foreach ($c in $cmakeCandidates) {
    if ($c -eq 'cmake') {
        if (Get-Command cmake -ErrorAction SilentlyContinue) { $cmake = 'cmake'; break }
    } elseif (Test-Path $c) {
        $cmake = $c; break
    }
}
if (-not $cmake) { throw "CMake not found. Install via D:\DevTools\CMake (see docs/DEVELOPMENT.md)." }

Write-Host "Using cmake: $cmake"
Write-Host "Preset:       $preset"
Write-Host "Source:       $upstream"

if ($Stage -in @('configure', 'all')) {
    Write-Host ""
    Write-Host "--- Configuring (will fetch obs-deps + Qt6 + CEF on first run) ---"
    Push-Location $upstream
    try {
        & $cmake --preset $preset
        if ($LASTEXITCODE -ne 0) { throw "Configure failed" }
    } finally {
        Pop-Location
    }
}

if ($Stage -in @('build', 'all')) {
    Write-Host ""
    Write-Host "--- Building (RelWithDebInfo) ---"
    # `cmake --build --preset` reads CMakePresets.json from the current
    # directory, so we cd into upstream/ for the build call.
    Push-Location $upstream
    try {
        & $cmake --build --preset $preset --config RelWithDebInfo
        if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    } finally {
        Pop-Location
    }

    $obsExe = Join-Path $upstream 'build_x64\rundir\RelWithDebInfo\bin\64bit\obs64.exe'
    if (Test-Path $obsExe) {
        Write-Host ""
        Write-Host "Built: $obsExe"
    } else {
        Write-Host "Build finished but obs64.exe not found at expected path:"
        Write-Host "  $obsExe"
        Write-Host "Inspect upstream/build_x64/ to locate the produced binary."
    }
}
