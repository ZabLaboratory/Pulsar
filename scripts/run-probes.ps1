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

# CTest captures this process's stdout into a buffer and only prints it
# (via --output-on-failure) once the test process exits -- pass, fail, or
# CTest's own TIMEOUT. If something upstream of CTest kills the whole tree
# first (e.g. a CI wrapper timeout shorter than CTest's TIMEOUT), nothing
# gets printed at all and a hang is indistinguishable from a slow pass.
# Explicit flush after every phase marker below is what lets a partial
# buffer -- whatever CTest captured before being killed -- still name the
# last phase reached instead of showing nothing.
function Log-Phase {
    param([string] $Message)
    Write-Host $Message
    [Console]::Out.Flush()
}

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
    Log-Phase "==> FATAL: the shared pulsar.exe DIED (pid $($Proc.Id), exit code $code)."
    if ($LastAliveProbe) {
        Log-Phase "==>        Last probe that had it alive: $LastAliveProbe"
    } else {
        Log-Phase "==>        It died before the first connect-only probe completed."
    }
    Log-Phase "==>        Any 'connection closed / refused' above is a CONSEQUENCE, not the cause."
    Log-Phase "==>        This is a SERVER-side defect (crash in the libobs/plugin lifecycle),"
    Log-Phase "==>        not CI flakiness -- diagnose the tails below, do not retry blind."
    Log-Phase "==> Pulsar stdout tail:"
    Show-LogTail -Path $StdoutLog -Tail 60
    Log-Phase "==> Pulsar stderr tail (libobs log -- crash context lives here):"
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
Log-Phase "==> Running probe-websocket.py (self-spawn smoke, M1)"
& python $smokeProbe --exe $pulsar
$smokeCode = $LASTEXITCODE
if ($smokeCode -ne 0) {
    Log-Phase "==> probe-websocket.py FAILED (exit $smokeCode)"
    Log-Phase "==> The freshly-built pulsar.exe did not boot cleanly -- aborting before the shared suite."
    exit 1
}
Log-Phase "==> probe-websocket.py OK"

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
Log-Phase "==> Running probe-record-m2.py (self-spawn media output, M2)"
& python $recordProbe --exe $pulsar
$recordCode = $LASTEXITCODE
if ($recordCode -ne 0) {
    Log-Phase "==> probe-record-m2.py FAILED (exit $recordCode)"
    Log-Phase "==> The binary could not be driven to produce a valid MP4 -- aborting before the shared suite."
    exit 1
}
Log-Phase "==> probe-record-m2.py OK"

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
Log-Phase "==> Running probe-browser-m3.py (self-spawn CEF capture, M3)"
& python $browserProbe --exe $pulsar
$browserCode = $LASTEXITCODE
if ($browserCode -eq 3) {
    Log-Phase "==> probe-browser-m3.py SKIPPED (light build -- browser_source absent, no CEF)"
} elseif ($browserCode -ne 0) {
    Log-Phase "==> probe-browser-m3.py FAILED (exit $browserCode)"
    Log-Phase "==> CEF could not render + capture a page -- aborting before the shared suite."
    exit 1
} else {
    Log-Phase "==> probe-browser-m3.py OK"
}

