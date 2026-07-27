#!/usr/bin/env python3
"""
Pulsar v5 STREAM-EGRESS GUARD probe (Bastion C1/C2 on PR #133).

WHAT IT FENCES
--------------
#131 bound the frontend stub's `streamService` to `streamOutput`, turning the
v5 `SetStreamServiceSettings` + `StartStream` path into a LIVE egress for the
first time. That binding reopened the hole #114 had closed by leaving the path
dead: the stub boots with an `rtmp_common` / "Twitch" placeholder, and upstream
resolves an rtmp_common Twitch service through `update_ingest`
(upstream/plugins/rtmp-services/rtmp-common.c), which falls back to the bundled
`rtmp://live.twitch.tv/app` (service-specific/twitch.c:45) whenever the ingest
list is missing -- first run, cold cache, offline. The stream key would travel
in CLEARTEXT, on a path whose twin `pulsar:StartDestination` guarantees rtmps://
by `static_assert`.

The fix (plugins/pulsar-frontend-stub/include/pulsar-stream-egress.h) is form
(b): Twitch is barred from the v5 single-stream path -- it goes through
`pulsar:StartDestination` -- and the v5 path gets the SAME front-loaded
validation as that twin (rtmp scheme + non-empty key).

This probe is the executable statement of both. Every case below FAILS on the
pre-fix binary and passes on the fixed one:

  C1a  the BOOT placeholder is rtmp_common/"Twitch": StartStream with no
       request at all must be refused, naming Twitch and the cleartext URL.
       Pre-fix: the placeholder had no key, so the refusal came from libobs by
       accident -- change nothing but the key and the cleartext push is live.
  C1b  SetStreamServiceSettings{rtmp_common, service:"Twitch", key:...} must be
       refused at the CONFIGURATION seam, including the exploit verbatim
       (`server: "rtmp://live.twitch.tv/app"`). MEASURED on the pre-fix binary:
       Set answered `result: true`, StartStream answered `result: true`, and
       OBS_WEBSOCKET_OUTPUT_STARTING went on the wire -- the stream key leaving
       over unencrypted RTMP.
  C1c  same, spelled "twitch" -- the guard reads the setting case-insensitively,
       not a literal.
  C2a  a non-rtmp scheme (http://) must be refused. Pre-fix: accepted.
  C2b  an EMPTY stream key must be refused. Pre-fix: accepted.
  REG  the nominal rtmp_custom destination (#131's own reason to exist) still
       configures AND still starts -- the guard must not have re-killed the
       path it protects.
  C2c  the merge path, BOTH directions: a partial update inheriting a valid key
       must pass, and a partial update that blanks the key must fail.
       Validation is on the EFFECTIVE settings, never on the request payload --
       a payload-only validator gets both of these wrong.

The refusal must always carry a NAMED cause; a bare error code would be the
#120 defect wearing a security hat.

LICENSE INVARIANT (LICENSE-INVARIANTS.md, ADR 008 §3.1): WebSocket process
boundary only, like every other scripts/probe-*.py. No FFI, no ctypes, no
native import.

Usage (from the repo root, against the built rundir):
    pip install websockets
    python scripts/probe-stream-egress-guard.py
    python scripts/probe-stream-egress-guard.py --exe /path/to/pulsar.exe
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
    print("error: pip install websockets (pure WS client -- no native deps)")
    sys.exit(2)


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    REPO_ROOT / "upstream" / "build_x64" / "rundir" / "RelWithDebInfo" / "bin" / "64bit" / "pulsar.exe"
)

READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0

STATUS_INVALID_REQUEST_FIELD = 400
STATUS_OUTPUT_NOT_RUNNING = 501

# A destination that is well-formed but unreachable: the probe never touches
# the network, and libobs hands the connect to its own thread either way.
GOOD_SERVER = "rtmp://127.0.0.1:1/probe"
GOOD_KEY = "probe-key"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


class Failure(Exception):
    pass


# --------------------------------------------------------------------------
# Process management -- same shape as probe-output-effect.py.
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
                raise Failure(f"pulsar.exe exited (code {self.proc.returncode}) before READY.\n" + self.diag())
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
# obs-websocket v5 client.
# --------------------------------------------------------------------------
def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


class Client:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._n = 0
        self.events: list[dict] = []

    async def req(self, request_type: str, data: dict | None = None, timeout: float = 15.0):
        """Returns (ok, code, comment, responseData)."""
        self._n += 1
        rid = f"egress-{self._n}"
        body: dict = {"requestType": request_type, "requestId": rid}
        if data is not None:
            body["requestData"] = data

        await self.ws.send(json.dumps({"op": 6, "d": body}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("op") == 5:
                self.events.append(msg["d"])
                continue
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

    async def wait_event(self, event_type: str, state: str, timeout: float) -> bool:
        for ev in self.events:
            if ev.get("eventType") == event_type and (ev.get("eventData") or {}).get("outputState") == state:
                return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                return False
            msg = json.loads(raw)
            if msg.get("op") != 5:
                continue
            self.events.append(msg["d"])
            d = msg["d"]
            if d.get("eventType") == event_type and (d.get("eventData") or {}).get("outputState") == state:
                return True
        return False


async def connect(url: str, password: str) -> Client:
    ws = await websockets.connect(url, subprotocols=["obswebsocket.json"], open_timeout=10)
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    if hello.get("op") != 0:
        raise Failure(f"expected Hello (op=0), got {hello}")
    # Subscribe to Outputs so the regression leg can read StreamStateChanged.
    identify: dict = {"rpcVersion": hello["d"]["rpcVersion"], "eventSubscriptions": (1 << 6)}
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
def require_refusal(name: str, ok: bool, code, comment, expected_code: int, hints: tuple[str, ...]) -> None:
    if ok:
        raise Failure(
            f"{name}: ACCEPTED. The v5 stream-egress guard is not enforcing -- this is the "
            f"cleartext-Twitch / unvalidated-destination hole (Bastion C1/C2 on PR #133)."
        )
    if code != expected_code:
        raise Failure(f"{name}: refused with code {code}, expected {expected_code} ({comment})")
    if not comment or not str(comment).strip():
        raise Failure(f"{name}: refused with no comment -- the cause must be named, never a bare code")
    missing = [h for h in hints if h.lower() not in str(comment).lower()]
    if missing:
        raise Failure(
            f"{name}: comment {comment!r} does not name the cause (expected mention of {missing!r})"
        )
    print(f"   OK  {name} -> refused {code}: {comment}")


async def set_service(c: Client, service_type: str, settings: dict):
    return await c.req(
        "SetStreamServiceSettings", {"streamServiceType": service_type, "streamServiceSettings": settings}
    )


async def case_boot_placeholder_start(c: Client) -> None:
    """C1a -- the state the binary boots in, before ANY request."""
    print("-- C1a. boot placeholder (rtmp_common/Twitch) must not reach the wire")
    ok, code, comment, data = await c.req("GetStreamServiceSettings")
    if not ok:
        raise Failure(f"GetStreamServiceSettings failed: {code} {comment}")
    svc_type = data.get("streamServiceType")
    svc_name = (data.get("streamServiceSettings") or {}).get("service")
    print(f"   boot service: type={svc_type!r} service={svc_name!r}")
    if svc_type != "rtmp_common" or str(svc_name or "").lower() != "twitch":
        raise Failure(
            f"the boot placeholder is no longer rtmp_common/Twitch (got {svc_type!r}/{svc_name!r}). "
            "This probe's premise moved -- re-read pulsar-frontend-stub.cpp setup() before relaxing it."
        )

    ok, code, comment, _ = await c.req("StartStream")
    require_refusal("StartStream (boot placeholder)", ok, code, comment, STATUS_OUTPUT_NOT_RUNNING,
                    ("twitch", "cleartext", "service"))

    _, _, _, status = await c.req("GetStreamStatus")
    if status.get("outputActive"):
        raise Failure("GetStreamStatus reports active after a refused start -- the guard did not hold")
    print("   OK  GetStreamStatus agrees: outputActive=false")


async def case_twitch_config_refused(c: Client) -> None:
    """C1b/C1c -- the configuration seam, both spellings."""
    print("-- C1b/c. SetStreamServiceSettings(rtmp_common, Twitch) refused at configuration time")
    # The third payload is the EXPLOIT VERBATIM, measured on the pre-fix binary:
    # with an explicit cleartext `server` the pre-fix build answered
    # `result: true` to this Set, then `result: true` to StartStream, and put
    # OBS_WEBSOCKET_OUTPUT_STARTING on the wire -- the key going out over
    # unencrypted RTMP. The first two payloads (no `server`) were accepted too;
    # they only stopped short at StartStream because rtmp_common has no server
    # to connect to, which is a bug-compatible accident, not a defence.
    payloads = (
        ("Twitch", {"service": "Twitch", "key": "live_123_secret"}),
        ("twitch", {"service": "twitch", "key": "live_123_secret"}),
        ("Twitch+cleartext server",
         {"service": "Twitch", "server": "rtmp://live.twitch.tv/app", "key": "live_123_secret"}),
    )
    for label, settings in payloads:
        ok, code, comment, _ = await set_service(c, "rtmp_common", settings)
        require_refusal(
            f"SetStreamServiceSettings(rtmp_common,{label})", ok, code, comment,
            STATUS_INVALID_REQUEST_FIELD, ("twitch", "cleartext"),
        )

    # And it did not half-apply: the service is untouched.
    _, _, _, data = await c.req("GetStreamServiceSettings")
    if (data.get("streamServiceSettings") or {}).get("key"):
        raise Failure("the refused Twitch key was written into the service anyway -- refusal must be atomic")
    print("   OK  the refused settings were not applied")


async def case_scheme_and_key(c: Client) -> None:
    """C2a/C2b -- parity with pulsar:StartDestination's front-loaded validation."""
    print("-- C2a. non-rtmp scheme refused")
    ok, code, comment, _ = await set_service(c, "rtmp_custom", {"server": "http://127.0.0.1:1/probe", "key": GOOD_KEY})
    require_refusal("SetStreamServiceSettings(http://)", ok, code, comment, STATUS_INVALID_REQUEST_FIELD,
                    ("rtmp",))

    print("-- C2b. empty stream key refused")
    ok, code, comment, _ = await set_service(c, "rtmp_custom", {"server": GOOD_SERVER, "key": ""})
    require_refusal("SetStreamServiceSettings(empty key)", ok, code, comment, STATUS_INVALID_REQUEST_FIELD,
                    ("key",))


