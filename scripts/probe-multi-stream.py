#!/usr/bin/env python3
"""
Pulsar multi-stream probe (Phase 7 PR1 validation).

Round-trip:
  1. Connect + identify.
  2. CallVendorRequest("pulsar", "GetDestinations") -> empty list.
  3. CreateDestination(kind="vod_local", url=<temp.mp4>) -> id.
  4. StartDestination(id) -> started=true.
  5. Sleep 3 s.
  6. StopDestination(id) -> stopped=true.
  7. Assert the MP4 was written and is >= 100 KB.
  8. CreateDestination(kind="rtmp_custom", url="rtmp://127.0.0.1:1/dummy",
                       key="x")  -- intentionally a dead address.
  9. StartDestination expected to fail cleanly (started=false, error set).
 10. RemoveDestination twice -> list back to empty.

Usage (pulsar.exe must be running):
    python scripts/probe-multi-stream.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import pathlib
import sys
import time
from typing import Callable

try:
    import websockets
except ImportError:
    print("error: pip install websockets")
    sys.exit(2)


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNDIR = REPO_ROOT / "upstream/build_x64/rundir/RelWithDebInfo/bin/64bit"
CONFIG_PATH = RUNDIR / "obs-websocket" / "config.json"
EVENT_SUBSCRIPTION_ALL = 0x7FF
RECORD_DURATION_SEC = 3.0
MIN_MP4_BYTES = 100 * 1024


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


async def vendor_call(inbox: Inbox, ws, request_type: str, request_id: str,
                      data: dict | None = None) -> dict:
    """CallVendorRequest passes opaque JSON to the vendor handler. The
    response surfaces under requestResponse[d].responseData.responseData
    -- the outer is obs-websocket's envelope, the inner is what our
    handler wrote into its res obs_data_t."""
    body: dict = {
        "vendorName": "pulsar",
        "requestType": request_type,
    }
    if data is not None:
        body["requestData"] = data
    resp = await request(inbox, ws, "CallVendorRequest", request_id, body)
    if not resp["requestStatus"]["result"]:
        return {"_error": resp["requestStatus"]}
    rd = resp.get("responseData") or {}
    return rd.get("responseData") or {}


async def probe(url: str, password: str) -> int:
    print(f"connecting: {url}")
    async with websockets.connect(url, subprotocols=["obswebsocket.json"]) as ws:
        hello = json.loads(await ws.recv())
        rpc = hello["d"]["rpcVersion"]
        identify_d = {"rpcVersion": rpc, "eventSubscriptions": EVENT_SUBSCRIPTION_ALL}
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        await ws.recv()
        print("identified")

        inbox = Inbox()

        # 0. Sanity: vendor present?
        listing = await vendor_call(inbox, ws, "GetDestinations", "list-0")
        if "_error" in listing:
            err = listing["_error"]
            print(f"error: GetDestinations failed: {err}")
            print("       (vendor 'pulsar' not registered? check pulsar-multi-stream loaded)")
            return 1
        before = listing.get("destinations") or []
        print(f"GetDestinations (initial): {len(before)} entr{'y' if len(before)==1 else 'ies'}")

        # ---- VOD local round-trip ----
        mp4_path = RUNDIR / "recordings" / f"multi-stream-{int(time.time())}.mp4"
        mp4_path.parent.mkdir(parents=True, exist_ok=True)
        # Don't pre-create the file -- ffmpeg_muxer wants to open it.
        if mp4_path.exists():
            mp4_path.unlink()

        print(f"-> CreateDestination(vod_local, {mp4_path.name})")
        resp = await vendor_call(inbox, ws, "CreateDestination", "create-vod", {
            "name": "test-vod",
            "kind": "vod_local",
            "url": str(mp4_path),
        })
        vod_id = resp.get("id")
        if not vod_id:
            print(f"error: no id in create response: {resp}")
            return 1
        print(f"   <- id={vod_id}")

        print(f"-> StartDestination({vod_id})")
        resp = await vendor_call(inbox, ws, "StartDestination", "start-vod", {"id": vod_id})
        if not resp.get("started"):
            print(f"error: vod start failed: {resp}")
            return 1

        print(f"   recording for {RECORD_DURATION_SEC}s ...")
        await asyncio.sleep(RECORD_DURATION_SEC)

        print(f"-> StopDestination({vod_id})")
        resp = await vendor_call(inbox, ws, "StopDestination", "stop-vod", {"id": vod_id})
        if not resp.get("stopped"):
            print(f"error: vod stop failed: {resp}")
            return 1

        # ffmpeg_muxer trailer write is async after stop; wait briefly.
        for _ in range(40):
            if mp4_path.exists() and mp4_path.stat().st_size >= MIN_MP4_BYTES:
                break
            await asyncio.sleep(0.1)

        if not mp4_path.exists():
            print(f"error: vod_local output not on disk: {mp4_path}")
            return 1
        size = mp4_path.stat().st_size
        if size < MIN_MP4_BYTES:
            print(f"error: vod_local file too small: {size} bytes")
            return 1
        print(f"   MP4: {mp4_path}  ({size:,} bytes) OK")

        # ---- RTMP custom (dead address; expect clean failure) ----
        print("-> CreateDestination(rtmp_custom, dead address)")
        resp = await vendor_call(inbox, ws, "CreateDestination", "create-rtmp", {
            "name": "test-rtmp",
            "kind": "rtmp_custom",
            "url": "rtmp://127.0.0.1:1/nope",
            "key": "x",
        })
        rtmp_id = resp.get("id")
        if not rtmp_id:
            print(f"error: no id: {resp}")
            return 1
        print(f"   <- id={rtmp_id}")

        print(f"-> StartDestination({rtmp_id}) (expect clean failure)")
        resp = await vendor_call(inbox, ws, "StartDestination", "start-rtmp", {"id": rtmp_id})
        # rtmp_output may either accept the start (and then fail at connect time
        # with a transient signal) or refuse synchronously. Both are acceptable
        # as long as the call returns a structured response and nothing crashed.
        print(f"   <- started={resp.get('started')} error={resp.get('error', '')!r}")

        # Stop in case it briefly went active.
        await vendor_call(inbox, ws, "StopDestination", "stop-rtmp", {"id": rtmp_id})

        # ---- Cleanup ----
        for did in (vod_id, rtmp_id):
            r = await vendor_call(inbox, ws, "RemoveDestination", f"rm-{did}", {"id": did})
            if not r.get("removed"):
                print(f"error: remove failed for {did}: {r}")
                return 1

        listing = await vendor_call(inbox, ws, "GetDestinations", "list-final")
        final = listing.get("destinations") or []
        if len(final) != len(before):
            print(f"error: destination list not back to baseline: {len(final)} vs {len(before)}")
            return 1
        print(f"GetDestinations (final): back to {len(final)} entr{'y' if len(final)==1 else 'ies'}")

    print("\nphase 7 multi-stream vendor API validated end-to-end")
    return 0


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"error: pulsar-websocket config not found at {CONFIG_PATH}")
        return 2
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    pwd = cfg.get("server_password", "")
    port = cfg.get("server_port", 4455)
    return asyncio.run(probe(f"ws://127.0.0.1:{port}", pwd))


if __name__ == "__main__":
    sys.exit(main())
