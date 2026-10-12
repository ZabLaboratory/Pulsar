#!/usr/bin/env python3
"""Private Preview→Program regression: no capture device, recording or stream.

Reuses the existing synthetic media fixture and native process/shutdown helper.
The loopback browser producer counts document acquisition, allowing a Take to
prove source reuse as well as frame-boundary commit and observation admission.
"""
import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import platform
from pathlib import Path
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("handoff_helpers", ROOT / "scripts/probe-configurable-transition.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class Producer(BaseHTTPRequestHandler):
    documents = 0
    def do_GET(self):
        if self.path in ["/scene", "/scene-b"]:
            Producer.documents += 1
            colour = "rgb(35,180,80)" if self.path == "/scene" else "rgb(35,80,180)"
            body = ("<!doctype html><style>body{margin:0;background:" + colour + "}</style><script>setInterval(()=>document.title=String(performance.now()),50)</script>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *_):
        pass


async def exercise(args, proc, server, report):
    proc.spawn()
    ready = await asyncio.to_thread(proc.wait_for, helpers.process.READY_RE, 45)
    uri, password = ready.groups()
    async with websockets.connect(uri, max_size=16 * 1024 * 1024) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion": 1, "eventSubscriptions": 0x7ff}
        if "authentication" in hello:
            a = hello["authentication"]
            secret = base64.b64encode(hashlib.sha256((password + a["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(hashlib.sha256((secret + a["challenge"]).encode()).digest()).decode()
        await ws.send(json.dumps({"op": 1, "d": identify}))
        assert json.loads(await ws.recv())["op"] == 2
        wire = helpers.Wire(ws)
        native = await wire.vendor("GetState")
        guard = {"runtime_instance_id": native["runtime_instance_id"]}
        await wire.call("CreateScene", {"sceneName": "ZabPreviewComposite"})
        await wire.call("CreateInput", {"sceneName": "ZabPreviewComposite", "inputName": "handoff-browser",
            "inputKind": "browser_source", "sceneItemEnabled": True,
            "inputSettings": {"url": f"http://127.0.0.1:{server.server_port}/scene", "width": 1920,
                "height": 1080, "shutdown": False, "restart_when_active": False}})
        await wire.call("SetStudioModeEnabled", {"studioModeEnabled": True})
        await wire.call("SetCurrentPreviewScene", {"sceneName": "ZabPreviewComposite"})
        deadline = time.monotonic() + 20
        while Producer.documents == 0 and time.monotonic() < deadline:
            await asyncio.sleep(.1)
        assert Producer.documents == 1, Producer.documents
        before_inputs = (await wire.call("GetInputList"))["inputs"]
        before = next(x for x in before_inputs if x["inputName"] == "handoff-browser")
        report["producer_before"] = {"uuid": before.get("inputUuid"), "documents": Producer.documents}
        if not args.baseline:
            async def preview_cover(tag, abort=False):
                program_before = (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"]
                await wire.poll("GetState", {}, lambda s: s.get("ready") and not s.get("busy"))
                begun = await wire.vendor("BeginPreview", {**guard, "command_id": tag})
                assert "error" not in begun, begun
                covered = await wire.poll("GetPreviewResult", {**guard, "command_id": tag},
                    lambda s: s.get("status") == "covered")
                assert covered["start_frame_id"] > 0 and covered["start_pts_ns"] > 0, covered
                ended = await wire.vendor("EndPreview", {**guard, "command_id": tag, "abort": abort})
                assert "error" not in ended, ended
                done = await wire.poll("GetPreviewResult", {**guard, "command_id": tag},
                    lambda s: s.get("status") == ("aborted" if abort else "completed"))
                assert done["frame_id"] > covered["start_frame_id"], done
                assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] == program_before
                return done

            assert (await wire.vendor("SetPreviewComposite", {"enabled": True}, "pulsar-scene-switch"))["ok"]
        configured = await wire.vendor("Configure", {**guard, "config": {
            "path": str(args.asset), "cut_point_ms": 700, "volume": 0, "muted": True}})
        assert "error" not in configured, configured
        await wire.poll("GetState", {}, lambda s: s.get("ready"))
        state = await wire.vendor("GetState", vendor="pulsar-scene-switch")
        base = {"contract": "pulsar.scene-switch.v1", "schema_version": 1, "message_type": "command",
            **guard, "intent_id": "handoff", "timeout_ms": 8000}
        prep = {**base, "command_type": "Prepare", "command_id": "handoff-prepare",
            "expected_revisions": state["revisions"], "expected_server_seq": state["server_seq"],
            "target": {"lane_id": state["role_map"]["preview"], "scene_id": "ZabPreviewComposite"}}
        assert (await wire.vendor("Prepare", prep, "pulsar-scene-switch"))["event_type"] == "PrepareAccepted"
        state = await wire.poll("GetState", {}, lambda s: s.get("state") == "preview_ready", vendor="pulsar-scene-switch")
        take = {**base, "command_type": "Take", "command_id": "handoff-take", "expected_revisions": state["revisions"],
            "expected_server_seq": state["server_seq"], "prepared_command_id": "handoff-prepare"}
        accepted = await wire.vendor("Take", take, "pulsar-scene-switch")
        if args.trace_anchor_fault:
            assert accepted["event_type"] != "TakeAccepted", accepted
            assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] != "ZabPreviewComposite"
            assert any("Take rejected: runtime telemetry integrity fail-stop is degraded" in x for x in proc.lines)
            report.update(passed=True, configured_trace_fault_blocks_take=True, refused=accepted)
            return
        assert accepted["event_type"] == "TakeAccepted", accepted
        report["reads_during_take"] = []
        for op in ["GetAdaptiveState", "GetVideoSettings", "GetDestinations"]:
            pending = await wire.vendor("GetState", vendor="pulsar-scene-switch")
            assert pending["state"] == "take_accepted", pending
            started = time.perf_counter()
            data = {"vendorName": "pulsar", "requestType": op, "requestData": {}}
            if args.baseline:
                result = await wire.call("CallVendorRequest", data, error=True)
                assert "PREVIEW_FROZEN" in result.get("comment", ""), result
            else:
                result = (await wire.call("CallVendorRequest", data))["responseData"]
                assert "error" not in result, result
            report["reads_during_take"].append({"operation": op, "elapsed_ms": (time.perf_counter()-started)*1000,
                "result": result})
        for vendor, op in [("pulsar", "GetUnknownFutureOperation"), ("unknown", "GetVideoSettings"), ("pulsar", "SetVideoBitrate")]:
            result = await wire.call("CallVendorRequest", {"vendorName": vendor, "requestType": op, "requestData": {}}, error=True)
            assert "PREVIEW_FROZEN" in result.get("comment", ""), result
        malformed = await wire.call("CallVendorRequest", {"vendorName": ["pulsar"], "requestType": "GetVideoSettings"}, error=True)
        assert "PREVIEW_FROZEN" in malformed.get("comment", ""), malformed
        composite_mutation = await wire.call("CallVendorRequest", {"vendorName": "pulsar-scene-switch", "requestType": "SetPreviewComposite",
            "requestData": {"enabled": True, "scene_name": "ZabPreviewComposite-B"}}, error=True)
        assert "PREVIEW_FROZEN" in composite_mutation.get("comment", ""), composite_mutation
        report["unsafe_vendor_calls_gated"] = 5
        committed = await wire.poll("GetState", {}, lambda s: s.get("revisions", {}).get("program") == state["revisions"]["program"] + 1, vendor="pulsar-scene-switch")
        await asyncio.sleep(.1)
        events = [x["eventData"]["eventData"] for x in wire.events if x.get("eventType") == "VendorEvent"
            and x.get("eventData", {}).get("vendorName") == "pulsar-scene-switch" and x["eventData"].get("eventType") == "TakeCommitted"]
        assert len(events) == 1 and events[0]["frame_id"] > 0 and events[0]["pts_ns"] > 0, events
        program = (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"]
        preview = (await wire.call("GetCurrentPreviewScene"))["currentPreviewSceneName"]
        assert program == "ZabPreviewComposite" and preview != program, (program, preview)
        if not args.baseline:
            rebound = await wire.vendor("SetPreviewComposite", {"enabled": True}, "pulsar-scene-switch")
            assert not rebound["ok"] and rebound["error_code"] == "COMPOSITE_ON_PROGRAM", rebound
            assert (await wire.vendor("SetPreviewComposite", {"enabled": False}, "pulsar-scene-switch"))["ok"]
            root_cover = await wire.vendor("BeginPreview", {**guard, "command_id": "root-cover-refused"})
            assert root_cover.get("error") == "PREVIEW_COMPOSITE_REQUIRED", root_cover
            report["program_preview_alias_rejected"] = True
            for name in [17, [], "", "x" * 257, "bad\x00name"]:
                invalid = await wire.vendor("SetPreviewComposite", {"enabled": True, "scene_name": name}, "pulsar-scene-switch")
                assert not invalid["ok"], invalid
            missing = await wire.vendor("SetPreviewComposite", {"enabled": True, "scene_name": "not-created"}, "pulsar-scene-switch")
            assert not missing["ok"] and missing["error_code"] == "COMPOSITE_UNAVAILABLE", missing
            report["invalid_composite_names_refused"] = 6
        after_inputs = (await wire.call("GetInputList"))["inputs"]
        after = next(x for x in after_inputs if x["inputName"] == "handoff-browser")
        assert before["inputUuid"] == after["inputUuid"] and Producer.documents == 1
        assert len(before_inputs) == len(after_inputs), (before_inputs, after_inputs)
        report.update(committed=events[0], role_map=committed["role_map"], program=program, preview=preview,
            producer_after={"uuid": after["inputUuid"], "documents": Producer.documents}, passed=True)
        if not args.baseline:
            assert not (await wire.call("GetStreamStatus"))["outputActive"]
            assert not (await wire.call("GetRecordStatus"))["outputActive"]

            async def rotate(scene, command):
                await wire.poll("GetState", {}, lambda s: s.get("ready") and not s.get("busy"))
                current = await wire.vendor("GetState", vendor="pulsar-scene-switch")
                p = {**base, "command_type": "Prepare", "command_id": command + "-prepare",
                    "expected_revisions": current["revisions"], "expected_server_seq": current["server_seq"],
                    "target": {"lane_id": current["role_map"]["preview"], "scene_id": scene}}
                assert (await wire.vendor("Prepare", p, "pulsar-scene-switch"))["event_type"] == "PrepareAccepted"
                ready = await wire.poll("GetState", {}, lambda s: s.get("state") == "preview_ready", vendor="pulsar-scene-switch")
                t = {**base, "command_type": "Take", "command_id": command + "-take",
                    "expected_revisions": ready["revisions"], "expected_server_seq": ready["server_seq"],
                    "prepared_command_id": command + "-prepare"}
                admission = await wire.vendor("Take", t, "pulsar-scene-switch")
                assert admission["event_type"] == "TakeAccepted", admission
                return await wire.poll("GetState", {}, lambda s: s.get("revisions", {}).get("program") == current["revisions"]["program"] + 1,
                    vendor="pulsar-scene-switch")

            await wire.call("CreateScene", {"sceneName": "ZabPreviewComposite-B"})
            await wire.call("CreateInput", {"sceneName": "ZabPreviewComposite-B", "inputName": "handoff-browser-B",
                "inputKind": "browser_source", "sceneItemEnabled": True,
                "inputSettings": {"url": f"http://127.0.0.1:{server.server_port}/scene-b", "width": 1920,
                    "height": 1080, "shutdown": False, "restart_when_active": False}})
            chosen = await wire.vendor("SetPreviewComposite", {"enabled": True, "scene_name": "ZabPreviewComposite-B"}, "pulsar-scene-switch")
            assert chosen["ok"] and chosen["source_name"] == "ZabPreviewComposite-B", chosen
            assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] == "ZabPreviewComposite"
            deadline = time.monotonic() + 20
            while Producer.documents < 2 and time.monotonic() < deadline:
                await asyncio.sleep(.1)
            assert Producer.documents == 2, Producer.documents
            browser_b = next(x for x in (await wire.call("GetInputList"))["inputs"] if x["inputName"] == "handoff-browser-B")
            assert browser_b["inputUuid"] != before["inputUuid"]
            cover_b = await preview_cover("cover-b")
            await rotate("ZabPreviewComposite-B", "take-b")
            assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] == "ZabPreviewComposite-B"
            refused = await wire.vendor("SetPreviewComposite", {"enabled": True, "scene_name": "ZabPreviewComposite-B"}, "pulsar-scene-switch")
            assert not refused["ok"] and refused["error_code"] == "COMPOSITE_ON_PROGRAM", refused
            await wire.vendor("SetPreviewComposite", {"enabled": False}, "pulsar-scene-switch")
            assert (await wire.vendor("SetPreviewComposite", {"enabled": True, "scene_name": "ZabPreviewComposite"}, "pulsar-scene-switch"))["ok"]
            cover_a = await preview_cover("cover-a-abort", abort=True)
            await rotate("ZabPreviewComposite", "take-a")
            assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] == "ZabPreviewComposite"
            unbound = await wire.vendor("BeginPreview", {**guard, "command_id": "cover-b-after-third-take"})
            assert unbound.get("error") == "PREVIEW_COMPOSITE_REQUIRED", unbound
            assert (await wire.vendor("SetPreviewComposite", {"enabled": True, "scene_name": "ZabPreviewComposite-B"}, "pulsar-scene-switch"))["ok"]
            cover_b_again = await preview_cover("cover-b-after-third-take")
            final = next(x for x in (await wire.call("GetInputList"))["inputs"] if x["inputName"] == "handoff-browser")
            assert final["inputUuid"] == before["inputUuid"] and Producer.documents == 2
            final_b = next(x for x in (await wire.call("GetInputList"))["inputs"] if x["inputName"] == "handoff-browser-B")
            assert final_b["inputUuid"] == browser_b["inputUuid"]
            retained = await wire.poll("GetState", {}, lambda s: s.get("ready") and not s.get("busy"))
            assert retained["config"]["path"] == str(args.asset) and retained["ready"]
            report["two_producer_rotation"] = {"program_sequence": ["ZabPreviewComposite", "ZabPreviewComposite-B", "ZabPreviewComposite"],
                "browser_a_uuid": final["inputUuid"], "browser_b_uuid": final_b["inputUuid"], "documents": Producer.documents,
                "stinger_configuration_retained": True, "takes_completed": 3}
            report["preview_media_after_take"] = {"named_b_completed": cover_b, "named_a_aborted": cover_a,
                "named_b_completed_after_third_take": cover_b_again,
                "root_requires_explicit_composite_rebinding": True, "program_unchanged": True, "bound_composite_restored": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--container", choices=["webm", "mp4"], default="webm")
    parser.add_argument("--trace-anchor-fault", action="store_true", help="Configured trace with a private invalid file anchor; Take must fail-stop")
    args = parser.parse_args()
    args.output = args.output.resolve(); args.output.mkdir(parents=True, exist_ok=True)
    for key in ["PULSAR_STINGER_ASSET", "PULSAR_MIC_DEVICE_ID", "PULSAR_PROCESS_AUDIO_NAME", "PULSAR_TRACE_PATH"]:
        os.environ.pop(key, None)
    os.environ.update(PULSAR_NATIVE_STINGER="0", PULSAR_DUAL_LANE_TRANSITIONS="0", PULSAR_LEGACY_ALIAS="disabled",
        PULSAR_DIRECTSHOW_LEGACY_ALIAS="0", PULSAR_DESKTOP_AUDIO_DEVICE_ID="pulsar-synthetic-test-no-device", PULSAR_FPS="30")
    helpers.fixture(args.output / "fixture.webm")
    args.asset = args.output / ("fixture." + args.container)
    if args.container == "mp4":
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(args.output / "fixture.webm"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(args.asset)], check=True, timeout=30)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Producer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    extra = {}
    if args.trace_anchor_fault:
        trace_path = args.output / "trace.ndjson"
        trace_path.mkdir(exist_ok=True)
        os.environ["PULSAR_TRACE_HMAC_KEY"] = secrets.token_hex(32)
        extra = {"trace_path": trace_path, "build_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "trace_host": platform.node(), "trace_gpu": "functional-fault-injection-no-performance-claim"}
    proc = helpers.process.PulsarProcess(args.exe.resolve(), "x264", args.output, **extra)
    report = {"passed": False, "baseline": args.baseline, "exe_sha256": hashlib.sha256(args.exe.read_bytes()).hexdigest(),
        "recording_started": False, "stream_started": False, "physical_microphone_opened": False,
        "container": args.container, "trace_anchor_fault": args.trace_anchor_fault}
    try:
        asyncio.run(exercise(args, proc, server, report))
    except Exception as exc:
        report["failure"] = repr(exc)
        raise
    finally:
        try:
            proc.shutdown()
            report["shutdown"] = {"exit_code": proc.proc.returncode if proc.proc else None, "forced": proc.forced_kill_used}
            report["disabled_trace_errors"] = sum("accept rejected reason=producer_disabled" in x for x in proc.lines)
        finally:
            server.shutdown(); server.server_close()
            (args.output / "result.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
            safe = [x for x in proc.lines if "password" not in x.lower() and "PULSAR_READY" not in x]
            (args.output / "runtime.log").write_text("\n".join(safe)+"\n", encoding="utf-8")
    assert report["shutdown"] == {"exit_code": 0, "forced": False}, report["shutdown"]
    if not args.baseline:
        assert report["disabled_trace_errors"] == 0, report["disabled_trace_errors"]
    print(json.dumps({"passed": report["passed"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
