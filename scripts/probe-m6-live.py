#!/usr/bin/env python3
"""
Pulsar M6 live probe -- the REAL Solar scene on air to Twitch (ADR 008 §8).

This is M4 (CEF scene -> RTMP -> Twitch, proven) but with the *real live
Solar page* in the browser_source instead of a local test scene. The Solar
page is served by Orion over the LSDP wire (through an SSH tunnel): it opens a
WebSocket to the gateway, fetches the content-hashed render bundle, and paints
the pushed scene at full fidelity. M6 proves that Pulsar's CEF can render that
*remote, gateway-backed* scene -- not a frame of test HTML -- and push it live.

Flow (ADR 008 §8, M6):
  1. Spawn pulsar.exe headless (self-spawn + reap, like M3/M4); v5 auth.
  2. SetCaptureSource(browser_source, url=<external Solar live URL>,
     1920x1080) via the `pulsar-scene` vendor -- this creates the managed
     CEF source `PulsarSceneSource` on the current frontend scene.
  3. PRE-FLIGHT NON-BLANK (the heart of M6, BEFORE going live):
     poll GetSourceScreenshot("PulsarSceneSource") until the decoded PNG is
     non-blank with real content (colour variance + non-background pixel
     ratio, the same guard as M3). The deadline is LONGER than M3's local
     test: the Solar page does WS LSDP connect + bundle fetch through the
     tunnel, which takes longer than serving a static local page. Save the
     PNG as proof (build/m6-live-scene.png). If it is still blank/black after
     the deadline -> DO NOT GO LIVE. Diagnose (WS LSDP failed? bundle 404?
     CORS? tunnel down?) and report. No broadcasting a blank scene.
  4. Only if non-blank: CreateDestination(twitch, $TWITCH_STREAM_KEY) ->
     StartDestination -> live. StartRecord in parallel for an offline proof.
  5. Poll metrics ~25 s: active=true, drop_ratio, bitrate, fps.
  6. StopDestination + StopRecord + RemoveDestination, reap pulsar.
     Idempotent, no orphan.

SECRET HANDLING: TWITCH_STREAM_KEY is read from the environment ONLY (set by
the caller from the etage-1 secret file). It is NEVER printed, logged, or
written anywhere. Any log line that could contain it is redacted to <KEY>.
The proof PNG is the rendered scene -- not a secret -- and is safe to save.

LICENSE INVARIANT (LICENSE-INVARIANTS.md #1/#2/#3, ADR 008 §3.1): this probe
talks to Pulsar over the WebSocket process boundary ONLY. It spawns
pulsar.exe as a separate OS process and exchanges nothing but obs-websocket v5
frames (+ `pulsar`/`pulsar-scene` vendor requests over that same wire). There
is NO FFI, no ctypes/cffi, no LoadLibrary of obs.dll / pulsar-browser.dll /
libcef.dll, no native import. CEF runs entirely inside the pulsar.exe process
tree. Pure aggregation -- Pulsar's GPL never crosses into this probe.

Usage (from the repo root, against the built rundir):
    pip install websockets
    export TWITCH_STREAM_KEY=...        # from etage-1 secret, NEVER committed
    python scripts/probe-m6-live.py
    python scripts/probe-m6-live.py --solar-url '<override>'   # tunnel URL
    python scripts/probe-m6-live.py --preflight-only           # no broadcast

Required env:
  TWITCH_STREAM_KEY   Twitch stream key (opaque; never logged)

Optional env / flags:
  SOLAR_SCENE_URL     override the Solar live URL (default = the tunnel URL)
  PULSAR_EXE / --exe  override pulsar.exe path
  LIVE_TEST_DURATION  broadcast seconds (default 25)
  LIVE_TEST_FPS       encoder fps target (default 60)

Exit codes:
  0  pass (pre-flight non-blank confirmed; if not --preflight-only, live ok)
  1  fail (Solar never rendered / broadcast assertion failed)
  2  config error (no key, no exe, bad args)
  3  typed skip (browser_source not registered -- LIGHT build, needs -Full)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from typing import Callable, Optional

# Force UTF-8 on stdout/stderr so a stray non-ASCII char in a diagnostic line
# can't crash the probe on a legacy Windows code page (cp1252). Harmless on a
# UTF-8 terminal. Best-effort: reconfigure exists on Python 3.7+.
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
    REPO_ROOT
    / "upstream"
    / "build_x64"
    / "rundir"
    / "RelWithDebInfo"
    / "bin"
    / "64bit"
    / "pulsar.exe"
)
BUILD_DIR = REPO_ROOT / "build"
PROOF_PNG = BUILD_DIR / "m6-live-scene.png"

# The live Solar page served by Orion over LSDP, reachable here through an SSH
# tunnel (host.html on :8099 -> gateway WS on :14000). The query carries the
# Orion LSDP wire URL + a *viewer* show-token (public, read-only) + broadcast
# mode. Overridable via SOLAR_SCENE_URL / --solar-url when the tunnel moves.
# NOTE: the token here is a VIEWER show-token (role=viewer), not a secret in
# the TWITCH_STREAM_KEY sense -- it only grants read-only subscription to the
# show stream. It is intentionally part of the default so the probe is
# runnable as-is against the standing tunnel.
DEFAULT_SOLAR_URL = (
    "http://127.0.0.1:8099/host.html"
    "?orion=ws%3A//127.0.0.1%3A14000/orion/api/v1/show/stream.lsdp"
    "%3Ftoken%3DeyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJ2aWV3ZXIiLCJyb2xlIjoidmlld2VyIiwiYXVkIjoiemFiZ2F0ZSIsImlzcyI6"
    "InphYmF1dGgiLCJleHAiOjE3ODA3MjA5MTZ9"
    ".oDbQNgDgaVfgvdkQau_gLCo_S6OkmD0HGo_n-boH0ok"
    "&mode=broadcast"
)

READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0
EVENT_SUBSCRIPTION_ALL = 0x7FF

# The managed source name the pulsar-scene plugin creates for the
# browser_source (plugins/pulsar-scene-source/src/plugin-main.cpp:60
# kCaptureSourceName = "PulsarSceneSource"). GetSourceScreenshot targets it.
CAPTURE_SOURCE_NAME = "PulsarSceneSource"
CANVAS_W = 1920
CANVAS_H = 1080

# Pre-flight: CEF must (a) spin up its render subprocess, (b) load host.html,
# (c) open the LSDP WS to the gateway through the tunnel, (d) fetch the
# content-hashed render bundle through the same tunnel, (e) lay out + paint.
# That is materially slower than M3's local static page -- give it a generous
# deadline. The first grabs are routinely blank; poll, never single-shot.
CAPTURE_POLL_DEADLINE_S = 45.0
CAPTURE_POLL_INTERVAL_S = 1.0

# Non-blank / content thresholds (mirror M3). A blank/single-colour frame has
# distinct=1 / all_same=True / nonbg=0 and fails all three. The real Solar
# scene clears them comfortably. Background-distance test is generic (the
# Solar scene background is unknown here), so we lean on distinct-colour count
# + a relaxed "non-background" measure against the modal colour.
MIN_DISTINCT_COLOURS = 12
MIN_NONBG_PIXEL_RATIO = 0.01   # >= 1% of pixels clearly off the modal colour
MODAL_MANHATTAN_TOL = 40

# Broadcast thresholds (mirror M4).
FRAME_DROP_RATIO_MAX = 0.05
POLL_INTERVAL_SEC = 5.0
DESTINATION_NAME = "pulsar-m6-live"

# StartDestination boot-race retry (ported verbatim from
# probe-twitch-live.py's START_DEST_* pattern, validated by Vigil on the
# scene-switch). The frontend streaming output can briefly be unavailable
# right after a scene-switch/boot; that ONE exact transient error is polled
# out within a bounded budget. Any OTHER error is a hard failure on the first
# attempt, and an exhausted budget is a hard failure too -- zero masking.
START_DEST_BOOT_ERROR   = "frontend streaming output unavailable"
START_DEST_RETRY_BUDGET = 20.0   # seconds to wait out the boot race
START_DEST_RETRY_DELAY  = 1.0    # poll cadence between attempts

LIVE_VOD_DIR = (BUILD_DIR / "m6-live-vod")

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
# Process management -- mirrors probe-browser-m3.py PulsarProcess.
# --------------------------------------------------------------------------
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
        self._pump_thread: Optional[threading.Thread] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        env["PULSAR_FPS"] = str(self.fps)
        env["PULSAR_RESOLUTION"] = f"{CANVAS_W}x{CANVAS_H}"
        env["PULSAR_VIDEO_BITRATE"] = "6000"
        LIVE_VOD_DIR.mkdir(parents=True, exist_ok=True)
        env["PULSAR_RECORD_DIR"] = str(LIVE_VOD_DIR)
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)

        creationflags = 0
        if os.name == "nt":
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        # --disable-gpu / --no-sandbox: forwarded to CEF via GetCommandLineW().
        # Without --disable-gpu CEF's GPU subprocess crashes at first frame in
        # a headless host (no compositor) and takes obs-browser down with it.
        # SW rasterization is the canonical headless-CEF config (same as M4).
        self.proc = subprocess.Popen(
            [str(self.exe), "--disable-gpu", "--no-sandbox"],
            cwd=str(self.exe.parent),  # MANDATORY: libobs resolves data/ from cwd
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
        self._pump_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._pump_thread.start()

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
# obs-websocket v5 plumbing -- mirrors probe-browser-m3.py / probe-twitch-live.
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


# --------------------------------------------------------------------------
# Pure-stdlib PNG decode + non-blank analysis (mirror probe-browser-m3.py).
# --------------------------------------------------------------------------
def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png(data: bytes) -> tuple[int, int, int, bytearray]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG (bad signature)")
    off = 8
    width = height = bit_depth = colour_type = interlace = 0
    idat = bytearray()
    while off + 8 <= len(data):
        (length,) = struct.unpack(">I", data[off : off + 4])
        ctype = data[off + 4 : off + 8]
        body = data[off + 8 : off + 8 + length]
        off += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, colour_type, _comp, _filt, interlace = (
                struct.unpack(">IIBBBBB", body)
            )
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
    if bit_depth != 8:
        raise ValueError(f"unsupported PNG bit depth {bit_depth} (want 8)")
    if interlace != 0:
        raise ValueError("interlaced PNG not supported")
    if colour_type == 2:
        channels = 3
    elif colour_type == 6:
        channels = 4
    else:
        raise ValueError(f"unsupported PNG colour type {colour_type} (want 2/6)")

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(width * height * channels)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        filt = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if filt == 0:
            pass
        elif filt == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filt == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif filt == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                c = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        else:
            raise ValueError(f"unknown PNG filter {filt}")
        out[y * stride : (y + 1) * stride] = line
        prev = line
    return width, height, channels, out


def analyse_frame(width: int, height: int, channels: int, px: bytearray) -> dict:
    """Blank/content metrics. The Solar scene background is unknown a priori,
    so 'non-background' is measured against the MODAL (most common) colour --
    a blank frame's modal colour IS the whole frame, so its nonbg_ratio is ~0
    and distinct is ~1. A real scene has many distinct colours and a healthy
    share of pixels away from whatever the dominant background is."""
    total = width * height
    if total == 0:
        return {"distinct": 0, "nonbg_ratio": 0.0, "sampled": 0, "all_same": True,
                "modal": None}

    step = max(1, total // 40000)
    counts: dict[int, int] = {}
    samples: list[tuple[int, int, int]] = []
    first_rgb: Optional[tuple[int, int, int]] = None
    all_same = True
    for idx in range(0, total, step):
        base = idx * channels
        r, g, b = px[base], px[base + 1], px[base + 2]
        key = (r << 16) | (g << 8) | b
        counts[key] = counts.get(key, 0) + 1
        samples.append((r, g, b))
        if first_rgb is None:
            first_rgb = (r, g, b)
        elif (r, g, b) != first_rgb:
            all_same = False

    sampled = len(samples)
    modal_key = max(counts, key=lambda k: counts[k])
    mr, mg, mb = (modal_key >> 16) & 0xFF, (modal_key >> 8) & 0xFF, modal_key & 0xFF
    nonbg = sum(
        1 for (r, g, b) in samples
        if abs(r - mr) + abs(g - mg) + abs(b - mb) > MODAL_MANHATTAN_TOL
    )
    return {
        "distinct": len(counts),
        "nonbg_ratio": nonbg / sampled if sampled else 0.0,
        "sampled": sampled,
        "all_same": all_same,
        "modal": (mr, mg, mb),
    }


def frame_is_content(metrics: dict) -> bool:
    if metrics["all_same"]:
        return False
    if metrics["distinct"] < MIN_DISTINCT_COLOURS:
        return False
    if metrics["nonbg_ratio"] < MIN_NONBG_PIXEL_RATIO:
        return False
    return True


def _strip_data_uri(image_data: str) -> bytes:
    comma = image_data.find(",")
    payload = image_data[comma + 1 :] if comma != -1 else image_data
    return base64.b64decode(payload)


# --------------------------------------------------------------------------
# M6 pre-flight: render the live Solar scene + capture a non-blank frame.
# --------------------------------------------------------------------------
async def preflight_non_blank(inbox: Inbox, ws, solar_url: str) -> tuple[int, dict]:
    """Returns (rc, last_metrics). rc=0 means a non-blank Solar frame was
    captured + saved to PROOF_PNG. rc=1 means the deadline elapsed blank."""
    # 1. SetCaptureSource(browser_source, <Solar live URL>). This creates the
    #    managed CEF source on the current frontend scene.
    print("-> SetCaptureSource(browser_source, <Solar live URL via tunnel>)")
    r = await vendor_call(inbox, ws, "set-capture", "pulsar-scene",
        "SetCaptureSource", {
            "kind": "browser_source",
            "url": solar_url,
            "width": CANVAS_W,
            "height": CANVAS_H,
            "fps": 60,
            "reroute_audio": True,
        })
    data = vendor_response_data(r)
    if data.get("kind") != "browser_source":
        status = vendor_request_status(r)
        print(f"FAIL: SetCaptureSource did not return browser_source: "
              f"status={status} data={data}")
        return 1, {}
    print(f"   <- kind=browser_source {data.get('width')}x{data.get('height')} "
          f"removed_prior={data.get('removed_prior')}")

    # 1b. GetCaptureSource confirms the active snapshot.
    r = await vendor_call(inbox, ws, "get-capture", "pulsar-scene",
        "GetCaptureSource", {})
    got = vendor_response_data(r)
    if got.get("kind") != "browser_source":
        print(f"FAIL: GetCaptureSource snapshot not browser_source: {got}")
        return 1, {}
    print("   <- GetCaptureSource confirms active snapshot")

    # 2. Poll GetSourceScreenshot(PulsarSceneSource) until CEF has rendered
    #    the live Solar scene. This is slower than M3: WS LSDP + bundle fetch
    #    through the tunnel. The first grabs are blank -- poll, don't
    #    single-shot.
    print(f"-> polling GetSourceScreenshot({CAPTURE_SOURCE_NAME!r}) until "
          f"non-blank (deadline {CAPTURE_POLL_DEADLINE_S:.0f}s; tunnel render "
          f"is slower than the local M3 page) ...")
    deadline = time.monotonic() + CAPTURE_POLL_DEADLINE_S
    attempt = 0
    last_metrics: dict = {}
    last_png: Optional[bytes] = None
    last_dims = (0, 0)
    while time.monotonic() < deadline:
        attempt += 1
        r = await request(inbox, ws, "GetSourceScreenshot", f"shot-{attempt}", {
            "sourceName": CAPTURE_SOURCE_NAME,
            "imageFormat": "png",
            "imageWidth": CANVAS_W,
            "imageHeight": CANVAS_H,
        })
        if not r["requestStatus"]["result"]:
            code = r["requestStatus"].get("code")
            comment = r["requestStatus"].get("comment", "")
            if attempt == 1 or attempt % 5 == 0:
                print(f"   attempt {attempt}: not ready (code={code} {comment!r})")
            await asyncio.sleep(CAPTURE_POLL_INTERVAL_S)
            continue

        png = _strip_data_uri(r["responseData"]["imageData"])
        try:
            w, h, ch, px = decode_png(png)
        except Exception as exc:  # noqa: BLE001
            print(f"   attempt {attempt}: PNG decode failed: {exc}")
            await asyncio.sleep(CAPTURE_POLL_INTERVAL_S)
            continue

        metrics = analyse_frame(w, h, ch, px)
        last_metrics, last_png, last_dims = metrics, png, (w, h)
        if attempt == 1 or attempt % 3 == 0:
            print(f"   attempt {attempt}: {w}x{h} ch={ch} "
                  f"distinct={metrics['distinct']} "
                  f"nonbg={metrics['nonbg_ratio']*100:.2f}% "
                  f"all_same={metrics['all_same']} modal={metrics['modal']}")
        if frame_is_content(metrics):
            elapsed_s = CAPTURE_POLL_DEADLINE_S - (deadline - time.monotonic())
            print(f"   CONTENT at attempt {attempt} (t~{elapsed_s:.1f}s): "
                  f"{w}x{h} distinct={metrics['distinct']} "
                  f"nonbg={metrics['nonbg_ratio']*100:.2f}%")
            BUILD_DIR.mkdir(parents=True, exist_ok=True)
            PROOF_PNG.write_bytes(png)
            print(f"   PROOF PNG saved: {PROOF_PNG} ({len(png):,} bytes)")
            if (w, h) != (CANVAS_W, CANVAS_H):
                print(f"   note: captured dims {w}x{h} != requested "
                      f"{CANVAS_W}x{CANVAS_H} (scale-to-inner)")
            return 0, metrics
        await asyncio.sleep(CAPTURE_POLL_INTERVAL_S)

    # Deadline hit blank -- diagnose precisely. No broadcast.
    print("\nFAIL: the live Solar scene never produced a non-blank frame "
          "within the deadline. NOT going live.")
    if not last_metrics:
        print("  No screenshot ever decoded -- the browser source produced no "
              "frame at all. Likely: CEF render subprocess missing, OR "
              "host.html unreachable from CEF (tunnel :8099 down).")
    else:
        w, h = last_dims
        print(f"  Last frame: {w}x{h} distinct={last_metrics['distinct']} "
              f"nonbg={last_metrics['nonbg_ratio']*100:.2f}% "
              f"all_same={last_metrics['all_same']} modal={last_metrics['modal']}")
        if last_metrics["all_same"]:
            print("  The frame is a SOLID colour (blank). CEF loaded host.html "
                  "but Solar never painted a scene. Most likely the Solar "
                  "runtime could not render: LSDP WS to the gateway (:14000) "
                  "failed (token/auth), OR the render-bundle fetch 404'd, OR a "
                  "CORS rejection. Inspect pulsar stdout for browser console "
                  "lines + check the tunnel + gateway.")
        else:
            print("  The frame has variance but did not clear the content "
                  "thresholds -- partial/garbled Solar render. Inspect the "
                  "saved PNG.")
        if last_png is not None:
            BUILD_DIR.mkdir(parents=True, exist_ok=True)
            PROOF_PNG.write_bytes(last_png)
            print(f"  last frame saved for inspection: {PROOF_PNG}")
    return 1, last_metrics


# --------------------------------------------------------------------------
# M6 broadcast: push the live Solar scene to Twitch + poll metrics.
# --------------------------------------------------------------------------
def _scan_rtmp_diagnostic(lines: list[str]) -> list[str]:
    """Pull the RTMP/ingest lines from pulsar stdout so a broadcast failure
    is self-explanatory: a clean 'started=true' that immediately goes
    active=False is almost always Twitch ingest refusing the session (bad/
    expired/duplicate stream key), surfaced here as 'remote host closed
    connection' / 'failed: -N'. Surfacing this distinguishes a Twitch-side
    rejection from a Pulsar-side fault."""
    hits = []
    for ln in lines:
        low = ln.lower()
        if ("rtmp" in low or "remote host closed" in low) and (
            "connecting to rtmp" in low
            or "remote host closed" in low
            or "failed:" in low
            or "connection to" in low
        ):
            hits.append(ln.strip())
    return hits


async def start_destination_with_retry(inbox: Inbox, ws, dest_id: str,
                                       stream_key: str) -> bool:
    """StartDestination, polling out the post-boot/scene-switch race.

    Ported from probe-twitch-live.py (START_DEST_* pattern). Returns True once
    the destination reports started=true. Returns False on a HARD failure:
    either a non-transient StartDestination error (failed on the first
    attempt) OR the boot race never cleared within START_DEST_RETRY_BUDGET.

    Strict semantics (validated by Vigil on the scene-switch): the retry fires
    ONLY when the error is exactly START_DEST_BOOT_ERROR. Any other error ends
    the loop immediately. No error string is ever masked or swallowed.
    """
    deadline = time.time() + START_DEST_RETRY_BUDGET
    attempt = 0
    while True:
        attempt += 1
        r = await vendor_call(inbox, ws, f"start-dest-{attempt}", "pulsar",
            "StartDestination", {"id": dest_id})
        sd = vendor_response_data(r)
        if sd.get("started"):
            print(f"-> StartDestination started=true -- LIVE on Twitch "
                  f"(attempt #{attempt})")
            return True
        err = str(sd.get("error", ""))
        transient = (err == START_DEST_BOOT_ERROR)
        if transient and time.time() < deadline:
            print(f"   start-dest attempt #{attempt}: streaming output not "
                  f"ready yet ('{err}'), retrying in {START_DEST_RETRY_DELAY}s")
            await asyncio.sleep(START_DEST_RETRY_DELAY)
            continue
        # Either a non-transient error, or the boot race never cleared within
        # budget -- both are hard failures.
        status = vendor_request_status(r)
        reason = ("boot race unresolved after "
                  f"{START_DEST_RETRY_BUDGET}s ({attempt} attempts)"
                  if transient else "not started")
        print(f"FAIL: StartDestination {reason}; "
              f"status={redact(json.dumps(status), stream_key)}")
        return False


async def broadcast(inbox: Inbox, ws, stream_key: str, duration_sec: int,
                    pulsar: "PulsarProcess") -> int:
    # 1. CreateDestination(twitch). The key is passed opaquely; it never
    #    appears in any print().
    r = await vendor_call(inbox, ws, "create-dest", "pulsar",
        "CreateDestination", {
            "name": DESTINATION_NAME,
            "kind": "twitch",
            "key": stream_key,
        })
    dest_data = vendor_response_data(r)
    dest_id = dest_data.get("id")
    if not dest_id:
        status = vendor_request_status(r)
        # redact in case the comment echoes the key
        print(f"FAIL: CreateDestination returned no id; "
              f"status={redact(json.dumps(status), stream_key)}")
        return 1
    print(f"-> CreateDestination(twitch) id={dest_id}")

    # 2. StartDestination -> live, with the bounded anti-boot-race retry
    #    (the frontend streaming output can be briefly unavailable right after
    #    a boot/scene-switch -- see start_destination_with_retry). A hard
    #    failure rolls the destination back, exactly as the single-shot did.
    if not await start_destination_with_retry(inbox, ws, dest_id, stream_key):
        await vendor_call(inbox, ws, "rm-dest", "pulsar",
            "RemoveDestination", {"id": dest_id})
        return 1

    # 2b. StartRecord -- local MP4 as an offline broadcast proof.
    recording = False
    r = await request(inbox, ws, "StartRecord", "start-rec")
    if r.get("requestStatus", {}).get("result"):
        recording = True
        print(f"-> StartRecord ok (writing under {LIVE_VOD_DIR})")
    else:
        print(f"   warn: StartRecord declined: {r.get('requestStatus')}")

    # 3. Poll metrics.
    rc = 0
    start_t = time.time()
    poll = 0
    adaptive_seen = 0
    while time.time() - start_t < duration_sec:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        poll += 1
        elapsed = int(time.time() - start_t)

        r = await vendor_call(inbox, ws, f"get-dest-{poll}", "pulsar",
            "GetDestinations", {})
        lst = vendor_response_data(r).get("destinations", [])
        ours = next((d for d in lst if d.get("id") == dest_id), None)
        if not ours or not ours.get("active"):
            print(f"FAIL: destination not active at poll #{poll}: {ours}")
            # Give pulsar's log pump a beat, then surface the RTMP cause.
            await asyncio.sleep(0.3)
            rtmp = _scan_rtmp_diagnostic(pulsar.lines)
            if rtmp:
                print("  RTMP ingest diagnostic (pulsar log):")
                for ln in rtmp[-6:]:
                    print(f"    {redact(ln, stream_key)}")
                if any("remote host closed" in ln.lower()
                       or "failed:" in ln.lower() for ln in rtmp):
                    print("  -> Twitch ingest CLOSED the RTMP connection right "
                          "after connect. This is a Twitch-side rejection "
                          "(invalid/expired/revoked stream key, OR a stream is "
                          "already live on this channel, OR wrong ingest "
                          "endpoint) -- NOT a Pulsar fault. The scene rendered "
                          "and encoded correctly (see the proof PNG + local "
                          "MP4); only the Twitch handshake was refused.")
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

        print(f"   poll #{poll} t={elapsed}s active=true samples={samples} "
              f"drop_ratio={drop_ratio:.4f} bitrate={cur_kbps} fps={fps_str}")

        if drop_ratio > FRAME_DROP_RATIO_MAX:
            print(f"FAIL: frame drop ratio {drop_ratio:.4f} > "
                  f"{FRAME_DROP_RATIO_MAX} at poll #{poll}")
            rc = 1
            break

    # 4. Stop cleanly (best-effort even on a failed poll).
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
        print(f"FAIL: adaptive worker never reported samples "
              f"(saw {adaptive_seen}) -- encoder may not have produced frames")
        rc = 1
    if rc == 0:
        print(f"-> broadcast clean: adaptive_samples_seen={adaptive_seen}")
    return rc


async def run(url: str, password: str, solar_url: str, stream_key: str,
              duration_sec: int, preflight_only: bool,
              pulsar: "PulsarProcess") -> int:
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
                  "M6 needs a full build (scripts/build-win.ps1 -Full). "
                  "Typed skip, NOT a pass.")
            return 3
        print(f"browser_source registered ({len(kinds)} input kinds total)")

        # --- PRE-FLIGHT: the heart of M6 ---
        rc, _metrics = await preflight_non_blank(inbox, ws, solar_url)
        if rc != 0:
            return 1
        print("\n[M6] PRE-FLIGHT PASSED -- the live Solar scene rendered "
              "non-blank in Pulsar's CEF.")

        if preflight_only:
            print("[M6] --preflight-only set: skipping broadcast.")
            return 0

        # --- BROADCAST: only after non-blank confirmed ---
        print("\n[M6] going live to Twitch ...")
        return await broadcast(inbox, ws, stream_key, duration_sec, pulsar)


def pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar M6 live Solar -> Twitch probe")
    ap.add_argument("--exe", type=pathlib.Path,
                    default=pathlib.Path(os.environ.get("PULSAR_EXE", str(DEFAULT_EXE))),
                    help="path to pulsar.exe (default: built rundir)")
    ap.add_argument("--solar-url", type=str,
                    default=os.environ.get("SOLAR_SCENE_URL", DEFAULT_SOLAR_URL),
                    help="live Solar page URL (default: the standing tunnel URL)")
    ap.add_argument("--duration", type=int,
                    default=int(os.environ.get("LIVE_TEST_DURATION", "25")),
                    help="broadcast duration in seconds (default 25)")
    ap.add_argument("--fps", type=int,
                    default=int(os.environ.get("LIVE_TEST_FPS", "60")),
                    help="encoder fps target (default 60)")
    ap.add_argument("--preflight-only", action="store_true",
                    help="render + verify the Solar scene but do NOT broadcast")
    ap.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S)
    args = ap.parse_args()

    exe: pathlib.Path = args.exe
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        print("Build it first: scripts/build-win.ps1 -Full")
        return 2

    stream_key = os.environ.get("TWITCH_STREAM_KEY", "").strip()
    if not args.preflight_only and not stream_key:
        print("error: TWITCH_STREAM_KEY env var is empty (required unless "
              "--preflight-only). Set it from the etage-1 secret; never commit.")
        return 2

    port = pick_free_port()
    password = secrets.token_urlsafe(16)
    print(f"spawning: {exe}")
    print(f"  cwd={exe.parent}")
    print(f"  PULSAR_PORT={port}  PULSAR_PASSWORD=<redacted {len(password)} chars>")
    print(f"  solar-url host: {args.solar_url.split('?')[0]}  (query redacted)")
    print(f"  TWITCH_STREAM_KEY: {'<set, redacted>' if stream_key else '<unset>'}")

    pulsar = PulsarProcess(exe, port, password, args.fps)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        rc = asyncio.run(run(
            ws_url, sentinel_pw, args.solar_url, stream_key,
            args.duration, args.preflight_only, pulsar,
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
        # On failure, dump pulsar stdout (redacted) so a reviewer sees what the
        # engine + CEF reported (browser console lines surface here).
        if rc != 0:
            tail = pulsar.lines[-80:]
            if tail:
                print("\n---- pulsar stdout (last 80 lines, redacted) ----")
                for ln in tail:
                    print(f"  {redact(ln, stream_key)}")
                print("---- end pulsar stdout ----\n")
        pulsar.shutdown()
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
