#!/usr/bin/env python3
"""
Pulsar monitoring-device selection probe (#173, ADR Prism 029 §3.6 manque A1).

Binding "Default"/"default" at boot made monitoring *work*; it did not let an
operator choose WHICH output sounds. `pulsar:GetMonitoringDeviceList` and
`pulsar:SetMonitoringDevice` are that choice. This probe asks a live pulsar.exe
whether they are really there and really refuse what they say they refuse --
the lesson of ADR 028 §3.5 / B6 being that a registered request is not a
capability.

What we DO validate, against a running binary:
  - the list answers with an explicit `available` and a well-formed `devices`
    list -- non-empty ids, non-empty names, no duplicate id -- and, when
    monitoring is available, contains libobs's own dynamic `"default"`;
  - the manifest agrees with it: `audio_monitoring.device_selectable` is the
    same fact as the list's `available`, read through a second, independent
    request. A manifest that drifted from the request it advertises would let
    Prism offer a selector the binary does not honour (ADR 027 §3.1);
  - a device id the machine does not enumerate is refused BY NAME, and leaves
    the device in force untouched. libobs stores any non-empty {name,id} pair
    and returns true, so an unchecked id would be accepted into silence -- the
    exact "succès muet" the issue forbids;
  - a legitimate selection is reported only after read-back: the device the
    call returns is the device the list then names as active.

What we do NOT validate:
  - that sound actually comes out of the selected endpoint. No probe can hear;
    that is the manual criterion of #173. Read-back proves the bind took, not
    that it is audible.
  - WHICH devices the host has. A CI runner enumerates no render endpoint at
    all and answers the single `"default"` entry; a régie box answers several.
    The probe checks the shape, the agreement and the refusals, never the
    cardinality -- so it round-trips between two real devices only when the
    machine has two, and settles for re-selecting the active one otherwise.

Usage (from the repo root with pulsar.exe already running):
    pip install websockets
    python scripts/probe-monitoring-devices.py
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

BOGUS_ID = "{pulsar-probe}.{no-such-endpoint}"


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


async def vendor_call(inbox: Inbox, ws, request_type: str, request_id: str,
                      data: dict | None = None) -> dict:
    payload: dict = {"vendorName": "pulsar", "requestType": request_type}
    if data is not None:
        payload["requestData"] = data
    resp = await request(inbox, ws, "CallVendorRequest", request_id, payload)
    if not resp["requestStatus"]["result"]:
        return {"_transport_error": resp["requestStatus"]}
    rd = resp.get("responseData") or {}
    return rd.get("responseData") or {}


def check_list_shape(listing: dict, problems: list[str]) -> list[dict]:
    """Validates the list envelope; returns the items worth using downstream."""
    available = listing.get("available")
    if not isinstance(available, bool):
        problems.append(
            f"GetMonitoringDeviceList: 'available' is {available!r} -- the request must state "
            f"the platform fact explicitly, never leave it to be inferred"
        )

    devices = listing.get("devices")
    if not isinstance(devices, list):
        problems.append(
            f"GetMonitoringDeviceList: 'devices' is {devices!r}, expected a list (empty is a "
            f"legitimate answer, absent is not)"
        )
        return []

    seen: set[str] = set()
    for idx, item in enumerate(devices):
        if not isinstance(item, dict):
            problems.append(f"devices[{idx}]: item is {type(item).__name__}")
            continue
        did = item.get("id")
        if not isinstance(did, str) or not did:
            problems.append(
                f"devices[{idx}]: 'id' is {did!r} -- an item whose id cannot be sent back to "
                f"SetMonitoringDevice is an entry no selector can use"
            )
            continue
        if not isinstance(item.get("name"), str) or not item.get("name"):
            problems.append(f"devices[{idx}]: 'name' is {item.get('name')!r}, expected a label")
        if did in seen:
            problems.append(f"devices[{idx}]: duplicate id {did!r}")
        seen.add(did)

    if available is True and "default" not in seen:
        problems.append(
            "GetMonitoringDeviceList: monitoring is available but the list does not carry the "
            "'default' id -- that is the device pulsar-headless binds at boot, so a list "
            "omitting it cannot name the device already in force"
        )

    return [d for d in devices if isinstance(d, dict) and isinstance(d.get("id"), str) and d["id"]]


def check_manifest_agreement(caps: dict, listing: dict, problems: list[str]) -> None:
    entry = (caps.get("capabilities") or {}).get("audio_monitoring")
    if not isinstance(entry, dict):
        problems.append(
            "GetCapabilities: no 'audio_monitoring' entry -- the manifest is what gates the "
            "Prism selector, so the requests cannot exist without it declaring them"
        )
        return

    selectable = entry.get("device_selectable")
    if not isinstance(selectable, bool):
        problems.append(
            f"audio_monitoring.device_selectable is {selectable!r} -- Prism may only offer a "
            f"selector on an explicit true, so the field must be stated, not omitted"
        )
        return

    if selectable != listing.get("available"):
        problems.append(
            f"audio_monitoring.device_selectable is {selectable!r} while "
            f"GetMonitoringDeviceList reports available={listing.get('available')!r} -- both "
            f"read obs_audio_monitoring_available() on the same instance and cannot disagree"
        )

    if selectable and entry.get("applicability") != "live":
        problems.append(
            f"audio_monitoring: applicability is {entry.get('applicability')!r} while the "
            f"device is selectable -- a write path with read-back IS the 'live' regime "
            f"(ADR 027 §3.2)"
        )


async def probe(url: str, password: str) -> int:
    problems: list[str] = []
    async with websockets.connect(url, max_size=64 * 1024 * 1024) as ws:
        hello = json.loads(await ws.recv())
        auth = hello["d"].get("authentication")
        identify: dict = {"rpcVersion": 1}
        if auth:
            identify["authentication"] = compute_auth(password, auth["salt"], auth["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify}))
        await ws.recv()  # Identified

        inbox = Inbox()

        listing = await vendor_call(inbox, ws, "GetMonitoringDeviceList", "list")
        if "_transport_error" in listing:
            print(f"FAIL -- GetMonitoringDeviceList is not registered: "
                  f"{listing['_transport_error']}", file=sys.stderr)
            return 1
        if listing.get("error"):
            print(f"FAIL -- GetMonitoringDeviceList: {listing['error']}", file=sys.stderr)
            return 1

        devices = check_list_shape(listing, problems)
        active = listing.get("active_device_id")
        print(f"  devices: {[d['id'] for d in devices]}")
        print(f"  active: {active!r} ({listing.get('active_device_name')!r}), "
              f"available={listing.get('available')!r}")

        caps = await vendor_call(inbox, ws, "GetCapabilities", "caps")
        if "_transport_error" in caps:
            problems.append("GetCapabilities did not answer; manifest agreement unchecked")
        else:
            check_manifest_agreement(caps, listing, problems)

        # A device the machine does not have must be refused by name, and must
        # not disturb the one in force.
        bogus = await vendor_call(inbox, ws, "SetMonitoringDevice", "bogus",
                                  {"device_id": BOGUS_ID})
        if not isinstance(bogus.get("error"), str) or not bogus["error"]:
            problems.append(
                f"SetMonitoringDevice({BOGUS_ID!r}) answered {bogus!r} -- an id no endpoint "
                f"carries must produce a NAMED failure; libobs stores any pair and returns "
                f"true, so silence here is a bind into silence"
            )
        elif BOGUS_ID not in bogus["error"]:
            problems.append(
                f"SetMonitoringDevice refusal does not name the id it refused: "
                f"{bogus['error']!r}"
            )
        if bogus.get("changed"):
            problems.append("SetMonitoringDevice reported changed=true on a refused id")

        after_bogus = await vendor_call(inbox, ws, "GetMonitoringDeviceList", "list2")
        if after_bogus.get("active_device_id") != active:
            problems.append(
                f"a refused SetMonitoringDevice moved the device in force from {active!r} to "
                f"{after_bogus.get('active_device_id')!r} -- a refusal must change nothing"
            )

        # A legitimate selection. Prefer a device that is NOT the active one, so
        # the read-back proves a real move; on a machine with a single entry it
        # re-selects the active one, which still exercises the write path.
        target = next((d for d in devices if d["id"] != active), None)
        if target is None:
            target = next((d for d in devices if d["id"] == active), devices[0] if devices else None)
        if target is None:
            problems.append("no selectable device at all -- nothing to prove the write path on")
        else:
            wrote = await vendor_call(inbox, ws, "SetMonitoringDevice", "set",
                                      {"device_id": target["id"]})
            if wrote.get("error"):
                problems.append(
                    f"SetMonitoringDevice({target['id']!r}) refused an id it had just "
                    f"enumerated: {wrote['error']}"
                )
            elif wrote.get("device_id") != target["id"]:
                problems.append(
                    f"SetMonitoringDevice returned device_id {wrote.get('device_id')!r} for "
                    f"{target['id']!r} -- the answer must be the READ-BACK, not the request"
                )
            else:
                confirm = await vendor_call(inbox, ws, "GetMonitoringDeviceList", "list3")
                if confirm.get("active_device_id") != target["id"]:
                    problems.append(
                        f"after selecting {target['id']!r} the list still reports "
                        f"{confirm.get('active_device_id')!r} in force -- the two requests "
                        f"disagree about the same libobs state"
                    )
                else:
                    print(f"  selected {target['id']!r} -> read back as in force")

            # Leave the instance as we found it: the probes after this one share
            # the process, and a régie device left selected is a side effect.
            if active and target["id"] != active:
                await vendor_call(inbox, ws, "SetMonitoringDevice", "restore",
                                  {"device_id": active})

        if problems:
            print("FAIL -- monitoring device selection violations:", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            return 1

        print("\nmonitoring device selection is registered, refuses by name and reads back")
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