async def case_merge_path(c: Client) -> None:
    """C2c -- same-type updates MERGE onto the current settings, so the guard
    must read the EFFECTIVE result and not the request payload. Both directions
    are asserted, because a payload-only validator gets BOTH wrong:
      - a partial update whose missing field is inherited and VALID must pass;
      - a partial update whose merged result is INVALID must fail.
    Runs after the nominal case, which leaves an accepted rtmp_custom in place
    (same type -> the merge branch is the one actually taken)."""
    print("-- C2c. validation reads the MERGED settings, not the payload")

    other_server = "rtmps://127.0.0.1:1/probe"
    ok, code, comment, _ = await set_service(c, "rtmp_custom", {"server": other_server})
    if not ok:
        raise Failure(
            f"SetStreamServiceSettings(server only) was refused ({code}: {comment}) -- the key was "
            "already set on the current rtmp_custom service and must be inherited by the merge; "
            "the guard is validating the payload instead of the effective settings"
        )
    _, _, _, data = await c.req("GetStreamServiceSettings")
    settings = data.get("streamServiceSettings") or {}
    if settings.get("server") != other_server or settings.get("key") != GOOD_KEY:
        raise Failure(f"the partial update did not merge as expected: {settings}")
    print("   OK  partial update accepted, key inherited from the current service")

    ok, code, comment, _ = await set_service(c, "rtmp_custom", {"key": ""})
    require_refusal("SetStreamServiceSettings(key blanked by a partial update)", ok, code, comment,
                    STATUS_INVALID_REQUEST_FIELD, ("key",))


