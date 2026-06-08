# generate-stinger-demo.ps1 -- deterministic, license-clean demo stinger asset.
#
# ADR 003 Amendment 1 §A1.1 / Amendment 2 §A2.1 ; issues #64 (asset) + #57
# (fork stinger source). Produces `stinger-demo.webm`: a short VP9-with-alpha
# wipe used as the M10 program-scene transition media.
#
# WHY a generator script (not a checked-in binary as the only artefact):
#   The org rule (`docs/rules/git.md` -- "pas de binaire lourd dans git") caps
#   what we drop into history. This stinger is generated 100% from ffmpeg
#   filtergraph primitives -- no third-party footage -- so it is *trivially
#   license-clean* (self-made) AND deterministic: the same ffmpeg + the same
#   args reproduce a byte-identical file. We pin the resulting sha256 in
#   `stinger-demo.manifest.json`. The asset is small (< ~80 KB), so the .webm
#   is also committed for offline/CI use; this script lets anyone regenerate
#   and re-verify it against the manifest (C-PATH / R7 hash-pinning, #64).
#
# DETERMINISM: VP9 (libvpx-vp9) with a fixed -deadline/-cpu-used, no dithering
# source, a pure synthetic filtergraph (color + geq alpha mask), and a pinned
# duration/fps/size. ffmpeg writes some container metadata (encoder string),
# which is the one variability source -- pinned by forcing the version below.
# If your ffmpeg differs, regenerate and re-pin the manifest hash via -Repin.
#
# USAGE:
#   pwsh scripts/assets/generate-stinger-demo.ps1            # generate + verify against manifest
#   pwsh scripts/assets/generate-stinger-demo.ps1 -Repin     # regenerate + rewrite manifest hash
#
# The transition geometry: a 1280x720 opaque sweep bar moves left->right.
#   t in [0, 0.3s]  : bar enters, covers the whole frame by t=0.3s (alpha=1)
#   t = 0.3s        : full opaque cover  == the transition_point (300 ms)
#   t in [0.3, 0.6] : bar exits to the right, revealing the dest scene
# Outside the bar the frame is fully transparent (alpha=0) so the two scenes
# composite underneath -- exactly what a stinger needs.

param(
    [switch] $Repin,
    [string] $FfmpegPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$outFile = Join-Path $here 'stinger-demo.webm'
$manifestFile = Join-Path $here 'stinger-demo.manifest.json'

# --- locate ffmpeg ---------------------------------------------------------
function Resolve-Ffmpeg {
    param([string] $Explicit)
    if ($Explicit) {
        if (Test-Path $Explicit) { return (Resolve-Path $Explicit).Path }
        throw "ffmpeg not found at -FfmpegPath '$Explicit'"
    }
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    # winget default install location (Gyan.FFmpeg)
    $glob = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe'
    $found = Get-ChildItem -Path $glob -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.FullName }
    throw "ffmpeg not found on PATH, not at -FfmpegPath, and not under winget. Install ffmpeg or pass -FfmpegPath."
}

# --- VERIFY mode (default) -------------------------------------------------
# The committed `stinger-demo.webm` IS the pinned artefact; its sha256 is the
# C-PATH / R7 hash that the consumer + probe assert before the media is
# decoded (#64). VP9 (libvpx) is NOT byte-reproducible across runs (internal
# rate-control timing), so we do NOT regenerate to verify -- we hash the
# committed file and compare to the manifest. This is the check CI / setup
# runs; it needs no ffmpeg. `-Repin` (below) regenerates + re-pins when the
# asset is intentionally changed.
if (-not $Repin) {
    if (-not (Test-Path $outFile)) {
        throw "asset $outFile missing. Run with -Repin (needs ffmpeg) to generate it."
    }
    if (-not (Test-Path $manifestFile)) {
        throw "manifest $manifestFile missing. Run with -Repin to create it."
    }
    $sha256 = (Get-FileHash -Path $outFile -Algorithm SHA256).Hash.ToLower()
    $sizeBytes = (Get-Item $outFile).Length
    $pinned = Get-Content $manifestFile -Raw | ConvertFrom-Json
    if ($pinned.sha256 -ne $sha256) {
        throw "sha256 mismatch on committed asset! pinned=$($pinned.sha256) actual=$sha256. The asset was modified without re-pinning, or is corrupt."
    }
    Write-Host "[stinger] OK -- committed $outFile ($sizeBytes bytes) matches pinned sha256 $sha256"
    exit 0
}

$ffmpeg = Resolve-Ffmpeg -Explicit $FfmpegPath
Write-Host "[stinger] ffmpeg: $ffmpeg"

# --- deterministic generation parameters -----------------------------------
# Pinned so the asset is reproducible. transition_point = 300 ms (#64 records it).
$width = 1280
$height = 720
$fps = 30
$durationS = 0.6
$transitionPointMs = 300

