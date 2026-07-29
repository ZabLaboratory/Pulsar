#!/usr/bin/env python3
"""
Pulsar MULTI-TRACK AUDIO probe (#168, ADR Prism 028 section 3.5).

WHAT IT PROVES
  M1  Several audio encoders are created and bound to DISTINCT slots of the
      SAME output, each pulling from its own libobs mixer index, each carrying
      its own settings (criterion 1).
  M2  An input routed to track N is EFFECTIVELY CONSUMED by that track --
      measured on the audio mix the track's encoder is attached to, never by
      re-reading the input (criterion 2). See below: this is the whole point.
  M3  The three outputs carry DIFFERENT track sets (criterion 3).
  M4  capabilities.audio_tracks reports the slots really bound, and WHICH
      tracks they are (criterion 4).
  M5  A spawn with no audio env is byte-for-byte the pre-#168 wiring: one
      encoder, slot 0, track 1, on all three outputs (criterion 5).

WHY M2 IS NOT A READ-BACK OF THE INPUT
  obs_source_set_audio_mixers() writes the mixer bit on the input whatever
  anything downstream carries, and libobs hands every fresh source
  audio_mixers = 0xFF. So GetInputAudioTracks answers "enabled" in the healthy
  case AND in the broken one -- an input-side oracle CONFIRMS a lie instead of
  detecting it. That is #157, and the same trap sits one level down here: an
  encoder can be bound to slot 1 of an output and still be fed nothing.

  The oracle is therefore taken downstream, on the very bus the encoder reads:
  pulsar:MeasureAudioTrackFlow installs a raw audio callback on each libobs mix
  for a bounded window and reports the peak amplitude that flowed. An encoder
  created with obs_audio_encoder_create(..., mixer_idx, ...) and that callback
  are registered as inputs of the SAME obs->audio.mixes[mixer_idx]. What the
  probe measures is what the encoder is fed.

  And it is measured DIFFERENTIALLY: the same input is routed to track 3, then
  to track 1, and the signal must MOVE. A static property of the mixes, or a
  routing that is written but not honoured, fails one of the two halves. The
  probe also asserts the discriminating case explicitly -- an unbacked track
  the input reports as enabled carries the signal but no encoder consumes it
  (`encoder_bound: false`), which is #157 seen from the flow side.

LICENSE INVARIANT (LICENSE-INVARIANTS.md): the WebSocket process boundary only
-- no FFI, no ctypes, no LoadLibrary of obs.dll. The tone is a plain WAV file
written by this script and read back by an ffmpeg_source inside pulsar.exe.

Usage (from the repo root, against the built rundir):
    pip install websockets
    python scripts/probe-audio-multitrack.py
    python scripts/probe-audio-multitrack.py --exe /path/to/pulsar.exe
"""
from __future__ import annotations

import argparse
import array
import asyncio
import base64
import hashlib
import json
import math
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
import wave
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

# The multi-track spawn under test. The three outputs deliberately disagree:
# that disagreement IS criterion 3.
TRACK_COUNT = 3
STREAM_TRACKS = [1, 3]
RECORD_TRACKS = [1, 2, 3]
REPLAY_TRACKS = [2]
TRACK2_BITRATE = 96          # per-track override, distinct from the default 160
DEFAULT_BITRATE = 160

TONE_HZ = 440.0
TONE_AMPLITUDE = 0.5
TONE_SECONDS = 4

INPUT_NAME = "probe-audio-multitrack-tone"
INPUT_KIND = "ffmpeg_source"
DESKTOP_AUDIO_NAME = "PulsarDesktopAudio"

