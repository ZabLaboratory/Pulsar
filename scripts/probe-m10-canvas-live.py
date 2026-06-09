#!/usr/bin/env python3
r"""M10 live end-to-end probe — Blue trigger → leaf → {Solar overlay (CEF) +
stand-in cut} → invisible hard-cut between two WGC monitor_capture scenes, on
air mid-broadcast (ADR 003 Amendments 3 & 4 §A4.2/§A4.4/§A4.6, issue #79).

THE PIVOT THIS PROVES (Solar/CEF overlay, NOT an OBS-native transition)
  The visible transition is an OPAQUE full-screen overlay animated by OUR
  engine (Solar `wipe-cover`, #77) inside a CEF `browser_source`, layered above
  two `monitor_capture` scenes. The screen-1→screen-2 change underneath is an
  INSTANTANEOUS hard-cut (`SetCurrentProgramScene`) fired UNDER the overlay's
  opaque plateau — so 100 % of the visible animation is ours and the cut is
  never seen. There is NO OBS-native transition (C-MECH): the native stinger is
  dormant behind PULSAR_NATIVE_STINGER (default OFF, #73/#83).

THE CHAIN, END TO END (ADR §A4.4):

    fire blueprint on the VPS  ──►  POST /blue/api/v1/blueprints/{id}/trigger
        │  (operator Bearer HEADER, never query — Blue ADR 001 R6)
        ▼
    Blue writes a LEAF  __inputs.blue.<slug>.scene_control = {
        target_scene, overlay{kind,reveal_ms,hold_ms,retract_ms}, cut_at_ms }
        │  over Blue's service-token WS → ZabGate → Orion
        ▼
    Orion /show/stream  ── fans the leaf delta to ALL live subscribers ──►
        │  (ONLY if the active scene DECLARES the path — F2 / C-FANOUT)
        ├──► Solar (overlay, in the CEF) — reads `overlay`, REPLAYS wipe-cover
        └──► THIS stand-in cut consumer  — reads `cut_at_ms` + `target_scene`,
                 validates via the FROZEN contract, then at cut_at_ms issues a
                 single loopback obs-ws SetCurrentProgramScene{target_scene}
                 (a HARD-CUT — NO SetCurrentSceneTransition, ever).

THE STAND-IN CUT CONSUMER IS THE FROZEN CONTRACT, NOT A FORK
  ADR §A4.5 lets the probe stand in for Prism's cut executor (#72). To keep
  the stand-in provably identical to Prism's TS guard, every leaf is driven
  through the SAME canonical validator
  ``scripts/contracts/scene_control/validate_scene_control`` (#82, the
  re-frozen overlay-form contract). The Python stand-in here and the TS
  consumer there are two faithful expressions of one contract, both driven by
  ``scripts/contracts/scene_control/fixtures/*`` — they cannot drift.

DELIVERY MODES — so the chain is provable WITHOUT the VPS
  --live-wire     (default, Keeper's antenna run): the real POST /trigger on
                  the VPS writes the Blue leaf; the stand-in subscribes to the
                  REAL gateway /show/stream and reads the Orion-fanned delta.
  --loopback-leaf (dry-run / CI proof-only): the probe INJECTS the exact leaf
                  Orion would fan out into BOTH the in-process stand-in AND the
                  overlay page (via the loopback /leaf.json endpoint), against
                  a real local pulsar.exe. Proves validate→cut→capture +
                  overlay-blend + cut-skew, C-MECH, ordering, C-INJ, C-SEC on a
                  box with no VPS reach.

BROADCAST MODES
  --no-broadcast  proof-only: run the full chain OFF AIR (no Twitch key). The
                  mode Forge runs to debug the chain (WGC renders headless).
  --broadcast     go live to Twitch (needs TWITCH_STREAM_KEY, etage-1).

  GPU: pulsar.exe is spawned WITHOUT --disable-gpu (GPU-on). SPIKE-GPU (#70)
  proved WGC monitor_capture + CEF browser_source coexist GPU-on headless.

PROOFS (Resolution criteria #79)
  C5″ (overlay blend on CEF)  — capture frame A (pre), MID (during the opaque
        plateau — the Solar overlay covers the screen), B (post). The MID frame
        is the overlay cover (near-uniform opaque fill), NOT a hard cut between
        two captures.
  C-CUT + SPIKE-CUT (invisible cut) — measure the skew between the overlay's
        real CEF opacity (read from window.__m10) and the cut instant
        `cut_at_ms`; prove the cut fires while opacity≈1 (under the plateau).
  C-MECH (no native transition) — assert ZERO SetCurrentSceneTransition /
        SetCurrentSceneTransitionSettings requests are ever issued.
  C-FANOUT / ordering / C-SEC — the delta reaches the consumer; the cut is
        CAUSED by the leaf; grep-assert no secret in stdout or any PNG.

  --allow-blank   proof-only CI-safe: do not FAIL on a blank/identical capture
                  (a CI runner where WGC/Solar may not render). The wire +
                  validate + cut + C-MECH + C-INJ + C-FANOUT + C-SEC are still
                  asserted; the visual blend + skew are then the antenna run.

Exit codes (probe-family convention):
  0  pass · 1 assertion/integration failure · 2 config/env error
  3  typed skip (monitor_capture / browser_source not registered — LIGHT build)

Usage (from the repo root):
    pip install websockets
    # Dry-run Forge runs (no Twitch, no VPS) — the integration proof:
    python scripts/probe-m10-canvas-live.py --no-broadcast --loopback-leaf
    # CI proof-only (CTest): wiring/imports, no visual assertion:
    python scripts/probe-m10-canvas-live.py --no-broadcast --loopback-leaf \
        --allow-blank --duration 12
    # Keeper's antenna run (real VPS trigger + Twitch):
    export TWITCH_STREAM_KEY=...            # etage-1 secret, never committed
    export M8_OPERATOR_TOKEN=...            # etage-1 operator/admin JWT
    export M8_GATEWAY_URL=https://zabgate.cyell.dev
    export M10_BLUEPRINT_ID=...            # the scene-control blueprint id
    export M10_SHOW_TOKEN=...              # viewer show-token (etage-1)
    python scripts/probe-m10-canvas-live.py --broadcast --live-wire
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
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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

# The re-frozen cross-service contract (#82) is the single source of truth for
# the scene NAMES (target_scene allowlist), the overlay-kind allowlist, the
# canonical 3-segment leaf path, the overlay-form leaf-value shape, and the
# reject corpus. The stand-in cut consumer validates EVERY leaf through it.
_CONTRACTS_DIR = pathlib.Path(__file__).resolve().parent / "contracts"
if str(_CONTRACTS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS_DIR.parent))
from contracts.scene_control import (  # noqa: E402
    ALLOWED_OVERLAY_KINDS,
    DEFAULT_SCENE_ALLOWLIST,
    SceneControlContractError,
    assert_canonical_leaf_path,
    build_leaf_path,
    validate_scene_control,
)

# Reuse the #84 setup harness verbatim (WGC scene creation, U1 monitor enum, F2
# in-process declaration round-trip). The probe orchestrates it; it does NOT
# re-implement the scene plumbing #84 already froze.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import m10_setup  # noqa: E402

# The loopback Orion-WS stand-in (#79 fix). The REAL Solar host bundle gets its
# scene EXCLUSIVELY from the Orion snapshot/delta stream in mode=broadcast — the
# wipe-cover node is NOT baked in the JS. Without a WS peer the bundle connects
# to nothing and #scene stays a black div (the C5″ false positive on a blank
# frame). This serves the LSDP/1.1 scene + the bundle GET on one loopback port.
from m10_orion_standin import OrionStandIn  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    REPO_ROOT / "upstream" / "build_x64" / "rundir" / "RelWithDebInfo"
    / "bin" / "64bit" / "pulsar.exe"
)
OVERLAY_DIR = REPO_ROOT / "scripts" / "live-test"
OVERLAY_PAGE = OVERLAY_DIR / "m10-overlay.html"
BUILD_DIR = REPO_ROOT / "build"
LIVE_VOD_DIR = BUILD_DIR / "m10-live-vod"
FRAMES_DIR = BUILD_DIR / "m10-frames"

# The blueprint slug → canonical leaf path. Pinned by #84 / the contract.
M10_BLUEPRINT_SLUG = m10_setup.M10_BLUEPRINT_SLUG  # "m10-scene-control"
M10_LEAF_PATH = build_leaf_path(M10_BLUEPRINT_SLUG)  # 3-segment, contract-checked

SCENE_SCREEN_1 = m10_setup.SCENE_SCREEN_1
SCENE_SCREEN_2 = m10_setup.SCENE_SCREEN_2
SCENE_ALLOWLIST = frozenset(DEFAULT_SCENE_ALLOWLIST)
OVERLAY_KIND_ALLOWLIST = frozenset(ALLOWED_OVERLAY_KINDS)

MONITOR_CAPTURE_KIND = "monitor_capture"
BROWSER_SOURCE_KIND = "browser_source"
OVERLAY_SCENE = "scene-overlay"
OVERLAY_INPUT = "m10-overlay-cef"
# The native-stinger flag (#73/#83). Default OFF: the live pivot world this
# probe asserts. We make the dormancy explicit on the spawned fork.
NATIVE_STINGER_ENV = "PULSAR_NATIVE_STINGER"

READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0
EVENT_SUBSCRIPTION_ALL = 0x7FF
CANVAS_W = 1920
CANVAS_H = 1080
RESOURCE_ALREADY_EXISTS = 601

FRAME_DROP_RATIO_MAX = 0.05
POLL_INTERVAL_SEC = 5.0
DESTINATION_NAME = "pulsar-m10-live"

# WGC WARMUP-POLL (#79 timing fix). SPIKE-GPU proved WGC monitor_capture renders
# non-black GPU-on headless — but the FIRST WGC frame is black: the duplicator /
# WinRT capture needs a few hundred ms of warmup before it yields live content
# (probe-spike-gpu-coexist.py polls "GetSourceScreenshot ... until paint" for the
# same reason; its attempt-1 reads nonblack=0.0% then goes live after polling).
# capture_program_frame at a FIXED 2s sleep raced that warmup and frame A landed
# black, hard-failing a box where the pipeline was in fact about to render. The
# fix mirrors the spike: poll the program frame until frame_is_content (the same
# predicate the C5″ A/B legs use) before capturing frame A, with a budget. Only a
# budget expiry that is STILL blank is a real failure (a true dead WGC / blank
# desktop) — and --allow-blank still downgrades that to the antenna run.
WARMUP_POLL_BUDGET_S = 30.0
WARMUP_POLL_INTERVAL_S = 0.5
# After WGC content is live, give Solar (the CEF overlay bundle) a brief grace to
# finish connecting to the Orion stream + fetching its render bundle, so the leaf
# we deliver next is REACTED to (the wipe-cover replays) rather than missed. In
# loopback mode this is gated on the real signal (a subscriber connected + the
# bundle fetched on the Orion-WS stand-in); otherwise it is a flat grace.
SOLAR_READY_BUDGET_S = 12.0
SOLAR_READY_POLL_INTERVAL_S = 0.3
SOLAR_READY_GRACE_S = 1.5

# Frame-analysis thresholds (mirrors probe-m6-live.py).
MODAL_MANHATTAN_TOL = 24
MIN_DISTINCT_COLOURS = 12
MIN_NONBG_PIXEL_RATIO = 0.02
# The opaque-plateau cover fill (Solar wipe-cover DEFAULT_COVER_FILL = #000000).
# At the plateau the program output is near-uniform this colour.
COVER_FILL_RGB = (0, 0, 0)
# A MID frame is "covered" when it is near-uniform (very few distinct colours)
# and its mean sits near the cover fill — the overlay, not a capture.
COVER_MAX_DISTINCT = 8
COVER_MEAN_TOL = 28

# C5″ HARDENING (#79). A near-uniform black MID frame is AMBIGUOUS: it is
# equally the wipe-cover at its opaque plateau OR a blank/dead WGC capture
# (a black desktop, a build that renders nothing). The cover-fill is #000, so
# "MID is uniform black" alone is NOT proof of the overlay — that was the C5″
# false positive. The proof is only sound when the cover is DISTINGUISHABLE
# from a blank frame, and the only way a uniform-cover frame carries meaning is
# if the frames it sits BETWEEN are REAL, VARIED screen content:
#   * frame A (pre-cut)  MUST be content (varied desktop) — frame_is_content;
#   * frame B (post-cut) MUST be content (varied desktop) — frame_is_content;
#   * frame MID          MUST be the uniform cover (COVER_*), i.e. the overlay
#     has visibly REPLACED that varied content with a flat fill.
# A run where A or B is also blank cannot tell "overlay covered the screen"
# from "the screen was black the whole time" — so it FAILs (unless --allow-blank
# explicitly downgrades the visual leg to the antenna run). The MID-is-cover
# check additionally requires the frame be near-uniform AT the fill colour, so
# a busy desktop captured at MID (a VISIBLE hard cut, no overlay) also FAILs.
C5_REQUIRE_VARIED_AB = True

# The leaf value the demo blueprint emits — the canonical valid fixture case
# "wipe-cover-switch-to-screen-2" (re-frozen overlay form). cut_at_ms sits
# inside the opaque window [reveal_ms, reveal_ms+hold_ms].
DEMO_SCENE_CONTROL_VALUE: dict[str, Any] = {
    "target_scene": SCENE_SCREEN_2,
    "overlay": {
        "kind": "wipe-cover",
        "reveal_ms": 400,
        "hold_ms": 500,
        "retract_ms": 400,
    },
    "cut_at_ms": 650,  # mid-plateau: reveal(400) <= 650 <= reveal+hold(900)
}

# obs-ws requests that constitute an OBS-NATIVE transition. C-MECH asserts the
# stand-in NEVER issues any of these — the cut is a bare SetCurrentProgramScene.
NATIVE_TRANSITION_REQUESTS = frozenset({
    "SetCurrentSceneTransition",
    "SetCurrentSceneTransitionSettings",
    "SetCurrentSceneTransitionDuration",
})


# --------------------------------------------------------------------------
# Secret redaction — every live secret is scrubbed from every log line.
# --------------------------------------------------------------------------
class Redactor:
    """Replace every known live secret with a typed placeholder. The set is
    built from the runtime values (stream key, operator JWT, show-token,
    obs-ws password) so a value can never reach a log line or a saved PNG's
    surrounding output. Also masks any JWT-shaped ``token=eyJ…`` defensively."""

    _JWT_RE = re.compile(r"((?:[?&]|%3[Ff])token(?:=|%3[Dd]))ey[\w.\-]+")

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def add(self, value: Optional[str], label: str) -> None:
        v = (value or "").strip()
        if v:
            self._secrets[v] = f"<{label}>"

    def __call__(self, text: str) -> str:
        out = text
        for secret, placeholder in self._secrets.items():
            if secret in out:
                out = out.replace(secret, placeholder)
        out = self._JWT_RE.sub(r"\1<redacted>", out)
        return out

    def leaks(self, text: str) -> list[str]:
        """Return the labels of any live secret still present in ``text``."""
        return [
            placeholder
            for secret, placeholder in self._secrets.items()
            if secret in text
        ]


def redact_show_stream_url(url: str) -> str:
    """Mirror Prism's ``redactShowStreamUrl`` (url.ts) — strip the viewer
    show-token from a ``/show/stream?token=…`` URL for safe logging (C-SEC)."""
    url = re.sub(r"([?&]token=)[^&\s]+", r"\1<redacted>", url, flags=re.IGNORECASE)
    url = re.sub(r"token%3D[^%&\s]+", "token%3D<redacted>", url, flags=re.IGNORECASE)
    return url


# --------------------------------------------------------------------------
# Tee logger — captures every printed line so the final C-SEC grep-assert
# scans the EXACT bytes the run emitted (not a best-effort re-derivation).
# --------------------------------------------------------------------------
class TeeLog:
    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor
        self.lines: list[str] = []

    def __call__(self, line: str = "") -> None:
        safe = self.redactor(str(line))
        self.lines.append(safe)
        print(safe)

    def text(self) -> str:
        return "\n".join(self.lines)


# --------------------------------------------------------------------------
# Local HTTP server — serves the overlay page (or a real Solar bundle dir) to
# CEF AND exposes the loopback /leaf.json the overlay polls. The probe writes
# the leaf there at the synchronised delivery instant so the SAME leaf drives
# the overlay (CEF) and the cut (stand-in).
# --------------------------------------------------------------------------
def find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _LeafState:
    """Thread-safe holder for the current leaf value the overlay polls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._value: Optional[dict] = None

    def set(self, value: dict) -> int:
        with self._lock:
            self._seq += 1
            self._value = value
            return self._seq

    def snapshot(self) -> tuple[int, Optional[dict]]:
        with self._lock:
            return self._seq, self._value


