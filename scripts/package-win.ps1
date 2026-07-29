# package-win.ps1 -- Phase 12.5 packaging.
#
# Produces a self-contained windows-x64 distribution under
#   dist/pulsar-windows-x64-v<VERSION>/
#
# The output folder is everything pulsar.exe needs at runtime, with
# unused plugins / debug symbols / Mac-only stuff stripped. Designed to
# be consumed by Prism (Phase 13c) -- it copies this whole folder to its
# `resources/pulsar/` and spawns pulsar.exe from there.
#
# Usage:
#   scripts/package-win.ps1                 # produces the folder
#   scripts/package-win.ps1 -Zip            # also produces a .zip
#   scripts/package-win.ps1 -SkipBuild      # skip running build-win.ps1
#                                             (assumes upstream build is fresh)
#
# The version comes from the top-level VERSION file. Bump it manually
# before tagging a release.

param(
    [switch] $Zip,
    [switch] $SkipBuild,
    # Distribution variant:
    #   light (default) -- minimal bundle. game capture + window/monitor
    #     capture + WASAPI + x264/NVENC/QSV/AMF + ffmpeg/aac + filters
    #     + transitions + rtmp_output + ffmpeg_muxer + replay buffer +
    #     virtualcam + Pulsar's own plugins. ~40 MB zip.
    #   full           -- light + obs-browser (CEF) + obs-text +
    #     text-freetype2 + vlc-video. ~250 MB zip. Requires the upstream
    #     build to have been done with -Full (ENABLE_BROWSER=ON), else
    #     this script fails fast with a missing obs-browser.dll error.
    [ValidateSet('light', 'full')]
    [string] $Variant = 'light'
)

$ErrorActionPreference = 'Stop'

$root = Resolve-Path "$PSScriptRoot\.."
$version = (Get-Content (Join-Path $root 'VERSION') -Raw).Trim()

# rundir layout produced by upstream's CMake preset:
#   RelWithDebInfo/
#     bin/64bit/         <- pulsar.exe, obs.dll, Qt6, ffmpeg DLLs (loader CWD)
#     obs-plugins/64bit/ <- libobs scans this via add_default_module_paths
#     data/              <- libobs/ effects + obs-plugins/<name>/ assets
$runtimeRoot = Resolve-Path (Join-Path $root 'upstream\build_x64\rundir\RelWithDebInfo')
$binSrc      = Join-Path $runtimeRoot 'bin\64bit'
$pluginsSrc  = Join-Path $runtimeRoot 'obs-plugins\64bit'
$dataSrc     = Join-Path $runtimeRoot 'data'

$distRoot = Join-Path $root 'dist'
$distName = if ($Variant -eq 'full') {
    "pulsar-windows-x64-full-v$version"
} else {
    "pulsar-windows-x64-v$version"
}
$dist     = Join-Path $distRoot $distName
$binDst   = Join-Path $dist 'bin\64bit'

Write-Host "Pulsar version: $version"
Write-Host "Variant       : $Variant"
Write-Host "Source rundir : $runtimeRoot"
Write-Host "Output target : $dist"
Write-Host ""

if (-not $SkipBuild) {
    Write-Host "--- Running scripts/build-win.ps1 first ---"
    $buildArgs = @()
    if ($Variant -eq 'full') { $buildArgs += '-Full' }
    & (Join-Path $PSScriptRoot 'build-win.ps1') @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "build-win.ps1 failed" }
}

foreach ($p in @($binSrc, $pluginsSrc, $dataSrc)) {
    if (-not (Test-Path $p)) {
        throw "missing rundir component: $p -- run scripts/build-win.ps1 first"
    }
}
if (-not (Test-Path (Join-Path $binSrc 'pulsar.exe'))) {
    throw "pulsar.exe not found in $binSrc -- build is incomplete"
}

