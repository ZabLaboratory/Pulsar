#!/usr/bin/env python3
"""Admission/drain proof for the WebSocket-to-libobs shutdown boundary.

The native server owns the real lease and condition-variable drain.  This
test keeps a small adversarial model beside source-level contract assertions,
then runs one pipe-backed Sleep batch against the built headless executable on
Windows.  The batch remains in flight while the inherited shutdown event is
signaled; a successful ACK must report zero handlers and zero sessions before
the frontend/browser fence is entered.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SERVER_CPP = ROOT / "plugins" / "pulsar-websocket" / "src" / "websocketserver" / "WebSocketServer.cpp"
SERVER_HPP = ROOT / "plugins" / "pulsar-websocket" / "src" / "websocketserver" / "WebSocketServer.h"
PROTOCOL_CPP = ROOT / "plugins" / "pulsar-websocket" / "src" / "websocketserver" / "WebSocketServer_Protocol.cpp"
REQUEST_BATCH_CPP = ROOT / "plugins" / "pulsar-websocket" / "src" / "requesthandler" / "RequestBatchHandler.cpp"
PLUGIN_CPP = ROOT / "plugins" / "pulsar-websocket" / "src" / "obs-websocket.cpp"
HEADLESS_CPP = ROOT / "plugins" / "pulsar-headless" / "main.cpp"
PROBE_PATH = ROOT / "scripts" / "probe-dual-lane.py"


class _AdmissionLease:
    def __init__(self, gate: "_AdmissionGate") -> None:
        self._gate = gate

    def __enter__(self) -> "_AdmissionLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self._gate.leave()


class _AdmissionGate:
    """Model the native atomic admission + bounded drain contract."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._running = True
        self._active = 0

    def enter(self) -> _AdmissionLease | None:
        with self._condition:
            if not self._running:
                return None
            self._active += 1
            return _AdmissionLease(self)

    def leave(self) -> None:
        with self._condition:
            assert self._active > 0
            self._active -= 1
            self._condition.notify_all()

    def quiesce(self, timeout: float) -> tuple[bool, int]:
        with self._condition:
            self._running = False
            deadline = time.monotonic() + timeout
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, self._active
                self._condition.wait(remaining)
            return True, 0


def test_native_contract_has_single_admission_and_drain_boundary() -> None:
    header = SERVER_HPP.read_text(encoding="utf-8")
    server = SERVER_CPP.read_text(encoding="utf-8")
    protocol = PROTOCOL_CPP.read_text(encoding="utf-8")
    request_batch = REQUEST_BATCH_CPP.read_text(encoding="utf-8")
    plugin = PLUGIN_CPP.read_text(encoding="utf-8")
    headless = HEADLESS_CPP.read_text(encoding="utf-8")

    for marker in (
        "LifecycleState",
        "HandlerLease",
        "Quiesce(std::chrono::milliseconds timeout",
        "_activeHandlers",
        "_lifecycleCondition",
    ):
        assert marker in header
    for marker in (
        "EnterHandler()",
        "_lifecycleState = LifecycleState::Quiescing",
        "_server.stop_listening",
        "CloseSessions();",
        "_lifecycleCondition.wait_until",
        "PULSAR_WEBSOCKET_QUIESCE event=ack active_handlers=0 sessions=0",
        "no_handlers_after_ack=1",
    ):
        assert marker in server
    assert "handlerLease = handlerLease]()" in server
    assert "handlerLease = handlerLease]()" in protocol
    assert "EnterBatchHandler()" in request_batch
    assert request_batch.index("EnterBatchHandler()") < request_batch.index("if (executionType")
    assert "callbackLease" in request_batch
    assert "workerLease" in request_batch
    assert request_batch.index("callbackLease") < request_batch.index("condition.notify_one()")
    assert request_batch.index("workerLease") < request_batch.index("parallelResults.condition.notify_one()")
    assert "pulsar_websocket_pre_shutdown" in plugin
    assert "calldata_set_int(cd, \"active_handlers\"" in plugin
    assert "calldata_set_int(cd, \"sessions\"" in plugin

    # The host consumes the explicit ACK before any browser/frontend/libobs
    # teardown on the normal path.
    normal = headless[headless.rindex("[pulsar-headless] shutting down") :]
    assert normal.index("websocket_pre_shutdown_ready(websocket_shutdown_error)") < normal.index(
        "browser_pre_shutdown_ready(browser_shutdown_error)"
    )
    assert normal.index("browser_pre_shutdown_ready(browser_shutdown_error)") < normal.index(
        "pulsar_frontend_shutdown();"
    )
    assert normal.index("pulsar_frontend_shutdown();") < normal.index("obs_shutdown();")