def _make_handler(directory: str, leaf: _LeafState):
    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)

        def log_message(self, *_a) -> None:  # silence per-request stderr noise
            pass

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] == "/leaf.json":
                seq, value = leaf.snapshot()
                body = json.dumps({"seq": seq, "value": value}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

    return _Handler


def start_overlay_server(port: int, directory: pathlib.Path,
                         leaf: _LeafState) -> socketserver.ThreadingTCPServer:
    handler = _make_handler(str(directory), leaf)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, name="m10-overlay-http",
                     daemon=True).start()
    return httpd


def resolve_overlay_serving(log: TeeLog) -> tuple[pathlib.Path, str, str]:
    """Decide WHAT to serve to CEF and return (serve_dir, entry_path, engine).

    Preference order:
      1) SOLAR_OVERLAY_BUNDLE env → a directory holding a built Solar bundle
         (index.html + assets); served verbatim. The REAL #77 engine path.
      2) A discovered Solar dist/host bundle next to Pulsar (../Solar/dist/host).
      3) The self-contained Pulsar fallback page (scripts/live-test/
         m10-overlay.html) — reproduces the same wipe-cover timeline keyed off
         the same leaf, enough to prove the blend + cut-skew without Solar #11.
    """
    env_bundle = os.environ.get("SOLAR_OVERLAY_BUNDLE", "").strip()
    if env_bundle:
        d = pathlib.Path(env_bundle)
        idx = d / "index.html"
        if idx.exists():
            log(f"[overlay] serving REAL Solar bundle from SOLAR_OVERLAY_BUNDLE="
                f"{d} (our engine renders wipe-cover).")
            return d, "index.html", "solar-bundle"
        log(f"[overlay] SOLAR_OVERLAY_BUNDLE={d} has no index.html — ignoring.")

    solar_host = (REPO_ROOT.parent / "Solar" / "dist" / "host")
    if (solar_host / "index.html").exists():
        log(f"[overlay] serving discovered Solar host bundle from {solar_host} "
            "(our engine renders wipe-cover).")
        return solar_host, "index.html", "solar-bundle"

    log("[overlay] no Solar bundle found — serving the self-contained Pulsar "
        f"fallback overlay {OVERLAY_PAGE.name} (same wipe-cover timeline, same "
        "leaf; the real Solar bundle path is gated on #11/#77 being assembled).")
    return OVERLAY_DIR, OVERLAY_PAGE.name, "pulsar-fallback"


