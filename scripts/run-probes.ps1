# run-probes.ps1 -- offline probe orchestrator.
#
# Two phases:
#   1. The self-spawning probes run STANDALONE first, each spawning and
#      reaping its OWN pulsar.exe child:
#        - probe-websocket.py  (M1) -- binary boots + v5 GetVersion.
#        - probe-record-m2.py  (M2) -- scene+source driven over WS to a
#          verified MP4 (h264 + aac + duration > 0).
#        - probe-browser-m3.py (M3) -- CEF browser_source renders a local
#          page; the frame is captured + verified non-blank.
#      They are NOT run against the shared instance because each child
#      re-seeds bin/64bit/obs-websocket/config.json with that child's
#      ephemeral port/password (pulsar-headless/main.cpp
#      seed_websocket_config), then dies -- which would leave config.json
#      pointing at a dead port for every connect-only probe that follows.
#   2. AFTER the self-spawning probes, a SINGLE shared pulsar.exe is
#      spawned. Its boot re-seeds config.json with the session
#      port/password below, so the connect-only probes (which read
#      config.json) all connect to the live shared instance. They run
#      sequentially against it.
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
Write-Host "==> Running probe-websocket.py (self-spawn smoke, M1)"
& python $smokeProbe --exe $pulsar
$smokeCode = $LASTEXITCODE
if ($smokeCode -ne 0) {
    Write-Host "==> probe-websocket.py FAILED (exit $smokeCode)"
    Write-Host "==> The freshly-built pulsar.exe did not boot cleanly -- aborting before the shared suite."
    exit 1
}
Write-Host "==> probe-websocket.py OK"

# --------------------------------------------------------------------
# Phase 1b -- self-spawning media-output probe (probe-record-m2.py, M2).
#
# Drives a scene + synthetic source over WS and records it to a verified
# MP4 (h264 + aac + duration > 0). Self-spawns its OWN pulsar.exe child
# (fresh ephemeral port + password, isolated PULSAR_RECORD_DIR temp dir)
# for the same config.json-reseed reason as M1 -- so it must run before
# the shared instance is spawned. It cleans up its temp MP4 and reaps
# its child. Uses the v5 StartRecord path (CI-stable), NOT the vod_local
# rtmp destination lifecycle that keeps probe-multi-stream.py out of the
# shared suite.
# --------------------------------------------------------------------
$recordProbe = Join-Path $repoRoot "scripts/probe-record-m2.py"
Write-Host "==> Running probe-record-m2.py (self-spawn media output, M2)"
& python $recordProbe --exe $pulsar
$recordCode = $LASTEXITCODE
if ($recordCode -ne 0) {
    Write-Host "==> probe-record-m2.py FAILED (exit $recordCode)"
    Write-Host "==> The binary could not be driven to produce a valid MP4 -- aborting before the shared suite."
    exit 1
}
Write-Host "==> probe-record-m2.py OK"

# --------------------------------------------------------------------
# Phase 1c -- self-spawning CEF browser-source capture probe
# (probe-browser-m3.py, M3).
#
# Serves a local HTML page from a throwaway http.server, drives a
# browser_source pointed at it over WS, lets CEF paint, then captures
# the frame via GetSourceScreenshot and asserts it is non-blank +
# carries real content. Self-spawns its OWN pulsar.exe child (fresh
# ephemeral port + password) for the same config.json-reseed reason as
# the smoke probe -- so it must run before the shared instance is
# spawned. It reaps its child and stops its http.server.
#
# A LIGHT build (no CEF) makes browser_source absent; the probe exits 3
# (typed skip) which we tolerate here so the offline suite still passes
# on a light build. release.yml / live-test.yml build -Full where CEF
# is present and M3 asserts for real.
# --------------------------------------------------------------------
$browserProbe = Join-Path $repoRoot "scripts/probe-browser-m3.py"
Write-Host "==> Running probe-browser-m3.py (self-spawn CEF capture, M3)"
& python $browserProbe --exe $pulsar
$browserCode = $LASTEXITCODE
if ($browserCode -eq 3) {
    Write-Host "==> probe-browser-m3.py SKIPPED (light build -- browser_source absent, no CEF)"
} elseif ($browserCode -ne 0) {
    Write-Host "==> probe-browser-m3.py FAILED (exit $browserCode)"
    Write-Host "==> CEF could not render + capture a page -- aborting before the shared suite."
    exit 1
} else {
    Write-Host "==> probe-browser-m3.py OK"
}

# --------------------------------------------------------------------
# Phase 1d -- self-spawning stinger transition smoke (probe-stinger-
# smoke.py, M10 #57).
#
# Proves the Gap B' fix loads on the full build: the frontend-stub
# registers a "Stinger" transition (obs_stinger_transition), the obs-ws
# transition-config requests are accepted, and a program-scene change
# with the stinger active does NOT error and does NOT blank the encoder
# (record stays active, activeFps>0, drop ratio low across the switch).
# Self-spawns its OWN pulsar.exe child (pins PULSAR_STINGER_ASSET to the
# committed demo asset, fresh ephemeral port) for the config.json-reseed
# reason as the other Phase-1 probes -- so it runs before the shared
# instance. The full VISUAL mid-transition proof is the M10 live probe
# (#61), not this smoke. A LIGHT build (no obs_stinger_transition kind)
# makes the probe exit 3 (typed skip), tolerated here.
# --------------------------------------------------------------------
$stingerProbe = Join-Path $repoRoot "scripts/probe-stinger-smoke.py"
Write-Host "==> Running probe-stinger-smoke.py (self-spawn stinger seam, M10 #57)"
& python $stingerProbe --exe $pulsar
$stingerCode = $LASTEXITCODE
if ($stingerCode -eq 3) {
    Write-Host "==> probe-stinger-smoke.py SKIPPED (light build -- obs_stinger_transition absent)"
} elseif ($stingerCode -ne 0) {
    Write-Host "==> probe-stinger-smoke.py FAILED (exit $stingerCode)"
    Write-Host "==> The stinger transition seam did not load/compose cleanly -- aborting before the shared suite."
    exit 1
} else {
    Write-Host "==> probe-stinger-smoke.py OK"
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
