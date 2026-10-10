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

async def exercise(proc, work, report, record_video=False):
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
        report["mutedDeviceInputs"] = []
        for item in (await wire.call("GetInputList"))["inputs"]:
            if item["inputKind"].startswith("wasapi_"):
                await wire.call("SetInputMute", {"inputName": item["inputName"], "inputMuted": True})
                report["mutedDeviceInputs"].append(item["inputName"])
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
        async def image(source="Stripes"):
            data = await wire.call("GetSourceScreenshot", {"sourceName": source, "imageFormat": "png", "imageWidth": 480, "imageHeight": 270})
            return np.asarray(Image.open(io.BytesIO(base64.b64decode(data["imageData"].split(",", 1)[1]))).convert("RGBA"))
        await asyncio.sleep(1)
        baseline = await image()
        baseline_program = await image("NvidiaSDKRegression")
        async def caption(text):
            if not record_video:
                return
            await wire.call("SetInputSettings", {"inputName": "QualificationLabel", "inputSettings": {"text": text}, "overlay": True})
            report.setdefault("videoTimeline", []).append({"seconds": time.monotonic()-recording_started, "text": text})
        if record_video:
            await wire.call("CreateInput", {"sceneName": "NvidiaSDKRegression", "inputName": "QualificationLabel", "inputKind": "text_gdiplus_v3", "inputSettings": {"text": "NVIDIA / Pulsar - qualification native\nFixture synthetique / aucune camera ni microphone", "font": {"face": "Segoe UI", "size": 36, "flags": 0}, "color": 0xffffff, "bk_color": 0x151515, "bk_opacity": 95, "outline": True}, "sceneItemEnabled": True})
            await wire.call("StartRecord")
            recording_started = time.monotonic()
            # Labels are part of the native recording, but the pixel oracle
            # below remains the unchanged source, independent of those labels.
            baseline_program = await image("NvidiaSDKRegression")
        report["video"] = []
        for kind in KINDS[:3]:
            await caption("NVIDIA / Pulsar - sortie native 1080p60\n"+kind+" - traitement actif (fixture synthetique)")
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
            # Exclude the optional native caption from the Program oracle.
            row["programScenePixelDifferenceMean"] = float(np.abs((await image("NvidiaSDKRegression"))[40:].astype(float)-baseline_program[40:]).mean())
            report["video"].append(row)
            assert row["pixelDifferenceMean"] > 1, row
            assert row["programScenePixelDifferenceMean"] > 1, row
            assert row["frameDelta"] >= 250 and row["skippedDelta"] == 0, row
            if kind != KINDS[0]:
                row["liveIntensity"] = []
                for intensity in [0.0, 0.2, 0.8, 0.0, 0.8]:
                    await wire.call("SetSourceFilterSettings", {"sourceName": "Stripes", "filterName": "sdk/test", "filterSettings": {"intensity": intensity}, "overlay": True})
                    await asyncio.sleep(.6)
                    current = await image()
                    difference = float(np.abs(current.astype(float)-baseline).mean())
                    row["liveIntensity"].append({"intensity": intensity, "pixelDifferenceMean": difference})
                    assert difference == 0 if intensity == 0 else difference > 1, row
            await wire.call("SetSourceFilterEnabled", {"sourceName": "Stripes", "filterName": "sdk/test", "filterEnabled": False})
            await asyncio.sleep(.5)
            assert np.array_equal(await image(), baseline), kind
            await wire.call("RemoveSourceFilter", {"sourceName": "Stripes", "filterName": "sdk/test"})
        # Reuse one processed source across scene switches; no physical device
        # or inactive-scene cache is involved in this isolated native check.
        await wire.call("CreateScene", {"sceneName": "NvidiaSDKRegressionB"})
        await wire.call("CreateSceneItem", {"sceneName": "NvidiaSDKRegressionB", "sourceName": "Stripes", "sceneItemEnabled": True})
        if record_video:
            await wire.call("CreateSceneItem", {"sceneName": "NvidiaSDKRegressionB", "sourceName": "QualificationLabel", "sceneItemEnabled": True})
        for kind in KINDS[1:3]:
            await wire.call("CreateSourceFilter", {"sourceName": "Stripes", "filterName": "sdk/"+kind, "filterKind": kind, "filterSettings": {"intensity": .4}})
        await asyncio.sleep(2)
        before = await wire.call("GetStats")
        report["stackedSceneSwitches"] = []
        for index in range(20):
            scene = "NvidiaSDKRegressionB" if index % 2 == 0 else "NvidiaSDKRegression"
            await wire.call("SetCurrentProgramScene", {"sceneName": scene})
            await caption(f"NVIDIA / Pulsar - 2 effets video simultanes\nSwitch natif {index+1}/20 : {scene}")
            await asyncio.sleep(.2)
            assert (await wire.call("GetCurrentProgramScene"))["currentProgramSceneName"] == scene
            report["stackedSceneSwitches"].append({"scene": scene, "pixelDifferenceMean": float(np.abs((await image()).astype(float)-baseline).mean())})
            assert report["stackedSceneSwitches"][-1]["pixelDifferenceMean"] > 1
        after = await wire.call("GetStats")
        report["stackedStats"] = {"frameDelta": after["renderTotalFrames"]-before["renderTotalFrames"], "skippedDelta": after["renderSkippedFrames"]-before["renderSkippedFrames"], "lastRenderMs": after["averageFrameRenderTime"]}
        assert report["stackedStats"]["frameDelta"] >= 240 and report["stackedStats"]["skippedDelta"] == 0
        for kind in KINDS[1:3]:
            await wire.call("RemoveSourceFilter", {"sourceName": "Stripes", "filterName": "sdk/"+kind})
        await wire.call("SetCurrentProgramScene", {"sceneName": "NvidiaSDKRegression"})
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
        await caption(f"NVIDIA / Pulsar - audio synthetique 48 kHz\nSignal original : magnitude native {original:.5f}")
        report["audio"] = {"fixture": "seeded white noise, raw native volume-meter magnitude", "baseline": original, "methods": []}
        live_intensity_failures = []
        for method in ["denoiser", "dereverb", "dereverb_denoiser"]:
            await caption(f"NVIDIA AFX / {method}\nIntensite 100 % / VAD OFF - bruit synthetique, aucune voix")
            await wire.call("CreateSourceFilter", {"sourceName": "Noise", "filterName": "sdk/audio", "filterKind": KINDS[3], "filterSettings": {"method": method, "intensity": 1.0, "vad": False}})
            await asyncio.sleep(3)
            magnitude = await levels(2)
            report["audio"]["methods"].append({"method": method, "magnitude": magnitude})
            if method in ("denoiser", "dereverb_denoiser"):
                assert magnitude < original*.5, (method, original, magnitude)
            intermediate = []
            for intensity in [.75, .25]:
                await caption(f"NVIDIA AFX / {method} - demande {intensity:.0%}\nChargement asynchrone puis mesure native du signal")
                await wire.call("SetSourceFilterSettings", {"sourceName": "Noise", "filterName": "sdk/audio", "filterSettings": {"intensity": intensity}, "overlay": True})
                await asyncio.sleep(1)
                intermediate.append({"intensity": intensity, "magnitude": await levels(2)})
                await caption(f"NVIDIA AFX / {method} - intensite {intensity:.0%}\nMagnitude native : {intermediate[-1]['magnitude']:.5f} / original : {original:.5f}")
            report["audio"]["methods"][-1]["intermediateIntensities"] = intermediate
            intermediate_passed = intermediate[1]["magnitude"] > intermediate[0]["magnitude"]*1.2
            report["audio"]["methods"][-1]["intermediateIntensityPassed"] = intermediate_passed
            if not intermediate_passed:
                live_intensity_failures.append(method+"/intermediate")
            await caption(f"NVIDIA AFX / {method} - demande 0 %\nBypass PCM / mesure native en cours")
            await wire.call("SetSourceFilterSettings", {"sourceName": "Noise", "filterName": "sdk/audio", "filterSettings": {"intensity": 0.0}, "overlay": True})
            await asyncio.sleep(1)
            bypass_magnitude = await levels(2)
            await caption(f"NVIDIA AFX / {method} - intensite 0 %\nBypass PCM : {bypass_magnitude:.5f} / original : {original:.5f}")
            report["audio"]["methods"][-1]["liveIntensityZeroMagnitude"] = bypass_magnitude
            # A contemporaneous disabled-filter control distinguishes an SDK
            # slider failure from a stopped fixture or source-loop gap.
            await caption(f"NVIDIA AFX / {method}\nFiltre desactive : controle du signal original")
            await wire.call("SetSourceFilterEnabled", {"sourceName": "Noise", "filterName": "sdk/audio", "filterEnabled": False})
            await asyncio.sleep(.5)
            disabled = await levels(2)
            report["audio"]["methods"][-1]["disabledMagnitude"] = disabled
            assert disabled > original*.8, ("fixture no longer running", original, disabled)
            await caption(f"NVIDIA AFX / {method} - reappliquer 100 %\nChargement asynchrone puis mesure native du signal")
            await wire.call("SetSourceFilterSettings", {"sourceName": "Noise", "filterName": "sdk/audio", "filterSettings": {"intensity": 1.0}, "overlay": True})
            await wire.call("SetSourceFilterEnabled", {"sourceName": "Noise", "filterName": "sdk/audio", "filterEnabled": True})
            await asyncio.sleep(1)
            reapplied = await levels(2)
            report["audio"]["methods"][-1]["liveIntensityOneMagnitude"] = reapplied
            if method in ("denoiser", "dereverb_denoiser"):
                assert reapplied < original*.5, (method, original, reapplied)
            intensity_passed = bypass_magnitude > disabled*.8
            report["audio"]["methods"][-1]["liveIntensityPassed"] = intensity_passed
            if not intensity_passed:
                live_intensity_failures.append(method)
            await wire.call("RemoveSourceFilter", {"sourceName": "Noise", "filterName": "sdk/audio"})
            print(f"AFX {method}: intermediate={intermediate_passed}, zero={intensity_passed}", flush=True)
        # Rapid settings changes, real VAD reload, and removal while loading.
        # Every request has a bounded wire timeout. This is also a regression
        # for the former join-under-loader-mutex destruction deadlock.
        await caption("NVIDIA AFX - test de cycle de vie\n72 reglages rapides et 6 retraits pendant chargement")
        report["audioLifecycle"] = []
        for cycle in range(6):
            await wire.call("CreateSourceFilter", {"sourceName": "Noise", "filterName": "sdk/stress", "filterKind": KINDS[3], "filterSettings": {"method": "denoiser", "intensity": 1.0, "vad": False}})
            durations = []
            for index in range(12):
                started = time.perf_counter()
                await wire.call("SetSourceFilterSettings", {"sourceName": "Noise", "filterName": "sdk/stress", "filterSettings": {"method": ["denoiser", "dereverb", "dereverb_denoiser"][index % 3], "intensity": .25 if index % 2 else .75, "vad": bool(index % 2)}, "overlay": True})
                durations.append(time.perf_counter()-started)
            # Allow native deferred settings/worker execution before detaching.
            await asyncio.sleep(.05)
            started = time.perf_counter()
            await wire.call("RemoveSourceFilter", {"sourceName": "Noise", "filterName": "sdk/stress"})
            row = {"cycle": cycle, "updates": len(durations), "maxUpdateMs": max(durations)*1000, "removeMs": (time.perf_counter()-started)*1000}
            report["audioLifecycle"].append(row)
            assert row["maxUpdateMs"] < 1000 and row["removeMs"] < 5000, row
        # Two active filters must not tear down each other's SDK logger/state.
        await caption("NVIDIA AFX - deux filtres audio\nRetrait du premier / le second doit rester operationnel")
        for name in ["sdk/first", "sdk/second"]:
            await wire.call("CreateSourceFilter", {"sourceName": "Noise", "filterName": name, "filterKind": KINDS[3], "filterSettings": {"method": "denoiser", "intensity": .5, "vad": False}})
        await asyncio.sleep(4)
        await wire.call("RemoveSourceFilter", {"sourceName": "Noise", "filterName": "sdk/first"})
        await asyncio.sleep(2)
        surviving = await levels(2)
        assert surviving < original*.8, surviving
        report["audioSurvivingFilterMagnitude"] = surviving
        await caption("NVIDIA AFX - VAD et continuite\nIntensite 25 % / VAD ON puis OFF / qualite voix non evaluee")
        await wire.call("SetSourceFilterSettings", {"sourceName": "Noise", "filterName": "sdk/second", "filterSettings": {"intensity": .25, "vad": True}, "overlay": True})
        await asyncio.sleep(2)
        vad_on = await levels(2)
        vad_on_settings = (await wire.call("GetSourceFilter", {"sourceName": "Noise", "filterName": "sdk/second"}))["filterSettings"]
        await wire.call("SetSourceFilterSettings", {"sourceName": "Noise", "filterName": "sdk/second", "filterSettings": {"vad": False}, "overlay": True})
        await asyncio.sleep(2)
        vad_off = await levels(2)
        vad_off_settings = (await wire.call("GetSourceFilter", {"sourceName": "Noise", "filterName": "sdk/second"}))["filterSettings"]
        report["audioLiveVad"] = {"enabledMagnitude": vad_on, "disabledMagnitude": vad_off, "enabledSettings": vad_on_settings, "disabledSettings": vad_off_settings, "scope": "live reload/readback and signal continuity; white noise cannot qualify voice detection accuracy"}
        assert vad_on_settings["vad"] is True and vad_off_settings["vad"] is False
        # At .25 the SDK retains a dry component even with VAD; requiring
        # silence would incorrectly test full suppression at a partial ratio.
        assert vad_on > original*.5 and vad_off > original*.5, report["audioLiveVad"]
        await caption("NVIDIA AFX - bypass a 0 % avec VAD ON\nLe signal original doit continuer")
        await wire.call("SetSourceFilterSettings", {"sourceName": "Noise", "filterName": "sdk/second", "filterSettings": {"intensity": 0., "vad": True}, "overlay": True})
        await asyncio.sleep(1)
        report["audioLiveVad"]["zeroWithVadMagnitude"] = await levels(2)
        assert report["audioLiveVad"]["zeroWithVadMagnitude"] > original*.8
        await wire.call("RemoveSourceFilter", {"sourceName": "Noise", "filterName": "sdk/second"})
        await caption("NVIDIA / Pulsar - fin de qualification\n72 mises a jour rapides / retrait pendant chargement / deux filtres audio")
        if record_video:
            await asyncio.sleep(2)
            report["nativeRecording"] = (await wire.call("StopRecord"))["outputPath"]
        await wire.call("RemoveInput", {"inputName": "Noise"})
        assert (await wire.call("GetSourceFilterList", {"sourceName": "Stripes"}))["filters"] == []
        report["checks"] = ["SDK usable/version", "four filters registered", "three VFX source and Program scene pixel changes and exact bypass", "live VFX intensity", "20 scene switches with stacked VFX", "native render timings", "three AFX model methods", "noise suppression signal", "filter removal"]
        report["audio"]["liveIntensityPassed"] = not live_intensity_failures
        assert not live_intensity_failures, ("AFX live intensity contract failed", live_intensity_failures)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ["exe", "work", "report", "afx", "vfx"]:
        parser.add_argument("--"+key, type=Path, required=True)
    parser.add_argument("--record-video", action="store_true", help="Record only this isolated synthetic Program, inside --work")
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["NVAFX_SDK_DIR"] = str(args.afx.resolve())
    os.environ["NV_VIDEO_EFFECTS_PATH"] = str(args.vfx.resolve())
    os.environ["PULSAR_LEGACY_ALIAS"] = "disabled"
    os.environ["PULSAR_DIRECTSHOW_LEGACY_ALIAS"] = "0"
    proc = transport.process.PulsarProcess(args.exe.resolve(), "nvenc", work)
    nv_module = args.exe.resolve().parents[2] / "obs-plugins/64bit/nv-filters.dll"
    report = {"started": time.time(), "mode": "isolated installed NVIDIA SDK synthetic regression", "exe": str(args.exe.resolve()), "exeSha256": hashlib.sha256(args.exe.read_bytes()).hexdigest(), "nvFiltersSha256": hashlib.sha256(nv_module.read_bytes()).hexdigest(), "sdkPaths": {"afx": str(args.afx.resolve()), "vfx": str(args.vfx.resolve())}}
    try:
        asyncio.run(exercise(proc, work, report, args.record_video))
    except Exception as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        proc.shutdown()
        report["shutdown"] = {"exitCode": proc.proc.poll() if proc.proc else None, "forced": proc.forced_kill_used}
        # Store SDK diagnostics, never authentication/stream keys from raw logs.
        report["sdkLogs"] = [transport.process.SENSITIVE_LOG_VALUE_RE.sub(r"\1\2<redacted>", line) for line in proc.snapshot() if "NVIDIA" in line or "nvvfx" in line or "nvafx" in line]
        report["sdkDiagnostics"] = [line for line in report["sdkLogs"] if " ERROR " in line or " WARN " in line]
        report["qualification"] = "installed-SDK synthetic regression; excludes voice/person quality, full-package CI and hardware capacity"
        report["passed"] = "failure" not in report and not report["sdkDiagnostics"] and report["shutdown"] == {"exitCode": 0, "forced": False}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2)+"\n", encoding="utf8", newline="\n")
        print(json.dumps({key: value for key, value in report.items() if key not in ["capabilities", "sdkLogs"]}))
    return 0 if report["passed"] else 1

if __name__ == "__main__":
    sys.exit(main())
