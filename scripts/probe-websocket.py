#!/usr/bin/env python3
"""
Pulsar WebSocket round-trip probe (Phase 4e validation).

Connects to a running pulsar.exe + pulsar-websocket plugin over
ws://127.0.0.1:<port>, performs the obs-websocket v5 handshake
(Hello -> Identify -> Identified), issues a GetVersion request, and
prints the response.

The server password is read from the config.json that pulsar-websocket
persists on First Load. Under headless, obs_module_get_config_path()
returns a relative path, so the file lives at:
    <rundir>/bin/64bit/obs-websocket/config.json

Usage (from the repo root with pulsar.exe already running):
    pip install websockets
    python scripts/probe-websocket.py
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
WS_URL_DEFAULT = "ws://127.0.0.1:4455"


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


async def probe(url: str, password: str) -> int:
    print(f"connecting: {url}")
    async with websockets.connect(url, subprotocols=["obswebsocket.json"]) as ws:
        # op=0 Hello
        hello = json.loads(await ws.recv())
        if hello["op"] != 0:
            print(f"error: expected Hello (op=0), got {hello}")
            return 1
        ver = hello["d"]["obsWebSocketVersion"]
        rpc = hello["d"]["rpcVersion"]
        print(f"hello: obsWebSocketVersion={ver} rpcVersion={rpc}")

        identify_d = {"rpcVersion": rpc}
        if "authentication" in hello["d"]:
            auth = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(
                password, auth["salt"], auth["challenge"]
            )
            print("auth: computed challenge response")
        else:
            print("auth: server does not require authentication")

        await ws.send(json.dumps({"op": 1, "d": identify_d}))

        # op=2 Identified
        ident = json.loads(await ws.recv())
        if ident["op"] != 2:
            print(f"error: expected Identified (op=2), got {ident}")
            return 1
        print(f"identified: negotiatedRpcVersion={ident['d']['negotiatedRpcVersion']}")

        # op=6 Request — GetVersion
        await ws.send(
            json.dumps(
                {
                    "op": 6,
                    "d": {
                        "requestType": "GetVersion",
                        "requestId": "probe-1",
                    },
                }
            )
        )

        # op=7 RequestResponse
        resp = json.loads(await ws.recv())
        if resp["op"] != 7:
            print(f"error: expected RequestResponse (op=7), got {resp}")
            return 1
        if not resp["d"]["requestStatus"]["result"]:
            print(f"error: GetVersion failed: {resp['d']['requestStatus']}")
            return 1

        print("GetVersion ok:")
        for k, v in resp["d"]["responseData"].items():
            print(f"  {k}: {v}")

    print("connection closed cleanly")
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
