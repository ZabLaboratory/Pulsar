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
# Server-death detector (#128 follow-up).
#
# A connect-only probe that finds the shared instance gone reports a
# CLIENT-side symptom and nothing else: `websockets` raises
# ConnectionClosedError ("no close frame received or sent") for the probe
# that was mid-session when the server vanished, then
# ConnectionRefusedError ([WinError 1225]) for every probe queued behind
# it. Read literally, that reads like a network flake and invites a blind
# retry. It is not a network flake -- it means pulsar.exe DIED, and the
# probes after it prove nothing at all.
#
# Until this check existed, the suite printed only the stdout tail, and
# nothing said whether the process was still alive -- so a server crash
# and a genuine probe assertion failure looked identical in the CI log.
# libobs writes its own log to STDERR, which was never surfaced: the
# crash context was captured on disk and thrown away. Both are dumped
# here, and the exit code of the process is named.
#
# This does NOT retry and does NOT tolerate: it converts a misleading
# failure into a named one, and stops the suite at the first probe that
# runs against a corpse.
# --------------------------------------------------------------------

# Every dump of the pulsar logs goes through here. pulsar-headless prints
# `PULSAR_READY ws=... password=<session>` on its first stdout line
# (plugins/pulsar-headless/main.cpp) and mirrors it into config.json, so a
# short log -- exactly the case when it died early -- puts an ephemeral
# session credential in the CI transcript. It dies with the runner, but it
# has no business being printed: mask it at the only choke point.
function Show-LogTail {
    param(
        [Parameter(Mandatory = $true)] [string] $Path,
        [int] $Tail = 60
    )
    Get-Content $Path -Tail $Tail -ErrorAction SilentlyContinue |
        ForEach-Object {
            ($_ -replace 'password=\S+', 'password=<redacted>') `
                -replace '("server_password"\s*:\s*)"[^"]*"', '$1"<redacted>"'
        }
}

function Show-PulsarDeath {
    param(
        [Parameter(Mandatory = $true)] $Proc,
        [string] $LastAliveProbe,
        [Parameter(Mandatory = $true)] [string] $StdoutLog,
        [Parameter(Mandatory = $true)] [string] $StderrLog
    )
    $code = 'unknown'
    try { $code = $Proc.ExitCode } catch { }
    Write-Host ""
    Write-Host "==> FATAL: the shared pulsar.exe DIED (pid $($Proc.Id), exit code $code)."
    if ($LastAliveProbe) {
        Write-Host "==>        Last probe that had it alive: $LastAliveProbe"
    } else {
        Write-Host "==>        It died before the first connect-only probe completed."
    }
    Write-Host "==>        Any 'connection closed / refused' above is a CONSEQUENCE, not the cause."
    Write-Host "==>        This is a SERVER-side defect (crash in the libobs/plugin lifecycle),"
    Write-Host "==>        not CI flakiness -- diagnose the tails below, do not retry blind."
    Write-Host "==> Pulsar stdout tail:"
    Show-LogTail -Path $StdoutLog -Tail 60
    Write-Host "==> Pulsar stderr tail (libobs log -- crash context lives here):"
    Show-LogTail -Path $StderrLog -Tail 60
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

# --------------------------------------------------------------------
# Phase 1f -- self-spawning output-effect probe (probe-output-effect.py,
# #120 / ADR Prism 026 §3.2).
#
# Drives a REAL refusal in each of the four output families (replay buffer
# with no encoder, stream with no service, record with an uncreatable
# PULSAR_RECORD_DIR, virtualcam) and asserts the v5 request answers with an
# explicit error carrying the cause -- never the pre-#120 "Success() then
# outputActive:false". Also asserts the positive control (record into a
# writable dir still succeeds) and the latency bound: the verification must
# not have turned any start/stop into a wait for activation.
#
# Self-spawns TWO pulsar.exe children of its own (one per PULSAR_RECORD_DIR
# world) and reaps them, so it belongs in Phase 1 for the same
# config.json-reseed reason as the probes above. No skip path: it needs no
# CEF, no capture target and no network -- it runs on a light build too.
# --------------------------------------------------------------------
$outputEffectProbe = Join-Path $repoRoot "scripts/probe-output-effect.py"
Write-Host "==> Running probe-output-effect.py (self-spawn output effect, #120)"
& python $outputEffectProbe --exe $pulsar
$outputEffectCode = $LASTEXITCODE
if ($outputEffectCode -ne 0) {
    Write-Host "==> probe-output-effect.py FAILED (exit $outputEffectCode)"
    Write-Host "==> An output request reported an effect it did not have -- aborting before the shared suite."
    exit 1
}
Write-Host "==> probe-output-effect.py OK"

# --------------------------------------------------------------------
# Phase 1g -- self-spawning VCAM SOURCE-MODE probe
# (probe-vcam-scene-mode.py, #119 resolution criterion 3).
#
# #119 criterion 3 is a NON-REGRESSION clause: the virtual cam in source mode
# (VCAM_SCENE) must behave as before the scene-mirror removal. That path
# resolves obs_get_source_by_name("ZabVirtualCamSource"), never
# obs_frontend_get_scenes -- so the criterion was only ever reasoned about.
# This probe exercises it: creates the scene over the WIRE, asserts
# GetSceneList now lists it (the mirror used to hide exactly that), puts a
# DIFFERENT scene on program so a fallback to the program mix would show,
# then starts the cam and demands both the real effect
# (GetVirtualCamStatus.outputActive) and the stub's own "virtual cam SOURCE
# mode -> 'ZabVirtualCamSource'" log line.
#
# The DEVICE leg needs a working virtual-camera DirectShow filter on the box
# (win-dshow/dshow-plugin.cpp:48 gates the whole `virtualcam_output` type on
# it). A CI runner has none, so the probe splits the criterion: the SCENE
# RESOLVE -- the only half the #119 mirror removal could have broken -- is
# asserted everywhere, and only the device leg degrades to exit 3 (typed
# skip), tolerated here. The probe then prints what to install; it never
# passes silently.
# --------------------------------------------------------------------
$vcamProbe = Join-Path $repoRoot "scripts/probe-vcam-scene-mode.py"
Write-Host "==> Running probe-vcam-scene-mode.py (self-spawn vcam source mode, #119 crit 3)"
& python $vcamProbe --exe $pulsar
$vcamCode = $LASTEXITCODE
if ($vcamCode -eq 3) {
    Write-Host "==> probe-vcam-scene-mode.py SKIPPED (no virtual-camera driver registered on this machine)"
} elseif ($vcamCode -ne 0) {
    Write-Host "==> probe-vcam-scene-mode.py FAILED (exit $vcamCode)"
    Write-Host "==> The vcam source mode regressed after the #119 mirror removal -- aborting before the shared suite."
    exit 1
} else {
    Write-Host "==> probe-vcam-scene-mode.py OK"
}

# --------------------------------------------------------------------
# Phase 1h -- self-spawning v5 CAPABILITY CONTRACT gate
# (probe-capability-contract.py, #121 / ADR Prism 026 §3.3 palier 3).
#
# The other probes assert that a named feature works. This one asks a
# different question of the whole v5 surface: does a request that answers
# `result: true` actually DO what it says? For each subject it re-queries
# the server with a DIFFERENT request and classifies ok / explicit error /
# ok-but-no-effect. An explicit refusal PASSES -- refusing honestly is
# correct behaviour; only "success that did not happen" fails.
#
# It is wired here as a BLOCKING gate, no continue-on-error, because the
# paliers were respected: it is green on its perimeter only since #117,
# #119, #120 and #127 landed. Wiring it before those would have condemned
# it to be ignored, then disarmed (ADR Prism 026 §3.3).
#
# The gate is BY TIERS: 32 request types across 13 families are frozen in
# scripts/contracts/capability-coverage.json and cross-checked at the end
# of every run (a subject that stops being driven fails the probe; a
# subject driven but not declared fails it too). Families outside that
# list are measured ignorance, not failures -- including two KNOWN reds
# documented in the artefact (RemoveInput, PauseRecord-before-first-byte),
# deliberately left out rather than gated onto a defect.
#
# Self-spawns its OWN pulsar.exe child (fresh ephemeral port + isolated
# PULSAR_RECORD_DIR, cleaned up) for the same config.json-reseed reason as
# the other Phase-1 probes. No skip path: it needs no CEF, no capture
# target and no network, so it runs on a light build too -- a box with no
# virtual-camera driver simply lands VirtualCam in the "explicit refusal"
# branch, which passes. ~10 s wall clock.
# --------------------------------------------------------------------
$contractProbe = Join-Path $repoRoot "scripts/probe-capability-contract.py"
Write-Host "==> Running probe-capability-contract.py (self-spawn v5 capability contract, #121)"
& python $contractProbe --exe $pulsar
$contractCode = $LASTEXITCODE
if ($contractCode -ne 0) {
    Write-Host "==> probe-capability-contract.py FAILED (exit $contractCode)"
    Write-Host "==> A v5 request reported an effect it did not have, or the frozen coverage list drifted -- aborting before the shared suite."
    exit 1
}
Write-Host "==> probe-capability-contract.py OK"

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
    Show-LogTail -Path $stdoutLog -Tail 30
    Write-Host "==> stderr:"
    Show-LogTail -Path $stderrLog -Tail 30
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
    if ($proc.HasExited) {
        Show-PulsarDeath -Proc $proc -LastAliveProbe '' -StdoutLog $stdoutLog -StderrLog $stderrLog
    } else {
        Write-Host "==> Stopping pulsar.exe"
        Stop-Process -Id $proc.Id -Force; $proc.WaitForExit(5000) | Out-Null
    }
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
    # #119 / ADR Prism 026 §3.1 -- GetSceneList must enumerate libobs, not
    # a stub-side snapshot. Creates and removes its own scene, so it leaves
    # the instance exactly as it found it.
    'probe-scene-list-truth.py',
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
$died = $false
$lastAlive = ''
for ($i = 0; $i -lt $probes.Count; $i++) {
    $p = $probes[$i]

    # The server must be alive BEFORE the probe runs -- otherwise the probe
    # only measures the corpse (WinError 1225) and buries the real event.
    if ($proc.HasExited) {
        Show-PulsarDeath -Proc $proc -LastAliveProbe $lastAlive -StdoutLog $stdoutLog -StderrLog $stderrLog
        $skipped = $probes[$i..($probes.Count - 1)]
        Write-Host "==> NOT RUN (nothing to connect to): $($skipped -join ', ')"
        $died = $true
        break
    }

    $script = Join-Path $repoRoot "scripts/$p"
    Write-Host ""
    Write-Host "==> Running $p"
    & python $script
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Host "==> $p FAILED (exit $code)"
        $failed += $p
        # Distinguish "the probe's assertion failed" from "the server died
        # under it" -- same exit code, opposite diagnosis.
        if ($proc.HasExited) {
            Show-PulsarDeath -Proc $proc -LastAliveProbe $lastAlive -StdoutLog $stdoutLog -StderrLog $stderrLog
            Write-Host "==>        $p is the probe it died UNDER, not necessarily the culprit."
            if ($i -lt $probes.Count - 1) {
                $skipped = $probes[($i + 1)..($probes.Count - 1)]
                Write-Host "==> NOT RUN (nothing to connect to): $($skipped -join ', ')"
            }
            $died = $true
            break
        }
    } else {
        Write-Host "==> $p OK"
        $lastAlive = $p
    }
}

Write-Host ""
if ($proc.HasExited) {
    Write-Host "==> pulsar.exe already exited on its own -- nothing to stop."
} else {
    Write-Host "==> Stopping pulsar.exe"
    Stop-Process -Id $proc.Id -Force
    $proc.WaitForExit(5000) | Out-Null
}

if ($died) {
    Write-Host "==> SUITE ABORTED -- shared pulsar.exe crash (see FATAL above)."
    exit 1
}

if ($failed.Count -gt 0) {
    Write-Host "==> $($failed.Count) probe(s) failed: $($failed -join ', ')"
    Write-Host "==> (pulsar.exe stayed alive throughout -- these are real assertion failures.)"
    Write-Host "==> Pulsar stdout tail:"
    Show-LogTail -Path $stdoutLog -Tail 50
    Write-Host "==> Pulsar stderr tail (libobs log):"
    Show-LogTail -Path $stderrLog -Tail 50
    exit 1
}

Write-Host "==> All probes passed"
exit 0
