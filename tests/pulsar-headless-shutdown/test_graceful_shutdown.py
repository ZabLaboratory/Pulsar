#!/usr/bin/env python3
"""Windows integration proof for pipe-backed Pulsar graceful shutdown.

The test deliberately uses the same ``PulsarProcess`` shape as the canary:
stdout is a pipe, the child is a ``/SUBSYSTEM:WINDOWS`` executable, and the
shutdown request travels through an anonymous inherited event. A forced kill
or a missing release marker is always a failure. CTest invokes this script
only on Windows after the real ``pulsar-headless`` target has been built.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = ROOT / "scripts" / "probe-dual-lane.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "pulsar_shutdown_integration_probe", PROBE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load probe module: {PROBE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _raise_with_sanitized_tail(probe, process, message: str) -> None:
    tail = probe.failure_tail(process.snapshot(), 40)
    raise RuntimeError(f"{message}\nSanitized Pulsar log tail:\n{tail}")


def main() -> int:
    if os.name != "nt":
        print("SKIP: graceful-shutdown integration proof is Windows-only")
        return 0
    executable = Path(
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PULSAR_HEADLESS_EXE", "")
    )
    if not executable.is_file():
        raise RuntimeError(f"Pulsar executable not found: {executable}")

    probe = _load_probe()
    runtime_id = f"shutdown-probe-{os.getpid()}-{secrets.token_hex(4)}"
    previous_runtime_id = os.environ.get("PULSAR_RUNTIME_INSTANCE_ID")
    os.environ["PULSAR_RUNTIME_INSTANCE_ID"] = runtime_id
    record_dir = Path(tempfile.mkdtemp(prefix="pulsar-shutdown-record-"))
    process = probe.PulsarProcess(
        executable.resolve(),
        "x264",
        record_dir,
        trace_path=None,
        runtime_id=runtime_id,
    )
    started = False
    cleanup_error: Exception | None = None
    try:
        process.spawn()
        started = True
        # The child-side ACK must precede acceptance of PULSAR_READY.
        process.wait_for_shutdown_control_ready(timeout=60)
        process.wait_for(probe.READY_RE, timeout=60)
    finally:
        try:
            process.shutdown()
        except Exception as exc:  # preserve the primary startup error
            cleanup_error = exc
        try:
            if started and cleanup_error is None:
                process.assert_shutdown_clean(require_runtime_lease=True)
        except Exception as exc:
            cleanup_error = cleanup_error or exc
        finally:
            if previous_runtime_id is None:
                os.environ.pop("PULSAR_RUNTIME_INSTANCE_ID", None)
            else:
                os.environ["PULSAR_RUNTIME_INSTANCE_ID"] = previous_runtime_id
            try:
                shutil.rmtree(record_dir)
            except Exception as exc:
                cleanup_error = cleanup_error or exc

    if cleanup_error is not None:
        _raise_with_sanitized_tail(probe, process, str(cleanup_error))
    if not started:
        raise RuntimeError("Pulsar process never spawned")
    lines = process.snapshot()
    required = (
        "PULSAR_SHUTDOWN_CONTROL event=ready",
        "PULSAR_SHUTDOWN_CONTROL event=signaled",
        "PULSAR_RUNTIME_INSTANCE runtime_dir_lease=released",
        "PULSAR_RUNTIME_INSTANCE lease=released",
        "[pulsar-headless] shutting down",
    )
    for marker in required:
        if not any(marker in line for line in lines):
            _raise_with_sanitized_tail(
                probe, process, f"missing graceful-shutdown marker: {marker}"
            )
    control_lines = [line for line in lines if line.startswith("PULSAR_SHUTDOWN_CONTROL")]
    if any(
        "PULSAR_SHUTDOWN_EVENT_HANDLE" in line or "handle" in line.lower()
        for line in control_lines
    ):
        _raise_with_sanitized_tail(probe, process, "shutdown handle or its value was logged")
    if process.forced_kill_used:
        _raise_with_sanitized_tail(probe, process, "forced process kill was used")
    if process.proc is None or process.proc.returncode != 0:
        status = process.proc.returncode if process.proc is not None else None
        _raise_with_sanitized_tail(probe, process, f"Pulsar exited unsuccessfully: {status}")
    print("PASS: pipe-backed Windows graceful shutdown released runtime leases without forced kill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
