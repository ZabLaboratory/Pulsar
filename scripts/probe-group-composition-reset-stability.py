#!/usr/bin/env python3
"""Bounded private-host probe for a synchronous native composition reset."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

from PIL import Image
import websockets

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence" / "local-20261017-native-reset-stability" / "eleven"
CACHE = ROOT / ".cache" / "native-reset-stability"


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load probe helper {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def connect(protocol, url: str, password: str):
    ws = await websockets.connect(url, subprotocols=["obswebsocket.json"],
                                  open_timeout=10)
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))["d"]
    identify = {"rpcVersion": hello["rpcVersion"], "eventSubscriptions": 0}
    auth = hello.get("authentication")
    if auth:
        identify["authentication"] = protocol.compute_auth(
            password, auth["salt"], auth["challenge"])
    await ws.send(json.dumps({"op": 1, "d": identify}))
    identified = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    if identified.get("op") != 2:
        raise RuntimeError(f"obs-websocket Identify failed: {identified}")
    return ws, protocol.Inbox()


async def request(protocol, inbox, ws, request_type: str, data=None,
                  timeout: float = 10.0):
    response = await protocol.request(inbox, ws, request_type, uuid.uuid4().hex,
                                      data, timeout=timeout)
    status = response.get("requestStatus", {})
    if not status.get("result"):
        raise RuntimeError(f"{request_type} failed: {status}")
    return response.get("responseData", {})


async def screenshot(protocol, inbox, ws, source_name: str,
                     output: pathlib.Path, stamp: str):
    result = await request(protocol, inbox, ws, "GetSourceScreenshot", {
        "sourceName": source_name, "imageFormat": "png",
        "imageWidth": 64, "imageHeight": 64,
    }, timeout=12)
    encoded = result["imageData"].split(",", 1)[1]
    data = base64.b64decode(encoded)
    path = output / f"{stamp}-scene-{source_name}.png"
    path.write_bytes(data)
    image = Image.open(io.BytesIO(data)).convert("RGBA")
    return {
        "path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
        "centerPixelRgba": list(image.getpixel((32, 32))),
        "dimensions": list(image.size),
    }


async def scenario(args, process, runtime_id: str, port: int, password: str,
                   report: dict, stamp: str):
    comp = load("reset_probe_composition", "probe-group-composition.py")
    protocol = load("reset_probe_protocol", "probe-source-kinds.py")
    url = f"ws://127.0.0.1:{port}"
    primary, primary_inbox = await connect(protocol, url, password)
    stats_ws = None
    stats_inbox = None
    stats_task = None
    stop_stats = asyncio.Event()
    try:
        settings = await request(protocol, primary_inbox, primary,
                                 "GetVideoSettings")
        viewport = (int(settings["baseWidth"]), int(settings["baseHeight"]))
        if min(viewport) <= 0:
            raise RuntimeError(f"invalid host viewport: {settings}")
        token = uuid.uuid4().hex[:12]
        scene = f"native-reset-stability-{token}"
        appearance_scene = f"native-reset-appearance-{token}"
        red_name = f"native-reset-red-{token}"
        blue_name = f"native-reset-blue-{token}"
        await request(protocol, primary_inbox, primary, "CreateScene",
                      {"sceneName": scene})
        await request(protocol, primary_inbox, primary, "CreateScene",
                      {"sceneName": appearance_scene})
        await request(protocol, primary_inbox, primary, "CreateInput", {
            "sceneName": scene, "inputName": red_name,
            "inputKind": "color_source_v3",
            "inputSettings": {"color": 0xFFFF0000,
                              "width": viewport[0], "height": viewport[1]},
            "sceneItemEnabled": True,
        })
        await request(protocol, primary_inbox, primary, "CreateInput", {
            "sceneName": appearance_scene, "inputName": blue_name,
            "inputKind": "color_source_v3",
            "inputSettings": {"color": 0xFF0000FF,
                              "width": viewport[0], "height": viewport[1]},
            "sceneItemEnabled": True,
        })
        wrapper = await request(protocol, primary_inbox, primary,
                                "CreateSceneItem", {
            "sceneName": scene, "sourceName": appearance_scene,
            "sceneItemEnabled": False,
        })
        red_id = (await request(protocol, primary_inbox, primary,
                                "GetSceneItemId", {
            "sceneName": scene, "sourceName": red_name,
        }))['sceneItemId']
        blue_id = wrapper["sceneItemId"]

        generation_one = comp.plan(scene, runtime_id, 1, red_id, blue_id,
                                   group_opacity=0.5, viewport=viewport)
        first = await request(protocol, primary_inbox, primary,
                              "CallVendorRequest", {
            "vendorName": "pulsar-capture-effects",
            "requestType": "CommitGroupComposition",
            "requestData": generation_one,
        })
        if not first.get("responseData", {}).get("ok"):
            raise RuntimeError(f"generation 1 commit rejected: {first}")

        generation_two = comp.plan(scene, runtime_id, 2, red_id, blue_id,
                                   group_opacity=0.75, viewport=viewport,
                                   reverse_children=True)
        second = await request(protocol, primary_inbox, primary,
                               "CallVendorRequest", {
            "vendorName": "pulsar-capture-effects",
            "requestType": "CommitGroupComposition",
            "requestData": generation_two,
        })
        if not second.get("responseData", {}).get("ok"):
            raise RuntimeError(f"replacement generation 2 rejected: {second}")
        await asyncio.sleep(0.2)
        before_reset = await screenshot(protocol, primary_inbox, primary,
                                        scene, EVIDENCE, stamp)
        if before_reset["centerPixelRgba"][3] not in (191, 192):
            raise AssertionError({
                "phase": "pre-reset-replacement-readback",
                "actualCenterPixel": before_reset["centerPixelRgba"],
                "expectedAlpha": [191, 192],
            })

        stats_ws, stats_inbox = await connect(protocol, url, password)
        stats_samples: list[dict] = []
        reset_started = time.monotonic()

        async def collect_stats():
            while not stop_stats.is_set():
                started = time.monotonic()
                try:
                    stats = await request(protocol, stats_inbox, stats_ws,
                                          "GetStats", timeout=3)
                    stats_samples.append({
                        "secondsFromReset": round(started - reset_started, 3),
                        "ok": True, "stats": stats,
                    })
                except Exception as error:
                    stats_samples.append({
                        "secondsFromReset": round(started - reset_started, 3),
                        "ok": False,
                        "error": f"{type(error).__name__}: {error}",
                    })
                try:
                    await asyncio.wait_for(stop_stats.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

        stats_task = asyncio.create_task(collect_stats())
        request_id = uuid.uuid4().hex
        reset_payload = {
            "sceneName": scene, "generation": 2,
            "runtimeInstanceId": runtime_id,
        }
        reset_body = {
            "vendorName": "pulsar-capture-effects",
            "requestType": "ResetGroupComposition",
            "requestData": reset_payload,
        }
        report["scenario"] = {
            "viewport": list(viewport), "sceneName": scene,
            "appearanceSceneName": appearance_scene,
            "redSceneItemId": red_id, "appearanceSceneItemId": blue_id,
            "generation1Plan": generation_one,
            "generation2ReplacementPlan": generation_two,
            "replacementGeneration2Response": second,
            "preResetScreenshot": before_reset,
            "resetRequestId": request_id,
            "resetPayload": reset_body,
            "resetTimeoutSeconds": args.reset_timeout,
        }

        async def send_reset():
            return await protocol.request(
                primary_inbox, primary, "CallVendorRequest", request_id,
                reset_body, timeout=args.reset_timeout)

        reset_task = asyncio.create_task(send_reset())
        try:
            response = await reset_task
            report["scenario"]["resetOutcome"] = "response"
            report["scenario"]["resetResponse"] = response
            status = response.get("requestStatus", {})
            if status.get("result"):
                reset_data = response.get("responseData", {}).get("responseData", {})
                report["scenario"]["resetAccepted"] = bool(
                    reset_data.get("ok") and reset_data.get("reset"))
            else:
                report["scenario"]["resetAccepted"] = False
            await asyncio.sleep(0.15)
            report["scenario"]["afterResetScreenshot"] = await screenshot(
                protocol, primary_inbox, primary, scene, EVIDENCE,
                stamp + "-after-reset")
        except (asyncio.TimeoutError, TimeoutError) as error:
            report["scenario"]["resetOutcome"] = "timeout"
            report["scenario"]["resetError"] = (
                f"{type(error).__name__}: {error}")
        except Exception as error:
            report["scenario"]["resetOutcome"] = "error"
            report["scenario"]["resetError"] = (
                f"{type(error).__name__}: {error}")
        finally:
            stop_stats.set()
            if stats_task is not None:
                try:
                    await asyncio.wait_for(stats_task, timeout=4)
                except Exception as error:
                    report["scenario"]["statsCollectorStopError"] = (
                        f"{type(error).__name__}: {error}")
            report["scenario"]["independentStatsSamples"] = stats_samples
            if not reset_task.done():
                reset_task.cancel()
                try:
                    await reset_task
                except BaseException:
                    pass
    finally:
        if stats_ws is not None:
            await stats_ws.close()
        await primary.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-source", type=pathlib.Path, required=True,
                        help="Qualified, read-only upstream rundir/RelWithDebInfo")
    parser.add_argument("--upstream-source", type=pathlib.Path, required=True,
                        help="Qualified upstream OBS source cache (read-only)")
    parser.add_argument("--upstream-build", type=pathlib.Path, required=True,
                        help="Qualified upstream OBS ABI/build cache (read-only)")
    parser.add_argument("--plugin", type=pathlib.Path, required=True,
                        help="Private instrumented pulsar-capture-effects.dll")
    parser.add_argument("--output", type=pathlib.Path, default=EVIDENCE)
    parser.add_argument("--reset-timeout", type=float, default=20.0)
    args = parser.parse_args()
    runtime_source = args.runtime_source.resolve()
    upstream_source = args.upstream_source.resolve()
    upstream_build = args.upstream_build.resolve()
    plugin_source = args.plugin.resolve()
    output = args.output.resolve()
    if output != EVIDENCE.resolve():
        raise ValueError(f"Output must be canonical: {EVIDENCE}")
    if not runtime_source.is_dir() or not plugin_source.is_file():
        raise FileNotFoundError("qualified runtime or private plugin is missing")
    if not upstream_source.is_dir() or not upstream_build.is_dir():
        raise FileNotFoundError("qualified upstream source/ABI cache is missing")
    if runtime_source != (upstream_build / "rundir" / "RelWithDebInfo").resolve():
        raise ValueError("runtime must be the rundir of the supplied qualified ABI cache")
    if args.reset_timeout < 5 or args.reset_timeout > 60:
        raise ValueError("reset timeout must be bounded between 5 and 60 seconds")

    CACHE.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    private_root = CACHE / f"private-runtime-{stamp}"
    if private_root.exists() or not private_root.resolve().is_relative_to(CACHE.resolve()):
        raise FileExistsError(private_root)
    runtime = private_root / "runtime"
    report_path = output / f"{stamp}-reset-stability.json"
    if report_path.exists():
        raise FileExistsError(report_path)

    process_protocol = load("reset_probe_process", "probe-websocket.py")
    composition_probe = load("reset_probe_composition", "probe-group-composition.py")
    source_status = subprocess.run(
        ["git", "-C", str(upstream_source), "status", "--short"],
        check=True, capture_output=True, text=True).stdout.strip()
    source_revision = subprocess.run(
        ["git", "-C", str(upstream_source), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()
    websocket_dir = runtime_source / "obs-plugins" / "64bit"
    browser_modules = sorted(
        p.name for p in websocket_dir.glob("*.dll")
        if "browser" in p.name.casefold())
    if browser_modules:
        raise RuntimeError(
            "this bounded synthetic reset probe requires the qualified no-browser "
            f"runtime; found: {browser_modules}")

    report = {
        "startedUtc": datetime.now(timezone.utc).isoformat(),
        "passed": False,
        "diagnosticCompleted": False,
        "scope": "private synthetic group replacement and one synchronous reset",
        "transitionReadinessSwitchDurationTouched": False,
        "liveOrRecording": False,
        "runtimeSource": str(runtime_source),
        "qualifiedObsProvenance": {
            "upstreamSource": str(upstream_source),
            "upstreamRevision": source_revision,
            "upstreamWorkingTreeStatus": source_status,
            "upstreamBuild": str(upstream_build),
            "obsLibSha256": sha256(upstream_build / "libobs" / "RelWithDebInfo" / "obs.lib"),
            "libobsSourceHashes": {
                name: sha256(upstream_source / "libobs" / name)
                for name in ("obs.c", "obs-video.c", "obs-scene.c", "obs-source.c")
            },
        },
        "runtimeProvenance": {
            "pulsarExeSha256": sha256(runtime_source / "bin" / "64bit" / "pulsar.exe"),
            "obsDllSha256": sha256(runtime_source / "bin" / "64bit" / "obs.dll"),
            "obsWebsocketSha256": sha256(websocket_dir / "obs-websocket.dll"),
            "capturePluginOriginalPresent": (websocket_dir / "pulsar-capture-effects.dll").exists(),
        },
        "instrumentedPlugin": {
            "path": str(plugin_source), "sha256": sha256(plugin_source),
            "sourcePath": str(ROOT / "plugins/pulsar-capture-effects/src/group-composition.cpp"),
            "sourceSha256": sha256(ROOT / "plugins/pulsar-capture-effects/src/group-composition.cpp"),
            "traceCompileDefinition": "PULSAR_GROUP_COMPOSITION_RESET_TRACE",
            "buildCache": str(plugin_source.parent.parent),
            "buildConfiguration": "RelWithDebInfo",
            "buildCompiler": "MSVC 19.44.35207 / Visual Studio 17.14.40",
            "buildFlags": "/DPULSAR_GROUP_COMPOSITION_RESET_TRACE /EHsc",
        },
        "probeSource": {
            "path": str(pathlib.Path(__file__).resolve()),
            "sha256": sha256(pathlib.Path(__file__).resolve()),
        },
        "browserModules": browser_modules,
    }
    process = None
    previous_env = dict(os.environ)
    runtime_id = "reset-stability-" + uuid.uuid4().hex
    try:
        for directory in ("bin", "data", "obs-plugins"):
            shutil.copytree(runtime_source / directory, runtime / directory)
        capture_target = runtime / "obs-plugins" / "64bit" / "pulsar-capture-effects.dll"
        shutil.copyfile(plugin_source, capture_target)
        report["privateRuntime"] = {
            "path": str(runtime),
            "capturePluginPath": str(capture_target),
            "capturePluginSha256": sha256(capture_target),
            "pluginBytesMatchSource": sha256(capture_target) == sha256(plugin_source),
        }
        for key in list(os.environ):
            if key.startswith("PULSAR_"):
                del os.environ[key]
        os.environ.update({
            "PULSAR_RUNTIME_ROOT": str(private_root / "state"),
            "PULSAR_RUNTIME_DIR": str(private_root / "state" / "runtime"),
            "PULSAR_RUNTIME_INSTANCE_ID": runtime_id,
            "PULSAR_DESKTOP_AUDIO_DEVICE_ID": "pulsar-synthetic-test-no-device",
            "PULSAR_TEST_NO_BROWSER_PRE_SHUTDOWN": "1",
        })
        port = process_protocol.pick_free_port()
        password = uuid.uuid4().hex + uuid.uuid4().hex
        exe = runtime / "bin" / "64bit" / "pulsar.exe"
        process = composition_probe.GracefulPrivatePulsarProcess(
            process_protocol, exe, port, password)
        process.spawn()
        report["ownedPid"] = process.proc.proc.pid
        report["shutdownHandlePresent"] = process.shutdown_handle is not None
        print(f"PRIVATE_PULSAR_PID={report['ownedPid']}", flush=True)
        print(f"PRIVATE_RUNTIME={runtime}", flush=True)
        process.wait_ready(60)
        time.sleep(2)
        initialized_audio = [line for line in process._lines
                             if "WASAPI:" in line and " initialized " in line]
        if initialized_audio:
            raise RuntimeError("synthetic host initialized a WASAPI endpoint")
        report["initializedWasapiDevices"] = initialized_audio
        asyncio.run(scenario(args, process, runtime_id, port, password,
                             report, stamp))
        report["diagnosticCompleted"] = True
        report["passed"] = report.get("scenario", {}).get("resetAccepted") is True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if process is not None:
            try:
                process.shutdown(20)
                report["gracefulShutdownRequested"] = process.graceful_shutdown_requested
                report["forcedTerminationUsed"] = process.forced_termination_used
                child = process.proc.proc
                report["hostExitCode"] = child.returncode if child else None
                report["hostStillRunning"] = bool(child and child.poll() is None)
                report["hostLogLines"] = [
                    process.protocol.redact_boot_line(line)
                    for line in process._lines
                ]
            except Exception as error:
                report["shutdownError"] = f"{type(error).__name__}: {error}"
        os.environ.clear()
        os.environ.update(previous_env)

    report["endedUtc"] = datetime.now(timezone.utc).isoformat()
    report["resetTraceLines"] = [
        line for line in report.get("hostLogLines", [])
        if "PULSAR_GROUP_COMPOSITION_RESET" in line
    ]
    phases = []
    for line in report["resetTraceLines"]:
        marker = " phase="
        if marker in line:
            phases.append(line.split(marker, 1)[1].strip())
    report["resetTracePhases"] = phases
    report["traceInterpretation"] = (
        "queue_wait_begin without graphics_enter means reset callback did not start; "
        "otherwise the last emitted phase narrows the in-callback wait. A missing "
        "queue_wait_end means the synchronous graphics queue wait did not return."
    )
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    host_log_path = output / f"{stamp}-host-log.txt"
    host_log_path.write_text("\n".join(report.get("hostLogLines", [])) + "\n",
                             encoding="utf-8")
    print(json.dumps({
        "report": str(report_path), "passed": report["passed"],
        "resetOutcome": report.get("scenario", {}).get("resetOutcome"),
        "ownedPid": report.get("ownedPid"),
        "resetTracePhases": report.get("resetTracePhases"),
    }, ensure_ascii=False), flush=True)
    return 0 if report["diagnosticCompleted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