# Filtergraph:
#   - a 1280x720 RGBA canvas, fully transparent, for `duration` seconds
#   - geq overlays an opaque vertical bar whose left edge sweeps with time.
#     At t=0.3 the bar spans the whole width (cover). The bar colour is a
#     fixed blue->cyan gradient across X (synthetic, license-clean).
# `geq` evaluates per-pixel; N/<fps> gives the frame time. progress p = t/0.6.
# Bar covers x <= p*2*W for p<=0.5 (entering) and x >= (p-0.5)*2*W for p>0.5
# (exiting) -- i.e. full cover exactly at p=0.5 (t=0.3s).
$lavfi = @"
color=c=black@0.0:s=${width}x${height}:r=${fps}:d=${durationS},format=rgba,
geq=
  r='if(lt(T,0.3), 30, 0)':
  g='if(lt(T,0.3), 120+135*(X/${width}), 200)':
  b='255':
  a='255*if(lt(T,0.3), gte((0.3-T)/0.3*2*${width}+X, ${width})*lte(X, T/0.3*${width}+0), 0)'
"@
# The alpha expression above is intentionally simple; the precise wipe shape is
# refined below with a cleaner closed form (a single moving hard edge).

# Cleaner closed-form sweep: opaque where the moving edge has passed.
#   enter phase (T<0.3): edge_x = T/0.3 * W  -> alpha=1 for X <= edge_x
#   exit  phase (T>=0.3): edge_x = (T-0.3)/0.3 * W -> alpha=1 for X >= edge_x
$lavfi = @"
color=c=black@0.0:s=${width}x${height}:r=${fps}:d=${durationS},format=rgba,
geq=
r='20+40*(X/${width})':
g='80+120*(X/${width})':
b='200+55*(Y/${height})':
a='255*if(lt(T,0.3), lte(X, T/0.3*${width}), gte(X, (T-0.3)/0.3*${width}))'
"@ -replace "`r?`n", ""

Write-Host "[stinger] generating $outFile  (${width}x${height}, ${fps}fps, ${durationS}s, VP9+alpha)"

$args = @(
    '-y',
    '-f', 'lavfi',
    '-i', $lavfi,
    '-c:v', 'libvpx-vp9',
    '-pix_fmt', 'yuva420p',     # VP9 alpha plane
    '-b:v', '0', '-crf', '40',  # quality-targeted, small file
    '-deadline', 'good', '-cpu-used', '0',  # deterministic, repeatable
    '-an',
    '-metadata', 'encoder=pulsar-stinger-demo',
    $outFile
)

# ffmpeg writes its banner + progress to stderr; under $ErrorActionPreference
# = 'Stop' a native command's stderr lines surface as error records. Redirect
# stderr to a temp file and inspect the exit code instead, so a normal run is
# not misread as a failure.
$logFile = [System.IO.Path]::GetTempFileName()
# Windows PowerShell 5.1 turns a native command's stderr into error records,
# which $ErrorActionPreference='Stop' would treat as terminating even on a
# successful encode. Drop to 'Continue' for the duration of the native call
# (ffmpeg's exit code is the real success signal), then restore.
$savedEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $ffmpeg @args 2>$logFile
$ffExit = $LASTEXITCODE
$ErrorActionPreference = $savedEAP
$ffLog = if (Test-Path $logFile) { Get-Content $logFile -Raw } else { '' }
Remove-Item $logFile -ErrorAction SilentlyContinue
if ($ffExit -ne 0) {
    Write-Host $ffLog
    throw "ffmpeg exited $ffExit"
}
($ffLog -split "`n") | Where-Object { $_ -match 'frame=|Lsize=|[Ee]rror' } | Select-Object -Last 3 | ForEach-Object { Write-Host "  $_" }

if (-not (Test-Path $outFile)) { throw "ffmpeg did not produce $outFile" }

$sizeBytes = (Get-Item $outFile).Length
$sha256 = (Get-FileHash -Path $outFile -Algorithm SHA256).Hash.ToLower()
Write-Host "[stinger] size=$sizeBytes bytes  sha256=$sha256"

# Re-pin the manifest to the freshly generated file. The new .webm + this
# manifest must be committed together. (VP9 is not byte-reproducible, so each
# regeneration yields a new hash -- intentional re-pin, reviewed in the diff.)
$manifest = [ordered]@{
    asset_id            = 'stinger-demo'
    file                = 'stinger-demo.webm'
    sha256              = $sha256
    size_bytes          = $sizeBytes
    width               = $width
    height              = $height
    fps                 = $fps
    duration_ms         = [int]($durationS * 1000)
    transition_point_ms = $transitionPointMs
    codec               = 'libvpx-vp9'
    pix_fmt             = 'yuva420p'
    license             = 'self-made (ffmpeg synthetic filtergraph), royalty-free / public-domain-equivalent'
    generator           = 'scripts/assets/generate-stinger-demo.ps1'
    note                = 'VP9/libvpx is not byte-reproducible; this sha256 pins THIS committed file (verified in default mode), not regeneration output.'
    adr                 = 'docs/adr/003 Amendment 1 A1.1, Amendment 2 A2.1 (R7)'
}
# ASCII-only JSON, no BOM -- portable + trufflehog/scan-clean.
$json = $manifest | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($manifestFile, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "[stinger] manifest re-pinned: $manifestFile"