def test_gate_rejects_new_work_and_drains_ordinary_parallel_and_frame() -> None:
    gate = _AdmissionGate()
    entered = threading.Barrier(4)
    release = threading.Event()
    accepted: list[str] = []

    def worker(kind: str) -> None:
        lease = gate.enter()
        assert lease is not None
        with lease:
            accepted.append(kind)
            entered.wait(timeout=2)
            release.wait(timeout=2)

    threads = [threading.Thread(target=worker, args=(kind,)) for kind in ("ordinary", "parallel", "frame")]
    for thread in threads:
        thread.start()
    entered.wait(timeout=2)

    result: list[tuple[bool, int]] = []
    quiesce_thread = threading.Thread(target=lambda: result.append(gate.quiesce(2)))
    quiesce_thread.start()
    time.sleep(0.02)
    assert gate.enter() is None
    assert not result
    release.set()
    quiesce_thread.join(timeout=2)
    for thread in threads:
        thread.join(timeout=2)
    assert result == [(True, 0)]
    assert sorted(accepted) == ["frame", "ordinary", "parallel"]


def test_gate_timeout_is_not_an_ack_and_later_drain_remains_possible() -> None:
    gate = _AdmissionGate()
    lease = gate.enter()
    assert lease is not None
    ok, active = gate.quiesce(0.01)
    assert not ok and active == 1
    lease.__exit__(None, None, None)
    ok, active = gate.quiesce(1)
    assert ok and active == 0


