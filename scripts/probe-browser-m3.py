#!/usr/bin/env python3
"""
Pulsar CEF browser-source capture probe — M3 (ADR 008 §8).

Proves the freshly-built pulsar.exe + the `pulsar-browser` plugin (CEF) can
*render a web page into a source and have that frame captured*. This is the
"page -> video frames" link of the broadcast pipeline:

  1. boot pulsar.exe headless (self-spawn + reap, like M1/M2),
  2. serve a representative local HTML page from a throwaway http.server
     (dark background + big recognisable "ZABLAB LIVE M3" text),
  3. drive obs-websocket v5 to CreateScene + CreateInput(browser_source,
     {url, width, height}) + SetCurrentProgramScene,
  4. let CEF actually load + paint (poll, NOT a single early grab -- the
     classic CEF pitfall is capturing a blank frame before OnPaint runs),
  5. GetSourceScreenshot(png) the browser source, decode the base64 PNG,
  6. assert the frame is NON-blank: right dimensions, not all-one-colour,
     real colour variance, AND a meaningful share of non-background pixels
     (the rendered text/foreground) -- not just "it didn't crash".

SCOPE OF M3 (read this before extending):
  M3 proves the *CEF capture mechanism* -- that Pulsar's bundled Chromium
  renders an arbitrary URL and the frame is capturable. It deliberately
  serves a LOCAL page we control, NOT the live Solar/Orion scene URL. The
  live Solar page (served by Orion over the LSDP wire) was already proven
  separately (full-fidelity render via headless-chromium against the real
  gateway). Wiring the *real Solar live URL* into the browser_source is M6
  (the go-live rewire) -- a URL swap on top of this proven mechanism, since
  the Solar render itself is already proven. M3 must not pull in the Orion
  stack.

LICENSE INVARIANT (LICENSE-INVARIANTS.md #1/#2/#3, ADR 008 §3.1): this probe
talks to Pulsar over the WebSocket process boundary ONLY. It spawns
pulsar.exe as a separate OS process and exchanges nothing but obs-websocket
v5 frames. It runs a stdlib http.server in a background thread to serve the
page -- a plain HTTP server, no link to Pulsar. There is NO FFI, no
ctypes/cffi, no LoadLibrary of obs.dll / pulsar-browser.dll / libcef.dll, no
native import. CEF runs entirely inside the pulsar.exe process tree
(pulsar-browser-page.exe is CEF's own helper, spawned by libcef, never by
us). Pure aggregation -- Pulsar's GPL never crosses into this probe.

Steps (M3 brief, ADR 008 §8):
  0. Start a local http.server on a free loopback port serving page.html.
  1. Spawn pulsar.exe (cwd=bin/64bit, fresh PULSAR_PORT/PULSAR_PASSWORD);
     wait for the PULSAR_READY sentinel.
  2. v5 handshake (Hello -> Identify -> Identified).
  3. Confirm `browser_source` is a registered input kind (it is full-variant
     only; a light build is a typed, diagnosable skip -- NOT a false pass).
  4. CreateScene -> CreateInput(browser_source, {url, width, height,
     is_local_file=false, fps_custom/fps, shutdown=false}) ->
     SetCurrentProgramScene.
  5. Poll GetSourceScreenshot(png) until the decoded frame is non-blank or a
     deadline elapses -- CEF loads async; the first few grabs may be blank.
  6. Verify the decoded PNG: dimensions match, colour variance is real, and
     the foreground (text) covers a plausible pixel share.
  7. Save the captured PNG as proof (path printed; --keep to retain).
  8. Tear the scene graph down (RemoveInput + RemoveScene).
  9. Clean shutdown: WS close -> terminate child -> kill fallback -> stop the
     http.server. Idempotent, no orphan, no leaked port.

Usage (from the repo root, against the built rundir):
    pip install websockets
    python scripts/probe-browser-m3.py
    python scripts/probe-browser-m3.py --exe /path/to/pulsar.exe   # override
    python scripts/probe-browser-m3.py --keep                      # keep PNG
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
import secrets
import socket
import struct
import sys
import tempfile
import threading
import time
import zlib
from typing import Callable, Optional

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

# obs-websocket v5 READY sentinel — stable, machine-parseable
# (PROTOCOL.md, pulsar-headless/main.cpp seed_websocket_config).
READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0

# 0x7FF is the obs-websocket "all non-high-volume" mask used by the other
# probes. M3 does not depend on a specific event but subscribes for parity.
EVENT_SUBSCRIPTION_ALL = 0x7FF

SCENE_NAME = "probe-m3-scene"
INPUT_NAME = "probe-m3-browser"
# Registered by pulsar-browser (plugins/pulsar-browser/obs-browser-plugin.cpp
# info.id = "browser_source"). Settings keys from obs-browser-source.cpp
# Update(): url / width / height / is_local_file / fps_custom / fps / shutdown.
INPUT_KIND = "browser_source"
CANVAS_W = 1280
CANVAS_H = 720

# CEF needs time to spin up the render process (pulsar-browser-page.exe), do
# the HTTP fetch, lay out, and paint the first frame. The first screenshots
# are routinely blank — poll, don't single-shot. (DEVELOPMENT.md: CEF first
# paint is async; capturing too early is the canonical "blank frame" trap.)
CAPTURE_POLL_DEADLINE_S = 30.0
CAPTURE_POLL_INTERVAL_S = 0.75

# The page renders bright foreground text on a dark background. After a real
# paint, a meaningful share of pixels must be non-background (the text/accent)
# and the image must carry real colour variance. A blank/single-colour grab
# fails both. Thresholds kept generous: CEF AA + the accent bar comfortably
# clear them; an all-black or all-one-colour frame does not.
MIN_NONBG_PIXEL_RATIO = 0.005   # >= 0.5% of pixels clearly off the background
MIN_DISTINCT_COLOURS = 8        # a blank frame has 1; a real render has many
# Background of the page is near-black (#0b0e1a). "Background" for the ratio
# test = pixels within this Manhattan distance of that colour.
BG_RGB = (0x0B, 0x0E, 0x1A)
BG_MANHATTAN_TOL = 48


# The representative scene-card page. Dark background + a bright accent bar +
# large recognisable text. Deterministic, self-contained, no external assets,
# no network beyond the local http.server. Body sized to the source so the
# capture is full-bleed.
PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ZABLAB LIVE M3</title>
<style>
  html, body {
    margin: 0; padding: 0; width: 100%; height: 100%;
    background: #0b0e1a; overflow: hidden;
  }
  .stage {
    width: 100vw; height: 100vh;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    font-family: Arial, Helvetica, sans-serif;
  }
  .accent {
    width: 60%; height: 14px; border-radius: 7px;
    background: linear-gradient(90deg, #ff3da6, #38e8ff);
    margin-bottom: 48px;
  }
  .title {
    color: #f5f7ff; font-size: 96px; font-weight: 800;
    letter-spacing: 4px; text-shadow: 0 0 24px rgba(56,232,255,0.55);
  }
  .sub {
    color: #8fa0c8; font-size: 32px; margin-top: 24px; letter-spacing: 2px;
  }
</style>
</head>
<body>
  <div class="stage">
    <div class="accent"></div>
    <div class="title">ZABLAB LIVE M3</div>
    <div class="sub">CEF BROWSER SOURCE CAPTURE</div>
  </div>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Local HTTP server — serves the page from an in-memory document root on a
# background thread. Plain stdlib http.server; no link to Pulsar.
# --------------------------------------------------------------------------
class _PageHandler(http.server.BaseHTTPRequestHandler):
    # Class attribute set by the probe before the server starts.
    page_bytes: bytes = PAGE_HTML.encode("utf-8")

    def do_GET(self) -> None:  # noqa: N802 — http.server contract
        if self.path in ("/", "/page.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(self.page_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(self.page_bytes)
        else:
            self.send_error(404, "not found")

    def log_message(self, *args) -> None:  # silence the per-request stderr spam
        return


class LocalPageServer:
    def __init__(self) -> None:
        self.httpd: Optional[http.server.ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.port: int = 0

    def start(self) -> str:
        # Bind :0 so the OS hands us a free loopback port (no collision with
        # the pulsar WS port or a stale listener).
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.port}/page.html"

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None


# --------------------------------------------------------------------------
# Process management — mirrors probe-record-m2.py / probe-websocket.py.
# --------------------------------------------------------------------------
class PulsarProcess:
    """Spawns pulsar.exe and pumps its stdout on a background thread so the
    READY sentinel is parsed without blocking. Captures the full boot log for
    diagnostics on failure."""

    def __init__(self, exe: pathlib.Path, port: int, password: str) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.proc = None
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None
        self._pump_thread: Optional[threading.Thread] = None

    def spawn(self) -> None:
        import subprocess

        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        # No mic / no capture target — the browser source is the only visible
        # thing; we never need desktop/mic capture for M3.
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)

        creationflags = 0
        if os.name == "nt":
            # CREATE_NO_WINDOW — keep the console-subsystem child headless.
            creationflags = 0x08000000

        self.proc = subprocess.Popen(
            [str(self.exe)],
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
                    "Likely causes (DEVELOPMENT.md Troubleshooting): wrong cwd "
                    "(default.effect not found), port conflict, AV quarantine, "
                    "or obs-websocket.dll failing to load.\n" + self._diag()
                )

    def diag(self) -> str:
        return self._diag()

    def _diag(self) -> str:
        tail = self._lines[-40:]
        body = "\n".join(f"  | {ln}" for ln in tail) if tail else "  | (no output)"
        return f"--- pulsar stdout/stderr (last {len(tail)} lines) ---\n{body}"

    def shutdown(self, grace: float = SHUTDOWN_GRACE_S) -> None:
        """terminate -> wait grace -> kill fallback. Idempotent."""
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
# obs-websocket v5 request/event plumbing — mirrors probe-record-m2.py.
# --------------------------------------------------------------------------
def compute_auth(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 challenge/response (PROTOCOL.md):
    sha256( base64( sha256(password + salt) ) + challenge )."""
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
    inbox: Inbox,
    ws,
    request_type: str,
    request_id: str,
    data: dict | None = None,
    timeout: float = 15.0,
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


