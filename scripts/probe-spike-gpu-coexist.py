#!/usr/bin/env python3
r"""SPIKE-GPU — ADR 003 Amendment 4 §A4.6 build gate #1 (issue #61).

THE ONE QUESTION THIS SPIKE TRANCHES
====================================
On the real, GPU-ON interactive desktop (pulsar.exe spawned **WITHOUT**
``--disable-gpu``), do these two GPU consumers render **SIMULTANEOUSLY**, both
NON-BLACK, in the same program output of one pulsar.exe:

  (A) a ``monitor_capture`` source — DXGI/D3D11 desktop duplication
      (``DuplicateOutput1``), and
  (B) a CEF ``browser_source`` (the Solar overlay mechanism — the fork's
      ``ENABLE_BROWSER_SHARED_TEXTURE`` D3D11 shared-texture path).

PASS  = both planes are live at once (the precondition of the Q3=(b) design:
        "real capture + Solar overlay" — A4.4/A4.6). The build may proceed.
FAIL  = one plane is black/absent. A HARD FINDING that reopens the mechanism
        (A4.6: "FAIL = reopen mechanism"). The exact diagnostic (which plane is
        black, and the log marker) is the deliverable, NOT a probe bug.

WHY THE FLAG IS THE WHOLE POINT (do not "fix" this by adding --disable-gpu)
==========================================================================
``--disable-gpu`` is a HOST process command-line flag. CEF reads it via
``GetCommandLineW()`` at ``CefInitialize`` and propagates it to every CEF
subprocess (``probe-twitch-live.py:156-165`` documents this). So passing
``--disable-gpu`` to pulsar.exe does TWO things at once:
  1. it forces CEF to software rasterisation, AND
  2. it disables the HOST's GPU — which **breaks DXGI desktop duplication**
     (``887A0004 DXGI_ERROR_UNSUPPORTED``), so ``monitor_capture`` goes all
     black (the exact failure ``probe-m10-canvas-live.py:743-754`` documents).

The prior black-frame runs all carried ``--disable-gpu`` (M8/M9 headless
inheritance, ``probe-m10-canvas-live.py:300``). This spike removes it. The
risk it probes is the INVERSE one: GPU-on, CEF's own GPU subprocess might
crash ("GPU process isn't usable. Goodbye." — the fork README's documented
failure mode for a display-less host) or fail to paint while the host holds
the GPU for DXGI duplication. We must SEE both render before trusting Q3=(b).

We keep ``--no-sandbox`` (the fork is built without the CEF sandbox SDK; the
CEF renderer subprocess cannot init otherwise — ``browser-app.cpp:68-71``).
That is NOT the variable under test. ``--disable-gpu`` is the only variable.

WHAT THE FORK SOURCE ALREADY TELLS US (read alongside the verdict)
==================================================================
- The fork's ``OnBeforeCommandLineProcessing`` (``browser-app.cpp:66-105``)
  does NOT unconditionally append ``--disable-gpu`` (the plugin README is
  STALE on this point). It only appends ``disable-gpu-compositing``, and only
  when ``shared_texture_available`` is false. So GPU-on, the CEF page still
  renders via OSR; the open question is whether its GPU subprocess survives.
- ``obs_module_load`` (``obs-browser-plugin.cpp:760-771``) reads
  ``BrowserHWAccel`` from ``obs_get_private_data()``; pulsar-headless never
  calls ``obs_set_private_data`` for it, so it defaults FALSE → CEF takes the
  OSR (non-shared-texture) path, gets ``disable-gpu-compositing``, but STILL
  PAINTS. The host graphics module is ``libobs-d3d11.dll``
  (``pulsar-headless/main.cpp:133``) which owns the D3D11 device DXGI
  duplication needs. The plausibility note (``obs-browser-plugin.cpp:760-766``)
  is exactly the hypothesis this spike must confirm on the real box.

WHAT THIS SPIKE IS NOT
======================
NO Twitch broadcast, no VPS, no Blue leaf, no stinger, no scene-switch. This
is a LOCAL RENDER spike: one scene, two sources, one captured frame, two
colour analyses. It reuses the proven spawn/ready/reap + PNG-decode/colour-
analysis machinery from ``probe-m10-canvas-live.py`` / ``probe-browser-m3.py``
and the ``monitor_capture`` display enumeration from ``m10_setup.py`` —
verbatim logic, but spawned WITHOUT ``--disable-gpu``.

Exit codes (probe-family convention):
  0  PASS — both planes non-black simultaneously (GPU-on coexistence proven).
  1  FAIL — a hard finding: one plane is black/absent GPU-on (with diagnostic).
  2  config/env error (pulsar.exe missing, bad args).
  3  typed skip — monitor_capture or browser_source not registered (LIGHT
     build, no CEF / no win-capture). NOT a pass.

Usage — RUN ON THE REAL INTERACTIVE GPU-ON DESKTOP (this is mandatory):
    pip install websockets
    # The verdict run (GPU-on — the actual gate):
    python scripts/probe-spike-gpu-coexist.py
    # Control run — confirm --disable-gpu is the culprit (expects monitor_capture
    # black; proves the flag, not the box, broke the prior runs):
    python scripts/probe-spike-gpu-coexist.py --disable-gpu-control

NOTE ON WHERE THIS MUST RUN: DXGI desktop duplication only succeeds in a real
interactive desktop session bound to the GPU. In a non-interactive / service /
RDP / CI context, DuplicateOutput1 fails 887A0004 and monitor_capture is black
regardless of the GPU flag — so a FAIL there is inconclusive. The verdict is
only meaningful on the porteur's own logged-in desktop. Forge can only prove
the spike PARSES and LAUNCHES up to the capture; the porteur reads the verdict.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import http.server
import json
import os
import pathlib
import re
import secrets as _secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from typing import Any, Callable, Optional

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

# Reuse the #60 setup harness for monitor_capture display enumeration verbatim
# (U1/#56 — the device-id-vs-index resolution). We do NOT re-implement it.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import m10_setup  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    REPO_ROOT / "upstream" / "build_x64" / "rundir" / "RelWithDebInfo"
    / "bin" / "64bit" / "pulsar.exe"
)
BUILD_DIR = REPO_ROOT / "build"
FRAMES_DIR = BUILD_DIR / "spike-gpu-frames"

READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0
EVENT_SUBSCRIPTION_ALL = 0x7FF
CANVAS_W = 1920
CANVAS_H = 1080
RESOURCE_ALREADY_EXISTS = 601

MONITOR_CAPTURE_KIND = "monitor_capture"
BROWSER_KIND = "browser_source"

SPIKE_SCENE = "spike-gpu-coexist-scene"
CAPTURE_INPUT = "spike-monitor-capture"
BROWSER_INPUT = "spike-browser-overlay"

# The browser_source occupies the bottom-right quadrant so BOTH planes are
# visible in the same program frame: the capture fills the canvas, the overlay
# covers a partial region. We analyse the two regions separately.
OVERLAY_W = CANVAS_W // 2          # right half
OVERLAY_H = CANVAS_H // 2          # bottom half
OVERLAY_X = CANVAS_W - OVERLAY_W   # left edge of the overlay region
OVERLAY_Y = CANVAS_H - OVERLAY_H   # top edge of the overlay region

# CEF first-paint is async; the first screenshots are routinely blank. Poll.
CAPTURE_POLL_DEADLINE_S = 35.0
CAPTURE_POLL_INTERVAL_S = 0.75

# A pixel counts as "black" (a failed plane) if it is within this Manhattan
# distance of pure black. DXGI failure / unpainted CEF surface → all-black.
BLACK_MANHATTAN_TOL = 18
# A plane is "live" only if a meaningful share of its pixels are clearly
# non-black AND it shows real colour variety (a uniform grey would otherwise
# squeak past a single threshold). Kept generous — a real desktop / a bright
# CEF page clear these comfortably; an all-black plane clears neither.
MIN_NONBLACK_RATIO = 0.05
MIN_DISTINCT_COLOURS = 12

# The CEF overlay page: a FRANK, fully-opaque colour the analysis can find by
# hue, plus animated text so a stale/blank surface is obvious. The signature
# colour is magenta (#ff19c8) — far from any plausible desktop background and
# from pure black, so its presence in the overlay region proves CEF painted.
SIGNATURE_RGB = (0xFF, 0x19, 0xC8)
SIGNATURE_TOL = 80  # Manhattan tolerance for "this pixel is the signature"
MIN_SIGNATURE_RATIO = 0.10  # >=10% of overlay-region pixels near the signature

OVERLAY_PAGE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>SPIKE-GPU</title>
<style>
  html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;
    background:#ff19c8;}
  .stage{width:100vw;height:100vh;display:flex;flex-direction:column;
    align-items:center;justify-content:center;
    font-family:Arial,Helvetica,sans-serif;}
  .title{color:#0a0a0a;font-size:84px;font-weight:900;letter-spacing:3px;
    text-shadow:0 0 18px rgba(255,255,255,0.6);}
  .sub{color:#160016;font-size:30px;margin-top:18px;letter-spacing:2px;}
  .pulse{margin-top:28px;width:50%;height:18px;border-radius:9px;
    background:#13f0ff;animation:b 1s ease-in-out infinite alternate;}
  @keyframes b{from{opacity:.25;transform:scaleX(.6);}to{opacity:1;transform:scaleX(1);}}
</style></head>
<body><div class="stage">
  <div class="title">SOLAR OVERLAY</div>
  <div class="sub">CEF GPU-ON COEXISTENCE PROBE</div>
  <div class="pulse"></div>
</div></body></html>
"""


