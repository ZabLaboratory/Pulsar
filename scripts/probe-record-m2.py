#!/usr/bin/env python3
"""
Pulsar media-output probe — M2 (ADR 008 §8).

Proves the freshly-built pulsar.exe can be *driven over WebSocket to produce
a real media file*: it boots headless, is told (over obs-websocket v5) to
build a scene with a synthetic source, then to record that scene to a local
MP4. The probe stops the recording, reads the output path off the
RecordStateChanged event, and validates the MP4 with ffprobe — h264 video +
aac audio + a real (> 0) duration. A 0-byte / undecodable file fails.

This is the M2 deliverable: the smallest provable "scene + source -> record
-> verified MP4" round-trip, self-contained (spawns + reaps its own
pulsar.exe like the M1 smoke probe). It does NOT depend on a running
instance and does NOT touch the rtmp destination lifecycle (the vod_local /
rtmp path in probe-multi-stream.py is excluded from the offline suite due to
known upstream-obs race crashes). The v5 StartRecord path used here is the
CI-stable record route — same encoder + ffmpeg_muxer pipeline, no rtmp
worker-thread races.

Why a synthetic source and not the boot capture target:
  probe-record.py (the connect-only suite member) records whatever
  PULSAR_CAPTURE_WINDOW pointed at when pulsar.exe launched — with no
  target that is a black canvas. M2's brief asks us to *drive* the scene
  graph: CreateScene + CreateInput(color_source_v3) + CreateSceneItem,
  set it program, then record. The synthetic color source needs no
  external window, no GPU capture target, and no network — fully
  deterministic on any runner.

LICENSE INVARIANT (LICENSE-INVARIANTS.md #1/#2/#3, ADR 008 §3.1): the probe
talks to Pulsar over the WebSocket process boundary ONLY. It spawns
pulsar.exe as a separate OS process and exchanges nothing but obs-websocket
v5 frames. No FFI, no ctypes/cffi, no LoadLibrary of obs.dll / pulsar-*.dll /
libcef.dll, no native import. ffprobe is invoked as a separate process on the
produced file — it never links Pulsar. Pure aggregation — Pulsar's GPL never
crosses into the consumer.

Steps (M2 brief, ADR 008 §8):
  1. Spawn pulsar.exe (cwd=bin/64bit, fresh PULSAR_PORT/PULSAR_PASSWORD,
     PULSAR_RECORD_DIR=<temp>); wait for the PULSAR_READY sentinel.
  2. v5 handshake (Hello -> Identify -> Identified) with eventSubscriptions
     covering Outputs (RecordStateChanged).
  3. CreateScene -> CreateInput(color_source_v3, opaque magenta, 1920x1080)
     -> CreateSceneItem (enabled) -> SetCurrentProgramScene.
  4. StartRecord -> assert RecordStateChanged{OUTPUT_STARTED} -> record
     RECORD_DURATION_SEC -> StopRecord -> assert
     RecordStateChanged{OUTPUT_STOPPED} carrying the outputPath.
  5. Tear the scene graph back down (RemoveInput + RemoveScene).
  6. ffprobe the MP4: exactly one h264 video stream, one aac audio stream,
     container/stream duration > 0. File must be > MIN_MP4_BYTES on disk.
  7. Delete the test MP4 (leaves the temp record dir clean).
  8. Clean shutdown: WS close -> terminate child -> kill fallback. No orphan.
  9. Exit 0 on success, non-zero + diagnostic on any failure.

If ffprobe is not on PATH and not shipped under upstream/.deps, the probe
falls back to a structural MP4 check (ftyp + moov box present, size
plausible) and prints that full codec verification runs in CI (where
FedericoCarboni/setup-ffmpeg provides ffprobe).

Usage (from the repo root, against the built rundir):
    pip install websockets
    python scripts/probe-record-m2.py
    python scripts/probe-record-m2.py --exe /path/to/pulsar.exe   # override
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
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
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
# (PROTOCOL.md, pulsar-headless/main.cpp:342).
READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0

# Subscribe to the Outputs event group so RecordStateChanged reaches us.
# 0x7FF is the obs-websocket "all non-high-volume" mask used by the other
# probes; it covers Outputs.
EVENT_SUBSCRIPTION_ALL = 0x7FF

RECORD_DURATION_SEC = 3.0
# A 3 s 1080p60 h264+aac MP4 is ~2 MB; 100 KB cleanly separates a real
# capture from an empty/truncated container.
MIN_MP4_BYTES = 100 * 1024
STOP_PENDING_CODE = 702
STOP_EVENT_TIMEOUT_SEC = 15.0

SCENE_NAME = "probe-m2-scene"
INPUT_NAME = "probe-m2-color"
# color_source_v3 is the synthetic solid-colour source (REQUIRED_KINDS_LIGHT
# in probe-source-kinds.py). No external dependency, no capture target.
INPUT_KIND = "color_source_v3"
# obs colour is 0xAABBGGRR. Opaque magenta = full alpha + full R + full B.
COLOR_MAGENTA_ABGR = 0xFFFF00FF
CANVAS_W = 1920
CANVAS_H = 1080


# --------------------------------------------------------------------------
# Process management — mirrors probe-websocket.py (M1) PulsarProcess.
# --------------------------------------------------------------------------
class PulsarProcess:
    """Spawns pulsar.exe and pumps its stdout on a background thread so the
    READY sentinel is parsed without blocking. Captures the full boot log
    for diagnostics on failure."""

    def __init__(
        self, exe: pathlib.Path, port: int, password: str, record_dir: pathlib.Path
    ) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.record_dir = record_dir
        self.proc: Optional[subprocess.Popen] = None
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None
        self._pump_thread: Optional[threading.Thread] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        # Record into an isolated temp dir so we never collide with the
        # shared rundir recordings/ and can clean up deterministically.
        env["PULSAR_RECORD_DIR"] = str(self.record_dir)
        # No mic / no capture target — the synthetic colour source is the
        # only visible thing; audio comes from the silent desktop mix.
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)

        creationflags = 0
        if os.name == "nt":
            # CREATE_NO_WINDOW — keep the console-subsystem child headless
            # (PRISM-EMBEDDING.md:107).
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
# obs-websocket v5 request/event plumbing — mirrors probe-record.py Inbox.
# --------------------------------------------------------------------------
def compute_auth(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 challenge/response (PROTOCOL.md:169):
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
    timeout: float = 10.0,
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


async def expect_event(
    inbox: Inbox,
    ws,
    event_type: str,
    predicate: Callable[[dict], bool] | None = None,
    timeout: float = 10.0,
) -> dict:
    def has_event(ix: Inbox) -> bool:
        for e in ix.events:
            if e.get("eventType") != event_type:
                continue
            if predicate is None or predicate(e.get("eventData") or {}):
                return True
        return False

    await inbox.pump(ws, has_event, timeout)
    for i, e in enumerate(inbox.events):
        if e.get("eventType") != event_type:
            continue
        if predicate is None or predicate(e.get("eventData") or {}):
            return inbox.events.pop(i)
    raise RuntimeError("unreachable")


async def wait_record_stop(
    inbox: Inbox, ws, response: dict, record_dir: pathlib.Path
) -> dict | None:
    """Drain a StopRecord response without treating pending as success.

    A 702 means the request was accepted and the muxer is still flushing. The
    authoritative completion is RecordStateChanged=STOPPED followed by an
    inactive GetRecordStatus observation. The output path must come from that
    event, be present, and belong to this run's isolated record directory;
    missing, stale, or unrelated paths are rejected.
    """
    status = response.get("requestStatus") or {}
    if status.get("result") is not True and int(status.get("code") or 0) != STOP_PENDING_CODE:
        print(f"error: StopRecord failed before acceptance: {status}")
        return None
    if status.get("result") is not True:
        print("   StopRecord pending (702); waiting for RecordStateChanged STOPPED")

    try:
        event = await expect_event(
            inbox,
            ws,
            "RecordStateChanged",
            predicate=lambda d: d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED",
            timeout=STOP_EVENT_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        print(f"error: StopRecord did not emit STOPPED within {STOP_EVENT_TIMEOUT_SEC:.0f}s")
        return None
    except Exception as exc:
        print(f"error: StopRecord STOPPED wait failed: {exc}")
        return None

    event_data = event.get("eventData") or {}
    raw_path = event_data.get("outputPath")
    if not isinstance(raw_path, str) or not raw_path:
        print("error: STOPPED event did not carry an outputPath")
        return None
    try:
        output_path = pathlib.Path(raw_path).resolve()
        output_path.relative_to(record_dir.resolve())
    except (OSError, ValueError):
        print(f"error: STOPPED event outputPath is stale or outside this run: {raw_path}")
        return None

    deadline = asyncio.get_event_loop().time() + STOP_EVENT_TIMEOUT_SEC
    attempt = 0
    while True:
        attempt += 1
        try:
            status_response = await request(
                inbox, ws, "GetRecordStatus", f"stop-status-{attempt}"
            )
        except Exception as exc:
            print(f"error: could not re-read GetRecordStatus after STOPPED: {exc}")
            return None
        response_data = status_response.get("responseData")
        if not isinstance(response_data, dict):
            print("error: GetRecordStatus responseData is malformed after STOPPED")
            return None
        output_active = response_data.get("outputActive")
        if output_active is False:
            if not output_path.is_file():
                print(f"error: STOPPED event outputPath does not exist: {output_path}")
                return None
            return event
        if output_active is not True:
            print("error: GetRecordStatus.outputActive is malformed after STOPPED")
            return None
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            print("error: STOPPED event arrived but GetRecordStatus.outputActive stayed true")
            return None
        await asyncio.sleep(min(0.25, remaining))


# --------------------------------------------------------------------------
# MP4 verification.
# --------------------------------------------------------------------------
def find_ffprobe() -> str | None:
    """Prefer the ffprobe shipped with obs-deps (same build the muxer was
    linked against). Fall back to PATH."""
    for cand in (REPO_ROOT / "upstream/.deps").glob("obs-deps-*-x64/bin/ffprobe.exe"):
        return str(cand)
    return shutil.which("ffprobe")


def parse_rate(rate_str: str) -> float:
    if not rate_str:
        return 0.0
    if "/" in rate_str:
        num, den = rate_str.split("/", 1)
        try:
            n = float(num)
            d = float(den)
            return n / d if d else 0.0
        except ValueError:
            return 0.0
    try:
        return float(rate_str)
    except ValueError:
        return 0.0


def verify_mp4_structural(path: pathlib.Path) -> bool:
    """ffprobe-free fallback: confirm the MP4 has an `ftyp` box at the head
    and a `moov` box somewhere — i.e. a finalised, demuxable container, not a
    truncated/0-byte stub. Box layout: [4-byte big-endian size][4-byte type].
    """
    data = path.read_bytes()
    if len(data) < 16:
        print(f"error: file too short to be an MP4: {len(data)} bytes")
        return False
    # First box must be ftyp.
    first_type = data[4:8]
    if first_type != b"ftyp":
        print(f"error: first box is {first_type!r}, expected b'ftyp'")
        return False
    # Walk top-level boxes looking for moov (the index — proves finalisation).
    found_moov = False
    found_mdat = False
    off = 0
    n = len(data)
    while off + 8 <= n:
        size = struct.unpack(">I", data[off : off + 4])[0]
        btype = data[off + 4 : off + 8]
        if btype == b"moov":
            found_moov = True
        elif btype == b"mdat":
            found_mdat = True
        if size == 0:  # box extends to EOF
            break
        if size == 1:  # 64-bit largesize follows the type
            if off + 16 > n:
                break
            size = struct.unpack(">Q", data[off + 8 : off + 16])[0]
        if size < 8:
            break
        off += size
    print(f"   structural: ftyp=yes moov={found_moov} mdat={found_mdat}")
    if not found_moov:
        print("error: no moov box — recording was not finalised")
        return False
    if not found_mdat:
        print("error: no mdat box — container carries no media payload")
        return False
    return True


def verify_mp4(path: pathlib.Path) -> bool:
    """Full codec + duration verification via ffprobe; structural fallback if
    ffprobe is unavailable locally (CI always has it)."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        print("warn: ffprobe not found on PATH or under upstream/.deps —")
        print("      falling back to structural MP4 check. Full codec + duration")
        print("      verification runs in CI (FedericoCarboni/setup-ffmpeg).")
        return verify_mp4_structural(path)

    try:
        out = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            stderr=subprocess.STDOUT,
            timeout=15,
        )
    except subprocess.CalledProcessError as e:
        print(f"error: ffprobe failed: {e.output.decode(errors='replace')}")
        return False
    info = json.loads(out)
    streams = info.get("streams", [])

    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        print(f"error: MP4 has no video stream: {path}")
        return False
    v = video[0]
    vcodec = v.get("codec_name")
    fps = parse_rate(v.get("r_frame_rate") or v.get("avg_frame_rate") or "")
    print(
        f"   video: codec={vcodec} size={v.get('width')}x{v.get('height')} "
        f"fps={fps:.0f}"
    )
    if vcodec != "h264":
        print(f"error: expected h264 video, got {vcodec!r}")
        return False

    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        print(f"error: MP4 has no audio stream: {path}")
        return False
    a = audio[0]
    acodec = a.get("codec_name")
    print(
        f"   audio: codec={acodec} channels={a.get('channels')} "
        f"sample_rate={a.get('sample_rate')}"
    )
    if acodec != "aac":
        print(f"error: expected aac audio, got {acodec!r}")
        return False

    # Duration > 0 is the core "real media" assertion. Prefer the
    # container-level duration; fall back to the video stream's own.
    fmt_dur = float(info.get("format", {}).get("duration") or 0.0)
    vid_dur = float(v.get("duration") or 0.0)
    duration = fmt_dur or vid_dur
    print(f"   duration: {duration:.3f}s (format={fmt_dur:.3f} video={vid_dur:.3f})")
    if duration <= 0.0:
        print("error: MP4 duration is 0 — no media was written")
        return False

    return True


