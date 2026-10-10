#!/usr/bin/env python3
"""Native AR regression using an explicit --portrait fixture, never a camera.

The supplied portrait is resized/translated for tracking tests. Proof separates
same-frame pixels, lack-of-face fallback, live intensity and native render time.
"""
import argparse
import asyncio
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import subprocess
import time

import numpy as np
from PIL import Image
import websockets

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installed_ar_probe", ROOT / "scripts/probe-nvidia-effects.py")
base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base
spec.loader.exec_module(base)
portrait_arg = argparse.ArgumentParser(add_help=False)
portrait_arg.add_argument("--portrait", type=Path, required=True)
portrait_options, remaining = portrait_arg.parse_known_args()
sys.argv = [sys.argv[0], *remaining]

async def exercise(proc, work, report, record_video=False):
    report["fixture"] = {"path": str(portrait_options.portrait.resolve()), "sha256": hashlib.sha256(portrait_options.portrait.read_bytes()).hexdigest()}
    portrait = Image.open(portrait_options.portrait).convert("RGB")
    for name, x in [("left", 120), ("right", 1000)]:
        canvas = Image.new("RGB", (1920, 1080), (32, 32, 32))
        canvas.paste(portrait.resize((800, 450)), (x, 310))
        canvas.save(work / (name+".png"))
    # A spatial pattern makes lingering crop observable; a uniform image cannot.
    y, x = np.mgrid[:1080, :1920]
    pattern = np.stack((x // 8 % 256, y // 5 % 256, (x // 16 + y // 16) % 256), axis=2).astype(np.uint8)
    Image.fromarray(pattern).save(work / "no-face.png")
    portrait.resize((1920, 1080)).save(work / "eyes.png")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "testsrc2=size=1920x1080:rate=30", "-t", "3", "-an", "-c:v",
         "libx264", "-preset", "ultrafast", "-crf", "24", "-y",
         str(work / "background-video.mp4")],
        check=True,
    )
    proc.spawn()
    await asyncio.to_thread(proc.wait_for, base.transport.process.READY_RE, 45)
    await asyncio.to_thread(proc.wait_for_shutdown_control_ready, 10)
    async with websockets.connect(f"ws://127.0.0.1:{proc.port}", max_size=24*1024*1024) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion": 1, "eventSubscriptions": 0}
        if "authentication" in hello:
            auth = hello["authentication"]
            secret = base64.b64encode(hashlib.sha256((proc.password+auth["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(hashlib.sha256((secret+auth["challenge"]).encode()).digest()).decode()
        await ws.send(json.dumps({"op": 1, "d": identify}))
        assert json.loads(await ws.recv())["op"] == 2
        wire = base.transport.Wire(ws)
        for item in (await wire.call("GetInputList"))["inputs"]:
            if item["inputKind"].startswith("wasapi_"):
                await wire.call("SetInputMute", {"inputName": item["inputName"], "inputMuted": True})
        caps = await wire.vendor("GetCapabilities", vendor="pulsar")
        nv = caps["capabilities"]["nv_filters"]
        report["nativeSdkCapability"] = nv
        assert nv["same_frame_comparison"] is True
        assert nv["ar"]["usable"] is True and nv["ar"]["version"] == "0.8.7.0", nv
        inventory = {row["value"] for row in caps["capabilities"]["filters"]["values"]}
        assert {"nv_autoframe_filter", "nv_eye_contact_filter"} <= inventory, inventory
        report["gpuBefore"] = await wire.vendor("GetNvidiaGpuUsage", vendor="pulsar")
        assert report["gpuBefore"]["available"] is True and report["gpuBefore"]["scope"] == "whole-device", report["gpuBefore"]
        await wire.call("CreateScene", {"sceneName": "ArParity"})
        await wire.call("CreateInput", {"sceneName": "ArParity", "inputName": "Portrait", "inputKind": "image_source", "inputSettings": {"file": str(work / "left.png")}, "sceneItemEnabled": True})
        await wire.call("CreateScene", {"sceneName": "IdleParity"})
        await wire.call("SetCurrentProgramScene", {"sceneName": "IdleParity"})
        async def metrics():
            return await wire.vendor("GetNativeEffectMetrics", {"sourceName": "Portrait", "filterName": "idle"}, vendor="pulsar")
        await wire.call("CreateSourceFilter", {"sourceName": "Portrait", "filterName": "idle", "filterKind": "nv_denoise_filter", "filterSettings": {"intensity": 1., "mode": 1}})
        await asyncio.sleep(2)
        cold = await metrics()
        assert cold["available"] and not cold["imagesAllocated"] and cold["processedFrames"] == 0, cold
        await wire.call("SetCurrentProgramScene", {"sceneName": "ArParity"})
        await asyncio.sleep(3)
        active = await metrics()
        assert active["imagesAllocated"] and active["processedFrames"] >= 60 and not active["processingStopped"], active
        await wire.call("SetCurrentProgramScene", {"sceneName": "IdleParity"})
        await asyncio.sleep(1)
        idle_start = await metrics()
        await asyncio.sleep(2)
        idle_end = await metrics()
        assert idle_end["processedFrames"] == idle_start["processedFrames"], (idle_start, idle_end)
        await wire.call("SetCurrentProgramScene", {"sceneName": "ArParity"})
        await asyncio.sleep(2)
        resumed = await metrics()
        assert resumed["processedFrames"] > idle_end["processedFrames"] + 60, resumed
        report["demandProcessing"] = {"cold": cold, "active": active, "idleStart": idle_start, "idleEnd": idle_end, "resumed": resumed}
        await wire.call("RemoveSourceFilter", {"sourceName": "Portrait", "filterName": "idle"})
        async def image(source="Portrait"):
            data = await wire.call("GetSourceScreenshot", {"sourceName": source, "imageFormat": "png", "imageWidth": 960, "imageHeight": 540})
            return np.asarray(Image.open(io.BytesIO(base64.b64decode(data["imageData"].split(",", 1)[1]))).convert("RGBA"))
        recording_started = time.monotonic()
        if record_video:
            await wire.call("CreateInput", {"sceneName": "ArParity", "inputName": "Label", "inputKind": "text_gdiplus_v3", "inputSettings": {"text": "NVIDIA AR / qualification native\nFixture officielle NVIDIA / aucune camera", "font": {"face": "Segoe UI", "size": 36, "flags": 0}, "color": 0xffffff, "bk_color": 0x151515, "bk_opacity": 95}, "sceneItemEnabled": True})
            await wire.call("StartRecord")
            recording_started = time.monotonic()
            await asyncio.sleep(2)
        async def caption(text):
            if record_video:
                await wire.call("SetInputSettings", {"inputName": "Label", "inputSettings": {"text": text}, "overlay": True})
                report.setdefault("timeline", []).append({"seconds": time.monotonic()-recording_started, "text": text})
        await wire.call("CreateSourceFilter", {"sourceName": "Portrait", "filterName": "ar", "filterKind": "nv_autoframe_filter", "filterSettings": {"intensity": 1., "zoom": 2.5, "smoothing": .15}})
        report["tracking"] = []
        for position in ["left", "right"]:
            await wire.call("SetSourceFilterSettings", {"sourceName": "Portrait", "filterName": "ar", "filterSettings": {"intensity": 0.}, "overlay": True})
            await wire.call("SetInputSettings", {"inputName": "Portrait", "inputSettings": {"file": str(work / (position+".png"))}, "overlay": True})
            await asyncio.sleep(.3)
            original = await image()
            await wire.call("SetSourceFilterSettings", {"sourceName": "Portrait", "filterName": "ar", "filterSettings": {"intensity": 1.}, "overlay": True})
            await caption(f"NVIDIA Auto Frame / sujet a {position}\nSuivi natif du visage / zoom 2.5 maximum")
            await asyncio.sleep(3)
            processed = await image()
            difference = float(np.abs(processed.astype(float)-original).mean())
            report["tracking"].append({"position": position, "difference": difference})
            assert difference > 2, report["tracking"][-1]
            Image.fromarray(processed).save(work / (position+"-processed.png"))
        await wire.call("SetSourceFilterSettings", {"sourceName": "Portrait", "filterName": "ar", "filterSettings": {"intensity": 0.}, "overlay": True})
        await wire.call("SetInputSettings", {"inputName": "Portrait", "inputSettings": {"file": str(work / "no-face.png")}, "overlay": True})
        await asyncio.sleep(.3)
        no_face_original = await image()
        await wire.call("SetSourceFilterSettings", {"sourceName": "Portrait", "filterName": "ar", "filterSettings": {"intensity": 1.}, "overlay": True})
        await caption("NVIDIA Auto Frame / aucun visage\nRetour progressif au cadrage d'origine")
        await asyncio.sleep(.1)
        initial_crop = float(np.abs((await image()).astype(float)-no_face_original).mean())
        await asyncio.sleep(4)
        fallback_difference = float(np.abs((await image()).astype(float)-no_face_original).mean())
        report["noFaceFallback"] = {"initialCropDifference": initial_crop, "finalDifference": fallback_difference}
        assert initial_crop > 2 and fallback_difference < .1, report["noFaceFallback"]
        await wire.call("RemoveSourceFilter", {"sourceName": "Portrait", "filterName": "ar"})
        await wire.call("SetInputSettings", {"inputName": "Portrait", "inputSettings": {"file": str(work / "eyes.png")}, "overlay": True})
        await asyncio.sleep(.3)
        original = await image()
        await wire.call("CreateSourceFilter", {"sourceName": "Portrait", "filterName": "ar", "filterKind": "nv_eye_contact_filter", "filterSettings": {"intensity": 1., "compare": True}})
        await caption("NVIDIA Eye Contact / comparaison meme image\nGAUCHE : avant / DROITE : correction du regard")
        await asyncio.sleep(3)
        compared = await image()
        left = float(np.abs(compared[:, :470].astype(float)-original[:, :470]).mean())
        right = float(np.abs(compared[:, 490:].astype(float)-original[:, 490:]).mean())
        report["eyeContact"] = {"leftDifference": left, "rightDifference": right}
        assert left < .1 and right > .05, report["eyeContact"]
        Image.fromarray(compared).save(work / "eye-contact-compared.png")
        stats = await wire.call("GetStats")
        await asyncio.sleep(5)
        after = await wire.call("GetStats")
        report["eyeContact"]["render"] = {"frames": after["renderTotalFrames"]-stats["renderTotalFrames"], "skipped": after["renderSkippedFrames"]-stats["renderSkippedFrames"], "meanMs": after["averageFrameRenderTime"]}
        assert report["eyeContact"]["render"]["frames"] >= 270 and report["eyeContact"]["render"]["skipped"] == 0
        report["gpuWithEyeContact"] = await wire.vendor("GetNvidiaGpuUsage", vendor="pulsar")
        await wire.call("SetSourceFilterSettings", {"sourceName": "Portrait", "filterName": "ar", "filterSettings": {"intensity": 0.}, "overlay": True})
        await caption("NVIDIA Eye Contact / intensite zero\nBypass exact de la correction")
        await asyncio.sleep(1)
        assert np.array_equal(original, await image())
        await wire.call("SetInputSettings", {"inputName": "Portrait", "inputSettings": {"file": str(work / "left.png")}, "overlay": True})
        await asyncio.sleep(.3)
        stack_original = await image()
        await wire.call("SetSourceFilterSettings", {"sourceName": "Portrait", "filterName": "ar", "filterSettings": {"intensity": .5, "compare": False}, "overlay": True})
        await wire.call("CreateSourceFilter", {"sourceName": "Portrait", "filterName": "frame", "filterKind": "nv_autoframe_filter", "filterSettings": {"intensity": 1., "zoom": 2., "smoothing": .15}})
        await caption("NVIDIA / effets AR simultanes\nAuto Frame + Eye Contact 50 %")
        await asyncio.sleep(3)
        stats = await wire.call("GetStats")
        await asyncio.sleep(5)
        after = await wire.call("GetStats")
        report["stackedAr"] = {"frames": after["renderTotalFrames"]-stats["renderTotalFrames"], "skipped": after["renderSkippedFrames"]-stats["renderSkippedFrames"], "meanMs": after["averageFrameRenderTime"], "difference": float(np.abs((await image()).astype(float)-stack_original).mean())}
        assert report["stackedAr"]["frames"] >= 270 and report["stackedAr"]["skipped"] == 0 and report["stackedAr"]["difference"] > 2, report["stackedAr"]
        await wire.call("RemoveSourceFilter", {"sourceName": "Portrait", "filterName": "frame"})
        await wire.call("RemoveSourceFilter", {"sourceName": "Portrait", "filterName": "ar"})
        assert (await wire.call("GetSourceFilterList", {"sourceName": "Portrait"}))["filters"] == []
        await wire.call("SetInputSettings", {"inputName": "Portrait", "inputSettings": {"file": str(work / "eyes.png")}, "overlay": True})
        await wire.call("CreateSourceFilter", {"sourceName": "Portrait", "filterName": "segment", "filterKind": "nv_greenscreen_filter", "filterSettings": {"mode": 0, "threshold": .5, "processing_interval": 1}})
        await caption("NVIDIA / remplacement d'arriere-plan\nDetourage natif / fond image sous la personne")
        Image.new("RGB", (1920, 1080), (48, 110, 190)).save(work / "background-blue.png")
        Image.new("RGB", (1920, 1080), (190, 110, 48)).save(work / "background-red.png")
        created = await wire.call("CreateInput", {"sceneName": "ArParity", "inputName": "Background", "inputKind": "image_source", "inputSettings": {"file": str(work / "background-blue.png")}, "sceneItemEnabled": True})
        await wire.call("SetSceneItemIndex", {"sceneName": "ArParity", "sceneItemId": created["sceneItemId"], "sceneItemIndex": 0})
        await asyncio.sleep(3)
        segmented = await image()
        assert segmented[:,:,3].min() == 0 and segmented[:,:,3].max() == 255, "Portrait must contain both transparent background and opaque subject"
        blue = await image("ArParity")
        await wire.call("SetInputSettings", {"inputName": "Background", "inputSettings": {"file": str(work / "background-red.png")}, "overlay": True})
        await asyncio.sleep(2)
        red = await image("ArParity")
        opaque = segmented[:,:,3] == 255
        transparent = segmented[:,:,3] == 0
        # Ignore the caption area when comparing the composited Program pixels.
        opaque[:80] = False; transparent[:80] = False
        report["backgroundComposition"] = {"opaquePixels": int(opaque.sum()), "transparentPixels": int(transparent.sum()), "subjectDifference": float(np.abs(blue.astype(float)-red)[opaque].mean()), "backgroundDifference": float(np.abs(blue.astype(float)-red)[transparent].mean())}
        assert report["backgroundComposition"]["subjectDifference"] < .1 and report["backgroundComposition"]["backgroundDifference"] > 10, report["backgroundComposition"]
        Image.fromarray(red).save(work / "background-replacement.png")
        await wire.call("RemoveInput", {"inputName": "Background"})
        created = await wire.call("CreateInput", {"sceneName": "ArParity", "inputName": "Background", "inputKind": "ffmpeg_source", "inputSettings": {"local_file": str(work / "background-video.mp4"), "is_local_file": True, "looping": True}, "sceneItemEnabled": True})
        await wire.call("SetSceneItemIndex", {"sceneName": "ArParity", "sceneItemId": created["sceneItemId"], "sceneItemIndex": 0})
        await wire.call("SetInputMute", {"inputName": "Background", "inputMuted": True})
        await caption("NVIDIA / remplacement d'arriere-plan\nFond VIDEO en boucle / personne detouree native")
        await asyncio.sleep(1)
        first = await image("ArParity")
        await asyncio.sleep(4.3)
        second = await image("ArParity")
        report["videoBackground"] = {"media": await wire.call("GetMediaInputStatus", {"inputName": "Background"}), "backgroundDifference": float(np.abs(first.astype(float)-second)[transparent].mean()), "subjectDifference": float(np.abs(first.astype(float)-second)[opaque].mean())}
        assert report["videoBackground"]["media"]["mediaState"] == "OBS_MEDIA_STATE_PLAYING" and report["videoBackground"]["backgroundDifference"] > 1 and report["videoBackground"]["subjectDifference"] < .1, report["videoBackground"]
        await wire.call("RemoveSourceFilter", {"sourceName": "Portrait", "filterName": "segment"})
        await wire.call("RemoveInput", {"inputName": "Background"})
        if record_video:
            await asyncio.sleep(2)
            report["nativeRecording"] = (await wire.call("StopRecord"))["outputPath"]
        report["checks"] = ["cold allocation and processing only on render demand", "idle processing stops and resumes", "actual AR registration", "face-follow and native zoom at two positions", "eye redirection same-frame comparison", "stacked AR effects", "segmented portrait over authored background in native Program", "native render windows", "exact bypass", "cleanup and graceful shutdown"]

base.exercise = exercise
if __name__ == "__main__":
    sys.exit(base.main())
