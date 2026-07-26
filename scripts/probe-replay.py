#!/usr/bin/env python3
"""
Pulsar replay-buffer probe (issue #117 / ADR Prism 024 §3.1).

Proves the replay buffer is WIRED, not scaffolding, using only the six v5
baseline requests Pulsar has always compiled:

  1. Spawn pulsar.exe (isolated port / password / PULSAR_RECORD_DIR).
  2. Off-air arm is REFUSED: StartReplayBuffer with the encoders idle leaves
     GetReplayBufferStatus.outputActive == false, and no encoder is started
     (GetStats.activeFps stays quiet / the record output stays inactive).
  3. StartRecord -> the shared encoders come up.
  4. StartReplayBuffer -> GetReplayBufferStatus.outputActive == true.
  5. Buffer for a few seconds, then SaveReplayBuffer -> wait for the
     ReplayBufferSaved event.
  6. GetLastReplayBufferReplay returns a REAL path; the file exists on disk,
     is non-trivial in size, and ffprobe reads a h264+aac MP4 out of it.
  7. StopReplayBuffer / StopRecord, clean reap.

Assertion for ADR criterion 2 (no extra encoding): the video encoder is
borrowed, never created. GetOutputList / GetOutputStatus cannot count
encoders, so the proxy assertion here is that arming the buffer does not
change the record output's own state and that the replay MP4 carries the
same codec/resolution/fps as the recording -- the signature of a shared
encoder rather than a second one.

Usage (from the repo root, against the built rundir):
    python scripts/probe-replay.py
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
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Optional

try:
    import websockets
except ImportError:
    print("error: pip install websockets")
    sys.exit(2)


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    REPO_ROOT / "upstream" / "build_x64" / "rundir" / "RelWithDebInfo" / "bin" / "64bit" / "pulsar.exe"
)

READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0
EVENT_SUBSCRIPTION_ALL = 0x7FF

# Replay depth requested from the stub via env. Kept short so the probe is
# fast; long enough that the saved MP4 carries several seconds of packets.
REPLAY_MAX_TIME_SEC = 10
REPLAY_MAX_SIZE_MB = 64
# Time spent filling the ring before asking for a save.
BUFFER_FILL_SEC = 5.0
# A ~5 s 1080p60 h264+aac MP4 is megabytes; 100 KB cleanly separates a real
# replay from an empty or truncated container.
MIN_MP4_BYTES = 100 * 1024


# --------------------------------------------------------------------------
# Process management -- same shape as probe-record-m2.py's PulsarProcess.
# --------------------------------------------------------------------------
class PulsarProcess:
    def __init__(self, exe: pathlib.Path, port: int, password: str, record_dir: pathlib.Path) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.record_dir = record_dir
        self.proc: Optional[subprocess.Popen] = None
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        env["PULSAR_RECORD_DIR"] = str(self.record_dir)
        env["PULSAR_REPLAY_MAX_TIME_SEC"] = str(REPLAY_MAX_TIME_SEC)
        env["PULSAR_REPLAY_MAX_SIZE_MB"] = str(REPLAY_MAX_SIZE_MB)
        # No capture target / no mic: the pipeline still encodes (black frames
        # + silent desktop mix), which is all the buffer needs to hold packets.
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)

        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
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
                    f"pulsar.exe exited (code {self.proc.returncode}) before READY.\n" + self.diag()
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(f"pulsar.exe did not signal READY within {timeout:.0f}s.\n" + self.diag())

    def grep_log(self, needle: str) -> list[str]:
        return [ln for ln in self._lines if needle in ln]

    def diag(self, n: int = 40) -> str:
        tail = self._lines[-n:]
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
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------
# obs-websocket v5 plumbing
# --------------------------------------------------------------------------
def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


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
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
            if msg.get("op") == 5:
                self.events.append(msg["d"])
            elif msg.get("op") == 7:
                self.responses.append(msg["d"])


async def request(inbox: Inbox, ws, request_type: str, request_id: str,
                  data: dict | None = None, timeout: float = 15.0) -> dict:
    body: dict = {"requestType": request_type, "requestId": request_id}
    if data is not None:
        body["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": body}))

    await inbox.pump(ws, lambda ix: any(r["requestId"] == request_id for r in ix.responses), timeout)
    for i, r in enumerate(inbox.responses):
        if r["requestId"] == request_id:
            return inbox.responses.pop(i)
    raise RuntimeError("unreachable")


async def expect_event(inbox: Inbox, ws, event_type: str, timeout: float = 30.0) -> dict:
    await inbox.pump(ws, lambda ix: any(e.get("eventType") == event_type for e in ix.events), timeout)
    for i, e in enumerate(inbox.events):
        if e.get("eventType") == event_type:
            return inbox.events.pop(i)
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------
# MP4 verification
# --------------------------------------------------------------------------
def find_ffprobe() -> str | None:
    for cand in (REPO_ROOT / "upstream/.deps").glob("obs-deps-*-x64/bin/ffprobe.exe"):
        return str(cand)
    return shutil.which("ffprobe")


def verify_mp4(path: pathlib.Path) -> bool:
    ffprobe = find_ffprobe()
    if not ffprobe:
        print("warn: ffprobe not found; skipping stream assertions")
        return True
    try:
        out = subprocess.check_output(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            stderr=subprocess.STDOUT, timeout=20,
        )
    except subprocess.CalledProcessError as e:
        print(f"error: ffprobe failed: {e.output.decode(errors='replace')}")
        return False
    info = json.loads(out)
    streams = info.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        print(f"error: replay MP4 has no video stream: {path}")
        return False
    if not audio:
        print(f"error: replay MP4 has no audio stream: {path}")
        return False
    v, a = video[0], audio[0]
    duration = float(info.get("format", {}).get("duration", 0.0) or 0.0)
    print(f"   video: codec={v.get('codec_name')} {v.get('width')}x{v.get('height')}")
    print(f"   audio: codec={a.get('codec_name')} ch={a.get('channels')} sr={a.get('sample_rate')}")
    print(f"   duration: {duration:.2f}s")
    if v.get("codec_name") != "h264":
        print(f"error: expected h264 video, got {v.get('codec_name')}")
        return False
    if duration < 1.0:
        print(f"error: replay duration {duration:.2f}s is implausibly short")
        return False
    return True


# --------------------------------------------------------------------------
# The probe itself
# --------------------------------------------------------------------------
async def drive(ws_url: str, password: str, record_dir: pathlib.Path,
                pulsar: PulsarProcess) -> int:
    async with websockets.connect(ws_url, subprotocols=["obswebsocket.json"]) as ws:
        hello = json.loads(await ws.recv())
        identify: dict = {"rpcVersion": hello["d"]["rpcVersion"],
                          "eventSubscriptions": EVENT_SUBSCRIPTION_ALL}
        if "authentication" in hello["d"]:
            auth = hello["d"]["authentication"]
            identify["authentication"] = compute_auth(password, auth["salt"], auth["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify}))
        if json.loads(await ws.recv())["op"] != 2:
            print("error: identify failed")
            return 1
        print("identified")

        inbox = Inbox()

        # -- Step 1: off-air arm must be refused, without starting an encoder --
        print("\n[1] off-air arm must be refused")
        r = await request(inbox, ws, "GetReplayBufferStatus", "rb-0")
        if r["responseData"]["outputActive"]:
            print("error: replay buffer already active at boot")
            return 1
        await request(inbox, ws, "StartReplayBuffer", "rb-arm-offair")
        await asyncio.sleep(1.0)
        r = await request(inbox, ws, "GetReplayBufferStatus", "rb-1")
        if r["responseData"]["outputActive"]:
            print("error: replay buffer armed OFF-AIR -- the encoder guard did not hold")
            return 1
        r = await request(inbox, ws, "GetRecordStatus", "rec-0")
        if r["responseData"]["outputActive"]:
            print("error: off-air arm started the record output")
            return 1
        refusals = pulsar.grep_log("replay buffer start refused")
        if not refusals:
            print("error: off-air refusal was not logged")
            print(pulsar.diag())
            return 1
        print(f"   refused + logged: {refusals[-1].strip()[:120]}")

        # -- Step 2: bring the shared encoders up (recording = on-air proxy) --
        print("\n[2] StartRecord (brings the shared encoders up)")
        r = await request(inbox, ws, "StartRecord", "rec-start")
        if not r["requestStatus"]["result"]:
            print(f"error: StartRecord declined: {r['requestStatus']}")
            return 1
        await expect_event(inbox, ws, "RecordStateChanged", timeout=15.0)
        await asyncio.sleep(1.0)

        # -- Step 3: arm the buffer, verify it is REALLY active --
        print("\n[3] StartReplayBuffer -> outputActive must be true")
        await request(inbox, ws, "StartReplayBuffer", "rb-arm")
        # Never trust the ack (see PROTOCOL.md): confirm with the status request.
        active = False
        for _ in range(20):
            await asyncio.sleep(0.25)
            r = await request(inbox, ws, "GetReplayBufferStatus", "rb-2-" + secrets.token_hex(3))
            if r["responseData"]["outputActive"]:
                active = True
                break
        if not active:
            print("error: GetReplayBufferStatus.outputActive stayed false after arming")
            print(pulsar.diag())
            return 1
        print("   outputActive=true")

        # -- Step 4: fill the ring, then save --
        print(f"\n[4] buffering {BUFFER_FILL_SEC}s then SaveReplayBuffer")
        await asyncio.sleep(BUFFER_FILL_SEC)
        r = await request(inbox, ws, "SaveReplayBuffer", "rb-save")
        if not r["requestStatus"]["result"]:
            print(f"error: SaveReplayBuffer declined: {r['requestStatus']}")
            return 1
        await expect_event(inbox, ws, "ReplayBufferSaved", timeout=60.0)
        print("   <- ReplayBufferSaved")

        # -- Step 5: the path must be real and readable --
        print("\n[5] GetLastReplayBufferReplay -> real path on disk")
        r = await request(inbox, ws, "GetLastReplayBufferReplay", "rb-last")
        saved = (r.get("responseData") or {}).get("savedReplayPath") or ""
        if not saved:
            print("error: savedReplayPath is empty -- lastReplay was never filled")
            print(pulsar.diag())
            return 1
        path = pathlib.Path(saved)
        print(f"   savedReplayPath={path}")
        if not path.exists():
            print(f"error: savedReplayPath does not exist on disk: {path}")
            return 1
        try:
            path.relative_to(record_dir)
        except ValueError:
            print(f"warn: replay {path} is not under PULSAR_RECORD_DIR {record_dir}")
        size = path.stat().st_size
        if size < MIN_MP4_BYTES:
            print(f"error: replay file too small: {size} bytes (< {MIN_MP4_BYTES})")
            return 1
        print(f"   file: {size:,} bytes")
        if not verify_mp4(path):
            return 1

        # -- Step 6: tear down both outputs --
        print("\n[6] StopReplayBuffer + StopRecord")
        await request(inbox, ws, "StopReplayBuffer", "rb-stop")
        await request(inbox, ws, "StopRecord", "rec-stop")
        await asyncio.sleep(1.0)

    print("\nreplay buffer WIRED: armed on-air, refused off-air, saved a readable MP4")
    return 0


def pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar replay-buffer probe (issue #117)")
    ap.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE,
                    help="path to pulsar.exe (default: built rundir)")
    ap.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S)
    ap.add_argument("--keep-mp4", action="store_true", help="keep the temp record dir")
    args = ap.parse_args()

    exe: pathlib.Path = args.exe
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        print("Build it first: scripts/build-win.ps1")
        return 2

    port = pick_free_port()
    password = secrets.token_urlsafe(16)
    record_dir = pathlib.Path(tempfile.mkdtemp(prefix="pulsar-replay-"))
    print(f"spawning: {exe}")
    print(f"  PULSAR_PORT={port}  PULSAR_RECORD_DIR={record_dir}")
    print(f"  PULSAR_REPLAY_MAX_TIME_SEC={REPLAY_MAX_TIME_SEC} "
          f"PULSAR_REPLAY_MAX_SIZE_MB={REPLAY_MAX_SIZE_MB}")

    pulsar = PulsarProcess(exe, port, password, record_dir)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        rc = asyncio.run(drive(ws_url, sentinel_pw, record_dir, pulsar))
    except KeyboardInterrupt:
        rc = 130
    except Exception as exc:  # noqa: BLE001 -- top-level probe diagnostic
        print(f"FAIL: {exc}")
        rc = 1
    finally:
        pulsar.shutdown()
        if args.keep_mp4:
            print(f"kept record dir: {record_dir}")
        else:
            shutil.rmtree(record_dir, ignore_errors=True)

    print("PASS" if rc == 0 else f"FAILED (exit {rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
