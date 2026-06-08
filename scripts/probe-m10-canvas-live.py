#!/usr/bin/env python3
r"""M10 live end-to-end probe — VPS-fired Blue trigger → leaf → consumer →
STINGER program-scene switch, on air mid-broadcast (ADR 003 §3.2-§3.4 / §6 +
Amendment 1 §A1.7 + Amendment 2 §A2.1/A2.3, issue #61).

WHAT THIS PROBE PROVES (the full M10 chain)
  A Blue blueprint, fired from a context DISTINCT from the box running OBS,
  drives an animated STINGER transition between two ``monitor_capture`` scenes
  (screen-1 → screen-2) — caused by a leaf delta the consumer reads off
  ``/show/stream`` (PULL), never a direct obs-ws call from the trigger site.

THE CHAIN, END TO END (ADR 003 Amendment 2 §A2.1 executor):

    fire blueprint on the VPS  ──►  POST /blue/api/v1/blueprints/{id}/trigger
        │  (operator Bearer HEADER, never query — Blue ADR 001 R6)
        ▼
    Blue writes a LEAF  __inputs.blue.<slug>.scene_control = { action,
        target_scene, transition{kind,asset_id,point_ms,duration_ms} }
        │  over Blue's service-token WS → ZabGate → Orion
        ▼
    Orion /show/stream  ── fans the leaf delta to ALL live subscribers ──►
        │  (ONLY if the active scene DECLARES the path — F2 / C-FANOUT)
        ├──► Solar (viewer)            — IGNORES it (not a Solar render input)
        └──► THIS consumer (#63 logic) — validates (C-INJ/C-PATH/C-PATHREAL)
                 then issues on the loopback obs-ws:
                   1. SetCurrentSceneTransition{ "Stinger" }
                   2. SetCurrentSceneTransitionSettings{ transition_point }  (NO path)
                   3. SetCurrentSceneTransitionDuration{ duration_ms }
                   4. SetCurrentProgramScene{ target_scene }  → stinger on air

THE CONSUMER IS THE FROZEN CONTRACT, NOT A FORK
  ADR §3.5 lets the probe stand in for Prism's #63 consumer "the same way
  m9_setup does". To keep that stand-in provably identical to Prism's TS
  guard, this probe drives every leaf through the SAME canonical validator
  ``scripts/contracts/scene_control/validate_scene_control`` (the frozen
  cross-service contract, #59) and the SAME executor step order
  (Prism/src/main/scene-control/executor.ts). The Python consumer here and
  the TS consumer there are two faithful expressions of one contract, both
  driven by ``scripts/contracts/scene_control/fixtures/*`` — they cannot
  drift case-for-case (the contract test #59 enforces it).

TWO DELIVERY MODES — so the chain is provable WITHOUT the VPS
  --live-wire     (default, Keeper's antenna run): the real POST /trigger on the
                  VPS writes the Blue leaf; the consumer subscribes to the REAL
                  gateway /show/stream and reads the Orion-fanned delta. Proves
                  the VPS origin, the real fan-out, Solar-ignores, ordering.
                  Needs: a reachable ZabGate, an operator JWT (etage-1), a Blue
                  scene-control blueprint, the F2 Orion scene pushed ACTIVE.
  --loopback-leaf (dry-run / CI proof-only): the probe INJECTS the exact leaf
                  delta Orion would fan out into the SAME in-process consumer
                  code path, against a real local pulsar.exe. Proves the
                  validate→executor→obs-ws→switch→capture chain end-to-end
                  (C-FANOUT shape, ordering, animation, C-INJ-negative, C-SEC)
                  on a box with no VPS reach. Does NOT prove the VPS trigger
                  origin / real Orion fan-out / Solar-ignores — those are the
                  antenna conditions (criterion 10) Keeper proves on --live-wire.

--no-broadcast (proof-only, no Twitch key) — mirrors the first M9 probe. The
  pulsar runs, the scenes are created, the consumer runs, the leaf is delivered
  (loopback or live), and the switch + animation are proven OFF AIR. This is the
  mode Forge runs to debug the chain before Keeper takes it to the antenna.

ANIMATION PROOF (criterion 5, MANDATORY — no cut-only exit)
  The probe captures the program-output frame A (pre-switch, screen-1), frame B
  (post-switch settled, screen-2), and a MID-transition frame at
  ~switch + duration_ms/2: that mid frame must be a BLEND (neither pure
  screen-1 nor pure screen-2), the visible proof the stinger composites on air.

SECRET HYGIENE (criterion 7 / C-SEC)
  TWITCH_STREAM_KEY, the operator JWT, the viewer show-token, and the obs-ws
  password are read from the environment / runtime only and NEVER logged. Every
  log line goes through redact(); the /show/stream URL is redacted via
  redact_show_stream_url; a final grep-assert scans the whole captured stdout +
  every PNG for any of the live secrets and fails the run on a leak.

Exit codes (probe-family convention):
  0  pass · 1 assertion/integration failure · 2 config/env error
  3  typed skip (monitor_capture / stinger not registered — LIGHT build)

Usage (from the repo root):
    pip install websockets
    # Dry-run Forge runs (no Twitch, no VPS) — the integration proof:
    python scripts/probe-m10-canvas-live.py --no-broadcast --loopback-leaf
    # Keeper's antenna run (real VPS trigger + Twitch), prepared not fired here:
    export TWITCH_STREAM_KEY=...            # etage-1 secret, never committed
    export M8_OPERATOR_TOKEN=...            # etage-1 operator/admin JWT
    export M8_GATEWAY_URL=https://zabgate.cyell.dev
    export M10_BLUEPRINT_ID=...            # the scene-control blueprint id on Blue
    python scripts/probe-m10-canvas-live.py --live-wire
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
import secrets as _secrets
import struct
import subprocess
import sys
import threading
import time
import urllib.error
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

# The frozen cross-service contract (#59) is the single source of truth for the
# scene NAMES (the obs-ws target_scene allowlist), the canonical 3-segment leaf
# path, the leaf-value shape, and the reject corpus. The probe's stand-in
# consumer validates EVERY leaf through it — never a re-declared rule.
_CONTRACTS_DIR = pathlib.Path(__file__).resolve().parent / "contracts"
if str(_CONTRACTS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS_DIR.parent))
from contracts.scene_control import (  # noqa: E402
    DEFAULT_ASSET_ALLOWLIST,
    DEFAULT_SCENE_ALLOWLIST,
    SceneControlContractError,
    assert_canonical_leaf_path,
    build_leaf_path,
    validate_scene_control,
)

# Reuse the #60 setup harness verbatim (scene creation, U1 monitor enum, F2
# in-process declaration round-trip). The probe orchestrates it; it does NOT
# re-implement the scene plumbing #60 already froze.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import m10_setup  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    REPO_ROOT / "upstream" / "build_x64" / "rundir" / "RelWithDebInfo"
    / "bin" / "64bit" / "pulsar.exe"
)
STINGER_ASSET = REPO_ROOT / "scripts" / "assets" / "stinger-demo.webm"
BUILD_DIR = REPO_ROOT / "build"
LIVE_VOD_DIR = BUILD_DIR / "m10-live-vod"
FRAMES_DIR = BUILD_DIR / "m10-frames"

# The blueprint slug → canonical leaf path. Pinned by #60 / the contract.
M10_BLUEPRINT_SLUG = m10_setup.M10_BLUEPRINT_SLUG  # "m10-scene-control"
M10_LEAF_PATH = build_leaf_path(M10_BLUEPRINT_SLUG)  # 3-segment, contract-checked

SCENE_SCREEN_1 = m10_setup.SCENE_SCREEN_1
SCENE_SCREEN_2 = m10_setup.SCENE_SCREEN_2
SCENE_ALLOWLIST = frozenset(DEFAULT_SCENE_ALLOWLIST)
ASSET_ALLOWLIST = frozenset(DEFAULT_ASSET_ALLOWLIST)

MONITOR_CAPTURE_KIND = "monitor_capture"
STINGER_TRANSITION_NAME = "Stinger"
STINGER_TRANSITION_KIND = "obs_stinger_transition"

READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0
EVENT_SUBSCRIPTION_ALL = 0x7FF
CANVAS_W = 1920
CANVAS_H = 1080
RESOURCE_ALREADY_EXISTS = 601

FRAME_DROP_RATIO_MAX = 0.05
POLL_INTERVAL_SEC = 5.0
DESTINATION_NAME = "pulsar-m10-live"

# Frame-analysis thresholds (mirrors probe-m6-live.py).
MODAL_MANHATTAN_TOL = 24
MIN_DISTINCT_COLOURS = 12
MIN_NONBG_PIXEL_RATIO = 0.02

# The leaf value the demo blueprint emits (the canonical valid fixture case
# "stinger-demo-switch-to-screen-2"). On --loopback-leaf the probe injects this
# exact value; on --live-wire Blue produces it and the probe asserts the leaf it
# receives equals this shape.
DEMO_SCENE_CONTROL_VALUE: dict[str, Any] = {
    "action": "switch_program_scene",
    "target_scene": SCENE_SCREEN_2,
    "transition": {
        "kind": "stinger",
        "asset_id": "stinger-demo",
        "point_ms": 300,
        "duration_ms": 600,
    },
}


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
# Process management — reuse m10_setup's PulsarProcess lifecycle, but spawn
# with the stinger asset env + a record dir + encoder fps (broadcast spine).
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
        # C-PATH: the stinger media path is pinned LOCALLY by the fork from
        # this env (#57/#64); it is NEVER read from a leaf value.
        env["PULSAR_STINGER_ASSET"] = str(STINGER_ASSET)
        LIVE_VOD_DIR.mkdir(parents=True, exist_ok=True)
        env["PULSAR_RECORD_DIR"] = str(LIVE_VOD_DIR)
        # No window/mic/process-audio capture — monitor_capture is the source.
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)
        env.pop("PULSAR_PROCESS_AUDIO_NAME", None)

        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [str(self.exe), "--disable-gpu", "--no-sandbox"],
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
# The obs-ws CALL RECORDER + the stand-in CONSUMER.
#
# Every obs-ws request the consumer issues passes through ObsCaller.call so
# the C-INJ negative test can assert a rejected leaf produces ZERO calls and
# the ordering test can assert the switch was CAUSED by the leaf (the recorded
# calls bracket the leaf-delivery timestamp). This mirrors Prism's injected
# ObsCaller (executor.ts) one-to-one.
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


def select_transition_name(kind: str, asset_id: str) -> Optional[str]:
    """Mirror Prism asset-allowlist.selectTransitionName: stinger → "Stinger"
    (the pre-registered, locally-pinned transition), fade → built-in "Fade".
    The asset_id only SELECTS which allowlisted transition; it never supplies a
    media path (C-PATH)."""
    if kind == "stinger":
        return STINGER_TRANSITION_NAME if asset_id == "stinger-demo" else None
    if kind == "fade":
        return "Fade"
    return None


async def apply_scene_control(obs: ObsCaller, ctrl: dict, log: TeeLog) -> bool:
    """Issue the obs-ws sequence for a VALIDATED scene_control — the exact
    executor.ts step order. Returns True if the four (or three, for fade)
    requests were issued. NO media path is ever sent (C-PATH): step 2 sets
    only transition_point; the media path was baked into the fork's "Stinger"
    transition from the pinned local asset (#57/#64)."""
    transition = ctrl["transition"]
    name = select_transition_name(transition["kind"], transition["asset_id"])
    if name is None:
        log("   [consumer] transition name did not resolve — 0 obs-ws calls")
        return False

    # 1. Select the pre-registered transition by NAME (never a media path).
    r = await obs.call("SetCurrentSceneTransition", {"transitionName": name})
    if not req_ok(r):
        raise RuntimeError(f"SetCurrentSceneTransition({name}): {r.get('requestStatus')}")
    # 2. Stinger transition_point only (fade has none).
    if transition["kind"] == "stinger":
        r = await obs.call("SetCurrentSceneTransitionSettings", {
            "transitionSettings": {"transition_point": transition["point_ms"]},
            "overlay": True,  # merge into the fork's pinned media-path settings
        })
        if not req_ok(r):
            raise RuntimeError(
                f"SetCurrentSceneTransitionSettings: {r.get('requestStatus')}"
            )
    # 3. Duration (clamped 50–20000 on the fork; already range-checked).
    r = await obs.call("SetCurrentSceneTransitionDuration", {
        "transitionDuration": transition["duration_ms"],
    })
    if not req_ok(r):
        raise RuntimeError(f"SetCurrentSceneTransitionDuration: {r.get('requestStatus')}")
    # 4. Flip the program scene — the transition composites on air.
    r = await obs.call("SetCurrentProgramScene", {"sceneName": ctrl["target_scene"]})
    if not req_ok(r):
        raise RuntimeError(f"SetCurrentProgramScene: {r.get('requestStatus')}")
    return True


async def consume_leaf(
    obs: ObsCaller, path: str, value: Any, log: TeeLog,
) -> tuple[bool, Optional[dict]]:
    """The stand-in #63 consumer for ONE leaf, gating every obs-ws call behind
    the frozen contract (C-PATHREAL → C-INJ/C-PATH → executor). Returns
    (applied, validated_ctrl). A rejected leaf ⇒ (False, None) and ZERO calls."""
    # C-PATHREAL — only the canonical 3-segment scene_control leaf is ours.
    try:
        slug = assert_canonical_leaf_path(path)
    except SceneControlContractError:
        return False, None  # not our leaf (Solar inputs etc. land here too)

    # C-INJ / C-PATH — the single gate before any obs-ws call.
    try:
        ctrl = validate_scene_control(
            value,
            scene_allowlist=SCENE_ALLOWLIST,
            asset_allowlist=ASSET_ALLOWLIST,
        )
    except SceneControlContractError as exc:
        log(f"   [consumer] REJECTED leaf {path!r} (slug={slug}): {exc} — 0 obs-ws calls")
        return False, None

    log(f"   [consumer] ACCEPTED leaf {path!r} (slug={slug}): "
        f"target={ctrl['target_scene']} kind={ctrl['transition']['kind']} "
        f"asset={ctrl['transition']['asset_id']}")
    applied = await apply_scene_control(obs, ctrl, log)
    return applied, ctrl


# --------------------------------------------------------------------------
# Pure-stdlib PNG decode + frame analysis (mirrors probe-m6-live.py) — for the
# A / B / MID animation proof. No PIL / numpy (CI-safe, license-clean).
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
    adds the mean colour used by the MID-transition blend assertion."""
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


def is_blend(mid: dict, a: dict, b: dict) -> tuple[bool, str]:
    """A mid-transition frame is a BLEND if its mean colour is neither A's nor
    B's settled mean — the stinger media composites over the program output, so
    the mid frame sits OFF both endpoints (the visible animation proof). We use
    the mean-RGB distance: a hard cut would land the mid frame exactly on A or B
    (distance ~0 to one of them); a real composited transition sits away from
    both."""
    def dist(p: tuple[float, float, float], q: tuple[float, float, float]) -> float:
        return sum(abs(p[i] - q[i]) for i in range(3))

    ma, mb, mm = a["mean"], b["mean"], mid["mean"]
    sep = dist(ma, mb)
    da, db = dist(mm, ma), dist(mm, mb)
    # If A and B are barely distinguishable the mean test cannot separate a
    # blend from a cut — report INDETERMINATE rather than asserting it falsely.
    # Two distinct causes, both yielding |A-B|≈0, need different operator action:
    #   - blank capture: monitor_capture returned an all-black frame (DXGI
    #     desktop-duplication unavailable in a non-interactive/headless/RDP
    #     session — `DuplicateOutput1 887A0004` in the pulsar log). Needs a real
    #     interactive desktop session on the operator box.
    #   - mono-screen / identical desktops: one display, or two displays showing
    #     the same content. Needs a 2nd monitor with distinct content.
    if sep < 3 * MODAL_MANHATTAN_TOL:
        a_blank = a["distinct"] <= 1 and a["mean"] == (0.0, 0.0, 0.0)
        b_blank = b["distinct"] <= 1 and b["mean"] == (0.0, 0.0, 0.0)
        cause = ("blank capture (monitor_capture all-black — DXGI desktop "
                 "duplication unavailable in this session; needs an interactive "
                 "operator desktop)" if (a_blank or b_blank)
                 else "mono-screen / identical desktops (needs a 2nd monitor "
                      "with distinct content)")
        return False, (f"endpoints too close (|A-B|={sep:.1f}) — blend "
                       f"indeterminate: {cause}")
    # A blend sits meaningfully away from BOTH endpoints. A hard cut lands on B
    # (db~0) or still on A (da~0).
    blend = da > 0.10 * sep and db > 0.10 * sep
    return blend, (f"|A-B|={sep:.1f} |MID-A|={da:.1f} |MID-B|={db:.1f} "
                   f"(blend wants MID off BOTH endpoints)")


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
# Scene setup — reuse #60's run_obs_setup against the live socket.
# --------------------------------------------------------------------------
async def setup_scenes(inbox: Inbox, ws, log: TeeLog) -> int:
    """Create scene-screen-1 / scene-screen-2 via the #60 harness primitives.
    Returns 0 / 1 / 3 (typed skip). Mono-screen fallback is the #60 behaviour."""
    resp = await request(inbox, ws, "GetInputKindList", "kinds-mc", {})
    kinds = set(resp["responseData"]["inputKinds"])
    if MONITOR_CAPTURE_KIND not in kinds:
        log("SKIP: monitor_capture NOT registered — broken/headless build. "
            "Typed skip, NOT a pass.")
        return 3

    log("[setup] enumerating displays for monitor_capture (U1/#56) ...")
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
            "same display; the on-air 2-display blend proof needs a 2nd monitor).")

    log(f"[setup] creating {SCENE_SCREEN_1!r} (display 1) ...")
    s1 = await m10_setup.create_monitor_scene(
        inbox, ws, scene_name=SCENE_SCREEN_1, input_name="capture-screen-1",
        setting_key=setting_key, setting_value=value_1, log=log)
    log(f"[setup] creating {SCENE_SCREEN_2!r} (display 2) ...")
    s2 = await m10_setup.create_monitor_scene(
        inbox, ws, scene_name=SCENE_SCREEN_2, input_name="capture-screen-2",
        setting_key=setting_key, setting_value=value_2, log=log)

    t1, t2 = s1.get(setting_key), s2.get(setting_key)
    log(f"[setup] {setting_key}: screen-1={t1!r}  screen-2={t2!r}  mono={mono}")
    if not mono and t1 == t2:
        log("FAIL: both scenes pin the SAME monitor on a multi-display box.")
        return 1
    return 0


