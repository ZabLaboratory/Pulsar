#!/usr/bin/env python3
"""
Pulsar v5 capability contract probe (Probe/B7).

Thinker's report on Pulsar (`docs/adr` fanout, item B7) found that nothing
in the repo distinguishes a v5 request that is *served* (answers on the
wire, `requestStatus.result == true`) from one that is *functional* (its
promised effect is observable afterwards). `docs/PROTOCOL.md` advertises
"137 v5 request types" but no test walks that surface end to end; three
families were flagged by name as suspect: replay buffer, studio-mode
transition, canvases.

This probe is a CONTRACT test, not a smoke test. For every request it
drives it does three things:
  1. call the request:
  2. independently re-query the server for the state the request claims
     to change (a *different* request than the one under test, wherever
     one exists);
  3. classify the outcome:
       - OK              : requestStatus.result == True AND the
                            independent re-query shows the promised state
                            change.
       - ERROR_EXPLICIT   : requestStatus.result == False with a comment
                             (or a comparable clean refusal). Correct
                             behaviour for an unsupported/misconfigured
                             capability -- NOT a failure of this probe.
       - OK_NO_EFFECT      : requestStatus.result == True but the
                             independent re-query shows NO observable
                             change. This is the B7 failure mode.

Any OK_NO_EFFECT classification fails the probe UNLESS the request is
listed in KNOWN_OK_NO_EFFECT below, with a date and a reason. That list is
for capabilities where the no-effect behaviour is an already-documented,
deliberate protocol tradeoff (cf. PROTOCOL.md) -- it is NOT a place to
silence new findings. Every entry routes to Atlas/Eleven for a decision
(documented tradeoff vs bug) rather than being fixed here -- Probe writes
tests, it does not patch the app (docs/rules/agents.md).

Scope of this first pass (per Eleven's B7 brief):
  - calibration families already exercised by Prism in production: General,
    Scenes, Inputs, Stream.
  - suspect families named by Thinker: ReplayBuffer, StudioMode, Canvases.

Coverage is a SAMPLE of the 137 request types, not exhaustive -- see the
"Known gaps" note at the bottom of this file for what is intentionally
left out of this first pass and why.

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
import socket
import subprocess
import sys
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

READY_TIMEOUT_S = 60.0
READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
SHUTDOWN_GRACE_S = 8.0

# --------------------------------------------------------------------------
# Known, DATED, JUSTIFIED exceptions to "OK_NO_EFFECT fails the probe".
#
# Each entry is a decision hole, not a fix Probe is allowed to make (Probe
# writes tests only -- docs/rules/agents.md). Routed to Atlas/Eleven.
# --------------------------------------------------------------------------
KNOWN_OK_NO_EFFECT: dict[str, str] = {
    # PROTOCOL.md:59-66 already documents this as intentional wire behaviour
    # ("StartStream != go live"): obs_output_start() on an rtmp_output with
    # no reachable service returns true synchronously, the actual TCP
    # connect fails asynchronously afterwards. Kept OUT of this probe's own
    # StartStream case below by configuring a service first (matching how
    # Prism actually drives it) -- this entry exists only in case a future
    # run exercises the *documented* bare case and needs a name to point at.
    # 2026-07-26 (Probe/B7): decision to raise to Atlas is whether
    # `StartStream` should refuse synchronously instead of reporting
    # success for a request that is known-doomed at call time
    # (unconfigured/empty stream service). Left open, not fixed here.
    "StartStream:no-service-configured": (
        "2026-07-26 Probe/B7 -- documented in PROTOCOL.md:59-66 as "
        "intentional v5-baseline behaviour, upstream-shaped (obs-websocket "
        "StartStream mirrors obs_output_start's fire-and-forget contract). "
        "Decision (\"should this synchronously error instead\") not made -- "
        "routed to Atlas, not silenced."
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
# Process management -- same shape as probe-websocket.py.
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
    def __init__(self, exe: pathlib.Path, port: int, password: str) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.proc = None
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None
        self._pump_thread: Optional[threading.Thread] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password

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

    async def req(self, request_type: str, request_data: dict | None = None, timeout: float = 10) -> tuple[bool, dict, str | None]:
        """Returns (result, responseData, comment)."""
        self._req_counter += 1
        rid = f"contract-{self._req_counter}"
        payload: dict[str, Any] = {"requestType": request_type, "requestId": rid}
        if request_data is not None:
            payload["requestData"] = request_data
        await self.ws.send(json.dumps({"op": 6, "d": payload}))
        while True:
            resp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            if resp.get("op") != 7:
                continue  # skip events interleaved on the same socket
            d = resp["d"]
            if d.get("requestId") != rid:
                continue
            status = d["requestStatus"]
            return status.get("result", False), d.get("responseData", {}), status.get("comment")

    async def close(self) -> None:
        await self.ws.close(code=1000, reason="contract probe complete")


# --------------------------------------------------------------------------
# Family drivers. Each returns nothing; they call `record()` directly so a
# family can emit more than one verdict (request + independent re-query).
# --------------------------------------------------------------------------
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


async def drive_scenes_and_inputs(ws: PulsarWs) -> tuple[str, str]:
    """Calibration family: scenes + inputs, already used by Prism in prod.
    Returns (scene_a, scene_b) names for reuse by the studio-mode driver."""
    scene_a, scene_b = "ProbeContractSceneA", "ProbeContractSceneB"

    # NB (Probe/B7, 2026-07-26): if this comes back OK_NO_EFFECT, the root
    # cause is not on the wire -- it's in Pulsar's own frontend stub.
    # CreateScene succeeds and returns a real sceneUuid (obs_canvas_scene_
    # create() in RequestHandler_Scenes.cpp genuinely creates the libobs
    # scene source), but `GetSceneList` walks
    # PulsarFrontendAPI::obs_frontend_get_scenes(), which iterates the
    # stub's own `scenes` vector (plugins/pulsar-frontend-stub/src/
    # pulsar-frontend-stub.cpp:1213-1220) -- populated at collection load,
    # never appended to when a scene is created straight through libobs.
    # SetCurrentProgramScene still "works" afterwards because
    # obs_frontend_set_current_scene() takes the raw source pointer and
    # never checks stub membership. Net effect: a scene can be created and
    # even driven live, yet never show up in any listing request. This is
    # a calibration-family (Scenes) finding, not a suspect-family one --
    # worse than what Thinker's report named.
    ok, _, comment = await ws.req("CreateScene", {"sceneName": scene_a})
    ok2, _, _ = await ws.req("CreateScene", {"sceneName": scene_b})
    ok_list, list_data, _ = await ws.req("GetSceneList")
    names = {s["sceneName"] for s in list_data.get("scenes", [])}
    if ok and ok2 and scene_a in names and scene_b in names:
        record("Scenes", "CreateScene", Verdict.OK, f"both scenes present in GetSceneList ({len(names)} total)")
    elif not ok or not ok2:
        record("Scenes", "CreateScene", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("Scenes", "CreateScene", Verdict.OK_NO_EFFECT, f"result=True but scene(s) absent from GetSceneList: {names}")

    ok, _, comment = await ws.req("SetCurrentProgramScene", {"sceneName": scene_a})
    ok_g, cur, _ = await ws.req("GetCurrentProgramScene")
    if ok and ok_g and cur.get("sceneName") == scene_a:
        record("Scenes", "SetCurrentProgramScene", Verdict.OK, f"GetCurrentProgramScene reflects {scene_a}")
    elif not ok:
        record("Scenes", "SetCurrentProgramScene", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("Scenes", "SetCurrentProgramScene", Verdict.OK_NO_EFFECT, f"result=True but program scene is {cur.get('sceneName')!r}, not {scene_a!r}")

    ok, _, comment = await ws.req(
        "CreateInput",
        {
            "sceneName": scene_a,
            "inputName": "ProbeContractColorSource",
            "inputKind": "color_source_v3",
            "inputSettings": {"color": 4278190335},  # opaque magenta ABGR
        },
    )
    ok_list, inputs_data, _ = await ws.req("GetInputList")
    input_names = {i["inputName"] for i in inputs_data.get("inputs", [])}
    ok_items, items_data, _ = await ws.req("GetSceneItemList", {"sceneName": scene_a})
    item_sources = {i["sourceName"] for i in items_data.get("sceneItems", [])}
    if ok and "ProbeContractColorSource" in input_names and "ProbeContractColorSource" in item_sources:
        record("Inputs", "CreateInput", Verdict.OK, "input in GetInputList AND scene item in GetSceneItemList")
    elif not ok:
        record("Inputs", "CreateInput", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record(
            "Inputs",
            "CreateInput",
            Verdict.OK_NO_EFFECT,
            f"result=True but not fully wired: in inputs={('ProbeContractColorSource' in input_names)}, in scene items={('ProbeContractColorSource' in item_sources)}",
        )

    return scene_a, scene_b


async def drive_stream(ws: PulsarWs) -> None:
    """Calibration family, PROTOCOL.md's own documented caveat (StartStream
    != go live) is sidestepped by configuring a service first -- matching
    how Prism actually drives this in production."""
    ok, _, comment = await ws.req(
        "SetStreamServiceSettings",
        {"streamServiceType": "rtmp_custom", "streamServiceSettings": {"server": "rtmp://127.0.0.1:1/probe", "key": "x"}},
    )
    if not ok:
        record("Stream", "SetStreamServiceSettings", Verdict.ERROR_EXPLICIT, f"comment={comment}")
        return

    ok_start, _, comment = await ws.req("StartStream")
    await asyncio.sleep(0.5)
    ok_status, status_data, _ = await ws.req("GetStreamStatus")
    active = status_data.get("outputActive")
    if ok_start and active:
        record("Stream", "StartStream", Verdict.OK, "GetStreamStatus.outputActive == True after StartStream (service configured)")
    elif not ok_start:
        record("Stream", "StartStream", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record(
            "Stream",
            "StartStream",
            Verdict.OK_NO_EFFECT,
            f"result=True but outputActive={active} even with a service configured",
        )
    # cleanup regardless of branch above
    await ws.req("StopStream")


async def drive_replay_buffer(ws: PulsarWs) -> None:
    """Suspect family (Thinker B7)."""
    ok0, status0, _ = await ws.req("GetReplayBufferStatus")
    active0 = status0.get("outputActive")

    ok_start, _, comment = await ws.req("StartReplayBuffer")
    await asyncio.sleep(0.5)
    ok1, status1, _ = await ws.req("GetReplayBufferStatus")
    active1 = status1.get("outputActive")

    if not ok_start:
        record("ReplayBuffer", "StartReplayBuffer", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    elif active1 and not active0:
        record("ReplayBuffer", "StartReplayBuffer", Verdict.OK, "GetReplayBufferStatus.outputActive flips False->True")
    else:
        record(
            "ReplayBuffer",
            "StartReplayBuffer",
            Verdict.OK_NO_EFFECT,
            f"result=True but outputActive before={active0} after={active1} (no observable state change)",
        )

    if active1:
        ok_save, save_data, comment = await ws.req("SaveReplayBuffer")
        await asyncio.sleep(0.3)
        ok_last, last_data, _ = await ws.req("GetLastReplayBufferReplay")
        saved_path = last_data.get("savedReplayPath")
        if ok_save and ok_last and saved_path:
            record("ReplayBuffer", "SaveReplayBuffer", Verdict.OK, f"GetLastReplayBufferReplay.savedReplayPath={saved_path!r}")
        elif not ok_save:
            record("ReplayBuffer", "SaveReplayBuffer", Verdict.ERROR_EXPLICIT, f"comment={comment}")
        else:
            record(
                "ReplayBuffer",
                "SaveReplayBuffer",
                Verdict.OK_NO_EFFECT,
                f"result=True but GetLastReplayBufferReplay never surfaced a path (save_ok={ok_save}, last_ok={ok_last}, path={saved_path!r})",
            )
        await ws.req("StopReplayBuffer")
    else:
        record(
            "ReplayBuffer",
            "SaveReplayBuffer",
            Verdict.OK_NO_EFFECT,
            "skipped exercising SaveReplayBuffer for real: the buffer never actually started (see StartReplayBuffer verdict above) -- calling Save on a buffer the server itself never activated would just double-count the same root cause",
        )


async def drive_studio_mode(ws: PulsarWs, scene_a: str, scene_b: str) -> None:
    """Suspect family (Thinker B7). Uses Cut transition so the swap is
    instantaneous and deterministic for the probe (no fade-duration wait)."""
    await ws.req("SetCurrentSceneTransition", {"transitionName": "Cut"})

    ok_en, _, comment = await ws.req("SetStudioModeEnabled", {"studioModeEnabled": True})
    ok_g, g_data, _ = await ws.req("GetStudioModeEnabled")
    if ok_en and ok_g and g_data.get("studioModeEnabled") is True:
        record("StudioMode", "SetStudioModeEnabled", Verdict.OK, "GetStudioModeEnabled reflects True")
    elif not ok_en:
        record("StudioMode", "SetStudioModeEnabled", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    else:
        record("StudioMode", "SetStudioModeEnabled", Verdict.OK_NO_EFFECT, f"result=True but GetStudioModeEnabled={g_data}")

    # Program is scene_a (set by drive_scenes_and_inputs). Preview scene_b,
    # then trigger the transition -- program should become scene_b.
    await ws.req("SetCurrentProgramScene", {"sceneName": scene_a})
    ok_prev, _, comment = await ws.req("SetCurrentPreviewScene", {"sceneName": scene_b})
    ok_before, before_data, _ = await ws.req("GetCurrentProgramScene")

    ok_trig, _, comment_trig = await ws.req("TriggerStudioModeTransition")
    await asyncio.sleep(0.5)
    ok_after, after_data, _ = await ws.req("GetCurrentProgramScene")

    if not ok_prev:
        record("StudioMode", "SetCurrentPreviewScene", Verdict.ERROR_EXPLICIT, f"comment={comment}")
    if not ok_trig:
        record("StudioMode", "TriggerStudioModeTransition", Verdict.ERROR_EXPLICIT, f"comment={comment_trig}")
    elif ok_after and after_data.get("sceneName") == scene_b and before_data.get("sceneName") != scene_b:
        record("StudioMode", "TriggerStudioModeTransition", Verdict.OK, f"program scene flips {before_data.get('sceneName')!r} -> {scene_b!r}")
    else:
        record(
            "StudioMode",
            "TriggerStudioModeTransition",
            Verdict.OK_NO_EFFECT,
            f"result=True but program scene before={before_data.get('sceneName')!r} after={after_data.get('sceneName')!r} (preview was {scene_b!r})",
        )

    await ws.req("SetStudioModeEnabled", {"studioModeEnabled": False})


async def drive_canvases(ws: PulsarWs) -> None:
    """Suspect family (Thinker B7). Cross-checks GetCanvasList against an
    independently-fetched GetVideoSettings -- a served-but-empty canvas
    list would either be empty or carry dimensions disconnected from the
    real, boot-configured video pipeline."""
    ok_v, video_data, _ = await ws.req("GetVideoSettings")
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
        record("Canvases", "GetCanvasList", Verdict.OK, f"main canvas dimensions match GetVideoSettings ({main})")
    else:
        record(
            "Canvases",
            "GetCanvasList",
            Verdict.OK_NO_EFFECT,
            f"result=True with a non-empty list, but canvas entry does not correlate to GetVideoSettings: canvas={main} video={video_data}",
        )


async def cleanup(ws: PulsarWs, scene_a: str, scene_b: str) -> None:
    await ws.req("RemoveInput", {"inputName": "ProbeContractColorSource"})
    await ws.req("RemoveScene", {"sceneName": scene_a})
    await ws.req("RemoveScene", {"sceneName": scene_b})


async def run_contract(url: str, password: str) -> bool:
    ws = await PulsarWs.connect(url, password)
    try:
        print("== General (calibration) ==")
        await drive_general(ws)
        print("== Scenes + Inputs (calibration) ==")
        scene_a, scene_b = await drive_scenes_and_inputs(ws)
        print("== Stream (calibration) ==")
        await drive_stream(ws)
        print("== ReplayBuffer (suspect, B7) ==")
        await drive_replay_buffer(ws)
        print("== StudioMode (suspect, B7) ==")
        await drive_studio_mode(ws, scene_a, scene_b)
        print("== Canvases (suspect, B7) ==")
        await drive_canvases(ws)
        print("== cleanup ==")
        await cleanup(ws, scene_a, scene_b)
    finally:
        await ws.close()

    print()
    print("== Verdict summary ==")
    unexcused_no_effect = [r for r in RESULTS if r.verdict is Verdict.OK_NO_EFFECT and r.excused is None]
    for r in RESULTS:
        print(f"  {r.verdict.value:20s} {r.family}/{r.request_type}")
    print()
    if unexcused_no_effect:
        print(f"FAIL: {len(unexcused_no_effect)} unexcused ok-mais-sans-effet finding(s):")
        for r in unexcused_no_effect:
            print(f"  - {r.family}/{r.request_type}: {r.detail}")
        return False
    print("PASS: no unexcused ok-mais-sans-effet findings.")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar v5 capability contract probe (Probe/B7)")
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
    print(f"spawning: {exe}")

    pulsar = PulsarProcess(exe, port, password)
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
        rc = 1
    finally:
        pulsar.shutdown()
        if pulsar.proc is not None and pulsar.proc.poll() is None:
            print("error: pulsar.exe still running after shutdown attempt")
            rc = rc or 1
        else:
            print("pulsar.exe reaped cleanly")

    print("PASS" if rc == 0 else f"FAILED (exit {rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())

# --------------------------------------------------------------------------
# Known gaps in this first pass (report these to Eleven/Vigil, not silently
# left implicit):
#
#   - Coverage is ~15 of 137 advertised request types. Untouched: Filters,
#     Outputs (generic, beyond Stream/Record/ReplayBuffer), Profiles/Scene
#     Collections, Hotkeys, VirtualCam, MediaInput cursor control, Record
#     pause/resume/split, SceneItem transform/index/blend-mode geometry.
#   - "Canvases" as tested here only proves the SINGLE default canvas
#     correlates with GetVideoSettings. Pulsar's v5 baseline (obs-websocket
#     upstream, single-canvas OBS build) does not expose CreateCanvas /
#     RemoveCanvas at all -- multi-canvas is not testable through this
#     surface at all, which is itself worth a decision (is multi-canvas an
#     advertised capability anywhere in Pulsar's docs? docs/PROTOCOL.md does
#     not claim it -- if no doc claims it, this is not a B7 finding).
#   - VirtualCam (StartVirtualCam/StopVirtualCam/GetVirtualCamStatus) is
#     exactly the same shape of risk as ReplayBuffer (a device-backed output
#     that may report success without a real effect) and was NOT covered in
#     this pass purely for time -- flagged here rather than silently
#     skipped.
# --------------------------------------------------------------------------
