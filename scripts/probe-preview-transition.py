#!/usr/bin/env python3
"""Native Preview continuity regression probe, using only synthetic sources.

No Prism session, camera, desktop capture, recording or stream is touched.
--output owns temporary generated media and --report names durable JSON evidence.
Reuses the existing public WebSocket/process lifecycle and alpha fixture helpers.
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
import sys
import time

import websockets

spec = importlib.util.spec_from_file_location(
    "preview_probe_helpers", Path(__file__).with_name("probe-configurable-transition.py")
)
helpers = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helpers
spec.loader.exec_module(helpers)


async def exercise(args, proc, report):
    proc.spawn()
    ready = await asyncio.to_thread(proc.wait_for, helpers.process.READY_RE, 45)
    uri, password = ready.groups()
    async with websockets.connect(uri, max_size=16 * 1024 * 1024) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion": 1, "eventSubscriptions": 0x7ff}
        if "authentication" in hello:
            auth = hello["authentication"]
            secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()
            ).decode()
        await ws.send(json.dumps({"op": 1, "d": identify}))
        assert json.loads(await ws.recv())["op"] == 2
        wire = helpers.Wire(ws)
        native = await wire.vendor("GetState")
        assert native["operational"] and not native["configured"], native
        guard = {"runtime_instance_id": native["runtime_instance_id"]}
        report["runtime_instance_id"] = native["runtime_instance_id"]
        await wire.call("CreateScene", {"sceneName": "ZabPreviewComposite"})
        await wire.call("CreateInput", {
            "sceneName": "ZabPreviewComposite", "inputName": "synthetic-preview",
            "inputKind": "color_source_v3", "sceneItemEnabled": True,
            "inputSettings": {"color": 0xff2828dc, "width": 1920, "height": 1080},
        })
        assert (await wire.vendor("SetPreviewComposite", {"enabled": True}, "pulsar-scene-switch"))["ok"]
        program = (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"]
        await wire.vendor("Configure", {**guard, "config": {
            "path": str(args.output / "fixture.webm"), "cut_point_ms": 700,
            "volume": .5, "muted": False,
        }})
        await wire.poll("GetState", {}, lambda s: s.get("ready"))
        report["runs"] = []

        for index, delay in enumerate([.8, 0, .35]):
            command = {**guard, "command_id": f"continuous-preview-{index}"}
            start = time.perf_counter()
            accepted = await wire.vendor("BeginPreview", command)
            assert "error" not in accepted, accepted
            covered = await wire.poll("GetPreviewResult", command, lambda s: s.get("status") == "covered")
            capture_ms = (time.perf_counter() - start) * 1000
            assert covered["cover_kind"] == "outgoing_frame", covered
            assert "playback_start_frame_id" not in covered, covered
            assert "media_time_ms" not in covered, covered
            # Exercise the same in-place reconciliation Prism performs while
            # the captured outgoing frame remains attached to PreviewView.
            await wire.call("SetInputSettings", {"inputName": "synthetic-preview",
                "inputSettings": {"color": 0xff28dc28 if index % 2 == 0 else 0xffdc2828}})
            assert (await wire.vendor("SetPreviewComposite", {"enabled": True}, "pulsar-scene-switch"))["ok"]
            await asyncio.sleep(delay)
            still = await wire.vendor("GetPreviewResult", command)
            assert still["status"] == "covered" and "media_time_ms" not in still, still
            assert (await wire.vendor("GetState"))["busy"]
            playback_start = time.perf_counter()
            released = await wire.vendor("EndPreview", {**command, "abort": False})
            assert "error" not in released, released
            samples = []
            deadline = playback_start + 5
            while time.perf_counter() < deadline:
                state = await wire.vendor("GetPreviewResult", command)
                samples.append({"elapsed_ms": (time.perf_counter() - playback_start) * 1000, **state})
                assert state.get("media_paused") is not True, state
                if state["status"] in ["completed", "aborted", "failed"]:
                    break
                await asyncio.sleep(.04)
            final = samples[-1]
            report["last_attempt"] = {"preparation_delay_ms": delay * 1000, "samples": samples}
            assert final["status"] == "completed", final
            assert final["frame_id"] > final["playback_start_frame_id"] > final["start_frame_id"] > 0
            assert final["pts_ns"] > final["playback_start_pts_ns"] > final["start_pts_ns"] > 0
            phases = {s["status"] for s in samples}
            assert {"closing", "opening", "completed"} <= phases, phases
            times = [s["media_time_ms"] for s in samples if "media_time_ms" in s]
            assert all(b >= a for a, b in zip(times, times[1:])), times
            # A wall-clock plateau at the old 700ms pause would fail this bound.
            around_cut = [s for s in samples if 500 <= s.get("media_time_ms", -1) <= 900]
            assert len(around_cut) >= 4, around_cut
            span = around_cut[-1]["elapsed_ms"] - around_cut[0]["elapsed_ms"]
            advance = around_cut[-1]["media_time_ms"] - around_cut[0]["media_time_ms"]
            assert advance > 200 and abs(span - advance) < 170, (span, advance)
            assert 1200 < final["elapsed_ms"] < 2100, final["elapsed_ms"]
            assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] == program
            assert (await wire.vendor("EndPreview", {**command, "abort": False}))["status"] == "completed"
            report["runs"].append({"preparation_delay_ms": delay * 1000, "capture_ms": capture_ms,
                "cut_window_wall_ms": span, "cut_window_media_ms": advance, "samples": samples})
            await wire.poll("GetState", {}, lambda s: s.get("ready") and not s.get("busy"))

        report["aborts"] = []
        for phase in ["preparing", "playing"]:
            command = {**guard, "command_id": f"abort-{phase}"}
            await wire.vendor("BeginPreview", command)
            await wire.poll("GetPreviewResult", command, lambda s: s.get("status") == "covered")
            if phase == "playing":
                await wire.vendor("EndPreview", {**command, "abort": False})
                await wire.poll("GetPreviewResult", command, lambda s: s.get("media_time_ms", 0) >= 300)
            await wire.vendor("EndPreview", {**command, "abort": True})
            result = await wire.poll("GetPreviewResult", command, lambda s: s.get("status") == "aborted")
            assert result["frame_id"] > 0 and result["pts_ns"] > 0
            report["aborts"].append({"phase": phase, "result": result})
            await wire.poll("GetState", {}, lambda s: s.get("ready") and not s.get("busy"))
        await wire.vendor("Clear", guard)
        assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] == program
        report["passed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    for key in ["PULSAR_STINGER_ASSET", "PULSAR_MIC_DEVICE_ID", "PULSAR_PROCESS_AUDIO_NAME", "PULSAR_TRACE_PATH"]:
        os.environ.pop(key, None)
    os.environ.update(PULSAR_NATIVE_STINGER="0", PULSAR_DUAL_LANE_TRANSITIONS="0", PULSAR_LEGACY_ALIAS="disabled",
        PULSAR_DIRECTSHOW_LEGACY_ALIAS="0", PULSAR_DESKTOP_AUDIO_DEVICE_ID="pulsar-synthetic-test-no-device", PULSAR_FPS="30")
    helpers.fixture(args.output / "fixture.webm")
    proc = helpers.process.PulsarProcess(args.exe.resolve(), "x264", args.output)
    report = {"passed": False, "exe_sha256": hashlib.sha256(args.exe.read_bytes()).hexdigest()}
    try:
        asyncio.run(exercise(args, proc, report))
    except Exception as exc:
        report["failure"] = repr(exc)
        raise
    finally:
        try:
            proc.shutdown()
            report["shutdown"] = {"exit_code": proc.proc.returncode if proc.proc else None, "forced": proc.forced_kill_used}
        finally:
            args.report.write_bytes((json.dumps(report, indent=2) + "\n").encode())
            safe = [line for line in proc.lines if "password" not in line.lower() and "PULSAR_READY" not in line]
            (args.output / "runtime.log").write_bytes(("\n".join(safe) + "\n").encode())
    assert report["shutdown"] == {"exit_code": 0, "forced": False}, report["shutdown"]
    print(json.dumps({"passed": report["passed"], "report": str(args.report)}))


if __name__ == "__main__":
    main()
