# run-probes.ps1 -- offline probe orchestrator.
#
# Spawns pulsar.exe with cwd=bin/64bit, waits for the PULSAR_READY
# sentinel, then runs each offline probe sequentially against it.
# Exit 0 = every probe passed. Non-zero = exit code of the first
# probe that failed.
#
# Used by:
#   - CTest (top-level CMakeLists.txt -- `add_test(NAME probes ...)`)
#   - build.yml CI workflow (called via ctest)
#   - Local development (just run it directly)
#
# This is the integration-test layer for V1. The probes themselves
# live in scripts/probe-*.py and are deliberately self-contained
# (each is the source of truth for what its assertions are).
#
# Excluded from this orchestrator:
#   - probe-twitch-live.py -- needs a real Twitch stream key + 5min
#     of network bandwidth; runs from live-test.yml on tag push only.

param(
    [string] $RundirRoot = "upstream/build_x64/rundir/RelWithDebInfo",
    [int]    $ReadyTimeoutSec = 60
)

$ErrorActionPreference = 'Stop'

$repoRoot = Resolve-Path "$PSScriptRoot/.."
$binDir   = Join-Path $repoRoot "$RundirRoot/bin/64bit"
$pulsar   = Join-Path $binDir "pulsar.exe"

if (-not (Test-Path $pulsar)) {
    Write-Error "pulsar.exe not found at $pulsar -- build it first via scripts/build-win.ps1"
    exit 1
}

# Each test run gets its own session credentials so an existing
# obs-websocket/config.json from a prior session never leaks in.
# Port 0 -> bind a random free port so back-to-back ctest runs don't
# collide on TIME_WAIT'd 4455 from a prior live-probe run.
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$sessionPort = $listener.LocalEndpoint.Port
$listener.Stop()
$sessionPwd  = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 22 | ForEach-Object { [char]$_ })

# Wipe any stale config from a prior run so the seeded one is what
# obs-websocket sees on first load.
$configDir = Join-Path $binDir "obs-websocket"
if (Test-Path "$configDir/config.json") {
    Remove-Item "$configDir/config.json" -Force
}

# Drop any leftover environment overrides from the parent shell so
# we control what pulsar sees end-to-end.
$env:PULSAR_PORT     = "$sessionPort"
$env:PULSAR_PASSWORD = $sessionPwd
$env:PULSAR_MIC_DEVICE_ID = $null
$env:PULSAR_CAPTURE_WINDOW = $null

$stdoutLog = Join-Path $repoRoot "build/probe-pulsar-stdout.log"
$stderrLog = Join-Path $repoRoot "build/probe-pulsar-stderr.log"
New-Item -ItemType Directory -Path (Split-Path $stdoutLog) -Force | Out-Null

Write-Host "==> Spawning pulsar.exe (cwd=$binDir, port=$sessionPort)"
# pulsar.exe is built /SUBSYSTEM:WINDOWS so spawning it never allocates
# a console window, regardless of redirection. Start-Process with
# -RedirectStandard* drains stdio at OS level (file redirection),
# avoiding the userland pipe-buffer drain hazard you hit with
# ProcessStartInfo + async readers under PS 5.1.
$proc = Start-Process -FilePath $pulsar `
    -WorkingDirectory $binDir `
    -PassThru `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError  $stderrLog

# Wait until PULSAR_READY appears in stdout, or timeout.
$ready = $false
$readyDeadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
while ((Get-Date) -lt $readyDeadline) {
    if (Test-Path $stdoutLog) {
        $hit = Select-String -Path $stdoutLog -Pattern '^PULSAR_READY ' -SimpleMatch:$false -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { $ready = $true; break }
    }
    if ($proc.HasExited) { break }
    Start-Sleep -Milliseconds 250
}

if (-not $ready) {
    Write-Host "==> Pulsar failed to reach READY within ${ReadyTimeoutSec}s. Tail of stdout:"
    Get-Content $stdoutLog -Tail 30 -ErrorAction SilentlyContinue
    Write-Host "==> stderr:"
    Get-Content $stderrLog -Tail 30 -ErrorAction SilentlyContinue
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    exit 1
}
Write-Host "==> PULSAR_READY received"

$probes = @(
    'probe-websocket.py',
    'probe-source-kinds.py',
    'probe-events.py',
    'probe-multi-stream.py',
    'probe-adaptive.py',
    'probe-record.py'
)

$failed = @()
foreach ($p in $probes) {
    $script = Join-Path $repoRoot "scripts/$p"
    Write-Host ""
    Write-Host "==> Running $p"
    & python $script
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Host "==> $p FAILED (exit $code)"
        $failed += $p
    } else {
        Write-Host "==> $p OK"
    }
}

Write-Host ""
Write-Host "==> Stopping pulsar.exe"
if (-not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force
    $proc.WaitForExit(5000) | Out-Null
}

if ($failed.Count -gt 0) {
    Write-Host "==> $($failed.Count) probe(s) failed: $($failed -join ', ')"
    Write-Host "==> Pulsar stdout tail:"
    Get-Content $stdoutLog -Tail 50 -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "==> All probes passed"
exit 0
