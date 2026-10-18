"""Run the existing capture pixel probe against this freshly packaged runtime."""
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).resolve().parent
BUNDLE = ROOT / "dist/pulsar-windows-x64-full-v3.0.1"
CACHE = ROOT / ".cache/full-runtime-admission/capture-proof"


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    runtime = load("packaged_capture_runtime", "scripts/probe-dual-lane.py")
    capture = load("packaged_capture_probe", "scripts/probe-capture-effects.py")
    CACHE.mkdir(parents=True, exist_ok=True)
    identity = "packaged-capture-" + secrets.token_hex(8)
    os.environ.update({
        "PULSAR_RUNTIME_ROOT": str(CACHE / "runtimes"),
        "PULSAR_RUNTIME_DIR": str(CACHE / "runtimes" / identity),
        "PULSAR_LEGACY_ALIAS": "disabled",
        "PULSAR_DIRECTSHOW_LEGACY_ALIAS": "0",
    })
    process = runtime.PulsarProcess(BUNDLE / "bin/64bit/pulsar.exe", "x264",
                                    CACHE / "recordings", runtime_id=identity)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "startedUtc": datetime.now(timezone.utc).isoformat(), "passed": False,
        "sourceHead": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "privateRuntime": True, "runtimeInstanceId": identity,
        "sourceChanges": "CMake staging, package presence and CI cache gate correction pending signed checkpoint",
        "binaryHashes": {p: hashlib.sha256((BUNDLE / p).read_bytes()).hexdigest() for p in (
            "bin/64bit/pulsar.exe", "bin/64bit/obs.dll",
            "obs-plugins/64bit/obs-websocket.dll", "obs-plugins/64bit/pulsar-capture-effects.dll",
            "obs-plugins/64bit/pulsar-browser.dll", "obs-plugins/64bit/pulsar-browser-page.exe")},
        "scope": "Existing seven-effect fixed-fixture GPU pixel probe, 80 atomic appearance admissions, rotation/framing/reset/collision on the fresh full bundle; no physical capture, public stream, or installed Prism qualification.",
    }
    try:
        process.spawn()
        report["ownedPid"] = process.proc.pid
        process.wait_for_shutdown_control_ready(timeout=60)
        process.wait_for(runtime.READY_RE, timeout=60)
        result = asyncio.run(capture.run({"server_port": process.port,
                                          "server_password": process.password}, OUTPUT))
        report["pixelProof"] = {
            "effects": len(result["effects"]), "atomicAdmissions": len(result["atomicAdmissions"]),
            "rotationAdmissions": len(result["rotationAdmissions"]), "reset": result["reset"],
            "zOrderPreserved": result["zOrderPreserved"],
            "foreignCollisionPreserved": result["foreignCollisionPreserved"],
        }
    except Exception as error:
        report["error"] = runtime.redact_log_line(str(error))
    finally:
        try:
            process.shutdown()
            process.assert_shutdown_clean(require_runtime_lease=True)
            report["gracefulShutdown"] = True
        except Exception as error:
            report["shutdownError"] = runtime.redact_log_line(str(error))
        report["exitCode"] = process.proc.returncode if process.proc else None
        report["forcedKill"] = process.forced_kill_used
        report["passed"] = "error" not in report and report.get("gracefulShutdown", False) and not report["forcedKill"]
        report["endedUtc"] = datetime.now(timezone.utc).isoformat()
        log = "\n".join(runtime.redact_log_line(line).rstrip() for line in process.snapshot()) + "\n"
        (OUTPUT / f"{stamp}-packaged-capture-host.txt").write_bytes(log.encode())
        (OUTPUT / f"{stamp}-packaged-capture-runtime.json").write_bytes((json.dumps(report, indent=2) + "\n").encode())
        print(json.dumps({k: report.get(k) for k in ("passed", "pixelProof", "error", "shutdownError", "exitCode", "forcedKill")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
