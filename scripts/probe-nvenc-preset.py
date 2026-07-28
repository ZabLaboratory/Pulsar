#!/usr/bin/env python3
"""
Pulsar NVENC PRESET probe -- PULSAR_VIDEO_PRESET survives encoder ACTIVATION.

WHAT IT FENCES
--------------
The RUNTIME half of the #152 fix, on a real NVENC GPU. #152 split the preset
property name per encoder id (presetPropForId): the 31.0+ `obs_nvenc_h264_tex`
reads `"preset"`, while the pre-31.0 compat shims `jim_nvenc` and `ffmpeg_nvenc`
read `"preset2"`. Writing `"preset"` to a shim was worse than inert:
migrate_settings() (nvenc-compat.c:20) copies `"preset2"` -- default `"p5"`
(nvenc-compat.c:111) -- OVER `"preset"` before rerouting, so every NVENC spawn
encoded at p5 whatever the operator asked for. `jim_nvenc` is the id
resolveEncoderId tries FIRST, so that was the live path.

scripts/check-nvenc-preset-contract.py proves the mapping hardware-free, out of
obs-nvenc's own source. It cannot prove the TIMING, which is what made the
defect survive the #148/#150 encoder work: libobs activates an encoder lazily,
at the first obs_encoder_initialize (first StartStream/StartRecord), not at
create. A check that reads GetVideoSettings without ever recording sees the
value Pulsar wrote and passes even on the pre-fix tree. So this probe RECORDS
before it reads -- and asserts against libobs's own init log, not only against
Pulsar's echo of its own settings object.

  N1  nvenc, p1: after StartRecord, the encoder still carries p1. Pre-fix this
      reports p5 -- and p1 is deliberately as far from p5 as the scale goes.
  N2  nvenc, p7: same on the other side of the default, so a probe that
      accidentally asked for the default value could not pass by luck.
  N3  the libobs log of the N2 spawn never announces a preset other than the
      one asked for -- the second, independent witness (the encoder's own
      init log), not Pulsar echoing its own settings object back.

HARDWARE LIMIT (same posture as scripts/probe-qsv-preset.py): N1-N3 need an
NVENC-capable NVIDIA GPU. Without one the boot falls back to x264, the probe
prints a NAMED partial and asserts NOTHING about NVENC. No runner in the
current CI fleet has that device; the hardware-free half of the proof is
scripts/check-nvenc-preset-contract.py, which runs in the lint job.

LICENSE INVARIANT (LICENSE-INVARIANTS.md, ADR 008 section 3.1): WebSocket
process boundary only. No FFI, no ctypes, no native import.

Usage (from the repo root, against the built rundir):
    pip install websockets
    python scripts/probe-nvenc-preset.py
    python scripts/probe-nvenc-preset.py --exe /path/to/pulsar.exe
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
RECORD_SECONDS = 3.0

# nvenc-compat.c:111 -- the value migrate_settings() forces onto "preset" when
# nothing sets "preset2". A clobbered spawn lands exactly here, which is why
# neither leg below ever asks for it.
NVENC_CLOBBER_DEFAULT = "p5"

# libobs's own encoder init line, e.g. "preset: p7" / "preset:      p7".
LOG_PRESET_RE = re.compile(r"^\s*preset:\s*(p[1-7])\s*$", re.IGNORECASE)


class Failure(Exception):
    pass


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------
# Process management -- same shape as probe-qsv-preset.py.
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

    def log_presets(self) -> list[str]:
        """Every preset libobs itself announced, in order."""
        out = []
        for ln in self._lines:
            m = LOG_PRESET_RE.match(ln)
            if m:
                out.append(m.group(1).lower())
        return out

    def diag(self) -> str:
        tail = self._lines[-60:]
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


async def _request(ws, request_type: str, request_id: str, data: Optional[dict] = None) -> dict:
    body: dict = {"requestType": request_type, "requestId": request_id}
    if data is not None:
        body["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": body}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        if msg.get("op") == 7 and msg["d"]["requestId"] == request_id:
            return msg["d"]


async def record_then_read(url: str, password: str) -> dict:
    """StartRecord (forces obs_encoder_initialize), then GetVideoSettings."""
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

        # THE point of this probe: the compat shim rewrites "preset" inside
        # obs_encoder_initialize, which only runs when an output starts.
        started = await _request(ws, "StartRecord", "rec-start")
        if not started["requestStatus"].get("result"):
            raise Failure(f"StartRecord declined: {started['requestStatus']} -- "
                          f"the encoder was never activated, so this probe proves nothing")
        await asyncio.sleep(RECORD_SECONDS)

        got = await _request(ws, "CallVendorRequest", "getvideo", {
            "vendorName": "pulsar", "requestType": "GetVideoSettings"})
        if not got["requestStatus"].get("result"):
            raise Failure(f"GetVideoSettings failed: {got['requestStatus']}")

        await _request(ws, "StopRecord", "rec-stop")
        return (got.get("responseData") or {}).get("responseData") or {}
    finally:
        await ws.close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def spawn_record_read(exe: pathlib.Path, record_dir: str, family: str,
                            preset: str) -> tuple[dict, list[str], str]:
    proc = PulsarProcess(exe, free_port(), secrets.token_urlsafe(16), record_dir, {
        "PULSAR_VIDEO_ENCODER": family,
        "PULSAR_VIDEO_PRESET": preset,
    })
    proc.spawn()
    try:
        url, pw = proc.wait_ready(READY_TIMEOUT_S)
        settings = await record_then_read(url, pw)
        return settings, proc.log_presets(), proc.diag()
    except Failure as exc:
        raise Failure(f"{exc}\n{proc.diag()}") from None
    finally:
        proc.shutdown()


async def leg(exe: pathlib.Path, record_dir: str, preset: str) -> tuple[dict, list[str], str]:
    settings, logged, diag = await spawn_record_read(exe, record_dir, "nvenc", preset)
    if settings.get("video_encoder") != "nvenc":
        return settings, logged, diag
    got = settings.get("video_preset")
    if got == NVENC_CLOBBER_DEFAULT and preset != NVENC_CLOBBER_DEFAULT:
        raise Failure(
            f"PULSAR_VIDEO_PRESET={preset} but the ACTIVATED encoder carries "
            f"{NVENC_CLOBBER_DEFAULT!r} -- the compat shim's migrate_settings() overwrote "
            f"'preset' from 'preset2'. This is the pre-#152 behaviour: the boot setter wrote "
            f"the preset under a name the SELECTED id does not read (presetPropForId).\n{diag}")
    if got != preset:
        raise Failure(f"PULSAR_VIDEO_PRESET={preset} but GetVideoSettings reports {got!r}\n{diag}")
    return settings, logged, diag


async def run(exe: pathlib.Path, record_dir: str) -> bool:
    """Returns True when the NVENC legs could actually be asserted."""
    print("-- N1. nvenc p1: the preset survives StartRecord (encoder activation)")
    settings, _, _ = await leg(exe, record_dir, "p1")
    if settings.get("video_encoder") != "nvenc":
        print(f"   PARTIAL: boot fell back to {settings.get('video_encoder')!r} -- no NVENC device "
              f"on this host, so N1-N3 assert NOTHING. Not an NVENC pass.")
        print("            Hardware-free half of the proof: scripts/check-nvenc-preset-contract.py")
        return False
    print("   OK  video_encoder='nvenc' video_preset='p1' after recording")

    print("-- N2. nvenc p7: same on the other side of the shim's p5 default")
    _, logged, diag = await leg(exe, record_dir, "p7")
    print("   OK  video_preset='p7' after recording")

    print("-- N3. libobs's own log never announces a preset other than p7")
    wrong = [p for p in logged if p != "p7"]
    if wrong:
        raise Failure(
            f"libobs logged preset(s) {wrong} for a p7 spawn -- the encoder that actually ran "
            f"is not the one GetVideoSettings describes\n{diag}")
    if not logged:
        print("   SKIP libobs printed no 'preset:' line on this build (N1/N2 still hold)")
    else:
        print(f"   OK  libobs logged {logged}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar NVENC preset probe (survives encoder activation)")
    ap.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    args = ap.parse_args()

    if not args.exe.is_file():
        print(f"error: pulsar.exe not found at {args.exe} -- build it first (scripts/build-win.ps1)")
        return 2

    with tempfile.TemporaryDirectory(prefix="pulsar-probe-nvenc-") as tmp:
        try:
            asserted = asyncio.run(run(args.exe, str(pathlib.Path(tmp) / "recordings")))
        except Failure as exc:
            print(f"\nFAIL: {exc}")
            return 1

    if asserted:
        print("\nprobe-nvenc-preset: PASS -- the requested NVENC preset survives encoder activation")
    else:
        print("\nprobe-nvenc-preset: PASS (no NVENC device) -- NVENC UNPROVEN on this host")
    return 0


if __name__ == "__main__":
    sys.exit(main())
