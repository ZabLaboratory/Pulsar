"""Rebuild admission proof: private runtime, existing synthetic capture probes.

No live/recording/device capture. Reuses the existing process owner and probes.
The external runtime supplies dependencies; this does not qualify a full package.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import pathlib
import secrets
import shutil
import uuid
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args):
    output = args.output.resolve()
    evidence_root = ROOT / "evidence" / "local-20261015-capture-paint-integration" / "eleven"
    if output != evidence_root:
        raise ValueError("Explicit output must be this unit's canonical evidence directory")
    cache = (ROOT / ".cache").resolve()
    if not cache.is_relative_to(ROOT):
        raise ValueError("Cache resolves outside the assigned worktree")
    scratch = cache / ("capture-paint-proof-" + uuid.uuid4().hex)
    if scratch.exists() or not scratch.resolve().is_relative_to(cache):
        raise ValueError("Private scratch must be new and inside the owned cache")
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = output / (stamp + "-native-admission.json")
    if report_path.exists():
        raise ValueError("Refusing to overwrite existing proof")
    process = None
    env_before = {key: value for key, value in os.environ.items() if key.startswith("PULSAR_")}
    report = {"startedUtc": datetime.now(timezone.utc).isoformat(), "passed": False}
    try:
        source = args.runtime.resolve()
        runtime = scratch / "runtime"
        for directory in ("bin", "data", "obs-plugins"):
            shutil.copytree(source / directory, runtime / directory)
        source_exe = source / "bin" / "64bit" / "pulsar.exe"
        target_exe = runtime / "bin" / "64bit" / "pulsar.exe"
        target_plugin = runtime / "obs-plugins" / "64bit" / "pulsar-capture-effects.dll"
        report["provenance"] = {
            "runtimeSource": str(source), "runtimeExeSha256": digest(source_exe),
            "runtimeObsSha256": digest(source / "bin" / "64bit" / "obs.dll"),
            "rebuiltPlugin": str(args.plugin.resolve()),
            "rebuiltPluginSha256": digest(args.plugin),
            "sourceCppSha256": digest(ROOT / "plugins/pulsar-capture-effects/src/capture-effects.cpp"),
            "sourceShaderSha256": digest(ROOT / "plugins/pulsar-capture-effects/src/capture-effects.effect"),
        }
        shutil.copyfile(args.plugin, target_plugin)
        assert digest(target_plugin) == digest(args.plugin)
        assert digest(target_exe) == digest(source_exe)
        for key in list(os.environ):
            if key.startswith("PULSAR_"):
                del os.environ[key]
        os.environ.update({
            "PULSAR_RUNTIME_ROOT": str(scratch / "state"),
            "PULSAR_RUNTIME_DIR": str(scratch / "state" / "runtime"),
            "PULSAR_RUNTIME_INSTANCE_ID": "paint-" + uuid.uuid4().hex,
        })
        protocol = load("private_paint_process", "probe-websocket.py")
        port = protocol.pick_free_port()
        password = secrets.token_urlsafe(32)
        process = protocol.PulsarProcess(target_exe, port, password)
        process.spawn()
        report["ownedPid"] = process.proc.pid
        process.wait_ready(45)
        config = {"server_port": port, "server_password": password}
        effects = load("private_paint_effects", "probe-capture-effects.py")
        report["effects"] = asyncio.run(effects.run(config, output))
        ancestors = load("private_paint_ancestors", "probe-capture-ancestor-clips.py")
        asyncio.run(ancestors.run(config, output))
        report["passed"] = True
    except Exception as error:
        report["errorType"] = type(error).__name__
        report["error"] = str(error)[:1500]
        if process is not None:
            report["redactedBootTail"] = process._diag()
        raise
    finally:
        if process is not None:
            process.shutdown()
            report["ownedProcessExited"] = process.proc.poll() is not None
        else:
            report["ownedProcessExited"] = True
        for key in list(os.environ):
            if key.startswith("PULSAR_"):
                del os.environ[key]
        os.environ.update(env_before)
        if scratch.exists() and report["ownedProcessExited"]:
            if not scratch.resolve().is_relative_to(cache):
                raise ValueError("Scratch cleanup escaped owned cache")
            shutil.rmtree(scratch)
        report["scratchRemoved"] = not scratch.exists()
        report["finishedUtc"] = datetime.now(timezone.utc).isoformat()
        report["limits"] = "Synthetic GPU fixtures only; external runtime dependencies; no full package, physical capture, arbitrary group mask, live, recording or transition proof"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(report_path), "passed": report["passed"],
                          "ownedProcessExited": report["ownedProcessExited"],
                          "scratchRemoved": report["scratchRemoved"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=pathlib.Path, required=True)
    parser.add_argument("--plugin", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    run(parser.parse_args())
