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
import sys
from typing import Callable

try:
    import websockets
except ImportError:
    print("error: pip install websockets")
    sys.exit(2)


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
        if not resp["requestStatus"]["result"]:
            print(f"error: StopRecord declined: {resp['requestStatus']}")
            return 1

        evt = await expect_event(
            inbox, ws, "RecordStateChanged",
            predicate=lambda d: d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED",
            timeout=10.0,
        )
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

    print("\nphase 6 record pipeline validated end-to-end")
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
