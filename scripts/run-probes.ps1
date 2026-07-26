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
# Phase 1d -- self-spawning FLAG-AWARE stinger smoke (probe-stinger-
# smoke.py, M10 #79 / pivot).
#
# With PULSAR_NATIVE_STINGER unset (the DEFAULT, the Solar/CEF pivot
# world, #73/#83) the probe asserts the native stinger is DORMANT: NO
# "Stinger" transition instance is registered, and a program-scene
# change is an instantaneous HARD-CUT that does NOT error and does NOT
# blank the encoder (record stays active + outputTotalFrames grows
# across the switch window -- activeFps is NOT a liveness signal on the
# record-only encoder, handoff #73). The CTest runs this default world.
# Set PULSAR_NATIVE_STINGER=1 locally to assert the dormant #57 native
# path instead. Self-spawns its OWN pulsar.exe child (fresh ephemeral
# port) for the config.json-reseed reason as the other Phase-1 probes.
# The VISUAL overlay-blend + invisible-cut proof is the M10 live probe
# (#79), not this smoke. A LIGHT build -> exit 3 (typed skip) only in
# the NATIVE_STINGER=1 world; the default hard-cut world runs on any
# full build.
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

# --------------------------------------------------------------------
# Phase 1d-bis -- self-spawning REPLAY BUFFER probe (probe-replay.py,
# issue #117 / ADR Prism 024 §3.1).
#
# Asserts the replay buffer is wired, not scaffolding: an off-air arm is
# refused AND logged (no encoder started), then StartRecord brings the
# shared encoders up, StartReplayBuffer really flips
# GetReplayBufferStatus.outputActive to true, SaveReplayBuffer produces
# a readable h264+aac MP4, and GetLastReplayBufferReplay returns its
# REAL path (it used to return "" forever). Self-spawns its OWN
# pulsar.exe child (fresh ephemeral port + isolated PULSAR_RECORD_DIR)
# for the same config.json-reseed reason as the other Phase-1 probes.
# Encoder-agnostic, so it runs on a light build too.
# --------------------------------------------------------------------
$replayProbe = Join-Path $repoRoot "scripts/probe-replay.py"
Write-Host "==> Running probe-replay.py (self-spawn replay buffer, #117)"
& python $replayProbe --exe $pulsar
$replayCode = $LASTEXITCODE
if ($replayCode -ne 0) {
    Write-Host "==> probe-replay.py FAILED (exit $replayCode)"
    Write-Host "==> The replay buffer did not arm/save a real file -- aborting before the shared suite."
    exit 1
}
Write-Host "==> probe-replay.py OK"

# --------------------------------------------------------------------
# Phase 1e -- self-spawning M10 OVERLAY live end-to-end probe in
# PROOF-ONLY mode (probe-m10-canvas-live.py, #79 / Solar-CEF pivot).
#
# Runs the FULL M10 overlay chain WITHOUT going live to Twitch and WITHOUT
# the VPS:
#   --no-broadcast    no Twitch key, no StartDestination (the on-air leg is
#                     Keeper's antenna run).
#   --loopback-leaf   injects the exact scene_control leaf Orion would fan out
#                     into BOTH the in-process stand-in cut consumer AND the
#                     overlay page (validate -> hard-cut -> capture + overlay
#                     blend + cut-skew), so the integration is proven on a box
#                     with no VPS reach.
#   --allow-blank     a CI runner may not render WGC monitor_capture / the
#                     Solar CEF overlay, so the visual blend + real-opacity
#                     skew can't be asserted; the wire + the hard-cut + C-MECH
#                     (no native transition) + C-INJ + C-FANOUT(F2) + C-SEC are
#                     ALL still asserted. The VISUAL overlay-blend + invisible-
#                     cut skew proof needs a real desktop + the Solar bundle --
#                     that is the antenna run (#81), by doctrine.
# Self-spawns + reaps its own pulsar.exe (config.json-reseed reason) so it runs
# in Phase 1. pulsar.exe is launched GPU-ON (no --disable-gpu) so WGC + CEF
# coexist (SPIKE-GPU #70). A LIGHT build (no browser_source/monitor_capture)
# -> exit 3, tolerated.
# --------------------------------------------------------------------
$m10Probe = Join-Path $repoRoot "scripts/probe-m10-canvas-live.py"
Write-Host "==> Running probe-m10-canvas-live.py (self-spawn M10 e2e, proof-only, #61)"
& python $m10Probe --exe $pulsar --no-broadcast --loopback-leaf --allow-blank --duration 12
$m10Code = $LASTEXITCODE
if ($m10Code -eq 3) {
    Write-Host "==> probe-m10-canvas-live.py SKIPPED (light build -- stinger/monitor_capture absent)"
} elseif ($m10Code -ne 0) {
    Write-Host "==> probe-m10-canvas-live.py FAILED (exit $m10Code)"
    Write-Host "==> The M10 Blue->leaf->consumer->switch chain did not wire/compose cleanly -- aborting before the shared suite."
    exit 1
} else {
    Write-Host "==> probe-m10-canvas-live.py OK"
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

# --------------------------------------------------------------------
# Phase 2a -- scene-source name-drift regression (probe-scene-name-drift.py,
# #110). Connect-only: drives pulsar-scene:SetCaptureSource N times against
# the shared instance and asserts the program scene keeps EXACTLY ONE
# Pulsar-managed browser_source (canonically named) at every step -- no
# stale "PulsarSceneSource 2" accumulation. Runs BEFORE the $probes loop so
# its exit-3 (light build: obs-browser absent) can be tolerated explicitly,
# same pattern as the Phase-1 skip-aware probes. On a -Full build (CI) it
# asserts for real. It leaves one managed browser_source on the program
# scene; harmless to the record/adaptive probes that follow (dead URL, no
# paint).
# --------------------------------------------------------------------
$nameDriftProbe = Join-Path $repoRoot "scripts/probe-scene-name-drift.py"
Write-Host "==> Running probe-scene-name-drift.py (connect-only, #110)"
& python $nameDriftProbe
$nameDriftCode = $LASTEXITCODE
if ($nameDriftCode -eq 3) {
    Write-Host "==> probe-scene-name-drift.py SKIPPED (light build -- browser_source absent)"
} elseif ($nameDriftCode -ne 0) {
    Write-Host "==> probe-scene-name-drift.py FAILED (exit $nameDriftCode)"
    Write-Host "==> Stopping pulsar.exe"
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force; $proc.WaitForExit(5000) | Out-Null }
    exit 1
} else {
    Write-Host "==> probe-scene-name-drift.py OK"
}

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
