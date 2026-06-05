# run-probes.ps1 -- offline probe orchestrator.
#
# Two phases:
#   1. The self-spawning smoke probe (probe-websocket.py, M1) runs
#      STANDALONE first. It spawns and reaps its OWN pulsar.exe child
#      to prove the freshly-built binary boots end to end. It is NOT
#      run against the shared instance because its child re-seeds
#      bin/64bit/obs-websocket/config.json with that child's ephemeral
#      port/password (pulsar-headless/main.cpp seed_websocket_config),
#      then dies -- which would leave config.json pointing at a dead
#      port for every connect-only probe that follows.
#   2. AFTER the smoke probe, a SINGLE shared pulsar.exe is spawned.
#      Its boot re-seeds config.json with the session port/password
#      below, so the connect-only probes (which read config.json) all
#      connect to the live shared instance. They run sequentially
#      against it.
#
# Exit 0 = every probe passed. Non-zero = the first probe that failed.
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

# --------------------------------------------------------------------
# Phase 1 -- self-spawning smoke probe (probe-websocket.py, M1).
#
# This probe is self-contained: it spawns its OWN pulsar.exe child
# (fresh ephemeral port + password) to prove the binary boots end to
# end, then reaps it. Because that child runs seed_websocket_config()
# (pulsar-headless/main.cpp), it rewrites bin/64bit/obs-websocket/
# config.json to ITS port/password and then dies -- so it must run
# BEFORE the shared instance is spawned. The shared instance's boot
# (Phase 2) is the last writer of config.json, leaving it pointing at
# the live connect-only target.
#
# Run it standalone here, NOT inside the shared $probes loop.
# --------------------------------------------------------------------
$smokeProbe = Join-Path $repoRoot "scripts/probe-websocket.py"
Write-Host "==> Running probe-websocket.py (self-spawn smoke)"
& python $smokeProbe --exe $pulsar
$smokeCode = $LASTEXITCODE
if ($smokeCode -ne 0) {
    Write-Host "==> probe-websocket.py FAILED (exit $smokeCode)"
    Write-Host "==> The freshly-built pulsar.exe did not boot cleanly -- aborting before the shared suite."
    exit 1
}
Write-Host "==> probe-websocket.py OK"

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

# --------------------------------------------------------------------
# Phase 2 -- shared instance for the connect-only probes.
# --------------------------------------------------------------------
Write-Host "==> Spawning shared pulsar.exe (cwd=$binDir, port=$sessionPort)"
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

# Connect-only probes. Each reads the shared instance's port/password
# from bin/64bit/obs-websocket/config.json (seeded by the Phase 2 boot
# above) and connects to the single live pulsar.exe.
#
# probe-websocket.py is NOT here -- it is the self-spawning smoke probe
# and runs standalone in Phase 1 (see top of file). Adding it back here
# would re-poison config.json mid-suite and break every probe after it.
$probes = @(
    'probe-source-kinds.py',
    'probe-events.py',
    # probe-multi-stream.py is INTENTIONALLY excluded.
    # The destination lifecycle has known race-condition crash paths
    # in obs upstream (rtmp_output worker thread vs ECONNREFUSED-fast
    # path, ffmpeg_muxer flush vs Stop, service-ref vs worker exit
    # ordering) which surface ~30 % of the time on the windows-2022
    # CI runner and bring pulsar.exe down -- killing every subsequent
    # probe in the suite. The pulsar:CallVendorRequest multi-stream
    # API contract is exercised by `live broadcast (Twitch)` against a
    # real ingest, which is the gold-standard validation path. Offline
    # multi-stream coverage is restored when the upstream-obs races
    # are fixed.
    # TODO(upstream-obs) : audit rtmp_output worker_thread vs
    # obs_output_signal_stop ordering on ECONNREFUSED ; audit
    # ffmpeg_muxer Stop flush path ; submit fixes to obs-studio.
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