# Plugin strip rules. Each entry strips both the .dll under
# obs-plugins/64bit/ and any matching directory under
# data/obs-plugins/<name>/.
#
# Always stripped (both variants):
#   coreaudio-encoder  -- macOS-only encoder, useless on Windows.
#   obs-vst            -- VST audio host, niche + arbitrary DLL load
#                         attack surface. obs-filters covers the standard
#                         compressor/EQ/gate/limiter natively.
#   decklink-*         -- Blackmagic Design hardware, n/a.
#   frontend-tools     -- Lua/Python scripting + auto-remux; redundant
#                         with the embedder's TS host, plus most code
#                         paths null-deref in headless mode.
#   obs-libfdk         -- FDK-AAC, commercial license, off upstream.
#
# NOT stripped any more -- nv-filters (#167, Prism ADR 023 Amendment 3).
#   It was stripped under NS1 because both of its loaders resolved an
#   NVIDIA SDK DLL by BARE NAME off a path read from the inherited
#   environment, which Windows answers from the application directory
#   first: arbitrary code execution in the process holding the Twitch
#   stream key. §A3.1 overrides that strip; §A3.4 requires the invariant to
#   survive the reinstatement, and stripping is no longer what carries it.
#   What carries it now, in this order:
#     (i)  the module refuses to load at all unless a capability probe
#          finds a validated SDK directory, the DLLs, the version minima
#          and the three .trtpkg models -- so with no SDK, which is the
#          normal state, neither loader ever runs;
#     (ii) every load is LoadLibraryExW(<absolute path>, NULL,
#          LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32),
#          so neither the DLL nor its own imports can come from beside
#          pulsar.exe; the CUDA driver library is taken from System32,
#          where the display driver installs it;
#     (iii) the embedder pins the SDK directory (Prism's twin issue).
#   Rules: plugins/pulsar-nv-secure-load/. Gate: tests/nv-probe/ (CTest,
#   no GPU, no SDK). Putting it back: docs/runbooks/nv-filters-rollback.md.
#
#   No NVIDIA DLL and no model file is bundled by this script -- the SDK
#   stays a dependency of the host machine. scripts/check-nv-filters-packaging.py
#   enforces both halves of that sentence in CI.
$baseStrippedPlugins = @(
    'coreaudio-encoder',
    'obs-vst',
    'obs-webrtc',
    'decklink-captions',
    'decklink-output-ui',
    'frontend-tools',
    'obs-libfdk'
)

# Stripped only in the 'light' variant. In 'full' these stay so Prism
# (and any embedder driving Pulsar with composed scenes) gets browser
# sources for HTML overlays, native text sources, and VLC-backed media
# sources.
$lightOnlyStrippedPlugins = @(
    'obs-browser',     # CEF runtime, ~200 MB
    'obs-text',        # GDI+ text source
    'text-freetype2',  # freetype-backed text source (companion to obs-text)
    'vlc-video'        # VLC media source
)

# Always-stripped hardware capture plugins -- we don't bundle them
# anyway because Pulsar targets software/encoded sources, and shipping
# their stub DLLs makes pulsar.exe log "Failed to initialize module"
# warnings at boot.
$baseStrippedPlugins += @('aja', 'decklink')

if ($Variant -eq 'full') {
    $strippedPlugins = $baseStrippedPlugins
} else {
    $strippedPlugins = $baseStrippedPlugins + $lightOnlyStrippedPlugins
}

# CEF runtime files sit alongside the OBS plugin DLLs under
# obs-plugins/64bit/ (NOT under bin/64bit/ as in some other OBS forks).
# Strip them in the light variant; the full variant keeps them so
# obs-browser.dll has a CEF runtime to link against.
$cefRuntimeFiles = @(
    'chrome_elf.dll',
    'libcef.dll',
    'libEGL.dll',
    'libGLESv2.dll',
    'icudtl.dat',
    'v8_context_snapshot.bin',
    'resources.pak',
    'chrome_100_percent.pak',
    'chrome_200_percent.pak'
)
# CEF locale .pak directory under obs-plugins/64bit/.
$cefRuntimeDirs = @('locales')

# Files we never copy: PDBs (debug symbols, ~50 MB), .ilk linker info,
# .exp/.lib intermediate files. Production runtime doesn't need them and
# Prism doesn't want to ship them.
$skippedExtensions = @('*.pdb', '*.ilk', '*.exp', '*.iobj', '*.ipdb')

# --- Wipe + recreate dest ---------------------------------------------------
if (Test-Path $dist) {
    Write-Host "Wiping previous $dist"
    Remove-Item -Recurse -Force $dist
}
New-Item -ItemType Directory -Path $dist -Force | Out-Null
New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

# --- Helpers ----------------------------------------------------------------
function Should-SkipFile([string] $name) {
    foreach ($ext in $skippedExtensions) {
        if ($name -like $ext) { return $true }
    }
    return $false
}

function Copy-Filtered([string] $src, [string] $dst) {
    if (-not (Test-Path $src)) { return }
    Get-ChildItem -Path $src -Recurse -File | ForEach-Object {
        if (Should-SkipFile $_.Name) { return }
        $rel = $_.FullName.Substring($src.Length).TrimStart('\','/')
        $target = Join-Path $dst $rel
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }
        Copy-Item -Force -LiteralPath $_.FullName -Destination $target
    }
}

# --- bin/64bit/ : pulsar.exe + Qt + FFmpeg DLLs ---------------------------
Write-Host ""
Write-Host "--- Copying bin/64bit/ ---"
New-Item -ItemType Directory -Path $binDst -Force | Out-Null
Get-ChildItem -Path $binSrc -File | ForEach-Object {
    if (Should-SkipFile $_.Name) { return }
    Copy-Item -Force -LiteralPath $_.FullName -Destination $binDst
}

