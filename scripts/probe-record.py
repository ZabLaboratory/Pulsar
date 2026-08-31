#!/usr/bin/env python3
"""
Pulsar record probe (Phase 6 validation).

Round-trip:
  1. Connect + identify with eventSubscriptions covering Outputs.
  2. StartRecord -> expect RecordStateChanged{outputState: RECORDING_STARTING/RECORDING_STARTED}.
  3. Sleep ~3 s while frames flow into ffmpeg_muxer.
  4. StopRecord  -> expect RecordStateChanged{outputState: RECORDING_STOPPING/RECORDING_STOPPED}.
  5. Read outputPath from the final stop event (or fall back to GetLastRecording),
     stat the MP4 and confirm size > 100 KB.

The capture target is whatever PULSAR_CAPTURE_WINDOW pointed at when pulsar.exe
launched. With no target the source produces black frames; the test still
proves the encoder + muxer pipeline closed a valid MP4. Set the env var
before launching pulsar.exe to capture a real window:

    set PULSAR_CAPTURE_WINDOW=Calculator:Windows.UI.Core.CoreWindow:CalculatorApp.exe
    pulsar.exe

Usage (from the repo root with pulsar.exe already running):
    python scripts/probe-record.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
from typing import Callable

try:
    import websockets
except ImportError:
    print("error: pip install websockets")
    sys.exit(2)


def find_ffprobe() -> str | None:
    """Prefer the ffprobe shipped with obs-deps so we use the same build the
    runtime muxer was linked against. Fall back to PATH."""
    repo = pathlib.Path(__file__).resolve().parent.parent
    for cand in (repo / "upstream/.deps").glob("obs-deps-*-x64/bin/ffprobe.exe"):
        return str(cand)
    return shutil.which("ffprobe")


def parse_rate(rate_str: str) -> float:
    """ffprobe r_frame_rate is "num/den" (e.g. "60/1"). Returns float."""
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


def assert_mp4_streams(path: pathlib.Path, expected_fps: int = 60,
                       min_video_kbps: int = 4500) -> bool:
    """Run ffprobe and confirm:
      - one AAC audio stream (Phase 9)
      - one h264 video stream at expected_fps (Phase 12a)
      - average video bit_rate >= min_video_kbps (Phase 12a; below this the
        encoder probably ignored our bitrate setting)
    Phase 12a default target is 6000 kbps so the floor is set generously
    at 4500 to absorb x264 RC variance over a 3 s sample."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        print("warn: ffprobe not found; skipping stream assertions")
        return True
    try:
        out = subprocess.check_output(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        print(f"error: ffprobe failed: {e.output.decode(errors='replace')}")
        return False
    info = json.loads(out)
    streams = info.get("streams", [])

    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        print(f"error: MP4 has no audio stream: {path}")
        return False
    a = audio[0]
    print(f"   audio stream: codec={a.get('codec_name')} "
          f"channels={a.get('channels')} sample_rate={a.get('sample_rate')}")

    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        print(f"error: MP4 has no video stream: {path}")
        return False
    v = video[0]
    fps = parse_rate(v.get("r_frame_rate") or v.get("avg_frame_rate") or "")
    print(f"   video stream: codec={v.get('codec_name')} "
          f"size={v.get('width')}x{v.get('height')} fps={fps:.0f}")
    if abs(fps - expected_fps) > 0.5:
        print(f"error: expected {expected_fps} fps, got {fps:.2f}")
        return False

    # The video stream's own bit_rate is rarely populated by ffmpeg_muxer;
    # fall back to the format-level bit_rate (audio + video). The 4500 kbps
    # floor easily separates a 6000 kbps target from a stuck-on-defaults
    # output even after subtracting ~160 kbps of audio.
    fmt_kbps = int(info.get("format", {}).get("bit_rate", 0)) // 1000
    print(f"   container bitrate: {fmt_kbps} kbps")
    if fmt_kbps < min_video_kbps:
        print(f"error: container bitrate {fmt_kbps} kbps below floor {min_video_kbps} kbps")
        return False

    return True


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = (
    REPO_ROOT
    / "upstream"
    / "build_x64"
    / "rundir"
    / "RelWithDebInfo"
    / "bin"
    / "64bit"
    / "obs-websocket"
    / "config.json"
)
EVENT_SUBSCRIPTION_ALL = 0x7FF
RECORD_DURATION_SEC = 3.0
MIN_MP4_BYTES = 100 * 1024  # 100 KB sanity threshold
STOP_PENDING_CODE = 702
STOP_EVENT_TIMEOUT_SEC = 15.0


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


class Inbox:
    def __init__(self):
        self.events: list[dict] = []
        self.responses: list[dict] = []

    async def pump(self, ws, until: Callable[["Inbox"], bool], timeout: float):
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


async def expect_event(inbox: Inbox, ws, event_type: str, timeout: float = 10.0,
                       predicate: Callable[[dict], bool] | None = None) -> dict:
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


async def wait_record_stop(inbox: Inbox, ws, response: dict) -> dict | None:
    """Consume a StopRecord result without losing a late stop.

    StopRecord returns a completed path only when its bounded server-side
    settlement lands.  A typed 702 means the stop was accepted but the muxer
    is still flushing; the probe must then wait for the authoritative STOPPED
    event and re-read outputActive before the shared process is reused.  No
    path or success is inferred from the 702 response itself.
    """
    status = response.get("requestStatus") or {}
    if not status.get("result") and int(status.get("code") or 0) != STOP_PENDING_CODE:
        print(f"error: StopRecord declined before acceptance: {status}")
        return None
    if not status.get("result"):
        print("   StopRecord pending (702); waiting for RecordStateChanged STOPPED")

    try:
        event = await expect_event(
            inbox,
            ws,
            "RecordStateChanged",
            timeout=STOP_EVENT_TIMEOUT_SEC,
            predicate=lambda d: d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED",
        )
    except asyncio.TimeoutError:
        print(f"error: StopRecord did not emit STOPPED within {STOP_EVENT_TIMEOUT_SEC:.0f}s")
        return None

    deadline = asyncio.get_event_loop().time() + STOP_EVENT_TIMEOUT_SEC
    n = 0
    while True:
        n += 1
        status_response = await request(inbox, ws, "GetRecordStatus", f"stop-status-{n}")
        if not (status_response.get("responseData") or {}).get("outputActive"):
            return event
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            print("error: STOPPED event arrived but GetRecordStatus.outputActive stayed true")
            return None
        await asyncio.sleep(min(0.25, remaining))


async def request(inbox: Inbox, ws, request_type: str, request_id: str,
                  data: dict | None = None, timeout: float = 10.0) -> dict:
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


async def probe(url: str, password: str) -> int:
    print(f"connecting: {url}")
    async with websockets.connect(url, subprotocols=["obswebsocket.json"]) as ws:
        hello = json.loads(await ws.recv())
        rpc = hello["d"]["rpcVersion"]
        identify_d: dict = {
            "rpcVersion": rpc,
            "eventSubscriptions": EVENT_SUBSCRIPTION_ALL,
        }
        if "authentication" in hello["d"]:
            auth = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(password, auth["salt"], auth["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        ident = json.loads(await ws.recv())
        if ident["op"] != 2:
            print(f"error: identify failed: {ident}")
            return 1
        print("identified")

        inbox = Inbox()

        # Sanity: GetRecordStatus baseline.
        resp = await request(inbox, ws, "GetRecordStatus", "rec-status-0")
        if resp["responseData"]["outputActive"]:
            print("error: recording already active before probe; stop it first")
            return 1

        print("-> StartRecord")
        resp = await request(inbox, ws, "StartRecord", "start-1")
        if not resp["requestStatus"]["result"]:
            print(f"error: StartRecord declined: {resp['requestStatus']}")
            return 1

        evt = await expect_event(
            inbox, ws, "RecordStateChanged",
            predicate=lambda d: d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED",
            timeout=5.0,
        )
        print(f"   <- RecordStateChanged outputState={evt['eventData']['outputState']}")

        print(f"   recording for {RECORD_DURATION_SEC}s ...")
        await asyncio.sleep(RECORD_DURATION_SEC)

        print("-> StopRecord")
        resp = await request(inbox, ws, "StopRecord", "stop-1")
        evt = await wait_record_stop(inbox, ws, resp)
        if evt is None:
            return 1
        output_path = evt["eventData"].get("outputPath") or ""
        print(f"   <- RecordStateChanged outputState=STOPPED outputPath={output_path}")

        if not output_path:
            # Fallback: ask the server for the last-recorded file.
            resp = await request(inbox, ws, "GetLastRecording", "last-1")
            if resp["requestStatus"]["result"]:
                output_path = resp["responseData"].get("recordingPath") or ""

        if not output_path:
            print("error: no outputPath available from event or GetLastRecording")
            return 1

        path = pathlib.Path(output_path)
        if not path.exists():
            print(f"error: output file does not exist on disk: {path}")
            return 1
        size = path.stat().st_size
        if size < MIN_MP4_BYTES:
            print(f"error: output file too small: {size} bytes (< {MIN_MP4_BYTES})")
            return 1
        print(f"   MP4 written: {path}  ({size:,} bytes)")

        if not assert_mp4_streams(path):
            return 1

    print("\nphase 6+9+12a record pipeline validated (video 60fps + bitrate + audio)")
    return 0


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"error: pulsar-websocket config not found at {CONFIG_PATH}")
        return 2
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    password = config.get("server_password", "")
    port = config.get("server_port", 4455)
    url = f"ws://127.0.0.1:{port}"
    return asyncio.run(probe(url, password))


if __name__ == "__main__":
    sys.exit(main())