# --------------------------------------------------------------------
# Phase 1c-bis -- self-spawning WEBPAGE CONTROL LEVEL + browser-source
# LIFECYCLE probe (probe-webpage-control-level.py, #158 / ADR Prism 028
# §3.2).
#
# M3 above proves a page RENDERS. This one asks what that page can DO, from
# inside the page: it serves a local page that calls window.obsstudio and
# reports what came back, then drives a browser source at it through BOTH
# creation paths (v5 CreateInput and pulsar-scene:SetCaptureSource) and
# demands the page see webpage_control_level=None and get nothing from
# getStatus / getCurrentScene / getScenes. A CreateInput request that ASKS
# for level All must still land on None -- the pin overrides the wire.
#
# It also settles the lifecycle (D2 of the same issue), in both directions:
# the active capture page KEEPS its JS state across a program-scene change
# (tearing CEF down on a cut would blank the antenna), and a page left on a
# scene the operator has since left DIES when the capture source is swapped
# -- the regression test for the sweep that used to visit only the current
# frontend scene.
#
# Skip-aware for the same reason as M3: no CEF on a light build -> exit 3.
# The SOURCE half of the guarantee is gated build-free in the lint job
# (scripts/check-webpage-control-level.py), so a light build still fails on
# an unpinned creation path. ~70 s wall clock (two report deadlines + the
# retire grace window).
# --------------------------------------------------------------------
$wclProbe = Join-Path $repoRoot "scripts/probe-webpage-control-level.py"
Log-Phase "==> Running probe-webpage-control-level.py (self-spawn control level + lifecycle, #158)"
& python $wclProbe --exe $pulsar
$wclCode = $LASTEXITCODE
if ($wclCode -eq 3) {
    Log-Phase "==> probe-webpage-control-level.py SKIPPED (light build -- browser_source absent, no CEF)"
} elseif ($wclCode -ne 0) {
    Log-Phase "==> probe-webpage-control-level.py FAILED (exit $wclCode)"
    Log-Phase "==> A third-party page can reach OBS state, or a retired page is still running -- aborting before the shared suite."
    exit 1
} else {
    Log-Phase "==> probe-webpage-control-level.py OK"
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
Log-Phase "==> Running probe-stinger-smoke.py (self-spawn stinger seam, M10 #57)"
& python $stingerProbe --exe $pulsar
$stingerCode = $LASTEXITCODE
if ($stingerCode -eq 3) {
    Log-Phase "==> probe-stinger-smoke.py SKIPPED (light build -- obs_stinger_transition absent)"
} elseif ($stingerCode -ne 0) {
    Log-Phase "==> probe-stinger-smoke.py FAILED (exit $stingerCode)"
    Log-Phase "==> The stinger transition seam did not load/compose cleanly -- aborting before the shared suite."
    exit 1
} else {
    Log-Phase "==> probe-stinger-smoke.py OK"
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
Log-Phase "==> Running probe-replay.py (self-spawn replay buffer, #117)"
& python $replayProbe --exe $pulsar
$replayCode = $LASTEXITCODE
if ($replayCode -ne 0) {
    Log-Phase "==> probe-replay.py FAILED (exit $replayCode)"
    Log-Phase "==> The replay buffer did not arm/save a real file -- aborting before the shared suite."
    exit 1
}
Log-Phase "==> probe-replay.py OK"

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
Log-Phase "==> Running probe-m10-canvas-live.py (self-spawn M10 e2e, proof-only, #61)"
& python $m10Probe --exe $pulsar --no-broadcast --loopback-leaf --allow-blank --duration 12
$m10Code = $LASTEXITCODE
if ($m10Code -eq 3) {
    Log-Phase "==> probe-m10-canvas-live.py SKIPPED (light build -- stinger/monitor_capture absent)"
} elseif ($m10Code -ne 0) {
    Log-Phase "==> probe-m10-canvas-live.py FAILED (exit $m10Code)"
    Log-Phase "==> The M10 Blue->leaf->consumer->switch chain did not wire/compose cleanly -- aborting before the shared suite."
    exit 1
} else {
    Log-Phase "==> probe-m10-canvas-live.py OK"
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
Log-Phase "==> Running probe-output-effect.py (self-spawn output effect, #120)"
& python $outputEffectProbe --exe $pulsar
$outputEffectCode = $LASTEXITCODE
if ($outputEffectCode -ne 0) {
    Log-Phase "==> probe-output-effect.py FAILED (exit $outputEffectCode)"
    Log-Phase "==> An output request reported an effect it did not have -- aborting before the shared suite."
    exit 1
}
Log-Phase "==> probe-output-effect.py OK"

# --------------------------------------------------------------------
# Phase 1f-bis -- self-spawning v5 STREAM-EGRESS GUARD
# (probe-stream-egress-guard.py, Bastion C1/C2 on PR #133, widened #135/#136).
#
# #131 made the v5 SetStreamServiceSettings + StartStream path a LIVE egress
# for the first time by binding `streamService` to `streamOutput`. That is
# exactly the path #114 had left dead so that an rtmp_common service -- whose
# ingest upstream resolves out of a service list downloaded at runtime,
# falling back for Twitch to the CLEARTEXT rtmp://live.twitch.tv/app when the
# list is absent -- could never reach the wire. This probe is the executable
# form of the guards that close it back: rtmp_common as a whole is barred from
# the v5 path (Twitch goes through pulsar:StartDestination, which pins rtmps://
# by static_assert; everything else through rtmp_custom with an explicit
# server), the boot placeholder is neutral so the default path has nothing to
# refuse (#136), and the v5 path validates scheme + non-empty key exactly like
# that twin does.
#
# BLOCKING, no skip path: it needs no CEF, no capture target and no network
# (its nominal destination is a deliberately unreachable rtmp://127.0.0.1:1).
# A security invariant with a tolerated red is not an invariant.
#
# Self-spawns its OWN pulsar.exe child for the same config.json-reseed reason
# as the other Phase-1 probes. ~5 s wall clock.
# --------------------------------------------------------------------
$egressProbe = Join-Path $repoRoot "scripts/probe-stream-egress-guard.py"
Log-Phase "==> Running probe-stream-egress-guard.py (self-spawn v5 egress guard, #133 C1/C2)"
& python $egressProbe --exe $pulsar
$egressCode = $LASTEXITCODE
if ($egressCode -ne 0) {
    Log-Phase "==> probe-stream-egress-guard.py FAILED (exit $egressCode)"
    Log-Phase "==> The v5 stream path accepted a destination it must refuse -- aborting before the shared suite."
    exit 1
}
Log-Phase "==> probe-stream-egress-guard.py OK"

# --------------------------------------------------------------------
# Phase 1f-ter -- self-spawning LOOPBACK BIND probe
# (probe-loopback-bind.py, #134).
#
# The obs-websocket server listened on every interface, so the whole v5
# surface -- including the egress path above -- was reachable from the LAN
# behind nothing but the session password. It now binds 127.0.0.1 unless
# PULSAR_WS_BIND says otherwise. The assertion is at the SOCKET layer: the
# loopback still speaks v5, a TCP connect to this host's own LAN address on
# the same port is refused, and the explicit override does re-open it.
#
# SKIPS (exit 3) on a host with no non-loopback IPv4 -- there the boundary is
# unobservable, and a probe that cannot observe must not invent a verdict.
# Self-spawns its OWN pulsar.exe children (two, sequential). ~10 s wall clock.
# --------------------------------------------------------------------
$bindProbe = Join-Path $repoRoot "scripts/probe-loopback-bind.py"
Log-Phase "==> Running probe-loopback-bind.py (self-spawn loopback bind, #134)"
& python $bindProbe --exe $pulsar
$bindCode = $LASTEXITCODE
if ($bindCode -eq 3) {
    Log-Phase "==> probe-loopback-bind.py SKIPPED (no non-loopback IPv4 on this host)"
} elseif ($bindCode -ne 0) {
    Log-Phase "==> probe-loopback-bind.py FAILED (exit $bindCode)"
    Log-Phase "==> The v5 server is listening beyond the loopback -- aborting before the shared suite."
    exit 1
} else {
    Log-Phase "==> probe-loopback-bind.py OK"
}

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
Log-Phase "==> Running probe-vcam-scene-mode.py (self-spawn vcam source mode, #119 crit 3)"
& python $vcamProbe --exe $pulsar
$vcamCode = $LASTEXITCODE
if ($vcamCode -eq 3) {
    Log-Phase "==> probe-vcam-scene-mode.py SKIPPED (no virtual-camera driver registered on this machine)"
} elseif ($vcamCode -ne 0) {
    Log-Phase "==> probe-vcam-scene-mode.py FAILED (exit $vcamCode)"
    Log-Phase "==> The vcam source mode regressed after the #119 mirror removal -- aborting before the shared suite."
    exit 1
} else {
    Log-Phase "==> probe-vcam-scene-mode.py OK"
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
Log-Phase "==> Running probe-capability-contract.py (self-spawn v5 capability contract, #121)"
& python $contractProbe --exe $pulsar
$contractCode = $LASTEXITCODE
if ($contractCode -ne 0) {
    Log-Phase "==> probe-capability-contract.py FAILED (exit $contractCode)"
    Log-Phase "==> A v5 request reported an effect it did not have, or the frozen coverage list drifted -- aborting before the shared suite."
    exit 1
}
Log-Phase "==> probe-capability-contract.py OK"

# --------------------------------------------------------------------
# Phase 1i -- self-spawning PRESET ROUND-TRIP probe
# (probe-qsv-preset.py, QSV target_usage mismatch).
#
# PULSAR_VIDEO_PRESET was written to the obs_data key "preset" for every
# family; obs-qsv11 has no such key (its knob is "target_usage"), so QSV
# spawns silently encoded at their own TU4 default, and the reader --
# which looked only at "preset" -- reported "" for them. This probe
# spawns with an explicit preset and asks GetVideoSettings what was
# actually applied.
#
# The x264 leg (P1) runs on every machine and is the blocking one. The
# QSV legs (P2/P3) need an Intel QSV device: without one the boot falls
# back to x264 and the probe prints a NAMED partial and exits 0 -- it
# asserts nothing about QSV rather than pretending. No runner in the
# current fleet has that device; the hardware-free half of the proof is
# scripts/check-qsv-preset-contract.py in the lint job, which pins the
# property name / values / default against obs-qsv11's own source.
#
# Self-spawns its own children (fresh ephemeral port + isolated
# PULSAR_RECORD_DIR) for the same config.json-reseed reason as the other
# Phase-1 probes.
# --------------------------------------------------------------------
$presetProbe = Join-Path $repoRoot "scripts/probe-qsv-preset.py"
Log-Phase "==> Running probe-qsv-preset.py (self-spawn preset round-trip)"
& python $presetProbe --exe $pulsar
$presetCode = $LASTEXITCODE
if ($presetCode -ne 0) {
    Log-Phase "==> probe-qsv-preset.py FAILED (exit $presetCode)"
    Log-Phase "==> The preset asked for at boot is not the preset the encoder carries -- aborting before the shared suite."
    exit 1
}
Log-Phase "==> probe-qsv-preset.py OK"

# --------------------------------------------------------------------
# Phase 1j -- self-spawning MULTI-TRACK AUDIO probe
# (probe-audio-multitrack.py, #168 / ADR Prism 028 section 3.5).
#
# #157 closed the LIE (a mixer bit written on an input that no encoder
# consumed); this probe fences the FUNCTION that replaced it: N audio
# encoders, one per libobs mixer index, and a per-output choice of the
# tracks each output carries.
#
# The assertion that matters is NOT that the wiring reads back -- it is
# that an input routed to track N is measurably CONSUMED by track N.
# That is read on the audio mix the track's encoder is attached to
# (pulsar:MeasureAudioTrackFlow), and DIFFERENTIALLY: the same input is
# moved from track 3 to track 1 and the signal must move with it. An
# input-side read (GetInputAudioTracks) answers "enabled" in both the
# healthy and the broken case and would confirm the lie -- do not
# "simplify" this probe in that direction.
#
# Self-spawns TWO pulsar.exe children of its own (multi-track, then a
# bare one for the non-regression leg), each with a fresh ephemeral port
# and an isolated PULSAR_RECORD_DIR, for the same config.json-reseed
# reason as the other Phase-1 probes. No CEF, no capture target, no
# network, no sound card: the tone is a WAV the probe writes and an
# ffmpeg_source reads. ~25 s wall clock.
# --------------------------------------------------------------------
$multitrackProbe = Join-Path $repoRoot "scripts/probe-audio-multitrack.py"
Log-Phase "==> Running probe-audio-multitrack.py (self-spawn multi-track audio, #168)"
& python $multitrackProbe --exe $pulsar
$multitrackCode = $LASTEXITCODE
if ($multitrackCode -ne 0) {
    Log-Phase "==> probe-audio-multitrack.py FAILED (exit $multitrackCode)"
    Log-Phase "==> Multi-track audio is wired but not carried, or the single-track default regressed -- aborting before the shared suite."
    exit 1
}
Log-Phase "==> probe-audio-multitrack.py OK"

# --------------------------------------------------------------------
# Phase 1k -- RESEED regression probe (#181 veto V1/V4, Bastion
# 2026-08-10). Every probe above wipes config.json before its own
# self-spawn -- none of them ever exercise a SECOND boot against an
# EXISTING config.json written by a PRIOR boot of the same binary.
# That is exactly the code path V1 broke: an earlier ownership
# heuristic classified our own just-written file as foreign under a
# full-admin token and refused to reseed on every boot after the
# first -- CI never caught it because every gate deleted config.json
# first. This probe boots pulsar.exe TWICE in the SAME rundir WITHOUT
# deleting config.json in between, then asserts (a) the DACL is still
# protected/current-user-only after the SECOND boot, and (b) the
# SECOND boot's port + password actually authenticate over a real
# WebSocket connection -- not a file-content assumption.
# --------------------------------------------------------------------
Log-Phase "==> Reseed regression probe: booting pulsar.exe twice without wiping config.json (#181 F4)"

function New-FreePort {
    $l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $l.Start()
    $p = $l.LocalEndpoint.Port
    $l.Stop()
    return $p
}
function New-SessionPassword {
    -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 22 | ForEach-Object { [char]$_ })
}
# Stop-Process only signals the pulsar.exe PID itself. pulsar-browser
# (CEF) spawns GPU/renderer/utility HELPER processes as children of
# that PID; TerminateProcess on the parent does not take them down,
# and CEF's ProcessSingleton mechanism keeps a lock file in the
# shared rundir's browser cache directory alive as long as any of
# those orphans survive. Every probe before this one is the only
# pulsar.exe running at any point in the suite with room to settle
# between boots -- this is the first back-to-back boot of the SAME
# rundir, so an orphaned helper from boot #1 can make boot #2 collide
# with that lock and stall for most/all of its own READY wait budget,
# consuming most of the CI job's fixed timeout (#181 CI timeout,
# offline-probes hitting the nick-fields/retry 300s ceiling on both
# attempts). `taskkill /T` kills the whole process tree so no CEF
# child survives into the next boot.
function Stop-PulsarProcessTree {
    param([System.Diagnostics.Process] $Proc, [int] $WaitMs = 10000)
    if (-not $Proc -or $Proc.HasExited) { return }
    & taskkill /PID $Proc.Id /T /F 2>&1 | Out-Null
    try { $Proc.WaitForExit($WaitMs) | Out-Null } catch {}
}
function Wait-PulsarReady {
    param($Proc, $StdoutLog, $TimeoutSec)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $StdoutLog) {
            $hit = Select-String -Path $StdoutLog -Pattern '^PULSAR_READY ' -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($hit) { return $true }
        }
        if ($Proc.HasExited) { return $false }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

$reseedConfigDir  = Join-Path $binDir "obs-websocket"
$reseedConfigPath = Join-Path $reseedConfigDir "config.json"
if (Test-Path $reseedConfigPath) {
    Remove-Item $reseedConfigPath -Force
}

$reseedPort1 = New-FreePort
$reseedPwd1  = New-SessionPassword
$env:PULSAR_PORT = "$reseedPort1"
$env:PULSAR_PASSWORD = $reseedPwd1
$env:PULSAR_MIC_DEVICE_ID = $null
$env:PULSAR_CAPTURE_WINDOW = $null
$reseedStdout1 = Join-Path $repoRoot "build/probe-reseed-boot1-stdout.log"
$reseedStderr1 = Join-Path $repoRoot "build/probe-reseed-boot1-stderr.log"
New-Item -ItemType Directory -Path (Split-Path $reseedStdout1) -Force | Out-Null

Log-Phase "==> Reseed probe: boot #1 (port=$reseedPort1)"
$reseedProc1 = Start-Process -FilePath $pulsar -WorkingDirectory $binDir -PassThru `
    -RedirectStandardOutput $reseedStdout1 -RedirectStandardError $reseedStderr1
if (-not (Wait-PulsarReady -Proc $reseedProc1 -StdoutLog $reseedStdout1 -TimeoutSec $ReadyTimeoutSec)) {
    Log-Phase "==> Reseed probe FAILED: boot #1 never reached READY"
    Show-LogTail -Path $reseedStdout1 -Tail 30
    Show-LogTail -Path $reseedStderr1 -Tail 30
    Stop-PulsarProcessTree -Proc $reseedProc1
    exit 1
}
if (-not (Test-Path $reseedConfigPath)) {
    Log-Phase "==> Reseed probe FAILED: $reseedConfigPath missing after boot #1"
    Stop-PulsarProcessTree -Proc $reseedProc1 -WaitMs 5000
    exit 1
}
Log-Phase "==> Reseed probe: boot #1 READY, config.json seeded"

# Stop boot #1 WITHOUT deleting config.json -- the whole point is the
# second boot finds its own prior file still on disk. Kill the whole
# process tree (see Stop-PulsarProcessTree) so no orphaned CEF helper
# survives to collide with boot #2's browser cache lock.
Stop-PulsarProcessTree -Proc $reseedProc1

$reseedPort2 = New-FreePort
$reseedPwd2  = New-SessionPassword
if ($reseedPort2 -eq $reseedPort1 -or $reseedPwd2 -eq $reseedPwd1) {
    Log-Phase "==> Reseed probe FAILED: boot #2 port/password collided with boot #1 (test cannot distinguish reseed from no-op)"
    exit 1
}
$env:PULSAR_PORT = "$reseedPort2"
$env:PULSAR_PASSWORD = $reseedPwd2
$reseedStdout2 = Join-Path $repoRoot "build/probe-reseed-boot2-stdout.log"
$reseedStderr2 = Join-Path $repoRoot "build/probe-reseed-boot2-stderr.log"

Log-Phase "==> Reseed probe: boot #2 (port=$reseedPort2) -- config.json from boot #1 is still on disk"
$reseedProc2 = Start-Process -FilePath $pulsar -WorkingDirectory $binDir -PassThru `
    -RedirectStandardOutput $reseedStdout2 -RedirectStandardError $reseedStderr2
if (-not (Wait-PulsarReady -Proc $reseedProc2 -StdoutLog $reseedStdout2 -TimeoutSec $ReadyTimeoutSec)) {
    Log-Phase "==> Reseed probe FAILED: boot #2 never reached READY -- this is the V1 regression class (reseed starved/refused after boot #1 owned the file)"
    Show-LogTail -Path $reseedStdout2 -Tail 30
    Show-LogTail -Path $reseedStderr2 -Tail 30
    Stop-PulsarProcessTree -Proc $reseedProc2
    exit 1
}
Log-Phase "==> Reseed probe: boot #2 READY"

# (a) DACL still protected + current-user-only after the SECOND boot.
$reseedCurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$reseedAcl = Get-Acl -Path $reseedConfigPath
$reseedBadAces = $reseedAcl.Access | Where-Object {
    -not $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Equals($reseedCurrentUser)
}
if (-not $reseedAcl.AreAccessRulesProtected -or $reseedBadAces) {
    Log-Phase "==> Reseed probe FAILED: config.json DACL is not restricted to the current account after boot #2 (reseed)"
    Write-Host ($reseedAcl.Access | Format-Table IdentityReference, FileSystemRights, AccessControlType | Out-String)
    Stop-PulsarProcessTree -Proc $reseedProc2 -WaitMs 5000
    exit 1
}
Log-Phase "==> Reseed probe: DACL OK after boot #2 (restricted to $($reseedCurrentUser.Value))"

# (b) boot #2's NEW port/password actually work -- a real WebSocket
# connection using the reseeded credentials, not a file-content
# assumption. probe-websocket.py --connect-port/--connect-password
# skips its own spawn and drives the v5 handshake against an
# already-running instance (added for this probe, #181 F4).
$reseedWsProbe = Join-Path $repoRoot "scripts/probe-websocket.py"
Log-Phase "==> Reseed probe: connecting to boot #2 with its reseeded credentials"
& python $reseedWsProbe --connect-port $reseedPort2 --connect-password $reseedPwd2 --ready-timeout $ReadyTimeoutSec
$reseedWsCode = $LASTEXITCODE
Stop-PulsarProcessTree -Proc $reseedProc2
if ($reseedWsCode -ne 0) {
    Log-Phase "==> Reseed probe FAILED: reseeded credentials (boot #2) did not authenticate over the wire (exit $reseedWsCode)"
    exit 1
}
Log-Phase "==> Reseed probe OK: config.json reseeds correctly across boots (#181 V1/V4 closed)"

if (Test-Path $reseedConfigPath) {
    Remove-Item $reseedConfigPath -Force
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
Log-Phase "==> Spawning shared pulsar.exe (cwd=$binDir, port=$sessionPort)"
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
    Log-Phase "==> Pulsar failed to reach READY within ${ReadyTimeoutSec}s. Tail of stdout:"
    Show-LogTail -Path $stdoutLog -Tail 30
    Log-Phase "==> stderr:"
    Show-LogTail -Path $stderrLog -Tail 30
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    exit 1
}
Log-Phase "==> PULSAR_READY received"

# --------------------------------------------------------------------
# Phase 2 (ACL) -- config.json DACL hardening regression, Bastion
# follow-up on #181/#191 (C4). The old best-effort comment above
# main.cpp's ACL code claimed a "confidentiality regression [to]
# catch via the probe script" that did not exist -- this is that
# probe. It asserts the config.json produced by the shared instance's
# boot carries a protected DACL naming only the current account, so a
# regression back to the inherited BUILTIN\Users grant (or a
# CreateFile/DACL-ordering bug) fails CI instead of leaking silently.
# --------------------------------------------------------------------
$aclConfigPath = Join-Path $configDir "config.json"
if (-not (Test-Path $aclConfigPath)) {
    Log-Phase "==> ACL probe FAILED: $aclConfigPath does not exist after boot"
    Stop-Process -Id $proc.Id -Force; $proc.WaitForExit(5000) | Out-Null
    exit 1
}
$aclCurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$aclInfo = Get-Acl -Path $aclConfigPath
$aclBadAces = $aclInfo.Access | Where-Object {
    -not $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Equals($aclCurrentUser)
}
if (-not $aclInfo.AreAccessRulesProtected -or $aclBadAces) {
    Log-Phase "==> ACL probe FAILED: config.json DACL is not restricted to the current account ($($aclCurrentUser.Value))"
    Write-Host ($aclInfo.Access | Format-Table IdentityReference, FileSystemRights, AccessControlType | Out-String)
    Stop-Process -Id $proc.Id -Force; $proc.WaitForExit(5000) | Out-Null
    exit 1
}
Log-Phase "==> ACL probe OK (config.json DACL restricted to $($aclCurrentUser.Value))"

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
Log-Phase "==> Running probe-scene-name-drift.py (connect-only, #110)"
& python $nameDriftProbe
$nameDriftCode = $LASTEXITCODE
if ($nameDriftCode -eq 3) {
    Log-Phase "==> probe-scene-name-drift.py SKIPPED (light build -- browser_source absent)"
} elseif ($nameDriftCode -ne 0) {
    Log-Phase "==> probe-scene-name-drift.py FAILED (exit $nameDriftCode)"
    if ($proc.HasExited) {
        Show-PulsarDeath -Proc $proc -LastAliveProbe '' -StdoutLog $stdoutLog -StderrLog $stderrLog
    } else {
        Log-Phase "==> Stopping pulsar.exe"
        Stop-Process -Id $proc.Id -Force; $proc.WaitForExit(5000) | Out-Null
    }
    exit 1
} else {
    Log-Phase "==> probe-scene-name-drift.py OK"
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
    # #144 / ADR Prism 027 §3.3 blocs 3-4 -- the capability manifest's
    # inventories must stay presence-only on the EMITTER's side: every item
    # carries exactly `value`, no filter property bound rides along (the
    # bounds are ADR 023 §3.3's, under its own clearance). Read-only probe:
    # it calls GetCapabilities and touches nothing.
    'probe-manifest-inventories.py',
    # #159 / ADR Prism 027 Amendment 1 -- the manifest's fifth block must carry
    # adapters READ from libobs (name + index, read-only) and output scales
    # DERIVED from what the binary can establish (boot-fixed, never exceeding
    # the canvas), and both must agree with GetVideoSettings on the same
    # instance. Read-only probe: two vendor reads, touches nothing.
    'probe-manifest-adapters-scales.py',
    # #157 / ADR Prism 028 §3.2 -- SetInputAudioTracks must be judged by the
    # OUTPUT (the encoder slots actually bound), never by the input, whose
    # mixer bit is written whatever the output carries. Creates and removes
    # its own scene + input.
    'probe-audio-tracks-oracle.py',
    # #173 / ADR Prism 029 §3.6 -- the monitoring DEVICE must be choosable, not
    # just bound: GetMonitoringDeviceList / SetMonitoringDevice registered, the
    # manifest agreeing with them, an unknown id refused BY NAME (libobs stores
    # any pair and returns true), and the write reported only after read-back.
    # Restores the device it found in force before returning.
    'probe-monitoring-devices.py',
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
    'probe-record.py',
    # #169 / ADR Prism 028 §3.5 -- obs_frontend_recording_split_file and
    # obs_frontend_recording_add_chapter were stubbed to `false`. Proves the
    # split on the DISK (two files, plus the RecordFileChanged event) and that
    # the chapter refusal names its cause instead of failing mutely. Runs after
    # probe-record.py: it drives its own StartRecord/StopRecord cycle and leaves
    # the instance idle.
    'probe-record-split.py'
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
        Log-Phase "==> NOT RUN (nothing to connect to): $($skipped -join ', ')"
        $died = $true
        break
    }

    $script = Join-Path $repoRoot "scripts/$p"
    Write-Host ""
    Log-Phase "==> Running $p"
    & python $script
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Log-Phase "==> $p FAILED (exit $code)"
        $failed += $p
        # Distinguish "the probe's assertion failed" from "the server died
        # under it" -- same exit code, opposite diagnosis.
        if ($proc.HasExited) {
            Show-PulsarDeath -Proc $proc -LastAliveProbe $lastAlive -StdoutLog $stdoutLog -StderrLog $stderrLog
            Log-Phase "==>        $p is the probe it died UNDER, not necessarily the culprit."
            if ($i -lt $probes.Count - 1) {
                $skipped = $probes[($i + 1)..($probes.Count - 1)]
                Log-Phase "==> NOT RUN (nothing to connect to): $($skipped -join ', ')"
            }
            $died = $true
            break
        }
    } else {
        Log-Phase "==> $p OK"
        $lastAlive = $p
    }
}

Write-Host ""
if ($proc.HasExited) {
    Log-Phase "==> pulsar.exe already exited on its own -- nothing to stop."
} else {
    Log-Phase "==> Stopping pulsar.exe"
    Stop-Process -Id $proc.Id -Force
    $proc.WaitForExit(5000) | Out-Null
}

if ($died) {
    Log-Phase "==> SUITE ABORTED -- shared pulsar.exe crash (see FATAL above)."
    exit 1
}

if ($failed.Count -gt 0) {
    Log-Phase "==> $($failed.Count) probe(s) failed: $($failed -join ', ')"
    Log-Phase "==> (pulsar.exe stayed alive throughout -- these are real assertion failures.)"
    Log-Phase "==> Pulsar stdout tail:"
    Show-LogTail -Path $stdoutLog -Tail 50
    Log-Phase "==> Pulsar stderr tail (libobs log):"
    Show-LogTail -Path $stderrLog -Tail 50
    exit 1
}

Log-Phase "==> All probes passed"
exit 0
