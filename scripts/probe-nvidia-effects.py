#!/usr/bin/env python3
"""Opt-in installed NVIDIA SDK regression. Synthetic video/audio; no devices or Live.

Uses the isolated process and authenticated wire helpers from the transition
probe. SDK paths remain subject to the runtime's secure loader. Output paths
are explicit; no SDK binaries or models are bundled by this probe.
"""
import argparse
import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
import wave

import numpy as np
from PIL import Image
import websockets

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nvidia_probe_transport", ROOT / "scripts/probe-configurable-transition.py")
transport = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = transport
spec.loader.exec_module(transport)
KINDS = ["nv_greenscreen_filter", "nv_blur_filter", "nv_background_blur_filter", "nvidia_audiofx_filter"]

async def exercise(proc, work, report):
    proc.spawn()
    await asyncio.to_thread(proc.wait_for, transport.process.READY_RE, 45)
    await asyncio.to_thread(proc.wait_for_shutdown_control_ready, 10)
    async with websockets.connect(f"ws://127.0.0.1:{proc.port}", max_size=8*1024*1024) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion": 1, "eventSubscriptions": 65536}
        if "authentication" in hello:
            auth = hello["authentication"]
            secret = base64.b64encode(hashlib.sha256((proc.password + auth["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        await ws.send(json.dumps({"op": 1, "d": identify}))
        assert json.loads(await ws.recv())["op"] == 2
        wire = transport.Wire(ws)
        caps = await wire.vendor("GetCapabilities", vendor="pulsar")
        report["capabilities"] = caps
        inventory = {row["value"] for row in caps["capabilities"]["filters"]["values"]}
        assert set(KINDS) <= inventory, inventory
        nv = caps["capabilities"]["nv_filters"]
        assert nv["afx"]["usable"] and nv["vfx"]["usable"], nv
        await wire.call("CreateScene", {"sceneName": "NvidiaSDKRegression"})
        # Fine stripes make a spatial blur measurable; a non-person fixture
        # makes the segmentation alpha measurable without biometric imagery.
        pixels = np.full((1080, 1920, 3), 20, np.uint8)
        pixels[:, ::8] = 240
        pixels[:, 1::8] = 240
        Image.fromarray(pixels).save(work / "stripes.png")
        await wire.call("CreateInput", {"sceneName": "NvidiaSDKRegression", "inputName": "Stripes", "inputKind": "image_source", "inputSettings": {"file": str(work / "stripes.png")}, "sceneItemEnabled": True})
        await wire.call("SetCurrentProgramScene", {"sceneName": "NvidiaSDKRegression"})
        async def image():
            data = await wire.call("GetSourceScreenshot", {"sourceName": "Stripes", "imageFormat": "png", "imageWidth": 480, "imageHeight": 270})
            return np.asarray(Image.open(io.BytesIO(base64.b64decode(data["imageData"].split(",", 1)[1]))).convert("RGBA"))
        await asyncio.sleep(1)
        baseline = await image()
        report["video"] = []
        for kind in KINDS[:3]:
            settings = {"mode": 1, "threshold": .8, "processing_interval": 1} if kind == KINDS[0] else {"intensity": .8}
            await wire.call("CreateSourceFilter", {"sourceName": "Stripes", "filterName": "sdk/test", "filterKind": kind, "filterSettings": settings})
            actual = await wire.call("GetSourceFilter", {"sourceName": "Stripes", "filterName": "sdk/test"})
            assert actual["filterKind"] == kind
            await asyncio.sleep(3)
            before = await wire.call("GetStats")
            samples = []
            for _ in range(5):
                await asyncio.sleep(1)
                samples.append(await wire.call("GetStats"))
            treated = await image()
            row = {"kind": kind, "settings": actual["filterSettings"], "pixelDifferenceMean": float(np.abs(treated.astype(float)-baseline).mean()), "alphaRange": [int(treated[:,:,3].min()), int(treated[:,:,3].max())], "frameDelta": samples[-1]["renderTotalFrames"]-before["renderTotalFrames"], "skippedDelta": samples[-1]["renderSkippedFrames"]-before["renderSkippedFrames"], "meanRenderMs": sum(s["averageFrameRenderTime"] for s in samples)/len(samples)}
            report["video"].append(row)
            assert row["pixelDifferenceMean"] > 1, row
            assert row["frameDelta"] >= 250 and row["skippedDelta"] == 0, row
            await wire.call("SetSourceFilterEnabled", {"sourceName": "Stripes", "filterName": "sdk/test", "filterEnabled": False})
            await asyncio.sleep(.5)
            assert np.array_equal(await image(), baseline), kind
            await wire.call("RemoveSourceFilter", {"sourceName": "Stripes", "filterName": "sdk/test"})
        rng = np.random.default_rng(20261010)
        noise = (rng.normal(0, .08, 48000*12).clip(-1, 1)*32767).astype("<i2")
        with wave.open(str(work / "noise.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(48000)
            audio.writeframes(noise.tobytes())
        await wire.call("CreateInput", {"sceneName": "NvidiaSDKRegression", "inputName": "Noise", "inputKind": "ffmpeg_source", "inputSettings": {"is_local_file": True, "local_file": str(work / "noise.wav"), "looping": True}, "sceneItemEnabled": True})
        await wire.call("SetInputAudioMonitorType", {"inputName": "Noise", "monitorType": "OBS_MONITORING_TYPE_NONE"})
        async def levels(seconds):
            start = len(wire.events)
            deadline = time.monotonic()+seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(.15)
                await wire.call("GetStats")
            values = [channel[0] for event in wire.events[start:] if event["eventType"] == "InputVolumeMeters" for inp in event["eventData"]["inputs"] if inp["inputName"] == "Noise" for channel in inp["inputLevelsMul"]]
            assert values, "No native volume meter events"
            return sum(values)/len(values)
        await asyncio.sleep(1)
        original = await levels(2)
        report["audio"] = {"fixture": "seeded white noise, raw native volume-meter magnitude", "baseline": original, "methods": []}
        for method in ["denoiser", "dereverb", "dereverb_denoiser"]:
            await wire.call("CreateSourceFilter", {"sourceName": "Noise", "filterName": "sdk/audio", "filterKind": KINDS[3], "filterSettings": {"method": method, "intensity": 1.0, "vad": False}})
            await asyncio.sleep(3)
            magnitude = await levels(2)
            report["audio"]["methods"].append({"method": method, "magnitude": magnitude})
            if method in ("denoiser", "dereverb_denoiser"):
                assert magnitude < original*.5, (method, original, magnitude)
            await wire.call("RemoveSourceFilter", {"sourceName": "Noise", "filterName": "sdk/audio"})
        await wire.call("RemoveInput", {"inputName": "Noise"})
        assert (await wire.call("GetSourceFilterList", {"sourceName": "Stripes"}))["filters"] == []
        report["checks"] = ["SDK usable/version", "four filters registered", "three VFX pixel changes and exact bypass", "native render timings", "three AFX model methods", "noise suppression signal", "filter removal"]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ["exe", "work", "report", "afx", "vfx"]:
        parser.add_argument("--"+key, type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["NVAFX_SDK_DIR"] = str(args.afx.resolve())
    os.environ["NV_VIDEO_EFFECTS_PATH"] = str(args.vfx.resolve())
    os.environ["PULSAR_LEGACY_ALIAS"] = "disabled"
    os.environ["PULSAR_DIRECTSHOW_LEGACY_ALIAS"] = "0"
    proc = transport.process.PulsarProcess(args.exe.resolve(), "nvenc", work)
    report = {"started": time.time(), "mode": "isolated installed NVIDIA SDK synthetic regression", "exe": str(args.exe.resolve()), "exeSha256": hashlib.sha256(args.exe.read_bytes()).hexdigest(), "sdkPaths": {"afx": str(args.afx.resolve()), "vfx": str(args.vfx.resolve())}}
    try:
        asyncio.run(exercise(proc, work, report))
    except Exception as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        proc.shutdown()
        report["shutdown"] = {"exitCode": proc.proc.poll() if proc.proc else None, "forced": proc.forced_kill_used}
        # Store SDK diagnostics, never authentication/stream keys from raw logs.
        report["sdkLogs"] = [transport.process.SENSITIVE_LOG_VALUE_RE.sub(r"\1\2<redacted>", line) for line in proc.snapshot() if "NVIDIA" in line or "nvvfx" in line or "nvafx" in line]
        report["sdkDiagnostics"] = [line for line in report["sdkLogs"] if " ERROR " in line or " WARN " in line]
        report["qualification"] = "synthetic functional/performance regression; SDK diagnostics require separate review"
        report["passed"] = "failure" not in report and report["shutdown"] == {"exitCode": 0, "forced": False}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2)+"\n", encoding="utf8", newline="\n")
        print(json.dumps({key: value for key, value in report.items() if key not in ["capabilities", "sdkLogs"]}))
    return 0 if report["passed"] else 1

if __name__ == "__main__":
    sys.exit(main())
