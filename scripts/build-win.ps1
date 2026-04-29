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
    [switch] $CI,
    # -GuiBuild keeps obs-studio's Qt frontend + Browser (CEF) plugin
    # enabled. Default is headless: Pulsar disables ENABLE_FRONTEND,
    # ENABLE_UI and ENABLE_BROWSER so libobs builds without Qt6 / CEF.
    # Pass -GuiBuild to opt back in to the full obs-studio build for
    # debugging or comparison runs.
    [switch] $GuiBuild,
    # -Clean wipes upstream/build_x64 before configure so stale
    # artifacts from a previous run (e.g. the obs64.exe + Qt DLLs left
    # behind by a GUI build) do not pollute a subsequent headless run.
    # The downloaded dependency caches under upstream/.deps/ are kept,
    # so re-configure does not re-download obs-deps / Qt6 / CEF; only
    # the CMake cache and compiled artefacts go.
    [switch] $Clean
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

# --- Apply Pulsar patches onto upstream/ -----------------------------------
#
# Reset upstream/ to the SHA recorded by Pulsar's submodule pointer, then
# re-apply every .patch under patches/ via git am. This is idempotent: a
# fresh tree, and any prior run's commits, are wiped before the apply, so
# the resulting upstream/ HEAD is always exactly:
#   recorded_sha + N patches in lexical order
#
# git describe in upstream's versionconfig.cmake then reports something
# like 32.1.2-N-g<sha> rather than -modified, which keeps OBS_VERSION
# clean.

$patchesDir = Join-Path $root 'patches'
$patches = @()
if (Test-Path $patchesDir) {
    $patches = Get-ChildItem $patchesDir -Filter '*.patch' -File | Sort-Object Name
}

# Recorded submodule SHA - read from the index via --cached so the
# value is the immutable pin recorded in Pulsar's tree, not the
# submodule's current HEAD (which may have been advanced by a previous
# `git am` run -- without --cached we'd reset to the post-patch SHA
# and the patch re-apply would fail with "patch does not apply").
$submoduleStatus = & git -C $root submodule status --cached -- upstream
if ($LASTEXITCODE -ne 0) { throw "Could not read submodule status" }
if ($submoduleStatus -notmatch '^[ +-]([0-9a-f]+)') {
    throw "Could not parse submodule SHA from: $submoduleStatus"
}
$recordedSha = $Matches[1]

Write-Host ""
Write-Host "--- Applying Pulsar patches ---"
Write-Host "Pinned upstream commit: $recordedSha"
Write-Host "Patches found:          $($patches.Count)"

Push-Location $upstream
try {
    # Abort any half-applied am session from a previous failed run.
    if (Test-Path '.git/rebase-apply') {
        & git am --abort 2>$null
    }
    & git reset --hard $recordedSha | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not reset upstream/ to $recordedSha" }

    foreach ($patch in $patches) {
        Write-Host "  applying $($patch.Name)"
        & git am --keep-non-patch $patch.FullName
        if ($LASTEXITCODE -ne 0) {
            & git am --abort 2>$null
            throw "Failed to apply $($patch.Name) -- see error above"
        }
    }
} finally {
    Pop-Location
}

if ($Clean) {
    $buildDir = Join-Path $upstream 'build_x64'
    if (Test-Path $buildDir) {
        Write-Host ""
        Write-Host "--- Wiping $buildDir (-Clean) ---"
        Remove-Item -Recurse -Force $buildDir
    }
}

