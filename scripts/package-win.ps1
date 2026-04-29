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
    [switch] $SkipBuild
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
$distName = "pulsar-windows-x64-v$version"
$dist     = Join-Path $distRoot $distName
$binDst   = Join-Path $dist 'bin\64bit'

Write-Host "Pulsar version: $version"
Write-Host "Source rundir : $runtimeRoot"
Write-Host "Output target : $dist"
Write-Host ""

if (-not $SkipBuild) {
    Write-Host "--- Running scripts/build-win.ps1 first ---"
    & (Join-Path $PSScriptRoot 'build-win.ps1')
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

# Plugins we deliberately do NOT ship. Each entry strips both the .dll
# under obs-plugins/64bit/ and any matching directory under
# data/obs-plugins/<name>/.
#   coreaudio-encoder  -- macOS-only encoder, useless on Windows.
#   obs-vst            -- VST audio plugin host (~10 MB), out of scope.
#   obs-webrtc         -- WHIP/WHEP outputs; Pulsar pushes RTMP, not WebRTC.
#   vlc-video          -- VLC-backed media source; ffmpeg_source covers it.
#   obs-text           -- GDI+ text source; Phase 13+ if needed.
#   text-freetype2     -- companion freetype text source; ditto.
#   decklink-*         -- Blackmagic Design hardware (~10 MB), n/a.
#   frontend-tools     -- Lua/Python scripting + auto-remux; headless n/a.
#   obs-libfdk         -- FDK-AAC, commercial license, off by default upstream.
$strippedPlugins = @(
    'coreaudio-encoder',
    'obs-vst',
    'obs-webrtc',
    'vlc-video',
    'obs-text',
    'text-freetype2',
    'decklink-captions',
    'decklink-output-ui',
    'frontend-tools',
    'obs-libfdk'
)

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
$kept = 0
$stripped = 0
Get-ChildItem -Path $pluginsSrc -File | ForEach-Object {
    if (Should-SkipFile $_.Name) { return }
    $base = [System.IO.Path]::GetFileNameWithoutExtension($_.Name)
    if ($strippedPlugins -contains $base) {
        Write-Host "  strip   $($_.Name)"
        $script:stripped++
        return
    }
    Copy-Item -Force -LiteralPath $_.FullName -Destination $pluginsDst
    $script:kept++
}
Write-Host "  kept $kept plugin(s), stripped $stripped"

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
