#!/usr/bin/env python3
"""
Pulsar virtual-cam SOURCE-mode probe — issue #119 resolution criterion 3.

#119 dropped the frontend stub's scene MIRROR: obs_frontend_get_scenes now
enumerates libobs instead of the stub's internal `scenes` vector. Criterion 3
of that issue is a NON-REGRESSION clause, not a fix: "the virtual cam in
source mode (VCAM_SCENE) behaves as before".

That path never went through obs_frontend_get_scenes at all — the stub
resolves it with obs_get_source_by_name("ZabVirtualCamSource")
(pulsar-frontend-stub.cpp:1605) and hands the resulting scene to a dedicated
obs_view / video mix. Reasoning says the mirror removal cannot touch it. This
probe stops reasoning and exercises it.

It also closes the loop the mirror used to hide: before #119 a scene created
over the WebSocket (CreateScene) was invisible to GetSceneList while being
perfectly findable by obs_get_source_by_name. So a Prism-created
`ZabVirtualCamSource` DID drive the cam while being absent from every scene
listing — two views of the same libobs state disagreeing. Post-#119 both
agree, and this probe asserts that pair.

Sequence:

  1. GetVirtualCamStatus. libobs only registers the `virtualcam_output` type
     when a virtual-camera DirectShow filter is registered on the machine
     (upstream/plugins/win-dshow/dshow-plugin.cpp:48, vcam_installed(false) —
     the 32-bit COM view of CLSID_OBS_VirtualVideo). Without it the request
     answers InvalidResourceState and the probe exits 3 (TYPED SKIP): the
     criterion is untestable on that box, and we say so rather than pretend.
  2. CreateScene("ZabVirtualCamSource") + a color_source item, so the cam
     carries something renderable.
  3. GetSceneList must list it (#119 criterion 1/2, on the very scene name
     the vcam path resolves).
  4. SetCurrentProgramScene to a DIFFERENT scene, so a cam that silently fell
     back to the program mix would not be carrying our scene.
  5. StartVirtualCam must succeed AND GetVirtualCamStatus.outputActive must
     be true (the #120 rule), AND the stub must have logged
     "virtual cam SOURCE mode -> 'ZabVirtualCamSource'" — the observable
     proof that obs_get_source_by_name resolved and the dedicated view was
     wired, rather than the default obs_get_video() mix.
     If the start is REFUSED, the two legs are split: the source-mode log
     must still be there (the resolve happens before the device is touched,
     and it is the only half #119 could have broken) — if it is, the probe
     exits 3 with the device as the named reason; if it is not, that is a
     real failure.
  6. StopVirtualCam must succeed and the cam must really be inactive.

LICENSE INVARIANT (LICENSE-INVARIANTS.md #1/#2/#3): the probe talks to Pulsar
over the WebSocket process boundary ONLY. It spawns pulsar.exe as a separate
OS process and exchanges nothing but obs-websocket v5 frames. No FFI, no
ctypes, no LoadLibrary of any Pulsar/obs DLL.

Exit codes: 0 pass, 1 fail, 2 usage, 3 typed skip (no vcam driver).

Usage (from the repo root, against the built rundir):
    pip install websockets
    python scripts/probe-vcam-scene-mode.py
    python scripts/probe-vcam-scene-mode.py --exe /path/to/pulsar.exe
"""
from __future__ import annotations

import argparse
import asyncio
import base64
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
from typing import Optional

try:
    import websockets
except ImportError:
    print("error: pip install websockets (pure WS client — no native deps)")
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

READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0

STATUS_INVALID_RESOURCE_STATE = 604

# The scene name the frontend stub looks up by name to drive the cam
# (pulsar-frontend-stub.cpp:1605). Not configurable — it is the contract.
VCAM_SCENE = "ZabVirtualCamSource"
DECOY_SCENE = "ProbeProgramScene"

# The stub's own log line on the source-mode branch
# (pulsar-frontend-stub.cpp:1615-1616). Its absence means the cam fell back
# to the program mix.
SOURCE_MODE_LOG = "virtual cam SOURCE mode"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_SKIP = 3