async def case_nominal_still_works(c: Client) -> None:
    """REG -- #131's own reason to exist must survive its own guard."""
    print("-- REG. the nominal rtmp_custom destination still configures and still starts (#131)")
    ok, code, comment, _ = await set_service(c, "rtmp_custom", {"server": GOOD_SERVER, "key": GOOD_KEY})
    if not ok:
        raise Failure(
            f"SetStreamServiceSettings(rtmp_custom, {GOOD_SERVER}, key) was refused ({code}: {comment}) "
            "-- the guard over-reached and killed the path #131 exists for"
        )
    _, _, _, data = await c.req("GetStreamServiceSettings")
    if (data.get("streamServiceSettings") or {}).get("server") != GOOD_SERVER:
        raise Failure(f"GetStreamServiceSettings does not reflect the accepted service: {data}")
    print("   OK  accepted and reflected by GetStreamServiceSettings")

    c.events.clear()
    ok, code, comment, _ = await c.req("StartStream")
    if not ok:
        raise Failure(
            f"StartStream on a valid rtmp_custom destination was refused ({code}: {comment}) "
            "-- the guard re-killed the v5 path"
        )
    starting = await c.wait_event("StreamStateChanged", "OBS_WEBSOCKET_OUTPUT_STARTING", timeout=8.0)
    if not starting:
        raise Failure(
            "StartStream returned success but no StreamStateChanged/OBS_WEBSOCKET_OUTPUT_STARTING was "
            "emitted -- the service never reached the output (#131 regression)"
        )
    print("   OK  StreamStateChanged STARTING on the wire -- the service is bound to the output")

    # Leave the child idle for the next case: SetStreamServiceSettings refuses
    # with OutputRunning (500) while the stream output is up, and the connect to
    # an unreachable endpoint takes a moment to give up on its own.
    await c.req("StopStream")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        _, _, _, status = await c.req("GetStreamStatus")
        if not status.get("outputActive"):
            break
        await asyncio.sleep(0.2)
    else:
        raise Failure("the stream output was still active 10 s after StopStream")


