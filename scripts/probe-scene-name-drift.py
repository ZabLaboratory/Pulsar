#!/usr/bin/env python3
"""
Pulsar scene-source name-drift regression probe (#110).

Reproduces, in pure libobs state (no CEF paint, no GPU), the bug where
repeated `pulsar-scene:SetCaptureSource` calls stacked stale
browser_source items on the program scene. The root cause: the plugin
created the new source (canonical name "PulsarSceneSource") BEFORE
removing the old one, so libobs de-duped the fresh instance to
"PulsarSceneSource 2" — a name the exact-strcmp cleanup then missed,
leaving the stale instance stranded on the scene forever.

WHAT THIS PROVES
  After N consecutive SetCaptureSource calls (N >= 4, matching the
  investigation's probe_name_drift.py), the program scene holds EXACTLY
  ONE Pulsar-managed browser_source at every step — no accumulation of
  numbered variants ("PulsarSceneSource 2", " 3", ...). It also asserts
  the surviving instance keeps the canonical name so a name-based
  consumer (Prism's findBrowserSourceName) can never lock onto a stale
  one.

WHAT THIS DOES NOT PROVE
  Nothing about rendered pixels — the URLs point at about:blank-class
  placeholders; CEF is never asked to paint anything meaningful. The
  visual "fresh content actually composites" check is the interactive
  live probe (needs a desktop + Solar), out of CI scope by doctrine.

LIGHT BUILD
  A -Light build has no obs-browser, so SetCaptureSource returns
  "browser_source_unavailable". The probe then exits 3 (typed skip),
  mirroring probe-browser-m3.py, so the offline suite still passes on a
  light build. The -Full CI build asserts for real.

LICENSE INVARIANT: talks to pulsar.exe over the obs-websocket process
boundary ONLY — no FFI, no ctypes, no LoadLibrary of obs.dll.

Usage (from the repo root with pulsar.exe already running):
    pip install websockets
    python scripts/probe-scene-name-drift.py
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

# Must match plugins/pulsar-scene-source/src/plugin-main.cpp.
CANONICAL_NAME = "PulsarSceneSource"
# How many re-points to drive. The bug is an intermittent timing race (the
# stale de-duped name surfaced on the 4th call of one CI run, not another),
# so drive MANY rapid consecutive re-points to give the race every chance to
# manifest. The original investigation reproduced coexistence by the 4th
# call; we go well past that to keep a real fix from passing on luck.
N_CALLS = 24


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


async def vendor_set_capture_source(inbox: Inbox, ws, request_id: str,
                                    data: dict) -> dict:
    body = {
        "vendorName": "pulsar-scene",
        "requestType": "SetCaptureSource",
        "requestData": data,
    }
    resp = await request(inbox, ws, "CallVendorRequest", request_id, body)
    if not resp["requestStatus"]["result"]:
        return {"_error": resp["requestStatus"]}
    rd = resp.get("responseData") or {}
    return rd.get("responseData") or {}


def scene_request_data(scene: dict[str, str]) -> dict[str, str]:
    """Address a scene by its public Main-canvas UUID when available."""
    if "sceneUuid" in scene:
        return {"sceneUuid": scene["sceneUuid"]}
    return {"sceneName": scene["sceneName"]}


async def resolve_main_canvas_scene(inbox: Inbox, ws) -> dict[str, str]:
    """Resolve a scene that is registered on the public Main canvas.

    A failed dual-lane setup can expose its private physical root through
    ``GetCurrentProgramScene`` even though that root is not addressable on Main.
    ``GetSceneList`` is the authoritative public inventory.  If the reported
    selection is absent, select the first public scene and verify the frontend
    accepted it before using it for the drift assertions.
    """
    current = await request(inbox, ws, "GetCurrentProgramScene", "gcps", {})
    if not current["requestStatus"]["result"]:
        raise RuntimeError(f"GetCurrentProgramScene failed: {current['requestStatus']}")
    current_data = current["responseData"]
    reported_name = current_data.get("currentProgramSceneName") or current_data.get("sceneName")
    reported_uuid = current_data.get("currentProgramSceneUuid") or current_data.get("sceneUuid")

    listing = await request(inbox, ws, "GetSceneList", "gsl", {})
    if not listing["requestStatus"]["result"]:
        raise RuntimeError(f"GetSceneList failed: {listing['requestStatus']}")
    refs: list[dict[str, str]] = []
    for entry in listing["responseData"].get("scenes") or []:
        name = entry.get("sceneName") or entry.get("name")
        uuid = entry.get("sceneUuid") or entry.get("uuid")
        if name:
            ref = {"sceneName": str(name)}
            if uuid:
                ref["sceneUuid"] = str(uuid)
            refs.append(ref)

    selected = next(
        (ref for ref in refs if (reported_uuid and ref.get("sceneUuid") == reported_uuid)
         or (reported_name and ref.get("sceneName") == reported_name)),
        None,
    )
    if selected is None:
        if not refs:
            raise RuntimeError(
                "GetSceneList returned no Main-canvas scenes while GetCurrentProgramScene "
                f"reported {reported_name or reported_uuid!r}")
        selected = refs[0]
        print(
            "note: current program selection is not registered on Main canvas "
            f"({reported_name or reported_uuid!r}); selecting public scene "
            f"{selected['sceneName']!r}")
        selected_request = await request(
            inbox, ws, "SetCurrentProgramScene", "scs-main", scene_request_data(selected))
        if not selected_request["requestStatus"]["result"]:
            raise RuntimeError(
                f"SetCurrentProgramScene failed: {selected_request['requestStatus']}")

        refreshed = await request(inbox, ws, "GetCurrentProgramScene", "gcps-main", {})
        if not refreshed["requestStatus"]["result"]:
            raise RuntimeError(
                f"GetCurrentProgramScene after repair failed: {refreshed['requestStatus']}")
        refreshed_data = refreshed["responseData"]
        refreshed_name = refreshed_data.get("currentProgramSceneName") or refreshed_data.get("sceneName")
        refreshed_uuid = refreshed_data.get("currentProgramSceneUuid") or refreshed_data.get("sceneUuid")
        if (("sceneUuid" in selected and refreshed_uuid != selected["sceneUuid"])
                or ("sceneUuid" not in selected and refreshed_name != selected["sceneName"])):
            raise RuntimeError(
                "SetCurrentProgramScene did not select the Main-canvas scene "
                f"{selected['sceneName']!r}: got {refreshed_name or refreshed_uuid!r}")

    return selected


def is_managed_variant(name: str) -> bool:
    """Mirror of the C++ is_managed_variant: base, or 'base <digits>'."""
    if name == CANONICAL_NAME:
        return True
    if not name.startswith(CANONICAL_NAME + " "):
        return False
    suffix = name[len(CANONICAL_NAME) + 1:]
    return suffix.isdigit()


async def managed_browser_items(inbox: Inbox, ws, scene: dict[str, str]) -> list[str]:
    r = await request(inbox, ws, "GetSceneItemList", "gsil", scene_request_data(scene))
    if not r["requestStatus"]["result"]:
        raise RuntimeError(f"GetSceneItemList failed: {r['requestStatus']}")
    items = r["responseData"]["sceneItems"]
    return [
        it["sourceName"]
        for it in items
        if it.get("inputKind") == "browser_source"
        and is_managed_variant(it["sourceName"])
    ]


async def probe(url: str, password: str) -> int:
    print(f"connecting: {url}")
    async with websockets.connect(url, subprotocols=["obswebsocket.json"]) as ws:
        hello = json.loads(await ws.recv())
        rpc = hello["d"]["rpcVersion"]
        identify_d: dict = {"rpcVersion": rpc,
                            "eventSubscriptions": EVENT_SUBSCRIPTION_ALL}
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(
                password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        await ws.recv()
        print("identified")

        inbox = Inbox()

        try:
            scene = await resolve_main_canvas_scene(inbox, ws)
        except RuntimeError as exc:
            print(f"FAIL Main-canvas scene setup: {exc}", file=sys.stderr)
            return 1
        print(f"program scene: {scene['sceneName']!r}")

        # First call also tells us whether obs-browser is present at all.
        first = await vendor_set_capture_source(inbox, ws, "scs-0", {
            "kind": "browser_source",
            "url": "http://127.0.0.1:1/probe-name-drift/0",
            "width": 1280, "height": 720, "fps": 30,
        })
        if "_error" in first:
            print(f"FAIL SetCaptureSource[0]: {first['_error']}",
                  file=sys.stderr)
            return 1
        if first.get("error") == "browser_source_unavailable":
            print("SKIP: light build — obs-browser absent, "
                  "browser_source cannot be created")
            return 3
        if first.get("kind") != "browser_source":
            print(f"FAIL SetCaptureSource[0] unexpected reply: {first}",
                  file=sys.stderr)
            return 1

        # Assert the invariant after call 0, then drive N_CALLS-1 more
        # re-points, asserting exactly one managed browser_source at every
        # step. The bug stacked a second one from the 2nd call onward.
        for i in range(N_CALLS):
            if i > 0:
                resp = await vendor_set_capture_source(inbox, ws, f"scs-{i}", {
                    "kind": "browser_source",
                    "url": f"http://127.0.0.1:1/probe-name-drift/{i}",
                    "width": 1280, "height": 720, "fps": 30,
                })
                if "_error" in resp or resp.get("kind") != "browser_source":
                    print(f"FAIL SetCaptureSource[{i}]: {resp}",
                          file=sys.stderr)
                    return 1

            managed = await managed_browser_items(inbox, ws, scene)
            print(f"  after call {i}: managed browser_source(s) = {managed}")
            if len(managed) != 1:
                print(f"FAIL: expected exactly 1 managed browser_source "
                      f"after call {i}, found {len(managed)}: {managed}",
                      file=sys.stderr)
                return 1
            if managed[0] != CANONICAL_NAME:
                print(f"FAIL: surviving source is {managed[0]!r}, expected the "
                      f"canonical {CANONICAL_NAME!r} (a numbered variant would "
                      f"break name-based consumers)", file=sys.stderr)
                return 1

        print(f"\nname-drift invariant holds: 1 canonical browser_source "
              f"across {N_CALLS} SetCaptureSource calls (#110)")
        return 0


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"error: {CONFIG_PATH} missing -- start pulsar.exe first",
              file=sys.stderr)
        return 2
    cfg = json.loads(CONFIG_PATH.read_text())
    port = cfg.get("server_port", 4455)
    password = cfg["server_password"]

    return asyncio.run(probe(f"ws://127.0.0.1:{port}", password))


if __name__ == "__main__":
    sys.exit(main())
