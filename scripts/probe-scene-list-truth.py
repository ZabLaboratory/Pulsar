#!/usr/bin/env python3
"""
Pulsar scene-list truth probe (#119, ADR Prism 026 §3.1).

Regression guard for the frontend stub mirroring a state libobs owns.

THE BUG IT PINS
  PulsarFrontendAPI::obs_frontend_get_scenes used to iterate an internal
  `scenes` vector that was only ever appended to at setup(). A scene
  created by an obs-websocket `CreateScene` request went straight to
  libobs, so the request returned a REAL sceneUuid while GetSceneList
  never listed it. The defect was structurally invisible: the scene was
  usable (SetCurrentProgramScene takes the raw source pointer), so
  nothing broke until a consumer LISTED scenes after creating one --
  which Prism's calibration does.

WHAT THIS PROVES
  1. CreateScene -> GetSceneList contains the new scene, by BOTH name
     and uuid (resolution criterion 1).
  2. The pre-existing scenes are still enumerated (no regression on the
     boot / collection-loaded scenes -- criterion 3).
  3. RemoveScene -> GetSceneList no longer lists it. The list tracks
     libobs in both directions, which a snapshot mirror cannot do.

WHAT THIS DOES NOT PROVE
  Nothing about rendering, nor about scenes created by a collection
  file (there is no collection loading in the headless stub).

LICENSE INVARIANT (LICENSE-INVARIANTS.md): talks to pulsar.exe over the
obs-websocket process boundary ONLY -- no FFI, no ctypes, no
LoadLibrary of obs.dll.

Usage (from the repo root with pulsar.exe already running):
    pip install websockets
    python scripts/probe-scene-list-truth.py
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

SCENE_NAME = "probe-scene-list-truth"


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


class Inbox:
    def __init__(self):
        self.responses: list[dict] = []

    async def pump(self, ws, until: Callable[["Inbox"], bool], timeout: float):
        end = asyncio.get_event_loop().time() + timeout
        while not until(self):
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
            if msg.get("op") == 7:
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


async def scene_list(inbox: Inbox, ws, request_id: str) -> list[dict]:
    r = await request(inbox, ws, "GetSceneList", request_id, {})
    if not r["requestStatus"]["result"]:
        raise RuntimeError(f"GetSceneList failed: {r['requestStatus']}")
    return r["responseData"]["scenes"]


async def probe(url: str, password: str) -> int:
    print(f"connecting: {url}")
    async with websockets.connect(url, subprotocols=["obswebsocket.json"]) as ws:
        hello = json.loads(await ws.recv())
        identify_d = {"rpcVersion": hello["d"]["rpcVersion"]}
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        await ws.recv()
        print("identified")

        inbox = Inbox()

        before = await scene_list(inbox, ws, "list-before")
        names_before = [s["sceneName"] for s in before]
        print(f"scenes before: {names_before}")
        if not names_before:
            print("FAIL GetSceneList is empty before CreateScene -- the boot "
                  "scene must be enumerated", file=sys.stderr)
            return 1
        if SCENE_NAME in names_before:
            print(f"FAIL '{SCENE_NAME}' already exists -- stale state from a "
                  "previous run", file=sys.stderr)
            return 1

        r = await request(inbox, ws, "CreateScene", "create", {"sceneName": SCENE_NAME})
        if not r["requestStatus"]["result"]:
            print(f"FAIL CreateScene: {r['requestStatus']}", file=sys.stderr)
            return 1
        created_uuid = r["responseData"]["sceneUuid"]
        print(f"CreateScene ok: sceneUuid={created_uuid}")

        rc = 0
        try:
            after = await scene_list(inbox, ws, "list-after")
            names_after = [s["sceneName"] for s in after]
            uuids_after = [s["sceneUuid"] for s in after]
            print(f"scenes after:  {names_after}")

            # Criterion 1 -- the created scene IS listed, by name and uuid.
            if SCENE_NAME not in names_after:
                print(f"FAIL '{SCENE_NAME}' created with uuid {created_uuid} but "
                      "absent from GetSceneList -- the frontend stub is "
                      "answering from a stale mirror (#119)", file=sys.stderr)
                return 1
            if created_uuid not in uuids_after:
                print(f"FAIL sceneUuid {created_uuid} returned by CreateScene is "
                      f"not in GetSceneList uuids {uuids_after}", file=sys.stderr)
                return 1

            # Criterion 3 -- no enumeration regression on pre-existing scenes.
            lost = [n for n in names_before if n not in names_after]
            if lost:
                print(f"FAIL scenes dropped from the list: {lost}", file=sys.stderr)
                return 1

            print("CreateScene -> GetSceneList: scene present (name + uuid)")
        finally:
            rm = await request(inbox, ws, "RemoveScene", "remove",
                               {"sceneName": SCENE_NAME})
            if not rm["requestStatus"]["result"]:
                print(f"FAIL RemoveScene: {rm['requestStatus']}", file=sys.stderr)
                rc = 1

        if rc != 0:
            return rc

        # The list tracks libobs in both directions -- a snapshot cannot.
        final = await scene_list(inbox, ws, "list-final")
        names_final = [s["sceneName"] for s in final]
        if SCENE_NAME in names_final:
            print(f"FAIL '{SCENE_NAME}' still listed after RemoveScene: "
                  f"{names_final}", file=sys.stderr)
            return 1
        print(f"RemoveScene -> GetSceneList: scene gone ({names_final})")

        print("\nscene list is libobs truth, not a mirror")
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
