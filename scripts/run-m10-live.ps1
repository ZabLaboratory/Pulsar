# run-m10-live.ps1 -- wrapper for the M10 live end-to-end probe
# (scripts/probe-m10-canvas-live.py, ADR 003 SS3.2-3.4 / SS6 + Amendment 1/2,
# issue #61).
#
# This is the ON-AIR test. By default it fires the REAL VPS Blue trigger
# (--live-wire) and broadcasts to Twitch. It is the run KEEPER executes at the
# antenna, with the porteur's go-ahead -- NOT the dry-run Forge runs.
#
# Two intended invocations:
#
#   1) Forge / CI dry-run (NO Twitch, NO VPS) -- the integration proof:
#        pwsh scripts/run-m10-live.ps1 -NoBroadcast -LoopbackLeaf
#      Spawns pulsar.exe, creates the two monitor_capture scenes, runs the
#      stand-in #63 consumer, injects the scene_control leaf, proves the
#      stinger switch screen-1 -> screen-2 OFF AIR (ordering / criterion 3-4 /
#      C-INJ / C-FANOUT(F2) / C-SEC). The visual blend (criterion 5) needs a
#      2-monitor desktop with real content -- that is the antenna run.
#
#   2) Keeper antenna run (REAL VPS trigger + Twitch) -- the on-air proof:
#        $env:TWITCH_STREAM_KEY = "..."     # etage-1 secret, never committed
#        $env:M8_OPERATOR_TOKEN = "..."     # etage-1 operator/admin JWT
#        $env:M8_GATEWAY_URL    = "https://zabgate.cyell.dev"
#        $env:M10_BLUEPRINT_ID  = "..."     # the scene-control blueprint on Blue
#        $env:M10_SHOW_TOKEN    = "..."     # viewer show-token (read-only)
#        pwsh scripts/run-m10-live.ps1 -LiveWire
#      PRECONDITION (F2 / C-FANOUT): the M10 Orion declaration scene
#      (scripts/fixtures/m10-orion-scene.lsml.json, declaring
#      __inputs.blue.m10-scene-control.scene_control) MUST be pushed ACTIVE on
#      Orion first, or Orion silent-drops the Blue delta and it never reaches
#      the consumer. Push it via the m9 authoring toolkit / Conduit-Keeper
#      wiring (#53); the probe does NOT author the Orion scene (out of probe
#      scope). If the delta never arrives, the probe times out with an explicit
#      F2-silent-drop diagnostic.
#
# This wrapper tees stdout to a log and runs a defensive secret grep-assert
# (parity with run-m10.ps1): the stream key, operator JWT and show-token must
# never appear in clear, and no JWT-shaped `token=eyJ` substring may survive.
# A leak fails the run (exit 1) regardless of the probe exit code.
#
# UTF-8 BOM is intentional: PowerShell 5.1 (Windows default) mis-decodes a
# BOM-less UTF-8 file with non-ASCII; the BOM keeps comments rendering -- same
# fix as run-m8/run-m9/run-m10.
#
# Probe exit codes surfaced verbatim:
#   0 = chain proven (switch fired + asserted)
#   1 = assertion/integration failure (e.g. F2 silent-drop, switch not honoured)
#   2 = usage/env error (pulsar.exe missing, no key when broadcasting, bad args)
#   3 = TYPED SKIP (stinger/monitor_capture absent -- not a full build)

[CmdletBinding()]
param(
    [switch] $NoBroadcast,
    [switch] $LoopbackLeaf,
    [switch] $LiveWire,
    [switch] $AllowBlank,
    [int]    $Duration = 30,
    [string] $PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$repoRoot  = Split-Path -Parent $PSScriptRoot
$stdoutLog = Join-Path $repoRoot "build\m10-live-stdout.log"
New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot "build") | Out-Null

# --- Build the probe argument list ---------------------------------------
$probeArgs = @("scripts/probe-m10-canvas-live.py", "--duration", "$Duration")
if ($NoBroadcast) { $probeArgs += "--no-broadcast" }
if ($AllowBlank)  { $probeArgs += "--allow-blank" }
if ($LoopbackLeaf -and $LiveWire) {
    Write-Error "Pass at most ONE of -LoopbackLeaf / -LiveWire."
}
if ($LoopbackLeaf) { $probeArgs += "--loopback-leaf" }
if ($LiveWire)     { $probeArgs += "--live-wire" }

# Guard: a live broadcast (no -NoBroadcast) needs the Twitch key.
if (-not $NoBroadcast -and -not $env:TWITCH_STREAM_KEY) {
    Write-Error "TWITCH_STREAM_KEY is not set and -NoBroadcast was not passed. This wrapper goes ON AIR by default; set the etage-1 key or pass -NoBroadcast for the dry-run."
}
# Guard: --live-wire needs the VPS credentials.
if ($LiveWire) {
    foreach ($v in @("M8_GATEWAY_URL","M8_OPERATOR_TOKEN","M10_BLUEPRINT_ID","M10_SHOW_TOKEN")) {
        if (-not (Get-Item "env:$v" -ErrorAction SilentlyContinue)) {
            Write-Error "-LiveWire requires env $v (etage-1). Set it or use -LoopbackLeaf for the VPS-less proof."
        }
    }
}

# --- Run the probe, tee stdout -------------------------------------------
Push-Location $repoRoot
try {
    Write-Host "[run-m10-live] launching probe (noBroadcast=$NoBroadcast loopback=$LoopbackLeaf liveWire=$LiveWire duration=$Duration)"
    & $PythonExe @probeArgs 2>&1 | Tee-Object -FilePath $stdoutLog
    $probeExit = $LASTEXITCODE
} finally {
    Pop-Location
}

# --- Grep-assert: no credential in clear in stdout -----------------------
$secrets = @()
foreach ($v in @("TWITCH_STREAM_KEY","M8_OPERATOR_TOKEN","M10_SHOW_TOKEN")) {
    $val = (Get-Item "env:$v" -ErrorAction SilentlyContinue).Value
    if ($val) { $secrets += $val }
}

$leak = $false
if (Test-Path $stdoutLog) {
    $bytes = [System.IO.File]::ReadAllBytes($stdoutLog)
    $text  = [System.Text.Encoding]::UTF8.GetString($bytes)
    foreach ($n in $secrets) {
        if ($n -and $text.Contains($n)) {
            Write-Host "::error::SECRET LEAK - a credential appears in clear in the probe stdout ($stdoutLog)"
            $leak = $true
        }
    }
    if ($text -match "token=eyJ" -or $text -match "token%3DeyJ") {
        Write-Host "::error::SECRET LEAK - an un-redacted JWT-shaped token (token=eyJ / token%3DeyJ) survived in the probe stdout"
        $leak = $true
    }
}

if ($leak) {
    Write-Host "[run-m10-live] GREP-ASSERT FAILED - credential leak detected; failing the run regardless of probe exit ($probeExit)."
    exit 1
}

Write-Host "[run-m10-live] grep-assert clean - no credential leaked to stdout."
Write-Host "[run-m10-live] probe exit code: $probeExit"
exit $probeExit