# --------------------------------------------------------------------------
# The M2 round-trip.
# --------------------------------------------------------------------------
async def drive_record(url: str, password: str, record_dir: pathlib.Path) -> int:
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

        # --- Guard: not already recording ---
        resp = await request(inbox, ws, "GetRecordStatus", "rec-status-0")
        if resp["responseData"]["outputActive"]:
            print("error: recording already active before probe")
            return 1

        # --- Build the scene graph: scene + synthetic colour source ---
        print(f"-> CreateScene {SCENE_NAME!r}")
        r = await request(inbox, ws, "CreateScene", "cs", {"sceneName": SCENE_NAME})
        if not r["requestStatus"]["result"]:
            print(f"error: CreateScene declined: {r['requestStatus']}")
            return 1

        print(f"-> CreateInput {INPUT_NAME!r} kind={INPUT_KIND}")
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
                    "color": COLOR_MAGENTA_ABGR,
                    "width": CANVAS_W,
                    "height": CANVAS_H,
                },
                "sceneItemEnabled": True,
            },
        )
        if not r["requestStatus"]["result"]:
            print(f"error: CreateInput declined: {r['requestStatus']}")
            return 1
        print(f"   <- sceneItemId={r['responseData'].get('sceneItemId')}")

        print(f"-> SetCurrentProgramScene {SCENE_NAME!r}")
        r = await request(
            inbox, ws, "SetCurrentProgramScene", "sps", {"sceneName": SCENE_NAME}
        )
        if not r["requestStatus"]["result"]:
            print(f"error: SetCurrentProgramScene declined: {r['requestStatus']}")
            return 1

        # --- Record ---
        print("-> StartRecord")
        r = await request(inbox, ws, "StartRecord", "start-1")
        if not r["requestStatus"]["result"]:
            # The classic "obs_output_start declined silently" trap surfaces
            # here as result=false (DEVELOPMENT.md §Troubleshooting).
            print(f"error: StartRecord declined: {r['requestStatus']}")
            return 1

        evt = await expect_event(
            inbox,
            ws,
            "RecordStateChanged",
            predicate=lambda d: d.get("outputState")
            == "OBS_WEBSOCKET_OUTPUT_STARTED",
            timeout=8.0,
        )
        print(f"   <- RecordStateChanged {evt['eventData']['outputState']}")

        print(f"   recording {SCENE_NAME!r} for {RECORD_DURATION_SEC}s ...")
        await asyncio.sleep(RECORD_DURATION_SEC)

        print("-> StopRecord")
        r = await request(inbox, ws, "StopRecord", "stop-1")
        evt = await wait_record_stop(inbox, ws, r, record_dir)
        if evt is None:
            return 1

        output_path = evt["eventData"].get("outputPath") or ""
        print(f"   <- RecordStateChanged STOPPED outputPath={output_path}")

        # --- Tear the scene graph back down (leave the engine as we found it) ---
        await request(inbox, ws, "RemoveInput", "ri", {"inputName": INPUT_NAME})
        await request(inbox, ws, "RemoveScene", "rs", {"sceneName": SCENE_NAME})

        await ws.close(code=1000, reason="m2 complete")

    # --- Resolve + verify the produced MP4 ---
    path = pathlib.Path(output_path)
    if not path.is_file():
        print(f"error: output file does not exist on disk: {path}")
        return 1

    size = path.stat().st_size
    if size < MIN_MP4_BYTES:
        print(f"error: output file too small: {size} bytes (< {MIN_MP4_BYTES})")
        return 1
    print(f"   MP4 written: {path} ({size:,} bytes)")

    if not verify_mp4(path):
        return 1

    print("\nM2 OK — scene+source driven over WS produced a verified MP4")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar M2 media-output probe")
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
        "--keep-mp4",
        action="store_true",
        help="do not delete the produced MP4 (debugging)",
    )
    args = ap.parse_args()

    exe: pathlib.Path = args.exe
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        print("Build it first: scripts/build-win.ps1 -Full")
        return 2

    port = pick_free_port()
    password = secrets.token_urlsafe(16)
    record_dir = pathlib.Path(tempfile.mkdtemp(prefix="pulsar-m2-rec-"))
    print(f"spawning: {exe}")
    print(f"  cwd={exe.parent}")
    print(f"  PULSAR_PORT={port}  PULSAR_PASSWORD=<redacted {len(password)} chars>")
    print(f"  PULSAR_RECORD_DIR={record_dir}")

    pulsar = PulsarProcess(exe, port, password, record_dir)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        rc = asyncio.run(drive_record(ws_url, sentinel_pw, record_dir))
    except KeyboardInterrupt:
        print("interrupted")
        rc = 130
    except Exception as exc:  # noqa: BLE001 — top-level probe diagnostic
        print(f"FAIL: {exc}")
        rc = 1
    finally:
        pulsar.shutdown()
        if pulsar.proc is not None and pulsar.proc.poll() is None:
            print("error: pulsar.exe still running after shutdown attempt")
            rc = rc or 1
        else:
            print("pulsar.exe reaped cleanly")
        # Clean up the temp record dir (and the MP4) unless asked to keep it.
        if not args.keep_mp4:
            shutil.rmtree(record_dir, ignore_errors=True)
        else:
            print(f"kept record dir: {record_dir}")

    print("PASS" if rc == 0 else f"FAILED (exit {rc})")
    return rc


def pick_free_port() -> int:
    """Bind :0 on loopback to let the OS hand us a free ephemeral port, then
    release it (tiny TOCTOU window, acceptable for a single-run local probe)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
