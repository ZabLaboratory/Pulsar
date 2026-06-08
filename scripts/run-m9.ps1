# run-m9.ps1 - wrapper for the M9 Blue-trigger live-repaint probe with a
# hard secret grep-assert (ADR Blue 001 R4/R6, M8 parity).
#
# It runs scripts/probe-m9-canvas-live.py, tees stdout to a log, then
# grep-asserts that NONE of the credentials appear in clear in the captured
# stdout OR in the produced before/after proof PNGs / VOD artefacts. The
# credential set for M9 is the SAME operator JWT (it drives BOTH the SETUP
# legs AND the /trigger fire - ADR Blue 001 R6) plus the Twitch stream key;
# the viewer show-token is minted at runtime. A leak fails the run (exit 1)
# REGARDLESS of the probe's own exit code - redaction is a gate, not a log
# nicety.
#
# The credentials come from the etage-1 environment (M8_OPERATOR_TOKEN,
# TWITCH_STREAM_KEY). Reading the minted show-token back out of the probe to
# scan for it is not possible from here, so we instead assert the KNOWN
# secrets never appear, and that no raw `token=eyJ` / `token%3DeyJ`
# JWT-shaped substring survived in the log.
#
# Usage (from the repo root):
#   $env:M8_OPERATOR_TOKEN = "..."                # etage-1 admin short-TTL JWT
#   $env:M8_GATEWAY_URL    = "http://127.0.0.1:8099"
#   $env:TWITCH_STREAM_KEY = "..."                # etage-1 (required for the live run)
#   pwsh scripts/run-m9.ps1                        # LIVE: broadcast + trigger mid-stream (on air)
#   pwsh scripts/run-m9.ps1 -NoBroadcast          # prove the repaint without going live (no Twitch)
#
# DEFAULT = live, on air: the broadcast opens on the green A frame and the Blue
# /trigger fires at duration/2 so the green->magenta swap is visible on the
# stream + in the VOD (porteur preference). -NoBroadcast keeps the old
# prove-only mode for a CI run with no Twitch key.

[CmdletBinding()]
param(
    [switch] $NoBroadcast,
    [string] $GatewayUrl = $env:M8_GATEWAY_URL,
    [string] $ShowStreamPath = "stream.lsdp",
    [string] $SolarVersion = "0.2.0",
    [string] $PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$stdoutLog  = Join-Path $repoRoot "build\m9-probe-stdout.log"
$beforePng  = Join-Path $repoRoot "build\m9-before.png"
$afterPng   = Join-Path $repoRoot "build\m9-after.png"
$vodDir     = Join-Path $repoRoot "build\m9-canvas-vod"

New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot "build") | Out-Null

# --- Build the probe argument list ---------------------------------------
$Broadcast = -not $NoBroadcast
$probeArgs = @("scripts/probe-m9-canvas-live.py")
if ($NoBroadcast) { $probeArgs += "--no-broadcast" }
if ($GatewayUrl) { $probeArgs += @("--gateway-url", $GatewayUrl) }
$probeArgs += @("--show-stream-path", $ShowStreamPath)
$probeArgs += @("--solar-version", $SolarVersion)

# --- Pre-flight env sanity (fail fast, never echo the values) ------------
if (-not $env:M8_OPERATOR_TOKEN) {
    Write-Error "M8_OPERATOR_TOKEN is not set (etage-1 admin short-TTL JWT; drives SETUP + /trigger). Refusing to run."
}
if ($Broadcast -and -not $env:TWITCH_STREAM_KEY) {
    Write-Error "TWITCH_STREAM_KEY is not set (etage-1) and a LIVE run was requested (default). Refusing to run. Pass -NoBroadcast to prove the repaint without going live."
}

# --- Run the probe, tee stdout -------------------------------------------
Push-Location $repoRoot
try {
    Write-Host "[run-m9] launching probe (wire=$ShowStreamPath solar=v$SolarVersion broadcast=$Broadcast)"
    & $PythonExe @probeArgs 2>&1 | Tee-Object -FilePath $stdoutLog
    $probeExit = $LASTEXITCODE
} finally {
    Pop-Location
}

# --- Grep-assert: no credential in clear in stdout or artefacts -----------
$secrets = @()
if ($env:M8_OPERATOR_TOKEN) { $secrets += $env:M8_OPERATOR_TOKEN }
if ($env:TWITCH_STREAM_KEY) { $secrets += $env:TWITCH_STREAM_KEY }

$leak = $false

function Assert-NoSecret {
    param([string] $Path, [string[]] $Needles, [string] $Label)
    if (-not (Test-Path $Path)) { return $false }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $text  = [System.Text.Encoding]::UTF8.GetString($bytes)
    foreach ($n in $Needles) {
        if ($n -and $text.Contains($n)) {
            Write-Host "::error::SECRET LEAK - a credential appears in clear in $Label ($Path)"
            return $true
        }
    }
    return $false
}

# 1. The known secrets (operator JWT used on SETUP + /trigger, Twitch key)
#    must not appear anywhere.
$leak = (Assert-NoSecret -Path $stdoutLog -Needles $secrets -Label "probe stdout")      -or $leak
$leak = (Assert-NoSecret -Path $beforePng -Needles $secrets -Label "before proof PNG")  -or $leak
$leak = (Assert-NoSecret -Path $afterPng  -Needles $secrets -Label "after proof PNG")   -or $leak

# 2. Heuristic: the minted show-token is unknown to this wrapper, so assert
#    no un-redacted JWT-shaped token survived in the log - neither a plain
#    `token=eyJ...` nor a url-encoded `token%3DeyJ...`.
if (Test-Path $stdoutLog) {
    $log = Get-Content -Raw -Path $stdoutLog
    if ($log -match "token=eyJ" -or $log -match "token%3DeyJ") {
        Write-Host "::error::SECRET LEAK - an un-redacted show-token (token=eyJ / token%3DeyJ) survived in the probe stdout"
        $leak = $true
    }
}

# 3. Scan VOD artefacts too (defensive; only present when -Broadcast).
if (Test-Path $vodDir) {
    Get-ChildItem -Path $vodDir -File -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
        $leak = (Assert-NoSecret -Path $_.FullName -Needles $secrets -Label "VOD artefact") -or $leak
    }
}

if ($leak) {
    Write-Host "[run-m9] GREP-ASSERT FAILED - credential leak detected; failing the run regardless of probe exit ($probeExit)."
    exit 1
}

Write-Host "[run-m9] grep-assert clean - no credential leaked to stdout / PNGs / VOD."
Write-Host "[run-m9] probe exit code: $probeExit"
exit $probeExit
