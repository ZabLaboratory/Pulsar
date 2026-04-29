#!/usr/bin/env python3
"""
Pulsar adaptive bitrate probe (Phase 12b validation).

Drives the Get/SetAdaptive vendor surface and waits long enough for the
worker thread to take at least two samples, so we can confirm:
  - target_kbps was latched from the encoder at first tick
  - stable_ticks accumulates with no drops on the wire
  - SetAdaptiveEnabled(false) -> Get reports enabled=false
  - SetAdaptiveEnabled(true)  -> stable_ticks resets to 0
  - Out-of-range / missing fields produce typed errors

Cannot reproduce real network drops in lab -- the actual feedback-loop
behaviour (ratio>1% triggering down-adjust + recovery climb) is
validated in production via prolonged live tests. This probe ensures
the orchestration plumbing is sound.

Usage (pulsar.exe must be running):
    python scripts/probe-adaptive.py
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
    / "upstream/build_x64/rundir/RelWithDebInfo/bin/64bit/obs-websocket/config.json"
)
TICK_SEC = 2  # mirrors AdaptiveBitrate::TICK_SEC


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
    body: dict = {"vendorName": "pulsar", "requestType": request_type}
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
        identify_d: dict = {"rpcVersion": rpc, "eventSubscriptions": 0x7FF}
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        await ws.recv()
        print("identified")

        inbox = Inbox()

        # Boot state. The worker may not have taken its first tick yet (it
        # sleeps TICK_SEC after starting). target_kbps could still be 0;
        # current_kbps follows.
        s = await vendor_call(inbox, ws, "GetAdaptiveState", "g0")
        print(f"initial: {s}")
        if "_error" in s:
            print("error: GetAdaptiveState rejected (subsystem not initialised?)")
            return 1
        if not s.get("enabled"):
            print("error: adaptive should be enabled by default")
            return 1

        # Wait 3 ticks so the worker has latched target + accumulated >= 1
        # stable tick (no drops in lab).
        wait = TICK_SEC * 3 + 1
        print(f"waiting {wait}s for worker to sample...")
        await asyncio.sleep(wait)

        s = await vendor_call(inbox, ws, "GetAdaptiveState", "g1")
        print(f"after warmup: {s}")
        if not s.get("target_kbps"):
            print("error: target_kbps not latched after warmup")
            return 1
        if s["current_kbps"] != s["target_kbps"]:
            print(f"warn: current ({s['current_kbps']}) != target ({s['target_kbps']}); "
                  f"unexpected with no drops in lab")
        if s["stable_ticks"] < 1:
            print(f"error: stable_ticks should have advanced, got {s['stable_ticks']}")
            return 1
        print(f"   target={s['target_kbps']} current={s['current_kbps']} "
              f"floor={s['floor_kbps']} stable_ticks={s['stable_ticks']}")

        # Disable cycle.
        print("-> SetAdaptiveEnabled(false)")
        r = await vendor_call(inbox, ws, "SetAdaptiveEnabled", "d1", {"enabled": False})
        if r.get("enabled") is not False:
            print(f"error: disable failed: {r}")
            return 1

        await asyncio.sleep(TICK_SEC + 1)
        s = await vendor_call(inbox, ws, "GetAdaptiveState", "g2")
        if s.get("enabled"):
            print("error: adaptive still reports enabled after disable")
            return 1
        ticks_when_disabled = s["stable_ticks"]
        print(f"   disabled state: stable_ticks={ticks_when_disabled}")

        # Reenable -- worker should reset stable_ticks to 0.
        print("-> SetAdaptiveEnabled(true)")
        r = await vendor_call(inbox, ws, "SetAdaptiveEnabled", "e1", {"enabled": True})
        if r.get("enabled") is not True:
            print(f"error: enable failed: {r}")
            return 1
        s = await vendor_call(inbox, ws, "GetAdaptiveState", "g3")
        if s["stable_ticks"] != 0:
            print(f"error: stable_ticks should reset on re-enable, got {s['stable_ticks']}")
            return 1
        print(f"   re-enabled: stable_ticks={s['stable_ticks']}")

        # Validation rejection
        r = await vendor_call(inbox, ws, "SetAdaptiveEnabled", "bad1")  # missing enabled
        if "error" not in r:
            print(f"error: missing 'enabled' should produce typed error, got {r}")
            return 1
        print(f"   typed reject (no enabled): {r['error']}")

    print("\nphase 12b adaptive bitrate orchestration validated")
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
