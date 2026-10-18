#!/usr/bin/env python3
"""Native vignette pixels and lifecycle. Synthetic inputs, no camera/live/recording."""
import argparse
import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path
from PIL import Image
import websockets

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("camera_effects_transport", ROOT / "scripts/probe-configurable-transition.py")
transport = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = transport
spec.loader.exec_module(transport)

async def exercise(proc, report):
    proc.spawn()
    await asyncio.to_thread(proc.wait_for, transport.process.READY_RE, 45)
    await asyncio.to_thread(proc.wait_for_shutdown_control_ready, 10)
    async with websockets.connect(f"ws://127.0.0.1:{proc.port}", max_size=8*1024*1024) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion": 1, "eventSubscriptions": 0}
        if "authentication" in hello:
            auth = hello["authentication"]
            secret = base64.b64encode(hashlib.sha256((proc.password + auth["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        await ws.send(json.dumps({"op": 1, "d": identify}))
        assert json.loads(await ws.recv())["op"] == 2
        wire = transport.Wire(ws)
        capabilities = await wire.vendor("GetCapabilities", vendor="pulsar")
        report["capabilities"] = capabilities
        kinds = (await wire.call("GetInputKindList"))["inputKinds"]
        # Filter registration is checked by actual creation, never inferred from DLL presence.
        report["inputInventoryCount"] = len(kinds)
        await wire.call("CreateScene", {"sceneName": "CameraEffectsRegression"})
        await wire.call("CreateInput", {"sceneName": "CameraEffectsRegression", "inputName": "WhiteFixture", "inputKind": "color_source_v3", "inputSettings": {"color": 0xffffffff, "width": 128, "height": 72}, "sceneItemEnabled": True})
        await wire.call("SetCurrentProgramScene", {"sceneName": "CameraEffectsRegression"})
        async def image():
            result = await wire.call("GetSourceScreenshot", {"sourceName": "WhiteFixture", "imageFormat": "png", "imageWidth": 128, "imageHeight": 72})
            return Image.open(io.BytesIO(base64.b64decode(result["imageData"].split(",", 1)[1]))).convert("RGBA")
        async def frame_matching(predicate):
            deadline = time.monotonic() + 2
            while True:
                frame = await image()
                if predicate(frame):
                    return frame
                if time.monotonic() >= deadline:
                    raise AssertionError("Native frame did not reach the requested effect state")
                await asyncio.sleep(.05)
        baseline = await image()
        report["baseline"] = {"center": list(baseline.getpixel((64, 36))), "corner": list(baseline.getpixel((0, 0)))}
        await wire.call("CreateSourceFilter", {"sourceName": "WhiteFixture", "filterName": "audit/vignette", "filterKind": "pulsar_vignette_filter", "filterSettings": {"amount": .8, "radius": .65, "softness": .5}})
        actual = await wire.call("GetSourceFilter", {"sourceName": "WhiteFixture", "filterName": "audit/vignette"})
        assert actual["filterKind"] == "pulsar_vignette_filter"
        assert actual["filterSettings"]["amount"] == .8
        vignette = await frame_matching(lambda frame: frame.getpixel((0, 0))[0] < 100)
        center, corner = vignette.getpixel((64, 36)), vignette.getpixel((0, 0))
        assert center[0] >= 250 and 40 <= corner[0] <= 65, (center, corner)
        assert vignette.getchannel("A").getextrema() == (255, 255)
        report["vignette"] = {"center": list(center), "corner": list(corner)}
        # Semi-transparent inputs retain alpha, not only fully opaque/keyed pixels.
        await wire.call("SetInputSettings", {"inputName": "WhiteFixture", "inputSettings": {"color": 0x80ffffff}, "overlay": True})
        translucent = await frame_matching(lambda frame: frame.getpixel((64, 36))[3] == 128)
        assert translucent.getchannel("A").getextrema() == (128, 128)
        report["translucentAlpha"] = list(translucent.getchannel("A").getextrema())
        await wire.call("SetInputSettings", {"inputName": "WhiteFixture", "inputSettings": {"color": 0xffffffff}, "overlay": True})
        await wire.call("SetSourceFilterEnabled", {"sourceName": "WhiteFixture", "filterName": "audit/vignette", "filterEnabled": False})
        await frame_matching(lambda frame: frame.tobytes() == baseline.tobytes())
        await wire.call("SetSourceFilterEnabled", {"sourceName": "WhiteFixture", "filterName": "audit/vignette", "filterEnabled": True})
        await wire.call("SetSourceFilterSettings", {"sourceName": "WhiteFixture", "filterName": "audit/vignette", "filterSettings": {"amount": 0}, "overlay": True})
        await frame_matching(lambda frame: frame.tobytes() == baseline.tobytes())
        # Stack and alpha: chroma fully keys the green center. Vignette cannot restore alpha.
        await wire.call("SetInputSettings", {"inputName": "WhiteFixture", "inputSettings": {"color": 0xff00ff00}, "overlay": True})
        await wire.call("CreateSourceFilter", {"sourceName": "WhiteFixture", "filterName": "audit/key", "filterKind": "chroma_key_filter_v2", "filterSettings": {"key_color_type": "green", "similarity": 400}})
        await wire.call("SetSourceFilterSettings", {"sourceName": "WhiteFixture", "filterName": "audit/vignette", "filterSettings": {"amount": .8}, "overlay": True})
        keyed = await frame_matching(lambda frame: frame.getpixel((64, 36))[3] == 0)
        assert keyed.getpixel((64, 36))[3] == 0
        report["keyedCenter"] = list(keyed.getpixel((64, 36)))
        await wire.call("RemoveSourceFilter", {"sourceName": "WhiteFixture", "filterName": "audit/key"})
        await wire.call("SetInputSettings", {"inputName": "WhiteFixture", "inputSettings": {"color": 0xffffffff, "width": 1920, "height": 1080}, "overlay": True})
        async def measure(enabled):
            await wire.call("SetSourceFilterEnabled", {"sourceName": "WhiteFixture", "filterName": "audit/vignette", "filterEnabled": enabled})
            await asyncio.sleep(2)
            before = await wire.call("GetStats")
            samples = []
            for _ in range(5):
                await asyncio.sleep(1)
                samples.append(await wire.call("GetStats"))
            after = samples[-1]
            return {"before": before, "samples": samples,
                "frameDelta": after["renderTotalFrames"] - before["renderTotalFrames"],
                "skippedDelta": after["renderSkippedFrames"] - before["renderSkippedFrames"],
                "meanRenderMs": sum(row["averageFrameRenderTime"] for row in samples) / len(samples)}
        report["performance"] = {"fixture": "1920x1080 white native source; concurrent dev GPU load",
            "bypass": await measure(False), "vignette": await measure(True)}
        for result in report["performance"]["bypass"], report["performance"]["vignette"]:
            assert result["frameDelta"] >= 200, result
            assert result["skippedDelta"] == 0, result
        for name in ["audit/vignette"]:
            await wire.call("RemoveSourceFilter", {"sourceName": "WhiteFixture", "filterName": name})
        assert (await wire.call("GetSourceFilterList", {"sourceName": "WhiteFixture"}))["filters"] == []
        report["checks"] = ["registration", "pixel falloff", "opaque alpha", "partial alpha", "native bypass", "zero intensity", "live update", "filter stack", "transparent center", "1080p realtime", "removal"]
        report["stats"] = await wire.call("GetStats")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    os.environ["PULSAR_DIRECTSHOW_LEGACY_ALIAS"] = "0"
    os.environ["PULSAR_LEGACY_ALIAS"] = "disabled"
    proc = transport.process.PulsarProcess(args.exe.resolve(), "nvenc", args.work.resolve())
    report = {"started": time.time(), "mode": "isolated synthetic camera-effect regression"}
    try:
        asyncio.run(exercise(proc, report))
    except Exception as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        proc.shutdown()
        report["shutdown"] = {"exitCode": proc.proc.poll(), "forced": proc.forced_kill_used}
        report["stagingErrors"] = sum("Failed to create staging surface" in line for line in proc.snapshot())
        report["passed"] = "failure" not in report and report["shutdown"] == {"exitCode": 0, "forced": False} and report["stagingErrors"] == 0
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8", newline="\n")
        print(json.dumps(report))
    return 0 if report["passed"] else 1

if __name__ == "__main__":
    sys.exit(main())
