# run-m10.ps1 — wrapper for the M10 scene-setup harness (scripts/m10_setup.py,
# ADR 003 §3.1 / §6.1, issue #60).
#
# m10_setup.py is SETUP-ONLY: it creates the two `monitor_capture` scenes
# (scene-screen-1 / scene-screen-2) over obs-websocket and validates the F2
# Orion scene-control declaration. It does NOT broadcast and — unless you pass
# -DeclareOrionScene — touches no etage-1 credential. The live, on-air test
# (Blue trigger → animated switch mid-broadcast) is #61, not this harness.
#
# This wrapper:
#   - launches the harness (spawning pulsar.exe from the built rundir by
#     default, or --connect to drive an already-running Prism-spawned one),
#   - tees stdout to a log,
#   - runs a defensive secret grep-assert on the log (parity with run-m8/run-m9):
#     if -DeclareOrionScene is used, the operator JWT (M8_OPERATOR_TOKEN) must
#     never appear in clear, and no JWT-shaped `token=eyJ` substring survives.
#     A leak fails the run (exit 1) regardless of the harness exit code.
#   - surfaces the harness's typed exit code:
#       0 = both scenes created + F2 declaration valid
#       2 = usage/env error (pulsar.exe missing, etc.)
#       3 = TYPED SKIP (monitor_capture not registered — broken/headless build)
#
# UTF-8 BOM is intentional: PowerShell 5.1 (Windows default) mis-decodes a
# BOM-less UTF-8 file containing non-ASCII (the em-dashes / § above), so the
# BOM is required for the comments to render — same fix as run-m8/run-m9.
#
# Usage (from the repo root):
#   pwsh scripts/run-m10.ps1                      # spawn pulsar.exe, create the 2 scenes
#   pwsh scripts/run-m10.ps1 -Connect             # drive an already-running pulsar.exe
#   $env:M8_OPERATOR_TOKEN = "..."                # etage-1 admin short-TTL JWT
#   $env:M8_GATEWAY_URL    = "http://127.0.0.1:8099"
#   pwsh scripts/run-m10.ps1 -DeclareOrionScene   # also push the F2 Orion declaration

[CmdletBinding()]
param(
    [switch] $Connect,
    [switch] $DeclareOrionScene,
    [string] $GatewayUrl = $env:M8_GATEWAY_URL,
    [string] $PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$repoRoot  = Split-Path -Parent $PSScriptRoot
$stdoutLog = Join-Path $repoRoot "build\m10-setup-stdout.log"

New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot "build") | Out-Null

# --- Build the harness argument list -------------------------------------
$harnessArgs = @("scripts/m10_setup.py")
if ($Connect)           { $harnessArgs += "--connect" }
if ($DeclareOrionScene) {
    $harnessArgs += "--declare-orion-scene"
    if ($GatewayUrl) { $harnessArgs += @("--gateway-url", $GatewayUrl) }
    if (-not $env:M8_OPERATOR_TOKEN) {
        Write-Error "M8_OPERATOR_TOKEN is not set (etage-1 admin short-TTL JWT) and -DeclareOrionScene was requested. Refusing to run."
    }
}

# --- Run the harness, tee stdout -----------------------------------------
Push-Location $repoRoot
try {
    Write-Host "[run-m10] launching harness (connect=$Connect declareOrion=$DeclareOrionScene)"
    & $PythonExe @harnessArgs 2>&1 | Tee-Object -FilePath $stdoutLog
    $harnessExit = $LASTEXITCODE
} finally {
    Pop-Location
}

# --- Grep-assert: no credential in clear in stdout -----------------------
$secrets = @()
if ($env:M8_OPERATOR_TOKEN) { $secrets += $env:M8_OPERATOR_TOKEN }

$leak = $false
if (Test-Path $stdoutLog) {
    $bytes = [System.IO.File]::ReadAllBytes($stdoutLog)
    $text  = [System.Text.Encoding]::UTF8.GetString($bytes)
    foreach ($n in $secrets) {
        if ($n -and $text.Contains($n)) {
            Write-Host "::error::SECRET LEAK - a credential appears in clear in the harness stdout ($stdoutLog)"
            $leak = $true
        }
    }
    if ($text -match "token=eyJ" -or $text -match "token%3DeyJ") {
        Write-Host "::error::SECRET LEAK - an un-redacted JWT-shaped token (token=eyJ / token%3DeyJ) survived in the harness stdout"
        $leak = $true
    }
}

if ($leak) {
    Write-Host "[run-m10] GREP-ASSERT FAILED - credential leak detected; failing the run regardless of harness exit ($harnessExit)."
    exit 1
}

Write-Host "[run-m10] grep-assert clean - no credential leaked to stdout."
Write-Host "[run-m10] harness exit code: $harnessExit"
exit $harnessExit