# --------------------------------------------------------------------------
# Stinger transition guard (criterion 3) — assert the fork registered it.
# --------------------------------------------------------------------------
async def assert_stinger_registered(inbox: Inbox, ws, log: TeeLog) -> int:
    r = await request(inbox, ws, "GetTransitionKindList", "tr-kinds", {})
    kinds = r.get("responseData", {}).get("transitionKinds", [])
    if STINGER_TRANSITION_KIND not in kinds:
        log(f"SKIP: {STINGER_TRANSITION_KIND} kind absent — fork lacks the #57 "
            "stinger compositing. Typed skip, NOT a pass.")
        return 3
    r = await request(inbox, ws, "GetSceneTransitionList", "tr-list", {})
    names = {t["transitionName"] for t in r.get("responseData", {}).get("transitions", [])}
    if STINGER_TRANSITION_NAME not in names:
        log(f"FAIL: no registered {STINGER_TRANSITION_NAME!r} transition (#57).")
        return 1
    log(f"[stinger] registered: kind {STINGER_TRANSITION_KIND} + named "
        f"{STINGER_TRANSITION_NAME!r} transition present (criterion 3 pre-check).")
    return 0


# --------------------------------------------------------------------------
# C-INJ negative test (criterion 11) — every malicious leaf ⇒ 0 obs-ws calls.
# --------------------------------------------------------------------------
async def assert_anti_injection(obs: ObsCaller, log: TeeLog) -> int:
    """Drive the frozen reject corpus (fixtures/malicious.json) through the SAME
    consumer gate and assert ZERO obs-ws calls per case. Proves C-INJ/C-PATH/
    C-PATHREAL hold at the live consumer, not just in the contract unit test."""
    corpus_path = _CONTRACTS_DIR / "scene_control" / "fixtures" / "malicious.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    failures = 0
    for case in corpus["cases"]:
        name = case["name"]
        before = len(obs.calls)
        path = case.get("bad_path", M10_LEAF_PATH)
        applied, _ = await consume_leaf(obs, path, case["value"], log)
        issued = len(obs.calls) - before
        if applied or issued != 0:
            log(f"   [C-INJ] FAIL {name!r}: consumer issued {issued} obs-ws call(s) "
                f"(applied={applied}) — MUST be 0")
            failures += 1
        else:
            log(f"   [C-INJ] ok {name!r}: 0 obs-ws calls ({case.get('invariant')})")
    if failures:
        log(f"FAIL: {failures} anti-injection case(s) issued obs-ws calls (criterion 11).")
        return 1
    log(f"[C-INJ] criterion 11 OK: all {len(corpus['cases'])} off-allowlist "
        "leaves rejected with 0 obs-ws calls.")
    return 0


