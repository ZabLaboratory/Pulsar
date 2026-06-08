# run-m8.ps1 — wrapper for the M8 Canvas-authored live probe with a hard
# secret grep-assert (Bastion PV-1 / CC-1, ADR Pulsar-002 §A1.5 criterion 7).
#
# It runs scripts/probe-m8-canvas-live.py, tees stdout to a log, then
# grep-asserts that NONE of the three credentials (the operator JWT, the
# minted show-token, the Twitch stream key) appear in clear in the captured
# stdout OR in the produced proof PNG / VOD artefacts. A leak fails the run
# (exit 1) REGARDLESS of the probe's own exit code — redaction is not a
# best-effort log nicety here, it is a gate.
#
# The credentials come from the étage-1 environment (M8_OPERATOR_TOKEN,
# TWITCH_STREAM_KEY); the show-token is minted at runtime, so we scan for it
# by reading it back out of the probe's own redaction is NOT possible — we
# instead assert the KNOWN secrets never appear, and that no raw
# `?token=<jwt-looking>` or `token%3DeyJ` substring survives in the log.
#
# Usage (from the repo root):
#   $env:M8_OPERATOR_TOKEN = (Get-Content ..\.env.m8 | ...)   # étage-1
#   $env:TWITCH_STREAM_KEY = "..."                            # étage-1
#   $env:M8_GATEWAY_URL    = "http://127.0.0.1:8099"
#   pwsh scripts/run-m8.ps1 -PreflightOnly
#   pwsh scripts/run-m8.ps1                                    # + broadcast

[CmdletBinding()]
param(
    [switch] $PreflightOnly,
    [string] $GatewayUrl = $env:M8_GATEWAY_URL,
    [string] $ShowStreamPath = "stream.lsdp",
    [string] $SolarVersion = "0.2.0",
    [string] $PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$stdoutLog = Join-Path $repoRoot "build\m8-probe-stdout.log"
$proofPng  = Join-Path $repoRoot "build\m8-canvas-scene.png"
$vodDir    = Join-Path $repoRoot "build\m8-canvas-vod"

New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot "build") | Out-Null

# --- Build the probe argument list ---------------------------------------
$probeArgs = @("scripts/probe-m8-canvas-live.py")
if ($PreflightOnly) { $probeArgs += "--preflight-only" }
if ($GatewayUrl)    { $probeArgs += @("--gateway-url", $GatewayUrl) }
$probeArgs += @("--show-stream-path", $ShowStreamPath)
$probeArgs += @("--solar-version", $SolarVersion)

# --- Pre-flight env sanity (fail fast, never echo the values) ------------
if (-not $env:M8_OPERATOR_TOKEN) {
    Write-Error "M8_OPERATOR_TOKEN is not set (étage-1 admin short-TTL JWT). Refusing to run."
}
if (-not $PreflightOnly -and -not $env:TWITCH_STREAM_KEY) {
    Write-Error "TWITCH_STREAM_KEY is not set (étage-1) and not --PreflightOnly. Refusing to run."
}

# --- Run the probe, tee stdout -------------------------------------------
Push-Location $repoRoot
try {
    Write-Host "[run-m8] launching probe (wire=$ShowStreamPath solar=v$SolarVersion preflight-only=$PreflightOnly)"
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
            Write-Host "::error::SECRET LEAK — a credential appears in clear in $Label ($Path)"
            return $true
        }
    }
    return $false
}

# 1. The known secrets (operator JWT, Twitch key) must not appear anywhere.
$leak = (Assert-NoSecret -Path $stdoutLog -Needles $secrets -Label "probe stdout") -or $leak
$leak = (Assert-NoSecret -Path $proofPng  -Needles $secrets -Label "proof PNG")    -or $leak

# 2. Heuristic: the minted show-token is unknown to this wrapper, so assert
#    no un-redacted JWT-shaped token survived in the log — neither a plain
#    `token=eyJ...` nor a url-encoded `token%3DeyJ...`. Redaction replaces
#    both with `<redacted>`, so any surviving `eyJ` after `token` is a leak.
if (Test-Path $stdoutLog) {
    $log = Get-Content -Raw -Path $stdoutLog
    if ($log -match "token=eyJ" -or $log -match "token%3DeyJ") {
        Write-Host "::error::SECRET LEAK — an un-redacted show-token (token=eyJ / token%3DeyJ) survived in the probe stdout"
        $leak = $true
    }
}

# 3. Scan VOD artefacts too (defensive; the MP4 is media, not text, but a
#    container metadata leak would surface as a substring).
if (Test-Path $vodDir) {
    Get-ChildItem -Path $vodDir -File -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
        $leak = (Assert-NoSecret -Path $_.FullName -Needles $secrets -Label "VOD artefact") -or $leak
    }
}

if ($leak) {
    Write-Host "[run-m8] GREP-ASSERT FAILED — credential leak detected; failing the run regardless of probe exit ($probeExit)."
    exit 1
}

Write-Host "[run-m8] grep-assert clean — no credential leaked to stdout / PNG / VOD."
Write-Host "[run-m8] probe exit code: $probeExit"
exit $probeExit