class Failure(Exception):
    pass


class Skip(Exception):
    pass


# libobs log lines carry the OS locale and the diagnostics quote them
# verbatim; on a cp1252 console that would turn a FAILURE into an
# UnicodeEncodeError traceback and hide the assertion that fired.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------
# Process management -- mirrors probe-output-effect.py PulsarProcess.
# --------------------------------------------------------------------------
class PulsarProcess:
    def __init__(self, exe: pathlib.Path, port: int, password: str) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.proc: Optional[subprocess.Popen] = None
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)

        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

        self.proc = subprocess.Popen(
            [str(self.exe)],
            cwd=str(self.exe.parent),  # MANDATORY: libobs resolves data/ from cwd
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # libobs' default log handler writes to stderr
            stdin=subprocess.DEVNULL,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()

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
                raise Failure(
                    f"pulsar.exe exited (code {self.proc.returncode}) before READY.\n" + self.diag()
                )
            if time.monotonic() >= deadline:
                raise Failure(f"pulsar.exe did not signal READY within {timeout:.0f}s.\n" + self.diag())

    def find_log(self, needle: str) -> Optional[str]:
        for line in list(self._lines):
            if needle in line:
                return line
        return None

    def diag(self) -> str:
        tail = self._lines[-40:]
        body = "\n".join(f"  | {ln}" for ln in tail) if tail else "  | (no output)"
        return f"--- pulsar stdout/stderr (last {len(tail)} lines) ---\n{body}"

    def shutdown(self, grace: float = SHUTDOWN_GRACE_S) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        for step in (self.proc.terminate, self.proc.kill):
            try:
                step()
                self.proc.wait(timeout=grace)
                return
            except Exception:
                continue


# --------------------------------------------------------------------------
# obs-websocket v5 client.
# --------------------------------------------------------------------------
def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


class Client:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._n = 0

    async def req(self, request_type: str, data: dict | None = None, timeout: float = 15.0):
        """Returns (ok, code, comment, responseData)."""
        self._n += 1
        rid = f"probe-{self._n}"
        body: dict = {"requestType": request_type, "requestId": rid}
        if data is not None:
            body["requestData"] = data

        await self.ws.send(json.dumps({"op": 6, "d": body}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("op") != 7:
                continue
            d = msg["d"]
            if d.get("requestId") != rid:
                continue
            status = d.get("requestStatus") or {}
            return (
                bool(status.get("result")),
                status.get("code"),
                status.get("comment"),
                d.get("responseData") or {},
            )

    async def must(self, request_type: str, data: dict | None = None) -> dict:
        ok, code, comment, response = await self.req(request_type, data)
        if not ok:
            raise Failure(f"{request_type} failed: {code} {comment}")
        return response


async def connect(url: str, password: str) -> Client:
    ws = await websockets.connect(url, subprotocols=["obswebsocket.json"], open_timeout=10)
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    if hello.get("op") != 0:
        raise Failure(f"expected Hello (op=0), got {hello}")
    identify: dict = {"rpcVersion": hello["d"]["rpcVersion"], "eventSubscriptions": 0}
    if "authentication" in hello["d"]:
        a = hello["d"]["authentication"]
        identify["authentication"] = compute_auth(password, a["salt"], a["challenge"])
    await ws.send(json.dumps({"op": 1, "d": identify}))
    ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    if ident.get("op") != 2:
        raise Failure(f"identify failed: {ident}")
    return Client(ws)


# --------------------------------------------------------------------------
# The criterion.
# --------------------------------------------------------------------------
async def scene_names(c: Client) -> list[str]:
    data = await c.must("GetSceneList")
    return [s.get("sceneName") for s in data.get("scenes", [])]


async def ensure_scene(c: Client, name: str) -> None:
    ok, code, comment, _ = await c.req("CreateScene", {"sceneName": name})
    if not ok and "already exists" not in str(comment).lower():
        raise Failure(f"CreateScene({name}) failed: {code} {comment}")


async def run(c: Client, proc: PulsarProcess) -> None:
    # 1. Is the criterion testable at all on this box?
    ok, code, comment, _ = await c.req("GetVirtualCamStatus")
    if not ok:
        if code == STATUS_INVALID_RESOURCE_STATE:
            raise Skip(
                "no virtual-camera driver registered on this machine, so libobs never registered "
                f"the `virtualcam_output` type ({comment})"
            )
        raise Failure(f"GetVirtualCamStatus failed unexpectedly: {code} {comment}")
    print("   OK  virtualcam_output is registered -- the criterion is testable here")

    # 2/3. A scene created over the WIRE must be visible to the wire.
    await ensure_scene(c, VCAM_SCENE)
    await ensure_scene(c, DECOY_SCENE)

    names = await scene_names(c)
    if VCAM_SCENE not in names:
        raise Failure(
            f"GetSceneList does not list {VCAM_SCENE!r} after CreateScene (got {names!r}) -- "
            "the #119 mirror is back"
        )
    if DECOY_SCENE not in names:
        raise Failure(f"GetSceneList does not list {DECOY_SCENE!r} after CreateScene (got {names!r})")
    print(f"   OK  GetSceneList lists both wire-created scenes: {names!r}")

    # Something renderable in the cam scene, so source mode is not carrying a
    # void. color_source is built into libobs -- present on a light build too.
    ok, code, comment, _ = await c.req(
        "CreateInput",
        {
            "sceneName": VCAM_SCENE,
            "inputName": "ProbeVCamFill",
            "inputKind": "color_source_v3",
            "inputSettings": {"color": 0xFF0000FF, "width": 1920, "height": 1080},
        },
    )
    if not ok:
        # Kind name differs across libobs versions; fall back, then give up
        # quietly -- an empty scene still exercises the resolve path.
        ok, code, comment, _ = await c.req(
            "CreateInput",
            {
                "sceneName": VCAM_SCENE,
                "inputName": "ProbeVCamFill",
                "inputKind": "color_source",
                "inputSettings": {"color": 0xFF0000FF, "width": 1920, "height": 1080},
            },
        )
    print(f"   ..  cam scene fill: {'created' if ok else f'skipped ({code} {comment})'}")

    # 4. Program shows something ELSE, so a fallback to the program mix is
    #    distinguishable from real source mode.
    await c.must("SetCurrentProgramScene", {"sceneName": DECOY_SCENE})
    current = await c.must("GetCurrentProgramScene")
    program = current.get("sceneName") or current.get("currentProgramSceneName")
    if program != DECOY_SCENE:
        raise Failure(f"program scene is {program!r}, expected {DECOY_SCENE!r}")
    print(f"   OK  program scene is {program!r} (NOT the cam scene)")

    # 5. Start, and demand the effect + the source-mode evidence.
    ok, code, comment, _ = await c.req("StartVirtualCam")
    if not ok:
        # `GetVirtualCamStatus` answering means the stub HOLDS a vcam output
        # handle -- it does not mean libobs can open the DirectShow device on
        # this machine (a CI runner registers no camera at all: libobs logs
        # "Output ID 'virtualcam_output' not found" and the start is declined
        # with no cause of its own).
        #
        # Split the two honestly. The #119-relevant half of the criterion runs
        # BEFORE the device is ever touched: the stub resolves
        # obs_get_source_by_name(VCAM_SCENE) and wires the dedicated obs_view,
        # which it announces in the log. If that happened, the mirror removal
        # is proven not to have broken the resolve, and only the device leg is
        # untestable here -> typed skip. If it did NOT happen, the resolve is
        # what broke, and that is a real failure.
        hit = proc.find_log(SOURCE_MODE_LOG)
        if hit is None:
            raise Failure(
                f"StartVirtualCam was refused ({code} {comment}) AND the stub never logged "
                f"{SOURCE_MODE_LOG!r}: obs_get_source_by_name did not resolve {VCAM_SCENE!r} "
                "-- the source-mode resolve itself is broken, not just the device\n"
                f"{proc.diag()}"
            )
        if VCAM_SCENE not in hit:
            raise Failure(f"source-mode log names the wrong scene: {hit!r}")
        raise Skip(
            f"the source-mode resolve DID happen ({hit.strip()!r}), but libobs could not open the "
            f"virtual camera device on this machine: StartVirtualCam -> {code} {comment}"
        )

    status = await c.must("GetVirtualCamStatus")
    if not status.get("outputActive"):
        raise Failure(
            "StartVirtualCam: success but GetVirtualCamStatus.outputActive is false "
            "-- the #120 defect verbatim"
        )
    print("   OK  StartVirtualCam succeeded AND the cam is genuinely active")

    hit = proc.find_log(SOURCE_MODE_LOG)
    if hit is None:
        raise Failure(
            f"the stub never logged {SOURCE_MODE_LOG!r}: the cam started on the DEFAULT program mix, "
            f"not on the {VCAM_SCENE!r} scene -- obs_get_source_by_name did not resolve it\n"
            f"{proc.diag()}"
        )
    if VCAM_SCENE not in hit:
        raise Failure(f"source-mode log names the wrong scene: {hit!r}")
    print(f"   OK  source mode confirmed by the stub: {hit.strip()!r}")

    # 6. And it stops for real.
    ok, code, comment, _ = await c.req("StopVirtualCam")
    if not ok:
        raise Failure(f"StopVirtualCam failed on an active cam: {code} {comment}")
    status = await c.must("GetVirtualCamStatus")
    if status.get("outputActive"):
        raise Failure("StopVirtualCam: success but the cam is still active")
    print("   OK  StopVirtualCam succeeded AND the cam is genuinely inactive")


# --------------------------------------------------------------------------
# Orchestration.
# --------------------------------------------------------------------------
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def drive(exe: pathlib.Path) -> None:
    proc = PulsarProcess(exe, free_port(), secrets.token_urlsafe(16))
    proc.spawn()
    try:
        url, pw = proc.wait_ready(READY_TIMEOUT_S)
        client = await connect(url, pw)
        try:
            await run(client, proc)
        finally:
            await client.ws.close()
    except Failure as exc:
        raise Failure(f"{exc}\n{proc.diag()}") from None
    finally:
        proc.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar vcam source-mode probe (#119 criterion 3)")
    ap.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    args = ap.parse_args()

    if not args.exe.is_file():
        print(f"error: pulsar.exe not found at {args.exe} -- build it first (scripts/build-win.ps1)")
        return EXIT_USAGE

    print("=== #119 criterion 3 -- virtual cam SOURCE mode after the scene-mirror removal")
    try:
        asyncio.run(drive(args.exe))
    except Skip as exc:
        print(f"\nSKIP: {exc}")
        print(
            "To exercise the DEVICE leg of this criterion, the box needs a working virtual-camera\n"
            "DirectShow filter:\n"
            "  1. install OBS Studio for Windows WITH the 'Virtual Camera' component -- its\n"
            "     installer regsvr32's obs-virtualcam-module32.dll AND ...module64.dll, which\n"
            "     registers CLSID_OBS_VirtualVideo {A3FCE0F5-3493-419F-958A-ABA1250EC20B};\n"
            "  2. libobs gates the whole `virtualcam_output` type on the 32-BIT COM view of that\n"
            "     key (HKCR\\WOW6432Node\\CLSID), see\n"
            "     upstream/plugins/win-dshow/dshow-plugin.cpp:48 -- a 64-bit-only registration is\n"
            "     not enough;\n"
            "  3. the filter DLLs must still be on disk at the registered path: an uninstall that\n"
            "     leaves the key behind gives exactly the half-state a CI runner shows\n"
            "     (\"Output ID 'virtualcam_output' not found\" and a start declined with no cause).\n"
            "A headless CI runner has none of this. The SCENE-RESOLVE leg of the criterion (the\n"
            "part #119 could actually have broken) is asserted above regardless."
        )
        return EXIT_SKIP
    except Failure as exc:
        print(f"\nFAIL: {exc}")
        return EXIT_FAIL

    print("\nprobe-vcam-scene-mode: PASS -- VCAM_SCENE source mode intact after #119")
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
