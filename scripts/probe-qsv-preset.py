#!/usr/bin/env python3
"""
Pulsar PRESET ROUND-TRIP probe -- PULSAR_VIDEO_PRESET actually reaches the
encoder, and GetVideoSettings reports what was applied.

WHAT IT FENCES
--------------
The boot setter wrote the preset to the obs_data key `"preset"` for every
family. obs-qsv11 has no such key: its knob is `target_usage`
(upstream/plugins/obs-qsv11/obs-qsv11.c:390, values TU1..TU7, default TU4), so
a QSV spawn silently ignored PULSAR_VIDEO_PRESET and always encoded at TU4. The
read side had the mirror defect: `on_get_video_settings` read only `"preset"`,
so `video_preset` came back `""` for QSV even once the setter was fixed. Two
halves of the same wrong assumption -- one knob name for every encoder.

  P1  x264 (available on every machine, no GPU): a preset asked for at boot is
      the preset GetVideoSettings reports back. This is the round-trip itself;
      it fails on any regression of either half for the default family.
  P2  QSV: same assertion against `target_usage`, with a value (TU7) that is
      NOT the encoder's default -- so a spawn that ignored the env var reports
      TU4 and fails here, exactly the pre-fix behaviour.
  P3  QSV, lowercase input (`tu2`): the applied value is the canonical `TU2`,
      the spelling capabilities.encoder_families publishes. A lowercased echo
      would be a value the manifest's own list does not contain.

HARDWARE LIMIT (same posture as NVENC elsewhere in this campaign): P2/P3 need
an Intel QSV device. Without one, `resolveEncoderId("qsv")` finds nothing (or
create() returns null) and the boot falls back to x264 -- the probe then prints
a NAMED partial and P2/P3 assert nothing. It is NOT a pass for QSV, and no
machine in the current CI fleet has that device. The hardware-free half of the
proof -- that setter, getter and obs-qsv11's own source agree on the property
name, the seven values and the default -- is scripts/check-qsv-preset-contract
.py, which runs in the lint job and needs no device at all.

LICENSE INVARIANT (LICENSE-INVARIANTS.md, ADR 008 section 3.1): WebSocket
process boundary only. No FFI, no ctypes, no native import.

Usage (from the repo root, against the built rundir):
    pip install websockets
    python scripts/probe-qsv-preset.py
    python scripts/probe-qsv-preset.py --exe /path/to/pulsar.exe
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

# obs-qsv11's own default (obs-qsv11.c:165). A QSV spawn that ignores the env
# var lands exactly here -- which is why P2/P3 never ask for it.
QSV_DEFAULT = "TU4"


class Failure(Exception):
    pass


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------
# Process management -- same shape as probe-loopback-bind.py.
# --------------------------------------------------------------------------
class PulsarProcess:
    def __init__(self, exe: pathlib.Path, port: int, password: str, record_dir: str,
                 extra_env: dict[str, str]) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.record_dir = record_dir
        self.extra_env = extra_env
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
        env.update(self.extra_env)

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


async def get_video_settings(url: str, password: str) -> dict:
    """CallVendorRequest("pulsar", "GetVideoSettings") over a fresh session."""
    ws = await websockets.connect(url, subprotocols=["obswebsocket.json"], open_timeout=10)
    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        identify: dict = {"rpcVersion": hello["d"]["rpcVersion"], "eventSubscriptions": 0}
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify["authentication"] = compute_auth(password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify}))
        ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ident.get("op") != 2:
            raise Failure(f"identify failed: {ident}")

        await ws.send(json.dumps({"op": 6, "d": {
            "requestType": "CallVendorRequest",
            "requestId": "getvideo",
            "requestData": {"vendorName": "pulsar", "requestType": "GetVideoSettings"},
        }}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("op") == 7 and msg["d"]["requestId"] == "getvideo":
                break
        status = msg["d"]["requestStatus"]
        if not status.get("result"):
            raise Failure(f"GetVideoSettings failed: {status}")
        return (msg["d"].get("responseData") or {}).get("responseData") or {}
    finally:
        await ws.close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def spawn_and_read(exe: pathlib.Path, record_dir: str, family: str, preset: str) -> tuple[dict, str]:
    proc = PulsarProcess(exe, free_port(), secrets.token_urlsafe(16), record_dir, {
        "PULSAR_VIDEO_ENCODER": family,
        "PULSAR_VIDEO_PRESET": preset,
    })
    proc.spawn()
    try:
        url, pw = proc.wait_ready(READY_TIMEOUT_S)
        return await get_video_settings(url, pw), proc.diag()
    except Failure as exc:
        raise Failure(f"{exc}\n{proc.diag()}") from None
    finally:
        proc.shutdown()


async def run(exe: pathlib.Path, record_dir: str) -> bool:
    """Returns True when the QSV half could actually be asserted."""
    # ---- P1: x264, available everywhere ---------------------------------
    print("-- P1. x264: the requested preset is the preset the encoder carries")
    settings, _ = await spawn_and_read(exe, record_dir, "x264", "faster")
    if settings.get("video_encoder") != "x264":
        raise Failure(f"asked for x264 and bound {settings.get('video_encoder')!r} -- unusable baseline")
    if settings.get("video_preset") != "faster":
        raise Failure(
            f"PULSAR_VIDEO_PRESET=faster but GetVideoSettings reports "
            f"{settings.get('video_preset')!r}: the boot setter and the reader disagree on x264")
    print("   OK  video_encoder='x264' video_preset='faster'")

    # ---- P2/P3: QSV, needs an Intel QSV device --------------------------
    print("-- P2. qsv: PULSAR_VIDEO_PRESET=TU7 lands on target_usage, not on a dead 'preset' key")
    settings, diag = await spawn_and_read(exe, record_dir, "qsv", "TU7")
    if settings.get("video_encoder") != "qsv":
        print(f"   PARTIAL: boot fell back to {settings.get('video_encoder')!r} -- no Intel QSV device "
              f"on this host, so P2/P3 assert NOTHING. Not a QSV pass.")
        print("            Hardware-free half of the proof: scripts/check-qsv-preset-contract.py")
        return False
    got = settings.get("video_preset")
    if got == QSV_DEFAULT:
        raise Failure(
            f"video_preset is {QSV_DEFAULT!r}, the encoder's OWN default: the spawn ignored "
            f"PULSAR_VIDEO_PRESET=TU7 (the exact pre-fix behaviour -- preset written to a key "
            f"obs-qsv11 does not read).\n{diag}")
    if got != "TU7":
        raise Failure(f"PULSAR_VIDEO_PRESET=TU7 but GetVideoSettings reports {got!r}\n{diag}")
    print("   OK  video_encoder='qsv' video_preset='TU7'")

    print("-- P3. qsv: lowercase 'tu2' is applied as the canonical 'TU2' the manifest publishes")
    settings, diag = await spawn_and_read(exe, record_dir, "qsv", "tu2")
    got = settings.get("video_preset")
    if got != "TU2":
        raise Failure(
            f"PULSAR_VIDEO_PRESET=tu2 reported back as {got!r}, expected the canonical 'TU2' -- "
            f"a consumer matching capabilities.encoder_families would not find it\n{diag}")
    print("   OK  video_preset='TU2'")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar preset round-trip probe (x264 + QSV target_usage)")
    ap.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    args = ap.parse_args()

    if not args.exe.is_file():
        print(f"error: pulsar.exe not found at {args.exe} -- build it first (scripts/build-win.ps1)")
        return 2

    with tempfile.TemporaryDirectory(prefix="pulsar-probe-preset-") as tmp:
        try:
            qsv_asserted = asyncio.run(run(args.exe, str(pathlib.Path(tmp) / "recordings")))
        except Failure as exc:
            print(f"\nFAIL: {exc}")
            return 1

    if qsv_asserted:
        print("\nprobe-qsv-preset: PASS -- the requested preset reaches the encoder and is read back, "
              "on x264 AND on QSV's target_usage")
    else:
        print("\nprobe-qsv-preset: PASS (x264 round-trip only) -- QSV UNPROVEN on this host, no device")
    return 0


if __name__ == "__main__":
    sys.exit(main())
