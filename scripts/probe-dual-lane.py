#!/usr/bin/env python3
"""Runtime probe for Pulsar's hot A/B scene lanes (issue #244).

The probe drives the public obs-websocket v5 boundary only.  It starts with
one logical scene on air, alternates a second scene into Preview, and commits
``--takes`` studio-mode Cuts.  Pulsar's structured dual-lane logs expose the
physical roots, stable view/video identities, and frame-boundary frame/PTS
commit; those values are checked for every Take.

Run the two acceptance campaigns independently against the same build::

    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder x264 --takes 100
    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder nvenc --takes 100

Exit codes are 0 (pass), 1 (assertion/runtime failure), 2 (usage or missing
WebSocket dependency), and 3 (typed environment skip, for example no binary
or no NVENC device).  This probe validates the routing and identity contract;
the raw NV12 time-code probe remains the pixel-level proof for no mixed frame.

The process boundary is deliberate: no libobs/OBS DLL is loaded and no native
object is accessed from Python.  Only obs-websocket v5 JSON frames and the
Pulsar child process's structured diagnostics are used.
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
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any

try:
    import websockets
except ImportError:
    print("error: pip install websockets (pure WebSocket client)", file=sys.stderr)
    raise SystemExit(2)


EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_SKIP = 3

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
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

READY_RE = re.compile(r"PULSAR_READY ws=(\S+) password=(\S+)")
DUAL_READY_RE = re.compile(
    r"\[pulsar-dual-lane\] ready LaneA=(\S+) LaneB=(\S+) "
    r"ProgramView=(\S+) PreviewView=(\S+) ProgramVideo=(\S+) PreviewVideo=(\S+)"
)
ENCODER_RE = re.compile(r"video encoder allocated: family=(\S+) id=(\S+)")
COMMIT_RE = re.compile(
    r"\[pulsar-dual-lane\] TakeCommitted count=(\d+) frame_id=(\d+) "
    r"pts_ns=(\d+) onair_lane=(-?\d+) preview_lane=(-?\d+) "
    r"OnAirRoot=(\S+) PreviewRoot=(\S+) ProgramView=(\S+) "
    r"PreviewView=(\S+) ProgramVideo=(\S+) PreviewVideo=(\S+)"
)

CANVAS_W = 1920
CANVAS_H = 1080
SCENE_A = "probe-dual-lane-A"
SCENE_B = "probe-dual-lane-B"
INPUT_A = "probe-dual-lane-color-A"
INPUT_B = "probe-dual-lane-color-B"
COLOR_RED_ABGR = 0xFF0000FF
COLOR_GREEN_ABGR = 0xFF00FF00


class ProbeFailure(RuntimeError):
    """A failed runtime assertion."""


class ProbeSkip(RuntimeError):
    """A reproducible environment limitation, not a product pass/fail."""


def choose_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


class PulsarProcess:
    """Spawn Pulsar and retain structured stdout for identity assertions."""

    def __init__(self, exe: pathlib.Path, encoder: str, record_dir: pathlib.Path) -> None:
        self.exe = exe
        self.encoder = encoder
        self.record_dir = record_dir
        self.port = choose_port()
        self.password = secrets.token_urlsafe(24)
        self.proc: subprocess.Popen[str] | None = None
        self.lines: list[str] = []
        self.condition = threading.Condition()
        self.thread: threading.Thread | None = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        env["PULSAR_RECORD_DIR"] = str(self.record_dir)
        env["PULSAR_VIDEO_ENCODER"] = self.encoder
        if self.encoder == "nvenc":
            # p1 is accepted by the current NVENC family and makes an
            # accidental x264 fallback visible in the boot log check below.
            env["PULSAR_VIDEO_PRESET"] = "p1"
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)

        creationflags = 0x08000000 if os.name == "nt" else 0
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
        self.thread = threading.Thread(target=self._pump, name="pulsar-probe-log", daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            with self.condition:
                self.lines.append(line.rstrip("\r\n"))
                self.condition.notify_all()

    def snapshot(self) -> list[str]:
        with self.condition:
            return list(self.lines)

    def wait_for(self, pattern: re.Pattern[str], timeout: float) -> re.Match[str]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for line in self.lines:
                    match = pattern.search(line)
                    if match:
                        return match
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = self.proc.poll() if self.proc is not None else None
                    tail = "\n".join(f"  | {line}" for line in self.lines[-40:])
                    raise ProbeFailure(
                        f"timeout waiting for {pattern.pattern!r}; exit={status}\n{tail}"
                    )
                self.condition.wait(timeout=min(0.25, remaining))

    def wait_for_commit(self, count: int, timeout: float) -> re.Match[str]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for line in self.lines:
                    match = COMMIT_RE.search(line)
                    if match and int(match.group(1)) == count:
                        return match
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    tail = "\n".join(f"  | {line}" for line in self.lines[-60:])
                    raise ProbeFailure(f"TakeCommitted count={count} not observed\n{tail}")
                self.condition.wait(timeout=min(0.25, remaining))

    def shutdown(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=8)
            except Exception:
                pass


class Inbox:
    """Small v5 response/event collector for one WebSocket connection."""

    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def store(self, message: dict[str, Any]) -> None:
        if message.get("op") == 7:
            self.responses.append(message.get("d", {}))
        elif message.get("op") == 5:
            self.events.append(message.get("d", {}))

    async def receive_until_response(self, ws: Any, request_id: str) -> dict[str, Any]:
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if message.get("op") == 7:
                data = message.get("d", {})
                if data.get("requestId") == request_id:
                    return data
                self.responses.append(data)
            elif message.get("op") == 5:
                self.events.append(message.get("d", {}))


async def request(
    inbox: Inbox, ws: Any, request_type: str, request_id: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    request_body: dict[str, Any] = {
        "requestType": request_type,
        "requestId": request_id,
    }
    if data is not None:
        request_body["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": request_body}))
    return await inbox.receive_until_response(ws, request_id)


def assert_success(response: dict[str, Any], operation: str) -> None:
    status = response.get("requestStatus") or {}
    if not status.get("result"):
        raise ProbeFailure(f"{operation} declined: {status}")


async def wait_event(
    inbox: Inbox,
    ws: Any,
    event_type: str,
    predicate: Any,
    timeout: float = 15,
) -> dict[str, Any]:
    """Wait for one v5 event while retaining unrelated messages."""

    def take_matching() -> dict[str, Any] | None:
        for index, event in enumerate(inbox.events):
            if event.get("eventType") != event_type:
                continue
            data = event.get("eventData") or {}
            if predicate is None or predicate(data):
                return inbox.events.pop(index)
        return None

    event = take_matching()
    if event is not None:
        return event

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeFailure(f"timeout waiting for event {event_type!r}")
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if message.get("op") == 5:
            inbox.events.append(message.get("d", {}))
        elif message.get("op") == 7:
            inbox.responses.append(message.get("d", {}))
        event = take_matching()
        if event is not None:
            return event


async def identify(ws: Any, password: str) -> None:
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    if hello.get("op") != 0:
        raise ProbeFailure(f"expected obs-websocket Hello, got {hello}")
    hello_data = hello.get("d") or {}
    identify_data: dict[str, Any] = {
        "rpcVersion": hello_data.get("rpcVersion", 1),
        "eventSubscriptions": 0x7FF,
    }
    auth = hello_data.get("authentication")
    if auth:
        identify_data["authentication"] = compute_auth(
            password, auth["salt"], auth["challenge"]
        )
    await ws.send(json.dumps({"op": 1, "d": identify_data}))
    identified = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    if identified.get("op") != 2:
        raise ProbeFailure(f"obs-websocket Identify failed: {identified}")


@dataclass(frozen=True)
class ReadyIdentity:
    lane_a: str
    lane_b: str
    program_view: str
    preview_view: str
    program_video: str
    preview_video: str


@dataclass(frozen=True)
class Commit:
    count: int
    frame_id: int
    pts_ns: int
    onair_lane: int
    preview_lane: int
    onair_root: str
    preview_root: str
    program_view: str
    preview_view: str
    program_video: str
    preview_video: str


def normalise_pointer(value: str) -> str:
    pointer = value.lower().removeprefix("0x").lstrip("0")
    return pointer or "0"


def parse_ready(match: re.Match[str]) -> ReadyIdentity:
    return ReadyIdentity(*(normalise_pointer(match.group(i)) for i in range(1, 7)))


def parse_commit(match: re.Match[str]) -> Commit:
    return Commit(
        count=int(match.group(1)),
        frame_id=int(match.group(2)),
        pts_ns=int(match.group(3)),
        onair_lane=int(match.group(4)),
        preview_lane=int(match.group(5)),
        onair_root=normalise_pointer(match.group(6)),
        preview_root=normalise_pointer(match.group(7)),
        program_view=normalise_pointer(match.group(8)),
        preview_view=normalise_pointer(match.group(9)),
        program_video=normalise_pointer(match.group(10)),
        preview_video=normalise_pointer(match.group(11)),
    )


def validate_commit(identity: ReadyIdentity, previous: Commit | None, commit: Commit) -> None:
    if commit.onair_lane not in (0, 1) or commit.preview_lane not in (0, 1):
        raise ProbeFailure(f"invalid role lanes in commit: {commit}")
    if commit.onair_lane == commit.preview_lane:
        raise ProbeFailure(f"OnAir and Preview lanes collided: {commit}")
    lane_roots = (identity.lane_a, identity.lane_b)
    if commit.onair_root != lane_roots[commit.onair_lane]:
        raise ProbeFailure(
            f"OnAir root does not match physical lane {commit.onair_lane}: {commit}"
        )
    if commit.preview_root != lane_roots[commit.preview_lane]:
        raise ProbeFailure(
            f"Preview root does not match physical lane {commit.preview_lane}: {commit}"
        )
    if (commit.program_view, commit.preview_view) != (
        identity.program_view,
        identity.preview_view,
    ):
        raise ProbeFailure(f"ProgramView/PreviewView identity changed: {commit}")
    if (commit.program_video, commit.preview_video) != (
        identity.program_video,
        identity.preview_video,
    ):
        raise ProbeFailure(f"ProgramVideo/PreviewVideo identity changed: {commit}")
    if previous is not None:
        if commit.count != previous.count + 1:
            raise ProbeFailure(f"non-contiguous Take count: previous={previous} current={commit}")
        if commit.frame_id <= previous.frame_id:
            raise ProbeFailure(f"frame_id did not increase: previous={previous} current={commit}")
        if commit.pts_ns <= previous.pts_ns:
            raise ProbeFailure(f"PTS did not increase: previous={previous} current={commit}")


def find_ffprobe() -> str | None:
    """Use the ffprobe shipped with the OBS dependencies, then PATH."""

    for candidate in (REPO_ROOT / "upstream/.deps").glob("obs-deps-*-x64/bin/ffprobe.exe"):
        return str(candidate)
    return shutil.which("ffprobe")


def verify_recording(path_text: str, ffprobe: str) -> None:
    path = pathlib.Path(path_text)
    if not path.is_file():
        raise ProbeFailure(f"recording output does not exist: {path}")
    if path.stat().st_size < 100 * 1024:
        raise ProbeFailure(f"recording output is implausibly small: {path.stat().st_size} bytes")

    try:
        raw = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        info = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ProbeFailure(f"ffprobe failed for {path}: {exc}") from exc

    streams = info.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    if len(videos) != 1:
        raise ProbeFailure(f"expected one video stream, got {len(videos)}")
    video = videos[0]
    if video.get("codec_name") != "h264":
        raise ProbeFailure(f"expected H.264 video, got {video.get('codec_name')!r}")
    if (video.get("width"), video.get("height")) != (CANVAS_W, CANVAS_H):
        raise ProbeFailure(
            f"expected {CANVAS_W}x{CANVAS_H}, got {video.get('width')}x{video.get('height')}"
        )

    rate_text = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        numerator, denominator = rate_text.split("/", 1)
        frame_rate = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        frame_rate = 0.0
    if abs(frame_rate - 60.0) > 0.5:
        raise ProbeFailure(f"expected 60fps video, got {rate_text!r}")

    frame_text = video.get("nb_read_frames") or video.get("nb_frames")
    if frame_text not in (None, "N/A") and int(frame_text) < 60:
        raise ProbeFailure(f"recording contains too few frames: {frame_text}")
    duration_text = video.get("duration") or (info.get("format") or {}).get("duration")
    if duration_text in (None, "N/A") or float(duration_text) <= 0.5:
        raise ProbeFailure(f"recording has no useful duration: {duration_text!r}")

    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(audios) != 1 or audios[0].get("codec_name") != "aac":
        raise ProbeFailure(f"expected one AAC audio stream, got {audios}")
    print(
        f"   recording verified: {path.name} {path.stat().st_size} bytes, "
        f"H.264 {CANVAS_W}x{CANVAS_H} {frame_rate:g}fps, frames={frame_text}, "
        f"duration={float(duration_text):.3f}s, AAC"
    )


async def create_scene(inbox: Inbox, ws: Any, scene: str, input_name: str, color: int) -> None:
    response = await request(inbox, ws, "CreateScene", f"create-scene-{scene}", {"sceneName": scene})
    assert_success(response, f"CreateScene({scene})")
    response = await request(
        inbox,
        ws,
        "CreateInput",
        f"create-input-{scene}",
        {
            "sceneName": scene,
            "inputName": input_name,
            "inputKind": "color_source_v3",
            "inputSettings": {"color": color, "width": CANVAS_W, "height": CANVAS_H},
            "sceneItemEnabled": True,
        },
    )
    assert_success(response, f"CreateInput({scene})")


async def drive(process: PulsarProcess, takes: int) -> list[Commit]:
    ready_match = process.wait_for(READY_RE, timeout=60)
    ws_url = ready_match.group(1)
    ready_password = ready_match.group(2)
    if ready_password != process.password:
        raise ProbeFailure("PULSAR_READY password did not match the generated probe secret")

    identity = parse_ready(process.wait_for(DUAL_READY_RE, timeout=60))
    if identity.lane_a == identity.lane_b:
        raise ProbeFailure(f"LaneA and LaneB are aliased: {identity}")
    if identity.program_view == identity.preview_view:
        raise ProbeFailure(f"ProgramView and PreviewView are aliased: {identity}")
    if identity.program_video == identity.preview_video:
        raise ProbeFailure(f"ProgramVideo and PreviewVideo are aliased: {identity}")

    encoder_match = process.wait_for(ENCODER_RE, timeout=60)
    actual_family = encoder_match.group(1).lower()
    if actual_family != process.encoder:
        if process.encoder == "nvenc":
            raise ProbeSkip(
                f"requested NVENC but Pulsar boot selected {actual_family}; no usable NVENC device"
            )
        raise ProbeFailure(
            f"requested encoder family {process.encoder!r}, boot selected {actual_family!r}"
        )
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise ProbeSkip("ffprobe is required for the active-recording acceptance proof")

    async with websockets.connect(
        ws_url, subprotocols=["obswebsocket.json"], open_timeout=15
    ) as ws:
        await identify(ws, process.password)
        inbox = Inbox()
        await create_scene(inbox, ws, SCENE_A, INPUT_A, COLOR_RED_ABGR)
        await create_scene(inbox, ws, SCENE_B, INPUT_B, COLOR_GREEN_ABGR)

        # Establish a known program before studio mode.  The non-studio path
        # mutates the active lane composition but keeps the physical root.
        response = await request(
            inbox,
            ws,
            "SetCurrentProgramScene",
            "set-initial-program",
            {"sceneName": SCENE_A},
        )
        assert_success(response, "SetCurrentProgramScene(A)")
        response = await request(
            inbox,
            ws,
            "SetStudioModeEnabled",
            "enable-studio",
            {"studioModeEnabled": True},
        )
        assert_success(response, "SetStudioModeEnabled(true)")

        # Start a real local recording before the first Cut.  This makes the
        # encoder active for the whole campaign and exercises the exact
        # constraint that no Take may call obs_encoder_set_video again.
        response = await request(inbox, ws, "StartRecord", "start-record")
        assert_success(response, "StartRecord")
        await wait_event(
            inbox,
            ws,
            "RecordStateChanged",
            lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED",
        )
        bind_lines = [
            line
            for line in process.snapshot()
            if "[pulsar-dual-lane] encoder video_t bound once to ProgramView" in line
        ]
        if len(bind_lines) != 1:
            raise ProbeFailure(
                f"expected exactly one setup-time encoder bind before active record, got {len(bind_lines)}"
            )
        # Give the recording path a few frames before stressing the Cut loop.
        await asyncio.sleep(0.5)

        commits: list[Commit] = []
        for number in range(1, takes + 1):
            target = SCENE_B if number % 2 else SCENE_A
            response = await request(
                inbox,
                ws,
                "SetCurrentPreviewScene",
                f"set-preview-{number}",
                {"sceneName": target},
            )
            assert_success(response, f"SetCurrentPreviewScene({target})")
            response = await request(
                inbox,
                ws,
                "TriggerStudioModeTransition",
                f"take-{number}",
            )
            assert_success(response, f"TriggerStudioModeTransition({number})")
            commit = parse_commit(process.wait_for_commit(number, timeout=15))
            validate_commit(identity, commits[-1] if commits else None, commit)
            commits.append(commit)
            if number in (1, takes) or number % 25 == 0:
                print(
                    f"   Take {number:03d}: frame_id={commit.frame_id} pts_ns={commit.pts_ns} "
                    f"onair_lane={commit.onair_lane} preview_lane={commit.preview_lane}"
                )

        response = await request(inbox, ws, "StopRecord", "stop-record")
        assert_success(response, "StopRecord")
        stopped = await wait_event(
            inbox,
            ws,
            "RecordStateChanged",
            lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED",
        )
        output_path = (stopped.get("eventData") or {}).get("outputPath") or ""
        if not output_path:
            raise ProbeFailure("RecordStateChanged STOPPED did not include outputPath")

        all_commits = [
            parse_commit(match)
            for line in process.snapshot()
            if (match := COMMIT_RE.search(line)) is not None
        ]
        if len(all_commits) != takes or [commit.count for commit in all_commits] != list(range(1, takes + 1)):
            raise ProbeFailure(
                f"expected exactly {takes} contiguous TakeCommitted logs, got "
                f"{[commit.count for commit in all_commits]}"
            )
        first_commit_index = next(
            index
            for index, line in enumerate(process.snapshot())
            if COMMIT_RE.search(line)
        )
        bind_index = next(
            index
            for index, line in enumerate(process.snapshot())
            if "[pulsar-dual-lane] encoder video_t bound once to ProgramView" in line
        )
        if bind_index >= first_commit_index:
            raise ProbeFailure("encoder video_t bind was not completed before the first Take")
        verify_recording(output_path, ffprobe)

    return commits


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    parser.add_argument("--encoder", choices=("x264", "nvenc"), required=True)
    parser.add_argument("--takes", type=int, default=100)
    args = parser.parse_args(argv)
    if args.takes < 1:
        parser.error("--takes must be >= 1")
    return args


def run(args: argparse.Namespace) -> int:
    if not args.exe.is_file():
        print(f"SKIP: Pulsar binary not found: {args.exe}")
        return EXIT_SKIP

    with tempfile.TemporaryDirectory(prefix="pulsar-dual-lane-") as record_dir_text:
        process = PulsarProcess(args.exe.resolve(), args.encoder, pathlib.Path(record_dir_text))
        try:
            process.spawn()
            print(f"dual-lane probe: encoder={args.encoder} takes={args.takes} exe={args.exe}")
            commits = asyncio.run(drive(process, args.takes))
            print(
                f"PASS: {len(commits)} Takes; LaneA/LaneB, ProgramView/PreviewView and "
                "ProgramVideo/PreviewVideo remained stable; frame_id/PTS monotone"
            )
            return 0
        except ProbeSkip as exc:
            print(f"SKIP: {exc}")
            return EXIT_SKIP
        except (ProbeFailure, asyncio.TimeoutError, OSError, json.JSONDecodeError) as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return EXIT_FAIL
        finally:
            process.shutdown()


def main() -> int:
    try:
        args = parse_args(sys.argv[1:])
    except SystemExit as exc:
        return int(exc.code)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
