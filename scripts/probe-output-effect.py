#!/usr/bin/env python3
"""
Pulsar output-effect probe — issue #120 / ADR Prism 026 §3.2.

Proves the rule "no Success() without a verified effect" on the four output
families: replay buffer, record, virtualcam, stream.

Before #120, every Start*/Stop* request called a `void` obs-frontend-api
entry point and returned Success() unconditionally. libobs declines silently
on an unconfigured output, so the client was told "started" while the very
next GetXStatus reported outputActive:false.

This probe drives a REAL refusal in each family and asserts the request now
answers with an explicit error carrying the cause — and, just as important,
that the legitimate paths still succeed and stay fast.

Cases (each one is a genuine refusal on the spawned binary, not a mock):

  A. Replay buffer — since #117 the stub DOES attach the shared encoders to
     `PulsarReplay`, and refuses to arm the buffer off-air: the buffer borrows
     the live stream/record encoders, so with nothing broadcasting they are
     attached but idle (ADR Prism 024 §3.1, "pas de replay hors antenne").
     Child 1 has no service bound and an uncreatable record dir, so nothing
     can be live — the off-air refusal is deterministic.
     That refusal is taken before `obs_output_start`, so libobs records no
     cause of its own; #117 publishes it via obs_output_set_last_error so the
     #120 verification names it instead of falling back to a generic
     "the output is not configured".
     Assert: StartReplayBuffer -> result:false, the comment names the idle
     encoders (not a generic message), and GetReplayBufferStatus still
     reports outputActive:false (the old bug was success + false).

  B. Stream — the singleton `PulsarStream` rtmp_output has no service bound,
     so obs_output_start bails before taking the action.
     Assert: StartStream -> result:false.

  C. Record — the child is spawned with PULSAR_RECORD_DIR pointing at an
     uncreatable path, so the stub's mkdir fails and it never reaches
     obs_output_start.
     Assert: StartRecord -> result:false.
     Then PULSAR_RECORD_DIR is valid in the second child (case E).

  D. VirtualCam — asserted only as "never a bare Success on a cam that did
     not start": either the output type is absent (InvalidResourceState, the
     pre-existing guard) or the start is refused. If the runner DOES have a
     working virtual cam, the request must succeed AND
     GetVirtualCamStatus.outputActive must be true. All three outcomes are
     self-consistent; "success + inactive" is the only failure.

  E. Positive control + latency bound (Resolution criterion 3), in a second
     child with a writable record dir: StartRecord succeeds AND
     GetRecordStatus.outputActive is true, StopRecord succeeds, and no
     start/stop request took longer than MAX_REQUEST_MS. The verification
     must not have turned any request into a wait for activation.

LICENSE INVARIANT (LICENSE-INVARIANTS.md #1/#2/#3): the probe talks to
Pulsar over the WebSocket process boundary ONLY. It spawns pulsar.exe as a
separate OS process and exchanges nothing but obs-websocket v5 frames. No
FFI, no ctypes, no LoadLibrary of any Pulsar/obs DLL.

Usage (from the repo root, against the built rundir):
    pip install websockets
    python scripts/probe-output-effect.py
    python scripts/probe-output-effect.py --exe /path/to/pulsar.exe
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
import tempfile
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

# v5 status codes reused by the verification (no new enum -- PROTOCOL.md).
STATUS_OUTPUT_RUNNING = 500
STATUS_OUTPUT_NOT_RUNNING = 501
STATUS_INVALID_RESOURCE_STATE = 604

# Resolution criterion 3: start/stop stay bounded and short. The server-side
# poll is capped at PULSAR_OUTPUT_VERIFY_MS (250 ms default); this budget
# leaves generous room for WS round-trip + CI runner jitter while still
# failing loudly if a request ever became a wait for activation.
MAX_REQUEST_MS = 2500

# A path that cannot be created as a directory on either OS: a component of
# it is an existing regular file. Forces the stub's create_directories to
# fail, so obs_output_start is never reached -- a real record refusal.
UNCREATABLE_DIR_LEAF = "not-a-dir"


class Failure(Exception):
    pass


# --------------------------------------------------------------------------
# Process management -- mirrors probe-record-m2.py PulsarProcess.
# --------------------------------------------------------------------------
class PulsarProcess:
    def __init__(self, exe: pathlib.Path, port: int, password: str, record_dir: str) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.record_dir = record_dir
        self.proc: Optional[subprocess.Popen] = None
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        env["PULSAR_RECORD_DIR"] = self.record_dir
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)
        # Leave PULSAR_OUTPUT_VERIFY_MS unset: the probe asserts the SHIPPED
        # default bound, not a tuned one.
        env.pop("PULSAR_OUTPUT_VERIFY_MS", None)

        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

        self.proc = subprocess.Popen(
            [str(self.exe)],
            cwd=str(self.exe.parent),  # MANDATORY: libobs resolves data/ from cwd
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
# obs-websocket v5 client (request/response only -- no events needed here).
# --------------------------------------------------------------------------
def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


class Client:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._n = 0

    async def req(self, request_type: str, data: dict | None = None, timeout: float = 15.0):
        """Returns (ok, code, comment, responseData, elapsed_ms)."""
        self._n += 1
        rid = f"probe-{self._n}"
        body: dict = {"requestType": request_type, "requestId": rid}
        if data is not None:
            body["requestData"] = data

        started = time.monotonic()
        await self.ws.send(json.dumps({"op": 6, "d": body}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("op") != 7:
                continue
            d = msg["d"]
            if d.get("requestId") != rid:
                continue
            elapsed_ms = (time.monotonic() - started) * 1000.0
            status = d.get("requestStatus") or {}
            return (
                bool(status.get("result")),
                status.get("code"),
                status.get("comment"),
                d.get("responseData") or {},
                elapsed_ms,
            )


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
# Assertions.
# --------------------------------------------------------------------------
LATENCIES: list[tuple[str, float]] = []


def note_latency(name: str, elapsed_ms: float) -> None:
    LATENCIES.append((name, elapsed_ms))


def require_explicit_error(
    name: str,
    ok: bool,
    code,
    comment,
    expected_code: int,
    cause_hint: str | tuple[str, ...] | None = None,
) -> None:
    """The heart of #120: the request must FAIL, with the right code and a
    comment carrying the cause. A bare Success is the regression we fence;
    a bare code with no cause is the other half of the same defect."""
    if ok:
        raise Failure(
            f"{name}: reported SUCCESS on an action that could not have taken effect "
            f"(this is exactly the #120 defect: Success() without verification)"
        )
    if code != expected_code:
        raise Failure(f"{name}: failed with code {code}, expected {expected_code}")
    if not comment or not str(comment).strip():
        raise Failure(f"{name}: failed with no comment -- the cause must be carried, never a bare code")
    if cause_hint is not None:
        hints = (cause_hint,) if isinstance(cause_hint, str) else cause_hint
        missing = [h for h in hints if h.lower() not in str(comment).lower()]
        if missing:
            raise Failure(
                f"{name}: comment {comment!r} does not name the actual cause "
                f"(expected it to mention {missing!r}) -- generic messages are not a cause"
            )
    print(f"   OK  {name} -> error {code}: {comment}")


async def case_replay(c: Client) -> None:
    print("-- A. replay buffer (encoders attached but idle off-air, #117)")
    ok, code, comment, _, ms = await c.req("StartReplayBuffer")
    note_latency("StartReplayBuffer", ms)
    if ok:
        # Nothing is live in this child, so #117 must refuse. If a future
        # revision ever lets it start, the effect still has to be real.
        _, _, _, status, _ = await c.req("GetReplayBufferStatus")
        if not status.get("outputActive"):
            raise Failure(
                "StartReplayBuffer: success but GetReplayBufferStatus.outputActive is false "
                "-- the #120 defect verbatim"
            )
        print("   OK  StartReplayBuffer succeeded AND the buffer is genuinely active")
        await c.req("StopReplayBuffer")
        return
    # #117 refuses off-air: the encoders are attached but idle. That is the
    # cause the error must name -- read off the output (obs_output_get_last_error,
    # set by the stub at the point of refusal), never a generic failure.
    require_explicit_error(
        "StartReplayBuffer", ok, code, comment, STATUS_OUTPUT_NOT_RUNNING, ("encoder", "idle")
    )

    _, _, _, status, _ = await c.req("GetReplayBufferStatus")
    if status.get("outputActive"):
        raise Failure("GetReplayBufferStatus reports active after a refused start -- inconsistent")
    print("   OK  GetReplayBufferStatus agrees: outputActive=false")

    # Stop on a buffer that never started keeps its pre-existing guard.
    ok, code, _, _, ms = await c.req("StopReplayBuffer")
    note_latency("StopReplayBuffer", ms)
    if ok or code != STATUS_OUTPUT_NOT_RUNNING:
        raise Failure(f"StopReplayBuffer on an idle buffer: ok={ok} code={code}, expected 501")
    print("   OK  StopReplayBuffer -> 501 (idle guard intact)")


async def case_stream(c: Client) -> None:
    print("-- B. stream (singleton rtmp_output, no service bound)")
    ok, code, comment, _, ms = await c.req("StartStream")
    note_latency("StartStream", ms)
    if ok:
        _, _, _, status, _ = await c.req("GetStreamStatus")
        if not status.get("outputActive"):
            raise Failure(
                "StartStream: success but GetStreamStatus.outputActive is false -- the #120 defect verbatim"
            )
        print("   OK  StartStream succeeded AND the output is genuinely active")
        await c.req("StopStream")
        return
    require_explicit_error("StartStream", ok, code, comment, STATUS_OUTPUT_NOT_RUNNING, "service")


async def case_record_refused(c: Client) -> None:
    print("-- C. record (PULSAR_RECORD_DIR uncreatable)")
    ok, code, comment, _, ms = await c.req("StartRecord")
    note_latency("StartRecord(refused)", ms)
    require_explicit_error("StartRecord", ok, code, comment, STATUS_OUTPUT_NOT_RUNNING)

    _, _, _, status, _ = await c.req("GetRecordStatus")
    if status.get("outputActive"):
        raise Failure("GetRecordStatus reports active after a refused start -- inconsistent")
    print("   OK  GetRecordStatus agrees: outputActive=false")


async def case_virtualcam(c: Client) -> None:
    print("-- D. virtualcam")
    ok, code, comment, _, ms = await c.req("StartVirtualCam")
    note_latency("StartVirtualCam", ms)
    if not ok:
        if code not in (STATUS_OUTPUT_NOT_RUNNING, STATUS_INVALID_RESOURCE_STATE):
            raise Failure(f"StartVirtualCam: unexpected failure code {code} ({comment})")
        if not comment or not str(comment).strip():
            raise Failure("StartVirtualCam: failed with no comment")
        print(f"   OK  StartVirtualCam -> error {code}: {comment}")
        return

    _, _, _, status, _ = await c.req("GetVirtualCamStatus")
    if not status.get("outputActive"):
        raise Failure(
            "StartVirtualCam: success but GetVirtualCamStatus.outputActive is false -- the #120 defect verbatim"
        )
    print("   OK  StartVirtualCam succeeded AND the cam is genuinely active")

    ok, code, comment, _, ms = await c.req("StopVirtualCam")
    note_latency("StopVirtualCam", ms)
    if not ok:
        raise Failure(f"StopVirtualCam on an active cam failed: {code} {comment}")
    print("   OK  StopVirtualCam succeeded")


async def case_record_nominal(c: Client) -> None:
    print("-- E. record positive control (writable dir)")
    ok, code, comment, _, ms = await c.req("StartRecord")
    note_latency("StartRecord(nominal)", ms)
    if not ok:
        raise Failure(
            f"StartRecord failed on a writable record dir: {code} {comment}\n"
            "This is the false-negative the verification must NEVER produce."
        )
    _, _, _, status, _ = await c.req("GetRecordStatus")
    if not status.get("outputActive"):
        raise Failure("StartRecord: success but GetRecordStatus.outputActive is false")
    print("   OK  StartRecord succeeded AND recording is genuinely active")

    await asyncio.sleep(1.0)

    ok, code, comment, data, ms = await c.req("StopRecord")
    note_latency("StopRecord(nominal)", ms)
    if not ok:
        raise Failure(f"StopRecord failed on an active recording: {code} {comment}")
    print(f"   OK  StopRecord succeeded (outputPath={data.get('outputPath')!r})")


def assert_bounded() -> None:
    print("-- latency bound (Resolution criterion 3)")
    worst = max(LATENCIES, key=lambda kv: kv[1])
    for name, ms in LATENCIES:
        print(f"   {name}: {ms:.0f} ms")
    if worst[1] > MAX_REQUEST_MS:
        raise Failure(
            f"{worst[0]} took {worst[1]:.0f} ms (> {MAX_REQUEST_MS} ms) -- "
            "the verification turned a start/stop into a wait for activation"
        )
    print(f"   OK  worst case {worst[0]} = {worst[1]:.0f} ms (bound {MAX_REQUEST_MS} ms)")


# --------------------------------------------------------------------------
# Orchestration.
# --------------------------------------------------------------------------
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def run_child(exe: pathlib.Path, record_dir: str, cases) -> None:
    proc = PulsarProcess(exe, free_port(), secrets.token_urlsafe(16), record_dir)
    proc.spawn()
    try:
        url, pw = proc.wait_ready(READY_TIMEOUT_S)
        client = await connect(url, pw)
        try:
            for case in cases:
                await case(client)
        finally:
            await client.ws.close()
    except Failure as exc:
        raise Failure(f"{exc}\n{proc.diag()}") from None
    finally:
        proc.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar output-effect probe (#120)")
    ap.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    args = ap.parse_args()

    if not args.exe.is_file():
        print(f"error: pulsar.exe not found at {args.exe} -- build it first (scripts/build-win.ps1)")
        return 2

    with tempfile.TemporaryDirectory(prefix="pulsar-probe-120-") as tmp:
        tmpdir = pathlib.Path(tmp)

        # Case C's uncreatable dir: <tmp>/not-a-dir/recordings, where
        # <tmp>/not-a-dir is an existing regular FILE.
        blocker = tmpdir / UNCREATABLE_DIR_LEAF
        blocker.write_text("blocks create_directories\n", encoding="utf-8")
        bad_dir = str(blocker / "recordings")

        good_dir = tmpdir / "recordings"

        try:
            print("=== child 1: refusal cases (A replay, B stream, C record, D virtualcam)")
            asyncio.run(run_child(args.exe, bad_dir, [case_replay, case_stream, case_record_refused, case_virtualcam]))

            print("=== child 2: positive control (E record)")
            asyncio.run(run_child(args.exe, str(good_dir), [case_record_nominal]))

            assert_bounded()
        except Failure as exc:
            print(f"\nFAIL: {exc}")
            return 1

    print("\nprobe-output-effect: PASS -- no Success() without a verified effect (#120)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