# Qt platforms/ subfolder (qminimal.dll required for QApplication)
Copy-Filtered (Join-Path $binSrc 'platforms') (Join-Path $binDst 'platforms')

# --- obs-plugins/64bit/ with strip ----------------------------------------
$pluginsDst = Join-Path $dist 'obs-plugins\64bit'
New-Item -ItemType Directory -Path $pluginsDst -Force | Out-Null

Write-Host ""
Write-Host "--- Copying obs-plugins/64bit/ (filtered) ---"
$skipCef = ($Variant -eq 'light')
$kept = 0
$stripped = 0
$cefStripped = 0
Get-ChildItem -Path $pluginsSrc -File | ForEach-Object {
    if (Should-SkipFile $_.Name) { return }
    $base = [System.IO.Path]::GetFileNameWithoutExtension($_.Name)

    if ($strippedPlugins -contains $base) {
        Write-Host "  strip   $($_.Name)"
        $script:stripped++
        return
    }

    # CEF runtime files (libcef.dll, .pak resources, etc.) live next to
    # obs-browser.dll under obs-plugins/64bit/. Light variant doesn't
    # ship obs-browser, so its CEF deps are dead weight too.
    if ($skipCef) {
        if ($cefRuntimeFiles -contains $_.Name) {
            $script:cefStripped++
            return
        }
        # Catch-all for stray .pak / .bin CEF resources we might miss.
        if ($_.Name -like '*.pak') { $script:cefStripped++; return }
    }

    Copy-Item -Force -LiteralPath $_.FullName -Destination $pluginsDst
    $script:kept++
}
Write-Host "  kept $kept plugin(s), stripped $stripped"
if ($skipCef -and $cefStripped -gt 0) {
    Write-Host "  stripped $cefStripped CEF runtime file(s) (light variant)"
}

# CEF subdirectories (locales/) under obs-plugins/64bit/.
if (-not $skipCef) {
    foreach ($cefDir in $cefRuntimeDirs) {
        $cefSrc = Join-Path $pluginsSrc $cefDir
        if (Test-Path $cefSrc) {
            Copy-Filtered $cefSrc (Join-Path $pluginsDst $cefDir)
        }
    }
}

# --- data/ with strip on plugin subfolders --------------------------------
$dataDst = Join-Path $dist 'data'

Write-Host ""
Write-Host "--- Copying data/ (filtered on stripped plugins) ---"

# data/libobs/ : compositor effects, locale -- always copy
Copy-Filtered (Join-Path $dataSrc 'libobs') (Join-Path $dataDst 'libobs')

# data/obs-plugins/<name>/ : per-plugin assets. Skip stripped plugins.
$dataPluginsSrc = Join-Path $dataSrc 'obs-plugins'
$dataPluginsDst = Join-Path $dataDst 'obs-plugins'
if (Test-Path $dataPluginsSrc) {
    Get-ChildItem -Path $dataPluginsSrc -Directory | ForEach-Object {
        if ($strippedPlugins -contains $_.Name) {
            Write-Host "  strip data/obs-plugins/$($_.Name)/"
            return
        }
        Copy-Filtered $_.FullName (Join-Path $dataPluginsDst $_.Name)
    }
}

# --- Marker file -----------------------------------------------------------
$readme = @"
Pulsar v$version (windows-x64)

Self-contained broadcast engine. Run pulsar.exe; it binds the
obs-websocket v5 WebSocket on loopback :4455 with a session-random
password printed in obs-websocket/config.json (created on first run).

This bundle is designed to be consumed by Prism's Electron installer
(packed as resources/pulsar/) but also works standalone for testing.

Built from https://github.com/ZabLaboratory/Pulsar (commit captured
in build metadata).
"@
$readme | Set-Content -Encoding utf8 (Join-Path $dist 'README.txt')

# --- Summary --------------------------------------------------------------
$total = (Get-ChildItem $dist -Recurse -File | Measure-Object -Property Length -Sum)
$mb = [math]::Round($total.Sum / 1MB, 1)
Write-Host ""
Write-Host "--- Summary ---"
Write-Host "Files : $($total.Count)"
Write-Host "Size  : $mb MB"
Write-Host "Path  : $dist"

# --- Optional zip ---------------------------------------------------------
if ($Zip) {
    $zipPath = Join-Path $distRoot "$distName.zip"
    if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
    Write-Host ""
    Write-Host "--- Zipping to $zipPath ---"
    Compress-Archive -Path "$dist\*" -DestinationPath $zipPath -CompressionLevel Optimal
    $zipMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    Write-Host "Zip   : $zipMb MB"
}
