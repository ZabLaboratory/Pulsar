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

$ErrorActionPreference = 'Stop'

$root = Resolve-Path "$PSScriptRoot\.."
$upstream = Join-Path $root 'upstream'
$preset = if ($CI) { 'windows-ci-x64' } else { 'windows-x64' }

if (-not (Test-Path (Join-Path $upstream 'CMakePresets.json'))) {
    throw "upstream/ submodule not initialised. Run: git submodule update --init --recursive"
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
    & $cmake --preset $preset -S $upstream
    if ($LASTEXITCODE -ne 0) { throw "Configure failed" }
}

if ($Stage -in @('build', 'all')) {
    Write-Host ""
    Write-Host "--- Building (RelWithDebInfo) ---"
    & $cmake --build --preset $preset --config RelWithDebInfo
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }

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
