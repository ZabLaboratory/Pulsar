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
import importlib.util
import json
import os
import pathlib
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
FINAL_QUEUED_RE = probe.DUAL_LANE_LOG_PREFIX + r"\s*transition_final_queued kind=(\S+)"
WIDTH = probe.CANVAS_W
HEIGHT = probe.CANVAS_H
FRAME_BYTES = WIDTH * HEIGHT * 3
COLOURS = {"red": (220, 40, 40), "green": (40, 220, 40)}


def classify_raw_frame(frame: bytes) -> str:
    """Classify one decoded Program frame, rejecting black/mixed pixels."""

    if len(frame) != FRAME_BYTES:
        raise probe.ProbeFailure(f"raw Program frame has {len(frame)} bytes, expected {FRAME_BYTES}")
    samples = range(0, len(frame), max(3, len(frame) // 12000 // 3 * 3))
    votes = {name: 0 for name in COLOURS}
    black = 0
    for offset in samples:
        red, green, blue = frame[offset : offset + 3]
        if max(red, green, blue) <= 8:
            black += 1
            continue
        nearest = min(
            COLOURS,
            key=lambda name: sum((channel - expected) ** 2 for channel, expected in zip((red, green, blue), COLOURS[name])),
        )
        expected = COLOURS[nearest]
        distance = sum((channel - target) ** 2 for channel, target in zip((red, green, blue), expected))
        if distance > 18_000:
            raise probe.ProbeFailure(
                f"raw Program frame contains an intermediate/mixed colour at byte {offset}: {(red, green, blue)}"
            )
        votes[nearest] += 1
    total = sum(votes.values()) + black
    if total == 0 or black > total // 100:
        raise probe.ProbeFailure(f"raw Program frame is black/empty: black={black} votes={votes}")
    label, count = max(votes.items(), key=lambda item: item[1])
    if count * 100 < max(1, total - black) * 95:
        raise probe.ProbeFailure(f"raw Program frame is mixed: black={black} votes={votes}")
    return label


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


def assert_commit_pixels(labels: list[str], boundary_index: int, *, before: str, after: str) -> None:
    """Require a complete old/new colour pair at a committed seam."""

    if boundary_index < 2 or boundary_index >= len(labels) - 2:
        raise probe.ProbeFailure(
            f"commit boundary index {boundary_index} is not surrounded by raw frames (count={len(labels)})"
        )
    before_window = labels[boundary_index - 2 : boundary_index]
    after_window = labels[boundary_index : boundary_index + 3]
    if any(label != before for label in before_window) or any(label != after for label in after_window):
        raise probe.ProbeFailure(
            f"raw Program commit seam was stale/mixed: before={before_window}, after={after_window}"
        )


async def vendor_request(inbox: Any, ws: Any, request_id: str, request_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = await probe.request(
        inbox,
        ws,
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
    runtime = probe.PulsarProcess(exe, "x264", record_dir, runtime_id=os.environ["PULSAR_RUNTIME_INSTANCE_ID"])
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
                ("SetCurrentSceneTransition", "boundary-transition", {"transitionName": transition}),
                ("SetCurrentSceneTransitionDuration", "boundary-duration", {"transitionDuration": 200}),
            ):
                probe.assert_success(await probe.request(inbox, ws, request_type, request_id, data), request_type)
            probe.assert_success(await probe.request(inbox, ws, "StartRecord", "boundary-record"), "StartRecord")
            await probe.wait_event(inbox, ws, "RecordStateChanged", lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED")
            stats = await probe.request(inbox, ws, "GetStats", "boundary-record-stats")
            probe.assert_success(stats, "GetStats(record start)")
            record_start_frames = int((stats.get("responseData") or {}).get("outputTotalFrames", 0) or 0)

            state = await vendor_request(inbox, ws, "boundary-state", "GetState", {})
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
            accepted = await vendor_request(inbox, ws, f"call-{prepare_id}", "Prepare", prepare)
            if accepted.get("event_type") != "PrepareAccepted":
                raise probe.ProbeFailure(f"Prepare was not accepted: {accepted}")
            await probe.wait_event(inbox, ws, "VendorEvent", lambda data: data.get("vendorName") == "pulsar-scene-switch" and (data.get("eventData") or {}).get("event_type") == "PreviewReady" and (data.get("eventData") or {}).get("command_id") == prepare_id)
            state = await vendor_request(inbox, ws, f"state-{phase}", "GetState", {})
            take = {
                "contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
                "command_type": "Take", "command_id": take_id, "intent_id": intent,
                "runtime_instance_id": runtime.runtime_id, "expected_revisions": state["revisions"],
                "expected_server_seq": state["server_seq"], "prepared_command_id": prepare_id, "timeout_ms": 5000,
            }
            take_task = asyncio.create_task(vendor_request(inbox, ws, f"call-{take_id}", "Take", take))
            if phase == "queued":
                abort_payload = {
                    "contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
                    "command_type": "Abort", "command_id": f"boundary-abort-{phase}", "intent_id": intent,
                    "runtime_instance_id": runtime.runtime_id, "expected_revisions": state["revisions"],
                    "take_command_id": take_id, "reason": "operator",
                }
                await asyncio.sleep(0)
                abort_task = asyncio.create_task(vendor_request(inbox, ws, f"call-abort-{phase}", "Abort", abort_payload))
                take_result, abort_result = await asyncio.gather(take_task, abort_task)
            else:
                take_result = await take_task
                if take_result.get("event_type") != "TakeAccepted":
                    raise probe.ProbeFailure(f"Take was not accepted: {take_result}")
                runtime.wait_for(FINAL_QUEUED_RE, 15)
                abort_payload = {
                    "contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
                    "command_type": "Abort", "command_id": f"boundary-abort-{phase}", "intent_id": intent,
                    "runtime_instance_id": runtime.runtime_id, "expected_revisions": state["revisions"],
                    "take_command_id": take_id, "reason": "operator",
                }
                abort_result = await vendor_request(inbox, ws, f"call-abort-{phase}", "Abort", abort_payload)
            if abort_result.get("event_type") != "TakeAborted":
                raise probe.ProbeFailure(f"Abort did not win {phase}: {abort_result}")
            abort_match = runtime.wait_for(__import__("re").compile(ABORT_RE), 15)
            fields = [int(abort_match.group(index)) for index in range(4, 8)]
            if fields != [1, 1, 1, 1]:
                raise probe.ProbeFailure(f"abort postconditions were not observed: {abort_match.group(0)}")

            # Keep the observed abort boundary in the recording before driving
            # a control commit.  The dwell is outside the callback and gives
            # the recorder complete post-abort frames to decode.
            await asyncio.sleep(0.25)
            after_abort = await vendor_request(inbox, ws, f"state-after-abort-{phase}", "GetState", {})
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
            commit_accepted = await vendor_request(inbox, ws, f"call-{commit_prepare_id}", "Prepare", commit_prepare)
            if commit_accepted.get("event_type") != "PrepareAccepted":
                raise probe.ProbeFailure(f"control Prepare after abort was not accepted: {commit_accepted}")
            await probe.wait_event(inbox, ws, "VendorEvent", lambda data: data.get("vendorName") == "pulsar-scene-switch" and (data.get("eventData") or {}).get("event_type") == "PreviewReady" and (data.get("eventData") or {}).get("command_id") == commit_prepare_id)
            ready_state = await vendor_request(inbox, ws, f"state-before-commit-{phase}", "GetState", {})
            commit_take = {
                **take,
                "command_type": "Take",
                "command_id": commit_take_id,
                "intent_id": commit_intent,
                "expected_revisions": ready_state["revisions"],
                "expected_server_seq": ready_state["server_seq"],
                "prepared_command_id": commit_prepare_id,
            }
            committed = await vendor_request(inbox, ws, f"call-{commit_take_id}", "Take", commit_take)
            if committed.get("event_type") != "TakeAccepted":
                raise probe.ProbeFailure(f"control Take after abort was not accepted: {committed}")
            commit_match = runtime.wait_for(probe.COMMIT_RE, 15)
            if int(commit_match.group(1)) < 1:
                raise probe.ProbeFailure(f"control Take commit count was invalid: {commit_match.group(0)}")
            probe.assert_success(await probe.request(inbox, ws, "StopRecord", "boundary-stop"), "StopRecord")
            stopped = await probe.wait_event(inbox, ws, "RecordStateChanged", lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED")
            output_path = stopped.get("eventData", {}).get("outputPath") or ""
            if not output_path:
                raise probe.ProbeFailure("abort recording did not expose outputPath")
            owned = probe.ensure_recording_output_owned(output_path, runtime.record_dir)
            ffmpeg = probe.find_ffmpeg(runtime.exe.parent)
            if ffmpeg is None:
                raise probe.ProbeSkip("ffmpeg is required for raw Program boundary decoding")
            labels = decode_raw_recording(ffmpeg, owned)
            abort_frame_index = int(abort_match.group(1)) - record_start_frames
            commit_frame_index = int(commit_match.group(2)) - record_start_frames
            assert_abort_pixels(labels, abort_frame_index, expected="red")
            assert_commit_pixels(labels, commit_frame_index, before="red", after="green")
            state_after = await vendor_request(inbox, ws, "boundary-state-after", "GetState", {})
            if state_after.get("role_map") == state.get("role_map"):
                raise probe.ProbeFailure(f"control commit did not swap role map: before={state['role_map']} after={state_after.get('role_map')}")
            return {"transition": transition, "phase": phase, "frames": len(labels), "abort_frame_id": int(abort_match.group(1)), "abort_pts_ns": int(abort_match.group(2)), "abort_recording_index": abort_frame_index, "commit_frame_id": int(commit_match.group(2)), "commit_recording_index": commit_frame_index, "abort_role_map": state["role_map"], "commit_role_map": state_after["role_map"], "raw_labels": {"abort": "red", "commit_before": "red", "commit_after": "green"}}
    finally:
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
