#!/usr/bin/env python3
"""Probe Fade/Stinger aborts at the raw Program frame boundary.

This is an opt-in runtime probe for #250.  It drives the versioned
``pulsar-scene-switch`` vendor, requests an abort while a transition is still
Queued and again after ``transition_final_queued``, then decodes the retained
recording as raw RGB frames.  The pixel check is deliberately independent of
the structured booleans: every settled frame around the observed abort boundary
must be the expected lane colour, while legitimate Fade blends and Stinger
compositions are accepted before the later settled seam.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from decimal import Decimal, InvalidOperation
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
POST_COMMIT_CAPTURE_SECONDS = 1.0
COLOURS = {"red": (220, 40, 40), "green": (40, 220, 40)}
LANE_DISTANCE_SQ = 45 * 45
FADE_DISTANCE_SQ = 80 * 80


class BoundaryDemux:
    """Single WebSocket reader/demultiplexer for responses and events."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.responses: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.buffered_responses: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.event_history: list[dict[str, Any]] = []
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
                    event = message.get("d", {})
                    self.events.append(event)
                    self.event_history.append(event)
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
    ``stinger`` palette.  A global lane-to-lane interpolation is a valid Fade;
    only simultaneous red and green base-lane samples are a torn Program seam.
    A Stinger may contain the base lane plus its opaque/alpha-composited blue
    sweep; only simultaneous red and green base-lane samples are a torn lane
    seam.
    """

    if len(frame) != FRAME_BYTES:
        raise probe.ProbeFailure(f"raw Program frame has {len(frame)} bytes, expected {FRAME_BYTES}")
    sample_step = max(3, ((len(frame) // 3) // 12000) * 3)
    samples = list(range(0, len(frame) - 2, sample_step))
    categories: list[str] = []
    black = 0
    for offset in samples:
        red, green, blue = frame[offset : offset + 3]
        if max(red, green, blue) <= 8:
            black += 1
            continue
        pixel = (red, green, blue)
        # First classify against the line segment joining the observed lane
        # colours.  A Fade frame is a coherent global blend, so (172,79,0)
        # and its neighbouring quantized values are valid composition samples,
        # not torn frames.  Endpoint projections remain lane colours.
        start = COLOURS["red"]
        end = COLOURS["green"]
        direction = tuple(float(b - a) for a, b in zip(start, end))
        relative = tuple(float(value - a) for value, a in zip(pixel, start))
        denominator = sum(component * component for component in direction)
        projection = sum(value * component for value, component in zip(relative, direction)) / denominator
        t = max(0.0, min(1.0, projection))
        projected = tuple(start[index] + t * direction[index] for index in range(3))
        segment_distance = sum((value - projected[index]) ** 2 for index, value in enumerate(pixel))
        if segment_distance <= FADE_DISTANCE_SQ:
            if t <= 0.08:
                categories.append("red")
            elif t >= 0.92:
                categories.append("green")
            else:
                categories.append("fade")
            continue
        nearest = min(
            COLOURS,
            key=lambda name: sum((channel - expected) ** 2 for channel, expected in zip(pixel, COLOURS[name])),
        )
        expected = COLOURS[nearest]
        distance = sum((channel - target) ** 2 for channel, target in zip(pixel, expected))
        if distance <= LANE_DISTANCE_SQ:
            categories.append(nearest)
            continue
        categories.append("stinger")

    total = len(samples)
    if total == 0 or black > total // 100:
        raise probe.ProbeFailure(f"raw Program frame is black/empty: black={black} categories={categories}")
    present = set(categories)
    lane_categories = present.intersection({"red", "green"})
    if len(lane_categories) > 1:
        raise probe.ProbeFailure(
            f"raw Program frame contains a torn spatial lane/composition intermediate/mixed: "
            f"black={black} categories={sorted(present)}"
        )
    if not categories:
        raise probe.ProbeFailure(f"raw Program frame is black/empty: black={black} categories={categories}")
    if "stinger" in present:
        # A WebM sweep may expose palette pixels which quantize onto the Fade
        # envelope or the settled base lane.  Stinger presence is the stronger
        # composition oracle as long as both base lanes are not present.
        return "stinger"
    if "fade" in present and lane_categories:
        return "fade"
    if present == {"red"} or present == {"green"} or present == {"fade"}:
        return next(iter(present))
    if present in ({"red", "stinger"}, {"green", "stinger"}):
        # Stinger overlays legitimately mix one settled base lane with a
        # distributed opaque/alpha-composited sweep. The lane+palette mix is
        # not torn unless both base lanes are sampled in the same frame.
        return "stinger"
    if present == {"stinger"}:
        # A real WebM Stinger can distribute many quantized colours, including
        # a contiguous or interleaved sweep.  Palette variation alone is not a
        # failure; the lane-category check above is the spatial-tear oracle.
        return "stinger"
    raise probe.ProbeFailure(f"raw Program frame is mixed: black={black} categories={sorted(present)}")


def assert_transition_timeline(labels: list[str], *, composition: str) -> None:
    """Require a monotone red -> composition -> green raw Program timeline."""

    if composition not in {"fade", "stinger"}:
        raise probe.ProbeFailure(f"unsupported transition composition {composition!r}")
    phases = {"red": 0, composition: 1, "green": 2}
    last_phase = 0
    for index, label in enumerate(labels):
        if label not in phases:
            raise probe.ProbeFailure(f"raw Program timeline has an unknown label at {index}: {label}")
        phase = phases[label]
        if phase < last_phase:
            raise probe.ProbeFailure(
                f"raw Program timeline is non-monotone at {index}: {labels[max(0, index - 3): index + 3]}"
            )
        last_phase = phase


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


def read_encoded_video_pts(ffprobe: str, recording: pathlib.Path) -> list[int]:
    """Read the encoded video PTS sequence for callback-to-recording proof."""

    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "frame=best_effort_timestamp_time,pts_time",
        "-of",
        "json",
        str(recording),
    ]
    try:
        raw = subprocess.check_output(command, stderr=subprocess.STDOUT, timeout=60)
        frames = json.loads(raw).get("frames", [])
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise probe.ProbeFailure(f"encoded video PTS read failed: {exc}") from exc
    result: list[int] = []
    for frame in frames:
        timestamp = frame.get("best_effort_timestamp_time") or frame.get("pts_time")
        if timestamp in (None, "N/A"):
            continue
        try:
            value = Decimal(str(timestamp))
        except (InvalidOperation, ValueError) as exc:
            raise probe.ProbeFailure(f"encoded video PTS is invalid: {timestamp!r}") from exc
        if not value.is_finite():
            raise probe.ProbeFailure(f"encoded video PTS is non-finite: {timestamp!r}")
        result.append(int((value * Decimal(1_000_000_000)).to_integral_value()))
    if len(result) < 3 or any(later <= earlier for earlier, later in zip(result, result[1:])):
        raise probe.ProbeFailure(f"encoded video PTS sequence is missing or non-monotone: count={len(result)}")
    return result


def assert_callback_pts_correlated(encoded_pts_ns: list[int], start_pts_ns: int, end_pts_ns: int) -> dict[str, int]:
    """Correlate callback start/end PTS to an encoded PTS span, never frame IDs."""

    observed_delta = end_pts_ns - start_pts_ns
    if observed_delta <= 0:
        raise probe.ProbeFailure(f"callback PTS interval is invalid: start={start_pts_ns} end={end_pts_ns}")
    encoded_deltas = [
        (end - start, start_index, end_index)
        for start_index, start in enumerate(encoded_pts_ns[:-1])
        for end_index, end in enumerate(encoded_pts_ns[start_index + 1 :], start_index + 1)
    ]
    nearest, encoded_start_index, encoded_end_index = min(
        encoded_deltas, key=lambda item: abs(item[0] - observed_delta)
    )
    frame_intervals = [later - earlier for earlier, later in zip(encoded_pts_ns, encoded_pts_ns[1:])]
    frame_interval = max(frame_intervals)
    error = abs(nearest - observed_delta)
    if error > frame_interval:
        raise probe.ProbeFailure(
            f"callback PTS interval has no encoded PTS correlation: observed={observed_delta} "
            f"nearest={nearest} tolerance={frame_interval}"
        )
    return {
        "callback_delta_ns": observed_delta,
        "encoded_delta_ns": nearest,
        "encoded_start_index": encoded_start_index,
        "encoded_end_index": encoded_end_index,
        "error_ns": error,
    }


def assert_terminal_event_contract(
    events: list[dict[str, Any]],
    *,
    aborted_command_id: str,
    committed_command_id: str,
    expected_revisions: dict[str, int],
    winner: str = "aborted",
) -> dict[str, int]:
    """Prove one terminal winner and its route-map revision delta."""

    aborted = [
        event
        for event in events
        if event.get("event_type") == "TakeAborted"
        and event.get("command_id") == aborted_command_id
    ]
    commits = [event for event in events if event.get("event_type") == "TakeCommitted"]
    control = [
        event
        for event in commits
        if committed_command_id != aborted_command_id and event.get("command_id") == committed_command_id
    ]
    original_commits = [event for event in commits if event.get("command_id") == aborted_command_id]
    if winner == "aborted":
        if len(aborted) != 1 or original_commits or len(control) != 1:
            raise probe.ProbeFailure(
                f"route-map terminal events are not unique: aborted={aborted} committed={commits}"
            )
        terminal = control[0]
    elif winner == "committed":
        if aborted or len(original_commits) != 1 or control:
            raise probe.ProbeFailure(
                f"commit winner has conflicting terminal events: aborted={aborted} committed={commits}"
            )
        terminal = original_commits[0]
    else:
        raise probe.ProbeFailure(f"unsupported terminal winner {winner!r}")
    previous = terminal.get("previous_revisions") or {}
    current = terminal.get("revisions") or {}
    for key in ("program", "preview", "role_map"):
        if not isinstance(previous.get(key), int) or not isinstance(current.get(key), int):
            raise probe.ProbeFailure(f"Take event has invalid {key} revisions: {terminal}")
        expected_delta = 1 if key in {"program", "role_map"} else 0
        if previous[key] != expected_revisions[key] or current[key] != previous[key] + expected_delta:
            raise probe.ProbeFailure(f"Take revision delta is invalid: event={terminal}")
    return {
        "aborted": len(aborted),
        "committed": len(commits),
        "control_committed": len(control),
        "winner_committed": len(original_commits),
    }


def locate_abort_settled_red(labels: list[str], commit_index: int) -> int:
    """Locate a settled red run without comparing callback frame IDs to video indices."""

    composition = {"fade", "stinger"}
    pre_commit = labels[:commit_index]
    first_composition = next((index for index, label in enumerate(pre_commit) if label in composition), len(pre_commit))
    candidates = [
        index
        for index in range(0, max(0, first_composition - 2))
        if all(label == "red" for label in labels[index : index + 3])
    ]
    if not candidates:
        raise probe.ProbeFailure("raw Program recording has no settled red post-abort run")
    return candidates[-1]


def assert_abort_pixels(labels: list[str], boundary_index: int, *, expected: str) -> None:
    """Require complete lane frames around the observed boundary."""

    if not labels:
        raise probe.ProbeFailure("raw Program recording contains no decoded frames")
    if boundary_index < 0 or boundary_index + 3 > len(labels):
        raise probe.ProbeFailure(
            f"abort boundary index {boundary_index} is not surrounded by raw frames (count={len(labels)})"
        )
    window = labels[max(0, boundary_index - 2) : min(len(labels), boundary_index + 3)]
    if any(label not in COLOURS and label not in {"fade", "stinger"} for label in window):
        raise probe.ProbeFailure(f"raw Program boundary contains an unknown frame label: {window}")
    if any(label == "green" for label in window) or any(label != expected for label in labels[boundary_index : boundary_index + 3]):
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
    if not candidates:
        raise probe.ProbeFailure(
            f"raw Program commit seam was not observed: candidates={candidates}"
        )
    # An aborted composition can expose an earlier settled green segment in
    # the same recording.  The control/terminal winner is the last settled
    # green seam; use the raw tail as the encoded settlement bound instead of
    # pretending visual seam count is route-map commit count.
    boundary_index = candidates[-1]
    if not any(label == before for label in labels[:boundary_index]):
        raise probe.ProbeFailure(f"raw Program commit seam has no settled {before} prelude")
    if any(label == before for label in labels[boundary_index:]):
        raise probe.ProbeFailure(f"raw Program commit seam is followed by stale {before} frames")
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


def vendor_event_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    """Unwrap the v5 VendorEvent outer eventData and inner vendor eventData."""

    outer_data = event.get("eventData") or {}
    if outer_data.get("vendorName") != "pulsar-scene-switch":
        return None
    return outer_data.get("eventData") or {}


def is_vendor_event(event: dict[str, Any], event_type: str, command_id: str) -> bool:
    payload = vendor_event_payload(event)
    return payload is not None and payload.get("event_type") == event_type and payload.get("command_id") == command_id


def resolve_abort_winner(abort_result: dict[str, Any]) -> str:
    """Accept only Abort success or the typed commit-race rejection."""

    event_type = abort_result.get("event_type")
    if event_type == "TakeAborted":
        return "TakeAborted"
    if event_type == "CommandRejected" and abort_result.get("error_code") == "TAKE_NOT_PENDING":
        return "TakeCommitted"
    raise probe.ProbeFailure(f"Abort did not produce an allowed terminal winner: {abort_result}")


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
            await demux.wait_event("VendorEvent", lambda data: is_vendor_event(data, "PreviewReady", prepare_id))
            state = await vendor_request(demux, f"state-{phase}", "GetState", {})
            take = {
                "contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
                "command_type": "Take", "command_id": take_id, "intent_id": intent,
                "runtime_instance_id": runtime.runtime_id, "expected_revisions": state["revisions"],
                "expected_server_seq": state["server_seq"], "prepared_command_id": prepare_id, "timeout_ms": 5000,
            }
            take_result = await vendor_request(demux, f"call-{take_id}", "Take", take)
            if take_result.get("event_type") != "TakeAccepted":
                raise probe.ProbeFailure(f"Take was not accepted before abort barrier: {take_result}")
            if phase == "queued":
                # TakeAccepted is the admission barrier.  A rejection that
                # arrives before it is a gateway scheduling race, not proof
                # that the Stinger/Fade transition is cancellable.  The
                # accepted response leaves the initial frame-boundary swap
                # pending for an immediate Abort.
                abort_payload = {
                    "contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
                    "command_type": "Abort", "command_id": f"boundary-abort-{phase}", "intent_id": intent,
                    "runtime_instance_id": runtime.runtime_id, "expected_revisions": state["revisions"],
                    "take_command_id": take_id, "reason": "operator",
                }
                abort_result = await vendor_request(demux, f"call-abort-{phase}", "Abort", abort_payload)
            else:
                runtime.wait_for(FINAL_QUEUED_PATTERN, 15)
                abort_payload = {
                    "contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
                    "command_type": "Abort", "command_id": f"boundary-abort-{phase}", "intent_id": intent,
                    "runtime_instance_id": runtime.runtime_id, "expected_revisions": state["revisions"],
                    "take_command_id": take_id, "reason": "operator",
                }
                abort_result = await vendor_request(demux, f"call-abort-{phase}", "Abort", abort_payload)
            # The frame-boundary commit may win after TakeAccepted. The Abort
            # response is then a typed rejection; terminal event history and
            # runtime trace remain the authoritative winner evidence.
            winner = resolve_abort_winner(abort_result)
            abort_match = None
            commit_prepare_id = None
            commit_take_id = take_id if winner == "TakeCommitted" else None
            ready_state = state
            if winner == "TakeAborted":
                abort_match = runtime.wait_for(ABORT_PATTERN, 15)
                fields = [int(abort_match.group(index)) for index in range(4, 8)]
                if fields != [1, 1, 1, 1]:
                    raise probe.ProbeFailure(f"abort postconditions were not observed: {abort_match.group(0)}")

                # Keep both the observed abort boundary and the later control
                # commit in the recording. The longer dwell is outside callbacks;
                # it gives the encoder enough settled frames for PTS correlation.
                await asyncio.sleep(0.5)
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
                await demux.wait_event("VendorEvent", lambda data: is_vendor_event(data, "PreviewReady", commit_prepare_id))
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
            else:
                # Abort can lose the race after TakeAccepted.  That is still a
                # valid deterministic terminal winner; do not manufacture a
                # second control Take or an abort assertion in this branch.
                commit_match = runtime.wait_for(TRANSITION_COMMITTED_PATTERN, 15)
                if commit_match.group(1).lower() != transition.lower() and not (transition == "Stinger" and commit_match.group(1).lower() == "stinger"):
                    raise probe.ProbeFailure(f"transition commit kind was not {transition}: {commit_match.group(0)}")
                if int(commit_match.group(8)) != 0:
                    raise probe.ProbeFailure(f"accepted Take unexpectedly fell back to Cut: {commit_match.group(0)}")
            if winner == "TakeAborted":
                commit_match = runtime.wait_for(TRANSITION_COMMITTED_PATTERN, 15)
            if commit_match.group(1).lower() != transition.lower() and not (transition == "Stinger" and commit_match.group(1).lower() == "stinger"):
                raise probe.ProbeFailure(f"transition commit kind was not {transition}: {commit_match.group(0)}")
            if int(commit_match.group(8)) != 0:
                raise probe.ProbeFailure(f"control transition unexpectedly fell back to Cut: {commit_match.group(0)}")
            transition_end_pts_ns = int(commit_match.group(7))
            await asyncio.sleep(POST_COMMIT_CAPTURE_SECONDS)
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
            ffprobe = probe.find_ffprobe()
            if ffprobe is None:
                raise probe.ProbeSkip("ffprobe is required for encoded Program PTS correlation")
            encoded_pts_ns = read_encoded_video_pts(ffprobe, owned)
            pts_correlation = assert_callback_pts_correlated(
                encoded_pts_ns,
                int(commit_match.group(5)),
                transition_end_pts_ns,
            )
            commit_frame_index = locate_commit_boundary(labels, before="red", after="green")
            abort_frame_index = None
            if winner == "TakeAborted":
                abort_frame_index = locate_abort_settled_red(labels, commit_frame_index)
                assert_abort_pixels(labels, abort_frame_index, expected="red")
            assert_commit_pixels(labels, commit_frame_index, before="red", after="green")
            state_after = await vendor_request(demux, "boundary-state-after", "GetState", {})
            if state_after.get("role_map") == state.get("role_map"):
                raise probe.ProbeFailure(f"winning commit did not swap role map: before={state['role_map']} after={state_after.get('role_map')}")
            vendor_events = []
            for event in demux.event_history:
                if event.get("eventType") != "VendorEvent":
                    continue
                outer_data = event.get("eventData") or {}
                if outer_data.get("vendorName") == "pulsar-scene-switch":
                    vendor_events.append(outer_data.get("eventData") or {})
            terminal_events = assert_terminal_event_contract(
                vendor_events,
                aborted_command_id=take_id,
                committed_command_id=commit_take_id or take_id,
                expected_revisions=ready_state["revisions"],
                winner="aborted" if winner == "TakeAborted" else "committed",
            )
            return {"transition": transition, "phase": phase, "terminal_winner": "abort" if winner == "TakeAborted" else "commit", "frames": len(labels), "abort_pts_ns": int(abort_match.group(2)) if abort_match else None, "abort_recording_index": abort_frame_index, "commit_pts_ns": transition_end_pts_ns, "commit_recording_index": commit_frame_index, "callback_pts_correlation": pts_correlation, "abort_role_map": state["role_map"] if winner == "TakeAborted" else None, "commit_role_map": state_after["role_map"], "terminal_events": terminal_events, "raw_labels": {"abort": "red" if winner == "TakeAborted" else None, "commit_before": "red", "commit_after": "green"}}
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