# --------------------------------------------------------------------------
# Local HTTP server for the CEF page (mirrors probe-browser-m3.py).
# --------------------------------------------------------------------------
class _PageHandler(http.server.BaseHTTPRequestHandler):
    page_bytes: bytes = OVERLAY_PAGE_HTML.encode("utf-8")

    def do_GET(self) -> None:  # noqa: N802 — http.server contract
        if self.path in ("/", "/overlay.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(self.page_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(self.page_bytes)
        else:
            self.send_error(404, "not found")

    def log_message(self, *args) -> None:  # silence per-request stderr spam
        return


class LocalPageServer:
    def __init__(self) -> None:
        self.httpd: Optional[http.server.ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.port: int = 0

    def start(self) -> str:
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.port}/overlay.html"

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None


# --------------------------------------------------------------------------
# Process management — spawn/ready/reap. IDENTICAL lifecycle to the M10 probe,
# with ONE deliberate difference: the spawn argv is GPU-on by default.
# --------------------------------------------------------------------------
class PulsarProcess:
    def __init__(self, exe: pathlib.Path, port: int, password: str,
                 disable_gpu: bool) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.disable_gpu = disable_gpu
        self.proc: Optional[subprocess.Popen] = None
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        env["PULSAR_RESOLUTION"] = f"{CANVAS_W}x{CANVAS_H}"
        # No window/mic/process-audio capture — the two sources are the test.
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)
        env.pop("PULSAR_PROCESS_AUDIO_NAME", None)

        # === THE VARIABLE UNDER TEST ===
        # GPU-ON (default): argv = [exe, --no-sandbox]. NO --disable-gpu. This
        # is the whole spike: let the host keep its GPU so DXGI desktop
        # duplication works for monitor_capture, AND let CEF spin its own GPU
        # subprocess. --no-sandbox is REQUIRED by the fork (no sandbox SDK,
        # browser-app.cpp:68-71) and is NOT the variable.
        # --disable-gpu-control: the inverse — re-add the flag to reproduce the
        # known black-frame failure and prove the flag (not the box) caused it.
        argv = [str(self.exe), "--no-sandbox"]
        if self.disable_gpu:
            argv.insert(1, "--disable-gpu")

        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            argv,
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
# obs-websocket v5 plumbing (mirrors m10_setup / probe-browser-m3).
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


