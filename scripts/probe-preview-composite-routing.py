#!/usr/bin/env python3
"""Exercise Preview composite binding across private native A/B role swaps.

Synthetic colour sources only; no Prism, capture device, stream or recording.
Reuses the existing isolated process, transition fixture and WebSocket helpers.
"""
import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys

spec = importlib.util.spec_from_file_location(
    "routing_helpers", Path(__file__).with_name("probe-configurable-transition.py")
)
helpers = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helpers
spec.loader.exec_module(helpers)


async def exercise(args, proc, report):
    proc.spawn()
    ready = await asyncio.to_thread(proc.wait_for, helpers.process.READY_RE, 45)
    uri, password = ready.groups()
    async with helpers.websockets.connect(uri) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion": 1, "eventSubscriptions": 0x7ff}
        if "authentication" in hello:
            auth = hello["authentication"]
            secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        await ws.send(json.dumps({"op": 1, "d": identify}))
        assert json.loads(await ws.recv())["op"] == 2
        wire = helpers.Wire(ws)
        vendor = "pulsar-scene-switch"
        names = {"A": "ZabLaneAComposite", "B": "ZabPreviewComposite"}
        for lane, scene in names.items():
            await wire.call("CreateScene", {"sceneName": scene})
            await wire.call("CreateInput", {
                "sceneName": scene, "inputName": "routing-colour-" + lane,
                "inputKind": "color_source_v3", "sceneItemEnabled": True,
                "inputSettings": {"color": 0xff2266cc if lane == "A" else 0xffcc6622,
                                  "width": 1920, "height": 1080},
            })
        state = await wire.vendor("GetState", vendor=vendor)
        runtime = state["runtime_instance_id"]
        guard = {"runtime_instance_id": runtime}
        await wire.vendor("Configure", {**guard, "config": {
            "path": str(args.output / "fixture.webm"), "cut_point_ms": 700,
            "volume": 0, "muted": True}})
        await wire.poll("GetState", {}, lambda s: s.get("ready"))
        report["bindings"] = []
        for index in range(4):
            state = await wire.vendor("GetState", vendor=vendor)
            lane = state["role_map"]["preview"]
            scene = names[lane]
            program_before = await wire.call("GetCurrentProgramScene")
            for request in ({"enabled": True, "scene_name": scene}, {"enabled": True}):
                bound = await wire.vendor("SetPreviewComposite", request, vendor)
                assert bound == {"ok": True, "source_name": scene}, bound
            refused = await wire.vendor("SetPreviewComposite", {
                "enabled": True, "scene_name": names[state["role_map"]["on_air"]]}, vendor)
            assert not refused["ok"] and "current Preview lane" in refused["error"], refused
            malformed = await wire.vendor("SetPreviewComposite", {"enabled": True, "scene_name": 7}, vendor)
            assert not malformed["ok"], malformed
            cover = {**guard, "command_id": f"binding-cover-{index}"}
            accepted_cover = await wire.vendor("BeginPreview", cover)
            assert "error" not in accepted_cover, accepted_cover
            await wire.poll("GetPreviewResult", cover, lambda s: s.get("status") == "covered")
            assert (await wire.vendor("SetPreviewComposite", {
                "enabled": True, "scene_name": scene}, vendor))["source_name"] == scene
            assert (await wire.vendor("GetPreviewResult", cover))["status"] == "covered"
            await wire.vendor("EndPreview", {**cover, "abort": True})
            await wire.poll("GetPreviewResult", cover, lambda s: s.get("status") == "aborted")
            await wire.poll("GetState", {}, lambda s: s.get("ready") and not s.get("busy"))
            assert await wire.call("GetCurrentProgramScene") == program_before
            assert (await wire.vendor("GetState", vendor=vendor))["revisions"] == state["revisions"]
            report["bindings"].append({"lane": lane, "source_name": scene,
                                       "programPreserved": True, "programBindingRejected": True,
                                       "transitionCoverPreserved": True})
            if index == 3:
                break
            base = {"contract": "pulsar.scene-switch.v1", "schema_version": 1,
                    "message_type": "command", **guard, "intent_id": f"routing-{index}", "timeout_ms": 8000}
            prep = {**base, "command_type": "Prepare", "command_id": f"prepare-{index}",
                    "expected_revisions": state["revisions"], "expected_server_seq": state["server_seq"],
                    "target": {"lane_id": lane, "scene_id": scene}}
            assert (await wire.vendor("Prepare", prep, vendor))["event_type"] == "PrepareAccepted"
            prepared = await wire.poll("GetState", {}, lambda s: s.get("state") == "preview_ready", vendor=vendor)
            take = {**base, "command_type": "Take", "command_id": f"take-{index}",
                    "expected_revisions": prepared["revisions"], "expected_server_seq": prepared["server_seq"],
                    "prepared_command_id": f"prepare-{index}"}
            assert (await wire.vendor("Take", take, vendor))["event_type"] == "TakeAccepted"
            await wire.poll("GetState", {}, lambda s: s.get("role_map", {}).get("on_air") == lane, vendor=vendor)
            await wire.poll("GetState", {}, lambda s: s.get("ready") and not s.get("busy"))
        for command in ("GetStreamStatus", "GetRecordStatus"):
            assert not (await wire.call(command))["outputActive"]
        report["outputsInactive"] = True
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
    identity = "preview-routing-" + secrets.token_hex(6)
    for key in ("PULSAR_STINGER_ASSET", "PULSAR_MIC_DEVICE_ID", "PULSAR_PROCESS_AUDIO_NAME", "PULSAR_TRACE_PATH"):
        os.environ.pop(key, None)
    os.environ.update(PULSAR_NATIVE_STINGER="0", PULSAR_DUAL_LANE_TRANSITIONS="0",
        PULSAR_LEGACY_ALIAS="disabled", PULSAR_DIRECTSHOW_LEGACY_ALIAS="0",
        PULSAR_DESKTOP_AUDIO_DEVICE_ID="pulsar-synthetic-test-no-device", PULSAR_FPS="30",
        PULSAR_RUNTIME_ROOT=str(args.output / "runtimes"), PULSAR_RUNTIME_DIR=str(args.output / "runtimes" / identity))
    helpers.fixture(args.output / "fixture.webm")
    proc = helpers.process.PulsarProcess(args.exe.resolve(), "x264", args.output, runtime_id=identity)
    report = {"passed": False, "exe_sha256": hashlib.sha256(args.exe.read_bytes()).hexdigest()}
    try:
        asyncio.run(exercise(args, proc, report))
    except Exception as error:
        report["error"] = repr(error)
    finally:
        proc.shutdown()
        report["shutdown"] = {"exitCode": proc.proc.returncode if proc.proc else None,
                              "forced": proc.forced_kill_used}
        report["passed"] = report["passed"] and report["shutdown"] == {"exitCode": 0, "forced": False}
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
