#!/usr/bin/env python3
"""Probe Fade/Stinger aborts at the raw Program frame boundary.

This is an opt-in runtime probe for #250.  It drives the versioned
``pulsar-scene-switch`` vendor, requests an abort while a transition is still
Queued and again after ``transition_final_queued``, then decodes the retained
recording as raw RGB frames.  The pixel check is deliberately independent of
the structured booleans: every frame around the observed abort boundary must
be a complete red/green lane colour, never black or an intermediate blend.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import Any

import websockets


ROOT = pathlib.Path(__file__).resolve().parents[1]
DUAL_LANE = ROOT / "scripts" / "probe-dual-lane.py"
SPEC = importlib.util.spec_from_file_location("pulsar_dual_lane_boundary", DUAL_LANE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load shared probe: {DUAL_LANE}")
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


ABORT_RE = probe.DUAL_LANE_LOG_PREFIX + (
    r"\s*transition_aborted fallback=cut fallback_to_cut=1 frame_id=(\d+) pts_ns=(\d+) "
    r"reason=(\S+) role_map_preserved=(\d) surfaces_stable=(\d) "
    r"video_t_stable=(\d) invariant_valid=(\d)"
)
ABORT_PATTERN = re.compile(ABORT_RE)
FINAL_QUEUED_PATTERN = re.compile(probe.DUAL_LANE_LOG_PREFIX + r"\s*transition_final_queued kind=(\S+)")
TRANSITION_COMMITTED_PATTERN = re.compile(
    probe.DUAL_LANE_LOG_PREFIX
    + r"\s*transition_committed kind=(\S+) requested_duration_ms=(\d+) "
    r"actual_duration_ms=(\d+) start_frame_id=(\d+) start_pts_ns=(\d+) "
    r"end_frame_id=(\d+) end_pts_ns=(\d+) fallback_to_cut=(\d+)"
)
WIDTH = probe.CANVAS_W
HEIGHT = probe.CANVAS_H
FRAME_BYTES = WIDTH * HEIGHT * 3
COLOURS = {"red": (220, 40, 40), "green": (40, 220, 40)}
LANE_DISTANCE_SQ = 18_000
PALETTE_BIN_SIZE = 16


class BoundaryDemux:
    """Single WebSocket reader/demultiplexer for responses and events."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.responses: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.buffered_responses: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.events_changed = asyncio.Event()
        self.reader: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self.reader is None:
            return
        self.reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.reader
        self.reader = None

    async def _read_loop(self) -> None:
        try:
            while True:
                message = json.loads(await self.ws.recv())
                if message.get("op") == 7:
                    data = message.get("d", {})
                    request_id = data.get("requestId")
                    future = self.responses.pop(request_id, None)
                    if future is None:
                        self.buffered_responses[request_id] = data
                    elif not future.done():
                        future.set_result(data)
                elif message.get("op") == 5:
                    self.events.append(message.get("d", {}))
                    self.events_changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in self.responses.values():
                if not future.done():
                    future.set_exception(probe.ProbeFailure(f"WebSocket demux stopped: {exc}"))

    async def request(self, request_type: str, request_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        buffered = self.buffered_responses.pop(request_id, None)
        if buffered is not None:
            return buffered
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.responses[request_id] = future
        request_body: dict[str, Any] = {"requestType": request_type, "requestId": request_id}
        if data is not None:
            request_body["requestData"] = data
        try:
            await self.ws.send(json.dumps({"op": 6, "d": request_body}))
            return await asyncio.wait_for(future, timeout=15)
        finally:
            self.responses.pop(request_id, None)

    async def wait_event(self, event_type: str, predicate: Any, timeout: float = 15) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for index, event in enumerate(self.events):
                if event.get("eventType") != event_type:
                    continue
                data = event.get("eventData") or {}
                if predicate is None or predicate(data):
                    return self.events.pop(index)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise probe.ProbeFailure(f"timeout waiting for event {event_type!r}")
            self.events_changed.clear()
            try:
                await asyncio.wait_for(self.events_changed.wait(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise probe.ProbeFailure(f"timeout waiting for event {event_type!r}") from exc


def classify_raw_frame(frame: bytes) -> str:
    """Classify one decoded Program frame against the observed lane palette.

    A real WebM Stinger is not required to be a solid red/green frame: opaque
    or alpha-composited source colours are accepted as one coherent
    ``stinger`` palette.  Red/green mixing and colours in the lane-to-lane
    interpolation envelope remain failures because those indicate a torn
    Program seam rather than a valid Stinger composition.
    """

    if len(frame) != FRAME_BYTES:
        raise probe.ProbeFailure(f"raw Program frame has {len(frame)} bytes, expected {FRAME_BYTES}")
    samples = range(0, len(frame), max(3, len(frame) // 12000 // 3 * 3))
    votes = {name: 0 for name in COLOURS}
    palette: dict[tuple[int, int, int], int] = {}
    black = 0
    for offset in samples:
        red, green, blue = frame[offset : offset + 3]
        if max(red, green, blue) <= 8:
            black += 1
            continue
        if red > 60 and green > 60 and blue < 100 and abs(red - green) < 100 and red + green > 180:
            raise probe.ProbeFailure(
                f"raw Program frame contains an intermediate/mixed colour at byte {offset}: {(red, green, blue)}"
            )
        nearest = min(
            COLOURS,
            key=lambda name: sum((channel - expected) ** 2 for channel, expected in zip((red, green, blue), COLOURS[name])),
        )
        expected = COLOURS[nearest]
        distance = sum((channel - target) ** 2 for channel, target in zip((red, green, blue), expected))
        if distance <= LANE_DISTANCE_SQ:
            votes[nearest] += 1
            continue
        # A colour that is inside the red/green lane envelope is an
        # intermediate blend, never a valid opaque Stinger palette sample.
        if red > 45 and green > 45 and blue < min(red, green) * 0.75 and abs(red - green) < 150:
            raise probe.ProbeFailure(
                f"raw Program frame contains an intermediate/mixed colour at byte {offset}: {(red, green, blue)}"
            )
        quantized = tuple(channel // PALETTE_BIN_SIZE for channel in (red, green, blue))
        palette[quantized] = palette.get(quantized, 0) + 1
    total = sum(votes.values()) + sum(palette.values()) + black
    if total == 0 or black > total // 100:
        raise probe.ProbeFailure(f"raw Program frame is black/empty: black={black} votes={votes} palette={palette}")
    visible = total - black
    if votes["red"] * 20 > visible and votes["green"] * 20 > visible:
        raise probe.ProbeFailure(f"raw Program frame is mixed red/green: black={black} votes={votes}")
    lane, lane_count = max(votes.items(), key=lambda item: item[1])
    palette_count = sum(palette.values())
    if lane_count * 100 >= visible * 80 and palette_count <= visible * 20:
        return lane
    if palette:
        _, dominant_palette_count = max(palette.items(), key=lambda item: item[1])
        if dominant_palette_count * 100 >= max(1, palette_count) * 55 and palette_count + lane_count == visible:
            return "stinger"
    raise probe.ProbeFailure(f"raw Program frame is mixed: black={black} votes={votes} palette={palette}")


def decode_raw_recording(ffmpeg: str, recording: pathlib.Path) -> list[str]:
    """Decode the recording to raw RGB and classify every complete frame."""

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(recording),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-vsync",
        "0",
        "pipe:1",
    ]
    try:
        raw = subprocess.check_output(command, stderr=subprocess.STDOUT, timeout=60)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise probe.ProbeFailure(f"raw Program recording decode failed: {exc}") from exc
    if len(raw) < FRAME_BYTES or len(raw) % FRAME_BYTES:
        raise probe.ProbeFailure(f"raw Program recording has incomplete frames: {len(raw)} bytes")
    return [classify_raw_frame(raw[offset : offset + FRAME_BYTES]) for offset in range(0, len(raw), FRAME_BYTES)]


def assert_abort_pixels(labels: list[str], boundary_index: int, *, expected: str) -> None:
    """Require complete lane frames around the observed boundary."""

    if not labels:
        raise probe.ProbeFailure("raw Program recording contains no decoded frames")
    if boundary_index < 1 or boundary_index >= len(labels) - 1:
        raise probe.ProbeFailure(
            f"abort boundary index {boundary_index} is not surrounded by raw frames (count={len(labels)})"
        )
    window = labels[max(0, boundary_index - 2) : min(len(labels), boundary_index + 3)]
    if any(label not in COLOURS for label in window):
        raise probe.ProbeFailure(f"raw Program boundary contains an unknown frame label: {window}")
    if any(label != expected for label in window):
        raise probe.ProbeFailure(
            f"abort winner exposed a stale/mixed Program frame: expected {expected}, window={window}"
        )


def locate_commit_boundary(labels: list[str], *, before: str, after: str) -> int:
    """Find the unique encoded seam from the settled raw Program timeline.

    The physical ``TakeCommitted`` callback precedes the encoded file by the
    encoder pipeline depth.  A fixed frame offset would therefore make the
    probe accidentally bless a stale frame.  Instead, require one unique
    transition into a settled ``after`` run; any duplicate, missing, or
    unresolved seam fails closed.
    """

    candidates = [
        index
        for index in range(3, len(labels) - 5)
        if labels[index] == after
        and all(label == after for label in labels[index : index + 5])
        and all(label != after for label in labels[index - 3 : index])
    ]
    if len(candidates) != 1:
        raise probe.ProbeFailure(
            f"raw Program commit seam was not uniquely observed: candidates={candidates}"
        )
    boundary_index = candidates[0]
    if not any(label == before for label in labels[:boundary_index]):
        raise probe.ProbeFailure(f"raw Program commit seam has no settled {before} prelude")
    return boundary_index


def assert_commit_pixels(labels: list[str], boundary_index: int, *, before: str, after: str) -> None:
    """Require the encoded seam to settle on the committed lane.

    Composition frames (including a real WebM Stinger palette) may occur
    between the old and new lane.  They are accepted only before the unique
    settled seam; an old frame after the seam is always a failure.
    """

    if boundary_index < 3 or boundary_index >= len(labels) - 5:
        raise probe.ProbeFailure(
            f"commit boundary index {boundary_index} is not surrounded by raw frames (count={len(labels)})"
        )
    before_window = labels[max(0, boundary_index - 3) : boundary_index]
    after_window = labels[boundary_index : boundary_index + 5]
    if not any(label == before for label in labels[:boundary_index]) or any(label != after for label in after_window):
        raise probe.ProbeFailure(
            f"raw Program commit seam was stale/mixed: before={before_window}, after={after_window}"
        )


async def vendor_request(demux: BoundaryDemux, request_id: str, request_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = await demux.request(
        "CallVendorRequest",
        request_id,
        {"vendorName": "pulsar-scene-switch", "requestType": request_type, "requestData": payload},
    )
    probe.assert_success(response, f"CallVendorRequest/{request_type}")
    return ((response.get("responseData") or {}).get("responseData") or {})


async def run_case(exe: pathlib.Path, record_dir: pathlib.Path, transition: str, phase: str) -> dict[str, Any]:
    os.environ["PULSAR_DUAL_LANE_TRANSITIONS"] = "1"
    os.environ["PULSAR_RUNTIME_INSTANCE_ID"] = f"transition-boundary-{transition}-{phase}"
    if transition == "Stinger":
        os.environ["PULSAR_STINGER_ASSET"] = str(ROOT / "scripts" / "assets" / "stinger-demo.webm")
    transition_name = "DualLaneStinger" if transition == "Stinger" else transition
    runtime = probe.PulsarProcess(exe, "x264", record_dir, runtime_id=os.environ["PULSAR_RUNTIME_INSTANCE_ID"])
    demux: BoundaryDemux | None = None
    runtime.spawn()
    try:
        ready = runtime.wait_for(probe.READY_RE, 90)
        async with websockets.connect(ready.group(1), subprotocols=["obswebsocket.json"], open_timeout=20) as ws:
            inbox = probe.Inbox()
            await probe.identify(ws, runtime.password)
            for scene, source, colour in ((probe.SCENE_A, probe.INPUT_A, probe.COLOR_RED_ABGR), (probe.SCENE_B, probe.INPUT_B, probe.COLOR_GREEN_ABGR)):
                await probe.create_scene(inbox, ws, scene, source, colour)
            for request_type, request_id, data in (
                ("SetCurrentProgramScene", "boundary-program", {"sceneName": probe.SCENE_A}),
                ("SetStudioModeEnabled", "boundary-studio", {"studioModeEnabled": True}),
                ("SetCurrentPreviewScene", "boundary-preview", {"sceneName": probe.SCENE_B}),
                ("SetCurrentSceneTransition", "boundary-transition", {"transitionName": transition_name}),
                ("SetCurrentSceneTransitionDuration", "boundary-duration", {"transitionDuration": 200}),
            ):
                probe.assert_success(await probe.request(inbox, ws, request_type, request_id, data), request_type)
            probe.assert_success(await probe.request(inbox, ws, "StartRecord", "boundary-record"), "StartRecord")
            await probe.wait_event(inbox, ws, "RecordStateChanged", lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED")
            demux = BoundaryDemux(ws)
            await demux.start()
            stats = await demux.request("GetStats", "boundary-record-stats")
            probe.assert_success(stats, "GetStats(record start)")
            record_start_frames = int((stats.get("responseData") or {}).get("outputTotalFrames", 0) or 0)

            state = await vendor_request(demux, "boundary-state", "GetState", {})
            lane = state["role_map"]["preview"]
            scene = probe.SCENE_B if lane == "B" else probe.SCENE_A
            intent = f"boundary-intent-{phase}"
            prepare_id = f"boundary-prepare-{phase}"
            take_id = f"boundary-take-{phase}"
            prepare = {
                "contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
                "command_type": "Prepare", "command_id": prepare_id, "intent_id": intent,
                "runtime_instance_id": runtime.runtime_id, "expected_revisions": state["revisions"],
                "expected_server_seq": state["server_seq"], "target": {"lane_id": lane, "scene_id": scene}, "timeout_ms": 5000,
            }
            accepted = await vendor_request(demux, f"call-{prepare_id}", "Prepare", prepare)
            if accepted.get("event_type") != "PrepareAccepted":
                raise probe.ProbeFailure(f"Prepare was not accepted: {accepted}")
            await demux.wait_event("VendorEvent", lambda data: data.get("vendorName") == "pulsar-scene-switch" and (data.get("eventData") or {}).get("event_type") == "PreviewReady" and (data.get("eventData") or {}).get("command_id") == prepare_id)
            state = await vendor_request(demux, f"state-{phase}", "GetState", {})
            take = {
                "contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
                "command_type": "Take", "command_id": take_id, "intent_id": intent,
                "runtime_instance_id": runtime.runtime_id, "expected_revisions": state["revisions"],
                "expected_server_seq": state["server_seq"], "prepared_command_id": prepare_id, "timeout_ms": 5000,
            }
            take_task = asyncio.create_task(vendor_request(demux, f"call-{take_id}", "Take", take))
            if phase == "queued":
                abort_payload = {
                    "contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
                    "command_type": "Abort", "command_id": f"boundary-abort-{phase}", "intent_id": intent,
                    "runtime_instance_id": runtime.runtime_id, "expected_revisions": state["revisions"],
                    "take_command_id": take_id, "reason": "operator",
                }
                await asyncio.sleep(0)
                abort_task = asyncio.create_task(vendor_request(demux, f"call-abort-{phase}", "Abort", abort_payload))
                take_result, abort_result = await asyncio.gather(take_task, abort_task)
            else:
                take_result = await take_task
                if take_result.get("event_type") != "TakeAccepted":
                    raise probe.ProbeFailure(f"Take was not accepted: {take_result}")
                runtime.wait_for(FINAL_QUEUED_PATTERN, 15)
                abort_payload = {
                    "contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
                    "command_type": "Abort", "command_id": f"boundary-abort-{phase}", "intent_id": intent,
                    "runtime_instance_id": runtime.runtime_id, "expected_revisions": state["revisions"],
                    "take_command_id": take_id, "reason": "operator",
                }
                abort_result = await vendor_request(demux, f"call-abort-{phase}", "Abort", abort_payload)
            if abort_result.get("event_type") != "TakeAborted":
                raise probe.ProbeFailure(f"Abort did not win {phase}: {abort_result}")
            abort_match = runtime.wait_for(ABORT_PATTERN, 15)
            fields = [int(abort_match.group(index)) for index in range(4, 8)]
            if fields != [1, 1, 1, 1]:
                raise probe.ProbeFailure(f"abort postconditions were not observed: {abort_match.group(0)}")

            # Keep the observed abort boundary in the recording before driving
            # a control commit.  The dwell is outside the callback and gives
            # the recorder complete post-abort frames to decode.
            await asyncio.sleep(0.25)
            after_abort = await vendor_request(demux, f"state-after-abort-{phase}", "GetState", {})
            if after_abort.get("role_map") != state.get("role_map"):
                raise probe.ProbeFailure(f"abort mutated role map: before={state['role_map']} after={after_abort.get('role_map')}")
            commit_prepare_id = f"boundary-commit-prepare-{phase}"
            commit_take_id = f"boundary-commit-take-{phase}"
            commit_intent = f"boundary-commit-intent-{phase}"
            commit_prepare = {
                **prepare,
                "command_type": "Prepare",
                "command_id": commit_prepare_id,
                "intent_id": commit_intent,
                "expected_revisions": after_abort["revisions"],
                "expected_server_seq": after_abort["server_seq"],
            }
            commit_accepted = await vendor_request(demux, f"call-{commit_prepare_id}", "Prepare", commit_prepare)
            if commit_accepted.get("event_type") != "PrepareAccepted":
                raise probe.ProbeFailure(f"control Prepare after abort was not accepted: {commit_accepted}")
            await demux.wait_event("VendorEvent", lambda data: data.get("vendorName") == "pulsar-scene-switch" and (data.get("eventData") or {}).get("event_type") == "PreviewReady" and (data.get("eventData") or {}).get("command_id") == commit_prepare_id)
            ready_state = await vendor_request(demux, f"state-before-commit-{phase}", "GetState", {})
            commit_take = {
                **take,
                "command_type": "Take",
                "command_id": commit_take_id,
                "intent_id": commit_intent,
                "expected_revisions": ready_state["revisions"],
                "expected_server_seq": ready_state["server_seq"],
                "prepared_command_id": commit_prepare_id,
            }
            committed = await vendor_request(demux, f"call-{commit_take_id}", "Take", commit_take)
            if committed.get("event_type") != "TakeAccepted":
                raise probe.ProbeFailure(f"control Take after abort was not accepted: {committed}")
            commit_match = runtime.wait_for(TRANSITION_COMMITTED_PATTERN, 15)
            if commit_match.group(1).lower() != transition.lower() and not (transition == "Stinger" and commit_match.group(1).lower() == "stinger"):
                raise probe.ProbeFailure(f"transition commit kind was not {transition}: {commit_match.group(0)}")
            if int(commit_match.group(8)) != 0:
                raise probe.ProbeFailure(f"control transition unexpectedly fell back to Cut: {commit_match.group(0)}")
            transition_end_frame_id = int(commit_match.group(6))
            transition_end_pts_ns = int(commit_match.group(7))
            probe.assert_success(await demux.request("StopRecord", "boundary-stop"), "StopRecord")
            stopped = await demux.wait_event("RecordStateChanged", lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED")
            output_path = stopped.get("eventData", {}).get("outputPath") or ""
            if not output_path:
                raise probe.ProbeFailure("abort recording did not expose outputPath")
            owned = probe.ensure_recording_output_owned(output_path, runtime.record_dir)
            ffmpeg = probe.find_ffmpeg(runtime.exe.parent)
            if ffmpeg is None:
                raise probe.ProbeSkip("ffmpeg is required for raw Program boundary decoding")
            labels = decode_raw_recording(ffmpeg, owned)
            abort_frame_index = int(abort_match.group(1)) - record_start_frames
            commit_frame_index = locate_commit_boundary(labels, before="red", after="green")
            assert_abort_pixels(labels, abort_frame_index, expected="red")
            assert_commit_pixels(labels, commit_frame_index, before="red", after="green")
            state_after = await vendor_request(demux, "boundary-state-after", "GetState", {})
            if state_after.get("role_map") == state.get("role_map"):
                raise probe.ProbeFailure(f"control commit did not swap role map: before={state['role_map']} after={state_after.get('role_map')}")
            return {"transition": transition, "phase": phase, "frames": len(labels), "abort_frame_id": int(abort_match.group(1)), "abort_pts_ns": int(abort_match.group(2)), "abort_recording_index": abort_frame_index, "commit_frame_id": transition_end_frame_id, "commit_pts_ns": transition_end_pts_ns, "commit_recording_index": commit_frame_index, "abort_role_map": state["role_map"], "commit_role_map": state_after["role_map"], "raw_labels": {"abort": "red", "commit_before": "red", "commit_after": "green"}}
    finally:
        if demux is not None:
            await demux.close()
        runtime.shutdown()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=pathlib.Path, required=True)
    parser.add_argument("--record-dir", type=pathlib.Path)
    parser.add_argument("--transition", choices=("Fade", "Stinger", "both"), default="both")
    parser.add_argument("--phase", choices=("queued", "final_queued", "both"), default="both")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    root = args.record_dir or pathlib.Path(tempfile.mkdtemp(prefix="pulsar-transition-boundary-"))
    transitions = ("Fade", "Stinger") if args.transition == "both" else (args.transition,)
    phases = ("queued", "final_queued") if args.phase == "both" else (args.phase,)
    results = []
    try:
        for transition in transitions:
            for phase in phases:
                results.append(asyncio.run(run_case(args.exe, root / f"{transition}-{phase}", transition, phase)))
        print(json.dumps({"format": "pulsar.transition-boundary.v1", "cases": results}, sort_keys=True))
        return 0
    except probe.ProbeSkip as exc:
        print(f"SKIP: {exc}")
        return 3
    except (probe.ProbeFailure, OSError, asyncio.TimeoutError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