# --------------------------------------------------------------------------
# Minimal dependency-free PNG decode — enough to read raw RGB(A) pixels for
# the non-blank assertions. We do NOT pull Pillow/numpy: a pure-stdlib decode
# (zlib for the IDAT, struct for the chunks) keeps the probe a flat
# `pip install websockets` like its siblings.
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
    """Decode a PNG into (width, height, channels, raw pixels). Supports
    8-bit truecolour (RGB / RGBA), no interlace — which is what Qt's PNG
    encoder emits for a screenshot. Returns channels in {3,4}."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG (bad signature)")
    off = 8
    width = height = bit_depth = colour_type = interlace = 0
    idat = bytearray()
    while off + 8 <= len(data):
        (length,) = struct.unpack(">I", data[off : off + 4])
        ctype = data[off + 4 : off + 8]
        body = data[off + 8 : off + 8 + length]
        off += 12 + length  # length + type + data + crc
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
        elif filt == 1:  # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filt == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:  # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif filt == 4:  # Paeth
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
# Non-blank / content assertions over the decoded pixels.
# --------------------------------------------------------------------------
def analyse_frame(width: int, height: int, channels: int, px: bytearray) -> dict:
    """Return blank/content metrics. Subsamples for speed on big frames."""
    total = width * height
    if total == 0:
        return {"distinct": 0, "nonbg_ratio": 0.0, "sampled": 0, "all_same": True}

    # Subsample to <= ~40k pixels for a fast but representative read.
    step = max(1, total // 40000)
    distinct: set[int] = set()
    nonbg = 0
    sampled = 0
    first_rgb: Optional[tuple[int, int, int]] = None
    all_same = True
    bg_r, bg_g, bg_b = BG_RGB
    for idx in range(0, total, step):
        base = idx * channels
        r, g, b = px[base], px[base + 1], px[base + 2]
        sampled += 1
        distinct.add((r << 16) | (g << 8) | b)
        if first_rgb is None:
            first_rgb = (r, g, b)
        elif (r, g, b) != first_rgb:
            all_same = False
        if abs(r - bg_r) + abs(g - bg_g) + abs(b - bg_b) > BG_MANHATTAN_TOL:
            nonbg += 1
    return {
        "distinct": len(distinct),
        "nonbg_ratio": nonbg / sampled if sampled else 0.0,
        "sampled": sampled,
        "all_same": all_same,
    }


def frame_is_content(metrics: dict) -> bool:
    if metrics["all_same"]:
        return False
    if metrics["distinct"] < MIN_DISTINCT_COLOURS:
        return False
    if metrics["nonbg_ratio"] < MIN_NONBG_PIXEL_RATIO:
        return False
    return True


# --------------------------------------------------------------------------
# The M3 round-trip.
# --------------------------------------------------------------------------
def _strip_data_uri(image_data: str) -> bytes:
    """GetSourceScreenshot returns a data URI: 'data:image/png;base64,<...>'
    (RequestHandler_Sources.cpp). Strip the scheme prefix before decoding."""
    comma = image_data.find(",")
    payload = image_data[comma + 1 :] if comma != -1 else image_data
    return base64.b64decode(payload)


async def drive_browser_capture(
    url: str, password: str, page_url: str, out_png: pathlib.Path
) -> int:
    print(f"connecting: {url}")
    async with websockets.connect(
        url, subprotocols=["obswebsocket.json"], open_timeout=10
    ) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if hello.get("op") != 0:
            print(f"error: expected Hello (op=0), got {hello}")
            return 1
        rpc = hello["d"]["rpcVersion"]
        identify_d: dict = {
            "rpcVersion": rpc,
            "eventSubscriptions": EVENT_SUBSCRIPTION_ALL,
        }
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(
                password, a["salt"], a["challenge"]
            )
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ident.get("op") != 2:
            print(f"error: identify failed: {ident}")
            return 1
        print("identified")

        inbox = Inbox()

        # --- Guard: browser_source must be registered (full-variant build) ---
        resp = await request(inbox, ws, "GetInputKindList", "kinds", {})
        kinds = set(resp["responseData"]["inputKinds"])
        if INPUT_KIND not in kinds:
            print(
                f"SKIP: input kind {INPUT_KIND!r} is NOT registered — this is a "
                "LIGHT build (no CEF). M3 requires a full build "
                "(scripts/build-win.ps1 -Full). This is a typed, diagnosable "
                "skip, NOT a pass."
            )
            print(f"      registered kinds: {sorted(kinds)}")
            # Exit 3 = environment/skip, distinct from 0 (pass) and 1 (fail).
            return 3
        print(f"browser_source registered ({len(kinds)} input kinds total)")

        # --- Build the scene graph: scene + CEF browser source ---
        print(f"-> CreateScene {SCENE_NAME!r}")
        r = await request(inbox, ws, "CreateScene", "cs", {"sceneName": SCENE_NAME})
        if not r["requestStatus"]["result"]:
            print(f"error: CreateScene declined: {r['requestStatus']}")
            return 1

        print(f"-> CreateInput {INPUT_NAME!r} kind={INPUT_KIND} url={page_url}")
        r = await request(
            inbox,
            ws,
            "CreateInput",
            "ci",
            {
                "sceneName": SCENE_NAME,
                "inputName": INPUT_NAME,
                "inputKind": INPUT_KIND,
                "inputSettings": {
                    "url": page_url,
                    "is_local_file": False,
                    "width": CANVAS_W,
                    "height": CANVAS_H,
                    "fps_custom": True,
                    "fps": 30,
                    # Keep CEF rendering even when "not visible" so the
                    # off-screen screenshot path always has a fresh frame.
                    "shutdown": False,
                    "restart_when_active": False,
                    "reroute_audio": False,
                },
                "sceneItemEnabled": True,
            },
        )
        if not r["requestStatus"]["result"]:
            print(f"error: CreateInput(browser_source) declined: {r['requestStatus']}")
            return 1
        print(f"   <- sceneItemId={r['responseData'].get('sceneItemId')}")

        print(f"-> SetCurrentProgramScene {SCENE_NAME!r}")
        r = await request(
            inbox, ws, "SetCurrentProgramScene", "sps", {"sceneName": SCENE_NAME}
        )
        if not r["requestStatus"]["result"]:
            print(f"error: SetCurrentProgramScene declined: {r['requestStatus']}")
            return 1

        # --- Poll the screenshot until CEF has actually painted the page ---
        print(
            f"-> polling GetSourceScreenshot (png) until non-blank "
            f"(deadline {CAPTURE_POLL_DEADLINE_S:.0f}s) ..."
        )
        deadline = time.monotonic() + CAPTURE_POLL_DEADLINE_S
        attempt = 0
        last_metrics: dict | None = None
        last_png: bytes | None = None
        last_dims: tuple[int, int] = (0, 0)
        while time.monotonic() < deadline:
            attempt += 1
            r = await request(
                inbox,
                ws,
                "GetSourceScreenshot",
                f"shot-{attempt}",
                {
                    "sourceName": INPUT_NAME,
                    "imageFormat": "png",
                    "imageWidth": CANVAS_W,
                    "imageHeight": CANVAS_H,
                },
            )
            if not r["requestStatus"]["result"]:
                # Early grabs can fail with "Failed to render screenshot." while
                # the source has no frame yet — that is expected, keep polling.
                code = r["requestStatus"].get("code")
                comment = r["requestStatus"].get("comment", "")
                if attempt == 1 or attempt % 5 == 0:
                    print(f"   attempt {attempt}: not ready (code={code} {comment!r})")
                await asyncio.sleep(CAPTURE_POLL_INTERVAL_S)
                continue

            png = _strip_data_uri(r["responseData"]["imageData"])
            try:
                w, h, ch, px = decode_png(png)
            except Exception as exc:  # noqa: BLE001 — diagnostic
                print(f"   attempt {attempt}: PNG decode failed: {exc}")
                await asyncio.sleep(CAPTURE_POLL_INTERVAL_S)
                continue

            metrics = analyse_frame(w, h, ch, px)
            last_metrics, last_png, last_dims = metrics, png, (w, h)
            if attempt == 1 or attempt % 4 == 0:
                print(
                    f"   attempt {attempt}: {w}x{h} ch={ch} distinct={metrics['distinct']} "
                    f"nonbg={metrics['nonbg_ratio']*100:.2f}% "
                    f"all_same={metrics['all_same']}"
                )
            if frame_is_content(metrics):
                print(
                    f"   CONTENT at attempt {attempt}: {w}x{h} "
                    f"distinct={metrics['distinct']} "
                    f"nonbg={metrics['nonbg_ratio']*100:.2f}%"
                )
                # Save proof, then tear down.
                out_png.write_bytes(png)
                print(f"   captured PNG saved: {out_png} ({len(png):,} bytes)")

                # Dimension assertion.
                if (w, h) != (CANVAS_W, CANVAS_H):
                    print(
                        f"warn: captured dims {w}x{h} != requested "
                        f"{CANVAS_W}x{CANVAS_H} (scale-to-inner)"
                    )

                await request(inbox, ws, "RemoveInput", "ri", {"inputName": INPUT_NAME})
                await request(inbox, ws, "RemoveScene", "rs", {"sceneName": SCENE_NAME})
                await ws.close(code=1000, reason="m3 complete")
                print("\nM3 OK — CEF browser_source rendered the page and the frame")
                print("        was captured + verified non-blank with real content.")
                return 0

            await asyncio.sleep(CAPTURE_POLL_INTERVAL_S)

        # --- Deadline hit without a content frame: diagnose precisely ---
        print("\nFAIL: CEF never produced a non-blank frame within the deadline.")
        if last_metrics is None:
            print(
                "  No screenshot ever decoded. The screenshot request kept failing "
                "to render — the browser source produced no frame at all.\n"
                "  Likely causes (DEVELOPMENT.md §Troubleshooting):\n"
                "   - pulsar-browser-page.exe missing next to libcef.dll in "
                "obs-plugins/64bit (CEF render subprocess can't launch)\n"
                "   - libcef.dll / CEF resources (*.pak, icudtl.dat, locales/) "
                "not staged in obs-plugins/64bit\n"
                "   - the local http.server URL was unreachable from CEF"
            )
        else:
            w, h = last_dims
            print(
                f"  Last frame: {w}x{h} distinct={last_metrics['distinct']} "
                f"nonbg={last_metrics['nonbg_ratio']*100:.2f}% "
                f"all_same={last_metrics['all_same']}"
            )
            if last_metrics["all_same"]:
                print(
                    "  The frame is a SOLID colour (blank). CEF rendered an empty "
                    "surface — the page likely never loaded (HTTP unreachable, or "
                    "the render subprocess never painted)."
                )
            else:
                print(
                    "  The frame has some variance but did not clear the content "
                    "thresholds — partial/garbled render. Inspect the saved PNG."
                )
            # Save the last frame as evidence even on failure.
            if last_png is not None:
                out_png.write_bytes(last_png)
                print(f"  last frame saved for inspection: {out_png}")

        await request(inbox, ws, "RemoveInput", "ri", {"inputName": INPUT_NAME})
        await request(inbox, ws, "RemoveScene", "rs", {"sceneName": SCENE_NAME})
        await ws.close(code=1000, reason="m3 deadline")
        return 1


def pick_free_port() -> int:
    """Bind :0 on loopback to let the OS hand us a free ephemeral port, then
    release it (tiny TOCTOU window, acceptable for a single-run local probe)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar M3 CEF browser-source probe")
    ap.add_argument(
        "--exe",
        type=pathlib.Path,
        default=DEFAULT_EXE,
        help="path to pulsar.exe (default: built rundir)",
    )
    ap.add_argument(
        "--ready-timeout",
        type=float,
        default=READY_TIMEOUT_S,
        help="seconds to wait for the READY sentinel",
    )
    ap.add_argument(
        "--out",
        type=pathlib.Path,
        default=None,
        help="path to write the captured PNG (default: temp file, printed)",
    )
    ap.add_argument(
        "--keep",
        action="store_true",
        help="do not delete the captured PNG on success (debugging)",
    )
    args = ap.parse_args()

    exe: pathlib.Path = args.exe
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        print("Build it first: scripts/build-win.ps1 -Full")
        return 2

    if args.out is not None:
        out_png = args.out
    else:
        fd, tmp = tempfile.mkstemp(prefix="pulsar-m3-capture-", suffix=".png")
        os.close(fd)
        out_png = pathlib.Path(tmp)

    server = LocalPageServer()
    page_url = server.start()
    print(f"local page served at: {page_url}")

    port = pick_free_port()
    password = secrets.token_urlsafe(16)
    print(f"spawning: {exe}")
    print(f"  cwd={exe.parent}")
    print(f"  PULSAR_PORT={port}  PULSAR_PASSWORD=<redacted {len(password)} chars>")

    pulsar = PulsarProcess(exe, port, password)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        rc = asyncio.run(drive_browser_capture(ws_url, sentinel_pw, page_url, out_png))
    except KeyboardInterrupt:
        print("interrupted")
        rc = 130
    except Exception as exc:  # noqa: BLE001 — top-level probe diagnostic
        print(f"FAIL: {exc}")
        if pulsar.proc is not None:
            print(pulsar.diag())
        rc = 1
    finally:
        pulsar.shutdown()
        if pulsar.proc is not None and pulsar.proc.poll() is None:
            print("error: pulsar.exe still running after shutdown attempt")
            rc = rc or 1
        else:
            print("pulsar.exe reaped cleanly")
        server.stop()
        # On success, clean up the temp PNG unless asked to keep it. On
        # failure or skip, always leave the evidence file in place.
        if rc == 0 and not args.keep and args.out is None:
            try:
                out_png.unlink()
                print("captured PNG deleted (pass; pass --keep to retain)")
            except OSError:
                pass
        elif out_png.exists():
            print(f"capture evidence: {out_png}")

    print("PASS" if rc == 0 else (f"SKIPPED (exit {rc})" if rc == 3 else f"FAILED (exit {rc})"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
