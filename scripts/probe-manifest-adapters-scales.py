#!/usr/bin/env python3
"""
Pulsar capability-manifest ADAPTERS + SCALES shape probe (#159, ADR Prism 027
Amendment 1).

The fifth manifest block answers two facts of the machine that a consumer used
to decree: which graphics adapters exist (Prism pinned `0` without ever asking),
and which output resolutions are admissible for the running canvas (Prism
assumed the canvas always was the output).

The client-side decoder already refuses to half-read either one. That is the
right lock, but it is the CONSUMER's. This probe is the EMITTER's: it asks a
live pulsar.exe what it actually puts on the wire, and -- the point of the
block -- cross-checks the declared values against the SAME libobs facts read
through a second, independent request (`GetVideoSettings`).

What we DO validate, against a running binary:
  - `graphics_adapters`, when declared, is `read-only` (nothing selects one),
    every item carries a non-empty `value` plus an integer `index`, indices are
    unique, and `active_index` -- when declared -- is one of them;
  - `output_scales`, when declared, is `boot-fixed` (PULSAR_RESOLUTION selects
    the resolution at spawn, SetVideoSettings refuses it hot), carries a whole
    `canvas`, and every admitted value is a whole {width,height} that does not
    EXCEED the canvas (a scale block is a downscale block: announcing an
    upscale would widen, and the manifest may only narrow -- §3.1);
  - the declared canvas and the admitted resolutions agree with what
    GetVideoSettings reports for the same running binary. A block that drifted
    from the values it claims to read is a constant, whatever its comment says.

What we do NOT validate:
  - WHICH adapters the host has (build/machine dependent: the CI runner
    enumerates a basic render driver, a dev box a real GPU);
  - HOW MANY scales are admitted -- one today, more the day a downscale path
    lands. The probe checks the shape and the agreement, not the cardinality.

A block the manifest does not declare at all is NOT a failure: absence is a
positive answer under §3.2 and the consumer keeps its own assumption. Only a
declared-but-malformed, or a declared-but-drifted, block fails here.

Usage (from the repo root with pulsar.exe already running):
    pip install websockets
    python scripts/probe-manifest-adapters-scales.py
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

REGIMES = ("live", "boot-fixed", "read-only")


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


def check_adapters(entry: dict, problems: list[str]) -> None:
    if entry.get("applicability") != "read-only":
        problems.append(
            f"graphics_adapters: applicability is {entry.get('applicability')!r}, expected "
            f"'read-only' -- no env var and no request selects an adapter, so any other "
            f"regime advertises a knob that does not exist"
        )

    values = entry.get("values")
    if not isinstance(values, list) or not values:
        problems.append(
            f"graphics_adapters: declared but 'values' is {values!r} -- an enumeration that "
            f"yielded nothing must publish NO entry at all"
        )
        return

    seen: set[int] = set()
    for idx, item in enumerate(values):
        if not isinstance(item, dict):
            problems.append(f"graphics_adapters[{idx}]: item is {type(item).__name__}")
            continue
        name = item.get("value")
        if not isinstance(name, str) or not name:
            problems.append(f"graphics_adapters[{idx}]: 'value' is {name!r}, expected a name")
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            problems.append(
                f"graphics_adapters[{idx}]: 'index' is {index!r} -- without the index libobs "
                f"numbers it by, the name cannot be matched against active_index"
            )
            continue
        if index in seen:
            problems.append(f"graphics_adapters[{idx}]: duplicate index {index}")
        seen.add(index)

    active = entry.get("active_index")
    if active is not None:
        if not isinstance(active, int) or isinstance(active, bool):
            problems.append(f"graphics_adapters: active_index is {active!r}, expected an int")
        elif active not in seen:
            problems.append(
                f"graphics_adapters: active_index {active} is not one of the enumerated "
                f"indices {sorted(seen)} -- the active adapter cannot be one the manifest "
                f"does not declare"
            )

    print(f"  graphics_adapters: {len(values)} adapter(s), active={active!r}, "
          f"regime {entry.get('applicability')!r}")
    for item in values:
        if isinstance(item, dict):
            print(f"    [{item.get('index')}] {item.get('value')!r}")


def whole_size(raw: object) -> tuple[int, int] | None:
    """A {width,height} pair, or None when either half is missing/unusable."""
    if not isinstance(raw, dict):
        return None
    w, h = raw.get("width"), raw.get("height")
    if not isinstance(w, int) or isinstance(w, bool) or w <= 0:
        return None
    if not isinstance(h, int) or isinstance(h, bool) or h <= 0:
        return None
    return (w, h)


def check_scales(entry: dict, video: dict, problems: list[str]) -> None:
    if entry.get("applicability") != "boot-fixed":
        problems.append(
            f"output_scales: applicability is {entry.get('applicability')!r}, expected "
            f"'boot-fixed' -- PULSAR_RESOLUTION selects it at spawn and SetVideoSettings "
            f"refuses it hot"
        )

    canvas = whole_size(entry.get("canvas"))
    if canvas is None:
        problems.append(
            f"output_scales: 'canvas' is {entry.get('canvas')!r} -- a scale is meaningless "
            f"without the canvas it is relative to; a half-read canvas must be omitted, and "
            f"omitting it means omitting the block"
        )
        return

    values = entry.get("values")
    if not isinstance(values, list) or not values:
        problems.append(
            f"output_scales: declared but 'values' is {values!r} -- a canvas with no "
            f"admissible output resolution is not a state this binary can be in"
        )
        return

    admitted: list[tuple[int, int]] = []
    for idx, item in enumerate(values):
        size = whole_size(item)
        if size is None:
            problems.append(
                f"output_scales[{idx}]: {item!r} carries no usable width/height"
            )
            continue
        w, h = size
        if w > canvas[0] or h > canvas[1]:
            problems.append(
                f"output_scales[{idx}]: {w}x{h} exceeds the canvas {canvas[0]}x{canvas[1]} -- "
                f"the manifest may only narrow (§3.1), never announce an upscale"
            )
        scale = item.get("scale")
        if scale is not None:
            if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
                problems.append(f"output_scales[{idx}]: 'scale' is {scale!r}")
            elif abs(scale * canvas[0] - w) > 0.5 or abs(scale * canvas[1] - h) > 0.5:
                problems.append(
                    f"output_scales[{idx}]: 'scale' {scale} does not reproduce {w}x{h} from "
                    f"the canvas {canvas[0]}x{canvas[1]}"
                )
        admitted.append(size)

    # The agreement check: same binary, same libobs, second request. A block
    # that drifted from the values it claims to read is a constant.
    vw, vh = video.get("width"), video.get("height")
    if isinstance(vw, int) and isinstance(vh, int) and vw > 0 and vh > 0:
        if (vw, vh) not in admitted:
            problems.append(
                f"output_scales: GetVideoSettings reports {vw}x{vh} in force, which is not "
                f"among the admitted resolutions {admitted} -- the block cannot exclude the "
                f"resolution the binary is actually running"
            )
    else:
        print("  (GetVideoSettings reported no usable width/height; agreement check skipped)")

    print(f"  output_scales: canvas {canvas[0]}x{canvas[1]}, "
          f"{len(admitted)} admitted {admitted}, regime {entry.get('applicability')!r}")


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
        video = await vendor_call(inbox, ws, "GetVideoSettings", "getvideo")
        if "_error" in video:
            print(f"FAIL GetVideoSettings: {video['_error']}", file=sys.stderr)
            return 1

        block = caps.get("capabilities")
        if not isinstance(block, dict):
            print(f"FAIL 'capabilities' is {type(block).__name__}, expected an object (#141)",
                  file=sys.stderr)
            return 1

        problems: list[str] = []

        adapters = block.get("graphics_adapters")
        if adapters is None:
            print("  graphics_adapters: not declared (absent -- consumer keeps its assumption)")
        elif not isinstance(adapters, dict):
            problems.append(f"graphics_adapters: entry is {type(adapters).__name__}")
        else:
            check_adapters(adapters, problems)

        scales = block.get("output_scales")
        if scales is None:
            print("  output_scales: not declared (absent -- consumer keeps its assumption)")
        elif not isinstance(scales, dict):
            problems.append(f"output_scales: entry is {type(scales).__name__}")
        else:
            check_scales(scales, video, problems)

        if problems:
            print("FAIL -- adapters/scales block violations:", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            return 1

        print("\nadapters + scales are well-formed and agree with the running binary")
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
