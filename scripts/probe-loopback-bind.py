#!/usr/bin/env python3
"""
Pulsar LOOPBACK BIND probe (issue #134).

WHAT IT FENCES
--------------
The obs-websocket server used to `listen()` without an address restriction, so
it accepted connections from every interface of the host. Everything the v5 API
can do -- including the stream-egress path #131 made live -- was therefore
reachable from the LAN, guarded by nothing but the session password (which the
CI failure-log artefact has been known to carry in clear, cf. #134). Nothing
needs that reach: every Pulsar consumer connects to 127.0.0.1
(packages/pulsar-bundle*/src/spawn.ts, the PULSAR_READY sentinel, every
scripts/probe-*.py), and docs/PROTOCOL.md already claimed loopback-only.

The fix binds `127.0.0.1` by default (plugins/pulsar-websocket/src/Config.h,
websocketserver/WebSocketServer.cpp), widened only by an explicit
`PULSAR_WS_BIND`.

This probe is the executable statement of that, at the socket layer -- no v5
request involved, because the property is about who can open the socket at all:

  L1  the loopback still accepts and still speaks v5 (the contract Prism and
      every probe depend on -- a bind fix that broke this would be worse than
      the exposure).
  L2  a TCP connect to the SAME port on this host's own non-loopback address
      must fail (refused / timed out). On the pre-fix binary it CONNECTS.
  L3  PULSAR_WS_BIND=0.0.0.0 re-opens the wider bind -- the override is real and
      explicit, so the default is a decision and not an accident.

L2 and L3 need a routable local address; a host that has none (isolated CI
container) can prove nothing either way, and the probe exits 3 (TYPED SKIP)
rather than inventing a verdict.

LICENSE INVARIANT (LICENSE-INVARIANTS.md, ADR 008 section 3.1): WebSocket /
socket process boundary only, like every other scripts/probe-*.py. No FFI, no
ctypes, no native import.

Usage (from the repo root, against the built rundir):
    pip install websockets
    python scripts/probe-loopback-bind.py
    python scripts/probe-loopback-bind.py --exe /path/to/pulsar.exe
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
CONNECT_TIMEOUT_S = 4.0

EXIT_SKIP = 3

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


class Failure(Exception):
    pass


class Skip(Exception):
    pass


# --------------------------------------------------------------------------
# Process management -- same shape as probe-stream-egress-guard.py.
# --------------------------------------------------------------------------
class PulsarProcess:
    def __init__(self, exe: pathlib.Path, port: int, password: str, record_dir: str,
                 bind: Optional[str] = None) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.record_dir = record_dir
        self.bind = bind
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
        if self.bind is None:
            env.pop("PULSAR_WS_BIND", None)
        else:
            env["PULSAR_WS_BIND"] = self.bind

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


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


async def v5_handshake(url: str, password: str) -> None:
    """Full v5 Identify -- proves the listener is the real server, not a stray socket."""
    ws = await websockets.connect(url, subprotocols=["obswebsocket.json"], open_timeout=10)
    try:
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
    finally:
        await ws.close()


# --------------------------------------------------------------------------
# Socket-level reachability.
# --------------------------------------------------------------------------
def tcp_reachable(host: str, port: int, timeout: float = CONNECT_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def local_routable_address() -> Optional[str]:
    """This host's own non-loopback IPv4, as the OS would use it to reach the LAN.

    UDP connect() to a routable address: no packet leaves the machine (the
    probe never touches the network), it only asks the routing table which
    local address would be used."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1, RFC 5737 -- never routed anywhere
        addr = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    if not addr or addr.startswith("127.") or addr == "0.0.0.0":
        return None
    return addr


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def run(exe: pathlib.Path, record_dir: str) -> None:
    lan = local_routable_address()
    if lan is None:
        raise Skip("this host has no non-loopback IPv4 address -- the bind boundary is unobservable here")
    print(f"   host non-loopback address: {lan}")

    # ---- L1 + L2: the default bind -------------------------------------
    port = free_port()
    proc = PulsarProcess(exe, port, secrets.token_urlsafe(16), record_dir)
    proc.spawn()
    try:
        url, pw = proc.wait_ready(READY_TIMEOUT_S)
        print("-- L1. the loopback still accepts and still speaks v5")
        if not url.startswith("ws://127.0.0.1:"):
            raise Failure(f"the READY sentinel no longer advertises the loopback: {url!r}")
        await v5_handshake(url, pw)
        print(f"   OK  {url} -> Hello/Identify")

        print("-- L2. the same port on this host's LAN address must NOT accept")
        if tcp_reachable(lan, port):
            raise Failure(
                f"TCP connect to {lan}:{port} SUCCEEDED -- the server is listening on a non-loopback "
                "interface. The whole v5 surface (including the stream-egress path) is reachable from "
                "the network behind a single password (#134)."
            )
        print(f"   OK  {lan}:{port} -> refused")
    except Failure as exc:
        raise Failure(f"{exc}\n{proc.diag()}") from None
    finally:
        proc.shutdown()

    # ---- L3: the override is real --------------------------------------
    print("-- L3. PULSAR_WS_BIND=0.0.0.0 re-opens the wider bind (explicit, not accidental)")
    port = free_port()
    proc = PulsarProcess(exe, port, secrets.token_urlsafe(16), record_dir, bind="0.0.0.0")
    proc.spawn()
    try:
        _, pw = proc.wait_ready(READY_TIMEOUT_S)
        if not tcp_reachable(lan, port):
            raise Failure(
                f"TCP connect to {lan}:{port} failed with PULSAR_WS_BIND=0.0.0.0 -- the documented "
                "override does not work, so the loopback default cannot be widened when a real need "
                "shows up."
            )
        await v5_handshake(f"ws://{lan}:{port}", pw)
        print(f"   OK  {lan}:{port} -> Hello/Identify under the explicit override")
    except Failure as exc:
        raise Failure(f"{exc}\n{proc.diag()}") from None
    finally:
        proc.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar loopback-bind probe (#134)")
    ap.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    args = ap.parse_args()

    if not args.exe.is_file():
        print(f"error: pulsar.exe not found at {args.exe} -- build it first (scripts/build-win.ps1)")
        return 2

    with tempfile.TemporaryDirectory(prefix="pulsar-probe-bind-") as tmp:
        try:
            asyncio.run(run(args.exe, str(pathlib.Path(tmp) / "recordings")))
        except Skip as exc:
            print(f"\nSKIP: {exc}")
            return EXIT_SKIP
        except Failure as exc:
            print(f"\nFAIL: {exc}")
            return 1

    print("\nprobe-loopback-bind: PASS -- the server binds the loopback by default and only a deliberate "
          "PULSAR_WS_BIND widens it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
