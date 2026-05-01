#!/usr/bin/env python3
"""
Pulsar source-kinds inventory probe.

Catches regressions where a plugin disappears from the bundle. The
README + docs/PROTOCOL.md promise specific source kinds (window
capture, game capture, browser source, WASAPI variants, ffmpeg/image
sources). This probe asserts each one is registered with libobs and
creatable -- not that it actually captures something. Real DLL-
injection game-capture tests require a graphics target and a real GPU
context that CI runners typically lack ; they belong in a dedicated
on-prem matrix, not here.

What we DO validate :
  - GetInputKindList returns the expected core kinds.
  - For each, CreateInput on a fresh scene succeeds.
  - For each, RemoveInput cleans up without erroring.

What we do NOT validate :
  - Whether captured frames flow (no frame-count comparison).
  - Whether DLL injection succeeds against a target process.
  - Whether the underlying capture stack works on the host hardware.

Usage (from the repo root with pulsar.exe already running):
    pip install websockets
    python scripts/probe-source-kinds.py
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
    print("error: pip install websockets", file=sys.stderr)
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

# Light-variant bundle. These are the kinds every Pulsar build must
# expose -- if any goes missing, the build is broken regardless of
# variant.
REQUIRED_KINDS_LIGHT = [
    "window_capture",                 # Windows Graphics Capture
    "monitor_capture",                # full screen / display capture
    "game_capture",                   # DLL injection capture
    "ffmpeg_source",                  # media files
    "image_source",                   # static images
    "color_source_v3",                # solid color
    "wasapi_output_capture",          # desktop loopback
    "wasapi_input_capture",           # microphone
    "wasapi_process_output_capture",  # per-process loopback (Win10 19041+)
]

# Full-variant kinds. Present only when scripts/build-win.ps1 -Full
# was used (CEF + obs-browser, etc.). Warned-on but not asserted ;
# build.yml runs without -Full, release.yml + live-test.yml build
# with it.
OPTIONAL_KINDS_FULL = [
    "browser_source",
    "vlc_source",
    "text_gdiplus_v3",
    "text_ft2_source_v2",
]


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

        resp = await request(inbox, ws, "GetInputKindList", "list", {})
        if not resp["requestStatus"]["result"]:
            print(f"error: GetInputKindList failed: {resp['requestStatus']}", file=sys.stderr)
            return 1
        kinds = set(resp["responseData"]["inputKinds"])
        print(f"got {len(kinds)} input kinds")

        missing = [k for k in REQUIRED_KINDS_LIGHT if k not in kinds]
        if missing:
            print(f"FAIL required kinds missing: {missing}", file=sys.stderr)
            print(f"     present: {sorted(kinds)}", file=sys.stderr)
            return 1

        present_optional = [k for k in OPTIONAL_KINDS_FULL if k in kinds]
        print(f"required kinds present: {len(REQUIRED_KINDS_LIGHT)}/{len(REQUIRED_KINDS_LIGHT)}")
        print(f"optional (full-variant) kinds present: {present_optional}")

        # Smoke test: create + remove each REQUIRED kind. Skip
        # wasapi_process_output_capture (needs a target window/process
        # to bind to ; CreateInput against an empty 'window' setting is
        # a typed reject, not a sign the source kind is broken).
        test_kinds = [
            k for k in REQUIRED_KINDS_LIGHT
            if k != "wasapi_process_output_capture"
        ]
        scene_name = "probe-source-kinds-scene"

        r = await request(inbox, ws, "CreateScene", "cs", {"sceneName": scene_name})
        if not r["requestStatus"]["result"]:
            print(f"FAIL CreateScene: {r['requestStatus']}", file=sys.stderr)
            return 1

        failed: list[tuple] = []
        try:
            for kind in test_kinds:
                inp_name = f"probe-input-{kind}"
                r = await request(inbox, ws, "CreateInput", f"ci-{kind}", {
                    "sceneName": scene_name,
                    "inputName": inp_name,
                    "inputKind": kind,
                    "inputSettings": {},
                    "sceneItemEnabled": False,
                })
                if not r["requestStatus"]["result"]:
                    failed.append((kind, "CreateInput", r["requestStatus"]))
                    continue
                r = await request(inbox, ws, "RemoveInput", f"ri-{kind}",
                                  {"inputName": inp_name})
                if not r["requestStatus"]["result"]:
                    failed.append((kind, "RemoveInput", r["requestStatus"]))
        finally:
            await request(inbox, ws, "RemoveScene", "rs", {"sceneName": scene_name})

        if failed:
            print("FAIL -- the following kinds could not be created/removed:", file=sys.stderr)
            for f in failed:
                print(f"  {f}", file=sys.stderr)
            return 1

        print(f"smoke test OK ({len(test_kinds)} kinds CreateInput/RemoveInput)")
        print("\nsource-kinds inventory validated")
        return 0


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"error: {CONFIG_PATH} missing -- start pulsar.exe first", file=sys.stderr)
        return 2
    cfg = json.loads(CONFIG_PATH.read_text())
    port = cfg.get("server_port", 4455)
    password = cfg["server_password"]

    return asyncio.run(probe(f"ws://127.0.0.1:{port}", password))


if __name__ == "__main__":
    sys.exit(main())
