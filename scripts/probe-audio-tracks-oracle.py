#!/usr/bin/env python3
"""
Pulsar audio-track oracle probe (#157, ADR Prism 028 §3.2 / 026 §3.2).

Regression guard for a request that answered Success() without verifying
its effect -- and, just as importantly, for VERIFYING IT AGAINST THE
WRONG ORACLE.

THE BUG IT PINS
  SetInputAudioTracks called obs_source_set_audio_mixers() and returned
  Success(). The mixer bit really is written on the input, so the client
  was told "track 4 is on" while the streaming output carries a single
  audio encoder, at slot 0: nothing on earth consumes track 4.

WHY THE OBVIOUS TEST WOULD MISS IT
  Re-reading the INPUT (GetInputAudioTracks) after the call answers
  "enabled" in both the healthy and the broken case -- libobs even hands
  every fresh source audio_mixers = 0xFF, so an input read reports all
  six tracks on before anyone asks for anything. An input-side oracle
  therefore CONFIRMS the lie. The oracle is the OUTPUT: the encoder
  slots actually bound, which pulsar-multi-stream already publishes as
  the `audio_tracks.bound` capability.

WHAT THIS PROVES
  1. The discriminating case (criterion 1): a track the INPUT reports as
     enabled -- so an input-reading oracle would call the request a
     success -- is REFUSED, because no output encoder consumes it. Both
     halves are asserted here; a probe that only re-read the input could
     not tell this case from a working one, and this file fails loudly
     if the case does not exist on the build under test rather than
     passing vacuously.
  2. The refusal carries the cause READ off libobs (criterion 2): the
     status is InvalidResourceState (604) and the comment names the
     tracks requested and the tracks actually bound to the output.
  3. Non-regression (criterion 3): a track that IS backed by an output
     encoder still answers success, and so does disabling an unbacked
     track -- turning audio OFF is honest whatever the output carries.
  4. Cross-module agreement: the handler's own read of the output slots
     (pulsar-websocket) and `audio_tracks.bound` (pulsar-multi-stream)
     count the same slots.

WHAT THIS DOES NOT PROVE
  Nothing about real multi-track output. Closing the lie binds no extra
  encoder to the output; that is a separate capability (ADR 028 §3.5).
  This probe asserts the SHAPE of the refusal, not that six tracks could
  ever be carried.

LICENSE INVARIANT (LICENSE-INVARIANTS.md): talks to pulsar.exe over the
obs-websocket process boundary ONLY -- no FFI, no ctypes, no
LoadLibrary of obs.dll.

Usage (from the repo root with pulsar.exe already running):
    pip install websockets
    python scripts/probe-audio-tracks-oracle.py
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

SCENE_NAME = "probe-audio-tracks-oracle"
INPUT_NAME = "probe-audio-tracks-oracle-input"
# An input whose kind carries OBS_SOURCE_AUDIO but binds no device: the
# probe must not depend on the CI runner owning a sound card.
INPUT_KIND = "ffmpeg_source"

# v5 status reused by the refusal -- no new enum (PROTOCOL.md).
INVALID_RESOURCE_STATE = 604


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


async def vendor_call(inbox: Inbox, ws, request_type: str, request_id: str) -> dict:
    resp = await request(inbox, ws, "CallVendorRequest", request_id,
                         {"vendorName": "pulsar", "requestType": request_type})
    if not resp["requestStatus"]["result"]:
        return {"_error": resp["requestStatus"]}
    rd = resp.get("responseData") or {}
    return rd.get("responseData") or {}


async def get_tracks(inbox: Inbox, ws, request_id: str) -> dict[int, bool]:
    r = await request(inbox, ws, "GetInputAudioTracks", request_id,
                      {"inputName": INPUT_NAME})
    if not r["requestStatus"]["result"]:
        raise RuntimeError(f"GetInputAudioTracks failed: {r['requestStatus']}")
    return {int(k): bool(v) for k, v in r["responseData"]["inputAudioTracks"].items()}


async def set_track(inbox: Inbox, ws, request_id: str, track: int, enabled: bool) -> dict:
    r = await request(inbox, ws, "SetInputAudioTracks", request_id,
                      {"inputName": INPUT_NAME,
                       "inputAudioTracks": {str(track): enabled}})
    return r["requestStatus"]


async def run(inbox: Inbox, ws) -> int:
    # --- The published oracle, read independently of the handler. --------
    caps = await vendor_call(inbox, ws, "GetCapabilities", "getcaps")
    if "_error" in caps:
        print(f"FAIL GetCapabilities: {caps['_error']}", file=sys.stderr)
        return 1
    entry = (caps.get("capabilities") or {}).get("audio_tracks") or {}
    count = entry.get("count")
    bound_count = entry.get("bound")
    if not isinstance(count, int) or count < 1:
        print(f"FAIL audio_tracks.count is {count!r}, expected an int >= 1",
              file=sys.stderr)
        return 1
    if not isinstance(bound_count, int):
        print("FAIL audio_tracks.bound absent -- the streaming output could not "
              "be read, so this probe cannot state anything about the oracle",
              file=sys.stderr)
        return 1
    print(f"capability audio_tracks: count={count} bound={bound_count}")

    # --- The input under test. ------------------------------------------
    tracks = await get_tracks(inbox, ws, "tracks-initial")
    print(f"input tracks as libobs reports them: {tracks}")

    # --- Per-track verdict from the request itself. ----------------------
    accepted: list[int] = []
    refused: dict[int, dict] = {}
    for track in range(1, count + 1):
        status = await set_track(inbox, ws, f"set-{track}", track, True)
        if status["result"]:
            accepted.append(track)
        else:
            refused[track] = status
    print(f"SetInputAudioTracks accepted: {accepted}")
    print(f"SetInputAudioTracks refused:  {sorted(refused)}")

    problems: list[str] = []

    # Criterion 3 -- a backed track still answers success.
    if not accepted:
        problems.append(
            "no track was accepted at all: the streaming output reports "
            f"bound={bound_count}, so at least one enable must succeed"
        )

    # Cross-module agreement (#157): the handler reads the same slots the
    # capability counts.
    if len(accepted) != bound_count:
        problems.append(
            f"the request accepted {len(accepted)} track(s) {accepted} while "
            f"audio_tracks.bound reports {bound_count} -- pulsar-websocket and "
            "pulsar-multi-stream are not reading the same output slots"
        )

    # Criterion 1 -- the discriminating case must EXIST on this build.
    if not refused:
        problems.append(
            f"every track 1..{count} was accepted: there is no track that the "
            "output does not carry, so this probe cannot distinguish an "
            "output-side oracle from an input-side one. Either the build now "
            "binds every slot (then this probe needs rewriting), or the "
            "verification is gone (#157 regression)"
        )

    for track, status in sorted(refused.items()):
        # Criterion 2 -- the cause is READ, never generic.
        if status.get("code") != INVALID_RESOURCE_STATE:
            problems.append(
                f"track {track}: refused with code {status.get('code')}, "
                f"expected {INVALID_RESOURCE_STATE} (InvalidResourceState)"
            )
        comment = status.get("comment") or ""
        if str(track) not in comment:
            problems.append(f"track {track}: comment does not name the track: {comment!r}")
        if "bound" not in comment.lower() or "streaming output" not in comment.lower():
            problems.append(
                f"track {track}: comment does not name what was read off the "
                f"streaming output -- generic failure message: {comment!r}"
            )
        print(f"track {track} refusal: [{status.get('code')}] {comment}")

    # Criterion 1, the half that makes this probe worth writing: the very
    # tracks the request REFUSED are reported ENABLED by the input. An
    # oracle that re-read the input would have called those calls a
    # success. Assert it, so a future rewrite towards the input oracle
    # turns this file red instead of green.
    tracks_after = await get_tracks(inbox, ws, "tracks-after")
    blind = [t for t in sorted(refused) if tracks_after.get(t)]
    if refused and not blind:
        problems.append(
            f"the refused tracks {sorted(refused)} are reported disabled on the "
            f"input ({tracks_after}) -- an input-side oracle would ALSO have "
            "called them a failure, so this run does not prove the output is "
            "the oracle. The discriminating case was not exercised"
        )
    elif blind:
        print(f"input reports the REFUSED tracks {blind} as enabled -- an "
              "input-side oracle would have answered success for exactly the "
              "calls the output oracle refused")

    # Criterion 3 bis -- turning an unbacked track OFF stays honest.
    for track in sorted(refused):
        status = await set_track(inbox, ws, f"unset-{track}", track, False)
        if not status["result"]:
            problems.append(
                f"track {track}: disabling an unbacked track was refused "
                f"({status.get('code')}: {status.get('comment')}) -- only "
                "enabling asserts an effect the output cannot deliver"
            )
        break  # one is enough; the rule is per-request, not per-track

    # Criterion 3 ter -- a backed track is still settable after all this.
    if accepted:
        status = await set_track(inbox, ws, "reset-bound", accepted[0], True)
        if not status["result"]:
            problems.append(
                f"track {accepted[0]} is bound to the output but was refused "
                f"({status.get('code')}: {status.get('comment')})"
            )

    if problems:
        print("\nFAIL:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print("\nSetInputAudioTracks is judged by the output, not by the input")
    return 0


async def probe(url: str, password: str) -> int:
    print(f"connecting: {url}")
    async with websockets.connect(url, subprotocols=["obswebsocket.json"]) as ws:
        hello = json.loads(await ws.recv())
        identify_d: dict = {"rpcVersion": hello["d"]["rpcVersion"],
                            "eventSubscriptions": 0}
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        await ws.recv()
        print("identified")

        inbox = Inbox()

        r = await request(inbox, ws, "CreateScene", "create-scene",
                          {"sceneName": SCENE_NAME})
        if not r["requestStatus"]["result"]:
            print(f"FAIL CreateScene: {r['requestStatus']}", file=sys.stderr)
            return 1
        try:
            r = await request(inbox, ws, "CreateInput", "create-input", {
                "sceneName": SCENE_NAME,
                "inputName": INPUT_NAME,
                "inputKind": INPUT_KIND,
                "inputSettings": {},
            })
            if not r["requestStatus"]["result"]:
                print(f"FAIL CreateInput({INPUT_KIND}): {r['requestStatus']}",
                      file=sys.stderr)
                return 1
            try:
                return await run(inbox, ws)
            finally:
                await request(inbox, ws, "RemoveInput", "remove-input",
                              {"inputName": INPUT_NAME})
        finally:
            # Leave the instance exactly as it was found -- the offline
            # suite shares one pulsar.exe across every connect-only probe.
            await request(inbox, ws, "RemoveScene", "remove-scene",
                          {"sceneName": SCENE_NAME})


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
