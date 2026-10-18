#!/usr/bin/env python3
"""Isolated screenshot/filter regression. Synthetic inputs only; no recording."""
import argparse, asyncio, base64, hashlib, importlib.util, json, os, sys
from pathlib import Path
import websockets

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("screenshot_transport", ROOT / "scripts/probe-configurable-transition.py")
transport = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = transport
spec.loader.exec_module(transport)

async def exercise(proc, report):
    proc.spawn()
    await asyncio.to_thread(proc.wait_for, transport.process.READY_RE, 45)
    await asyncio.to_thread(proc.wait_for_shutdown_control_ready, 10)
    async with websockets.connect(f"ws://127.0.0.1:{proc.port}", max_size=32*1024*1024) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion": 1, "eventSubscriptions": 0x7ff}
        if "authentication" in hello:
            auth = hello["authentication"]
            secret = base64.b64encode(hashlib.sha256((proc.password + auth["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        await ws.send(json.dumps({"op":1,"d":identify}))
        assert json.loads(await ws.recv())["op"] == 2
        wire = transport.Wire(ws)
        for item in (await wire.call("GetInputList"))["inputs"]:
            if item["inputKind"].startswith("wasapi"):
                await wire.call("SetInputMute", {"inputName":item["inputName"], "inputMuted":True})
        await wire.call("CreateScene", {"sceneName":"ScreenshotRegression"})
        await wire.call("CreateInput", {"sceneName":"ScreenshotRegression", "inputName":"NoVideo", "inputKind":"ffmpeg_source", "inputSettings":{}, "sceneItemEnabled":False})
        await wire.call("CreateInput", {"sceneName":"ScreenshotRegression", "inputName":"SyntheticColor", "inputKind":"color_source_v3", "inputSettings":{"color":0xffff00ff,"width":320,"height":180}, "sceneItemEnabled":True})
        modes = [{}, {"imageWidth":240}, {"imageHeight":135}, {"imageWidth":240,"imageHeight":135}]
        for dimensions in modes:
            status = await wire.call("GetSourceScreenshot", {"sourceName":"NoVideo", "imageFormat":"png", **dimensions}, error=True)
            assert status["code"] == 702, status
            valid = await wire.call("GetSourceScreenshot", {"sourceName":"SyntheticColor", "imageFormat":"png", **dimensions})
            assert valid["imageData"].startswith("data:image/png;base64,")
        report["screenshot_dimension_modes"] = len(modes)
        for logical in ["color_filter", "chroma_key_filter"]:
            kind = logical + "_v2"
            defaults = await wire.call("GetSourceFilterDefaultSettings", {"filterKind":kind})
            assert defaults["defaultFilterSettings"]["opacity"] == 1
            name = "prism/" + logical
            await wire.call("CreateSourceFilter", {"sourceName":"SyntheticColor", "filterName":name, "filterKind":kind, "filterSettings":{"opacity":0.5}})
            await wire.call("SetSourceFilterSettings", {"sourceName":"SyntheticColor", "filterName":name, "filterSettings":{"opacity":0.75}, "overlay":True})
            actual = await wire.call("GetSourceFilter", {"sourceName":"SyntheticColor", "filterName":name})
            assert actual["filterKind"] == kind and actual["filterSettings"]["opacity"] == 0.75
            await wire.call("RemoveSourceFilter", {"sourceName":"SyntheticColor", "filterName":name})
        report["modern_filter_lifecycle"] = True
        await asyncio.sleep(.2)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["PULSAR_DIRECTSHOW_LEGACY_ALIAS"] = "0"
    os.environ["PULSAR_LEGACY_ALIAS"] = "disabled"
    proc = transport.process.PulsarProcess(args.exe.resolve(), "nvenc", args.output.resolve())
    report = {"passed":False, "websocket_sha256":hashlib.sha256((args.exe.resolve().parents[2]/"obs-plugins/64bit/obs-websocket.dll").read_bytes()).hexdigest()}
    try:
        asyncio.run(exercise(proc, report))
    except Exception as error:
        report["error_type"] = type(error).__name__
        raise
    finally:
        proc.shutdown()
        lines = proc.snapshot()
        report["staging_errors"] = sum("Failed to create staging surface" in line for line in lines)
        report["shutdown"] = {"exit_code":proc.proc.poll(), "forced":proc.forced_kill_used}
        report["passed"] = "error_type" not in report and report["staging_errors"] == 0 and report["shutdown"] == {"exit_code":0,"forced":False}
        (args.output/"result.json").write_text(json.dumps(report,indent=2),encoding="utf8")
    print(json.dumps(report))
    sys.exit(0 if report["passed"] else 1)
