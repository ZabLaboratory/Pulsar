#!/usr/bin/env python3
"""
Pulsar capability-manifest inventory SHAPE probe (#144, ADR Prism 027 §3.3
blocs 3 et 4).

The manifest's inventory block declares a PRESENCE, never a permission: which
filter / source / destination kinds exist in this binary, and nothing more.
The bounds of a filter's settings stay owned by Prism's closed whitelist
(ADR 023 §3.3, under its own security clearance) -- a bound that leaked into
`capabilities.filters` would let a consumer derive one from the manifest and
void that control.

The client-side decoder already refuses to surface anything but the `value`
field. That is the right lock, but it is the CONSUMER's. This probe is the
EMITTER's: it asks a live pulsar.exe what it actually puts on the wire.

What we DO validate, against a running binary:
  - GetCapabilities answers a `version` >= 1 and a `capabilities` object;
  - every item of filters / source_kinds / destination_kinds is an object
    carrying EXACTLY the key `value`, whose content is a non-empty string --
    so no property bound can ride along;
  - the `filters` entry itself carries only `applicability` + `values`;
  - `video_colorimetry`, when declared, is `read-only` and carries no list of
    "available" spaces (nothing can select one).

What we do NOT validate:
  - WHICH filters/kinds are registered (that is build-variant dependent, and
    a light build legitimately registers fewer than a -Full one);
  - that anything in the list can be instantiated (probe-source-kinds.py).

An inventory the manifest does not declare at all is NOT a failure: absence is
a positive answer under §3.2 and the consumer keeps its own static list. Only
a declared-but-malformed inventory fails here.

Usage (from the repo root with pulsar.exe already running):
    pip install websockets
    python scripts/probe-manifest-inventories.py
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

INVENTORY_KEYS = ["filters", "source_kinds", "destination_kinds"]


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
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            msg = json.loads(raw)
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


async def vendor_call(inbox: Inbox, ws, request_type: str, request_id: str) -> dict:
    resp = await request(inbox, ws, "CallVendorRequest", request_id,
                         {"vendorName": "pulsar", "requestType": request_type})
    if not resp["requestStatus"]["result"]:
        return {"_error": resp["requestStatus"]}
    rd = resp.get("responseData") or {}
    return rd.get("responseData") or {}


def check_inventory(name: str, entry: dict, problems: list[str]) -> int:
    """Returns the number of items checked."""
    values = entry.get("values")
    if not isinstance(values, list):
        problems.append(f"{name}: declared but 'values' is not a list ({type(values).__name__})")
        return 0
    for idx, item in enumerate(values):
        if not isinstance(item, dict):
            problems.append(f"{name}[{idx}]: item is {type(item).__name__}, expected an object")
            continue
        extra = sorted(k for k in item if k != "value")
        if extra:
            problems.append(
                f"{name}[{idx}]: carries {extra} beside 'value' -- the inventory declares a "
                f"presence, never a bound (ADR 027 §3.1 / ADR 023 §3.3)"
            )
        v = item.get("value")
        if not isinstance(v, str) or not v:
            problems.append(f"{name}[{idx}]: 'value' is {v!r}, expected a non-empty string")
    return len(values)


async def probe(url: str, password: str) -> int:
    print(f"connecting: {url}")
    async with websockets.connect(url, subprotocols=["obswebsocket.json"]) as ws:
        hello = json.loads(await ws.recv())
        rpc = hello["d"]["rpcVersion"]
        identify_d: dict = {"rpcVersion": rpc, "eventSubscriptions": 0}
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        await ws.recv()
        print("identified")

        inbox = Inbox()
        caps = await vendor_call(inbox, ws, "GetCapabilities", "getcaps")
        if "_error" in caps:
            print(f"FAIL GetCapabilities: {caps['_error']}", file=sys.stderr)
            return 1

        problems: list[str] = []

        version = caps.get("version")
        if not isinstance(version, int) or version < 1:
            problems.append(f"version is {version!r}, expected an int >= 1 (#141)")

        block = caps.get("capabilities")
        if not isinstance(block, dict):
            print(f"FAIL 'capabilities' is {type(block).__name__}, expected an object (#141)",
                  file=sys.stderr)
            return 1

        for name in INVENTORY_KEYS:
            entry = block.get(name)
            if entry is None:
                # Absence is a positive answer (§3.2): the consumer keeps its
                # own static list. Nothing to check, nothing to fail.
                print(f"  {name}: not declared (absent -- consumer keeps its static list)")
                continue
            if not isinstance(entry, dict):
                problems.append(f"{name}: entry is {type(entry).__name__}, expected an object")
                continue
            if entry.get("applicability") not in ("live", "boot-fixed", "read-only"):
                problems.append(f"{name}: applicability is {entry.get('applicability')!r}")
            n = check_inventory(name, entry, problems)
            print(f"  {name}: {n} item(s), regime {entry.get('applicability')!r}")

        # The filters entry must carry NOTHING beside its regime and its list --
        # a sibling key would be exactly the smuggled bound this block forbids.
        filters = block.get("filters")
        if isinstance(filters, dict):
            extra = sorted(k for k in filters if k not in ("applicability", "values"))
            if extra:
                problems.append(
                    f"filters: entry carries {extra} beside applicability/values -- no filter "
                    f"property bound belongs in the manifest (ADR 023 §3.3 owns them)"
                )

        colorimetry = block.get("video_colorimetry")
        if isinstance(colorimetry, dict):
            if colorimetry.get("applicability") != "read-only":
                problems.append(
                    f"video_colorimetry: applicability is "
                    f"{colorimetry.get('applicability')!r}, expected 'read-only' -- nothing "
                    f"selects a colour space, not even an env var"
                )
            if "values" in colorimetry:
                problems.append(
                    "video_colorimetry: publishes a 'values' list -- no colour space is "
                    "selectable, announcing a choice the binary cannot honour is a decree"
                )
            print(f"  video_colorimetry: {colorimetry.get('value')!r} "
                  f"{colorimetry.get('range')!r} {colorimetry.get('format')!r}")
        else:
            print("  video_colorimetry: not declared (absent)")

        if problems:
            print("FAIL -- manifest inventory shape violations:", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            return 1

        print("\nmanifest inventories are presence-only and well-formed")
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
