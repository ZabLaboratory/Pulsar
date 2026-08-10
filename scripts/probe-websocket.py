#!/usr/bin/env python3
"""
Pulsar binary smoke probe — M1 (ADR 008 §9).

Proves the freshly-built pulsar.exe is *runnable*: it boots headless,
prints the PULSAR_READY sentinel, speaks obs-websocket v5 on loopback,
answers a GetVersion request, and shuts down without leaking a process.

This probe is SELF-CONTAINED. It spawns pulsar.exe itself (it does not
assume a running instance), seeds a fresh session port + password via
PULSAR_PORT / PULSAR_PASSWORD, reads the READY sentinel off stdout, then
drives the v5 handshake from a *pure WebSocket client*.

LICENSE INVARIANT (LICENSE-INVARIANTS.md, ADR 008 §3.1): the probe talks
to Pulsar over the WebSocket process boundary ONLY. It spawns pulsar.exe
as a separate OS process and exchanges nothing but obs-websocket v5
frames. No FFI, no ctypes/cffi, no LoadLibrary of obs.dll / pulsar-*.dll /
libcef.dll, no native import. Pure aggregation — Pulsar's GPL never
crosses into the consumer.

Steps (M1 brief, ADR 008 §9):
  1. Spawn pulsar.exe with cwd=bin/64bit, PULSAR_PORT=<free> +
     PULSAR_PASSWORD=<random> in env.
  2. Read stdout line-by-line until ^PULSAR_READY ws=(\\S+) password=(\\S+)$
     (<= READY_TIMEOUT_S). On timeout, fail with the captured output.
  3. Open ws://127.0.0.1:<port>, v5 handshake: Hello (op0) -> compute
     auth challenge/response -> Identify (op1) -> Identified (op2).
  4. Request GetVersion (op6) -> assert RequestResponse (op7)
     requestStatus.result == true and an obsVersion / obsWebSocketVersion
     field is present.
  5. Clean shutdown: WS close (1000) -> terminate child -> wait ->
     kill fallback. No orphan process.
  6. Exit 0 on success, non-zero + diagnostic on any failure.

Idempotent / re-runnable: a fresh ephemeral port + password each run,
the child is always reaped on every exit path.

Usage (from the repo root, against the built rundir):
    pip install websockets
    python scripts/probe-websocket.py
    python scripts/probe-websocket.py --exe /path/to/pulsar.exe   # override
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

# A clean boot reaches READY in ~3 s warm / ~6 s cold (PRISM-EMBEDDING.md
# §4). The 30 s troubleshooting threshold is the documented "did not
# signal ready" contract; 60 s here is the generous embedding default.
READY_TIMEOUT_S = 60.0
# obs-websocket v5 READY sentinel — stable, machine-parseable
# (PROTOCOL.md, pulsar-headless/main.cpp:342).
READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")

# Shutdown grace before escalating terminate -> kill (PRISM-EMBEDDING.md
# §5.3: never SIGKILL first, it skips obs_shutdown and leaks encoder
# threads).
SHUTDOWN_GRACE_S = 8.0


def pick_free_port() -> int:
    """Bind :0 on loopback to let the OS hand us a free ephemeral port,
    then release it. A tiny TOCTOU window exists between release and
    pulsar.exe binding it; acceptable for a single-run local probe."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def compute_auth(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 challenge/response (PROTOCOL.md:169):
    sha256( base64( sha256(password + salt) ) + challenge )."""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


class PulsarProcess:
    """Spawns pulsar.exe and pumps its stdout on a background thread so
    the READY sentinel is parsed without blocking. Captures the full
    boot log for diagnostics on failure."""

    def __init__(self, exe: pathlib.Path, port: int, password: str) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.proc = None  # subprocess.Popen, set in spawn()
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None
        self._pump_thread: Optional[threading.Thread] = None

    def spawn(self) -> None:
        import subprocess

        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password

        creationflags = 0
        if os.name == "nt":
            # CREATE_NO_WINDOW — keep the console-subsystem child headless,
            # mirroring Prism's windowsHide:true (PRISM-EMBEDDING.md:107).
            creationflags = 0x08000000

        self.proc = subprocess.Popen(  # type: ignore[assignment]
            [str(self.exe)],
            cwd=str(self.exe.parent),  # MANDATORY: libobs resolves data/ from cwd
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # fold stderr into the same stream
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
        """Block until the READY sentinel is seen or timeout/child-exit.
        Returns (ws_url, password). Raises RuntimeError with the boot log
        on any failure path."""
        deadline = time.monotonic() + timeout
        while True:
            if self._ready_event.wait(timeout=0.2):
                m = self._ready_match
                assert m is not None
                return m.group(1), m.group(2)
            # Child died before READY?
            assert self.proc is not None
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"pulsar.exe exited (code {self.proc.returncode}) before READY.\n"
                    + self._diag()
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"pulsar.exe did not signal READY within {timeout:.0f}s.\n"
                    "Likely causes (DEVELOPMENT.md Troubleshooting): wrong cwd "
                    "(default.effect not found), port conflict, AV quarantine, "
                    "or obs-websocket.dll failing to load.\n" + self._diag()
                )

    def _diag(self) -> str:
        tail = self._lines[-40:]
        body = "\n".join(f"  | {ln}" for ln in tail) if tail else "  | (no output captured)"
        return f"--- pulsar stdout/stderr (last {len(tail)} lines) ---\n{body}"

    def shutdown(self, grace: float = SHUTDOWN_GRACE_S) -> None:
        """terminate -> wait grace -> kill fallback. Idempotent."""
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            return
        # CTRL_CLOSE_EVENT equivalent: terminate() posts WM_CLOSE/TerminateProcess.
        # The headless main installs a console-ctrl handler that flips the
        # running flag and calls obs_shutdown cleanly on graceful signals;
        # terminate() on Windows is the best non-console-attached analogue
        # the embedding doc sanctions before the /F kill fallback.
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


async def handshake_and_getversion(url: str, password: str) -> dict:
    """Open the WS, do the v5 Hello/Identify/Identified handshake, send
    GetVersion, assert success, return the responseData. Raises on any
    protocol deviation."""
    print(f"connecting: {url}")
    async with websockets.connect(
        url, subprotocols=["obswebsocket.json"], open_timeout=10
    ) as ws:
        # op=0 Hello
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if hello.get("op") != 0:
            raise RuntimeError(f"expected Hello (op=0), got {hello}")
        ver = hello["d"]["obsWebSocketVersion"]
        rpc = hello["d"]["rpcVersion"]
        print(f"hello: obsWebSocketVersion={ver} rpcVersion={rpc}")

        identify_d: dict = {"rpcVersion": rpc}
        if "authentication" in hello["d"]:
            auth = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(
                password, auth["salt"], auth["challenge"]
            )
            print("auth: computed v5 challenge/response")
        else:
            # auth_required is seeded true (main.cpp:276) — absence means a
            # contract regression worth flagging, not silently passing.
            print("auth: server advertised no authentication (auth_required off?)")

        await ws.send(json.dumps({"op": 1, "d": identify_d}))

        # op=2 Identified
        ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ident.get("op") != 2:
            raise RuntimeError(f"expected Identified (op=2), got {ident}")
        negotiated = ident["d"]["negotiatedRpcVersion"]
        print(f"identified: negotiatedRpcVersion={negotiated}")

        # op=6 Request — GetVersion
        await ws.send(
            json.dumps(
                {
                    "op": 6,
                    "d": {"requestType": "GetVersion", "requestId": "m1-getversion"},
                }
            )
        )

        # op=7 RequestResponse
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if resp.get("op") != 7:
            raise RuntimeError(f"expected RequestResponse (op=7), got {resp}")
        status = resp["d"]["requestStatus"]
        if not status.get("result"):
            raise RuntimeError(f"GetVersion failed: {status}")

        data = resp["d"]["responseData"]
        # A real GetVersion must advertise at least one version field. If
        # neither is present the handler is a stub, not the real surface.
        if "obsVersion" not in data and "obsWebSocketVersion" not in data:
            raise RuntimeError(
                f"GetVersion responseData missing version fields: {data}"
            )

        # Graceful WS close (1000) before tearing down the process.
        await ws.close(code=1000, reason="probe complete")
        return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar M1 binary smoke probe")
    ap.add_argument(
        "--exe",
        type=pathlib.Path,
        default=DEFAULT_EXE,
        help="path to pulsar.exe (default: built rundir)",
    )
    ap.add_argument(
        "--ready-timeout",
        type=float,
        default=READY_TIMEOUT_S,
        help="seconds to wait for the READY sentinel",
    )
    ap.add_argument(
        "--connect-port",
        type=int,
        default=None,
        help=(
            "skip spawning pulsar.exe and instead connect to an already-"
            "running instance on this loopback port (used by run-probes.ps1's "
            "reseed regression probe, #181 F4, to prove a SECOND boot's "
            "reseeded credentials actually authenticate over the wire — "
            "requires --connect-password too)"
        ),
    )
    ap.add_argument(
        "--connect-password",
        type=str,
        default=None,
        help="obs-websocket password for --connect-port (see --connect-port)",
    )
    args = ap.parse_args()

    if bool(args.connect_port) != bool(args.connect_password):
        print("error: --connect-port and --connect-password must be given together")
        return 2

    if args.connect_port is not None:
        # Connect-only mode: no spawn, no lifecycle to manage — just prove
        # the given port/password authenticate a real v5 handshake.
        ws_url = f"ws://127.0.0.1:{args.connect_port}"
        print(f"connect-only mode: {ws_url}")
        try:
            data = asyncio.run(
                handshake_and_getversion(ws_url, args.connect_password)
            )
        except Exception as exc:  # noqa: BLE001 — top-level probe diagnostic
            print(f"FAIL: {exc}")
            print("FAILED (exit 1)")
            return 1
        print("GetVersion ok:")
        for k, v in data.items():
            print(f"  {k}: {v}")
        print("PASS")
        return 0

    exe: pathlib.Path = args.exe
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        print("Build it first: scripts/build-win.ps1 -Full")
        return 2

    port = pick_free_port()
    password = secrets.token_urlsafe(16)
    print(f"spawning: {exe}")
    print(f"  cwd={exe.parent}")
    print(f"  PULSAR_PORT={port}  PULSAR_PASSWORD=<redacted {len(password)} chars>")

    pulsar = PulsarProcess(exe, port, password)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        # The sentinel echoes the password we seeded — confirm the seam.
        if sentinel_pw != password:
            print(
                "warning: sentinel password differs from the one we seeded "
                "(pulsar generated its own?) — using the sentinel value"
            )
        data = asyncio.run(handshake_and_getversion(ws_url, sentinel_pw))
        print("GetVersion ok:")
        for k, v in data.items():
            print(f"  {k}: {v}")
        rc = 0
    except KeyboardInterrupt:
        print("interrupted")
        rc = 130
    except Exception as exc:  # noqa: BLE001 — top-level probe diagnostic
        print(f"FAIL: {exc}")
        rc = 1
    finally:
        pulsar.shutdown()
        # Confirm no orphan.
        if pulsar.proc is not None and pulsar.proc.poll() is None:
            print("error: pulsar.exe still running after shutdown attempt")
            rc = rc or 1
        else:
            print("pulsar.exe reaped cleanly")

    print("PASS" if rc == 0 else f"FAILED (exit {rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
