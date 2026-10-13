"""Read telemetry, stop polling, and prove the read-owned profiler expires."""
import asyncio
import argparse
import importlib.util
import json
import math
import os
import pathlib
import secrets
import sys

spec = importlib.util.spec_from_file_location(
    "source_kinds", pathlib.Path(__file__).with_name("probe-source-kinds.py")
)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


async def probe(url, password, browser_url=None):
    async with transport.websockets.connect(url, subprotocols=["obswebsocket.json"]) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion": hello["rpcVersion"], "eventSubscriptions": 0}
        if "authentication" in hello:
            auth = hello["authentication"]
            identify["authentication"] = transport.compute_auth(password, auth["salt"], auth["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify}))
        await ws.recv()
        inbox = transport.Inbox()
        seq = 0

        async def call(name, data=None):
            nonlocal seq
            seq += 1
            result = await transport.request(inbox, ws, name, str(seq), data or {})
            if not result["requestStatus"]["result"]:
                raise RuntimeError(result["requestStatus"])
            return result.get("responseData", {})

        previous = (await call("GetCurrentProgramScene"))["sceneName"]
        version = await call("GetVersion")
        if "GetSourceStats" not in version["availableRequests"]:
            raise RuntimeError("GetSourceStats is not advertised")
        scene = "SourceTelemetryLeaseProof"
        source = "SourceTelemetryLeaseColor"
        kinds = (await call("GetInputKindList"))["inputKinds"]
        kind = next(k for k in kinds if k.startswith("color_source"))
        await call("CreateScene", {"sceneName": scene})
        try:
            await call("CreateInput", {
                "sceneName": scene, "inputName": source, "inputKind": kind,
                "inputSettings": {"color": 0xffcc8844, "width": 640, "height": 360},
                "sceneItemEnabled": True,
            })
            await call("SetCurrentProgramScene", {"sceneName": scene})
            browser = "SourceTelemetryLeaseBrowser"
            if browser_url:
                if "browser_source" not in kinds:
                    raise RuntimeError("Full bundle browser_source missing")
                await call("CreateInput", {"sceneName": scene, "inputName": browser, "inputKind": "browser_source", "inputSettings": {"url": browser_url, "width": 640, "height": 360, "fps_custom": True, "fps": 60, "shutdown": False, "reroute_audio": False}, "sceneItemEnabled": True})
            samples = []
            browser_samples = []
            for _ in range(4):
                samples.append(await call("GetSourceStats", {"sourceName": source}))
                if browser_url:
                    browser_samples.append(await call("GetSourceStats", {"sourceName": browser}))
                await asyncio.sleep(1)
            if not any(sample["available"] and sample["cpuMs"] is not None for sample in samples):
                raise RuntimeError("No CPU source sample while polling")
            if browser_url and not any(sample["available"] and sample["cpuMs"] is not None for sample in browser_samples):
                raise RuntimeError("No real browser source sample while polling")
            for sample in samples + browser_samples:
                if sample.get("ramScope") != "core-allocations-lower-bound" or not isinstance(sample.get("ramBytes"), int) or sample["ramBytes"] <= 0:
                    raise RuntimeError("Missing source-local core RAM measurement")
                if not isinstance(sample.get("frameBudgetMs"), (int, float)) or sample["frameBudgetMs"] <= 0:
                    raise RuntimeError("Missing video frame budget")
                for field in ("cpuMs", "gpuMs"):
                    value = sample.get(field)
                    if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                        raise RuntimeError(f"Invalid native timing: {field}")
                if "cpuMaxMs" in sample or "gpuMaxMs" in sample:
                    raise RuntimeError("Misleading combined maximum remains")
                for field in ("cpuTickMaxMs", "cpuRenderFirstPassMaxMs", "gpuRenderFirstPassMaxMs"):
                    if field not in sample:
                        raise RuntimeError(f"Missing component maximum: {field}")
            await asyncio.sleep(7)
            renewed = await call("GetSourceStats", {"sourceName": source})
            if renewed["available"]:
                raise RuntimeError("Read-owned profiler did not expire after polling stopped")
            stream, recording = await call("GetStreamStatus"), await call("GetRecordStatus")
            if stream["outputActive"] or recording["outputActive"]:
                raise RuntimeError("Unexpected output active in telemetry probe")
            print("Source telemetry: samples available during polling; expired after idle; component maxima explicit")
            return {"samples": samples, "browserSamples": browser_samples, "afterIdle": renewed, "streamActive": False, "recordingActive": False}
        finally:
            await call("SetCurrentProgramScene", {"sceneName": previous})
            await call("RemoveScene", {"sceneName": scene})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--exe", type=pathlib.Path, help="Spawn and reap a private runtime")
    mode.add_argument("--config", type=pathlib.Path, help="Existing disposable runtime configuration")
    parser.add_argument("--work-dir", type=pathlib.Path)
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    if not args.exe:
        config = json.loads((args.config or transport.CONFIG_PATH).read_text(encoding="utf-8"))
        result = asyncio.run(probe(f"ws://127.0.0.1:{config['server_port']}", config["server_password"]))
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0
    if not args.work_dir or not args.report:
        parser.error("--exe requires explicit --work-dir and --report")
    spec = importlib.util.spec_from_file_location("telemetry_process_probe", pathlib.Path(__file__).with_name("probe-dual-lane.py"))
    runtime = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runtime
    spec.loader.exec_module(runtime)
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    page = work / "telemetry.html"
    page.write_text('<!doctype html><html><body style="margin:0;background:#28465a"><div id="p" style="width:100px;height:100px;background:#ebc34b"></div><script>let n=0;function frame(){document.getElementById("p").style.transform="translateX("+(n++%300)+"px)";requestAnimationFrame(frame)}frame()</script></body></html>', encoding="utf-8")
    identity = f"source-telemetry-{secrets.token_hex(6)}"
    os.environ.update({"PULSAR_RUNTIME_ROOT": str(work / "runtimes"), "PULSAR_RUNTIME_DIR": str(work / "runtimes" / identity), "PULSAR_LEGACY_ALIAS": "disabled", "PULSAR_DIRECTSHOW_LEGACY_ALIAS": "0"})
    process = runtime.PulsarProcess(args.exe.resolve(), "x264", work / "recordings", runtime_id=identity)
    report = {"schema": "pulsar.source-telemetry-proof.v1", "privateRuntime": True, "passed": False}
    try:
        process.spawn()
        process.wait_for_shutdown_control_ready(timeout=60)
        ready = process.wait_for(runtime.READY_RE, timeout=60)
        report.update(asyncio.run(probe(ready.group(1), process.password, page.as_uri())))
    except Exception as error:
        report["error"] = str(error)
    finally:
        try:
            process.shutdown()
            process.assert_shutdown_clean(require_runtime_lease=True)
            report["gracefulShutdown"] = True
        except Exception as error:
            report["shutdownError"] = str(error)
        report["exitCode"] = process.proc.returncode if process.proc else None
        report["forcedKill"] = process.forced_kill_used
        report["passed"] = "error" not in report and report.get("gracefulShutdown", False) and not report["forcedKill"]
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