CASES = [
    case_boot_placeholder_start,
    case_twitch_config_refused,
    case_scheme_and_key,
    case_nominal_still_works,
    case_merge_path,
]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def run_child(exe: pathlib.Path, record_dir: str) -> None:
    proc = PulsarProcess(exe, free_port(), secrets.token_urlsafe(16), record_dir)
    proc.spawn()
    try:
        url, pw = proc.wait_ready(READY_TIMEOUT_S)
        client = await connect(url, pw)
        try:
            for case in CASES:
                await case(client)
        finally:
            await client.ws.close()
    except Failure as exc:
        raise Failure(f"{exc}\n{proc.diag()}") from None
    finally:
        proc.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar v5 stream-egress guard probe (Bastion C1/C2, PR #133)")
    ap.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    args = ap.parse_args()

    if not args.exe.is_file():
        print(f"error: pulsar.exe not found at {args.exe} -- build it first (scripts/build-win.ps1)")
        return 2

    with tempfile.TemporaryDirectory(prefix="pulsar-probe-egress-") as tmp:
        try:
            asyncio.run(run_child(args.exe, str(pathlib.Path(tmp) / "recordings")))
        except Failure as exc:
            print(f"\nFAIL: {exc}")
            return 1

    print("\nprobe-stream-egress-guard: PASS -- Twitch is off the v5 path and the v5 path validates "
          "its destination like pulsar:StartDestination does")
    return 0


if __name__ == "__main__":
    sys.exit(main())
