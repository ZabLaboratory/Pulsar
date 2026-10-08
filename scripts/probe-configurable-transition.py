#!/usr/bin/env python3
"""Isolated synthetic media probe. No Prism, capture device, RTMP or default asset.

Requires --exe (a dedicated build), --output (evidence directory), ffmpeg,
numpy and websockets. Generates its own small alpha WebM inside --output and
records only colour scenes and that test tone. Reuses the existing process
lifecycle helper, including the inherited Windows shutdown event.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import websockets

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("transition_probe_process", ROOT / "scripts/probe-dual-lane.py")
process = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = process
spec.loader.exec_module(process)


def fixture(path: Path, color=(230, 180, 35)):
    frames = np.zeros((45, 180, 320, 4), dtype=np.uint8)
    frames[:, :, :, :3] = color
    for i in range(45):
        t = i / 30
        alpha = min(1, max(0, (t - .15) / .3), max(0, (1.35 - t) / .3))
        frames[i, :, :, 3] = round(255 * alpha)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", "320x180", "-r", "30", "-i", "pipe:0", "-f", "lavfi", "-i",
        "sine=frequency=523:duration=1.5:sample_rate=48000", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
        "-b:v", "0", "-crf", "15", "-auto-alt-ref", "0", "-c:a", "libopus", str(path)],
        input=frames.tobytes(), check=True, timeout=30)


class Wire:
    def __init__(self, ws):
        self.ws, self.counter, self.events = ws, 0, []

    async def call(self, kind, data=None, *, error=False):
        self.counter += 1
        rid = str(self.counter)
        await self.ws.send(json.dumps({"op": 6, "d": {"requestType": kind, "requestId": rid, "requestData": data or {}}}))
        while True:
            frame = json.loads(await asyncio.wait_for(self.ws.recv(), 15))
            if frame["op"] == 5:
                self.events.append(frame["d"])
            elif frame["op"] == 7 and frame["d"]["requestId"] == rid:
                response = frame["d"]
                if error:
                    assert not response["requestStatus"]["result"], response
                    return response["requestStatus"]
                assert response["requestStatus"]["result"], response
                return response.get("responseData", {})

    async def vendor(self, operation, data=None, vendor="pulsar-transitions"):
        return (await self.call("CallVendorRequest", {"vendorName": vendor, "requestType": operation,
            "requestData": data or {}})).get("responseData", {})

    async def poll(self, operation, data, predicate, *, vendor="pulsar-transitions", timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = await self.vendor(operation, data, vendor)
            if predicate(value):
                return value
            await asyncio.sleep(.04)
        raise AssertionError(f"Timed out {operation}: {value}")


def decode_recording(path: str):
    rgb = subprocess.check_output(["ffmpeg", "-v", "error", "-i", path, "-vf", "scale=32:18", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], timeout=30)
    frames = np.frombuffer(rgb, np.uint8).reshape((-1, 18, 32, 3))
    centers = frames[:, 9, 16, :].astype(float)
    samples = subprocess.check_output(["ffmpeg", "-v", "error", "-i", path, "-vn", "-f", "f32le", "-ac", "1", "pipe:1"], timeout=30)
    audio = np.frombuffer(samples, np.float32)
    return centers, float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0


async def exercise(args, proc, report):
    proc.spawn()
    ready = await asyncio.to_thread(proc.wait_for, process.READY_RE, 45)
    uri, password = ready.groups()
    async with websockets.connect(uri, max_size=16 * 1024 * 1024) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion": 1, "eventSubscriptions": 0x7ff}
        if "authentication" in hello:
            auth = hello["authentication"]
            secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        await ws.send(json.dumps({"op": 1, "d": identify}))
        assert json.loads(await ws.recv())["op"] == 2
        wire = Wire(ws)
        state = await wire.vendor("GetState")
        report["default"] = state
        assert state["operational"] and not state["configured"] and not state["ready"], state
        runtime = state["runtime_instance_id"]
        guard = {"runtime_instance_id": runtime}
        # The probe never captures the desktop sound; disable the boot input
        # before starting any synthetic recording.
        inputs = (await wire.call("GetInputList"))["inputs"]
        for item in inputs:
            if item["inputKind"].startswith("wasapi_"):
                await wire.call("SetInputMute", {"inputName": item["inputName"], "inputMuted": True})
        for name, color in [("red", (220, 40, 40)), ("green", (40, 220, 40)), ("blue", (40, 40, 220))]:
            await wire.call("CreateScene", {"sceneName": name})
            r, g, b = color
            await wire.call("CreateInput", {"sceneName": name, "inputName": name + "-colour", "inputKind": "color_source_v3",
                "inputSettings": {"color": r | (g << 8) | (b << 16) | 0xff000000, "width": 1920, "height": 1080}, "sceneItemEnabled": True})
        await wire.call("SetCurrentProgramScene", {"sceneName": "red"})
        await wire.call("SetStudioModeEnabled", {"studioModeEnabled": True})
        await wire.call("SetCurrentPreviewScene", {"sceneName": "blue"})
        config = {"path": str(args.output / "fixture.webm"), "cut_point_ms": 700, "volume": .5, "muted": False}
        assert (await wire.vendor("Configure", {"runtime_instance_id": "wrong-runtime", "config": config}))["error"] == "RUNTIME_MISMATCH"
        for bad in [{**config, "path": "https://example.invalid/test.webm"}, {**config, "cut_point_ms": -1}, {**config, "volume": 2}]:
            assert "error" in await wire.vendor("Configure", {**guard, "config": bad})
        configured = await wire.vendor("Configure", {**guard, "config": config})
        assert "error" not in configured, configured
        state = await wire.poll("GetState", {}, lambda s: s.get("ready"))
        report["ready"] = state
        print("Decoder ready", flush=True)

        async def switch(command, lane, old, new):
            return await wire.vendor("SwitchLane", {**guard, "command_id": command, "lane_id": lane,
                "expected_scene_name": old, "scene_name": new})

        async def result(command):
            return await wire.poll("GetResult", {**guard, "command_id": command}, lambda s: s.get("status") in ["completed", "aborted", "failed"])

        await wire.call("StartRecord")
        await asyncio.sleep(.35)
        initial_v1 = await wire.vendor("GetState", vendor="pulsar-scene-switch")
        accepted = await switch("same-program", "A", "red", "green")
        assert accepted["status"] == "accepted", accepted
        busy = await wire.vendor("GetState")
        assert busy["busy"]
        assert (await wire.vendor("Abort", {**guard, "command_id": "unrelated"}))["error"] == "COMMAND_NOT_PENDING"
        for operation in ["Configure", "Clear", "SwitchLane"]:
            frozen = await wire.call("CallVendorRequest", {"vendorName": "pulsar-transitions", "requestType": operation,
                "requestData": {**guard, "config": config}}, error=True)
            assert "PREVIEW_FROZEN" in frozen.get("comment", ""), frozen
        completed = await result("same-program")
        assert completed["status"] == "completed" and completed["role_map"] == state["role_map"], completed
        assert completed["frame_id"] > 0 and completed["pts_ns"] > 0
        after_v1 = await wire.vendor("GetState", vendor="pulsar-scene-switch")
        assert after_v1["revisions"]["program"] == initial_v1["revisions"]["program"] + 1
        assert after_v1["revisions"]["role_map"] == initial_v1["revisions"]["role_map"]
        assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] == "green"
        assert (await switch("same-program", "A", "red", "green"))["status"] == "completed"
        assert (await switch("same-program", "A", "red", "blue"))["error"] == "IDEMPOTENCY_CONFLICT"
        await asyncio.sleep(.35)
        recording = (await wire.call("StopRecord"))["outputPath"]
        centers, rms = decode_recording(recording)
        assert np.linalg.norm(centers[0] - (220, 40, 40)) < 30, centers[0]
        assert np.linalg.norm(centers[-1] - (40, 220, 40)) < 30, centers[-1]
        assert np.any(np.linalg.norm(centers - (230, 180, 35), axis=1) < 30), centers.tolist()
        # Partial-alpha frames must reveal the outgoing and incoming colours;
        # an opaque or absent overlay cannot satisfy both blend windows.
        assert np.count_nonzero((centers[:, 0] > 180) & (centers[:, 1] > 65) & (centers[:, 1] < 150)) >= 2
        assert np.count_nonzero((centers[:, 0] > 65) & (centers[:, 0] < 180) & (centers[:, 1] > 150)) >= 2
        assert np.min(np.max(centers, axis=1)) > 90, "black frame"
        assert rms > .002, ("missing Program transition audio", rms)
        report["same_program"] = {"result": completed, "recording": recording, "frames": len(centers), "audio_rms": rms,
            "centers": centers.tolist()}
        print("Same-lane Program and alpha/audio passed", flush=True)

        # Preview animation must neither replace Program nor play its stinger
        # into Program's common audio route.
        await wire.call("StartRecord")
        assert (await switch("same-preview", "B", "blue", "red"))["status"] == "accepted"
        preview = await result("same-preview")
        await asyncio.sleep(.25)
        preview_recording = (await wire.call("StopRecord"))["outputPath"]
        centers, preview_rms = decode_recording(preview_recording)
        assert np.max(np.linalg.norm(centers - (40, 220, 40), axis=1)) < 30
        assert preview_rms < .0001, ("Preview sound leaked", preview_rms)
        assert preview["role_map"] == state["role_map"]
        report["same_preview"] = {"result": preview, "recording": preview_recording, "audio_rms": preview_rms}
        print("Preview isolation passed", flush=True)

        v1 = await wire.vendor("GetState", vendor="pulsar-scene-switch")
        base = {"contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
            **guard, "intent_id": "test-take", "timeout_ms": 8000}
        prep = {**base, "command_type": "Prepare", "command_id": "test-prepare", "expected_revisions": v1["revisions"],
            "expected_server_seq": v1["server_seq"], "target": {"lane_id": "B", "scene_id": "red"}}
        assert (await wire.vendor("Prepare", prep, "pulsar-scene-switch"))["event_type"] == "PrepareAccepted"
        ready_v1 = await wire.poll("GetState", {}, lambda s: s.get("state") == "preview_ready", vendor="pulsar-scene-switch")
        take = {**base, "command_type": "Take", "command_id": "test-take-command", "expected_revisions": ready_v1["revisions"],
            "expected_server_seq": ready_v1["server_seq"], "prepared_command_id": "test-prepare"}
        await wire.call("StartRecord")
        await asyncio.sleep(.35)
        taken = await wire.vendor("Take", take, "pulsar-scene-switch")
        assert taken["event_type"] == "TakeAccepted", taken
        v1_done = await wire.poll("GetState", {}, lambda s: s.get("role_map", {}).get("on_air") == "B", vendor="pulsar-scene-switch")
        assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] == "red"
        await asyncio.sleep(.35)
        take_recording = (await wire.call("StopRecord"))["outputPath"]
        centers, take_rms = decode_recording(take_recording)
        assert np.linalg.norm(centers[0] - (40, 220, 40)) < 30
        assert np.linalg.norm(centers[-1] - (220, 40, 40)) < 30
        assert np.any(np.linalg.norm(centers - (230, 180, 35), axis=1) < 30)
        assert np.min(np.max(centers, axis=1)) > 90
        assert take_rms > .002, "Program audio was not restored after Preview"
        report["take"] = {"state": v1_done, "recording": take_recording, "audio_rms": take_rms}
        print("Configured Take passed", flush=True)

        # Reconfiguration replaces the decoder only while idle. An abort
        # restores the original composition and has a terminal frame/PTS.
        replacement = {**config, "path": str(args.output / "second-éffect.webm"), "cut_point_ms": 900, "volume": .2, "muted": True}
        assert "error" not in await wire.vendor("Configure", {**guard, "config": replacement})
        await wire.poll("GetState", {}, lambda s: s.get("ready"))
        assert (await switch("abort-me", "B", "red", "blue"))["status"] == "accepted"
        await asyncio.sleep(.2)
        assert (await wire.vendor("Abort", {**guard, "command_id": "abort-me"}))["status"] == "aborting"
        aborted = await result("abort-me")
        assert aborted["status"] == "aborted"
        assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] == "red"
        report["abort"] = aborted
        assert (await switch("stale", "B", "blue", "green"))["error"] == "SCENE_MISMATCH"
        assert (await switch("alias", "B", "red", "green"))["error"] == "LANE_ALIAS"

        # A different filename/decoder works after an early abort, with the
        # host-selected cut point and mute. No implementation-default artwork.
        await wire.call("StartRecord")
        await asyncio.sleep(.35)
        assert (await switch("after-abort", "B", "red", "blue"))["status"] == "accepted"
        assert (await result("after-abort"))["status"] == "completed"
        await asyncio.sleep(.35)
        muted_recording = (await wire.call("StopRecord"))["outputPath"]
        centers, muted_rms = decode_recording(muted_recording)
        assert np.linalg.norm(centers[0] - (220, 40, 40)) < 30
        assert np.linalg.norm(centers[-1] - (40, 40, 220)) < 30
        assert np.any(np.linalg.norm(centers - (155, 100, 235), axis=1) < 30)
        assert np.min(np.max(centers, axis=1)) > 90
        assert muted_rms < .0001
        report["replacement_muted"] = {"state": await wire.vendor("GetState"), "recording": muted_recording, "audio_rms": muted_rms}

        assert "error" not in await wire.vendor("Configure", {**guard, "config": {**replacement, "muted": False}})
        await wire.poll("GetState", {}, lambda s: s.get("ready"))
        await wire.call("StartRecord")
        await asyncio.sleep(.35)
        assert (await switch("lower-gain", "B", "blue", "red"))["status"] == "accepted"
        await result("lower-gain")
        await asyncio.sleep(.35)
        gain_recording = (await wire.call("StopRecord"))["outputPath"]
        _, gain_rms = decode_recording(gain_recording)
        assert .3 < gain_rms / rms < .5, ("media gain ratio", gain_rms / rms)
        report["media_gain"] = {"recording": gain_recording, "audio_rms": gain_rms, "ratio_to_half_gain": gain_rms / rms}

        assert not (await wire.vendor("Clear", guard))["configured"]
        assert (await wire.vendor("GetState"))["readiness_error"] == "NOT_CONFIGURED"
        await wire.call("StartRecord")
        await asyncio.sleep(.35)
        assert (await switch("clear-cut", "B", "red", "blue"))["status"] == "accepted"
        cut = await result("clear-cut")
        await asyncio.sleep(.35)
        cut_recording = (await wire.call("StopRecord"))["outputPath"]
        centers, cut_rms = decode_recording(cut_recording)
        distances = np.minimum(np.linalg.norm(centers - (220, 40, 40), axis=1), np.linalg.norm(centers - (40, 40, 220), axis=1))
        assert np.max(distances) < 30 and cut_rms < .0001
        assert cut["role_map"] == v1_done["role_map"]
        report["clear_cut"] = {"result": cut, "recording": cut_recording, "audio_rms": cut_rms}
        noop = await switch("noop", "B", "blue", "blue")
        assert noop["status"] == "noop" and "frame_id" not in noop

        # Accepted configuration may still be unready; never cut around its
        # error. Synchronously rejected replacement preserves the old config.
        assert "error" not in await wire.vendor("Configure", {**guard, "config": {**config, "cut_point_ms": 19000}})
        invalid = await wire.poll("GetState", {}, lambda s: s.get("readiness_error") == "CUT_POINT_OUTSIDE_MEDIA")
        assert not invalid["ready"]
        assert (await switch("unready", "B", "blue", "red"))["error"] == "CUT_POINT_OUTSIDE_MEDIA"
        assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] == "blue"
        assert (await wire.vendor("Configure", {**guard, "config": {**config, "path": str(args.output / "invalid.webm")}}))["error"] == "ASSET_INVALID_CONTAINER"
        assert (await wire.vendor("GetState"))["config"]["cut_point_ms"] == 19000
        assert (await wire.vendor("Configure", {**guard, "config": {**config, "path": str(args.output / "missing.webm")}}))["error"] == "ASSET_MISSING"
        await wire.vendor("Clear", guard)
        print("Replacement, abort/replay, mute/gain, clear and invalid media passed", flush=True)
        terminal = [e["eventData"]["eventData"] for e in wire.events if e.get("eventType") == "VendorEvent" and
            e.get("eventData", {}).get("vendorName") == "pulsar-transitions"]
        report["terminal_events"] = terminal
        assert any(e.get("command_id") == "same-program" and e.get("status") == "completed" for e in terminal)
        take_events = [e["eventData"]["eventData"] for e in wire.events if e.get("eventType") == "VendorEvent" and
            e.get("eventData", {}).get("vendorName") == "pulsar-scene-switch" and
            e["eventData"].get("eventType") == "TakeCommitted"]
        assert len(take_events) == 1 and take_events[0]["take_command_id"] == "test-take-command"
        assert take_events[0]["frame_id"] > 0 and take_events[0]["pts_ns"] > 0
        assert take_events[0]["runtime_instance_id"] == runtime
        report["take"]["committed_event"] = take_events[0]
        report["passed"] = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    for key in ["PULSAR_STINGER_ASSET", "PULSAR_MIC_DEVICE_ID", "PULSAR_PROCESS_AUDIO_NAME", "PULSAR_TRACE_PATH"]:
        os.environ.pop(key, None)
    os.environ.update(PULSAR_NATIVE_STINGER="0", PULSAR_DUAL_LANE_TRANSITIONS="0", PULSAR_LEGACY_ALIAS="disabled",
        PULSAR_DIRECTSHOW_LEGACY_ALIAS="0", PULSAR_DESKTOP_AUDIO_DEVICE_ID="pulsar-synthetic-test-no-device",
        PULSAR_FPS="30", PULSAR_RECORD_CONTAINER="mkv")
    fixture(args.output / "fixture.webm")
    fixture(args.output / "second-éffect.webm", (155, 100, 235))
    (args.output / "invalid.webm").write_bytes(b"not a WebM")
    proc = process.PulsarProcess(args.exe.resolve(), "x264", args.output)
    report = {"passed": False}
    try:
        asyncio.run(exercise(args, proc, report))
    except Exception as exc:
        report["failure"] = str(exc)
        raise
    finally:
        try:
            proc.shutdown()
            report["shutdown"] = {"exit_code": proc.proc.returncode if proc.proc else None, "forced": proc.forced_kill_used}
        finally:
            (args.output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            safe = [line for line in proc.lines if "password" not in line.lower() and "PULSAR_READY" not in line]
            (args.output / "runtime.log").write_text("\n".join(safe), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "evidence": str(args.output / "result.json")}))


if __name__ == "__main__":
    main()
