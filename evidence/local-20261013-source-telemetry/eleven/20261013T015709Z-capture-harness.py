import importlib.util, json, os, pathlib, secrets, subprocess, sys

root = pathlib.Path.cwd()
spec = importlib.util.spec_from_file_location("capture_bundle_runtime", root / "scripts/probe-dual-lane.py")
runtime = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runtime
spec.loader.exec_module(runtime)
work = root / ".cache/packaged-capture-proof"
work.mkdir(parents=True, exist_ok=True)
identity = "capture-bundle-" + secrets.token_hex(6)
runtime_dir = work / "runtimes" / identity
os.environ.update({"PULSAR_RUNTIME_ROOT": str(work / "runtimes"), "PULSAR_RUNTIME_DIR": str(runtime_dir), "PULSAR_LEGACY_ALIAS":"disabled", "PULSAR_DIRECTSHOW_LEGACY_ALIAS":"0"})
process = runtime.PulsarProcess(root / "dist/pulsar-windows-x64-full-v3.0.1/bin/64bit/pulsar.exe", "x264", work / "recordings", runtime_id=identity)
report = {"private_runtime": True, "passed": False}
try:
    process.spawn()
    process.wait_for_shutdown_control_ready(timeout=60)
    process.wait_for(runtime.READY_RE, timeout=60)
    config = runtime_dir / "obs-websocket/config.json"
    probe = "D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20261009-capture-effects/scripts/probe-capture-effects.py"
    child = subprocess.run([sys.executable, probe, "--config", str(config), "--output", str(work / "gpu")], capture_output=True, text=True, timeout=120)
    (work / "probe.log").write_text(child.stdout + child.stderr, encoding="utf-8")
    report["capture_probe_exit"] = child.returncode
    if child.returncode:
        raise RuntimeError("Capture pixel probe failed; inspect private probe.log")
except Exception as error:
    report["error"] = str(error)
finally:
    try:
        process.shutdown()
        process.assert_shutdown_clean(require_runtime_lease=True)
        report["graceful_shutdown"] = True
    except Exception as error:
        report["shutdown_error"] = str(error)
    report["exit_code"] = process.proc.returncode if process.proc else None
    report["forced_kill"] = process.forced_kill_used
    report["passed"] = report.get("capture_probe_exit") == 0 and report.get("graceful_shutdown", False) and not report["forced_kill"]
    (work / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
raise SystemExit(0 if report["passed"] else 1)
