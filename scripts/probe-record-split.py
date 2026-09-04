#!/usr/bin/env python3
"""
Pulsar record split / chapter probe (issue #169, ADR Prism 028 §3.5).

`obs_frontend_recording_split_file` and `obs_frontend_recording_add_chapter`
were stubbed to an unconditional `false`: SplitRecordFile / CreateRecordChapter
were registered requests that could only fail. This probe holds the two
resolution criteria of #169 that a return code alone cannot prove.

  1. SplitRecordFile on a live recording succeeds AND a second file really
     lands on disk -- asserted from the RecordFileChanged event *and* from the
     directory, not from the request result.
  2. Every refusal is NAMED. CreateRecordChapter is expected to fail on this
     build (chapter markers exist only on the hybrid-MP4 output; Pulsar records
     through ffmpeg_muxer) and its comment must say so, naming the output and
     the missing procedure -- never a mute `false` nor a generic sentence.

Usage (from the repo root with pulsar.exe already running):
    python scripts/probe-record-split.py
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
# Two keyframe intervals (PULSAR_VIDEO_KEYINT_SEC defaults to 2 s): the muxer
# only switches file on the next keyframe after the split is armed.
PRE_SPLIT_SEC = 3.0
POST_SPLIT_SEC = 4.0
FIRST_BYTE_TIMEOUT_SEC = 15.0
MIN_PART_BYTES = 4 * 1024
STOP_PENDING_CODE = 702
STOP_EVENT_TIMEOUT_SEC = 15.0

# The generic upstream comments this probe must NOT see any more.
UPSTREAM_GENERIC = (
    "Verify that file splitting is enabled in the output settings.",
    "Verify that the output being used supports chapter markers.",
)


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


async def wait_record_stop(inbox: Inbox, ws, response: dict) -> bool:
    """Drain a bounded StopRecord Pending result before this probe continues."""
    status = response.get("requestStatus") or {}
    if not status.get("result") and int(status.get("code") or 0) != STOP_PENDING_CODE:
        print(f"error: StopRecord declined before acceptance: {status}")
        return False
    if not status.get("result"):
        print("   StopRecord pending (702); waiting for RecordStateChanged STOPPED")

    try:
        await expect_event(
            inbox,
            ws,
            "RecordStateChanged",
            timeout=STOP_EVENT_TIMEOUT_SEC,
            predicate=lambda d: d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED",
        )
    except asyncio.TimeoutError:
        print(f"error: StopRecord did not emit STOPPED within {STOP_EVENT_TIMEOUT_SEC:.0f}s")
        return False

    deadline = asyncio.get_event_loop().time() + STOP_EVENT_TIMEOUT_SEC
    n = 0
    while True:
        n += 1
        status_response = await request(inbox, ws, "GetRecordStatus", f"stop-status-{n}")
        if not (status_response.get("responseData") or {}).get("outputActive"):
            return True
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            print("error: STOPPED event arrived but GetRecordStatus.outputActive stayed true")
            return False
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


def status_of(resp: dict) -> tuple[bool, str, str]:
    st = resp["requestStatus"]
    return bool(st.get("result")), str(st.get("code")), str(st.get("comment") or "")


def named_refusal(comment: str, *must_contain: str) -> bool:
    """A refusal is named when it carries a cause, and that cause is not one of
    upstream's generic sentences."""
    if not comment.strip():
        print("error: refusal carried no comment at all (mute false)")
        return False
    for generic in UPSTREAM_GENERIC:
        if generic in comment:
            print(f"error: refusal is upstream's generic advice, not a cause: {comment!r}")
            return False
    for needle in must_contain:
        if needle.lower() not in comment.lower():
            print(f"error: refusal does not name {needle!r}: {comment!r}")
            return False
    return True


async def wait_first_byte(inbox: Inbox, ws) -> bool:
    """Poll GetRecordStatus until the muxer wrote its first byte."""
    deadline = asyncio.get_event_loop().time() + FIRST_BYTE_TIMEOUT_SEC
    n = 0
    while asyncio.get_event_loop().time() < deadline:
        n += 1
        resp = await request(inbox, ws, "GetRecordStatus", f"bytes-{n}")
        if int(resp["responseData"].get("outputBytes") or 0) > 0:
            return True
        await asyncio.sleep(0.25)
    return False


async def cleanup_recording(inbox: Inbox, ws, reason: str) -> bool:
    """Best-effort bounded cleanup after a failed recording assertion.

    A failed first-byte/split assertion must not leave the next probe attached
    to a recording from this run.  Cleanup still requires the normal terminal
    STOPPED event and inactive status; it never turns an incomplete stop into a
    pass.
    """
    status = await request(inbox, ws, "GetRecordStatus", f"cleanup-status-{reason}")
    if not (status.get("responseData") or {}).get("outputActive"):
        return True
    print(f"   cleanup: recording remains active after {reason}; requesting StopRecord")
    stopped = await wait_record_stop(
        inbox, ws, await request(inbox, ws, "StopRecord", f"cleanup-stop-{reason}")
    )
    if not stopped:
        print(f"error: cleanup after {reason} did not reach the terminal STOPPED/inactive state")
    return stopped


