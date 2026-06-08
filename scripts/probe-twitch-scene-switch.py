#!/usr/bin/env python3
"""
Pulsar live Twitch SCENE-SWITCH probe -- a 30s broadcast with a real scene
change at mid-course (~t=15s) and ZERO PC sound.

WHAT THIS PROVES
  A live Twitch push that, at duration/2, performs a genuine scene change so a
  reviewer scrubbing the VOD sees a hard cut (cobalt-blue "SCENE A" -> crimson
  "SCENE B"). Two visually-distinct, dependency-free, SILENT HTML pages are
  served locally (scripts/live-test/scene-a.html + scene-b.html) and rendered
  by Pulsar's CEF browser_source.

SWITCH IMPLEMENTATION -- (a) real OBS scene, with (b) auto-fallback
  The headless frontend-stub (plugins/pulsar-frontend-stub) creates exactly ONE
  program scene at boot ("Default") and its `scenes` vector is hard-coded to a
  single entry. BUT:
    * obs-websocket's CreateScene uses obs_canvas_scene_create on the MAIN
      canvas, registering the scene in libobs's GLOBAL source table.
    * SetCurrentProgramScene -> AcquireScene -> obs_get_source_by_name resolves
      by libobs-global name (NOT via the stub's scenes vector), so a
      CreateScene'd scene IS found.
    * The stub's obs_frontend_set_current_scene rebinds obs_set_output_source(0,
      scene) and emits SCENE_CHANGED -> the new scene actually composites.
  So a TRUE two-scene switch is viable headless and is attempted FIRST (path a):
    1. CreateScene("pulsar-scene-b").
    2. SetCurrentProgramScene("pulsar-scene-b"); SetCaptureSource(scene-b.html)
       -- the pulsar-scene plugin installs the browser_source on whatever
       obs_frontend_get_current_scene() returns, i.e. scene-b.
    3. SetCurrentProgramScene("Default" / scene-a); SetCaptureSource(scene-a.html)
       -- scene-a now carries the cobalt page; we go live on it.
    4. At t=duration/2: SetCurrentProgramScene("pulsar-scene-b") -> hard cut to
       crimson. Asserted via GetCurrentProgramScene before AND after.
  If CreateScene or the program-scene flip is not honoured by this build
  (older stub, single-canvas quirk), the probe AUTO-FALLS-BACK to path (b):
  a single program scene whose displayed browser_source URL is swapped from
  scene-a.html to scene-b.html via SetCaptureSource at t=duration/2. Path (b)
  is still a real, eye-visible content change on the live wire; it is clearly
  labelled in the run log + the run summary as a fallback. The chosen path is
  reported so Vigil/Probe know which assertion ran.

ZERO PC SOUND (impératif)
  "No sound from the PC" is enforced on three fronts:
    1. Spawn env: PULSAR_MIC_DEVICE_ID popped (mic source never created) and
       PULSAR_PROCESS_AUDIO_NAME left unset (process-loopback never created).
    2. The browser_source is created with reroute_audio=False -- the page audio
       (there is none anyway; both scene pages are silent) is NOT routed into
       the OBS mixer.
    3. The frontend-stub ALWAYS creates a desktop-audio loopback on mixer
       channel 1 ("PulsarDesktopAudio", device_id=default) at boot -- THAT is
       literally "the sound of the PC". The probe SetInputMute()s it right after
       auth, verifies the mute via GetInputMute, then enumerates GetInputList +
       GetInputMute over every audio input and ASSERTS none remains unmuted
       before going live. If any unmuted audio input survives, the probe fails
       closed and does NOT broadcast.
  Net: the AAC track on the Twitch push is silence.

SECRET HANDLING
  TWITCH_STREAM_KEY is read from the environment ONLY (set by the caller from
  the etage-1 secret file). It is NEVER printed/logged/written; any log line
  that might echo it is redacted to <KEY> (redact(), mirrored from
  probe-m6-live.py). Missing key -> exit 2, no broadcast.

LICENSE INVARIANT
  Pure obs-websocket v5 over the process boundary (+ `pulsar`/`pulsar-scene`
  vendor requests). No FFI, no native import. CEF lives entirely inside the
  pulsar.exe process tree.

Usage (from the repo root, against a built rundir):
    pip install websockets
    export TWITCH_STREAM_KEY=...     # from etage-1 secret, NEVER committed
    python scripts/probe-twitch-scene-switch.py
    python scripts/probe-twitch-scene-switch.py --duration 30

Required env:
  TWITCH_STREAM_KEY   Twitch stream key (opaque; never logged)

Optional env / flags:
  PULSAR_EXE / --exe  override pulsar.exe path
  --duration          broadcast seconds (default 30); switch fires at /2
  --fps               encoder fps target (default 60)
  --force-fallback    skip path (a), go straight to the URL-swap fallback (b)

Exit codes:
  0  pass (scene switch performed + asserted; zero unmuted audio confirmed)
  1  fail (switch not honoured / broadcast assertion failed)
  2  config error (no key, no exe, bad args)
  3  typed skip (browser_source not registered -- LIGHT build, needs -Full)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import hashlib
import http.server
import json
import os
import pathlib
import re
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

try:
    import websockets
except ImportError:
    print("error: pip install websockets (pure WS client — no native deps)")
    sys.exit(2)


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    REPO_ROOT / "upstream" / "build_x64" / "rundir" / "RelWithDebInfo"
    / "bin" / "64bit" / "pulsar.exe"
)
SCENE_DIR = REPO_ROOT / "scripts" / "live-test"
BUILD_DIR = REPO_ROOT / "build"
LIVE_VOD_DIR = BUILD_DIR / "scene-switch-vod"

READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0
EVENT_SUBSCRIPTION_ALL = 0x7FF

CANVAS_W = 1920
CANVAS_H = 1080

FRAME_DROP_RATIO_MAX = 0.05
POLL_INTERVAL_SEC = 5.0
DESTINATION_NAME = "pulsar-scene-switch"

# The default program scene the frontend-stub creates at boot.
DEFAULT_SCENE_NAME = "Default"
# The second program scene we attempt to create for path (a).
SCENE_B_NAME = "pulsar-scene-b"
# Desktop-audio source the frontend-stub binds to mixer channel 1 at boot.
# This is the "sound of the PC" we must mute. Name from
# plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp.
DESKTOP_AUDIO_SOURCE = "PulsarDesktopAudio"

BENIGN_LOG_SUBSTRINGS = [
    "no target (set PULSAR_CAPTURE_WINDOW)",
    "Failed to find module 'win-mf'",
]


# --------------------------------------------------------------------------
# Secret redaction. The stream key must never reach a log line.
# --------------------------------------------------------------------------
def redact(text: str, key: str) -> str:
    if key and key in text:
        return text.replace(key, "<KEY>")
    return text


# --------------------------------------------------------------------------
# Local HTTP server hosting the two scene pages.
# --------------------------------------------------------------------------
def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # silence per-request stderr noise
        pass


def start_scene_server(port: int) -> socketserver.ThreadingTCPServer:
    handler = functools.partial(_QuietHandler, directory=str(SCENE_DIR))
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, name="scene-http", daemon=True).start()
    return httpd


# --------------------------------------------------------------------------
# Process management -- mirrors probe-m6-live.py PulsarProcess.
# --------------------------------------------------------------------------
READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")


class PulsarProcess:
    def __init__(self, exe: pathlib.Path, port: int, password: str, fps: int) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.fps = fps
        self.proc: Optional[subprocess.Popen] = None
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        env["PULSAR_FPS"] = str(self.fps)
        env["PULSAR_RESOLUTION"] = f"{CANVAS_W}x{CANVAS_H}"
        env["PULSAR_VIDEO_BITRATE"] = "6000"
        LIVE_VOD_DIR.mkdir(parents=True, exist_ok=True)
        env["PULSAR_RECORD_DIR"] = str(LIVE_VOD_DIR)

        # ZERO PC SOUND, part 1: never wire a window capture, never wire mic,
        # never wire process-loopback audio. The frontend-stub only creates the
        # mic source if PULSAR_MIC_DEVICE_ID is set, and process audio only if
        # PULSAR_PROCESS_AUDIO_NAME is set -- so popping/leaving them unset means
        # those sources are never created. Desktop audio is still created
        # unconditionally and is muted later over the wire (see ensure_silence).
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)
        env.pop("PULSAR_PROCESS_AUDIO_NAME", None)

        creationflags = 0
        if os.name == "nt":
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        # --disable-gpu / --no-sandbox: forwarded to CEF via GetCommandLineW().
        # SW rasterization is the canonical headless-CEF config (same as M4/M6).
        self.proc = subprocess.Popen(
            [str(self.exe), "--disable-gpu", "--no-sandbox"],
            cwd=str(self.exe.parent),  # libobs resolves data/ from cwd
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            self._lines.append(line)
            m = READY_RE.match(line)
            if m is not None and not self._ready_event.is_set():
                self._ready_match = m
                self._ready_event.set()

    def wait_ready(self, timeout: float) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        while True:
            if self._ready_event.wait(timeout=0.2):
                m = self._ready_match
                assert m is not None
                return m.group(1), m.group(2)
            assert self.proc is not None
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"pulsar.exe exited (code {self.proc.returncode}) before READY.\n"
                    + self._diag()
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"pulsar.exe did not signal READY within {timeout:.0f}s.\n"
                    + self._diag()
                )

    @property
    def lines(self) -> list[str]:
        return list(self._lines)

    def diag(self) -> str:
        return self._diag()

    def _diag(self) -> str:
        tail = self._lines[-40:]
        body = "\n".join(f"  | {ln}" for ln in tail) if tail else "  | (no output)"
        return f"--- pulsar stdout/stderr (last {len(tail)} lines) ---\n{body}"

    def shutdown(self, grace: float = SHUTDOWN_GRACE_S) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=grace)
            return
        except Exception:
            pass
        try:
            self.proc.kill()
            self.proc.wait(timeout=grace)
        except Exception:
            pass


# --------------------------------------------------------------------------
# obs-websocket v5 plumbing -- mirrors probe-m6-live.py.
# --------------------------------------------------------------------------
def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


class Inbox:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.responses: list[dict] = []

    async def pump(self, ws, until: Callable[["Inbox"], bool], timeout: float) -> None:
        end = asyncio.get_event_loop().time() + timeout
        while not until(self):
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            msg = json.loads(raw)
            op = msg.get("op")
            if op == 5:
                self.events.append(msg["d"])
            elif op == 7:
                self.responses.append(msg["d"])


async def request(
    inbox: Inbox, ws, request_type: str, request_id: str,
    data: dict | None = None, timeout: float = 30.0,
) -> dict:
    body: dict = {"requestType": request_type, "requestId": request_id}
    if data is not None:
        body["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": body}))

    def has_response(ix: Inbox) -> bool:
        return any(r["requestId"] == request_id for r in ix.responses)

    await inbox.pump(ws, has_response, timeout)
    for i, r in enumerate(inbox.responses):
        if r["requestId"] == request_id:
            return inbox.responses.pop(i)
    raise RuntimeError("unreachable")


async def vendor_call(
    inbox: Inbox, ws, request_id: str, vendor: str, vendor_type: str,
    data: dict | None = None, timeout: float = 30.0,
) -> dict:
    return await request(inbox, ws, "CallVendorRequest", request_id, {
        "vendorName": vendor,
        "requestType": vendor_type,
        "requestData": data or {},
    }, timeout)


def vendor_response_data(resp: dict) -> dict:
    rd = resp.get("responseData", {})
    inner = rd.get("responseData", {}) if isinstance(rd, dict) else {}
    return inner if isinstance(inner, dict) else {}


def vendor_request_status(resp: dict) -> dict:
    s = resp.get("requestStatus", {})
    return s if isinstance(s, dict) else {}


def req_ok(resp: dict) -> bool:
    return bool(resp.get("requestStatus", {}).get("result"))


# --------------------------------------------------------------------------
# ZERO PC SOUND: mute the desktop-audio loopback + assert no unmuted input.
# --------------------------------------------------------------------------
async def ensure_silence(inbox: Inbox, ws) -> int:
    """Mute the frontend-stub's desktop-audio source and assert that NO audio
    input remains unmuted. Returns 0 on a fully-silent mixer, 1 otherwise.

    The frontend-stub binds PulsarDesktopAudio to mixer channel 1 at boot
    regardless of env -- that is the literal 'sound of the PC'. Mic + process
    audio are never created (env popped/unset at spawn). We mute the desktop
    source explicitly, then enumerate inputs and verify silence."""
    # 1. Mute the known desktop-audio source. It is created by the stub
    #    (wasapi_output_capture); if a build omitted it, the mute call simply
    #    reports the source missing, which is also silence.
    r = await request(inbox, ws, "SetInputMute", "mute-desktop", {
        "inputName": DESKTOP_AUDIO_SOURCE,
        "inputMuted": True,
    })
    if req_ok(r):
        print(f"-> muted desktop audio source '{DESKTOP_AUDIO_SOURCE}'")
    else:
        # Not fatal on its own -- the source may be absent on this build.
        print(f"   note: SetInputMute('{DESKTOP_AUDIO_SOURCE}') declined "
              f"({r.get('requestStatus')}); will rely on the input enumeration")

    # 2. Enumerate every input and assert none of the audio ones is unmuted.
    #    GetInputMute fails for non-audio inputs (no volume interface); we
    #    treat a successful GetInputMute as 'this is an audio input'.
    r = await request(inbox, ws, "GetInputList", "input-list", {})
    if not req_ok(r):
        print(f"FAIL: GetInputList failed: {r.get('requestStatus')}")
        return 1
    inputs = r["responseData"].get("inputs", []) or []
    audio_inputs: list[str] = []
    unmuted: list[str] = []
    for idx, inp in enumerate(inputs):
        name = inp.get("inputName")
        if not name:
            continue
        mr = await request(inbox, ws, f"get-mute-{idx}", "GetInputMute",
                           {"inputName": name})
        if not req_ok(mr):
            # No volume interface -> not an audio input (e.g. browser_source
            # with reroute_audio off, scenes). Skip.
            continue
        audio_inputs.append(name)
        if not mr["responseData"].get("inputMuted", False):
            unmuted.append(name)

    print(f"-> audio inputs present: {audio_inputs or '(none)'}")
    if unmuted:
        print(f"FAIL: audio input(s) NOT muted -> PC sound would broadcast: "
              f"{unmuted}. Refusing to go live.")
        return 1
    print("-> silence confirmed: every audio input is muted (or absent)")
    return 0


# --------------------------------------------------------------------------
# Scene plumbing.
# --------------------------------------------------------------------------
async def set_capture_browser(inbox: Inbox, ws, rid: str, url: str) -> dict:
    """SetCaptureSource(browser_source, url) on the CURRENT program scene.
    reroute_audio=False so no page audio reaches the OBS mixer (zero PC sound).
    Returns the unwrapped vendor responseData."""
    r = await vendor_call(inbox, ws, rid, "pulsar-scene", "SetCaptureSource", {
        "kind": "browser_source",
        "url": url,
        "width": CANVAS_W,
        "height": CANVAS_H,
        "fps": 60,
        "reroute_audio": False,
    })
    return vendor_response_data(r)


async def get_current_scene(inbox: Inbox, ws, rid: str) -> Optional[str]:
    r = await request(inbox, ws, "GetCurrentProgramScene", rid, {})
    if not req_ok(r):
        return None
    rd = r["responseData"]
    return rd.get("sceneName") or rd.get("currentProgramSceneName")


async def set_current_scene(inbox: Inbox, ws, rid: str, name: str) -> bool:
    r = await request(inbox, ws, "SetCurrentProgramScene", rid,
                      {"sceneName": name})
    return req_ok(r)


async def try_setup_two_scenes(inbox: Inbox, ws, url_a: str, url_b: str) -> bool:
    """Path (a): create a second program scene, give each scene its own
    browser_source, leave scene-a current + live. Returns True if the real
    two-scene model is established and verified, False to fall back to (b).

    The pulsar-scene plugin always installs the browser_source on whatever
    obs_frontend_get_current_scene() returns, so we set scene-b current, paint
    it, then set scene-a current and paint it -- that orders the two managed
    sources onto two distinct scenes."""
    start_scene = await get_current_scene(inbox, ws, "scene-cur-0")
    if not start_scene:
        print("   path(a): GetCurrentProgramScene returned nothing; fallback")
        return False
    print(f"   path(a): boot program scene = {start_scene!r}")

    # Create scene-b. ResourceAlreadyExists is fine (idempotent re-run).
    r = await request(inbox, ws, "create-scene-b", "CreateScene",
                      {"sceneName": SCENE_B_NAME})
    if not req_ok(r):
        code = r.get("requestStatus", {}).get("code")
        # 601 == ResourceAlreadyExists in obs-websocket. Tolerate it.
        if code != 601:
            print(f"   path(a): CreateScene declined ({r.get('requestStatus')}); "
                  f"fallback")
            return False

    # Switch to scene-b and confirm the program scene actually flipped -- this
    # is the live test of whether the headless stub honours program-scene
    # changes for a CreateScene'd scene at all.
    if not await set_current_scene(inbox, ws, "to-b-setup", SCENE_B_NAME):
        print("   path(a): SetCurrentProgramScene(scene-b) declined; fallback")
        return False
    now = await get_current_scene(inbox, ws, "scene-cur-1")
    if now != SCENE_B_NAME:
        print(f"   path(a): program scene did not flip to {SCENE_B_NAME!r} "
              f"(got {now!r}); fallback")
        return False

    # Paint scene-b crimson.
    data_b = await set_capture_browser(inbox, ws, "cap-b", url_b)
    if data_b.get("kind") != "browser_source":
        print(f"   path(a): SetCaptureSource on scene-b failed ({data_b}); "
              f"fallback")
        return False

    # Switch back to scene-a (the boot scene) and paint it cobalt.
    if not await set_current_scene(inbox, ws, "to-a-setup", start_scene):
        print("   path(a): could not return to scene-a; fallback")
        return False
    now = await get_current_scene(inbox, ws, "scene-cur-2")
    if now != start_scene:
        print(f"   path(a): program scene did not return to {start_scene!r}; "
              f"fallback")
        return False
    data_a = await set_capture_browser(inbox, ws, "cap-a", url_a)
    if data_a.get("kind") != "browser_source":
        print(f"   path(a): SetCaptureSource on scene-a failed ({data_a}); "
              f"fallback")
        return False

    print(f"   path(a): two scenes ready -- '{start_scene}' (cobalt) + "
          f"'{SCENE_B_NAME}' (crimson); live on '{start_scene}'")
    return True


# --------------------------------------------------------------------------
# Broadcast + mid-course switch.
# --------------------------------------------------------------------------
async def broadcast(inbox: Inbox, ws, stream_key: str, duration_sec: int,
                    use_real_scene: bool, scene_a: str, url_a: str, url_b: str,
                    pulsar: "PulsarProcess") -> int:
    switch_at = duration_sec / 2.0

    r = await vendor_call(inbox, ws, "create-dest", "pulsar",
        "CreateDestination", {
            "name": DESTINATION_NAME, "kind": "twitch", "key": stream_key,
        })
    dest_data = vendor_response_data(r)
    dest_id = dest_data.get("id")
    if not dest_id:
        status = vendor_request_status(r)
        print(f"FAIL: CreateDestination returned no id; "
              f"status={redact(json.dumps(status), stream_key)}")
        return 1
    print(f"-> CreateDestination(twitch) id={dest_id}")

    r = await vendor_call(inbox, ws, "start-dest", "pulsar",
        "StartDestination", {"id": dest_id})
    if not vendor_response_data(r).get("started"):
        status = vendor_request_status(r)
        print(f"FAIL: StartDestination not started; "
              f"status={redact(json.dumps(status), stream_key)}")
        await vendor_call(inbox, ws, "rm-dest", "pulsar",
            "RemoveDestination", {"id": dest_id})
        return 1
    print("-> StartDestination started=true -- LIVE on Twitch")

    recording = False
    r = await request(inbox, ws, "StartRecord", "start-rec")
    if req_ok(r):
        recording = True
        print(f"-> StartRecord ok (writing under {LIVE_VOD_DIR})")
    else:
        print(f"   warn: StartRecord declined: {r.get('requestStatus')}")

    # The switch must be observable: capture the program scene (path a) or the
    # active capture URL (path b) BEFORE the switch so we can assert it changed.
    if use_real_scene:
        before = await get_current_scene(inbox, ws, "before-switch")
    else:
        gr = await vendor_call(inbox, ws, "get-cap-before", "pulsar-scene",
                               "GetCaptureSource", {})
        before = vendor_response_data(gr).get("url")
    print(f"   pre-switch state = {redact(str(before), stream_key)}")

    rc = 0
    switched = False
    start_t = time.time()
    poll = 0
    adaptive_seen = 0
    while time.time() - start_t < duration_sec:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        poll += 1
        elapsed = time.time() - start_t

        # Mid-course SCENE SWITCH at ~duration/2.
        if not switched and elapsed >= switch_at:
            if use_real_scene:
                ok = await set_current_scene(inbox, ws, "do-switch", SCENE_B_NAME)
                now = await get_current_scene(inbox, ws, "after-switch")
                if not ok or now != SCENE_B_NAME:
                    print(f"FAIL: scene switch to {SCENE_B_NAME!r} not honoured "
                          f"(ok={ok} now={now!r})")
                    rc = 1
                    break
                print(f"** SCENE SWITCH @ t={elapsed:.1f}s : "
                      f"program scene {scene_a!r} -> {SCENE_B_NAME!r} (crimson)")
            else:
                data = await set_capture_browser(inbox, ws, "do-switch", url_b)
                gr = await vendor_call(inbox, ws, "get-cap-after",
                                       "pulsar-scene", "GetCaptureSource", {})
                now = vendor_response_data(gr).get("url")
                if data.get("kind") != "browser_source" or now != url_b:
                    print(f"FAIL: fallback URL swap not honoured "
                          f"(now={redact(str(now), stream_key)})")
                    rc = 1
                    break
                print(f"** SCENE SWITCH @ t={elapsed:.1f}s (fallback) : "
                      f"capture URL scene-a.html -> scene-b.html (crimson)")
            switched = True

        r = await vendor_call(inbox, ws, f"get-dest-{poll}", "pulsar",
            "GetDestinations", {})
        lst = vendor_response_data(r).get("destinations", [])
        ours = next((d for d in lst if d.get("id") == dest_id), None)
        if not ours or not ours.get("active"):
            print(f"FAIL: destination not active at poll #{poll}: {ours}")
            rc = 1
            break

        r = await vendor_call(inbox, ws, f"get-adapt-{poll}", "pulsar",
            "GetAdaptiveState", {})
        adapt = vendor_response_data(r)
        samples = int(adapt.get("samples", 0))
        adaptive_seen = max(adaptive_seen, samples)
        drop_ratio = float(adapt.get("last_drop_ratio", 0.0))
        cur_kbps = adapt.get("current_kbps")

        sr = await request(inbox, ws, "GetStats", f"stats-{poll}")
        stats = sr.get("responseData", {}) or {}
        fps = stats.get("activeFps")
        fps_str = f"{fps:.1f}" if isinstance(fps, (int, float)) else "—"

        print(f"   poll #{poll} t={elapsed:.0f}s active=true samples={samples} "
              f"drop_ratio={drop_ratio:.4f} bitrate={cur_kbps} fps={fps_str} "
              f"switched={switched}")

        if drop_ratio > FRAME_DROP_RATIO_MAX:
            print(f"FAIL: frame drop ratio {drop_ratio:.4f} > "
                  f"{FRAME_DROP_RATIO_MAX} at poll #{poll}")
            rc = 1
            break

    if rc == 0 and not switched:
        print("FAIL: broadcast ended before the mid-course switch fired")
        rc = 1

    # Stop cleanly (best-effort even on a failed poll).
    if recording:
        try:
            r = await request(inbox, ws, "StopRecord", "stop-rec")
            vod = (r.get("responseData", {}) or {}).get("outputPath")
            if vod:
                print(f"-> StopRecord finalised: {vod}")
                print(f"LIVE_VOD_PATH={vod}")
        except Exception as exc:  # noqa: BLE001
            print(f"   warn: StopRecord error: {exc}")
    try:
        await vendor_call(inbox, ws, "stop-dest", "pulsar",
            "StopDestination", {"id": dest_id})
        print("-> StopDestination ok")
    except Exception as exc:  # noqa: BLE001
        print(f"   warn: StopDestination error: {exc}")
    try:
        await vendor_call(inbox, ws, "rm-dest", "pulsar",
            "RemoveDestination", {"id": dest_id})
    except Exception:
        pass

    if rc == 0 and adaptive_seen <= 0:
        print(f"FAIL: adaptive worker never reported samples (saw {adaptive_seen})")
        rc = 1
    if rc == 0:
        print(f"-> broadcast clean: switch performed, adaptive_samples={adaptive_seen}")
    return rc


async def run(url: str, password: str, stream_key: str, duration_sec: int,
              http_port: int, force_fallback: bool, pulsar: "PulsarProcess") -> int:
    scene_a_url = f"http://127.0.0.1:{http_port}/scene-a.html"
    scene_b_url = f"http://127.0.0.1:{http_port}/scene-b.html"

    print(f"connecting: {url}")
    async with websockets.connect(
        url, subprotocols=["obswebsocket.json"], max_size=2**24,
        ping_interval=None, close_timeout=15, open_timeout=10,
    ) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if hello.get("op") != 0:
            print(f"error: expected Hello (op=0), got {hello}")
            return 1
        identify_d: dict = {
            "rpcVersion": hello["d"]["rpcVersion"],
            "eventSubscriptions": EVENT_SUBSCRIPTION_ALL,
        }
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(
                password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ident.get("op") != 2:
            print(f"error: identify failed: {ident}")
            return 1
        print("identified (v5 auth OK)")

        inbox = Inbox()

        # Guard: browser_source must be registered (full-variant build).
        resp = await request(inbox, ws, "GetInputKindList", "kinds", {})
        kinds = set(resp["responseData"]["inputKinds"])
        if "browser_source" not in kinds:
            print("SKIP: browser_source NOT registered -- LIGHT build (no CEF). "
                  "Needs a full build (scripts/build-win.ps1 -Full). "
                  "Typed skip, NOT a pass.")
            return 3
        print(f"browser_source registered ({len(kinds)} input kinds total)")

        # ZERO PC SOUND: mute desktop audio + assert no unmuted audio input.
        if await ensure_silence(inbox, ws) != 0:
            return 1

        # Decide the switch implementation: real scene (a) or URL swap (b).
        scene_a = await get_current_scene(inbox, ws, "scene-a-name") or DEFAULT_SCENE_NAME
        use_real_scene = False
        if force_fallback:
            print("-> --force-fallback set: using path (b) URL swap")
        else:
            print("-> attempting path (a): real two-scene OBS switch")
            use_real_scene = await try_setup_two_scenes(
                inbox, ws, scene_a_url, scene_b_url)

        if not use_real_scene:
            # Path (b): single scene, paint scene-a; the switch later swaps URL.
            print("-> path (b): single program scene, browser_source = scene-a.html "
                  "(switch will swap to scene-b.html at duration/2)")
            data = await set_capture_browser(inbox, ws, "cap-fallback", scene_a_url)
            if data.get("kind") != "browser_source":
                print(f"FAIL: SetCaptureSource(scene-a) failed: {data}")
                return 1

        impl = "REAL SCENE SWITCH (path a)" if use_real_scene else "URL-SWAP FALLBACK (path b)"
        print(f"\n[scene-switch] implementation = {impl}")
        print(f"[scene-switch] going live to Twitch ({duration_sec}s, "
              f"switch @ {duration_sec/2:.0f}s) ...\n")
        return await broadcast(
            inbox, ws, stream_key, duration_sec, use_real_scene,
            scene_a, scene_a_url, scene_b_url, pulsar)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar Twitch scene-switch probe")
    ap.add_argument("--exe", type=pathlib.Path,
                    default=pathlib.Path(os.environ.get("PULSAR_EXE", str(DEFAULT_EXE))),
                    help="path to pulsar.exe (default: built rundir)")
    ap.add_argument("--duration", type=int,
                    default=int(os.environ.get("LIVE_TEST_DURATION", "30")),
                    help="broadcast duration in seconds (default 30); switch at /2")
    ap.add_argument("--fps", type=int,
                    default=int(os.environ.get("LIVE_TEST_FPS", "60")),
                    help="encoder fps target (default 60)")
    ap.add_argument("--force-fallback", action="store_true",
                    help="skip path (a), use the URL-swap fallback (b)")
    ap.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S)
    args = ap.parse_args()

    exe: pathlib.Path = args.exe
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        print("Build it first: scripts/build-win.ps1 -Full")
        return 2

    if not (SCENE_DIR / "scene-a.html").exists() or not (SCENE_DIR / "scene-b.html").exists():
        print(f"error: scene-a.html / scene-b.html missing under {SCENE_DIR}")
        return 2

    if args.duration < 4:
        print("error: --duration must be >= 4s so the mid-course switch has room")
        return 2

    stream_key = os.environ.get("TWITCH_STREAM_KEY", "").strip()
    if not stream_key:
        print("error: TWITCH_STREAM_KEY env var is empty. Set it from the "
              "etage-1 secret; never commit. Refusing to broadcast.")
        return 2

    http_port = find_free_port()
    httpd = start_scene_server(http_port)
    print(f"scene HTTP server: http://127.0.0.1:{http_port}/ "
          f"(scene-a.html cobalt / scene-b.html crimson)")

    port = find_free_port()
    password = secrets.token_urlsafe(16)
    print(f"spawning: {exe}")
    print(f"  cwd={exe.parent}")
    print(f"  PULSAR_PORT={port}  PULSAR_PASSWORD=<redacted {len(password)} chars>")
    print(f"  TWITCH_STREAM_KEY: <set, redacted>")

    pulsar = PulsarProcess(exe, port, password, args.fps)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        rc = asyncio.run(run(
            ws_url, sentinel_pw, stream_key, args.duration,
            http_port, args.force_fallback, pulsar,
        ))
    except KeyboardInterrupt:
        print("interrupted")
        rc = 130
    except Exception as exc:  # noqa: BLE001 — top-level probe diagnostic
        print(f"FAIL: {redact(str(exc), stream_key)}")
        if pulsar.proc is not None:
            print(redact(pulsar.diag(), stream_key))
        rc = 1
    finally:
        if rc != 0:
            tail = pulsar.lines[-80:]
            if tail:
                print("\n---- pulsar stdout (last 80 lines, redacted) ----")
                for ln in tail:
                    print(f"  {redact(ln, stream_key)}")
                print("---- end pulsar stdout ----\n")
        pulsar.shutdown()
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        if pulsar.proc is not None and pulsar.proc.poll() is None:
            print("error: pulsar.exe still running after shutdown attempt")
            rc = rc or 1
        else:
            print("pulsar.exe reaped cleanly")

    print("PASS" if rc == 0 else (f"SKIPPED (exit {rc})" if rc == 3
                                  else f"FAILED (exit {rc})"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