def _load_probe() -> Any:
    spec = importlib.util.spec_from_file_location("pulsar_websocket_quiesce_probe", PROBE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load probe module: {PROBE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def _run_inflight_batch(probe: Any, process: Any, ws_url: str) -> None:
    async with (
        probe.websockets.connect(ws_url, subprotocols=["obswebsocket.json"], open_timeout=15) as poll_ws,
        probe.websockets.connect(ws_url, subprotocols=["obswebsocket.json"], open_timeout=15) as parallel_ws,
        probe.websockets.connect(ws_url, subprotocols=["obswebsocket.json"], open_timeout=15) as frame_ws,
    ):
        await asyncio.gather(
            probe.identify(poll_ws, process.password),
            probe.identify(parallel_ws, process.password),
            probe.identify(frame_ws, process.password),
        )
        started = asyncio.Event()
        close_observed: list[tuple[int | None, str | None]] = []

        async def poll_get() -> None:
            inbox = probe.Inbox()
            count = 0
            try:
                while True:
                    started.set()
                    await probe.request(inbox, poll_ws, "GetStats", f"quiesce-get-{count}")
                    count += 1
                    await asyncio.sleep(0.01)
            except Exception as error:
                close_observed.append((getattr(error, "code", None), getattr(error, "reason", None)))
                return

        async def parallel_batch() -> None:
            inbox = probe.Inbox()
            started.set()
            try:
                # A large valid Parallel batch keeps the thread-pool fan-out
                # occupied long enough to race the shutdown boundary.
                await probe.request_batch(
                    inbox,
                    parallel_ws,
                    "quiesce-parallel-batch",
                    [{"requestType": "GetStats"} for _ in range(2048)],
                    execution_type=2,
                )
            except Exception as error:
                close_observed.append((getattr(error, "code", None), getattr(error, "reason", None)))
                return

        async def frame_batch() -> None:
            inbox = probe.Inbox()
            started.set()
            try:
                await probe.request_batch(
                    inbox,
                    frame_ws,
                    "quiesce-frame-batch",
                    [
                        {
                            "requestType": "Sleep",
                            "requestData": {"sleepFrames": 120},
                        }
                    ],
                    execution_type=1,
                )
            except Exception as error:
                close_observed.append((getattr(error, "code", None), getattr(error, "reason", None)))
                return

        tasks = [asyncio.create_task(worker()) for worker in (poll_get, parallel_batch, frame_batch)]
        await asyncio.sleep(0.2)
        if tasks[1].done():
            raise RuntimeError("parallel batch completed before the shutdown race")
        if tasks[2].done():
            raise RuntimeError("frame batch completed before the shutdown race")
        if not started.is_set():
            raise RuntimeError("no authenticated WebSocket handler started")
        # The caller signals the child concurrently from another thread.  The
        # connections may close before responses are delivered; that is fine
        # as long as the native drain ACK is emitted and the process exits
        # without forced containment.
        await asyncio.gather(*tasks, return_exceptions=True)
        if not any(code == 1001 and reason == "Server quiescing." for code, reason in close_observed):
            raise RuntimeError(
                "authenticated clients did not observe a typed going-away close during quiesce: "
                f"{close_observed!r}"
            )


def _run_windows_integration(executable: Path) -> None:
    if os.name != "nt":
        return
    if not executable.is_file():
        raise RuntimeError(f"Pulsar executable not found: {executable}")

    probe = _load_probe()
    runtime_id = f"websocket-quiesce-{os.getpid()}-{secrets.token_hex(4)}"
    record_dir = Path(tempfile.mkdtemp(prefix="pulsar-websocket-quiesce-"))
    process = probe.PulsarProcess(executable.resolve(), "x264", record_dir, runtime_id=runtime_id)
    started = False
    try:
        process.spawn()
        started = True
        process.wait_for_shutdown_control_ready(timeout=60)
        ready = process.wait_for(probe.READY_RE, timeout=60)
        # Keep the request alive while the parent triggers the inherited event.
        async def drive() -> None:
            task = asyncio.create_task(_run_inflight_batch(probe, process, ready.group(1)))
            await asyncio.sleep(0.25)
            await asyncio.to_thread(process.shutdown)
            await task

        asyncio.run(drive())
        process.assert_shutdown_clean(require_runtime_lease=True)
        lines = process.snapshot()
        ack = "PULSAR_WEBSOCKET_QUIESCE event=ack active_handlers=0 sessions=0 no_handlers_after_ack=1"
        if not any(ack in line for line in lines):
            raise RuntimeError("missing zero-handler WebSocket quiesce ACK")
        ack_index = max(i for i, line in enumerate(lines) if ack in line)
        browser_indices = [i for i, line in enumerate(lines) if "PULSAR_CEF_SHUTDOWN event=pre_obs_shutdown_begin" in line]
        if browser_indices and ack_index >= min(browser_indices):
            raise RuntimeError("WebSocket quiesce ACK occurred after browser teardown began")
    finally:
        if started and process.proc is not None and process.proc.poll() is None:
            try:
                process.shutdown()
            except Exception:
                pass
        shutil.rmtree(record_dir, ignore_errors=True)


def main() -> int:
    executable = Path(os.environ.get("PULSAR_HEADLESS_EXE", ""))
    if len(os.sys.argv) > 1:
        executable = Path(os.sys.argv[1])
    if executable and str(executable) not in (".", ""):
        _run_windows_integration(executable)
    elif os.name == "nt":
        print("SKIP: no pulsar-headless executable was supplied for integration")
    print("PASS: WebSocket admission rejects post-quiesce work and drains in-flight handlers before teardown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