# The tone peaks at 0.5. Anything above this is unambiguously the tone; the
# floor is one tenth of it, and the two are also compared to each other, so a
# machine that happens to be playing sound cannot turn a red into a green.
FLOW_PRESENT = 0.05
FLOW_SILENT = 0.005
MEASURE_MS = 400
TONE_SETTLE_TIMEOUT_S = 20.0


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
        for key in list(env):
            if key.startswith("PULSAR_AUDIO_") or key.endswith("_AUDIO_TRACKS"):
                env.pop(key, None)
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

    def shutdown(self, grace: float = 8.0) -> None:
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


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_tone(path: pathlib.Path) -> None:
    """A plain 48 kHz stereo sine. Produced here, never committed: a binary
    fixture in git would be an artefact nobody can diff."""
    rate = 48000
    samples = array.array("h")
    for i in range(rate * TONE_SECONDS):
        value = int(TONE_AMPLITUDE * 32767 * math.sin(2.0 * math.pi * TONE_HZ * i / rate))
        samples.append(value)
        samples.append(value)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())


# --------------------------------------------------------------------------
# WebSocket session
# --------------------------------------------------------------------------
class Session:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._counter = 0

    async def request(self, request_type: str, data: dict | None = None,
                      timeout: float = 15.0) -> dict:
        self._counter += 1
        request_id = f"probe-{self._counter}"
        body: dict = {"requestType": request_type, "requestId": request_id}
        if data is not None:
            body["requestData"] = data
        await self.ws.send(json.dumps({"op": 6, "d": body}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            if msg.get("op") == 7 and msg["d"]["requestId"] == request_id:
                return msg["d"]

    async def ok(self, request_type: str, data: dict | None = None) -> dict:
        resp = await self.request(request_type, data)
        status = resp["requestStatus"]
        if not status.get("result"):
            raise Failure(f"{request_type} failed: {status}")
        return resp.get("responseData") or {}

    async def vendor(self, request_type: str, data: dict | None = None) -> dict:
        payload: dict = {"vendorName": "pulsar", "requestType": request_type}
        if data is not None:
            payload["requestData"] = data
        resp = await self.request("CallVendorRequest", payload)
        status = resp["requestStatus"]
        if not status.get("result"):
            raise Failure(f"pulsar:{request_type} failed: {status}")
        out = (resp.get("responseData") or {}).get("responseData") or {}
        if "error" in out:
            raise Failure(f"pulsar:{request_type} answered an error: {out['error']}")
        return out


async def connect(url: str, password: str) -> Session:
    ws = await websockets.connect(url, subprotocols=["obswebsocket.json"], open_timeout=15)
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    identify: dict = {"rpcVersion": hello["d"]["rpcVersion"], "eventSubscriptions": 0}
    if "authentication" in hello["d"]:
        a = hello["d"]["authentication"]
        identify["authentication"] = compute_auth(password, a["salt"], a["challenge"])
    await ws.send(json.dumps({"op": 1, "d": identify}))
    ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    if ident.get("op") != 2:
        raise Failure(f"identify failed: {ident}")
    return Session(ws)


def slots_of(tracks_payload: dict, output: str) -> list[dict]:
    for entry in tracks_payload.get("outputs") or []:
        if entry.get("output") == output:
            return entry.get("slots") or []
    raise Failure(
        f"pulsar:GetAudioTracks reported no '{output}' output at all: "
        f"{json.dumps(tracks_payload)}")


def peaks_by_track(flow: dict) -> dict[int, float]:
    return {int(e["track"]): float(e["peak"]) for e in (flow.get("tracks") or [])}


def bound_by_track(flow: dict) -> dict[int, bool]:
    return {int(e["track"]): bool(e.get("encoder_bound"))
            for e in (flow.get("tracks") or []) if "encoder_bound" in e}


# --------------------------------------------------------------------------
# M1 / M3 / M4 -- the wiring, read from the outputs
# --------------------------------------------------------------------------
async def assert_wiring(s: Session) -> None:
    tracks = await s.vendor("GetAudioTracks")

    print("-- M1. several encoders, distinct slots of the SAME output, distinct mixes")
    stream = slots_of(tracks, "stream")
    got_stream = [(int(x["slot"]), int(x["track"])) for x in stream]
    want_stream = list(enumerate(STREAM_TRACKS))
    if got_stream != want_stream:
        raise Failure(
            f"the streaming output carries {got_stream} (slot, track) but "
            f"PULSAR_STREAM_AUDIO_TRACKS asked for {STREAM_TRACKS}, i.e. {want_stream}")
    names = [x["encoder"] for x in stream]
    if len(set(names)) != len(names):
        raise Failure(f"the streaming output's slots share an encoder: {names}")
    print(f"   OK  stream slots {got_stream}, encoders {names}")

    print("-- M1b. per-track settings are the track's own, not the default repeated")
    record = slots_of(tracks, "record")
    bitrates = {int(x["track"]): int(x["bitrate"]) for x in record}
    if bitrates.get(2) != TRACK2_BITRATE:
        raise Failure(
            f"PULSAR_AUDIO_BITRATE_2={TRACK2_BITRATE} but track 2 encodes at "
            f"{bitrates.get(2)!r} -- the per-track override did not reach the encoder")
    if bitrates.get(1) != DEFAULT_BITRATE or bitrates.get(3) != DEFAULT_BITRATE:
        raise Failure(
            f"tracks 1/3 should keep the default {DEFAULT_BITRATE} kbps, got {bitrates}")
    print(f"   OK  bitrates per track {bitrates}")

    print("-- M3. the three outputs carry DIFFERENT track sets")
    got = {
        "stream": [int(x["track"]) for x in stream],
        "record": [int(x["track"]) for x in record],
        "replay": [int(x["track"]) for x in slots_of(tracks, "replay")],
    }
    want = {"stream": STREAM_TRACKS, "record": RECORD_TRACKS, "replay": REPLAY_TRACKS}
    if got != want:
        raise Failure(f"outputs carry {got}, asked for {want}")
    if len({tuple(v) for v in got.values()}) != 3:
        raise Failure(f"the three outputs do not actually differ: {got}")
    print(f"   OK  {got}")

    print("-- M4. capabilities.audio_tracks reflects the slots really bound")
    caps = await s.vendor("GetCapabilities")
    entry = (caps.get("capabilities") or {}).get("audio_tracks") or {}
    if entry.get("bound") != len(STREAM_TRACKS):
        raise Failure(
            f"audio_tracks.bound is {entry.get('bound')!r}, expected "
            f"{len(STREAM_TRACKS)} (the streaming output's bound slots)")
    published = [int(v["value"]) for v in (entry.get("tracks") or [])]
    if published != STREAM_TRACKS:
        raise Failure(
            f"audio_tracks.tracks is {published}, expected {STREAM_TRACKS} -- with "
            f"non-contiguous routing the slot index is not the track number")
    print(f"   OK  count={entry.get('count')} bound={entry.get('bound')} tracks={published}")


# --------------------------------------------------------------------------
# M2 -- the flow, measured on the bus the encoder reads
# --------------------------------------------------------------------------
async def set_only_track(s: Session, name: str, track: int) -> None:
    payload = {str(t): (t == track) for t in range(1, TRACK_COUNT + 1)}
    resp = await s.request("SetInputAudioTracks",
                           {"inputName": name, "inputAudioTracks": payload})
    status = resp["requestStatus"]
    if not status.get("result"):
        raise Failure(
            f"SetInputAudioTracks({name}, only track {track}) was refused: {status}. "
            f"Track {track} is bound on the streaming output, so this refusal is a "
            f"defect of the #157 oracle, not of the routing")


async def mute_source(s: Session, name: str) -> None:
    """Take a source off every track. Only DISABLE calls, so the #157 oracle
    never has to judge them."""
    await s.ok("SetInputAudioTracks", {
        "inputName": name,
        "inputAudioTracks": {str(t): False for t in range(1, 7)},
    })


async def measure(s: Session) -> dict[int, float]:
    flow = await s.vendor("MeasureAudioTrackFlow", {"duration_ms": MEASURE_MS})
    return peaks_by_track(flow)


async def assert_flow(s: Session, tone_path: pathlib.Path) -> None:
    print("-- M2. an input routed to track N is CONSUMED by track N (flow, not mixer bits)")

    program = await s.ok("GetCurrentProgramScene")
    scene = program.get("currentProgramSceneName") or program.get("sceneName")
    if not scene:
        raise Failure("GetCurrentProgramScene named no scene")

    # The boot desktop-audio source carries libobs' 0xFF default and would feed
    # every mix. Take it off the buses so what is measured is the tone alone.
    try:
        await mute_source(s, DESKTOP_AUDIO_NAME)
    except Failure as exc:
        print(f"   note: {DESKTOP_AUDIO_NAME} not mutable ({exc}); the differential "
              f"assertions below still hold")

    await s.ok("CreateInput", {
        "sceneName": scene,
        "inputName": INPUT_NAME,
        "inputKind": INPUT_KIND,
        "inputSettings": {
            "local_file": str(tone_path),
            "is_local_file": True,
            "looping": True,
            "close_when_inactive": False,
            "restart_on_activate": True,
        },
    })
    try:
        await set_only_track(s, INPUT_NAME, 3)

        # The media source decodes asynchronously; wait for the tone to exist
        # at all before asserting anything about WHERE it is.
        deadline = time.monotonic() + TONE_SETTLE_TIMEOUT_S
        peaks: dict[int, float] = {}
        while time.monotonic() < deadline:
            peaks = await measure(s)
            if peaks.get(3, 0.0) >= FLOW_PRESENT:
                break
            await asyncio.sleep(0.5)
        else:
            raise Failure(
                f"no audio ever reached track 3 within {TONE_SETTLE_TIMEOUT_S:.0f}s: "
                f"peaks {peaks}. The input's mixer bit for track 3 is set (the call "
                f"succeeded) and nothing consumes it -- which is precisely the state "
                f"an input-side read would have called a success")

        print(f"   routed to track 3 -> peaks {peaks}")
        if peaks.get(1, 0.0) > FLOW_SILENT or peaks.get(2, 0.0) > FLOW_SILENT:
            raise Failure(
                f"tracks 1/2 carry signal while only track 3 is routed: {peaks} -- "
                f"the routing is not honoured per track")

        # The discriminating case, #157 seen from the flow side: a track the
        # input reports enabled, that carries the signal, and that NO encoder
        # consumes.
        await s.ok("SetInputAudioTracks",
                   {"inputName": INPUT_NAME, "inputAudioTracks": {"3": True}})
        flow = await s.vendor("MeasureAudioTrackFlow", {"duration_ms": MEASURE_MS})
        bound = bound_by_track(flow)
        flowing = peaks_by_track(flow)
        unbacked = [t for t, is_bound in bound.items() if not is_bound]
        if not unbacked:
            raise Failure(
                "every track of this spawn carries an encoder, so the probe cannot "
                "distinguish 'routed and consumed' from 'routed and dropped'; "
                "reduce PULSAR_STREAM_AUDIO_TRACKS")
        blind = [t for t in unbacked if flowing.get(t, 0.0) >= FLOW_PRESENT]
        if not blind:
            raise Failure(
                f"no unbacked track carries the tone ({flowing}, unbacked {sorted(unbacked)}) "
                f"-- the discriminating case was not exercised, so this run does not "
                f"show that 'signal present on the mix' and 'consumed by an encoder' "
                f"are two different facts")
        print(f"   tracks carrying the tone with NO encoder on the streaming output: "
              f"{sorted(blind)} -- an input-side read reports them enabled all the same")

        # Now move the same input to track 1 and require the signal to MOVE.
        await set_only_track(s, INPUT_NAME, 1)
        deadline = time.monotonic() + TONE_SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            peaks = await measure(s)
            if peaks.get(1, 0.0) >= FLOW_PRESENT:
                break
            await asyncio.sleep(0.5)
        print(f"   re-routed to track 1 -> peaks {peaks}")
        if peaks.get(1, 0.0) < FLOW_PRESENT:
            raise Failure(f"track 1 carries nothing after re-routing: {peaks}")
        if peaks.get(3, 0.0) > FLOW_SILENT:
            raise Failure(
                f"track 3 still carries the signal after the input was moved to "
                f"track 1: {peaks} -- the flow does not follow the routing, so a "
                f"'success' on SetInputAudioTracks would again mean nothing")
        print("   OK  the signal FOLLOWS the routing, track by track")
    finally:
        await s.request("RemoveInput", {"inputName": INPUT_NAME})


# --------------------------------------------------------------------------
# M5 -- non-regression
# --------------------------------------------------------------------------
async def assert_single_track(s: Session) -> None:
    print("-- M5. a spawn with no audio env is the pre-#168 wiring, unchanged")
    tracks = await s.vendor("GetAudioTracks")
    for output in ("stream", "record", "replay"):
        slots = slots_of(tracks, output)
        got = [(int(x["slot"]), int(x["track"])) for x in slots]
        if got != [(0, 1)]:
            raise Failure(
                f"the {output} output carries {got}; with no audio env it must carry "
                f"exactly one encoder, at slot 0, on track 1")
    caps = await s.vendor("GetCapabilities")
    entry = (caps.get("capabilities") or {}).get("audio_tracks") or {}
    if entry.get("bound") != 1 or [int(v["value"]) for v in (entry.get("tracks") or [])] != [1]:
        raise Failure(f"audio_tracks is {entry!r}, expected bound=1 tracks=[1]")
    print("   OK  one encoder, slot 0, track 1, on all three outputs")


# --------------------------------------------------------------------------
async def run(exe: pathlib.Path, workdir: pathlib.Path) -> None:
    tone = workdir / "tone.wav"
    write_tone(tone)

    multitrack_env = {
        "PULSAR_AUDIO_TRACKS": str(TRACK_COUNT),
        "PULSAR_STREAM_AUDIO_TRACKS": ",".join(str(t) for t in STREAM_TRACKS),
        "PULSAR_RECORD_AUDIO_TRACKS": ",".join(str(t) for t in RECORD_TRACKS),
        "PULSAR_REPLAY_AUDIO_TRACKS": ",".join(str(t) for t in REPLAY_TRACKS),
        "PULSAR_AUDIO_BITRATE_2": str(TRACK2_BITRATE),
    }

    proc = PulsarProcess(exe, free_port(), secrets.token_urlsafe(16),
                         str(workdir / "recordings-multi"), multitrack_env)
    proc.spawn()
    try:
        url, pw = proc.wait_ready(READY_TIMEOUT_S)
        session = await connect(url, pw)
        try:
            await assert_wiring(session)
            await assert_flow(session, tone)
        finally:
            await session.ws.close()
    except Failure as exc:
        raise Failure(f"{exc}\n{proc.diag()}") from None
    finally:
        proc.shutdown()

    proc = PulsarProcess(exe, free_port(), secrets.token_urlsafe(16),
                         str(workdir / "recordings-single"), {})
    proc.spawn()
    try:
        url, pw = proc.wait_ready(READY_TIMEOUT_S)
        session = await connect(url, pw)
        try:
            await assert_single_track(session)
        finally:
            await session.ws.close()
    except Failure as exc:
        raise Failure(f"{exc}\n{proc.diag()}") from None
    finally:
        proc.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar multi-track audio probe (#168)")
    ap.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    args = ap.parse_args()

    if not args.exe.is_file():
        print(f"error: pulsar.exe not found at {args.exe} -- build it first (scripts/build-win.ps1)")
        return 2

    with tempfile.TemporaryDirectory(prefix="pulsar-probe-multitrack-") as tmp:
        try:
            asyncio.run(run(args.exe, pathlib.Path(tmp)))
        except Failure as exc:
            print(f"\nFAIL: {exc}")
            return 1

    print("\nprobe-audio-multitrack: PASS -- N encoders on distinct slots, per-output "
          "track sets, and a routed input measurably consumed by its own track")
    return 0


if __name__ == "__main__":
    sys.exit(main())