if ($Stage -in @('configure', 'all')) {
    Write-Host ""
    if ($GuiBuild) {
        Write-Host "--- Configuring (GUI build: obs-studio with Qt + Browser) ---"
    } else {
        Write-Host "--- Configuring (headless: ENABLE_FRONTEND=OFF + ENABLE_UI=OFF + ENABLE_BROWSER=OFF) ---"
    }
    # Headless overrides applied via -D on the cmake invocation rather
    # than via a patch on upstream's CMakePresets.json (which is a
    # high-churn upstream file). The preset still fetches Qt6 / CEF
    # tarballs because that is encoded in the dependencies block, but
    # nothing links against them when ENABLE_UI / ENABLE_BROWSER are
    # off.
    # Headless toggles. Two distinct ENABLE flags need to be off:
    #   ENABLE_FRONTEND  - drops the Qt6 obs-studio frontend (frontend/
    #                      CMakeLists.txt:5). This is the real Qt gate.
    #   ENABLE_UI        - gates frontend-api inclusion in the scripting
    #                      modules (obspython, obslua). Without this OFF
    #                      they would still try to link frontend-api,
    #                      which won't exist when ENABLE_FRONTEND=OFF.
    #   ENABLE_BROWSER   - default OFF in obs-browser, but the windows-x64
    #                      preset forces it ON in cacheVariables. Override.
    $extraArgs = @()
    if (-not $GuiBuild) {
        $extraArgs += '-DENABLE_FRONTEND=OFF'
        $extraArgs += '-DENABLE_UI=OFF'
        $extraArgs += '-DENABLE_BROWSER=OFF'
        # ENABLE_WEBSOCKET=OFF -- skip the upstream obs-websocket
        # plugin. It hardcodes Qt UI (forms/SettingsDialog) and
        # frontend-api dependencies that crash in obs_module_load
        # under our headless service: MigratePersistentData calls
        # obs_frontend_get_current_profile_path which returns null
        # without a registered frontend, and the migrate path then
        # dereferences it.
        #
        # Phase 4c will introduce plugins/pulsar-websocket/ -- a
        # vendor-fork of obs-websocket with the Qt UI / frontend
        # dependencies stripped -- and re-enable WebSocket support
        # via that plugin.
        $extraArgs += '-DENABLE_WEBSOCKET=OFF'
    }
    Push-Location $upstream
    try {
        & $cmake --preset $preset @extraArgs
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
        Write-Host "Built: $obsExe (GUI build, stale if -GuiBuild was not passed)"
    }
}

# --- Build Pulsar plugins (top-level Pulsar CMakeLists) -------------------
# Runs after upstream/ is built. plugins/pulsar-headless/ links against
# upstream's libobs and lands pulsar.exe next to obs.dll in the rundir
# so the loader resolves the runtime DLLs without PATH changes.

