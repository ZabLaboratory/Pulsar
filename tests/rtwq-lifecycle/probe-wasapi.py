"""Exercise real WASAPI sources in an isolated full Pulsar runtime, without output."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import time


ROOT = Path(__file__).resolve().parents[2]


async def exercise(probe, process, url: str) -> int:
    import websockets

    async with websockets.connect(url) as ws:
        await probe.identify(ws, process.password)
        inbox = probe.Inbox()
        counter = 0

        async def call(kind: str, data=None):
            nonlocal counter
            counter += 1
            reply = await probe.request(inbox, ws, kind, f"rtwq-{counter}", data)
            if not reply["requestStatus"]["result"]:
                raise RuntimeError(f"{kind}: {reply['requestStatus']}")
            return reply.get("responseData", {})

        await call("CreateScene", {"sceneName": "RtwqProbe"})
        for index in range(6):
            name = f"RtwqProbeOutput-{index}"
            await call("CreateInput", {
                "sceneName": "RtwqProbe", "inputName": name,
                "inputKind": "wasapi_output_capture",
                "inputSettings": {"device_id": "default"},
                "sceneItemEnabled": False,
            })
            deadline = time.monotonic() + 10
            while not any(f"initialized (source: {name})" in line for line in process.snapshot()):
                if time.monotonic() > deadline:
                    raise RuntimeError(f"WASAPI source did not initialize: {name}")
                await asyncio.sleep(0.05)
            await call("RemoveInput", {"inputName": name})
            # Exercise sources created after earlier queues have been released.
            await asyncio.sleep(0.15)
        await call("RemoveScene", {"sceneName": "RtwqProbe"})
        stream = await call("GetStreamStatus")
        recording = await call("GetRecordStatus")
        if stream["outputActive"] or recording["outputActive"]:
            raise RuntimeError("Unexpected output active in isolated probe")
        return 6


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("Windows-only WASAPI integration probe")
    spec = importlib.util.spec_from_file_location("rtwq_native_probe", ROOT / "scripts/probe-dual-lane.py")
    probe = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = probe
    spec.loader.exec_module(probe)

    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    runtime = f"rtwq-probe-{secrets.token_hex(6)}"
    os.environ.update({
        "PULSAR_RUNTIME_ROOT": str(work / "runtimes"),
        "PULSAR_RUNTIME_DIR": str(work / "runtimes" / runtime),
        "PULSAR_LEGACY_ALIAS": "disabled",
        "PULSAR_DIRECTSHOW_LEGACY_ALIAS": "0",
    })
    process = probe.PulsarProcess(args.exe.resolve(), "x264", work / "recordings", runtime_id=runtime)
    report = {"runtime": runtime, "sourcesInitialized": 0, "passed": False}
    try:
        process.spawn()
        process.wait_for_shutdown_control_ready(timeout=45)
        ready = process.wait_for(probe.READY_RE, timeout=45)
        assert any("RTWQ platform started" in line for line in process.snapshot()), "Missing RTWQ startup"
        report["sourcesInitialized"] = asyncio.run(exercise(probe, process, ready.group(1)))
    except Exception as error:
        report["error"] = str(error)
    finally:
        try:
            process.shutdown()
            process.assert_shutdown_clean(require_runtime_lease=True)
            report["gracefulShutdown"] = True
        except Exception as error:
            report["shutdownError"] = str(error)
        lines = process.snapshot()
        report["rtwqErrors"] = [line for line in lines if "RTWQ" in line and ("failed" in line or "ERROR" in line)]
        report["exitCode"] = process.proc.returncode if process.proc else None
        report["forcedKill"] = process.forced_kill_used
        report["passed"] = (
            report["sourcesInitialized"] == 6
            and report.get("gracefulShutdown", False)
            and not report["rtwqErrors"]
            and not report["forcedKill"]
            and "error" not in report
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
