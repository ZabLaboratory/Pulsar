#!/usr/bin/env python3
"""
Pulsar WebSocket events probe (Phase 5 validation).

Builds on probe-websocket.py: completes the v5 handshake with an
eventSubscriptions mask covering Ui + General + Scenes, then exercises
the path that proves pulsar-frontend-stub dispatches events on the wire:

  1. SetStudioModeEnabled(true)   -> expect StudioModeStateChanged{outputActive: true}
  2. SetStudioModeEnabled(false)  -> expect StudioModeStateChanged{outputActive: false}

Studio mode flips through obs_frontend_set_preview_program_mode in the
stub, which fires OBS_FRONTEND_EVENT_STUDIO_MODE_ENABLED/DISABLED;
obs-websocket's EventHandler::OnFrontendEvent translates that to the v5
StudioModeStateChanged event. Receiving it end-to-end validates:

  - obs_frontend_set_callbacks_internal was called before plugin load
  - obs-websocket's add_event_callback successfully registered
  - obs_set_ui_task_handler was installed (otherwise the obs_queue_task
    inside SetStudioModeEnabled drops the call silently)
  - PulsarFrontendAPI::on_event dispatches to registered callbacks

Usage (from the repo root with pulsar.exe already running):
    pip install websockets
    python scripts/probe-events.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import pathlib
import sys

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

# obs-websocket v5 EventSubscription bit layout. Ui = 1 << 10 carries
# StudioModeStateChanged. We pass All (every bit set) because Phase 5
# also wants to observe SceneListChanged etc. once those are wired.
EVENT_SUBSCRIPTION_ALL = 0x7FF  # General..Ui (bits 0..10)


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


# Shared inbox so request() doesn't accidentally drop events that arrive
# before the response. obs-websocket dispatches events on a thread pool and
# responses on the asio thread -- their ordering on the wire is not stable,
# so we drain into one queue and dispatch by op.
class Inbox:
    def __init__(self):
        self.events: list[dict] = []
        self.responses: list[dict] = []

    async def pump(self, ws, until: callable, timeout: float):
        """Consume incoming frames until until() returns True for the
        already-buffered state. Raises TimeoutError if nothing matches."""
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
            # silently drop other ops (Hello/Identified handled in probe)


async def expect_event(inbox: Inbox, ws, event_type: str, timeout: float = 5.0) -> dict:
    def has_event(ix: Inbox) -> bool:
        return any(e.get("eventType") == event_type for e in ix.events)

    await inbox.pump(ws, has_event, timeout)
    for i, e in enumerate(inbox.events):
        if e.get("eventType") == event_type:
            return inbox.events.pop(i)
    raise RuntimeError("unreachable")


async def request(inbox: Inbox, ws, request_type: str, request_id: str,
                  data: dict | None = None) -> dict:
    body: dict = {"requestType": request_type, "requestId": request_id}
    if data is not None:
        body["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": body}))

    def has_response(ix: Inbox) -> bool:
        return any(r["requestId"] == request_id for r in ix.responses)

    await inbox.pump(ws, has_response, timeout=5.0)
    for i, r in enumerate(inbox.responses):
        if r["requestId"] == request_id:
            return inbox.responses.pop(i)
    raise RuntimeError("unreachable")


async def probe(url: str, password: str) -> int:
    print(f"connecting: {url}")
    async with websockets.connect(url, subprotocols=["obswebsocket.json"]) as ws:
        # Hello (op=0)
        hello = json.loads(await ws.recv())
        if hello["op"] != 0:
            print(f"error: expected Hello (op=0), got {hello}")
            return 1
        rpc = hello["d"]["rpcVersion"]
        print(f"hello: rpcVersion={rpc}")

        # Identify (op=1)
        identify_d: dict = {
            "rpcVersion": rpc,
            "eventSubscriptions": EVENT_SUBSCRIPTION_ALL,
        }
        if "authentication" in hello["d"]:
            auth = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(
                password, auth["salt"], auth["challenge"]
            )
            print("auth: computed challenge response")

        await ws.send(json.dumps({"op": 1, "d": identify_d}))

        ident = json.loads(await ws.recv())
        if ident["op"] != 2:
            print(f"error: expected Identified (op=2), got {ident}")
            return 1
        print(
            f"identified: subscriptions=0x{EVENT_SUBSCRIPTION_ALL:x} "
            f"rpc={ident['d']['negotiatedRpcVersion']}"
        )

        inbox = Inbox()

        # Sanity: GetStudioModeEnabled before mutation. Needs Ui.
        resp = await request(inbox, ws, "GetStudioModeEnabled", "init-studio")
        before = resp["responseData"]["studioModeEnabled"]
        print(f"GetStudioModeEnabled (initial): {before}")

        async def toggle(target: bool, request_id: str) -> int:
            print(f"-> SetStudioModeEnabled({target})")
            resp = await request(
                inbox, ws, "SetStudioModeEnabled", request_id,
                {"studioModeEnabled": target},
            )
            if not resp["requestStatus"]["result"]:
                print(f"error: SetStudioModeEnabled failed: {resp['requestStatus']}")
                return 1
            try:
                evt = await expect_event(inbox, ws, "StudioModeStateChanged", timeout=3.0)
            except asyncio.TimeoutError:
                print(f"error: timed out waiting for StudioModeStateChanged({target})")
                return 1
            active = evt["eventData"]["studioModeEnabled"]
            if active != target:
                print(f"error: expected studioModeEnabled={target}, got {active}")
                return 1
            print(f"   <- StudioModeStateChanged.studioModeEnabled = {active} OK")
            return 0

        rc = await toggle(not before, "set-studio-1")
        if rc:
            return rc
        rc = await toggle(before, "set-studio-2")
        if rc:
            return rc

    print("\nphase 5 events validated end-to-end")
    return 0


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"error: pulsar-websocket config not found at {CONFIG_PATH}")
        print("Launch pulsar.exe at least once so the config + password are written.")
        return 2

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    password = config.get("server_password", "")
    if not password:
        print(f"error: no server_password in {CONFIG_PATH}")
        return 2
    port = config.get("server_port", 4455)
    url = f"ws://127.0.0.1:{port}"

    return asyncio.run(probe(url, password))


if __name__ == "__main__":
    sys.exit(main())