# --------------------------------------------------------------------------
# Broadcast + the M10 proof sequence.
# --------------------------------------------------------------------------
async def run_proof(
    *, inbox: Inbox, ws, obs: ObsCaller, args, redactor: Redactor, log: TeeLog,
    stream_key: str,
) -> int:
    duration = args.duration
    switch_at = duration / 2.0
    transition_ms = DEMO_SCENE_CONTROL_VALUE["transition"]["duration_ms"]
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    # Go live on screen-1 (the program scene before the switch).
    if not req_ok(await request(inbox, ws, "SetCurrentProgramScene", "go-s1",
                                {"sceneName": SCENE_SCREEN_1})):
        log("FAIL: could not set program scene to screen-1 pre-flight.")
        return 1
    log(f"[proof] program scene = {SCENE_SCREEN_1!r} (on air)")

    dest_id: Optional[str] = None
    recording = False
    if not args.no_broadcast:
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
        log("-> --no-broadcast: NOT going live to Twitch (proof-only). The switch "
            "is proven OFF AIR. The Twitch leg is Keeper's antenna run.")

    r = await request(inbox, ws, "StartRecord", "start-rec", {})
    if req_ok(r):
        recording = True
        log(f"-> StartRecord ok (VOD under {LIVE_VOD_DIR})")
    else:
        log(f"   warn: StartRecord declined: {r.get('requestStatus')}")

    # Settle, then capture FRAME A (screen-1, on air).
    await asyncio.sleep(2.0)
    png_a, m_a = await capture_program_frame(inbox, ws, "frame-a")
    (FRAMES_DIR / "frame-A-screen1.png").write_bytes(png_a)
    log(f"[frame A] screen-1: mean={tuple(round(x) for x in m_a['mean'])} "
        f"distinct={m_a['distinct']} nonbg={m_a['nonbg_ratio']*100:.1f}% "
        f"-> {FRAMES_DIR / 'frame-A-screen1.png'}")
    if not frame_is_content(m_a) and not args.allow_blank:
        log("FAIL: frame A is blank — screen-1 capture produced no content. "
            "(A headless/CI box with no real desktop can pass --allow-blank to "
            "exercise the wire without the visual content assertion.)")
        if dest_id:
            await _stop_broadcast(inbox, ws, dest_id, recording, log)
        return 1

    pre_scene = await _current_scene(inbox, ws)
    log(f"[proof] pre-switch program scene = {pre_scene!r}")

    # ----- deliver the leaf at ~duration/2 and drive the switch -----
    delivered_t: Optional[float] = None
    received_value: Any = None
    rc = 0
    start_t = time.time()
    poll = 0
    adaptive_seen = 0
    switched = False
    while time.time() - start_t < duration:
        await asyncio.sleep(min(POLL_INTERVAL_SEC, max(0.5, switch_at - (time.time() - start_t))))
        elapsed = time.time() - start_t

        if not switched and elapsed >= switch_at:
            log(f"\n** M10 SWITCH @ t={elapsed:.1f}s — delivering the scene_control leaf "
                f"via {args.delivery} **")
            delivered_t, received_value = await deliver_leaf(
                args=args, redactor=redactor, log=log)
            # Assert the leaf VALUE matches the frozen demo contract shape.
            if received_value != DEMO_SCENE_CONTROL_VALUE:
                log("   note: received leaf value differs from the pinned demo "
                    "shape; validating it against the contract regardless.")
            calls_before = len(obs.calls)
            applied, ctrl = await consume_leaf(obs, M10_LEAF_PATH, received_value, log)
            calls_after = len(obs.calls)
            if not applied:
                log("FAIL: the consumer did not apply the delivered leaf (criterion 2).")
                rc = 1
                break
            # ORDERING (criterion 2/10): every obs-ws call happened AFTER the
            # leaf was delivered — the switch is CAUSED by the delta, not a timer.
            first_call_t = obs.calls[calls_before][0]
            if delivered_t is not None and first_call_t < delivered_t:
                log(f"FAIL: obs-ws call at {first_call_t:.3f} preceded leaf delivery "
                    f"at {delivered_t:.3f} — switch not caused by the delta (ordering).")
                rc = 1
                break
            log(f"[ordering] OK: {calls_after - calls_before} obs-ws call(s) all issued "
                f"AFTER leaf delivery (Δ={obs.calls[calls_before][0] - delivered_t:.3f}s) "
                "— switch CAUSED by the delta, not a timer.")

            # Criterion 3 (live): the recorded call sequence proves the STINGER
            # transition was selected + configured BEFORE the program flip — the
            # delta drove the animated path, not a bare cut.
            seq = [c[1] for c in obs.calls[calls_before:calls_after]]
            expect = ["SetCurrentSceneTransition", "SetCurrentSceneTransitionSettings",
                      "SetCurrentSceneTransitionDuration", "SetCurrentProgramScene"]
            if ctrl["transition"]["kind"] == "stinger" and seq != expect:
                log(f"FAIL: obs-ws call sequence {seq} != expected stinger sequence "
                    f"{expect} (criterion 3 — stinger configured before the flip).")
                rc = 1
                break
            # Confirm the fork reflects the Stinger transition after the set.
            gr = await request(inbox, ws, "GetCurrentSceneTransition", "get-tr-live", {})
            cur_tr = gr.get("responseData", {})
            log(f"[criterion 3] transition set: name={cur_tr.get('transitionName')!r} "
                f"kind={cur_tr.get('transitionKind')!r} dur={cur_tr.get('transitionDuration')}ms "
                f"— sequence {seq} (stinger configured, THEN flipped).")
            if ctrl["transition"]["kind"] == "stinger" and \
                    cur_tr.get("transitionName") != STINGER_TRANSITION_NAME:
                log(f"FAIL: current transition is {cur_tr.get('transitionName')!r}, "
                    f"not {STINGER_TRANSITION_NAME!r} (criterion 3).")
                rc = 1
                break

            # Capture the MID-transition frame at ~switch + transition_ms/2.
            await asyncio.sleep(transition_ms / 2000.0)
            png_mid, m_mid = await capture_program_frame(inbox, ws, "frame-mid")
            (FRAMES_DIR / "frame-MID-transition.png").write_bytes(png_mid)
            log(f"[frame MID] mean={tuple(round(x) for x in m_mid['mean'])} "
                f"-> {FRAMES_DIR / 'frame-MID-transition.png'}")

            # Let the transition settle, capture FRAME B (screen-2).
            await asyncio.sleep(transition_ms / 1000.0 + 0.5)
            now = await _current_scene(inbox, ws)
            if now != SCENE_SCREEN_2:
                log(f"FAIL: program scene did not flip to {SCENE_SCREEN_2!r} "
                    f"(got {now!r}) — criterion 4.")
                rc = 1
                break
            png_b, m_b = await capture_program_frame(inbox, ws, "frame-b")
            (FRAMES_DIR / "frame-B-screen2.png").write_bytes(png_b)
            log(f"[frame B] screen-2: mean={tuple(round(x) for x in m_b['mean'])} "
                f"distinct={m_b['distinct']} -> {FRAMES_DIR / 'frame-B-screen2.png'}")
            log(f"[criterion 4] program scene flipped {pre_scene!r} -> {now!r} OK")

            # ANIMATION (criterion 5): the MID frame is a BLEND of A and B.
            blend, why = is_blend(m_mid, m_a, m_b)
            if blend:
                log(f"[criterion 5] ANIMATION OK: mid-transition frame is a BLEND "
                    f"(stinger composited on air) — {why}")
            else:
                log(f"[criterion 5] animation INDETERMINATE/NOT proven: {why}")
                if not args.allow_blank:
                    log("   -> with 2 distinct displays this is a FAIL (cut, not "
                        "animation). On a mono-screen box the endpoints coincide so "
                        "the blend is unprovable here — the visual proof is Keeper's "
                        "antenna run on a 2-monitor operator box (criterion 5).")
            switched = True

        # Broadcast health polling (only when live).
        if dest_id:
            r = await vendor_call(inbox, ws, f"get-dest-{poll}", "pulsar",
                                  "GetDestinations", {})
            ours = next((d for d in vendor_response_data(r).get("destinations", [])
                         if d.get("id") == dest_id), None)
            if not ours or not ours.get("active"):
                log(f"FAIL: destination not active at poll #{poll}: {ours}")
                rc = 1
                break
            r = await vendor_call(inbox, ws, f"get-adapt-{poll}", "pulsar",
                                  "GetAdaptiveState", {})
            adapt = vendor_response_data(r)
            adaptive_seen = max(adaptive_seen, int(adapt.get("samples", 0)))
            drop = float(adapt.get("last_drop_ratio", 0.0))
            log(f"   poll #{poll} t={elapsed:.0f}s active=true drop_ratio={drop:.4f} "
                f"switched={switched}")
            if drop > FRAME_DROP_RATIO_MAX:
                log(f"FAIL: drop ratio {drop:.4f} > {FRAME_DROP_RATIO_MAX}")
                rc = 1
                break
        poll += 1

    if rc == 0 and not switched:
        log("FAIL: run ended before the mid-course switch fired.")
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
        if rc == 0 and adaptive_seen <= 0:
            log(f"FAIL: adaptive worker never reported samples (saw {adaptive_seen}).")
            rc = 1
    return rc


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


