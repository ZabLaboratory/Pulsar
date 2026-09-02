#!/usr/bin/env python3
"""
Pulsar v5 capability contract probe (Probe/B7, gated by Forge/#121).

Thinker's report on Pulsar (`docs/adr` fanout, item B7) found that nothing
in the repo distinguishes a v5 request that is *served* (answers on the
wire, `requestStatus.result == true`) from one that is *functional* (its
promised effect is observable). `docs/PROTOCOL.md` advertises "137 v5
request types" but no test walked that surface end to end.

This probe is a CONTRACT test, not a smoke test. For every request it
drives it does three things:
  1. call the request;
  2. independently re-query the server for the state the request claims
     to change (a *different* request than the one under test, wherever
     one exists);
  3. classify the outcome:
       - OK               : requestStatus.result == True AND the
                            independent re-query shows the promised state
                            change.
       - ERROR_EXPLICIT   : requestStatus.result == False with a comment
                            (or a comparable clean refusal). Correct
                            behaviour for an unsupported/misconfigured
                            capability -- NOT a failure of this probe.
       - OK_NO_EFFECT     : requestStatus.result == True but the
                            independent re-query shows NO observable
                            change. This is the B7 failure mode.

Any OK_NO_EFFECT classification fails the probe UNLESS the request is
listed in KNOWN_OK_NO_EFFECT below, with a date and a reason. That list is
for capabilities where the no-effect behaviour is an already-documented,
deliberate protocol tradeoff (cf. PROTOCOL.md) -- it is NOT a place to
silence new findings.

CI GATE (ADR Prism 026 §3.3 palier 3, issue #121)
-------------------------------------------------
This probe is wired into scripts/run-probes.ps1 (Phase 1g), which CTest
runs as `pulsar-offline-probes` in the `offline probe suite (CTest)` job.
It is a BLOCKING gate: no `continue-on-error`, no tolerated red. That is
only defensible because palier 2 landed first -- #117 (replay buffer
wired), #118 (dead stub documented), #119 (scene mirror removed), #120 +
#127 (no Success() without a verified effect) -- so the probe is green on
its perimeter before it is allowed to block anything.

The same discipline applied to the three defects THIS probe found and routed
out rather than gated: #129 (RemoveInput removed nothing), #130 (PauseRecord
at 0 bytes wedged the muxer) and #131 (the v5 StartStream path was never
bound to its service). All three are fixed, so all three are now gated here --
the fix landed first, the gate widened after.

The gate is BY TIERS, not all-or-nothing: a request family this probe does
not cover is not a failure, it is measured ignorance. The covered set is
frozen in the versioned artefact `scripts/contracts/capability-coverage.json`
and cross-checked at the end of every run in BOTH directions -- a covered
subject that stops being driven fails the probe, and a driven subject
missing from the artefact fails it too. Widening the list is a separate
job; narrowing it is a visible diff that must fail review.

LICENSE INVARIANT (LICENSE-INVARIANTS.md, ADR 008 §3.1): WebSocket process
boundary only, same as every other scripts/probe-*.py. No FFI, no ctypes,
no native import.

Usage (from the repo root, against the built rundir):
    pip install websockets
    python scripts/probe-capability-contract.py
    python scripts/probe-capability-contract.py --exe /path/to/pulsar.exe
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from enum import Enum
from typing import Any, Optional

try:
    import websockets
except ImportError:
    print("error: pip install websockets (pure WS client -- no native deps)")
    sys.exit(2)


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    REPO_ROOT
    / "upstream"
    / "build_x64"
    / "rundir"
    / "RelWithDebInfo"
    / "bin"
    / "64bit"
    / "pulsar.exe"
)
COVERAGE_ARTEFACT = REPO_ROOT / "scripts" / "contracts" / "capability-coverage.json"

READY_TIMEOUT_S = 60.0
READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
SHUTDOWN_GRACE_S = 8.0

# Ring-fill before SaveReplayBuffer, aligned with probe-replay.py. The
# FILE-level proof (real mp4, h264+aac, on disk, under PULSAR_RECORD_DIR) stays
# probe-replay.py's job (#117); here we only need the effect to be observable.
REPLAY_FILL_S = 5.0

# Output stops go through a real muxer flush (ffmpeg_muxer) -- the probe waits,
# the request does not. Keep the recovery budget separate from the server's
# short response window: a loaded CI runner may need several seconds to drain
# the shared record/replay encoders after a truthful 702 Pending response.
STOP_ATTEMPTS = 60
PENDING_STOP_SETTLE_S = 45.0

# --------------------------------------------------------------------------
# Known, DATED, JUSTIFIED exceptions to "OK_NO_EFFECT fails the probe".
#
# Each entry is a decision hole, not a fix taken silently. Routed to
# Atlas/Eleven.
# --------------------------------------------------------------------------
KNOWN_OK_NO_EFFECT: dict[str, str] = {
    # PROTOCOL.md documents the bare "no service configured" case as intentional
    # wire behaviour, and since #120 the request answers it with an explicit
    # refusal -- the correct branch, never OK_NO_EFFECT. This entry is kept as a
    # name to point at if a future run ever walks the documented bare case.
    # 2026-07-27 (#131): the open Atlas question this used to carry is CLOSED --
    # the v5 path is wired (obs_output_set_service before obs_output_start), so
    # the probe now drives the CONFIGURED case and gates it on the STARTING
    # event. Nothing here is excused today; the dict is a tripwire, not a mute.
    "StartStream:no-service-configured": (
        "2026-07-26 Probe/B7, closed 2026-07-27 by #131 -- documented in "
        "PROTOCOL.md as intentional v5-baseline behaviour, upstream-shaped "
        "(obs-websocket StartStream mirrors obs_output_start's fire-and-forget "
        "contract). Unused: no verdict is excused through this key."
    ),
}


class Verdict(str, Enum):
    OK = "ok"
    ERROR_EXPLICIT = "erreur explicite"
    OK_NO_EFFECT = "ok-mais-sans-effet"


@dataclasses.dataclass
class RequestVerdict:
    family: str
    request_type: str
    verdict: Verdict
    detail: str
    excused: Optional[str] = None  # KNOWN_OK_NO_EFFECT reason, if any


RESULTS: list[RequestVerdict] = []


def record(family: str, request_type: str, verdict: Verdict, detail: str, excuse_key: str | None = None) -> None:
    excused = KNOWN_OK_NO_EFFECT.get(excuse_key) if excuse_key else None
    RESULTS.append(RequestVerdict(family, request_type, verdict, detail, excused))
    tag = verdict.value.upper()
    print(f"  [{tag}] {family}/{request_type}: {detail}")
    if excused:
        print(f"           (excused: {excused})")


# --------------------------------------------------------------------------
# Process management -- same shape as probe-websocket.py / probe-replay.py.
# --------------------------------------------------------------------------
def pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


class PulsarProcess:
    def __init__(self, exe: pathlib.Path, port: int, password: str, record_dir: pathlib.Path) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.record_dir = record_dir
        self.proc = None
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None
        self._pump_thread: Optional[threading.Thread] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        # Isolated record dir: the Record + ReplayBuffer families below write
        # real files, and they must not land in the shared rundir (or in
        # whatever a previous probe left behind).
        env["PULSAR_RECORD_DIR"] = str(self.record_dir)

        creationflags = 0
        if os.name == "nt":
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        self.proc = subprocess.Popen(
            [str(self.exe)],
            cwd=str(self.exe.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        self._pump_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._pump_thread.start()

    def _pump_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            self._lines.append(line)
            m = READY_RE.match(line)
            if m is not None and not self._ready_event.is_set():
                self._ready_match = m
                self._ready_event.set()

    def wait_ready(self, timeout: float) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        while True:
            if self._ready_event.wait(timeout=0.2):
                m = self._ready_match
                assert m is not None
                return m.group(1), m.group(2)
            assert self.proc is not None
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"pulsar.exe exited (code {self.proc.returncode}) before READY.\n"
                    + self._diag()
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"pulsar.exe did not signal READY within {timeout:.0f}s.\n" + self._diag()
                )

    def _diag(self) -> str:
        tail = self._lines[-40:]
        body = "\n".join(f"  | {ln}" for ln in tail) if tail else "  | (no output captured)"
        return f"--- pulsar stdout/stderr (last {len(tail)} lines) ---\n{body}"

    def shutdown(self, grace: float = SHUTDOWN_GRACE_S) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=grace)
            return
        except Exception:
            pass
        try:
            self.proc.kill()
            self.proc.wait(timeout=grace)
        except Exception:
            pass


class PulsarWs:
    """Thin v5 request wrapper: handshake once, then `.req(type, data)`."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self._req_counter = 0
        # Issue #131: some effects are only observable as EVENTS, never as a
        # re-query (an output that libobs accepted but whose connect thread is
        # still in flight reads back outputActive:false the whole time). The
        # probe already identifies with eventSubscriptions 0x7FF -- it just
        # threw the events away. Buffer them instead.
        self.events: list[dict] = []
        # requestStatus of the LAST req() call, so a driver can gate on the
        # status CODE (e.g. 604 InvalidResourceState) and not only on `comment`.
        self.last_status: dict = {}

    @classmethod
    async def connect(cls, url: str, password: str) -> "PulsarWs":
        ws = await websockets.connect(url, subprotocols=["obswebsocket.json"], open_timeout=10)
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert hello.get("op") == 0, f"expected Hello, got {hello}"
        rpc = hello["d"]["rpcVersion"]
        identify_d: dict = {"rpcVersion": rpc, "eventSubscriptions": 0x7FF}
        if "authentication" in hello["d"]:
            auth = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(password, auth["salt"], auth["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert ident.get("op") == 2, f"expected Identified, got {ident}"
        return cls(ws)

    async def req(self, request_type: str, request_data: dict | None = None, timeout: float = 15) -> tuple[bool, dict, str | None]:
        """Returns (result, responseData, comment)."""
        self._req_counter += 1
        rid = f"contract-{self._req_counter}"
        payload: dict[str, Any] = {"requestType": request_type, "requestId": rid}
        if request_data is not None:
            payload["requestData"] = request_data
        await self.ws.send(json.dumps({"op": 6, "d": payload}))
        while True:
            resp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            if resp.get("op") == 5:
                self.events.append(resp["d"])  # keep, do not drop (issue #131)
                continue
            if resp.get("op") != 7:
                continue  # any other envelope interleaved on the same socket
            d = resp["d"]
            if d.get("requestId") != rid:
                continue
            status = d["requestStatus"]
            self.last_status = status
            return status.get("result", False), d.get("responseData", {}), status.get("comment")

    # ---- event side of the wire (issue #131) ---------------------------
    def clear_events(self) -> None:
        self.events.clear()

    def event_states(self, event_type: str) -> list[str]:
        """Every `outputState` seen so far for `event_type`, in order."""
        return [
            e.get("eventData", {}).get("outputState")
            for e in self.events
            if e.get("eventType") == event_type
        ]

    def _seen(self, event_type: str, output_state: str | None) -> bool:
        for e in self.events:
            if e.get("eventType") != event_type:
                continue
            if output_state is None or e.get("eventData", {}).get("outputState") == output_state:
                return True
        return False

    async def wait_for_event(self, event_type: str, output_state: str | None = None, timeout: float = 5.0) -> bool:
        """Read the socket until the event shows up, or the budget runs out.

        The PROBE waits, the server does not -- same discipline as `poll`."""
        if self._seen(event_type, output_state):
            return True
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            except (asyncio.TimeoutError, TimeoutError):
                return False
            msg = json.loads(raw)
            if msg.get("op") != 5:
                continue
            self.events.append(msg["d"])
            if self._seen(event_type, output_state):
                return True

    async def poll(self, request_type: str, key: str, want: Any, attempts: int = 20, delay: float = 0.25) -> Any:
        """Re-query until `responseData[key] == want`, or give up and return
        the last value seen. Never blocks the request under test -- this is
        the PROBE waiting, not the server."""
        last: Any = None
        for _ in range(attempts):
            _, data, _ = await self.req(request_type)
            last = data.get(key)
            if last == want:
                return last
            await asyncio.sleep(delay)
        return last

    async def close(self) -> None:
        await self.ws.close(code=1000, reason="contract probe complete")


# --------------------------------------------------------------------------
# Family drivers. Each returns nothing (unless it hands a fixture to the
# next one); they call `record()` directly so a family can emit more than
# one verdict.
# --------------------------------------------------------------------------
SCENE_A = "ProbeContractSceneA"
SCENE_B = "ProbeContractSceneB"
COLOR_SRC = "ProbeContractColorSource"
FILTER_NAME = "ProbeContractFilter"


async def drive_general(ws: PulsarWs) -> None:
    ok, data, comment = await ws.req("GetVersion")
    n = len(data.get("availableRequests", []))
    if ok and n >= 100 and ("obsVersion" in data or "obsWebSocketVersion" in data):
        record("General", "GetVersion", Verdict.OK, f"{n} availableRequests advertised")
    elif ok:
        record("General", "GetVersion", Verdict.OK_NO_EFFECT, f"result=True but only {n} requests / missing version fields: {data}")
    else:
        record("General", "GetVersion", Verdict.ERROR_EXPLICIT, f"comment={comment}")

    ok, data, comment = await ws.req("GetStats")
    cpu = data.get("cpuUsage")
    mem = data.get("memoryUsage")
    if ok and isinstance(cpu, (int, float)) and isinstance(mem, (int, float)) and mem > 0:
        record("General", "GetStats", Verdict.OK, f"cpuUsage={cpu} memoryUsage={mem}")
    elif ok:
        record("General", "GetStats", Verdict.OK_NO_EFFECT, f"result=True but non-plausible stats: {data}")
    else:
        record("General", "GetStats", Verdict.ERROR_EXPLICIT, f"comment={comment}")


async def scene_names(ws: PulsarWs) -> set[str]:
    _, data, _ = await ws.req("GetSceneList")
    return {s["sceneName"] for s in data.get("scenes", [])}


async def drive_scenes_and_inputs(ws: PulsarWs) -> None:
    """Calibration family: scenes + inputs, already used by Prism in prod.

    Historical note (Probe/B7, 2026-07-26): CreateScene used to come back
    OK_NO_EFFECT here. The root cause was not on the wire -- GetSceneList
    walked a stub-side `scenes` vector that was populated at collection load
    and never appended to when a scene was created straight through libobs,
    so a scene could be created, even driven live, yet never show up in any
    listing. Removed by #119 (ADR Prism 026 §3.1): GetSceneList now
    enumerates libobs. This driver is the regression gate on that.
    """
    ok, _, comment = await ws.req("CreateScene", {"sceneName": SCENE_A})
    ok2, _, comment2 = await ws.req("CreateScene", {"sceneName": SCENE_B})
    names = await scene_names(ws)
    if ok and ok2 and SCENE_A in names and SCENE_B in names:
        record("Scenes", "CreateScene", Verdict.OK, f"both scenes present in GetSceneList ({len(names)} total)")
    elif not ok or not ok2:
        record("Scenes", "CreateScene", Verdict.ERROR_EXPLICIT, f"comment={comment or comment2}")
    else:
        record("Scenes", "CreateScene", Verdict.OK_NO_EFFECT, f"result=True but scene(s) absent from GetSceneList: {names}")

    ok, _, comment = await ws.req("SetCurrentProgramScene", {"sceneName": SCENE_A})
    ok_g, cur, _ = await ws.req("GetCurrentProgramScene")
    if ok and ok_g and cur.get("sceneName") == SCENE_A:
        record("Scenes", "SetCurrentProgramScene", Verdict.OK, f"GetCurrentProgramScene reflects {SCENE_A}")
    elif not ok:
        record("Scenes", "SetCurrentProgramScene", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("Scenes", "SetCurrentProgramScene", Verdict.OK_NO_EFFECT, f"result=True but program scene is {cur.get('sceneName')!r}, not {SCENE_A!r}")

    ok, _, comment = await ws.req(
        "CreateInput",
        {
            "sceneName": SCENE_A,
            "inputName": COLOR_SRC,
            "inputKind": "color_source_v3",
            "inputSettings": {"color": 4278190335},  # opaque magenta ABGR
        },
    )
    _, inputs_data, _ = await ws.req("GetInputList")
    input_names = {i["inputName"] for i in inputs_data.get("inputs", [])}
    _, items_data, _ = await ws.req("GetSceneItemList", {"sceneName": SCENE_A})
    item_sources = {i["sourceName"] for i in items_data.get("sceneItems", [])}
    if ok and COLOR_SRC in input_names and COLOR_SRC in item_sources:
        record("Inputs", "CreateInput", Verdict.OK, "input in GetInputList AND scene item in GetSceneItemList")
    elif not ok:
        record("Inputs", "CreateInput", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record(
            "Inputs",
            "CreateInput",
            Verdict.OK_NO_EFFECT,
            f"result=True but not fully wired: in inputs={(COLOR_SRC in input_names)}, in scene items={(COLOR_SRC in item_sources)}",
        )

    # SetInputSettings -- the settings the server hands back must be the ones
    # we pushed, not the ones it had.
    wanted_color = 4278255360  # opaque green ABGR, different from creation
    ok, _, comment = await ws.req(
        "SetInputSettings", {"inputName": COLOR_SRC, "inputSettings": {"color": wanted_color}}
    )
    _, settings_data, _ = await ws.req("GetInputSettings", {"inputName": COLOR_SRC})
    got_color = (settings_data.get("inputSettings") or {}).get("color")
    if ok and got_color == wanted_color:
        record("Inputs", "SetInputSettings", Verdict.OK, f"GetInputSettings reflects color={got_color}")
    elif not ok:
        record("Inputs", "SetInputSettings", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("Inputs", "SetInputSettings", Verdict.OK_NO_EFFECT, f"result=True but GetInputSettings.color={got_color!r}, wanted {wanted_color}")


async def drive_scene_items(ws: PulsarWs) -> None:
    """SceneItem geometry -- untouched by the first pass, and the family with
    the largest gap between "the request answered" and "the composition
    moved"."""
    ok_id, id_data, comment = await ws.req("GetSceneItemId", {"sceneName": SCENE_A, "sourceName": COLOR_SRC})
    item_id = id_data.get("sceneItemId")
    if not ok_id or item_id is None:
        record("SceneItems", "SetSceneItemTransform", Verdict.ERROR_EXPLICIT, f"no scene item to drive: comment={comment}")
        record("SceneItems", "SetSceneItemEnabled", Verdict.ERROR_EXPLICIT, f"no scene item to drive: comment={comment}")
        return

    wanted = {"positionX": 128.0, "positionY": 72.0, "rotation": 30.0}
    ok, _, comment = await ws.req(
        "SetSceneItemTransform", {"sceneName": SCENE_A, "sceneItemId": item_id, "sceneItemTransform": wanted}
    )
    _, tf_data, _ = await ws.req("GetSceneItemTransform", {"sceneName": SCENE_A, "sceneItemId": item_id})
    tf = tf_data.get("sceneItemTransform") or {}
    matched = all(abs(float(tf.get(k, -1)) - v) < 0.01 for k, v in wanted.items())
    if ok and matched:
        record("SceneItems", "SetSceneItemTransform", Verdict.OK, f"GetSceneItemTransform reflects {wanted}")
    elif not ok:
        record("SceneItems", "SetSceneItemTransform", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record(
            "SceneItems",
            "SetSceneItemTransform",
            Verdict.OK_NO_EFFECT,
            f"result=True but transform is positionX={tf.get('positionX')!r} positionY={tf.get('positionY')!r} rotation={tf.get('rotation')!r}",
        )

    ok, _, comment = await ws.req(
        "SetSceneItemEnabled", {"sceneName": SCENE_A, "sceneItemId": item_id, "sceneItemEnabled": False}
    )
    _, en_data, _ = await ws.req("GetSceneItemEnabled", {"sceneName": SCENE_A, "sceneItemId": item_id})
    enabled = en_data.get("sceneItemEnabled")
    if ok and enabled is False:
        record("SceneItems", "SetSceneItemEnabled", Verdict.OK, "GetSceneItemEnabled reflects False")
    elif not ok:
        record("SceneItems", "SetSceneItemEnabled", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("SceneItems", "SetSceneItemEnabled", Verdict.OK_NO_EFFECT, f"result=True but GetSceneItemEnabled={enabled!r}")
    await ws.req("SetSceneItemEnabled", {"sceneName": SCENE_A, "sceneItemId": item_id, "sceneItemEnabled": True})


async def filter_names(ws: PulsarWs) -> set[str]:
    _, data, _ = await ws.req("GetSourceFilterList", {"sourceName": COLOR_SRC})
    return {f["filterName"] for f in data.get("filters", [])}


async def drive_filters(ws: PulsarWs) -> None:
    """Filters -- ADR Prism 026 §3.3 widening priority #2 (surface opened by
    ADR Prism 023 §3.3, so new and sensitive). Four subjects: create, settings,
    enable, remove -- each cross-checked against GetSourceFilter(List)."""
    kind = "color_filter_v2"
    _, kinds_data, _ = await ws.req("GetSourceFilterKindList")
    kinds = kinds_data.get("sourceFilterKinds") or []
    if kinds and kind not in kinds:
        kind = kinds[0]

    ok, _, comment = await ws.req(
        "CreateSourceFilter",
        {"sourceName": COLOR_SRC, "filterName": FILTER_NAME, "filterKind": kind},
    )
    present = FILTER_NAME in await filter_names(ws)
    if ok and present:
        record("Filters", "CreateSourceFilter", Verdict.OK, f"{FILTER_NAME} ({kind}) present in GetSourceFilterList")
    elif not ok:
        record("Filters", "CreateSourceFilter", Verdict.ERROR_EXPLICIT, f"comment={comment}")
        # No fixture -> the three subjects below have nothing to act on. Say so
        # explicitly rather than dropping them from the run (the coverage
        # cross-check would fail, which is the point).
        for subject in ("SetSourceFilterSettings", "SetSourceFilterEnabled", "RemoveSourceFilter"):
            record("Filters", subject, Verdict.ERROR_EXPLICIT, "no filter fixture: CreateSourceFilter was refused above")
        return
    else:
        record("Filters", "CreateSourceFilter", Verdict.OK_NO_EFFECT, f"result=True but {FILTER_NAME} absent from GetSourceFilterList")

    ok, _, comment = await ws.req(
        "SetSourceFilterSettings",
        {"sourceName": COLOR_SRC, "filterName": FILTER_NAME, "filterSettings": {"opacity": 42}},
    )
    _, f_data, _ = await ws.req("GetSourceFilter", {"sourceName": COLOR_SRC, "filterName": FILTER_NAME})
    got = (f_data.get("filterSettings") or {}).get("opacity")
    if ok and got == 42:
        record("Filters", "SetSourceFilterSettings", Verdict.OK, "GetSourceFilter reflects opacity=42")
    elif not ok:
        record("Filters", "SetSourceFilterSettings", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("Filters", "SetSourceFilterSettings", Verdict.OK_NO_EFFECT, f"result=True but GetSourceFilter.filterSettings.opacity={got!r}")

    ok, _, comment = await ws.req(
        "SetSourceFilterEnabled",
        {"sourceName": COLOR_SRC, "filterName": FILTER_NAME, "filterEnabled": False},
    )
    _, f_data, _ = await ws.req("GetSourceFilter", {"sourceName": COLOR_SRC, "filterName": FILTER_NAME})
    enabled = f_data.get("filterEnabled")
    if ok and enabled is False:
        record("Filters", "SetSourceFilterEnabled", Verdict.OK, "GetSourceFilter.filterEnabled reflects False")
    elif not ok:
        record("Filters", "SetSourceFilterEnabled", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("Filters", "SetSourceFilterEnabled", Verdict.OK_NO_EFFECT, f"result=True but GetSourceFilter.filterEnabled={enabled!r}")

    ok, _, comment = await ws.req("RemoveSourceFilter", {"sourceName": COLOR_SRC, "filterName": FILTER_NAME})
    still_there = FILTER_NAME in await filter_names(ws)
    if ok and not still_there:
        record("Filters", "RemoveSourceFilter", Verdict.OK, "filter gone from GetSourceFilterList")
    elif not ok:
        record("Filters", "RemoveSourceFilter", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("Filters", "RemoveSourceFilter", Verdict.OK_NO_EFFECT, "result=True but the filter is still listed")


async def drive_transitions(ws: PulsarWs) -> None:
    """Pulsar's frontend stub registers its own transition set (Fade, plus the
    native Stinger when PULSAR_NATIVE_STINGER is on) -- it is NOT obs-studio's.
    Pick the name off the server instead of assuming one, otherwise the subject
    measures the probe's guess rather than the request."""
    _, list_data, _ = await ws.req("GetSceneTransitionList")
    available = [t.get("transitionName") for t in list_data.get("transitions", []) if t.get("transitionName")]
    if not available:
        record("Transitions", "SetCurrentSceneTransition", Verdict.ERROR_EXPLICIT, "GetSceneTransitionList is empty -- no transition to select")
        return
    wanted = available[0]

    ok, _, comment = await ws.req("SetCurrentSceneTransition", {"transitionName": wanted})
    _, data, _ = await ws.req("GetCurrentSceneTransition")
    name = data.get("transitionName")
    if ok and name == wanted:
        record("Transitions", "SetCurrentSceneTransition", Verdict.OK, f"GetCurrentSceneTransition reflects {wanted!r} (from {available})")
    elif not ok:
        record("Transitions", "SetCurrentSceneTransition", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("Transitions", "SetCurrentSceneTransition", Verdict.OK_NO_EFFECT, f"result=True but current transition is {name!r}, wanted {wanted!r}")
    # Keep the studio-mode swap deterministic whatever the transition is.
    await ws.req("SetCurrentSceneTransitionDuration", {"transitionDuration": 50})


async def drive_stream(ws: PulsarWs) -> None:
    """Calibration family.

    Two subjects, and they are deliberately judged against DIFFERENT promises:

    - SetStreamServiceSettings promises to set the stream service. It is
      cross-checked against GetStreamServiceSettings, which is the state it
      actually owns.
    - StartStream promises the v5 single-stream path really starts. Since #131
      the frontend stub binds `streamService` to `streamOutput` before
      obs_output_start(), so this path is reachable at all for the first time;
      before that fix no combination of requests could make it succeed.

    Why the verdict is now built on an EVENT and not on GetStreamStatus: the
    service pushed above points at rtmp://127.0.0.1:1 on purpose -- this probe
    never touches the network. libobs accepts the start and hands the connect
    to its own thread, so `outputActive` legitimately stays false the whole
    time and re-querying it proves nothing either way. What cannot happen
    without the binding is the EVENT: `OBS_FRONTEND_EVENT_STREAMING_STARTING`
    is emitted only after obs_output_start() returned true (#131 honesty
    corollary -- it used to be emitted before, unconditionally), and
    obs_output_start() returns false on the spot when the output has no
    service. A StreamStateChanged/OBS_WEBSOCKET_OUTPUT_STARTING on the wire is
    therefore proof the service reached the output -- deterministic, offline,
    and impossible to produce before #131.
    """
    ok, _, comment = await ws.req(
        "SetStreamServiceSettings",
        {"streamServiceType": "rtmp_custom", "streamServiceSettings": {"server": "rtmp://127.0.0.1:1/probe", "key": "x"}},
    )
    _, svc_data, _ = await ws.req("GetStreamServiceSettings")
    svc_type = svc_data.get("streamServiceType")
    svc_server = (svc_data.get("streamServiceSettings") or {}).get("server")
    if ok and svc_type == "rtmp_custom" and svc_server == "rtmp://127.0.0.1:1/probe":
        record("Stream", "SetStreamServiceSettings", Verdict.OK, "GetStreamServiceSettings reflects the pushed rtmp_custom service")
    elif not ok:
        record("Stream", "SetStreamServiceSettings", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record(
            "Stream",
            "SetStreamServiceSettings",
            Verdict.OK_NO_EFFECT,
            f"result=True but GetStreamServiceSettings reports type={svc_type!r} server={svc_server!r}",
        )

    ws.clear_events()
    ok_start, _, comment = await ws.req("StartStream")
    starting = await ws.wait_for_event("StreamStateChanged", "OBS_WEBSOCKET_OUTPUT_STARTING", timeout=8.0)
    # Drain a little longer so the sequel shows up in the detail line. The
    # unreachable endpoint means STOPPED normally follows within a second --
    # that is EXPECTED and named here, not an error: the contract StartStream
    # signs is "libobs took the action", not "the TCP connect succeeded"
    # (PROTOCOL.md, start/stop semantics).
    await ws.wait_for_event("StreamStateChanged", "OBS_WEBSOCKET_OUTPUT_STOPPED", timeout=4.0)
    states = [s for s in ws.event_states("StreamStateChanged") if s]
    if not ok_start:
        record("Stream", "StartStream", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    elif starting:
        record(
            "Stream",
            "StartStream",
            Verdict.OK,
            f"StreamStateChanged on the wire: {states} -- STARTING proves the service is bound to the "
            "output (#131); the trailing STOPPED is the deliberately unreachable rtmp endpoint",
        )
    else:
        record(
            "Stream",
            "StartStream",
            Verdict.OK_NO_EFFECT,
            f"result=True but no StreamStateChanged/OBS_WEBSOCKET_OUTPUT_STARTING was emitted "
            f"(states seen: {states}) -- obs_output_start never took the action",
        )
    await ws.req("StopStream")


async def drive_record(ws: PulsarWs) -> bool:
    """Record family. Returns True if the recording is running when we leave
    it (the replay-buffer driver needs the shared encoders up).

    PauseRecord carries THREE verdicts since #130, all on the same subject:
    the off-air refusal, the pre-video-frame refusal, and the healthy pause.
    """
    # Issue #130 (guard 1 / #120 family): pausing an output that is not
    # recording used to answer Success(). obs_output_pause() returns false on
    # an inactive output and obs_frontend_recording_pause() returns void, so
    # the refusal was thrown away. Fully deterministic -- nothing has started
    # yet at this point in the run.
    ok, _, comment = await ws.req("PauseRecord")
    code = ws.last_status.get("code")
    if ok:
        record("Record", "PauseRecord", Verdict.OK_NO_EFFECT,
               "result=True for PauseRecord issued with no recording running")
    else:
        record("Record", "PauseRecord", Verdict.ERROR_EXPLICIT,
               f"off-air pause refused (code={code}, expected 501 OutputNotRunning): {comment}")

    ok, _, comment = await ws.req("StartRecord")
    active = await ws.poll("GetRecordStatus", "outputActive", True)
    if not ok:
        record("Record", "StartRecord", Verdict.ERROR_EXPLICIT, f"comment={comment}")
        for subject in ("PauseRecord", "ResumeRecord", "StopRecord"):
            record("Record", subject, Verdict.ERROR_EXPLICIT, "recording never started: StartRecord was refused above")
        return False
    if not active:
        record("Record", "StartRecord", Verdict.OK_NO_EFFECT, "result=True but GetRecordStatus.outputActive stayed False")
        for subject in ("PauseRecord", "ResumeRecord", "StopRecord"):
            record("Record", subject, Verdict.OK_NO_EFFECT, "not exercisable: the recording the server claimed to start never became active")
        return False
    record("Record", "StartRecord", Verdict.OK, "GetRecordStatus.outputActive flips to True")

    # Issue #130 (guard 2): pausing an ffmpeg_muxer before its first VIDEO
    # frame wedges it FOR GOOD -- libobs computes the pause window from an
    # encoder timestamp that is still 0, so the pause never lifts, the replay
    # buffer borrowing the same encoders stops producing files, and Stop*
    # can otherwise answer Success() while outputActive stays true. The root cause is
    # upstream (obs-output.c / obs-encoder.c) and out of Pulsar's mandate; the
    # websocket layer refuses the precondition with the cause named
    # (InvalidResourceState 604). Driving it here is now safe BECAUSE of that
    # refusal -- before #130 this sequence bricked the rest of the run, which
    # is why the case was routed out of the gate instead of wired into it.
    _, st, _ = await ws.req("GetRecordStatus")
    frames_before_pause = st.get("outputTotalFrames") or 0
    ok, _, comment = await ws.req("PauseRecord")
    code = ws.last_status.get("code")
    if frames_before_pause > 0:
        # The muxer beat the probe to its first video frame: the wedging precondition
        # was not reachable this run. Say so and undo -- do not manufacture a
        # verdict out of a case that did not happen.
        print(f"  (pre-video-frame pause precondition not reachable: outputTotalFrames={frames_before_pause})")
        if ok:
            await ws.req("ResumeRecord")
    elif ok:
        record("Record", "PauseRecord", Verdict.OK_NO_EFFECT,
               "result=True for a pause issued at outputTotalFrames==0 -- libobs's pause timeline is now wedged (#130)")
    else:
        record("Record", "PauseRecord", Verdict.ERROR_EXPLICIT,
               f"pre-video-frame pause refused (code={code}, expected 604 InvalidResourceState): {comment}")

    # Now let the muxer receive its first video frame, and drive pause the way
    # an operator does -- on a recording that is really recording.  Bytes alone
    # are not enough: AAC may make outputBytes non-zero before video arrives.
    frames_ready = False
    for _ in range(STOP_ATTEMPTS):
        _, st, _ = await ws.req("GetRecordStatus")
        if (st.get("outputTotalFrames") or 0) > 0:
            frames_ready = True
            break
        await asyncio.sleep(0.25)
    if not frames_ready:
        raise RuntimeError(
            "recording never delivered a video frame before healthy pause test "
            "(GetRecordStatus.outputTotalFrames stayed 0)"
        )

    ok, _, comment = await ws.req("PauseRecord")
    paused = await ws.poll("GetRecordStatus", "outputPaused", True, attempts=8)
    if not ok or paused is not True:
        record(
            "Record",
            "PauseRecord",
            Verdict.ERROR_EXPLICIT,
            f"healthy pause did not land: ok={ok} paused={paused!r} comment={comment}",
        )
        raise RuntimeError("healthy PauseRecord did not become outputPaused=true")
    record("Record", "PauseRecord", Verdict.OK, "GetRecordStatus.outputPaused reflects True")

    await asyncio.sleep(1.0)  # dwell in pause; see the note above
    ok, _, comment = await ws.req("ResumeRecord")
    resumed = await ws.poll("GetRecordStatus", "outputPaused", False, attempts=8)
    if not ok or resumed is not False:
        record(
            "Record",
            "ResumeRecord",
            Verdict.ERROR_EXPLICIT,
            f"healthy resume did not land: ok={ok} paused={resumed!r} comment={comment}",
        )
        raise RuntimeError("healthy ResumeRecord did not become outputPaused=false")
    record("Record", "ResumeRecord", Verdict.OK, "GetRecordStatus.outputPaused reflects False")

    return True


async def finish_record(ws: PulsarWs) -> None:
    ok, _, comment = await ws.req("StopRecord")
    code = ws.last_status.get("code")
    if not ok and code == 702:
        # StopRecord keeps its response truthful when the muxer flush exceeds
        # the bounded server window.  Consume the authoritative completion
        # before Replay/scene probes reuse this shared process; do not turn the
        # 702 request response into a fake Success.  Observe the event and the
        # independent status readback in ONE bounded loop: waiting 15 s for the
        # event and only then polling status created a false negative when a
        # loaded runner delivered one boundary just after the other window.
        landed, stopped = await wait_for_record_stop(ws)
        # Either observation is authoritative.  The STOPPED event can race the
        # request response and already be buffered before this helper starts,
        # while GetRecordStatus remains a stable independent readback.
        if not landed and stopped is not False:
            record(
                "Record",
                "StopRecord",
                Verdict.ERROR_EXPLICIT,
                f"bounded Pending 702 did not settle: event_stopped={landed} outputActive={stopped!r} comment={comment}",
            )
            raise RuntimeError("StopRecord Pending did not settle before the next capability probe")
        record(
            "Record",
            "StopRecord",
            Verdict.ERROR_EXPLICIT,
            "bounded Pending 702 was truthful; completion was proven by "
            f"event_stopped={landed} or outputActive={stopped!r}",
        )
        return
    stopped = await ws.poll("GetRecordStatus", "outputActive", False, attempts=STOP_ATTEMPTS)
    if not ok:
        record("Record", "StopRecord", Verdict.ERROR_EXPLICIT, f"comment={comment}")
        raise RuntimeError(f"StopRecord was not successful after pause/replay sequence: {comment}")
    elif stopped is False:
        record("Record", "StopRecord", Verdict.OK, "GetRecordStatus.outputActive flips back to False")
    else:
        record("Record", "StopRecord", Verdict.OK_NO_EFFECT, f"result=True but outputActive={stopped!r}")
        raise RuntimeError("StopRecord returned success but outputActive stayed true")


async def wait_for_record_stop(ws: PulsarWs, timeout: float = PENDING_STOP_SETTLE_S) -> tuple[bool, Any]:
    """Wait for either authoritative record-stop boundary.

    A 702 response means libobs accepted the stop but its ffmpeg muxer is still
    draining.  Under load, the websocket STOPPED event and the status readback
    do not necessarily arrive in the same scheduling slice.  Poll both on one
    shared deadline so the probe never turns a late-but-valid completion into a
    failure, while still failing closed after a finite budget.
    """
    deadline = time.monotonic() + timeout
    landed = ws._seen("RecordStateChanged", "OBS_WEBSOCKET_OUTPUT_STOPPED")
    stopped: Any = None
    while True:
        if landed:
            return True, stopped

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return landed, stopped

        try:
            _, data, _ = await ws.req("GetRecordStatus", timeout=min(2.0, remaining))
            stopped = data.get("outputActive")
        except (asyncio.TimeoutError, TimeoutError):
            # A congested status request is not proof that the output is still
            # active; keep the same bounded deadline and continue observing.
            pass

        landed = ws._seen("RecordStateChanged", "OBS_WEBSOCKET_OUTPUT_STOPPED")
        if landed or stopped is False:
            return landed, stopped

        await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


async def probe_offair_arm(ws: PulsarWs) -> tuple[bool, bool, str | None]:
    """Arm the replay buffer with the encoders IDLE, before anything records.

    No verdict recorded here -- this is the first half of the StartReplayBuffer
    subject. Returns (result, outputActive, comment). Before #117/#120 this
    answered Success() and left outputActive False: the textbook B7 lie, and
    the exact regression the gate exists to catch."""
    ok, _, comment = await ws.req("StartReplayBuffer")
    _, st, _ = await ws.req("GetReplayBufferStatus")
    return ok, bool(st.get("outputActive")), comment


async def drive_replay_buffer(ws: PulsarWs, offair: tuple[bool, bool, str | None], encoders_up: bool) -> None:
    """Suspect family (Thinker B7), wired for real by #117.

    One verdict per subject, built from both halves:
      - OFF-AIR arm (encoders idle): must be an EXPLICIT refusal. A Success()
        that leaves outputActive False is the B7 failure mode and short-circuits
        the family.
      - ON-AIR arm (recording running, so the shared encoders exist): must
        really flip GetReplayBufferStatus.outputActive, and SaveReplayBuffer
        must surface a real path via GetLastReplayBufferReplay (it used to
        return "" forever).
    """
    offair_ok, offair_active, offair_comment = offair
    if offair_ok and not offair_active:
        record(
            "ReplayBuffer",
            "StartReplayBuffer",
            Verdict.OK_NO_EFFECT,
            "off-air arm (encoders idle) answered result=True but outputActive stayed False",
        )
        for subject in ("SaveReplayBuffer", "StopReplayBuffer"):
            record("ReplayBuffer", subject, Verdict.OK_NO_EFFECT, "not exercisable: the off-air arm above reported an effect it did not have")
        return
    if not offair_ok:
        print(f"  (off-air arm correctly refused: {offair_comment})")

    if not encoders_up:
        record("ReplayBuffer", "StartReplayBuffer", Verdict.ERROR_EXPLICIT, f"only the off-air branch was reachable (no encoders): comment={offair_comment}")
        for subject in ("SaveReplayBuffer", "StopReplayBuffer"):
            record("ReplayBuffer", subject, Verdict.ERROR_EXPLICIT, "buffer never armed (encoders idle) -- nothing to exercise")
        return

    ok, _, comment = await ws.req("StartReplayBuffer")
    active = await ws.poll("GetReplayBufferStatus", "outputActive", True)
    if not ok:
        record("ReplayBuffer", "StartReplayBuffer", Verdict.ERROR_EXPLICIT, f"comment={comment}")
        for subject in ("SaveReplayBuffer", "StopReplayBuffer"):
            record("ReplayBuffer", subject, Verdict.ERROR_EXPLICIT, "buffer never armed: StartReplayBuffer was refused above")
        return
    if not active:
        record("ReplayBuffer", "StartReplayBuffer", Verdict.OK_NO_EFFECT, "result=True but GetReplayBufferStatus.outputActive stayed False")
        for subject in ("SaveReplayBuffer", "StopReplayBuffer"):
            record("ReplayBuffer", subject, Verdict.OK_NO_EFFECT, "not exercisable: the buffer the server claimed to arm never became active")
        return
    record("ReplayBuffer", "StartReplayBuffer", Verdict.OK, "GetReplayBufferStatus.outputActive flips to True with the encoders up")

    await asyncio.sleep(REPLAY_FILL_S)
    ok, _, comment = await ws.req("SaveReplayBuffer")
    # The save is asynchronous (the muxer flushes the ring on its own thread);
    # GetLastReplayBufferReplay only fills once it lands. The probe waits, the
    # request does not -- same discipline as everywhere else here.
    saved = ""
    for _ in range(STOP_ATTEMPTS):
        _, last_data, _ = await ws.req("GetLastReplayBufferReplay")
        saved = last_data.get("savedReplayPath") or ""
        if saved:
            break
        await asyncio.sleep(0.25)
    if not ok:
        record("ReplayBuffer", "SaveReplayBuffer", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    elif saved:
        record("ReplayBuffer", "SaveReplayBuffer", Verdict.OK, f"GetLastReplayBufferReplay.savedReplayPath={saved!r}")
    else:
        record("ReplayBuffer", "SaveReplayBuffer", Verdict.OK_NO_EFFECT, "result=True but GetLastReplayBufferReplay never surfaced a path")

    ok, _, comment = await ws.req("StopReplayBuffer")
    stopped = await ws.poll("GetReplayBufferStatus", "outputActive", False, attempts=STOP_ATTEMPTS)
    if not ok:
        record("ReplayBuffer", "StopReplayBuffer", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    elif stopped is False:
        record("ReplayBuffer", "StopReplayBuffer", Verdict.OK, "GetReplayBufferStatus.outputActive flips back to False")
    else:
        record("ReplayBuffer", "StopReplayBuffer", Verdict.OK_NO_EFFECT, f"result=True but outputActive={stopped!r}")


async def drive_virtualcam(ws: PulsarWs) -> None:
    """ADR Prism 026 §3.3 widening priority #1: same risk shape as the replay
    buffer (a device-backed output that can report success without an effect),
    and Prism really uses it (source mode, ADR Prism 022 §3.1).

    A machine with no virtual-camera driver registered is NOT a failure here:
    libobs then has no `virtualcam_output` to hold, the requests answer with an
    explicit refusal, and ERROR_EXPLICIT is the passing branch. The gate only
    bites on "started, and it did not start"."""
    ok_status, st, comment = await ws.req("GetVirtualCamStatus")
    if not ok_status:
        record("VirtualCam", "StartVirtualCam", Verdict.ERROR_EXPLICIT, f"no virtualcam output on this machine: GetVirtualCamStatus comment={comment}")
        record("VirtualCam", "StopVirtualCam", Verdict.ERROR_EXPLICIT, "no virtualcam output on this machine")
        return

    ok, _, comment = await ws.req("StartVirtualCam")
    active = await ws.poll("GetVirtualCamStatus", "outputActive", True, attempts=12)
    if not ok:
        record("VirtualCam", "StartVirtualCam", Verdict.ERROR_EXPLICIT, f"comment={comment}")
        record("VirtualCam", "StopVirtualCam", Verdict.ERROR_EXPLICIT, "cam never started: StartVirtualCam was refused above")
        return
    if not active:
        record("VirtualCam", "StartVirtualCam", Verdict.OK_NO_EFFECT, "result=True but GetVirtualCamStatus.outputActive stayed False")
        record("VirtualCam", "StopVirtualCam", Verdict.OK_NO_EFFECT, "not exercisable: the cam the server claimed to start never became active")
        return
    record("VirtualCam", "StartVirtualCam", Verdict.OK, "GetVirtualCamStatus.outputActive flips to True")

    ok, _, comment = await ws.req("StopVirtualCam")
    stopped = await ws.poll("GetVirtualCamStatus", "outputActive", False, attempts=12)
    if not ok:
        record("VirtualCam", "StopVirtualCam", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    elif stopped is False:
        record("VirtualCam", "StopVirtualCam", Verdict.OK, "GetVirtualCamStatus.outputActive flips back to False")
    else:
        record("VirtualCam", "StopVirtualCam", Verdict.OK_NO_EFFECT, f"result=True but outputActive={stopped!r}")


async def drive_outputs(ws: PulsarWs) -> None:
    """Generic Outputs -- ADR Prism 026 §3.3 widening priority #3, and the
    surface #127 hardened (the by-name Start/StopOutput handlers used to
    Success() without verifying the effect)."""
    ok, data, comment = await ws.req("GetOutputList")
    outputs = data.get("outputs") or []
    if not ok:
        record("Outputs", "GetOutputList", Verdict.ERROR_EXPLICIT, f"comment={comment}")
        record("Outputs", "GetOutputStatus", Verdict.ERROR_EXPLICIT, "no output list to walk")
        record("Outputs", "StartOutput", Verdict.ERROR_EXPLICIT, "no output list to walk")
        return
    if not outputs:
        record("Outputs", "GetOutputList", Verdict.OK_NO_EFFECT, "result=True but outputs=[] -- no output is enumerable at all")
        record("Outputs", "GetOutputStatus", Verdict.OK_NO_EFFECT, "no output to cross-check against")
        record("Outputs", "StartOutput", Verdict.OK_NO_EFFECT, "no output to drive")
        return
    names = [o.get("outputName") for o in outputs if o.get("outputName")]
    record("Outputs", "GetOutputList", Verdict.OK, f"{len(outputs)} output(s) enumerated: {names}")

    # GetOutputStatus must agree with the list entry on the SAME output --
    # two independent reads of one piece of server state.
    first = outputs[0]
    name = first.get("outputName")
    ok, st, comment = await ws.req("GetOutputStatus", {"outputName": name})
    if not ok:
        record("Outputs", "GetOutputStatus", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    elif st.get("outputActive") == first.get("outputActive"):
        record("Outputs", "GetOutputStatus", Verdict.OK, f"{name!r}: outputActive agrees with GetOutputList ({st.get('outputActive')})")
    else:
        record(
            "Outputs",
            "GetOutputStatus",
            Verdict.OK_NO_EFFECT,
            f"{name!r}: GetOutputStatus.outputActive={st.get('outputActive')!r} but GetOutputList said {first.get('outputActive')!r}",
        )

    # StartOutput on the rtmp output: no service is bound to it (see the
    # Stream driver), so libobs refuses -- and the handler must SAY so. This
    # is the #127 regression gate on the by-name path.
    rtmp = next((o for o in outputs if o.get("outputKind") == "rtmp_output"), None)
    target = rtmp or first
    tname = target.get("outputName")
    was_active = bool(target.get("outputActive"))
    ok, _, comment = await ws.req("StartOutput", {"outputName": tname})
    _, st, _ = await ws.req("GetOutputStatus", {"outputName": tname})
    now_active = bool(st.get("outputActive"))
    if not ok:
        record("Outputs", "StartOutput", Verdict.ERROR_EXPLICIT, f"{tname!r} correctly refused: comment={comment}")
    elif now_active:
        record("Outputs", "StartOutput", Verdict.OK, f"{tname!r} is genuinely active after StartOutput")
        if not was_active:
            await ws.req("StopOutput", {"outputName": tname})
    else:
        record("Outputs", "StartOutput", Verdict.OK_NO_EFFECT, f"result=True but {tname!r} outputActive stayed False")


async def drive_studio_mode(ws: PulsarWs) -> None:
    """Suspect family (Thinker B7). The Cut transition set by drive_transitions
    makes the swap instantaneous and deterministic (no fade-duration wait)."""
    ok_en, _, comment = await ws.req("SetStudioModeEnabled", {"studioModeEnabled": True})
    _, g_data, _ = await ws.req("GetStudioModeEnabled")
    if ok_en and g_data.get("studioModeEnabled") is True:
        record("StudioMode", "SetStudioModeEnabled", Verdict.OK, "GetStudioModeEnabled reflects True")
    elif not ok_en:
        record("StudioMode", "SetStudioModeEnabled", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("StudioMode", "SetStudioModeEnabled", Verdict.OK_NO_EFFECT, f"result=True but GetStudioModeEnabled={g_data}")

    await ws.req("SetCurrentProgramScene", {"sceneName": SCENE_A})
    ok_prev, _, comment = await ws.req("SetCurrentPreviewScene", {"sceneName": SCENE_B})
    _, prev_data, _ = await ws.req("GetCurrentPreviewScene")
    if ok_prev and prev_data.get("sceneName") == SCENE_B:
        record("StudioMode", "SetCurrentPreviewScene", Verdict.OK, f"GetCurrentPreviewScene reflects {SCENE_B}")
    elif not ok_prev:
        record("StudioMode", "SetCurrentPreviewScene", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("StudioMode", "SetCurrentPreviewScene", Verdict.OK_NO_EFFECT, f"result=True but preview scene is {prev_data.get('sceneName')!r}")

    _, before_data, _ = await ws.req("GetCurrentProgramScene")
    ok_trig, _, comment_trig = await ws.req("TriggerStudioModeTransition")
    await ws.poll("GetCurrentProgramScene", "sceneName", SCENE_B, attempts=20)
    _, after_data, _ = await ws.req("GetCurrentProgramScene")
    if not ok_trig:
        record("StudioMode", "TriggerStudioModeTransition", Verdict.ERROR_EXPLICIT, f"comment={comment_trig}")
    elif after_data.get("sceneName") == SCENE_B and before_data.get("sceneName") != SCENE_B:
        record("StudioMode", "TriggerStudioModeTransition", Verdict.OK, f"program scene flips {before_data.get('sceneName')!r} -> {SCENE_B!r}")
    else:
        record(
            "StudioMode",
            "TriggerStudioModeTransition",
            Verdict.OK_NO_EFFECT,
            f"result=True but program scene before={before_data.get('sceneName')!r} after={after_data.get('sceneName')!r} (preview was {SCENE_B!r})",
        )

    await ws.req("SetStudioModeEnabled", {"studioModeEnabled": False})


async def drive_canvases(ws: PulsarWs) -> None:
    """Suspect family (Thinker B7). Cross-checks GetCanvasList against an
    independently-fetched GetVideoSettings -- a served-but-empty canvas
    list would either be empty or carry dimensions disconnected from the
    real, boot-configured video pipeline."""
    _, video_data, _ = await ws.req("GetVideoSettings")
    ok_c, canvas_data, comment = await ws.req("GetCanvasList")
    canvases = canvas_data.get("canvases", [])

    if not ok_c:
        record("Canvases", "GetCanvasList", Verdict.ERROR_EXPLICIT, f"comment={comment}")
        return
    if not canvases:
        record("Canvases", "GetCanvasList", Verdict.OK_NO_EFFECT, "result=True but canvases=[] -- no canvas is actually enumerable")
        return

    main = canvases[0]
    main_video = main.get("canvasVideoSettings", {})
    w_match = main_video.get("baseWidth") == video_data.get("baseWidth") and main_video.get("baseHeight") == video_data.get("baseHeight")
    if w_match:
        record("Canvases", "GetCanvasList", Verdict.OK, f"main canvas {main_video.get('baseWidth')}x{main_video.get('baseHeight')} matches GetVideoSettings")
    else:
        record(
            "Canvases",
            "GetCanvasList",
            Verdict.OK_NO_EFFECT,
            f"result=True with a non-empty list, but canvas entry does not correlate to GetVideoSettings: canvas={main_video} video={video_data}",
        )


async def drive_cleanup(ws: PulsarWs) -> None:
    """RemoveScene is cleanup AND a subject: "removed" must mean gone from the
    listing, the mirror image of the #119 finding.

    RemoveInput became a subject with #129. The v5 contract is explicit -- it
    "will immediately remove all associated scene items" -- and until the
    frontend stub grew a `source_remove` handler it removed NEITHER: the input
    stayed in GetInputList and its item in GetSceneItemList indefinitely, since
    libobs only prunes a scene it actually renders and SCENE_A is not the
    program scene here. That non-rendered scene is precisely why this is the
    honest place to gate it.
    """
    ok, _, comment = await ws.req("RemoveInput", {"inputName": COLOR_SRC})
    # The prune runs synchronously in the signal handler, but libobs defers the
    # final source destruction to its own thread, so the disappearance from
    # GetInputList lands a tick or two later. The probe waits; the request does
    # not.
    in_inputs = True
    in_items = True
    for _ in range(20):
        _, inputs_data, _ = await ws.req("GetInputList")
        in_inputs = COLOR_SRC in {i["inputName"] for i in inputs_data.get("inputs", [])}
        _, items_data, _ = await ws.req("GetSceneItemList", {"sceneName": SCENE_A})
        in_items = COLOR_SRC in {i["sourceName"] for i in items_data.get("sceneItems", [])}
        if not in_inputs and not in_items:
            break
        await asyncio.sleep(0.25)
    if not ok:
        record("Inputs", "RemoveInput", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    elif not in_inputs and not in_items:
        record("Inputs", "RemoveInput", Verdict.OK,
               "gone from GetInputList AND its scene item gone from GetSceneItemList")
    else:
        record("Inputs", "RemoveInput", Verdict.OK_NO_EFFECT,
               f"result=True but still listed: in GetInputList={in_inputs}, in GetSceneItemList={in_items}")

    ok_a, _, comment_a = await ws.req("RemoveScene", {"sceneName": SCENE_A})
    ok_b, _, comment_b = await ws.req("RemoveScene", {"sceneName": SCENE_B})
    names = await scene_names(ws)
    if not (ok_a and ok_b):
        record("Scenes", "RemoveScene", Verdict.ERROR_EXPLICIT, f"comment={comment_a or comment_b}")
    elif SCENE_A not in names and SCENE_B not in names:
        record("Scenes", "RemoveScene", Verdict.OK, "both scenes gone from GetSceneList")
    else:
        record("Scenes", "RemoveScene", Verdict.OK_NO_EFFECT, f"result=True but GetSceneList still lists {names & {SCENE_A, SCENE_B}}")


# --------------------------------------------------------------------------
# Coverage cross-check (issue #121 resolution criterion 3).
# --------------------------------------------------------------------------
def check_coverage() -> list[str]:
    """Compare what the run actually drove against the versioned artefact.
    Returns a list of problems (empty == in sync)."""
    problems: list[str] = []
    try:
        artefact = json.loads(COVERAGE_ARTEFACT.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- the artefact is part of the gate
        return [f"cannot read the coverage artefact {COVERAGE_ARTEFACT}: {exc}"]

    declared = {f"{fam}/{t}" for fam, types in artefact.get("subjects", {}).items() for t in types}
    driven = {f"{r.family}/{r.request_type}" for r in RESULTS}

    for missing in sorted(declared - driven):
        problems.append(f"declared in capability-coverage.json but NOT driven this run: {missing}")
    for extra in sorted(driven - declared):
        problems.append(f"driven this run but NOT declared in capability-coverage.json: {extra}")

    advertised = artefact.get("_denominator", {}).get("advertised_request_types")
    cross = set(artefact.get("cross_check_requests") or [])
    subjects = {s.split("/", 1)[1] for s in declared}
    print(
        f"coverage: {len(subjects)} gated request type(s) + {len(cross - subjects)} "
        f"cross-check-only = {len(subjects | cross)} of {advertised} advertised "
        f"({len(artefact.get('subjects', {}))} families gated)"
    )
    return problems


async def run_contract(url: str, password: str) -> bool:
    ws = await PulsarWs.connect(url, password)
    try:
        print("== General (calibration) ==")
        await drive_general(ws)
        print("== Scenes + Inputs (calibration) ==")
        await drive_scenes_and_inputs(ws)
        print("== SceneItems (geometry) ==")
        await drive_scene_items(ws)
        print("== Filters (ADR 026 §3.3 widening #2) ==")
        await drive_filters(ws)
        print("== Transitions ==")
        await drive_transitions(ws)
        # ORDER MATTERS (issue #131). drive_outputs gates the #127 by-name
        # StartOutput on the rtmp output REFUSING for want of a service; since
        # #131 the frontend binds `streamService` to that same output on the
        # first StartStream, and libobs keeps the binding afterwards. Driving
        # Outputs BEFORE Stream keeps both gates deterministic: no service is
        # bound yet here, and the Stream family below is what binds it.
        print("== Outputs, generic by-name (ADR 026 §3.3 widening #3, #127) ==")
        await drive_outputs(ws)
        print("== Stream (calibration) ==")
        await drive_stream(ws)
        print("== ReplayBuffer -- off-air arm (encoders idle, #117/#120) ==")
        offair = await probe_offair_arm(ws)
        print("== Record (brings the shared encoders up) ==")
        encoders_up = await drive_record(ws)
        print("== ReplayBuffer (suspect, B7) ==")
        await drive_replay_buffer(ws, offair, encoders_up)
        await finish_record(ws)
        print("== VirtualCam (ADR 026 §3.3 widening #1) ==")
        await drive_virtualcam(ws)
        print("== StudioMode (suspect, B7) ==")
        await drive_studio_mode(ws)
        print("== Canvases (suspect, B7) ==")
        await drive_canvases(ws)
        print("== cleanup (RemoveScene is a subject too) ==")
        await drive_cleanup(ws)
    finally:
        await ws.close()

    print()
    print("== Verdict summary ==")
    unexcused_no_effect = [r for r in RESULTS if r.verdict is Verdict.OK_NO_EFFECT and r.excused is None]
    for r in RESULTS:
        print(f"  {r.verdict.value:20s} {r.family}/{r.request_type}")
    print()
    coverage_problems = check_coverage()

    failed = False
    if unexcused_no_effect:
        failed = True
        print(f"FAIL: {len(unexcused_no_effect)} unexcused ok-mais-sans-effet finding(s):")
        for r in unexcused_no_effect:
            print(f"  - {r.family}/{r.request_type}: {r.detail}")
    if coverage_problems:
        failed = True
        print(f"FAIL: coverage drift vs {COVERAGE_ARTEFACT.name} ({len(coverage_problems)}):")
        for p in coverage_problems:
            print(f"  - {p}")
    if failed:
        return False
    print("PASS: no unexcused ok-mais-sans-effet finding, coverage matches the frozen list.")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar v5 capability contract probe (Probe/B7, CI gate #121)")
    ap.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    ap.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S)
    args = ap.parse_args()

    exe: pathlib.Path = args.exe
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        print("Build it first: scripts/build-win.ps1 -Full")
        return 2

    port = pick_free_port()
    password = secrets.token_urlsafe(16)
    record_dir = pathlib.Path(tempfile.mkdtemp(prefix="pulsar-contract-"))
    print(f"spawning: {exe}")
    print(f"PULSAR_RECORD_DIR={record_dir}")

    pulsar = PulsarProcess(exe, port, password, record_dir)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        ok = asyncio.run(run_contract(ws_url, sentinel_pw))
        rc = 0 if ok else 1
    except KeyboardInterrupt:
        print("interrupted")
        rc = 130
    except Exception as exc:  # noqa: BLE001 -- top-level probe diagnostic
        print(f"FAIL: {exc}")
        print(pulsar._diag())
        rc = 1
    finally:
        if rc != 0:
            # A red gate in CI is useless without the server's side of the
            # story; the classification alone does not say WHY libobs refused.
            print(pulsar._diag())
        pulsar.shutdown()
        if pulsar.proc is not None and pulsar.proc.poll() is None:
            print("error: pulsar.exe still running after shutdown attempt")
            rc = rc or 1
        else:
            print("pulsar.exe reaped cleanly")
        shutil.rmtree(record_dir, ignore_errors=True)

    print("PASS" if rc == 0 else f"FAILED (exit {rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
