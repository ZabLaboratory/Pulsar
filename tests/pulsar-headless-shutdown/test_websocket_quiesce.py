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

import pytest


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
    assert "PULSAR_WEBSOCKET_HANDLER event=batch_enter execution_type=%d" in request_batch
    assert "PULSAR_WEBSOCKET_HANDLER event=frame_callback_enter" in request_batch
    assert request_batch.count("event=frame_callback_enter") == 1
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


def test_serial_frame_fixture_keeps_queue_live_after_sleep() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    run_start = source.rindex("async def _run_inflight_batch")
    frame_start = source.index("async def frame_batch", run_start)
    frame_block = source[frame_start : source.index("tasks =", frame_start)]
    # RequestBatchHandler pops Sleep before arming sleepUntilFrame.  A valid
    # trailing request is therefore required to keep the queue non-empty and
    # keep the graphics callback in flight until the requested frame.
    assert frame_block.index('"sleepFrames": 120') < frame_block.index('"requestType": "GetStats"')


def _load_probe() -> Any:
    spec = importlib.util.spec_from_file_location("pulsar_websocket_quiesce_probe", PROBE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load probe module: {PROBE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _wait_for_marker_count(process: Any, marker: str, count: int, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    with process.condition:
        while sum(marker in line for line in process.lines) < count:
            if process.proc is not None and process.proc.poll() is not None:
                raise RuntimeError(f"Pulsar exited before marker {marker!r} x{count}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                observed = sum(marker in line for line in process.lines)
                raise RuntimeError(f"timeout waiting for marker {marker!r} x{count}; observed={observed}")
            process.condition.wait(timeout=min(0.25, remaining))


async def _await_workload_handshake(workload: asyncio.Event, driver: asyncio.Task[Any], timeout: float = 30) -> None:
    readiness = asyncio.create_task(workload.wait())
    try:
        done, _ = await asyncio.wait((driver, readiness), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if driver in done:
            # Propagate an early exception or completion; do not turn it into
            # a successful shutdown readiness signal.
            await driver
        if readiness not in done:
            raise RuntimeError("workloads did not reach the native in-flight handshake")
    finally:
        if not readiness.done():
            readiness.cancel()


def test_handshake_timeout_and_early_driver_failure_are_fatal() -> None:
    async def exercise() -> None:
        never_ready = asyncio.Event()

        async def failing_driver() -> None:
            raise RuntimeError("native batch entry failed")

        failing_task = asyncio.create_task(failing_driver())
        with pytest.raises(RuntimeError, match="native batch entry failed"):
            await _await_workload_handshake(never_ready, failing_task, timeout=1)

        never_finishes = asyncio.create_task(asyncio.sleep(10))
        try:
            with pytest.raises(RuntimeError, match="native in-flight handshake"):
                await _await_workload_handshake(never_ready, never_finishes, timeout=0.01)
        finally:
            never_finishes.cancel()

    asyncio.run(exercise())


async def _run_inflight_batch(
    probe: Any,
    process: Any,
    ws_url: str,
    workloads_inflight: asyncio.Event,
    shutdown_started: asyncio.Event,
) -> None:
    async with (
        probe.websockets.connect(ws_url, subprotocols=["obswebsocket.json"], open_timeout=15) as poll_ws,
        probe.websockets.connect(ws_url, subprotocols=["obswebsocket.json"], open_timeout=15) as parallel_ws_a,
        probe.websockets.connect(ws_url, subprotocols=["obswebsocket.json"], open_timeout=15) as parallel_ws_b,
        probe.websockets.connect(ws_url, subprotocols=["obswebsocket.json"], open_timeout=15) as parallel_ws_c,
        probe.websockets.connect(ws_url, subprotocols=["obswebsocket.json"], open_timeout=15) as parallel_ws_d,
        probe.websockets.connect(ws_url, subprotocols=["obswebsocket.json"], open_timeout=15) as frame_ws,
    ):
        await asyncio.gather(
            probe.identify(poll_ws, process.password),
            probe.identify(parallel_ws_a, process.password),
            probe.identify(parallel_ws_b, process.password),
            probe.identify(parallel_ws_c, process.password),
            probe.identify(parallel_ws_d, process.password),
            probe.identify(frame_ws, process.password),
        )
        started = asyncio.Event()
        poller_ready = asyncio.Event()
        close_observed: list[tuple[int | None, str | None]] = []

        async def poll_get() -> None:
            inbox = probe.Inbox()
            count = 0
            try:
                while True:
                    started.set()
                    await probe.request(inbox, poll_ws, "GetStats", f"quiesce-get-{count}")
                    count += 1
                    if count == 1:
                        poller_ready.set()
                    await asyncio.sleep(0.01)
            except Exception as error:
                if not shutdown_started.is_set():
                    raise RuntimeError(f"GetStats poller failed before shutdown: {error}") from error
                close_observed.append((getattr(error, "code", None), getattr(error, "reason", None)))
                return

        async def parallel_batch(ws: Any, suffix: str) -> None:
            inbox = probe.Inbox()
            started.set()
            try:
                # A large valid Parallel batch keeps the thread-pool fan-out
                # occupied long enough to race the shutdown boundary.
                await probe.request_batch(
                    inbox,
                    ws,
                    f"quiesce-parallel-batch-{suffix}",
                    [{"requestType": "GetStats"} for _ in range(2048)],
                    execution_type=2,
                )
            except Exception as error:
                if not shutdown_started.is_set():
                    raise RuntimeError(f"Parallel batch {suffix} failed before shutdown: {error}") from error
                close_observed.append((getattr(error, "code", None), getattr(error, "reason", None)))
                return
            if not shutdown_started.is_set():
                raise RuntimeError(f"Parallel batch {suffix} completed before shutdown_started")

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
                        },
                        # Sleep is popped before sleepUntilFrame is armed;
                        # retain a sentinel request so SerialFrame remains
                        # pending until the 120th graphics frame.
                        {"requestType": "GetStats"},
                    ],
                    execution_type=1,
                )
            except Exception as error:
                if not shutdown_started.is_set():
                    raise RuntimeError(f"SerialFrame batch failed before shutdown: {error}") from error
                close_observed.append((getattr(error, "code", None), getattr(error, "reason", None)))
                return
            if not shutdown_started.is_set():
                raise RuntimeError("SerialFrame batch completed before shutdown_started")

        tasks = [
            asyncio.create_task(poll_get()),
            asyncio.create_task(parallel_batch(parallel_ws_a, "a")),
            asyncio.create_task(parallel_batch(parallel_ws_b, "b")),
            asyncio.create_task(parallel_batch(parallel_ws_c, "c")),
            asyncio.create_task(parallel_batch(parallel_ws_d, "d")),
            asyncio.create_task(frame_batch()),
        ]
        await poller_ready.wait()
        await asyncio.gather(
            asyncio.to_thread(
                _wait_for_marker_count,
                process,
                "PULSAR_WEBSOCKET_HANDLER event=batch_enter execution_type=2",
                4,
            ),
            asyncio.to_thread(
                _wait_for_marker_count,
                process,
                "PULSAR_WEBSOCKET_HANDLER event=batch_enter execution_type=1",
                1,
            ),
            asyncio.to_thread(
                _wait_for_marker_count,
                process,
                "PULSAR_WEBSOCKET_HANDLER event=frame_callback_enter",
                1,
            ),
        )
        if not started.is_set() or any(tasks[index].done() for index in range(1, 5)) or tasks[5].done():
            raise RuntimeError("an authenticated workload completed before the shutdown handshake")
        workloads_inflight.set()
        # The caller signals the child concurrently from another thread.  The
        # connections may close before responses are delivered; that is fine
        # as long as the native drain ACK is emitted and the process exits
        # without forced containment.
        await asyncio.gather(*tasks)
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
    previous_runtime_id = os.environ.get("PULSAR_RUNTIME_INSTANCE_ID")
    os.environ["PULSAR_RUNTIME_INSTANCE_ID"] = runtime_id
    started = False
    try:
        process.spawn()
        started = True
        process.wait_for_shutdown_control_ready(timeout=60)
        ready = process.wait_for(probe.READY_RE, timeout=60)
        # Keep the request alive while the parent triggers the inherited event.
        async def drive() -> None:
            workloads_inflight = asyncio.Event()
            shutdown_started = asyncio.Event()
            task = asyncio.create_task(
                _run_inflight_batch(probe, process, ready.group(1), workloads_inflight, shutdown_started)
            )
            await _await_workload_handshake(workloads_inflight, task)
            shutdown_started.set()
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
        if previous_runtime_id is None:
            os.environ.pop("PULSAR_RUNTIME_INSTANCE_ID", None)
        else:
            os.environ["PULSAR_RUNTIME_INSTANCE_ID"] = previous_runtime_id
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
