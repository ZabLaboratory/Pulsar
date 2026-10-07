"""Read telemetry, stop polling, and prove the read-owned profiler expires."""
import asyncio
import importlib.util
import json
import pathlib

spec = importlib.util.spec_from_file_location(
    "source_kinds", pathlib.Path(__file__).with_name("probe-source-kinds.py")
)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


async def probe(url, password):
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
            samples = []
            for _ in range(4):
                samples.append(await call("GetSourceStats", {"sourceName": source}))
                await asyncio.sleep(1)
            if not any(sample["available"] and sample["cpuMs"] is not None for sample in samples):
                raise RuntimeError("No CPU source sample while polling")
            for sample in samples:
                if "cpuMaxMs" in sample or "gpuMaxMs" in sample:
                    raise RuntimeError("Misleading combined maximum remains")
                for field in ("cpuTickMaxMs", "cpuRenderFirstPassMaxMs", "gpuRenderFirstPassMaxMs"):
                    if field not in sample:
                        raise RuntimeError(f"Missing component maximum: {field}")
            await asyncio.sleep(7)
            renewed = await call("GetSourceStats", {"sourceName": source})
            if renewed["available"]:
                raise RuntimeError("Read-owned profiler did not expire after polling stopped")
            print("Source telemetry: samples available during polling; expired after idle; component maxima explicit")
        finally:
            await call("SetCurrentProgramScene", {"sceneName": previous})
            await call("RemoveScene", {"sceneName": scene})


if __name__ == "__main__":
    config = json.loads(transport.CONFIG_PATH.read_text(encoding="utf-8"))
    asyncio.run(probe(f"ws://127.0.0.1:{config['server_port']}", config["server_password"]))