if ($Stage -in @('build', 'all')) {
    $libobsLib = Join-Path $upstream 'build_x64\libobs\RelWithDebInfo\obs.lib'
    if (-not (Test-Path $libobsLib)) {
        Write-Host ""
        Write-Host "Skipping Pulsar plugin build: libobs.lib not present at $libobsLib"
        Write-Host "Run a full upstream build first."
    } else {
        Write-Host ""
        Write-Host "--- Configuring Pulsar plugins ---"
        $pulsarBuild = Join-Path $root 'build'
        & $cmake -S $root -B $pulsarBuild -G "Visual Studio 17 2022" -A x64
        if ($LASTEXITCODE -ne 0) { throw "Pulsar configure failed" }

        Write-Host ""
        Write-Host "--- Building Pulsar plugins ---"
        & $cmake --build $pulsarBuild --config RelWithDebInfo
        if ($LASTEXITCODE -ne 0) { throw "Pulsar build failed" }

        $pulsarExe = Join-Path $upstream 'build_x64\rundir\RelWithDebInfo\bin\64bit\pulsar.exe'
        if (Test-Path $pulsarExe) {
            Write-Host ""
            Write-Host "Built: $pulsarExe"
        } else {
            Write-Host "Pulsar build finished but pulsar.exe not found at:"
            Write-Host "  $pulsarExe"
        }

        # Stage Qt6 runtime DLLs into the rundir.
        #
        # pulsar-headless creates a QApplication so libobs plugins
        # that link against Qt (notably obs-websocket) can run inside
        # the headless service. We force QT_QPA_PLATFORM=minimal at
        # runtime so no Qt platform plugin DLL is required, but the
        # base Qt6 DLLs themselves still need to be loadable.
        # Upstream's CMake stages these as part of the frontend build
        # steps -- with ENABLE_FRONTEND=OFF that copy never happens.
        $qt6Bin = Get-ChildItem -Path (Join-Path $upstream '.deps') `
                                -Filter 'obs-deps-qt6-*-x64' `
                                -Directory `
                                -ErrorAction SilentlyContinue |
                  Select-Object -First 1
        if ($qt6Bin) {
            $qt6BinDir = Join-Path $qt6Bin.FullName 'bin'
            $rundirBin = Join-Path $upstream 'build_x64\rundir\RelWithDebInfo\bin\64bit'
            if ((Test-Path $qt6BinDir) -and (Test-Path $rundirBin)) {
                $qt6Copied = 0
                foreach ($name in @('Qt6Core.dll','Qt6Gui.dll','Qt6Widgets.dll','Qt6Network.dll','Qt6Svg.dll','Qt6Xml.dll')) {
                    $src = Join-Path $qt6BinDir $name
                    if (Test-Path $src) {
                        Copy-Item -Force $src $rundirBin
                        $qt6Copied++
                    }
                }
                Write-Host "Staged $qt6Copied Qt6 runtime DLLs into rundir/bin/64bit/"
            }

            # Qt platform plugins. The QApplication in pulsar-headless
            # forces QT_QPA_PLATFORM=minimal at runtime; Qt then looks
            # for `platforms/qminimal.dll` next to the exe. Without it
            # QApplication aborts with "Could not find the Qt platform
            # plugin". Copy the minimum we need (minimal + windows for
            # belt-and-braces) into rundir/bin/64bit/platforms/.
            $qt6PluginsDir = Join-Path $qt6Bin.FullName 'plugins'
            $platformsSrc  = Join-Path $qt6PluginsDir 'platforms'
            if (Test-Path $platformsSrc) {
                $platformsDst = Join-Path $rundirBin 'platforms'
                if (-not (Test-Path $platformsDst)) {
                    New-Item -ItemType Directory -Path $platformsDst -Force | Out-Null
                }
                $qt6PlatCopied = 0
                foreach ($name in @('qminimal.dll','qwindows.dll')) {
                    $src = Join-Path $platformsSrc $name
                    if (Test-Path $src) {
                        Copy-Item -Force $src $platformsDst
                        $qt6PlatCopied++
                    }
                }
                Write-Host "Staged $qt6PlatCopied Qt6 platform plugins into rundir/bin/64bit/platforms/"
            }
        }

        # Stage obs-deps runtime DLLs into the rundir.
        #
        # obs.dll imports avcodec-61, avformat-61, avutil-59, swscale-8,
        # swresample-5, zlib (and a few more transitively). Upstream's
        # CMake stages those into the rundir as part of the frontend
        # build steps -- with ENABLE_FRONTEND=OFF the copy never happens
        # and pulsar.exe fails to load with STATUS_DLL_NOT_FOUND
        # (0xC0000135) as soon as the loader resolves obs.dll's imports.
        #
        # Mirror the headless layout ourselves: take every .dll from
        # upstream/.deps/obs-deps-*/bin/ and copy alongside obs.dll.
        # Idempotent (Copy-Item -Force).
        $depsBin = Get-ChildItem -Path (Join-Path $upstream '.deps') `
                                 -Filter 'obs-deps-*' `
                                 -Directory `
                                 -ErrorAction SilentlyContinue |
                   Where-Object { $_.Name -notlike '*-x86*' } |
                   Select-Object -First 1
        if ($depsBin) {
            $depsBinDir = Join-Path $depsBin.FullName 'bin'
            $rundirBin  = Join-Path $upstream 'build_x64\rundir\RelWithDebInfo\bin\64bit'
            if ((Test-Path $depsBinDir) -and (Test-Path $rundirBin)) {
                $copied = 0
                Get-ChildItem $depsBinDir -Filter '*.dll' -File | ForEach-Object {
                    Copy-Item -Force $_.FullName $rundirBin
                    $copied++
                }
                Write-Host "Staged $copied obs-deps runtime DLLs into rundir/bin/64bit/"
            }
        }
    }
}
