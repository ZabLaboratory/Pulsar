#!/usr/bin/env python3
"""Private real-CEF pixels and active-browser graceful shutdown qualification.

Reuses M3's page/protocol/pixel oracle and the inherited-event host owner.
No physical device, installed runtime, Live, recording or scene switch is used.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

import websockets

ROOT = Path(__file__).resolve().parent.parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


async def capture_active(process, password, page_url, output, report, browser, with_audio):
    async with websockets.connect(
        f"ws://127.0.0.1:{process.port}", subprotocols=["obswebsocket.json"],
        open_timeout=10, max_size=8 * 1024 * 1024,
    ) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 10))["d"]
        identify = {"rpcVersion": hello["rpcVersion"], "eventSubscriptions": 0}
        if "authentication" in hello:
            auth = hello["authentication"]
            identify["authentication"] = browser.compute_auth(password, auth["salt"], auth["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify}))
        assert json.loads(await asyncio.wait_for(ws.recv(), 10))["op"] == 2
        inbox = browser.Inbox()
        sequence = 0

        async def call(kind, data):
            nonlocal sequence
            sequence += 1
            response = await browser.request(inbox, ws, kind, str(sequence), data)
            assert response["requestStatus"]["result"], response["requestStatus"]
            return response.get("responseData", {})

        kinds = await call("GetInputKindList", {})
        assert "browser_source" in kinds["inputKinds"], "Actual browser module was not loaded"
        await call("CreateScene", {"sceneName": "cef-proof"})
        await call("CreateInput", {
            "sceneName": "cef-proof", "inputName": "cef-proof-browser", "inputKind": "browser_source",
            "sceneItemEnabled": True,
            "inputSettings": {"url": page_url, "width": 1280, "height": 720,
                              "fps_custom": True, "fps": 30, "shutdown": False,
                              "restart_when_active": False, "reroute_audio": with_audio},
        })
        await call("SetCurrentProgramScene", {"sceneName": "cef-proof"})
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            response = await browser.request(inbox, ws, "GetSourceScreenshot", str(sequence + 1), {
                "sourceName": "cef-proof-browser", "imageFormat": "png",
                "imageWidth": 1280, "imageHeight": 720,
            })
            sequence += 1
            if response["requestStatus"]["result"]:
                png = base64.b64decode(response["responseData"]["imageData"].split(",", 1)[1])
                width, height, channels, pixels = browser.decode_png(png)
                metrics = browser.analyse_frame(width, height, channels, pixels)
                if (width, height) == (1280, 720) and browser.frame_is_content(metrics):
                    output.write_bytes(png)
                    report["render"] = {"width": width, "height": height, "metrics": metrics,
                                        "pngSha256": digest(output)}
                    break
            await asyncio.sleep(0.5)
        assert "render" in report, "CEF never produced the local M3 page pixels"
        if with_audio:
            deadline = time.monotonic() + 10
            while not any("event=audio_stream_started" in line for line in process._lines):
                assert time.monotonic() < deadline, "Synthetic CEF audio never started"
                await asyncio.sleep(0.1)
            report["syntheticBrowserAudioStarted"] = True
        inputs = await call("GetInputList", {})
        assert any(i["inputName"] == "cef-proof-browser" for i in inputs["inputs"])
        report["browserSourceRetainedAtShutdown"] = True
        # Intentionally keep this active source alive until the real native barrier.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--browser-runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--with-audio", action="store_true",
                        help="Reuse the native shutdown suite's looped synthetic WAV page")
    args = parser.parse_args()
    output = args.output.resolve()
    evidence = (ROOT / "evidence/local-20261017-cef-runtime-qualification/eleven").resolve()
    if output.parent != evidence or output.exists():
        raise ValueError("Output must be a new canonical evidence JSON file")
    cache = (ROOT / ".cache/cef-runtime").resolve()
    scratch = cache / ("run-" + uuid.uuid4().hex)
    assert scratch.parent == cache and not scratch.exists()
    report = {"passed": False, "startedUtc": datetime.now(timezone.utc).isoformat(),
              "probeSourceSha256": digest(Path(__file__).resolve())}
    process = None
    browser = load("cef_m3", "probe-browser-m3.py")
    group = load("cef_host_owner", "probe-group-composition.py")
    protocol = load("cef_protocol", "probe-websocket.py")
    if args.with_audio:
        spec = importlib.util.spec_from_file_location(
            "cef_audio_fixture", ROOT / "tests/pulsar-headless-shutdown/test_graceful_shutdown.py")
        audio = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audio)
        page = audio._CefAudioServer()
    else:
        page = browser.LocalPageServer()
    old_env = {k: v for k, v in os.environ.items() if k.startswith("PULSAR_")}
    try:
        runtime = scratch / "runtime"
        for directory in ("bin", "data", "obs-plugins"):
            shutil.copytree(args.runtime / directory, runtime / directory)
        plugin_dir = runtime / "obs-plugins/64bit"
        assert not any("browser" in p.name.casefold() for p in plugin_dir.glob("*.dll")), "Use the known no-browser ABI base"
        shutil.copytree(args.browser_runtime, plugin_dir, dirs_exist_ok=True)
        assert (plugin_dir / "pulsar-browser.dll").is_file()
        assert not (plugin_dir / "obs-browser.dll").exists(), "Duplicate browser source owner"
        exe = runtime / "bin/64bit/pulsar.exe"
        shutil.copyfile(args.host, exe)
        report["artifacts"] = {
            str(p.relative_to(runtime)): digest(p) for p in [exe, exe.parent / "obs.dll",
                plugin_dir / "pulsar-browser.dll", plugin_dir / "pulsar-browser-page.exe",
                plugin_dir / "libcef.dll", plugin_dir / "obs-websocket.dll"]
        }
        report["browserSourceFiles"] = {
            str(p.relative_to(ROOT)): digest(p) for p in (ROOT / "plugins/pulsar-browser").rglob("*")
            if p.is_file()
        }
        for key in list(os.environ):
            if key.startswith("PULSAR_"):
                del os.environ[key]
        os.environ.update({"PULSAR_RUNTIME_ROOT": str(scratch / "state"),
                           "PULSAR_RUNTIME_DIR": str(scratch / "state/runtime"),
                           "PULSAR_RUNTIME_INSTANCE_ID": "cef-proof-" + uuid.uuid4().hex,
                           "PULSAR_DESKTOP_AUDIO_DEVICE_ID": "pulsar-synthetic-test-no-device"})
        password = uuid.uuid4().hex + uuid.uuid4().hex
        process = group.GracefulPrivatePulsarProcess(protocol, exe, protocol.pick_free_port(), password)
        process.spawn()
        report["ownedPid"] = process.proc.proc.pid
        process.wait_ready(60)
        if args.with_audio:
            page.start()
            page_url = page.url + "?lane=A"
        else:
            page_url = page.start()
        asyncio.run(capture_active(process, password, page_url, output.with_suffix(".png"), report, browser, args.with_audio))
        process.shutdown(grace=35)
        lines = process._lines
        markers = [line for line in lines if "PULSAR_CEF_SHUTDOWN" in line]
        report["cefShutdownMarkers"] = markers
        expected = ["pre_obs_shutdown_begin", "cef_shutdown_begin", "cef_shutdown_complete", "pre_obs_shutdown_complete"]
        positions = [next((i for i, line in enumerate(lines) if "event=" + event in line), -1) for event in expected]
        report["shutdown"] = {"exitCode": process.proc.proc.returncode,
                              "gracefulEventRequested": process.graceful_shutdown_requested,
                              "forcedTerminationUsed": process.forced_termination_used,
                              "orderedMarkerPositions": positions}
        assert process.proc.proc.returncode == 0, report["shutdown"]
        assert not process.forced_termination_used
        assert all(i >= 0 for i in positions) and positions == sorted(set(positions)), markers
        assert any("event=close_requested browser_count=1" in line for line in markers), markers
        assert any("event=barrier_released phase=Drained browser_count=0" in line for line in markers), markers
        assert not any("event=timeout" in line or "event=cef_shutdown_skipped" in line for line in markers), markers
        if args.with_audio:
            started = next(i for i, line in enumerate(lines) if "event=audio_stream_started" in line)
            stopped = next(i for i, line in enumerate(lines) if "event=audio_stream_stopped" in line)
            quiescent = next(i for i, line in enumerate(lines) if "event=audio_quiescent" in line)
            assert started < stopped <= quiescent < positions[1], markers
            report["audioDrainOrder"] = [started, stopped, quiescent, positions[1]]
        assert not any(" initialized " in line and "WASAPI:" in line for line in lines), "Unexpected physical audio endpoint"
        report["passed"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        cleanup_errors = []
        if process:
            try:
                process.shutdown(grace=35)
            except Exception as error:
                cleanup_errors.append({"operation": "host shutdown", "error": str(error)})
            report["processExitCode"] = process.proc.proc.poll() if process.proc.proc else None
            log = "\n".join(process._lines)
            log = re.sub(r"password=\S+", "password=[redacted]", log)
            output.with_suffix(".log").write_text(log, encoding="utf-8")
        try:
            if args.with_audio:
                page.close()
            else:
                page.stop()
        except Exception as error:
            cleanup_errors.append({"operation": "HTTP server close", "error": str(error)})
        for key in list(os.environ):
            if key.startswith("PULSAR_"):
                del os.environ[key]
        os.environ.update(old_env)
        if scratch.exists():
            assert scratch.resolve().parent == cache
            try:
                if process and process.proc.proc and process.proc.proc.poll() is None:
                    raise RuntimeError("Owned child is still alive; preserve its runtime")
                shutil.rmtree(scratch)
            except Exception as error:
                cleanup_errors.append({"operation": "scratch cleanup", "error": str(error)})
        if cleanup_errors:
            report["cleanupErrors"] = cleanup_errors
            report["passed"] = False
        report["scratchRemoved"] = not scratch.exists()
        report["limitations"] = "Private synthetic local page, actual CEF/libobs pixels and shutdown; no installed runtime, Solar/LSDP integration, physical capture, network Meet or stream qualification."
        output.write_bytes((json.dumps(report, indent=2) + "\n").encode())
    print(json.dumps({"passed": report["passed"], "report": str(output), "error": report.get("error")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