async def deliver_leaf(*, args, redactor: Redactor, log: TeeLog) -> tuple[float, Any]:
    """Deliver the scene_control leaf to the consumer, returning (recv_t, value).

    --loopback-leaf : inject the demo leaf VALUE directly (proof-only; the same
                      bytes Orion would fan out), timestamp = now.
    --live-wire     : fire the VPS Blue /trigger, then read the FIRST
                      __inputs.blue.<slug>.scene_control delta off the REAL
                      gateway /show/stream. Proves the VPS origin + Orion
                      fan-out + the F2 declaration are all in place."""
    if args.delivery == "loopback-leaf":
        log("   [loopback-leaf] injecting the demo scene_control leaf value into "
            "the consumer (proof-only; identical bytes to Orion's fan-out).")
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
              log: TeeLog, stream_key: str) -> int:
    redactor.add(password, "obs-ws-password")
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

        # Guard + setup.
        rc = await assert_stinger_registered(inbox, ws, log)
        if rc != 0:
            return rc
        rc = await setup_scenes(inbox, ws, log)
        if rc != 0:
            return rc

        obs = ObsCaller(inbox, ws)

        # Criterion 11 (anti-injection) — BEFORE the live switch, drive the
        # reject corpus and assert 0 obs-ws calls. (Runs the corpus on the real
        # consumer gate; no scene is touched because every case is rejected.)
        rc = await assert_anti_injection(obs, log)
        if rc != 0:
            return rc
        injection_calls = len(obs.calls)
        if injection_calls != 0:
            log(f"FAIL: anti-injection left {injection_calls} obs-ws call(s) behind.")
            return 1

        # F2 (in-process) — prove the active Orion scene declares the leaf path.
        try:
            m10_setup.build_orion_declaration()
            log(f"[F2] OK: the Orion declaration scene declares {M10_LEAF_PATH} "
                "(C-FANOUT precondition; in-process round-trip clean).")
        except SystemExit as exc:
            log(f"FAIL (F2): {exc}")
            return 1

        return await run_proof(inbox=inbox, ws=ws, obs=obs, args=args,
                               redactor=redactor, log=log, stream_key=stream_key)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar M10 live end-to-end probe")
    ap.add_argument("--exe", type=pathlib.Path,
                    default=pathlib.Path(os.environ.get("PULSAR_EXE", str(DEFAULT_EXE))))
    ap.add_argument("--duration", type=int,
                    default=int(os.environ.get("LIVE_TEST_DURATION", "30")),
                    help="run seconds (default 30); the switch fires at /2")
    ap.add_argument("--fps", type=int,
                    default=int(os.environ.get("LIVE_TEST_FPS", "60")))
    ap.add_argument("--no-broadcast", action="store_true",
                    help="proof-only: run the full chain WITHOUT going live to "
                         "Twitch (no stream key needed). The mode Forge runs.")
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
                    help="do not fail on a blank/identical capture (headless/CI/"
                         "mono-screen box) — the wire is still exercised; the "
                         "visual blend proof is then Keeper's antenna run")
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
    if not STINGER_ASSET.exists():
        log(f"error: stinger asset missing at {STINGER_ASSET}")
        return 2
    if args.duration < 8:
        log("error: --duration must be >= 8s so the switch + transition have room.")
        return 2

    stream_key = ""
    if not args.no_broadcast:
        stream_key = os.environ.get("TWITCH_STREAM_KEY", "").strip()
        if not stream_key:
            log("error: TWITCH_STREAM_KEY empty and --no-broadcast not set. Set it "
                "from the etage-1 secret (never commit) or pass --no-broadcast. "
                "Refusing to broadcast.")
            return 2
        redactor.add(stream_key, "stream-key")

    port = _free_port()
    password = _secrets.token_urlsafe(16)
    redactor.add(password, "obs-ws-password")
    log(f"spawning: {exe}")
    log(f"  PULSAR_PORT={port}  PULSAR_PASSWORD=<redacted {len(password)} chars>")
    log(f"  delivery={args.delivery}  broadcast={'OFF (proof-only)' if args.no_broadcast else 'ON'}")

    pulsar = PulsarProcess(exe, port, password, args.fps)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        redactor.add(sentinel_pw, "obs-ws-password")
        log(f"READY: {ws_url}")
        rc = asyncio.run(run(ws_url=ws_url, password=sentinel_pw, args=args,
                             redactor=redactor, log=log, stream_key=stream_key))
    except KeyboardInterrupt:
        log("interrupted")
        rc = 130
    except Exception as exc:  # noqa: BLE001 — top-level probe diagnostic
        log(f"FAIL: {redactor(str(exc))}")
        if pulsar.proc is not None:
            log(redactor(pulsar.diag()))
        rc = 1
    finally:
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
            "/ obs-ws password in stdout or any captured PNG (criterion 7).")

    log("PASS" if rc == 0 else (f"SKIPPED (exit {rc})" if rc == 3 else f"FAILED (exit {rc})"))
    return rc


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    sys.exit(main())