# --------------------------------------------------------------------------
# Process management — spawn pulsar.exe GPU-ON (no --disable-gpu) so WGC +
# CEF coexist (SPIKE-GPU #70). Native stinger forced OFF (the pivot world).
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
        # The pivot world: native stinger DORMANT (#73/#83). The overlay does
        # the visible transition; the cut is a bare program switch.
        env[NATIVE_STINGER_ENV] = "0"
        LIVE_VOD_DIR.mkdir(parents=True, exist_ok=True)
        env["PULSAR_RECORD_DIR"] = str(LIVE_VOD_DIR)
        # No window/mic/process-audio capture — monitor_capture + the CEF
        # overlay are the only sources (the overlay page is silent).
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)
        env.pop("PULSAR_PROCESS_AUDIO_NAME", None)

        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        # GPU-ON: NO --disable-gpu. SPIKE-GPU (#70) proved WGC monitor_capture
        # and the CEF browser_source coexist GPU-on in a headless agent context.
        self.proc = subprocess.Popen(
            [str(self.exe), "--no-sandbox"],
            cwd=str(self.exe.parent),
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
# obs-websocket v5 plumbing — mirrors probe-twitch-scene-switch.py.
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


def req_code(resp: dict) -> Optional[int]:
    return resp.get("requestStatus", {}).get("code")


# --------------------------------------------------------------------------
# The obs-ws CALL RECORDER + the stand-in CUT CONSUMER.
#
# Every obs-ws request the consumer issues passes through ObsCaller.call so:
#   - C-INJ asserts a rejected leaf produces ZERO calls;
#   - ordering asserts the cut was CAUSED by the leaf (calls bracket delivery);
#   - C-MECH asserts NO native-transition request was EVER issued.
# This mirrors Prism's injected ObsCaller (executor.ts) one-to-one. The cut is
# a BARE SetCurrentProgramScene — the stand-in has no transition step at all.
# --------------------------------------------------------------------------
class ObsCaller:
    def __init__(self, inbox: Inbox, ws) -> None:
        self.inbox = inbox
        self.ws = ws
        self.calls: list[tuple[float, str, dict]] = []
        self._n = 0

    async def call(self, request_type: str, data: dict | None = None) -> dict:
        self._n += 1
        self.calls.append((time.monotonic(), request_type, data or {}))
        return await request(
            self.inbox, self.ws, request_type, f"consumer-{self._n}", data
        )

    def native_transition_calls(self) -> list[str]:
        """C-MECH evidence: any native-transition request the consumer issued."""
        return [rt for (_t, rt, _d) in self.calls if rt in NATIVE_TRANSITION_REQUESTS]


async def apply_cut(obs: ObsCaller, ctrl: dict, log: TeeLog) -> bool:
    """Issue the HARD-CUT for a VALIDATED scene_control — a SINGLE
    SetCurrentProgramScene{target_scene}. NO SetCurrentSceneTransition,
    NO SetCurrentSceneTransitionSettings, NO duration: there is no OBS-native
    transition in the pivot (C-MECH). The visible transition is the Solar
    overlay; this is the invisible content swap under its opaque plateau."""
    r = await obs.call("SetCurrentProgramScene", {"sceneName": ctrl["target_scene"]})
    if not req_ok(r):
        raise RuntimeError(f"SetCurrentProgramScene: {r.get('requestStatus')}")
    return True


async def validate_leaf(path: str, value: Any, log: TeeLog) -> Optional[dict]:
    """The stand-in cut consumer's GATE for ONE leaf (C-PATHREAL → contract).
    Returns the validated ctrl, or None if the leaf is not ours / is rejected
    (a rejected leaf ⇒ ZERO obs-ws calls; the caller never reaches apply_cut)."""
    # C-PATHREAL — only the canonical 3-segment scene_control leaf is ours.
    try:
        slug = assert_canonical_leaf_path(path)
    except SceneControlContractError:
        return None  # not our leaf (Solar overlay inputs etc. land here too)

    # The single gate before any obs-ws call — the FROZEN overlay-form contract.
    try:
        ctrl = validate_scene_control(
            value,
            scene_allowlist=SCENE_ALLOWLIST,
            overlay_kind_allowlist=OVERLAY_KIND_ALLOWLIST,
        )
    except SceneControlContractError as exc:
        log(f"   [cut] REJECTED leaf {path!r} (slug={slug}): {exc} — 0 obs-ws calls")
        return None

    log(f"   [cut] ACCEPTED leaf {path!r} (slug={slug}): "
        f"target={ctrl['target_scene']} overlay={ctrl['overlay']['kind']} "
        f"cut_at_ms={ctrl['cut_at_ms']} window=[{ctrl['overlay']['reveal_ms']}, "
        f"{ctrl['overlay']['reveal_ms'] + ctrl['overlay']['hold_ms']}]")
    return ctrl


# --------------------------------------------------------------------------
# Pure-stdlib PNG decode + frame analysis (mirrors probe-m6-live.py) — for the
# A / MID / B overlay-blend proof. No PIL / numpy (CI-safe, license-clean).
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


def _mean_rgb(width: int, height: int, channels: int, px: bytearray) -> tuple[float, float, float]:
    total = width * height
    if total == 0:
        return (0.0, 0.0, 0.0)
    step = max(1, total // 40000)
    n = 0
    sr = sg = sb = 0
    for idx in range(0, total, step):
        base = idx * channels
        sr += px[base]
        sg += px[base + 1]
        sb += px[base + 2]
        n += 1
    return (sr / n, sg / n, sb / n)


def analyse_frame(width: int, height: int, channels: int, px: bytearray) -> dict:
    """Blank/content metrics + mean RGB. Mirrors probe-m6-live.analyse_frame and
    adds the mean colour used by the overlay-cover MID assertion."""
    total = width * height
    if total == 0:
        return {"distinct": 0, "nonbg_ratio": 0.0, "all_same": True,
                "modal": None, "mean": (0.0, 0.0, 0.0)}
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
        "all_same": all_same,
        "modal": (mr, mg, mb),
        "mean": _mean_rgb(width, height, channels, px),
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


async def capture_program_frame(inbox: Inbox, ws, rid: str) -> tuple[bytes, dict]:
    """GetSourceScreenshot of the CURRENT program scene → (png_bytes, metrics).
    Targets the current program scene by name (the composed program output)."""
    r = await request(inbox, ws, "GetCurrentProgramScene", f"{rid}-name", {})
    rd = r.get("responseData", {})
    scene = rd.get("currentProgramSceneName") or rd.get("sceneName")
    r = await request(inbox, ws, "GetSourceScreenshot", rid, {
        "sourceName": scene,
        "imageFormat": "png",
        "imageWidth": CANVAS_W,
        "imageHeight": CANVAS_H,
    })
    if not req_ok(r):
        raise RuntimeError(
            f"GetSourceScreenshot({scene}) failed: {r.get('requestStatus')}"
        )
    png = _strip_data_uri(r["responseData"]["imageData"])
    w, h, ch, pxs = decode_png(png)
    metrics = analyse_frame(w, h, ch, pxs)
    return png, metrics


async def warmup_capture_until_content(
    inbox: Inbox, ws, log: TeeLog, *, budget_s: float = WARMUP_POLL_BUDGET_S,
    interval_s: float = WARMUP_POLL_INTERVAL_S,
) -> tuple[Optional[bytes], dict, bool]:
    """Poll the program frame until the WGC capture yields real content, then
    return (png, metrics, content_ok). The first WGC frame is black (capture
    warmup); a single screenshot races that and frame A lands blank. This mirrors
    probe-spike-gpu-coexist.run_spike's poll loop — capture, decode, analyse, and
    loop until frame_is_content (the same predicate C5″ uses for A/B) or the
    budget expires. On expiry it returns the LAST decoded frame with
    content_ok=False so the caller can apply its --allow-blank policy and the
    diagnostic ('WGC warmup timed out') is precise. Capture errors / decode
    failures are tolerated within the budget (the screenshot may not be ready on
    the first attempts), exactly as the spike tolerates them."""
    deadline = time.monotonic() + budget_s
    attempt = 0
    last_png: Optional[bytes] = None
    last_metrics: dict = {"distinct": 0, "nonbg_ratio": 0.0, "all_same": True,
                          "modal": None, "mean": (0.0, 0.0, 0.0)}
    log(f"[warmup] polling the program frame until WGC capture is non-black "
        f"(budget {budget_s:.0f}s, interval {interval_s:.1f}s) — the first WGC "
        "frame is black (capture warmup); a fixed-sleep grab races it.")
    while time.monotonic() < deadline:
        attempt += 1
        try:
            png, metrics = await capture_program_frame(inbox, ws, f"warmup-{attempt}")
        except Exception as exc:  # noqa: BLE001 — screenshot may not be ready yet
            if attempt == 1 or attempt % 6 == 0:
                log(f"   [warmup] attempt {attempt}: screenshot not ready "
                    f"({type(exc).__name__}) — still warming up")
            await asyncio.sleep(interval_s)
            continue
        last_png, last_metrics = png, metrics
        if frame_is_content(metrics):
            log(f"   [warmup] attempt {attempt}: capture LIVE "
                f"(distinct={metrics['distinct']} nonbg={metrics['nonbg_ratio']*100:.1f}%) "
                "— WGC warmed up, proceeding to frame A.")
            return last_png, last_metrics, True
        if attempt == 1 or attempt % 6 == 0:
            log(f"   [warmup] attempt {attempt}: still black/blank "
                f"(distinct={metrics['distinct']} nonbg={metrics['nonbg_ratio']*100:.1f}%) "
                "— polling")
        await asyncio.sleep(interval_s)
    log(f"[warmup] WGC warmup timed out after {budget_s:.0f}s ({attempt} attempt(s)) "
        "— the capture never produced non-black content. On a real interactive GPU "
        "desktop this should not happen; in CI / a blank box it is expected.")
    return last_png, last_metrics, False


async def wait_solar_ready(
    log: TeeLog, *, overlay_settled: bool, delivery: str,
    orion: Optional["OrionStandIn"] = None,
    budget_s: float = SOLAR_READY_BUDGET_S,
) -> None:
    """Hold until the Solar overlay (the CEF bundle) is ready to REACT to the
    leaf, so the leaf we deliver next replays the wipe-cover instead of arriving
    before the bundle loaded. In --loopback-leaf this gates on the real signal:
    the Orion-WS stand-in reports a subscriber connected AND the render bundle
    fetched (the bundle's KeyframePlayer is then live on the stream). Otherwise it
    is a flat grace. Always best-effort: a timeout is a NOTE, never a failure —
    delivery still happens (and --allow-blank covers a non-rendering box)."""
    if not overlay_settled:
        return
    if delivery == "loopback-leaf" and orion is not None:
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            if orion.subscribe_count > 0 and orion.bundle_fetch_count > 0:
                log(f"[solar] overlay ready: Orion-WS stand-in saw "
                    f"{orion.subscribe_count} subscriber(s) + "
                    f"{orion.bundle_fetch_count} bundle fetch(es) — the Solar "
                    "bundle is connected and listening; the leaf will be reacted to.")
                await asyncio.sleep(SOLAR_READY_GRACE_S)
                return
            await asyncio.sleep(SOLAR_READY_POLL_INTERVAL_S)
        log(f"[solar] NOTE: no Solar subscriber/bundle-fetch on the Orion-WS "
            f"stand-in within {budget_s:.0f}s (subs={orion.subscribe_count} "
            f"fetches={orion.bundle_fetch_count}) — the real bundle may not have "
            "connected (CORS / light build); delivering anyway, the /leaf.json "
            "fallback page still drives the overlay.")
        return
    # No real readiness signal available — a flat grace before delivery.
    log(f"[solar] grace {SOLAR_READY_GRACE_S:.1f}s for the CEF overlay to settle "
        "before delivering the leaf.")
    await asyncio.sleep(SOLAR_READY_GRACE_S)


def is_overlay_cover(mid: dict) -> tuple[bool, str]:
    """C5″ — the MID frame is the OVERLAY COVER (the Solar wipe-cover at its
    opaque plateau), NOT a hard cut between two captures. The cover is a
    near-uniform opaque fill: very few distinct colours AND a mean near the
    cover fill colour. (A capture-to-capture hard cut would land MID on a busy
    desktop image — many distinct colours, mean far from the fill.)"""
    distinct = mid["distinct"]
    mr, mg, mb = mid["mean"]
    fr, fg, fb = COVER_FILL_RGB
    mean_dist = abs(mr - fr) + abs(mg - fg) + abs(mb - fb)
    covered = distinct <= COVER_MAX_DISTINCT and mean_dist <= COVER_MEAN_TOL
    return covered, (f"distinct={distinct} (<= {COVER_MAX_DISTINCT}?) "
                     f"mean={tuple(round(x) for x in mid['mean'])} "
                     f"|mean-fill|={mean_dist:.0f} (<= {COVER_MEAN_TOL}?)")


# --------------------------------------------------------------------------
# Live-wire delivery: fire the VPS Blue trigger + subscribe to /show/stream.
# --------------------------------------------------------------------------
def fire_blue_trigger(
    *, gateway_url: str, blueprint_id: str, operator_token: str, log: TeeLog,
) -> dict:
    """POST /blue/api/v1/blueprints/{id}/trigger with the operator Bearer in the
    HEADER (never the query — Blue ADR 001 R6). Returns the parsed JSON body
    (which carries outputs.scene_control). Raises on a non-2xx."""
    url = f"{gateway_url.rstrip('/')}/blue/api/v1/blueprints/{blueprint_id}/trigger"
    body = json.dumps({"inputs": {}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {operator_token}")
    log(f"   [trigger] POST {url} (operator Bearer in header)")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — https gateway
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"trigger HTTP {exc.code}: {detail}") from exc
    return payload


async def subscribe_show_stream(
    *, show_stream_url: str, leaf_path: str, deadline_s: float, log: TeeLog,
) -> tuple[float, Any]:
    """Open a read-only /show/stream subscription, send the ADR-002 subscribe
    frame, and return (recv_monotonic, value) for the FIRST delta/snapshot that
    carries ``leaf_path``. Never sends an input frame (read-only — a viewer
    token cannot write a leaf). The URL carries the viewer show-token; only its
    redacted form is logged (C-SEC)."""
    log(f"   [consumer] subscribing to {redact_show_stream_url(show_stream_url)} "
        "(read-only, pull, no inbound port)")
    async with websockets.connect(show_stream_url, max_size=2**24,
                                  ping_interval=None, open_timeout=15) as ws:
        await ws.send(json.dumps({"type": "subscribe", "v": 1, "since_sequence": None}))
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.monotonic()))
            t = time.monotonic()
            try:
                frame = json.loads(raw)
            except Exception:
                continue
            ftype = frame.get("type")
            if ftype == "snapshot" and isinstance(frame.get("state"), dict):
                if leaf_path in frame["state"]:
                    return t, frame["state"][leaf_path]
            elif ftype == "delta" and isinstance(frame.get("patches"), list):
                for patch in frame["patches"]:
                    if isinstance(patch, dict) and patch.get("path") == leaf_path:
                        return t, patch.get("value")
        raise asyncio.TimeoutError(
            f"no {leaf_path} delta on /show/stream within {deadline_s:.0f}s "
            "(F2 silent-drop? active scene not declaring the path? consumer not "
            "subscribed before the trigger fired?)"
        )


# --------------------------------------------------------------------------
# Scene setup — two WGC monitor_capture scenes (#84) + the CEF overlay scene.
# --------------------------------------------------------------------------
async def setup_scenes(inbox: Inbox, ws, log: TeeLog) -> int:
    """Create scene-screen-1 / scene-screen-2 (WGC monitor_capture) via the #84
    harness primitives. Returns 0 / 1 / 3 (typed skip). Mono-screen fallback is
    the #84 behaviour."""
    resp = await request(inbox, ws, "GetInputKindList", "kinds-mc", {})
    kinds = set(resp["responseData"]["inputKinds"])
    if MONITOR_CAPTURE_KIND not in kinds:
        log("SKIP: monitor_capture NOT registered — broken/headless build. "
            "Typed skip, NOT a pass.")
        return 3

    log("[setup] enumerating displays for monitor_capture (U1/#84) ...")
    setting_key, values = await m10_setup.enumerate_monitors(inbox, ws, log)
    if not values:
        log("FAIL: no displays enumerated — cannot pin monitor_capture")
        return 1
    n = len(values)
    value_1 = values[0]
    if n >= 2:
        value_2 = values[1]
        mono = False
    else:
        value_2 = values[0]
        mono = True
        log("   NOTE: single display — mono-screen fallback (both scenes pin the "
            "same display; the on-air 2-display proof needs a 2nd monitor).")

    log(f"[setup] creating {SCENE_SCREEN_1!r} (display 1, WGC) ...")
    s1 = await m10_setup.create_monitor_scene(
        inbox, ws, scene_name=SCENE_SCREEN_1, input_name="capture-screen-1",
        setting_key=setting_key, setting_value=value_1, log=log)
    log(f"[setup] creating {SCENE_SCREEN_2!r} (display 2, WGC) ...")
    s2 = await m10_setup.create_monitor_scene(
        inbox, ws, scene_name=SCENE_SCREEN_2, input_name="capture-screen-2",
        setting_key=setting_key, setting_value=value_2, log=log)

    t1, t2 = s1.get(setting_key), s2.get(setting_key)
    log(f"[setup] {setting_key}: screen-1={t1!r}  screen-2={t2!r}  mono={mono}")
    if not mono and t1 == t2:
        log("FAIL: both scenes pin the SAME monitor on a multi-display box.")
        return 1
    return 0


async def add_overlay_to_scenes(inbox: Inbox, ws, *, overlay_url: str,
                                log: TeeLog) -> bool:
    """Install the Solar overlay CEF browser_source on BOTH content scenes, so
    the opaque cover composites above the monitor_capture in either scene. The
    overlay is created once and added to each scene. Returns True if the
    browser_source rendered (False ⇒ light build, caller may skip the blend)."""
    settled = False
    for scene in (SCENE_SCREEN_1, SCENE_SCREEN_2):
        # The pulsar-scene vendor installs the browser_source on the CURRENT
        # scene (mirrors probe-twitch-scene-switch), so set it current first.
        await request(inbox, ws, "SetCurrentProgramScene", f"ov-cur-{scene}",
                      {"sceneName": scene})
        r = await vendor_call(inbox, ws, f"ov-set-{scene}", "pulsar-scene",
                              "SetCaptureSource", {
                                  "kind": BROWSER_SOURCE_KIND,
                                  "url": overlay_url,
                                  "width": CANVAS_W,
                                  "height": CANVAS_H,
                                  "fps": 60,
                                  "reroute_audio": False,
                              })
        data = vendor_response_data(r)
        if data.get("kind") == BROWSER_SOURCE_KIND:
            settled = True
            log(f"   [overlay] browser_source installed on {scene!r} → "
                f"{redact_show_stream_url(overlay_url)}")
        else:
            log(f"   [overlay] SetCaptureSource on {scene!r} did not return a "
                f"browser_source ({data}) — light build / no CEF?")
    return settled


# --------------------------------------------------------------------------
# C-MECH guard — assert the fork is NOT defaulting to a native stinger.
# --------------------------------------------------------------------------
async def assert_no_native_stinger_default(inbox: Inbox, ws, log: TeeLog) -> int:
    """The pivot precondition: with PULSAR_NATIVE_STINGER OFF (forced on spawn),
    the DEFAULT current transition must NOT be a stinger (a hard-cut must never
    route through an OBS-native transition — C-MECH). Returns 0 OK / 1 fail.

    A registered 'Stinger' INSTANCE while the flag is OFF means the binary does
    not yet gate the native stinger behind #73/#83 (today's main build) — that
    is a build-side deferral, NOT a C-MECH break, because the decisive C-MECH
    proof is that THIS stand-in consumer issues ZERO native-transition requests
    (asserted later from the recorded calls, build-independent). So a Stinger
    instance is a NOTE, not a fail; only a stinger DEFAULT transition fails."""
    r = await request(inbox, ws, "GetSceneTransitionList", "tr-list", {})
    names = {t["transitionName"]: t for t in
             r.get("responseData", {}).get("transitions", [])}
    if "Stinger" in names:
        log("[C-MECH pre] NOTE: a 'Stinger' instance is registered while the "
            "native stinger is OFF — this binary does not yet gate it behind the "
            "#73/#83 flag (build deferral). The real C-MECH proof (consumer "
            "issues ZERO native-transition requests) is asserted from the cut "
            "calls below, regardless of any registered instance.")
    r = await request(inbox, ws, "GetCurrentSceneTransition", "tr-cur", {})
    cur = r.get("responseData", {})
    if cur.get("transitionKind") == "obs_stinger_transition":
        log("FAIL: the DEFAULT current transition is a stinger — a hard-cut must "
            "not route through an OBS-native transition (C-MECH).")
        return 1
    log(f"[C-MECH pre] OK: default transition={cur.get('transitionName')!r} "
        f"kind={cur.get('transitionKind')!r} (not a stinger — the cut will be a "
        "bare program switch).")
    return 0


# --------------------------------------------------------------------------
# C-INJ negative test — every malicious leaf ⇒ 0 obs-ws calls.
# --------------------------------------------------------------------------
async def assert_anti_injection(obs: ObsCaller, log: TeeLog) -> int:
    """Drive the frozen reject corpus (fixtures/malicious.json) through the SAME
    consumer gate and assert ZERO obs-ws calls per case. Proves C-INJ holds at
    the live consumer, not just in the contract unit test."""
    corpus_path = _CONTRACTS_DIR / "scene_control" / "fixtures" / "malicious.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    failures = 0
    for case in corpus["cases"]:
        name = case["name"]
        before = len(obs.calls)
        path = case.get("bad_path", M10_LEAF_PATH)
        ctrl = await validate_leaf(path, case["value"], log)
        if ctrl is not None:
            # A leaf that wrongly validated — the gate failed.
            log(f"   [C-INJ] FAIL {name!r}: leaf VALIDATED but must be rejected")
            failures += 1
            continue
        issued = len(obs.calls) - before
        if issued != 0:
            log(f"   [C-INJ] FAIL {name!r}: {issued} obs-ws call(s) — MUST be 0")
            failures += 1
        else:
            log(f"   [C-INJ] ok {name!r}: rejected, 0 obs-ws calls "
                f"({case.get('invariant')})")
    if failures:
        log(f"FAIL: {failures} anti-injection case(s) (C-INJ).")
        return 1
    log(f"[C-INJ] OK: all {len(corpus['cases'])} off-contract leaves rejected "
        "with 0 obs-ws calls.")
    return 0


# --------------------------------------------------------------------------
# Broadcast + the M10 overlay proof sequence.
# --------------------------------------------------------------------------
async def run_proof(
    *, inbox: Inbox, ws, obs: ObsCaller, args, redactor: Redactor, log: TeeLog,
    stream_key: str, leaf_state: _LeafState, overlay_settled: bool, engine: str,
    orion: Optional["OrionStandIn"] = None,
) -> int:
    duration = args.duration
    deliver_at = duration / 2.0
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    # Go live on screen-1 (the program scene before the cut).
    if not req_ok(await request(inbox, ws, "SetCurrentProgramScene", "go-s1",
                                {"sceneName": SCENE_SCREEN_1})):
        log("FAIL: could not set program scene to screen-1 pre-flight.")
        return 1
    log(f"[proof] program scene = {SCENE_SCREEN_1!r} (on air, overlay above it)")

    # NO PRE-WARM of screen-2 here (the earlier double-switch was removed, #79).
    # WGC keeps warm ONLY the capture of the scene currently PROGRAM: pre-warming
    # screen-2 (making it program, warming it, switching back) COOLED screen-1, so
    # screen-1 had to re-warm and frame A landed black. The natural mechanism is
    # the right one: screen-1 stays program through go-live and frame A (so it
    # warms and frame A is content); screen-2 becomes program AT the cut and warms
    # UNDER the opaque overlay plateau (invisible to the viewer), then a frame-B
    # warmup-poll (after the cut + overlay retract, below) waits for it to deliver
    # real content before capturing B. No screen-2 program switch happens here.

    dest_id: Optional[str] = None
    recording = False
    if args.broadcast:
        r = await vendor_call(inbox, ws, "create-dest", "pulsar", "CreateDestination",
                              {"name": DESTINATION_NAME, "kind": "twitch", "key": stream_key})
        dest_id = vendor_response_data(r).get("id")
        if not dest_id:
            log(f"FAIL: CreateDestination no id; status={vendor_request_status(r)}")
            return 1
        r = await vendor_call(inbox, ws, "start-dest", "pulsar", "StartDestination",
                              {"id": dest_id})
        if not vendor_response_data(r).get("started"):
            log(f"FAIL: StartDestination not started; status={vendor_request_status(r)}")
            await vendor_call(inbox, ws, "rm-dest", "pulsar", "RemoveDestination",
                              {"id": dest_id})
            return 1
        log("-> StartDestination started=true — LIVE on Twitch")
    else:
        log("-> --no-broadcast: NOT going live to Twitch (proof-only). The "
            "overlay + cut are proven OFF AIR. The Twitch leg is the antenna run.")

    r = await request(inbox, ws, "StartRecord", "start-rec", {})
    if req_ok(r):
        recording = True
        log(f"-> StartRecord ok (VOD under {LIVE_VOD_DIR})")
    else:
        log(f"   warn: StartRecord declined: {r.get('requestStatus')}")

    # WARMUP: poll the program frame until WGC capture is live BEFORE grabbing
    # frame A. The first WGC frame is black (capture warmup, SPIKE-GPU #70) — a
    # fixed sleep raced it and frame A landed blank. Only a budget expiry that is
    # STILL blank is a real failure (and --allow-blank downgrades it).
    png_a, m_a, content_ok = await warmup_capture_until_content(inbox, ws, log)
    if png_a is None:
        log("FAIL: never decoded a program frame during warmup — "
            "GetSourceScreenshot kept failing (the program scene produced none).")
        if dest_id:
            await _stop_broadcast(inbox, ws, dest_id, recording, log)
        return 1
    # Also wait for Solar to be ready to react before we deliver the leaf, so the
    # wipe-cover actually replays (the leaf is delivered later, at deliver_at).
    await wait_solar_ready(log, overlay_settled=overlay_settled,
                           delivery=args.delivery, orion=orion)
    (FRAMES_DIR / "frame-A-screen1.png").write_bytes(png_a)
    log(f"[frame A] screen-1: mean={tuple(round(x) for x in m_a['mean'])} "
        f"distinct={m_a['distinct']} nonbg={m_a['nonbg_ratio']*100:.1f}% "
        f"content={content_ok} -> {FRAMES_DIR / 'frame-A-screen1.png'}")
    if not content_ok and not args.allow_blank:
        log("FAIL: frame A is blank after WGC warmup timed out — screen-1 WGC "
            "capture never produced content within the warmup budget. (A CI box "
            "with no real desktop can pass --allow-blank to exercise the wire "
            "without the visual assertion.)")
        if dest_id:
            await _stop_broadcast(inbox, ws, dest_id, recording, log)
        return 1

    pre_scene = await _current_scene(inbox, ws)
    log(f"[proof] pre-cut program scene = {pre_scene!r}")

    rc = 0
    start_t = time.time()
    delivered = False
    while time.time() - start_t < duration:
        await asyncio.sleep(min(POLL_INTERVAL_SEC,
                                max(0.2, deliver_at - (time.time() - start_t))))
        elapsed = time.time() - start_t

        if not delivered and elapsed >= deliver_at:
            rc = await _do_overlay_cut(
                inbox=inbox, ws=ws, obs=obs, args=args, redactor=redactor,
                log=log, leaf_state=leaf_state, overlay_settled=overlay_settled,
                pre_scene=pre_scene, orion=orion, m_a=m_a)
            delivered = True
            if rc != 0:
                break

        # Broadcast health polling (only when live).
        if dest_id:
            r = await vendor_call(inbox, ws, f"get-dest-{int(elapsed)}", "pulsar",
                                  "GetDestinations", {})
            ours = next((d for d in vendor_response_data(r).get("destinations", [])
                         if d.get("id") == dest_id), None)
            if not ours or not ours.get("active"):
                log(f"FAIL: destination not active at t={elapsed:.0f}s: {ours}")
                rc = 1
                break

    if rc == 0 and not delivered:
        log("FAIL: run ended before the overlay/cut fired.")
        rc = 1

    # Stop cleanly.
    if recording:
        try:
            r = await request(inbox, ws, "StopRecord", "stop-rec", {})
            vod = (r.get("responseData", {}) or {}).get("outputPath")
            if vod:
                log(f"-> StopRecord finalised VOD: {vod}")
        except Exception as exc:  # noqa: BLE001
            log(f"   warn: StopRecord error: {exc}")
    if dest_id:
        await _stop_broadcast(inbox, ws, dest_id, False, log)
    return rc


async def _do_overlay_cut(
    *, inbox, ws, obs: ObsCaller, args, redactor: Redactor, log: TeeLog,
    leaf_state: _LeafState, overlay_settled: bool, pre_scene,
    orion: Optional["OrionStandIn"] = None, m_a: Optional[dict] = None,
) -> int:
    """Deliver the leaf (overlay + stand-in cut), schedule the hard-cut at
    cut_at_ms under the opaque plateau, capture the MID + B frames, and run the
    C5″ / C-CUT(SPIKE-CUT) / C-MECH / ordering proofs."""
    log(f"\n** M10 OVERLAY+CUT — delivering the scene_control leaf via "
        f"{args.delivery} **")
    delivered_t, value = await deliver_leaf(
        args=args, redactor=redactor, log=log, leaf_state=leaf_state,
        orion=orion)
    if value != DEMO_SCENE_CONTROL_VALUE:
        log("   note: received leaf differs from the pinned demo shape; "
            "validating against the contract regardless.")

    # The SAME leaf drives the overlay (CEF, via /leaf.json) AND the cut. Push
    # it to the overlay endpoint NOW (synchronised delivery instant).
    seq = leaf_state.set(json.loads(json.dumps(value)))
    overlay_t0 = time.monotonic()
    log(f"   [overlay] leaf pushed to CEF (seq={seq}); the same leaf clocks the "
        "cut — one leaf, co-specified (the leaf IS the synchronisation contract).")

    ctrl = await validate_leaf(M10_LEAF_PATH, value, log)
    if ctrl is None:
        log("FAIL: the stand-in cut consumer did not accept the delivered leaf.")
        return 1

    reveal = ctrl["overlay"]["reveal_ms"]
    hold = ctrl["overlay"]["hold_ms"]
    cut_at = ctrl["cut_at_ms"]
    opaque_start, opaque_end = reveal, reveal + hold

    # Capture the MID frame at mid-plateau (between opaque_start and opaque_end),
    # BEFORE the cut, so the frame shows the overlay covering the OLD scene while
    # opacity≈1 — then fire the cut at cut_at_ms (still under the plateau).
    mid_target_ms = (opaque_start + opaque_end) / 2.0
    mid_wait = max(0.0, (mid_target_ms - (time.monotonic() - overlay_t0) * 1000.0) / 1000.0)
    await asyncio.sleep(mid_wait)
    cef_opacity_mid = await _read_overlay_opacity(inbox, ws, log)
    png_mid, m_mid = await capture_program_frame(inbox, ws, "frame-mid")
    (FRAMES_DIR / "frame-MID-overlay.png").write_bytes(png_mid)
    log(f"[frame MID] mean={tuple(round(x) for x in m_mid['mean'])} "
        f"distinct={m_mid['distinct']} cef_opacity~{cef_opacity_mid} "
        f"-> {FRAMES_DIR / 'frame-MID-overlay.png'}")

    # Fire the HARD-CUT at cut_at_ms (under the plateau). Record the opacity
    # the overlay reports AT the cut instant (SPIKE-CUT skew evidence).
    now_ms = (time.monotonic() - overlay_t0) * 1000.0
    await asyncio.sleep(max(0.0, (cut_at - now_ms) / 1000.0))
    cef_opacity_at_cut = await _read_overlay_opacity(inbox, ws, log)
    calls_before = len(obs.calls)
    applied = await apply_cut(obs, ctrl, log)
    if not applied:
        log("FAIL: the stand-in did not apply the hard-cut (criterion C-CUT).")
        return 1

    # ORDERING — the cut happened AFTER the leaf was delivered.
    first_call_t = obs.calls[calls_before][0]
    if first_call_t < delivered_t:
        log(f"FAIL: cut at {first_call_t:.3f} preceded leaf delivery at "
            f"{delivered_t:.3f} — not caused by the delta (ordering).")
        return 1
    log(f"[ordering] OK: hard-cut issued AFTER leaf delivery "
        f"(Δ={first_call_t - delivered_t:.3f}s) — CAUSED by the delta.")

    # C-MECH — the cut was a BARE SetCurrentProgramScene; NO native transition.
    seq_types = [c[1] for c in obs.calls[calls_before:]]
    native = obs.native_transition_calls()
    if native:
        log(f"FAIL: C-MECH — native transition request(s) issued: {native}. The "
            "pivot forbids any OBS-native transition; the cut must be a bare "
            "SetCurrentProgramScene.")
        return 1
    if seq_types != ["SetCurrentProgramScene"]:
        log(f"FAIL: C-MECH — cut sequence {seq_types} != ['SetCurrentProgramScene'] "
            "(the hard-cut must be a single program switch, no transition steps).")
        return 1
    log(f"[C-MECH] OK: cut = {seq_types}; ZERO native-transition requests across "
        f"the whole run ({len(obs.calls)} obs-ws call(s) total).")

    # Settle past the retract, capture FRAME B (screen-2, overlay transparent).
    await asyncio.sleep(max(0.6, ctrl["overlay"]["retract_ms"] / 1000.0 + 0.4))
    now = await _current_scene(inbox, ws)
    if now != SCENE_SCREEN_2:
        log(f"FAIL: program scene did not flip to {SCENE_SCREEN_2!r} (got {now!r}) "
            "— C-CUT.")
        return 1
    # FRAME-B WARMUP-POLL (#79). screen-2 only became PROGRAM at the cut, and WGC
    # keeps warm only the program scene's capture — so screen-2's duplicator starts
    # cold here and its FIRST frame is black (SPIKE-GPU #70), exactly as screen-1's
    # was at go-live. screen-2 has been warming UNDER the opaque overlay plateau
    # since the cut (invisible to the viewer); we now poll its program capture until
    # it yields real content before grabbing frame B, reusing the SAME predicate and
    # loop as the frame-A warmup. This runs AFTER the cut/ordering/C-MECH have all
    # been measured, so waiting here affects NO sequencing assertion — B only
    # confirms screen-2 shows real content once the transition completes. The poll
    # uses raw GetSourceScreenshot reads (capture_program_frame), NOT obs.call, so
    # obs.calls (and thus C-MECH) is untouched. A budget expiry that is still blank
    # is tolerated by --allow-blank (CI box with no displays), like frame A.
    png_b, m_b, b_content_ok = await warmup_capture_until_content(inbox, ws, log)
    if png_b is None:
        log("FAIL: never decoded a screen-2 program frame during the frame-B "
            "warmup — GetSourceScreenshot kept failing after the cut.")
        return 1
    (FRAMES_DIR / "frame-B-screen2.png").write_bytes(png_b)
    log(f"[frame B] screen-2: mean={tuple(round(x) for x in m_b['mean'])} "
        f"distinct={m_b['distinct']} nonbg={m_b['nonbg_ratio']*100:.1f}% "
        f"content={b_content_ok} -> {FRAMES_DIR / 'frame-B-screen2.png'}")
    log(f"[C-CUT] program scene flipped {pre_scene!r} -> {now!r} OK")

    # ---- C-CUT + SPIKE-CUT: the cut fell UNDER the opaque plateau ----
    in_window = opaque_start <= cut_at <= opaque_end
    log(f"[SPIKE-CUT] cut_at_ms={cut_at} opaque window=[{opaque_start}, "
        f"{opaque_end}] in_window={in_window}; CEF opacity at cut~"
        f"{cef_opacity_at_cut}. Skew margins: cut-{opaque_start}="
        f"{cut_at - opaque_start}ms after reveal-end; {opaque_end}-cut="
        f"{opaque_end - cut_at}ms before retract-start.")
    if not in_window:
        log("FAIL: SPIKE-CUT — cut_at_ms is OUTSIDE the opaque plateau; the "
            "content snap would be SEEN. (Contract should have rejected this — "
            "internal inconsistency.)")
        return 1

    # The MID frame is the OVERLAY COVER (Solar wipe-cover at its opaque
    # plateau). Computed once here; both SPIKE-CUT (frame path) and C5″ read it.
    mid_covered, mid_why = is_overlay_cover(m_mid)
    a_varied = m_a is not None and frame_is_content(m_a)
    b_varied = frame_is_content(m_b)

    # ---- SPIKE-CUT: the cut fired UNDER the opaque plateau ----
    # The REAL bundle exposes no window.__m10 (it is Solar's @lumencast/runtime,
    # not the fallback page), so opacity read-by-eval is usually None. The
    # STRONGER proof — and the one actually on the antenna — is by FRAME: the
    # MID frame, captured mid-plateau, is a uniform cover (mid_covered) sitting
    # between VARIED A and B; the cut fired inside that same plateau window
    # (in_window, proven above) → it landed under the cover → invisible. We
    # prefer this; opacity (when present) is corroborating, never required.
    if cef_opacity_at_cut is not None and cef_opacity_at_cut >= 0.97:
        log(f"[SPIKE-CUT] OK (opacity): real CEF overlay opacity "
            f"{cef_opacity_at_cut:.3f} >= 0.97 at the cut instant — corroborates "
            "the frame proof.")
    elif overlay_settled and mid_covered and a_varied and b_varied:
        log(f"[SPIKE-CUT] OK (frame): MID is the uniform opaque cover ({mid_why}) "
            "between VARIED A and B, and the cut fell inside the same plateau "
            f"window [{opaque_start}, {opaque_end}] — the content swap happened "
            "UNDER the cover, never seen. This is the antenna-true proof (what is "
            "actually broadcast), stronger than an eval'd opacity number.")
    elif args.allow_blank:
        log("[SPIKE-CUT] neither opacity>=0.97 nor a frame-cover proof available "
            "but --allow-blank: WGC/Solar may not render on this CI box. The "
            "cut-WINDOW invariant is still proven by contract + timing; the "
            "invisible-cut visual proof is the antenna run.")
    else:
        log("FAIL: SPIKE-CUT — could not prove the cut was hidden. Opacity "
            f"readback={cef_opacity_at_cut}; MID-cover={mid_covered} "
            f"({mid_why}); A varied={a_varied}; B varied={b_varied}. A uniform "
            "MID is only a 'cover' when A and B are real varied content; a "
            "blank-everywhere run is NOT a hidden cut and fails here.")
        return 1

    # ---- C5″: the MID frame is the overlay COVER, not a hard cut ----
    if not overlay_settled:
        log("[C5″] overlay browser_source did not render (light build / no CEF) "
            "— overlay-blend visual assertion SKIPPED; wire + cut proven.")
    elif mid_covered and a_varied and b_varied:
        # The HARDENED proof (#79): a uniform MID is meaningful ONLY when it sits
        # between REAL varied screen content. Black-everywhere can no longer pass.
        log(f"[C5″] OVERLAY-BLEND OK: A is varied content (distinct={m_a['distinct']}"
            f", nonbg={m_a['nonbg_ratio']*100:.1f}%), B is varied content "
            f"(distinct={m_b['distinct']}, nonbg={m_b['nonbg_ratio']*100:.1f}%), "
            f"and MID is the uniform opaque Solar cover ({mid_why}) — the overlay "
            "visibly REPLACED varied content with a flat fill, NOT a hard cut "
            "between two captures, NOT a screen that was blank throughout.")
    elif args.allow_blank:
        log(f"[C5″] inconclusive (MID-cover={mid_covered}: {mid_why}; A varied="
            f"{a_varied}; B varied={b_varied}) but --allow-blank: WGC/Solar may "
            "not render on this CI box. The overlay-blend visual proof is the "
            "antenna run.")
    else:
        if mid_covered and not (a_varied and b_varied):
            log(f"[C5″] FAIL: MID looks uniform ({mid_why}) but A varied="
                f"{a_varied} / B varied={b_varied} — a uniform MID between blank "
                "frames is INDISTINGUISHABLE from a screen that was black the "
                "whole time. This is the blank false-positive C5″ now rejects: "
                "the cover only proves anything against VARIED A/B content.")
        else:
            log(f"[C5″] FAIL: MID frame is NOT the opaque overlay cover ({mid_why}) "
                "— the screen change was a visible hard cut, not covered by the "
                "Solar overlay.")
        return 1

    log(f"[proof] M10 overlay pivot proven (engine={getattr(args, '_engine', 'unknown')}): "
        "Solar wipe-cover covers the screen; the hard-cut fires invisibly under "
        "the opaque plateau; NO OBS-native transition.")
    return 0


async def _read_overlay_opacity(inbox: Inbox, ws, log: TeeLog) -> Optional[float]:
    """Read the overlay's live cover opacity from window.__m10 in the CEF page,
    via the pulsar-scene vendor's browser eval (if the fork exposes it). Returns
    None when the readback path is unavailable (real Solar bundle without the
    __m10 shim, or a light build) — the proof then leans on contract + timing."""
    try:
        r = await vendor_call(inbox, ws, "ov-op", "pulsar-scene", "EvalBrowser", {
            "expression": "JSON.stringify({op: (window.__m10 && window.__m10.opacity)})",
        })
        data = vendor_response_data(r)
        raw = data.get("result") or data.get("value")
        if isinstance(raw, str):
            parsed = json.loads(raw)
            op = parsed.get("op")
            if isinstance(op, (int, float)):
                return float(op)
    except Exception as exc:  # noqa: BLE001 — readback is best-effort
        log(f"   [overlay] opacity readback unavailable ({type(exc).__name__}).")
    return None


async def _current_scene(inbox: Inbox, ws) -> Optional[str]:
    r = await request(inbox, ws, "GetCurrentProgramScene", f"cur-{_secrets.token_hex(3)}", {})
    rd = r.get("responseData", {})
    return rd.get("currentProgramSceneName") or rd.get("sceneName")


async def _stop_broadcast(inbox: Inbox, ws, dest_id: str, recording: bool, log: TeeLog) -> None:
    try:
        await vendor_call(inbox, ws, "stop-dest", "pulsar", "StopDestination", {"id": dest_id})
        await vendor_call(inbox, ws, "rm-dest", "pulsar", "RemoveDestination", {"id": dest_id})
        log("-> StopDestination + RemoveDestination ok")
    except Exception as exc:  # noqa: BLE001
        log(f"   warn: stop/remove destination error: {exc}")


async def deliver_leaf(*, args, redactor: Redactor, log: TeeLog,
                       leaf_state: _LeafState,
                       orion: Optional["OrionStandIn"] = None) -> tuple[float, Any]:
    """Deliver the scene_control leaf, returning (recv_t, value).

    --loopback-leaf : inject the demo leaf VALUE directly (proof-only; the same
                      bytes Orion would fan out), timestamp = now. ALSO fans a
                      leaf delta through the Orion-WS stand-in so the REAL Solar
                      bundle's KeyframePlayer replays the wipe-cover (the delta
                      value is a leaf-grain primitive — LSDP forbids the object
                      on the wire; see m10_orion_standin docstring).
    --live-wire     : fire the VPS Blue /trigger, then read the FIRST
                      __inputs.blue.<slug>.scene_control delta off the REAL
                      gateway /show/stream."""
    if args.delivery == "loopback-leaf":
        log("   [loopback-leaf] injecting the demo scene_control leaf value "
            "(proof-only; identical bytes to Orion's fan-out).")
        if orion is not None:
            try:
                n = await asyncio.to_thread(orion.deliver_leaf)
                log(f"   [loopback-leaf] Orion-WS stand-in fanned the leaf delta "
                    f"to {n} Solar subscriber(s) — the real bundle replays "
                    "wipe-cover off the wire (not the /leaf.json fallback).")
            except Exception as exc:  # noqa: BLE001 — fallback page still drives
                log(f"   [loopback-leaf] Orion delta fan-out failed "
                    f"({type(exc).__name__}: {redactor(str(exc))}); the "
                    "/leaf.json fallback page still drives the overlay.")
        return time.monotonic(), json.loads(json.dumps(DEMO_SCENE_CONTROL_VALUE))

    # live-wire — fire on the VPS, receive off /show/stream.
    gw = args.gateway_url
    operator = os.environ.get("M8_OPERATOR_TOKEN", "").strip()
    show_token = os.environ.get("M10_SHOW_TOKEN", "").strip()
    bp = args.blueprint_id
    if not (gw and operator and bp and show_token):
        raise RuntimeError(
            "--live-wire needs M8_GATEWAY_URL, M8_OPERATOR_TOKEN, "
            "M10_BLUEPRINT_ID, M10_SHOW_TOKEN (etage-1). Missing one — refusing "
            "to half-fire. Use --loopback-leaf for the VPS-less proof."
        )
    redactor.add(operator, "operator-jwt")
    redactor.add(show_token, "show-token")
    show_url = (
        gw.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        + f"/orion/api/v1/show/stream?token={show_token}"
    )
    # Subscribe FIRST (so we never miss the delta), THEN fire the trigger.
    sub_task = asyncio.create_task(subscribe_show_stream(
        show_stream_url=show_url, leaf_path=M10_LEAF_PATH, deadline_s=30.0, log=log))
    await asyncio.sleep(1.0)  # let the subscription establish + snapshot drain
    body = await asyncio.to_thread(
        fire_blue_trigger, gateway_url=gw, blueprint_id=bp,
        operator_token=operator, log=log)
    outputs = body.get("outputs", {}) if isinstance(body, dict) else {}
    log(f"   [trigger] 200 OK; outputs.scene_control present="
        f"{bool(outputs.get('scene_control'))}")
    recv_t, value = await sub_task
    log("   [consumer] received the scene_control leaf delta off /show/stream "
        "(C-FANOUT: not silent-dropped — the active scene declared the path).")
    return recv_t, value


# --------------------------------------------------------------------------
# Top-level: connect, guard, setup, prove, reap, grep-assert.
# --------------------------------------------------------------------------
async def run(*, ws_url: str, password: str, args, redactor: Redactor,
              log: TeeLog, stream_key: str, overlay_url: str,
              leaf_state: _LeafState, engine: str,
              orion: Optional["OrionStandIn"] = None) -> int:
    redactor.add(password, "obs-ws-password")
    args._engine = engine
    log(f"connecting: {ws_url}")
    async with websockets.connect(
        ws_url, subprotocols=["obswebsocket.json"], max_size=2**24,
        ping_interval=None, close_timeout=15, open_timeout=10,
    ) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if hello.get("op") != 0:
            log(f"error: expected Hello (op=0), got {hello}")
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
            log(f"error: identify failed: {ident}")
            return 1
        log("identified (v5 auth OK)")

        inbox = Inbox()

        # C-MECH precondition — the native stinger is dormant by default.
        rc = await assert_no_native_stinger_default(inbox, ws, log)
        if rc != 0:
            return rc

        # Setup the two WGC monitor_capture scenes (#84).
        rc = await setup_scenes(inbox, ws, log)
        if rc != 0:
            return rc

        # Install the Solar overlay CEF browser_source above both scenes.
        kinds = set((await request(inbox, ws, "GetInputKindList", "kinds-bs", {}))
                    ["responseData"]["inputKinds"])
        overlay_settled = False
        if BROWSER_SOURCE_KIND in kinds:
            overlay_settled = await add_overlay_to_scenes(
                inbox, ws, overlay_url=overlay_url, log=log)
        else:
            log("   [overlay] browser_source NOT registered (light build / no "
                "CEF) — the overlay cannot render; wire + cut still proven.")

        obs = ObsCaller(inbox, ws)

        # C-INJ — drive the reject corpus through the cut gate, expect 0 calls.
        rc = await assert_anti_injection(obs, log)
        if rc != 0:
            return rc
        if len(obs.calls) != 0:
            log(f"FAIL: anti-injection left {len(obs.calls)} obs-ws call(s) behind.")
            return 1

        # F2 (in-process) — prove the active Orion scene declares the leaf path.
        try:
            m10_setup.build_orion_declaration()
            log(f"[F2] OK: the Orion declaration scene declares {M10_LEAF_PATH} "
                "(C-FANOUT precondition; in-process round-trip clean).")
        except SystemExit as exc:
            log(f"FAIL (F2): {exc}")
            return 1

        return await run_proof(
            inbox=inbox, ws=ws, obs=obs, args=args, redactor=redactor, log=log,
            stream_key=stream_key, leaf_state=leaf_state,
            overlay_settled=overlay_settled, engine=engine, orion=orion)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar M10 overlay live e2e probe")
    ap.add_argument("--exe", type=pathlib.Path,
                    default=pathlib.Path(os.environ.get("PULSAR_EXE", str(DEFAULT_EXE))))
    ap.add_argument("--duration", type=int,
                    default=int(os.environ.get("LIVE_TEST_DURATION", "30")),
                    help="run seconds (default 30); the overlay+cut fires at /2")
    ap.add_argument("--fps", type=int,
                    default=int(os.environ.get("LIVE_TEST_FPS", "60")))
    ap.add_argument("--no-broadcast", dest="broadcast", action="store_false",
                    help="proof-only: run the full chain WITHOUT going live to "
                         "Twitch (no stream key needed). The mode Forge runs.")
    ap.add_argument("--broadcast", dest="broadcast", action="store_true",
                    help="go live to Twitch (needs TWITCH_STREAM_KEY, etage-1)")
    ap.set_defaults(broadcast=False)
    ap.add_argument("--loopback-leaf", dest="delivery", action="store_const",
                    const="loopback-leaf",
                    help="inject the leaf locally (VPS-less integration proof)")
    ap.add_argument("--live-wire", dest="delivery", action="store_const",
                    const="live-wire",
                    help="fire the real VPS Blue trigger + read off /show/stream "
                         "(needs M8_GATEWAY_URL/M8_OPERATOR_TOKEN/M10_BLUEPRINT_ID/"
                         "M10_SHOW_TOKEN)")
    ap.add_argument("--gateway-url", default=os.environ.get("M8_GATEWAY_URL", ""))
    ap.add_argument("--blueprint-id", default=os.environ.get("M10_BLUEPRINT_ID", ""))
    ap.add_argument("--allow-blank", action="store_true",
                    help="do not fail on a blank/identical capture or an overlay "
                         "that did not render (headless/CI box) — the wire + cut "
                         "+ C-MECH + C-INJ + C-FANOUT + C-SEC are still asserted; "
                         "the visual blend + skew are then the antenna run")
    ap.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S)
    args = ap.parse_args()
    if args.delivery is None:
        args.delivery = "live-wire"  # default = Keeper's antenna run

    redactor = Redactor()
    log = TeeLog(redactor)

    exe: pathlib.Path = args.exe
    if not exe.exists():
        log(f"error: pulsar.exe not found at {exe}")
        log("Build it first: scripts/build-win.ps1 -Full")
        return 2
    if not OVERLAY_PAGE.exists():
        log(f"error: overlay page missing at {OVERLAY_PAGE}")
        return 2
    if args.duration < 8:
        log("error: --duration must be >= 8s so the overlay + cut have room.")
        return 2

    stream_key = ""
    if args.broadcast:
        stream_key = os.environ.get("TWITCH_STREAM_KEY", "").strip()
        if not stream_key:
            log("error: TWITCH_STREAM_KEY empty and --broadcast set. Set it from "
                "the etage-1 secret (never commit) or drop --broadcast. Refusing "
                "to broadcast.")
            return 2
        redactor.add(stream_key, "stream-key")

    # Resolve what to serve to CEF + start the overlay HTTP server.
    serve_dir, entry, engine = resolve_overlay_serving(log)
    leaf_state = _LeafState()
    http_port = find_free_port()
    httpd = start_overlay_server(http_port, serve_dir, leaf_state)

    # Start the loopback Orion-WS stand-in (#79). The REAL Solar bundle gets its
    # scene + leaf deltas ONLY from this stream in mode=broadcast; the bundle
    # GET is served on the SAME port so the runtime's baseUrl (derived from the
    # orion= host) resolves the wipe-cover RenderBundle. The fallback page does
    # not need it (it polls /leaf.json) but the extra orion= param is harmless.
    orion: Optional[OrionStandIn] = None
    ws_standin_port = find_free_port()
    try:
        orion = OrionStandIn(port=ws_standin_port, leaf_path=M10_LEAF_PATH, log=log)
        orion.start()
        log(f"[orion] loopback Orion-WS stand-in on {orion.orion_ws_url} "
            "(LSDP/1.1 snapshot+delta + bundle GET; the real Solar bundle "
            "renders wipe-cover from THIS stream).")
    except Exception as exc:  # noqa: BLE001 — fall back to the leaf-poll path
        log(f"[orion] could not start the Orion-WS stand-in ({type(exc).__name__}: "
            f"{redactor(str(exc))}); the real Solar bundle will have no scene "
            "source — only the /leaf.json fallback page can render.")
        orion = None

    # The overlay URL CEF loads. The REAL Solar host reads `orion=` (its mount()
    # bootstrap: orionUrl = params.get('orion') ?? wss://${host}/orion/...) and
    # `mode=broadcast`; a dummy viewer token satisfies the subscribe frame. The
    # fallback page ignores both and polls /leaf.json off the same origin.
    orion_q = ""
    if orion is not None:
        orion_q = "&orion=" + urllib.parse.quote(orion.orion_ws_url, safe="") \
            + "&token=m10-viewer-standin"
    overlay_url = f"http://127.0.0.1:{http_port}/{entry}?mode=broadcast{orion_q}"
    log(f"[overlay] serving {serve_dir} on http://127.0.0.1:{http_port} "
        f"(engine={engine}); CEF loads {entry} "
        f"{'(scene via orion= stand-in)' if orion else '(no orion stand-in)'}")

    port = find_free_port()
    password = _secrets.token_urlsafe(16)
    redactor.add(password, "obs-ws-password")
    log(f"spawning: {exe}  (GPU-ON — no --disable-gpu; WGC+CEF coexist, SPIKE-GPU #70)")
    log(f"  PULSAR_PORT={port}  PULSAR_PASSWORD=<redacted {len(password)} chars>")
    log(f"  delivery={args.delivery}  broadcast={'ON' if args.broadcast else 'OFF (proof-only)'}"
        f"  {NATIVE_STINGER_ENV}=0 (pivot — native stinger dormant)")

    pulsar = PulsarProcess(exe, port, password, args.fps)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        redactor.add(sentinel_pw, "obs-ws-password")
        log(f"READY: {ws_url}")
        rc = asyncio.run(run(ws_url=ws_url, password=sentinel_pw, args=args,
                             redactor=redactor, log=log, stream_key=stream_key,
                             overlay_url=overlay_url, leaf_state=leaf_state,
                             engine=engine, orion=orion))
    except KeyboardInterrupt:
        log("interrupted")
        rc = 130
    except Exception as exc:  # noqa: BLE001 — top-level probe diagnostic
        log(f"FAIL: {redactor(str(exc))}")
        if pulsar.proc is not None:
            log(redactor(pulsar.diag()))
        rc = 1
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        if orion is not None:
            try:
                orion.stop()
            except Exception:
                pass
        if rc not in (0, 3):
            for ln in pulsar.lines[-60:]:
                log(redactor(f"  | {ln}"))
        pulsar.shutdown()
        if pulsar.proc is not None and pulsar.proc.poll() is None:
            log("error: pulsar.exe still running after shutdown")
            rc = rc or 1
        else:
            log("pulsar.exe reaped cleanly")

    # ---- C-SEC grep-assert: no live secret in stdout or any saved PNG ----
    leaked = set(redactor.leaks(log.text()))
    for png in (FRAMES_DIR.glob("*.png") if FRAMES_DIR.exists() else []):
        try:
            blob = png.read_bytes()
        except Exception:
            continue
        for secret in list(redactor._secrets):  # noqa: SLF001 — same module
            if secret.encode("utf-8") in blob:
                leaked.add(f"{redactor._secrets[secret]} in {png.name}")  # noqa: SLF001
    if leaked:
        print(f"::error:: C-SEC LEAK — live secret(s) survived: {sorted(leaked)}")
        rc = 1
    else:
        log("[C-SEC] grep-assert clean: no stream key / operator JWT / show-token "
            "/ obs-ws password in stdout or any captured PNG.")

    log("PASS" if rc == 0 else (f"SKIPPED (exit {rc})" if rc == 3 else f"FAILED (exit {rc})"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