async def probe(url: str, password: str) -> int:
    print(f"connecting: {url}")
    async with websockets.connect(url, subprotocols=["obswebsocket.json"]) as ws:
        hello = json.loads(await ws.recv())
        rpc = hello["d"]["rpcVersion"]
        identify_d: dict = {"rpcVersion": rpc, "eventSubscriptions": EVENT_SUBSCRIPTION_ALL}
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

        resp = await request(inbox, ws, "GetRecordStatus", "rec-status-0")
        if resp["responseData"]["outputActive"]:
            print("   recording was still active before probe; draining the previous StopRecord")
            recovery = await request(inbox, ws, "StopRecord", "stop-recovery")
            if not await wait_record_stop(inbox, ws, recovery):
                return 1

        # -- Criterion 2, off-air half: no recording => refusal, not success.
        for req in ("SplitRecordFile", "CreateRecordChapter"):
            resp = await request(inbox, ws, req, f"idle-{req}")
            ok, code, comment = status_of(resp)
            if ok:
                print(f"error: {req} reported success with no recording running")
                return 1
            print(f"   {req} (idle) refused: code={code} comment={comment!r}")

        resp = await request(inbox, ws, "GetRecordDirectory", "dir-1")
        record_dir = pathlib.Path(resp["responseData"]["recordDirectory"])
        before = {p.resolve() for p in record_dir.glob("*.mp4")} if record_dir.exists() else set()
        print(f"   record directory: {record_dir} ({len(before)} mp4 already there)")

        print("-> StartRecord")
        resp = await request(inbox, ws, "StartRecord", "start-1")
        if not resp["requestStatus"]["result"]:
            print(f"error: StartRecord declined: {resp['requestStatus']}")
            return 1
        try:
            await expect_event(
                inbox, ws, "RecordStateChanged",
                predicate=lambda d: d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED",
                timeout=10.0,
            )
        except Exception:
            await cleanup_recording(inbox, ws, "start-event-timeout")
            raise

        if not await wait_first_byte(inbox, ws):
            print("error: the muxer wrote no byte within "
                  f"{FIRST_BYTE_TIMEOUT_SEC}s -- nothing to split")
            await cleanup_recording(inbox, ws, "first-byte-timeout")
            return 1
        print(f"   recording for {PRE_SPLIT_SEC}s before the split ...")
        await asyncio.sleep(PRE_SPLIT_SEC)

        # -- Criterion 1: the split is accepted AND a new file is really opened.
        print("-> SplitRecordFile")
        resp = await request(inbox, ws, "SplitRecordFile", "split-1")
        ok, code, comment = status_of(resp)
        if not ok:
            print(f"error: SplitRecordFile refused on a live recording: code={code} comment={comment!r}")
            await wait_record_stop(inbox, ws, await request(inbox, ws, "StopRecord", "stop-abort"))
            return 1

        try:
            evt = await expect_event(inbox, ws, "RecordFileChanged", timeout=15.0)
        except asyncio.TimeoutError:
            print("error: SplitRecordFile answered success but no RecordFileChanged "
                  "event followed -- the muxer never switched file")
            await wait_record_stop(inbox, ws, await request(inbox, ws, "StopRecord", "stop-abort"))
            return 1
        new_path = pathlib.Path(evt["eventData"]["newOutputPath"])
        print(f"   <- RecordFileChanged newOutputPath={new_path}")

        # -- Criterion 2, on-air half: the chapter refusal names its cause.
        print("-> CreateRecordChapter")
        resp = await request(inbox, ws, "CreateRecordChapter", "chapter-1",
                             {"chapterName": "probe"})
        ok, code, comment = status_of(resp)
        print(f"   CreateRecordChapter: result={ok} code={code} comment={comment!r}")
        if ok:
            # Would mean the record output grew chapter support (mp4_output).
            # Not a failure of the contract -- but the probe's expectation must
            # then be revisited deliberately, so make it loud.
            print("error: CreateRecordChapter succeeded; this build records through an "
                  "output without chapter support. Re-read the probe against #169.")
            await wait_record_stop(inbox, ws, await request(inbox, ws, "StopRecord", "stop-abort"))
            return 1
        if not named_refusal(comment, "add_chapter"):
            await wait_record_stop(inbox, ws, await request(inbox, ws, "StopRecord", "stop-abort"))
            return 1

        await asyncio.sleep(POST_SPLIT_SEC)

        print("-> StopRecord")
        resp = await request(inbox, ws, "StopRecord", "stop-1")
        if not await wait_record_stop(inbox, ws, resp):
            return 1

    # -- The proof is on disk, not in the return codes.
    produced = sorted({p.resolve() for p in record_dir.glob("*.mp4")} - before)
    for p in produced:
        print(f"   produced: {p}  ({p.stat().st_size:,} bytes)")
    if len(produced) < 2:
        print(f"error: expected at least 2 files (pre-split + post-split), got {len(produced)}")
        return 1
    if new_path.resolve() not in produced:
        print(f"error: the file named by RecordFileChanged is not among the produced files: {new_path}")
        return 1
    small = [p for p in produced if p.stat().st_size < MIN_PART_BYTES]
    if small:
        print(f"error: split parts below {MIN_PART_BYTES} bytes: {small}")
        return 1

    print("\n#169 validated: split really cut the recording in two files; "
          "the chapter refusal names its cause")
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