def req_ok(resp: dict) -> bool:
    return bool(resp.get("requestStatus", {}).get("result"))


def req_code(resp: dict) -> Optional[int]:
    return resp.get("requestStatus", {}).get("code")


# --------------------------------------------------------------------------
# Pure-stdlib PNG decode (mirrors probe-browser-m3 / probe-m10). No PIL/numpy.
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


# --------------------------------------------------------------------------
# Region analysis — the heart of the verdict. Analyse a rectangular region of
# the decoded frame for "is this plane LIVE (non-black) or BLACK?" and, for the
# overlay region, "is the CEF signature colour present?".
# --------------------------------------------------------------------------
def analyse_region(
    width: int, height: int, channels: int, px: bytearray,
    x0: int, y0: int, x1: int, y1: int,
) -> dict:
    """Metrics over the pixels in [x0,x1) x [y0,y1): non-black ratio, distinct
    colour count, and the share of pixels near the CEF signature colour."""
    x0 = max(0, min(x0, width))
    x1 = max(0, min(x1, width))
    y0 = max(0, min(y0, height))
    y1 = max(0, min(y1, height))
    rw, rh = x1 - x0, y1 - y0
    total = rw * rh
    if total <= 0:
        return {"distinct": 0, "nonblack_ratio": 0.0, "signature_ratio": 0.0,
                "sampled": 0, "all_black": True}
    # Subsample to ~40k pixels for speed.
    step = max(1, total // 40000)
    distinct: set[int] = set()
    nonblack = 0
    signature = 0
    sampled = 0
    sr, sg, sb = SIGNATURE_RGB
    seen = 0
    for ry in range(rh):
        row_base = ((y0 + ry) * width + x0) * channels
        for rx in range(rw):
            seen += 1
            if (seen - 1) % step != 0:
                continue
            base = row_base + rx * channels
            r, g, b = px[base], px[base + 1], px[base + 2]
            sampled += 1
            distinct.add((r << 16) | (g << 8) | b)
            if r + g + b > BLACK_MANHATTAN_TOL:
                nonblack += 1
            if abs(r - sr) + abs(g - sg) + abs(b - sb) <= SIGNATURE_TOL:
                signature += 1
    return {
        "distinct": len(distinct),
        "nonblack_ratio": nonblack / sampled if sampled else 0.0,
        "signature_ratio": signature / sampled if sampled else 0.0,
        "sampled": sampled,
        "all_black": nonblack == 0,
    }


def plane_is_live(metrics: dict) -> bool:
    """A plane (capture region) is LIVE if a real share of pixels are non-black
    and it carries genuine colour variety — i.e. NOT an all-black DXGI-failed
    surface and NOT a uniform fill."""
    if metrics["all_black"]:
        return False
    if metrics["nonblack_ratio"] < MIN_NONBLACK_RATIO:
        return False
    if metrics["distinct"] < MIN_DISTINCT_COLOURS:
        return False
    return True


def overlay_is_live(metrics: dict) -> bool:
    """The CEF overlay region is LIVE if the signature colour is present (CEF
    painted our magenta page) AND the region is non-black."""
    if metrics["all_black"]:
        return False
    if metrics["signature_ratio"] < MIN_SIGNATURE_RATIO:
        return False
    return True


def _strip_data_uri(image_data: str) -> bytes:
    comma = image_data.find(",")
    payload = image_data[comma + 1 :] if comma != -1 else image_data
    return base64.b64decode(payload)


# --------------------------------------------------------------------------
# Log-marker grep — the second half of the verdict (the WHY behind a black
# plane). Scans the captured pulsar stdout for the DXGI duplication failure and
# the CEF GPU-process crash signatures the ADR/README name.
# --------------------------------------------------------------------------
DXGI_FAIL_MARKERS = [
    "887A0004",            # DXGI_ERROR_UNSUPPORTED — DuplicateOutput1 refused
    "DuplicateOutput1",    # the duplication call that fails when GPU is off
    "DXGI_ERROR",
    "failed to get duplicator",
    "Failed to duplicate output",
]
CEF_CRASH_MARKERS = [
    "GPU process isn't usable",   # README's documented headless CEF crash
    "Goodbye",                    # "...GPU process isn't usable. Goodbye."
    "Webpage has crashed",        # the user-visible symptom
    "GPU process exited",
    "gpu_data_manager",
]


def scan_log_markers(lines: list[str]) -> dict[str, list[str]]:
    dxgi_hits: list[str] = []
    cef_hits: list[str] = []
    for ln in lines:
        for m in DXGI_FAIL_MARKERS:
            if m.lower() in ln.lower():
                dxgi_hits.append(ln.strip())
                break
        for m in CEF_CRASH_MARKERS:
            if m.lower() in ln.lower():
                cef_hits.append(ln.strip())
                break
    return {"dxgi": dxgi_hits, "cef": cef_hits}


# --------------------------------------------------------------------------
# The spike round-trip: build the scene, capture, analyse, verdict.
# --------------------------------------------------------------------------
async def run_spike(url: str, password: str, page_url: str,
                    disable_gpu: bool) -> int:
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
            identify_d["authentication"] = compute_auth(password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ident.get("op") != 2:
            print(f"error: identify failed: {ident}")
            return 1
        print("identified (v5 auth OK)")

        inbox = Inbox()

        # --- TYPED SKIP guard: both source kinds must be registered ---
        resp = await request(inbox, ws, "GetInputKindList", "kinds", {})
        kinds = set(resp["responseData"]["inputKinds"])
        missing = {MONITOR_CAPTURE_KIND, BROWSER_KIND} - kinds
        if missing:
            print(f"SKIP: input kind(s) {sorted(missing)} NOT registered — LIGHT "
                  "build (no CEF / no win-capture). SPIKE-GPU needs a full build "
                  "(scripts/build-win.ps1 -Full). Typed skip, NOT a pass.")
            return 3
        print(f"both source kinds registered ({MONITOR_CAPTURE_KIND}, {BROWSER_KIND})")

        # --- Build the single coexistence scene ---
        print(f"-> CreateScene {SPIKE_SCENE!r}")
        r = await request(inbox, ws, "CreateScene", "cs", {"sceneName": SPIKE_SCENE})
        if not req_ok(r) and req_code(r) != RESOURCE_ALREADY_EXISTS:
            print(f"error: CreateScene declined: {r.get('requestStatus')}")
            return 1

        # (a) monitor_capture, full canvas, pinned to display 1 via the #60 enum.
        print("[setup] enumerating displays for monitor_capture (U1/#56) ...")
        setting_key, values = await m10_setup.enumerate_monitors(inbox, ws, print)
        if not values:
            print("FAIL: no displays enumerated — cannot pin monitor_capture. "
                  "(On a real desktop this should never be empty.)")
            return 1
        display1 = values[0]
        print(f"[setup] pinning {CAPTURE_INPUT!r} to display 1 "
              f"({setting_key}={display1!r})")
        cap_settings = {setting_key: display1, "capture_cursor": True}
        r = await request(inbox, ws, "CreateInput", "ci-cap", {
            "sceneName": SPIKE_SCENE,
            "inputName": CAPTURE_INPUT,
            "inputKind": MONITOR_CAPTURE_KIND,
            "inputSettings": cap_settings,
            "sceneItemEnabled": True,
        })
        if not req_ok(r) and req_code(r) != RESOURCE_ALREADY_EXISTS:
            print(f"error: CreateInput(monitor_capture) declined: {r.get('requestStatus')}")
            return 1
        cap_item_id = (r.get("responseData") or {}).get("sceneItemId")

        # (b) browser_source CEF overlay, partial (bottom-right quadrant), on TOP.
        print(f"-> CreateInput {BROWSER_INPUT!r} kind={BROWSER_KIND} url={page_url}")
        r = await request(inbox, ws, "CreateInput", "ci-br", {
            "sceneName": SPIKE_SCENE,
            "inputName": BROWSER_INPUT,
            "inputKind": BROWSER_KIND,
            "inputSettings": {
                "url": page_url,
                "is_local_file": False,
                "width": OVERLAY_W,
                "height": OVERLAY_H,
                "fps_custom": True,
                "fps": 30,
                "shutdown": False,          # keep CEF rendering off-screen too
                "restart_when_active": False,
                "reroute_audio": False,
            },
            "sceneItemEnabled": True,
        })
        if not req_ok(r):
            print(f"error: CreateInput(browser_source) declined: {r.get('requestStatus')}")
            return 1
        br_item_id = (r.get("responseData") or {}).get("sceneItemId")
        print(f"   <- capture sceneItemId={cap_item_id} overlay sceneItemId={br_item_id}")

        # Position the overlay into the bottom-right quadrant (partial overlay,
        # so the capture plane underneath stays visible elsewhere in the frame).
        if br_item_id is not None:
            r = await request(inbox, ws, "SetSceneItemTransform", "tf-br", {
                "sceneName": SPIKE_SCENE,
                "sceneItemId": br_item_id,
                "sceneItemTransform": {
                    "positionX": float(OVERLAY_X),
                    "positionY": float(OVERLAY_Y),
                    "boundsType": "OBS_BOUNDS_SCALE_INNER",
                    "boundsWidth": float(OVERLAY_W),
                    "boundsHeight": float(OVERLAY_H),
                    "boundsAlignment": 5,  # top-left
                },
            })
            if not req_ok(r):
                print(f"   warn: SetSceneItemTransform(overlay) declined: "
                      f"{r.get('requestStatus')} — overlay may be full-canvas; "
                      "region analysis still distinguishes the two planes.")

        print(f"-> SetCurrentProgramScene {SPIKE_SCENE!r}")
        r = await request(inbox, ws, "SetCurrentProgramScene", "sps",
                          {"sceneName": SPIKE_SCENE})
        if not req_ok(r):
            print(f"error: SetCurrentProgramScene declined: {r.get('requestStatus')}")
            return 1

        # --- Poll the program frame until CEF has painted, then analyse ---
        FRAMES_DIR.mkdir(parents=True, exist_ok=True)
        print(f"-> polling GetSourceScreenshot of the PROGRAM scene until the CEF "
              f"overlay paints (deadline {CAPTURE_POLL_DEADLINE_S:.0f}s) ...")
        deadline = time.monotonic() + CAPTURE_POLL_DEADLINE_S
        attempt = 0
        last_png: Optional[bytes] = None
        last_cap: Optional[dict] = None
        last_ovl: Optional[dict] = None
        while time.monotonic() < deadline:
            attempt += 1
            r = await request(inbox, ws, "GetSourceScreenshot", f"shot-{attempt}", {
                "sourceName": SPIKE_SCENE,
                "imageFormat": "png",
                "imageWidth": CANVAS_W,
                "imageHeight": CANVAS_H,
            })
            if not req_ok(r):
                if attempt == 1 or attempt % 5 == 0:
                    print(f"   attempt {attempt}: screenshot not ready "
                          f"({req_code(r)} {r.get('requestStatus', {}).get('comment','')!r})")
                await asyncio.sleep(CAPTURE_POLL_INTERVAL_S)
                continue
            png = _strip_data_uri(r["responseData"]["imageData"])
            try:
                w, h, ch, pxs = decode_png(png)
            except Exception as exc:  # noqa: BLE001 — diagnostic
                print(f"   attempt {attempt}: PNG decode failed: {exc}")
                await asyncio.sleep(CAPTURE_POLL_INTERVAL_S)
                continue

            # Analyse the capture plane (a region NOT under the overlay: the
            # top-left quadrant) and the overlay plane (the overlay region).
            cap = analyse_region(w, h, ch, pxs, 0, 0, OVERLAY_X, OVERLAY_Y)
            ovl = analyse_region(w, h, ch, pxs, OVERLAY_X, OVERLAY_Y, w, h)
            last_png, last_cap, last_ovl = png, cap, ovl
            if attempt == 1 or attempt % 4 == 0:
                print(f"   attempt {attempt}: capture nonblack={cap['nonblack_ratio']*100:.1f}% "
                      f"distinct={cap['distinct']} | overlay sig={ovl['signature_ratio']*100:.1f}% "
                      f"nonblack={ovl['nonblack_ratio']*100:.1f}%")
            # Stop early once the CEF overlay has clearly painted (signature
            # present) — the capture plane does not need polling, it is live or
            # black from the first frame.
            if overlay_is_live(ovl):
                break
            await asyncio.sleep(CAPTURE_POLL_INTERVAL_S)

        # --- Verdict ---
        if last_png is None or last_cap is None or last_ovl is None:
            print("\nFAIL: never decoded a program frame. GetSourceScreenshot kept "
                  "failing — the program scene produced no frame at all.")
            await _teardown(inbox, ws)
            return 1

        out_png = FRAMES_DIR / ("spike-gpu-control.png" if disable_gpu
                                else "spike-gpu-coexist.png")
        out_png.write_bytes(last_png)
        print(f"\n[frame] saved program frame -> {out_png} ({len(last_png):,} bytes)")

        cap_live = plane_is_live(last_cap)
        ovl_live = overlay_is_live(last_ovl)
        print("---- PLANE ANALYSIS ----")
        print(f"  (A) monitor_capture region [0,0..{OVERLAY_X},{OVERLAY_Y}]: "
              f"nonblack={last_cap['nonblack_ratio']*100:.1f}% "
              f"distinct={last_cap['distinct']} all_black={last_cap['all_black']} "
              f"-> {'LIVE (non-black)' if cap_live else 'BLACK / absent'}")
        print(f"  (B) CEF browser_source region [{OVERLAY_X},{OVERLAY_Y}..{CANVAS_W},{CANVAS_H}]: "
              f"signature={last_ovl['signature_ratio']*100:.1f}% "
              f"nonblack={last_ovl['nonblack_ratio']*100:.1f}% "
              f"all_black={last_ovl['all_black']} "
              f"-> {'LIVE (CEF painted)' if ovl_live else 'BLACK / not painted'}")

        await _teardown(inbox, ws)

        if cap_live and ovl_live:
            print("\nSPIKE-GPU PASS: monitor_capture (DXGI duplication) AND the CEF "
                  "browser_source render SIMULTANEOUSLY, both non-black, GPU-on, in "
                  "one pulsar.exe. The Q3=(b) precondition (real capture + Solar "
                  "overlay) HOLDS on this fork + box.")
            return 0

        # FAIL — name precisely which plane is black (the hard finding).
        which = []
        if not cap_live:
            which.append("(A) monitor_capture is BLACK/absent — DXGI desktop "
                         "duplication did not produce content")
        if not ovl_live:
            which.append("(B) CEF browser_source did NOT paint the signature page "
                         "— the overlay surface is black/unpainted")
        print("\nSPIKE-GPU FAIL (hard finding — reopens the mechanism, A4.6): "
              + "; ".join(which) + ".")
        return 1


async def _teardown(inbox: Inbox, ws) -> None:
    for name, rid in ((BROWSER_INPUT, "ri-br"), (CAPTURE_INPUT, "ri-cap")):
        try:
            await request(inbox, ws, "RemoveInput", rid, {"inputName": name})
        except Exception:
            pass
    try:
        await request(inbox, ws, "RemoveScene", "rs", {"sceneName": SPIKE_SCENE})
    except Exception:
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="SPIKE-GPU — monitor_capture + CEF browser_source GPU-on "
                    "coexistence (ADR 003 A4.6 gate #1)")
    ap.add_argument("--exe", type=pathlib.Path,
                    default=pathlib.Path(os.environ.get("PULSAR_EXE", str(DEFAULT_EXE))),
                    help="path to pulsar.exe (default: built rundir)")
    ap.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S)
    ap.add_argument("--disable-gpu-control", action="store_true",
                    help="CONTROL run: re-add --disable-gpu to reproduce the known "
                         "black-frame failure and prove the flag (not the box) is "
                         "the culprit. Expect monitor_capture BLACK. Optional.")
    args = ap.parse_args()

    exe: pathlib.Path = args.exe
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        print("Build it first: scripts/build-win.ps1 -Full")
        return 2

    disable_gpu = args.disable_gpu_control
    mode = ("CONTROL (--disable-gpu — expect monitor_capture BLACK)"
            if disable_gpu else "VERDICT (GPU-ON — no --disable-gpu)")
    print("=" * 72)
    print(f"SPIKE-GPU mode: {mode}")
    print("This must run on the porteur's REAL INTERACTIVE GPU-ON DESKTOP.")
    print("DXGI desktop duplication fails in a non-interactive/RDP/CI session")
    print("regardless of the GPU flag — a FAIL there is inconclusive.")
    print("=" * 72)

    server = LocalPageServer()
    page_url = server.start()
    print(f"local overlay page served at: {page_url}")

    port = _free_port()
    password = _secrets.token_urlsafe(16)
    print(f"spawning: {exe}")
    argv_preview = [exe.name] + (["--disable-gpu"] if disable_gpu else []) + ["--no-sandbox"]
    print(f"  argv: {argv_preview}  (GPU {'OFF' if disable_gpu else 'ON'})")
    print(f"  PULSAR_PORT={port}  PULSAR_PASSWORD=<redacted {len(password)} chars>")

    pulsar = PulsarProcess(exe, port, password, disable_gpu)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        rc = asyncio.run(run_spike(ws_url, sentinel_pw, page_url, disable_gpu))
    except KeyboardInterrupt:
        print("interrupted")
        rc = 130
    except Exception as exc:  # noqa: BLE001 — top-level diagnostic
        print(f"FAIL: {exc}")
        if pulsar.proc is not None:
            print(pulsar.diag())
        rc = 1
    finally:
        # Log-marker scan is part of the verdict — report it whatever happened.
        markers = scan_log_markers(pulsar.lines)
        print("---- PULSAR LOG MARKERS ----")
        if markers["dxgi"]:
            print(f"  DXGI duplication failure marker(s) seen ({len(markers['dxgi'])}):")
            for ln in markers["dxgi"][:8]:
                print(f"    | {ln}")
        else:
            print("  DXGI: no 887A0004 / DuplicateOutput1 failure marker in the log.")
        if markers["cef"]:
            print(f"  CEF GPU-process crash marker(s) seen ({len(markers['cef'])}):")
            for ln in markers["cef"][:8]:
                print(f"    | {ln}")
        else:
            print("  CEF: no 'GPU process isn't usable / Goodbye' crash marker in the log.")
        if rc not in (0, 3):
            for ln in pulsar.lines[-50:]:
                print(f"  | {ln}")
        pulsar.shutdown()
        if pulsar.proc is not None and pulsar.proc.poll() is None:
            print("error: pulsar.exe still running after shutdown")
            rc = rc or 1
        else:
            print("pulsar.exe reaped cleanly")
        server.stop()

    print("PASS" if rc == 0 else (f"SKIPPED (exit {rc})" if rc == 3 else f"FAILED (exit {rc})"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
