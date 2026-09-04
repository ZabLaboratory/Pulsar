"""Contract tests for the AC-12 RTMP receiver harness."""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "probe-dual-lane.py"
HEADLESS = ROOT / "plugins" / "pulsar-headless" / "main.cpp"
SPEC = importlib.util.spec_from_file_location("probe_dual_lane_contract", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def _latency_fixture_records():
    helper_path = ROOT / "tests" / "probe-take-latency" / "test_probe_take_latency.py"
    spec = importlib.util.spec_from_file_location("latency_fixture_helper", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module._take_records(1)


def test_receiver_separates_server_url_and_stream_key():
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id="runtime-001", stream_id="runtime-001-x264")
    assert receiver.server_url.endswith("/pulsar")
    assert receiver.stream_key == "runtime-001-x264"
    assert receiver.endpoint == receiver.server_url + "/" + receiver.stream_key
    receiver.calibration = {"source": "perf_counter_ns/qpc", "qpc_delta_ns": 11, "qpc_bound_ns": 42}
    metadata = receiver.metadata()
    assert metadata["server_url"] == receiver.server_url
    assert metadata["endpoint"] == receiver.endpoint
    assert metadata["stream_key"] == receiver.stream_key


def test_rtmp_receiver_preserves_default_live_input_buffering():
    source = SCRIPT.read_text(encoding="utf-8")
    receiver_start = source.index("class RtmpReceiver:")
    receiver_end = source.index("class PulsarProcess:", receiver_start)
    receiver_source = source[receiver_start:receiver_end]
    # Keep the RTMP receiver's established default buffering policy.  The
    # DirectShow consumer has a separate low-latency path; applying those
    # input flags here changes FLV packet timing/correlation and can turn a
    # live packet into a fixed multi-hundred-millisecond offset.
    command_start = receiver_source.index("command = [")
    command_end = receiver_source.index("self.proc = subprocess.Popen", command_start)
    command_source = receiver_source[command_start:command_end]
    assert '"-loglevel",\n            "info"' in command_source
    assert '"-loglevel",\n            "verbose"' not in command_source
    assert '"-debug_ts"' in command_source
    assert '"-listen"' in command_source
    assert '"-fflags"' not in command_source
    assert '"-flags"' not in command_source
    assert '"-fps_mode",\n            "passthrough"' in command_source
    assert '"-nostats"' in command_source
    # fps_mode is output-scoped and must remain after the input URL and copy
    # codec; moving it before -i makes FFmpeg reject the receiver command.
    assert command_source.index('"-i"') < command_source.index('"-fps_mode"')
    assert command_source.index('"-c"') < command_source.index('"-fps_mode"')


def test_request_timeout_context_records_send_to_response_duration():
    source = SCRIPT.read_text(encoding="utf-8")
    request_start = source.index("async def request(")
    request_end = source.index("\n\nasync def request_batch(", request_start)
    request_source = source[request_start:request_end]
    assert "sent_monotonic_ns" in request_source
    assert "send_to_response_ms" in request_source
    assert "send_to_timeout_ms" in request_source
    assert "request_context[\"response_monotonic_ns\"]" in request_source
    assert "request_context[\"timeout_monotonic_ns\"]" in request_source


def test_inbox_consumes_out_of_order_buffered_response_without_socket_read():
    class NoReadSocket:
        async def recv(self):
            raise AssertionError("receive_until_response should consume the buffered response")

    inbox = probe.Inbox()
    later = {"requestId": "later", "requestStatus": {"result": True}}
    target = {"requestId": "target", "requestStatus": {"result": True}}
    inbox.responses.extend([later, target])

    received = asyncio.run(inbox.receive_until_response(NoReadSocket(), "target"))

    assert received == target
    assert inbox.responses == [later]


def test_prepare_record_directory_persists_unique_session(tmp_path):
    evidence_root = tmp_path / "evidence"
    context, session, persistent = probe.prepare_record_directory(evidence_root)
    assert persistent is True
    assert session.parent == evidence_root
    assert session.is_dir()
    (session / "pulsar-test.mp4").write_bytes(b"evidence")
    with context as context_path:
        assert Path(context_path) == session
    assert (session / "pulsar-test.mp4").is_file()


def test_prepare_record_directory_default_is_ephemeral():
    context, session, persistent = probe.prepare_record_directory(None)
    assert persistent is False
    assert session.is_dir()
    with context as context_path:
        assert Path(context_path) == session
    assert not session.exists()


def test_prepare_record_directory_rejects_ambiguous_or_unsafe_paths(tmp_path):
    existing_file = tmp_path / "recording.mp4"
    existing_file.write_bytes(b"old")
    with pytest.raises(probe.ProbeFailure, match="not a directory"):
        probe.prepare_record_directory(existing_file)

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    (evidence_root / "old.mp4").write_bytes(b"old")
    with pytest.raises(probe.ProbeFailure, match="ambiguous"):
        probe.prepare_record_directory(evidence_root)

    with pytest.raises(probe.ProbeFailure, match="outside the Pulsar repository"):
        probe.prepare_record_directory(probe.REPO_ROOT)


def test_prepare_record_directory_rejects_dangling_and_existing_symlinks(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    existing_link = tmp_path / "existing-link"
    dangling_link = tmp_path / "dangling-link"
    try:
        existing_link.symlink_to(target, target_is_directory=True)
        dangling_link.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(probe.ProbeFailure, match="symlink or reparse"):
        probe.prepare_record_directory(existing_link)
    with pytest.raises(probe.ProbeFailure, match="symlink or reparse"):
        probe.prepare_record_directory(dangling_link)


def test_prepare_record_directory_rejects_junction_component_on_windows(tmp_path):
    if probe.os.name != "nt":
        pytest.skip("junctions are Windows-specific")
    target = tmp_path / "junction-target"
    target.mkdir()
    junction = tmp_path / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")
    with pytest.raises(probe.ProbeFailure, match="symlink or reparse"):
        probe.prepare_record_directory(junction / "new-output")


def test_parse_args_accepts_persistent_record_directory(tmp_path):
    args = probe.parse_args(
        ["--encoder", "x264", "--record-dir", str(tmp_path / "evidence")]
    )
    assert args.record_dir == tmp_path / "evidence"


def test_return_transport_opt_in_is_explicit_and_defaults_to_inherited_policy(monkeypatch):
    monkeypatch.delenv("PULSAR_RETURN_TRANSPORT", raising=False)
    assert probe.parse_args(["--encoder", "x264"]).return_transport is None

    monkeypatch.setenv("PULSAR_RETURN_TRANSPORT", "d3d11")
    assert probe.parse_args(["--encoder", "x264"]).return_transport == "d3d11"

    assert probe.parse_args(["--encoder", "x264", "--return-transport", "cpu"]).return_transport == "cpu"


def test_return_transport_is_propagated_to_runtime_and_directshow_children():
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count('env["PULSAR_RETURN_TRANSPORT"] = self.return_transport') == 2
    assert "default=os.environ.get(\"PULSAR_RETURN_TRANSPORT\") or None" in source
    assert "args.return_transport," in source


def test_scene_composition_churn_diagnostic_is_opt_in_and_checks_active_binding():
    source = (ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp").read_text(
        encoding="utf-8"
    )
    assert 'PULSAR_SCENE_CHURN_DIAGNOSTICS' in source
    assert 'scene_composition_churn' in source
    assert 'obs_scene_enum_items(' in source
    assert 'obs_sceneitem_get_source(laneItems[lane]) == scene' in source
    assert '++sceneCompositionAdds[lane]' in source
    assert '++sceneCompositionRemoves[lane]' in source


def test_recording_output_must_stay_under_runtime_directory(tmp_path):
    record_dir = tmp_path / "session"
    record_dir.mkdir()
    owned = record_dir / "recording.mp4"
    owned.write_bytes(b"evidence")
    assert probe.ensure_recording_output_owned(str(owned), record_dir) == owned.resolve()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"evidence")
    with pytest.raises(probe.ProbeFailure, match="outside this probe"):
        probe.ensure_recording_output_owned(str(outside), record_dir)


def test_rtmp_packet_identity_is_bounded_for_maximum_stream_id():
    first = probe._rtmp_packet_identity("s" * 128, 0, 0, 0)
    second = probe._rtmp_packet_identity("s" * 128, 1, 0, 0)
    assert len(first) <= 128
    assert len(second) <= 128
    assert first != second


def test_stream_id_is_bounded_for_maximum_runtime_id():
    stream_id = probe._stream_id_for_runtime("r" * 128, "nvenc")
    assert len(stream_id) <= 128
    assert probe.ID_RE.fullmatch(stream_id) is not None
    assert stream_id.startswith("stream-nvenc-")


def test_spawn_after_rtmp_ready_starts_listener_before_spawn():
    events = []

    class FakeProcess:
        rtmp_receiver = object()

        def start_rtmp_consumer(self):
            events.append("receiver.start")

        def spawn(self):
            events.append("pulsar.spawn")
            assert events == ["receiver.start", "pulsar.spawn"]

    probe.spawn_after_rtmp_ready(FakeProcess())
    assert events == ["receiver.start", "pulsar.spawn"]


def test_windows_shutdown_uses_anonymous_inherited_event_and_requires_ack():
    headless = HEADLESS.read_text(encoding="utf-8")
    driver = SCRIPT.read_text(encoding="utf-8")

    assert "PULSAR_SHUTDOWN_EVENT_HANDLE" in headless
    assert "CreateEvent" not in headless
    assert "mechanism=named_event" not in headless
    assert "SetHandleInformation(candidate, HANDLE_FLAG_INHERIT, 0)" in headless
    assert "WaitForSingleObject(g_shutdown_event, 100)" in headless
    assert "reason=closed_handle" in headless
    assert 'std::fprintf(stderr, "[pulsar-headless] shutting down\\n");' in headless
    assert 'std::fflush(stderr);' in headless
    assert '"handle_list": [handle]' in driver
    assert "SetHandleInformation" in driver
    assert "_windows_signal_shutdown_event" in driver
    assert "self.forced_kill_used = True" in driver
    assert "wait_for_shutdown_control_ready" in driver
    assert driver.index("process.wait_for_shutdown_control_ready(timeout=60)") < driver.index(
        "ready_match = process.wait_for(READY_RE, timeout=60)"
    )


def test_ctest_resolves_python_from_runtime_path():
    cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    registration = cmake[cmake.index("NAME pulsar-headless-graceful-shutdown") :]
    registration = registration[: registration.index("endif()")]

    assert "COMMAND python" in registration
    assert "Python3_EXECUTABLE" not in registration
    assert "find_package(Python3" not in registration
    assert "hostedtoolcache" not in registration.lower()


def test_shutdown_integration_reports_sanitized_failure_tail():
    integration = (
        ROOT / "tests" / "pulsar-headless-shutdown" / "test_graceful_shutdown.py"
    ).read_text(encoding="utf-8")

    assert "probe.failure_tail(process.snapshot(), 40)" in integration
    assert "Sanitized Pulsar log tail:" in integration


def _boundary_wait_fixture(count: int = 1, *, omit: tuple[int, str] | None = None):
    records = []
    commits = []
    packets = []
    runtime_id = "runtime-boundary-wait"
    for number in range(1, count + 1):
        take_id = f"take-{number:03d}"
        intent_id = f"intent-{number:03d}"
        frame_id = 100 + number
        pts_ns = 10_000_000_000 + number * 16_666_667
        commit_at = 1_000_000_000 + number * 100_000_000
        revisions = {"program": number, "preview": 0, "role_map": number}
        records.extend(
            [
                {
                    "record_type": "event",
                    "event": {
                        "event_type": "TakeAccepted",
                        "runtime_instance_id": runtime_id,
                        "command_id": take_id,
                        "intent_id": intent_id,
                        "take_command_id": take_id,
                        "observed_at_monotonic_ns": commit_at - 5_000_000,
                    },
                },
                {
                    "record_type": "event",
                    "event": {
                        "event_type": "TakeCommitted",
                        "runtime_instance_id": runtime_id,
                        "command_id": take_id,
                        "intent_id": intent_id,
                        "take_command_id": take_id,
                        "observed_at_monotonic_ns": commit_at,
                        "frame_id": frame_id,
                        "pts_ns": pts_ns,
                        "revisions": revisions,
                    },
                },
            ]
        )
        commit = probe.Commit(number, frame_id, pts_ns, 0, 1, 1, 1, 1, 1)
        commits.append(commit)
        for boundary in probe.PRODUCER_BOUNDARIES:
            if omit == (number, boundary):
                continue
            record = {
                "record_type": "observation",
                "boundary": boundary,
                "runtime_instance_id": runtime_id,
                "command_id": take_id,
                "intent_id": intent_id,
                "take_command_id": take_id,
                "revisions": revisions,
                "frame_id": frame_id,
                "pts_ns": pts_ns,
                "observed_at_monotonic_ns": commit_at + 5_000_000,
                "valid": True,
            }
            if boundary == "encoded_first_packet":
                record.update(
                    {
                        "packet_index": number - 1,
                        "packet_pts": number - 1,
                        "packet_dts": number - 1,
                        "packet_timebase_num": 1,
                        "packet_timebase_den": 60,
                    }
                )
            records.append(record)
        packet_tick = 1500 + round((number - 1) * 1000 / 60)
        packets.append(
            {
                "packet_index": number - 1,
                "packet_pts": packet_tick,
                "packet_dts": packet_tick,
                "packet_identity": f"packet-{number}",
                "observed_at_monotonic_ns": commit_at + 7_000_000,
            }
        )
    return runtime_id, records, commits, packets


def _boundary_wait_process(trace: Path, runtime_id: str) -> Any:
    class LiveProc:
        def poll(self):
            return None

    class Process:
        proc: Any
        proc = LiveProc()
        trace_path = trace
        runtime_id: str

        def snapshot(self) -> list[str]:
            return []

    process = Process()
    process.runtime_id = runtime_id
    return process


def test_wait_for_take_boundaries_requires_all_four_unique_correlations(tmp_path):
    runtime_id, records, commits, packets = _boundary_wait_fixture(200)
    trace = tmp_path / "trace.jsonl"
    trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id=runtime_id, stream_id="stream-nvenc")
    receiver.packets = packets
    process = _boundary_wait_process(trace, runtime_id)
    correlation = probe.RtmpPacketCorrelation()

    for commit in commits:
        result = asyncio.run(
            probe.wait_for_take_boundaries(process, receiver, commit, correlation, timeout=0.1)
        )
        assert set(result) == {*probe.PRODUCER_BOUNDARIES, "rtmp_first_packet"}
    assert correlation.used_packet_indices == set(range(200))
    assert correlation.offset_min is not None
    assert correlation.offset_max is not None


def test_wait_for_take_boundaries_rejects_duplicate_or_shifted_receiver_index(tmp_path):
    runtime_id, records, commits, packets = _boundary_wait_fixture(1)
    trace = tmp_path / "trace.jsonl"
    trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    process = _boundary_wait_process(trace, runtime_id)
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id=runtime_id, stream_id="stream-x264")
    receiver.packets = [packets[0], dict(packets[0])]
    with pytest.raises(probe.ProbeFailure, match="ambiguous RTMP packet correlation"):
        asyncio.run(
            probe.wait_for_take_boundaries(
                process, receiver, commits[0], probe.RtmpPacketCorrelation(), timeout=0.1
            )
        )

    receiver.packets = [dict(packets[0], packet_index=packets[0]["packet_index"] + 1)]
    with pytest.raises(probe.ProbeFailure, match="rtmp_candidates=0"):
        asyncio.run(
            probe.wait_for_take_boundaries(
                process, receiver, commits[0], probe.RtmpPacketCorrelation(), timeout=0.05
            )
        )


def test_wait_for_take_boundaries_rejects_n_minus_one_and_duplicate(tmp_path):
    runtime_id, records, commits, packets = _boundary_wait_fixture(
        2, omit=(2, "directshow_return")
    )
    trace = tmp_path / "trace.jsonl"
    trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id=runtime_id, stream_id="stream-nvenc")
    receiver.packets = packets
    process = _boundary_wait_process(trace, runtime_id)
    correlation = probe.RtmpPacketCorrelation()
    asyncio.run(probe.wait_for_take_boundaries(process, receiver, commits[0], correlation, timeout=0.1))
    with pytest.raises(probe.ProbeFailure, match="directshow_return=0"):
        asyncio.run(probe.wait_for_take_boundaries(process, receiver, commits[1], correlation, timeout=0.05))

    runtime_id, records, commits, packets = _boundary_wait_fixture(1)
    encoded = next(record for record in records if record.get("boundary") == "encoded_first_packet")
    records.append(dict(encoded))
    trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    receiver.packets = packets
    with pytest.raises(probe.ProbeFailure, match="duplicate valid encoded_first_packet"):
        asyncio.run(
            probe.wait_for_take_boundaries(
                process, receiver, commits[0], probe.RtmpPacketCorrelation(), timeout=0.1
            )
        )


def test_wait_for_take_boundaries_rejects_frame_pts_mismatch_and_dead_process(tmp_path):
    runtime_id, records, commits, packets = _boundary_wait_fixture(1)
    directshow = next(record for record in records if record.get("boundary") == "directshow_return")
    directshow["pts_ns"] += 1
    trace = tmp_path / "trace.jsonl"
    trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id=runtime_id, stream_id="stream-nvenc")
    receiver.packets = packets
    process = _boundary_wait_process(trace, runtime_id)
    with pytest.raises(probe.ProbeFailure, match="frame/PTS"):
        asyncio.run(
            probe.wait_for_take_boundaries(
                process, receiver, commits[0], probe.RtmpPacketCorrelation(), timeout=0.1
            )
        )

    runtime_id, records, commits, packets = _boundary_wait_fixture(1)
    stale = next(record for record in records if record.get("boundary") == "encoder_input_raw")
    stale["runtime_instance_id"] = "stale-runtime"
    trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    receiver.packets = packets
    process = _boundary_wait_process(trace, runtime_id)
    with pytest.raises(probe.ProbeFailure, match="stale runtime"):
        asyncio.run(
            probe.wait_for_take_boundaries(
                process, receiver, commits[0], probe.RtmpPacketCorrelation(), timeout=0.1
            )
        )

    runtime_id, records, commits, packets = _boundary_wait_fixture(2)
    packets[1]["packet_pts"] += 5
    packets[1]["packet_dts"] += 5
    trace.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    receiver.packets = packets
    process = _boundary_wait_process(trace, runtime_id)
    correlation = probe.RtmpPacketCorrelation()
    asyncio.run(probe.wait_for_take_boundaries(process, receiver, commits[0], correlation, timeout=0.1))
    with pytest.raises(probe.ProbeFailure, match="rtmp_candidates=0"):
        asyncio.run(probe.wait_for_take_boundaries(process, receiver, commits[1], correlation, timeout=0.1))

    dead_process = _boundary_wait_process(trace, runtime_id)
    dead_process.proc = type("Dead", (), {"poll": lambda self: 17})()

    with pytest.raises(probe.ProbeFailure, match="runtime exited"):
        asyncio.run(
            probe.wait_for_take_boundaries(
                dead_process, receiver, commits[0], probe.RtmpPacketCorrelation(), timeout=1.0
            )
        )


def test_windows_shutdown_signal_failure_contains_with_forced_kill(monkeypatch):
    probe_module = probe

    class StuckProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("pulsar", timeout)
            return self.returncode

        def kill(self):
            self.returncode = -9

    process = probe_module.PulsarProcess(
        Path("pulsar.exe"), "x264", Path("record"), runtime_id="runtime-shutdown-failure"
    )
    process.proc = StuckProcess()
    process.shutdown_event_handle = 123
    monkeypatch.setattr(probe_module.os, "name", "nt")

    def fail_signal(_handle):
        raise probe_module.ProbeFailure("signal failed")

    monkeypatch.setattr(probe_module, "_windows_signal_shutdown_event", fail_signal)
    monkeypatch.setattr(probe_module, "_windows_close_handle", lambda _handle: None)

    with pytest.raises(probe_module.ProbeFailure, match="forced process kill|signal failed"):
        process.shutdown()
    assert process.forced_kill_used is True
    assert process.proc.returncode == -9


def test_drive_propagates_resource_wait_and_keeps_outputs_alive_until_threshold(tmp_path):
    assert "minimum_resource_samples" in inspect.signature(probe.drive).parameters
    assert "resource_sample_timeout" in inspect.signature(probe.drive).parameters
    drive_source = inspect.getsource(probe.drive)
    run_source = inspect.getsource(probe.run)
    assert "wait_for_eligible_resource_samples" in drive_source
    assert "wait_for_take_boundaries" in drive_source
    assert "complete_boundary_count += 1" in drive_source
    assert "complete_boundary_count != total_takes" in drive_source
    assert "minimum_resource_samples=args.resource_samples" in run_source
    assert "minimum_rtmp_samples=args.resource_samples" in run_source
    wait_index = drive_source.index("wait_for_eligible_resource_samples")
    boundary_wait_index = drive_source.index("wait_for_take_boundaries")
    commit_validation_index = drive_source.index("validate_commit(identity")
    stream_stop_index = drive_source.index("await process.stop_rtmp_stream", wait_index)
    record_stop_index = drive_source.index('request(inbox, ws, "StopRecord"', wait_index)
    assert wait_index < stream_stop_index < record_stop_index
    assert commit_validation_index < boundary_wait_index
    boundary_wait_source = inspect.getsource(probe.wait_for_take_boundaries)
    assert "receiver.snapshot()" in boundary_wait_source
    assert "receiver._lock" not in boundary_wait_source
    assert "stop_rtmp_stream" not in boundary_wait_source

    trace = tmp_path / "trace.jsonl"
    runtime_id_value = "runtime-resource-wait"

    class FakeProc:
        def poll(self):
            return None

    class FakeProcess:
        trace_path = trace
        resource_mode = "dual_lane"
        runtime_id = runtime_id_value
        proc = FakeProc()

    first = {
        "record_type": "resource_sample",
        "sample_mode": "dual_lane",
        "runtime_instance_id": runtime_id_value,
        "encoder_active": True,
        "encoder_family": "nvenc",
        "rtmp_load_active": False,
        "encode_time_samples": 100,
    }
    second = dict(first, rtmp_load_active=True)
    third = dict(first, rtmp_load_active=True)
    trace.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    with pytest.raises(probe.ProbeSkip, match="did not produce enough"):
        asyncio.run(probe.wait_for_eligible_resource_samples(FakeProcess(), "dual_lane", 2, 0.01))
    trace.write_text(
        json.dumps(first) + "\n" + json.dumps(second) + "\n" + json.dumps(third) + "\n",
        encoding="utf-8",
    )
    assert asyncio.run(probe.wait_for_eligible_resource_samples(FakeProcess(), "dual_lane", 2, 0.5)) == 2


def test_resource_wait_does_not_count_zero_encode_warmup_toward_minimum(tmp_path):
    trace = tmp_path / "trace-warmup.jsonl"
    runtime_id_value = "runtime-resource-warmup"

    class FakeProc:
        def poll(self):
            return None

    class FakeProcess:
        trace_path = trace
        runtime_id = runtime_id_value
        proc = FakeProc()

    sample = {
        "record_type": "resource_sample",
        "sample_mode": "dual_lane",
        "runtime_instance_id": runtime_id_value,
        "encoder_active": True,
        "encoder_family": "nvenc",
        "rtmp_load_active": True,
    }
    zero = dict(sample, encode_time_samples=0)
    one = dict(sample, encode_time_samples=1)
    trace.write_text(json.dumps(zero) + "\n" + json.dumps(one) + "\n", encoding="utf-8")
    with pytest.raises(probe.ProbeSkip, match="did not produce enough"):
        asyncio.run(probe.wait_for_eligible_resource_samples(FakeProcess(), "dual_lane", 2, 0.01))

    two = dict(sample, encode_time_samples=2)
    trace.write_text(
        json.dumps(zero) + "\n" + json.dumps(one) + "\n" + json.dumps(two) + "\n", encoding="utf-8"
    )
    assert asyncio.run(probe.wait_for_eligible_resource_samples(FakeProcess(), "dual_lane", 2, 0.5)) == 2


def test_resource_wait_reports_process_exit_as_failure(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("", encoding="utf-8")

    class DeadProc:
        def poll(self):
            return 17

    class DeadProcess:
        trace_path = trace
        runtime_id = "runtime-resource-exited"
        proc = DeadProc()

    with pytest.raises(probe.ProbeFailure, match="runtime exited"):
        asyncio.run(probe.wait_for_eligible_resource_samples(DeadProcess(), "dual_lane", 1, 30.0))


def test_fusion_is_atomic_and_keeps_existing_reference_on_validation_failure(tmp_path):
    records = [
        record
        for record in _latency_fixture_records()
        if not (record.get("record_type") == "observation" and record.get("boundary") == "rtmp_first_packet")
    ]
    producer = tmp_path / "producer.jsonl"
    producer.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    final = tmp_path / "final.jsonl"
    final.write_text("known-good-reference\n", encoding="utf-8")
    expected_hash = hashlib.sha256(final.read_bytes()).hexdigest()

    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id="runtime-fixture-001", stream_id="runtime-fixture-001-nvenc")
    receiver.calibration = {"source": "perf_counter_ns/qpc", "qpc_delta_ns": 0, "qpc_bound_ns": 42}
    receiver.packets = []
    with pytest.raises(probe.ProbeFailure, match="no demuxed video packet"):
        receiver.fuse_trace(producer, final)
    assert hashlib.sha256(final.read_bytes()).hexdigest() == expected_hash
    assert not list(tmp_path.glob("*.fused.tmp"))


def test_fusion_emits_receiver_observation_only_after_unique_pts_match(tmp_path):
    records = [
        record
        for record in _latency_fixture_records()
        if not (record.get("record_type") == "observation" and record.get("boundary") == "rtmp_first_packet")
    ]
    producer = tmp_path / "producer.jsonl"
    producer.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    final = tmp_path / "final.jsonl"
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id="runtime-fixture-001", stream_id="runtime-fixture-001-nvenc")
    receiver.calibration = {"source": "perf_counter_ns/qpc", "qpc_delta_ns": 0, "qpc_bound_ns": 42}
    receiver.packets = [
        {
            "packet_index": 0,
            "packet_pts": 1500,
            "packet_dts": 1500,
            "packet_pts_time_ms": 1500,
            "packet_dts_time_ms": 1500,
            "observed_at_monotonic_ns": 1_020_000_000,
            "packet_identity": "runtime-fixture-001-nvenc-video-0-1500-1500",
        }
    ]
    receiver.fuse_trace(producer, final)
    fused = [json.loads(line) for line in final.read_text(encoding="utf-8").splitlines()]
    rtmp = [record for record in fused if record.get("boundary") == "rtmp_first_packet"]
    assert len(rtmp) == 1
    assert rtmp[0]["packet_identity"] == receiver.packets[0]["packet_identity"]
    assert rtmp[0]["surface"] == "RTMP"
    assert rtmp[0]["consumer"] == "receiver"
    assert rtmp[0]["receiver_observed_normalized_ns"] == rtmp[0]["observed_at_monotonic_ns"]


def test_fusion_parser_failure_keeps_existing_final_byte_for_byte(tmp_path):
    records = [
        record
        for record in _latency_fixture_records()
        if not (record.get("record_type") == "observation" and record.get("boundary") == "rtmp_first_packet")
    ]
    # Keep the encoded producer record but remove its declaration.  The
    # fusion writer can build the sibling temp, while parser validation must
    # reject it before os.replace touches the existing final artifact.
    records[0]["capture_paths"] = [
        path for path in records[0]["capture_paths"] if path != "encoded_first_packet"
    ]
    producer = tmp_path / "producer.jsonl"
    producer.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    final = tmp_path / "final.jsonl"
    final.write_bytes(b"known-good-reference\n")
    expected = final.read_bytes()
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id="runtime-fixture-001", stream_id="runtime-fixture-001-nvenc")
    receiver.calibration = {"source": "perf_counter_ns/qpc", "qpc_delta_ns": 0, "qpc_bound_ns": 42}
    receiver.packets = [
        {
            "packet_index": 0,
            "packet_pts": 1500,
            "packet_dts": 1500,
            "packet_pts_time_ms": 1500,
            "packet_dts_time_ms": 1500,
            "observed_at_monotonic_ns": 1_020_000_000,
            "packet_identity": "runtime-fixture-001-nvenc-video-0-1500-1500",
        }
    ]
    with pytest.raises(probe.ProbeFailure, match="parser validation failed"):
        receiver.fuse_trace(producer, final)
    assert final.read_bytes() == expected
    assert not list(tmp_path.glob("*.fused.tmp"))


def test_receiver_metadata_rejects_unbounded_calibration():
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id="runtime-001", stream_id="runtime-001-nvenc")
    receiver.calibration = {"source": "perf_counter_ns/qpc", "qpc_delta_ns": 0, "qpc_bound_ns": 5_000_001}
    with pytest.raises(probe.ProbeFailure, match="calibration bound"):
        receiver.metadata()


def test_receiver_diagnostic_preserves_packets_tail_and_mux_calibration(tmp_path):
    runtime_id, records, _commits, packets = _boundary_wait_fixture(1)
    encoded = next(record for record in records if record.get("boundary") == "encoded_first_packet")
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id=runtime_id, stream_id="stream-x264")
    receiver.calibration = {
        "source": "perf_counter_ns/qpc",
        "qpc_delta_ns": 0,
        "qpc_bound_ns": 42,
    }
    receiver.packets = packets
    receiver.lines = [f"line-{index}" for index in range(250)]
    candidate = receiver.live_correlation.candidates(encoded, packets)[0]
    receiver.live_correlation.commit(candidate)

    diagnostic = receiver.persist_diagnostics(tmp_path / "producer.jsonl")
    payload = json.loads(diagnostic.read_text(encoding="utf-8"))
    assert payload["evidence_kind"] == "failed_run_diagnostic_only"
    assert payload["packet_count"] == 1
    assert payload["packets"] == packets
    assert len(payload["line_tail"]) == 200
    assert payload["line_tail"][0] == "line-50"
    assert payload["correlation"]["correlation_method"] == "packet_index_constant_mux_offset_v1"


def test_failure_diagnostic_is_emitted_only_after_owned_cleanup():
    source = inspect.getsource(probe.run)
    cleanup_index = source.index("process.shutdown()")
    diagnostic_index = source.index('print(f"FAIL: {failure_message}"')
    assert cleanup_index < diagnostic_index
    fusion = source[source.index("process.finalize_rtmp_trace") :]
    assert fusion.index("persist_diagnostics") < fusion.index("cleanup verification failed")


def test_injected_campaign_failure_runs_cleanup_before_reporting(monkeypatch, tmp_path):
    executable = tmp_path / "pulsar.exe"
    executable.write_bytes(b"fixture")
    args = probe.parse_args(["--exe", str(executable), "--encoder", "x264", "--takes", "1"])
    events: list[str] = []

    class FakeProcess:
        rtmp_receiver = None

        def __init__(self, *_args, **_kwargs):
            pass

        def shutdown(self):
            events.append("cleanup")

    async def fail_drive(*_args, **_kwargs):
        raise probe.ProbeFailure("injected boundary failure")

    original_print = builtins.print

    def record_print(*values, **kwargs):
        if values and str(values[0]).startswith("FAIL: injected boundary failure"):
            events.append("failure-report")
        return original_print(*values, **kwargs)

    monkeypatch.setattr(probe, "PulsarProcess", FakeProcess)
    monkeypatch.setattr(probe, "spawn_after_rtmp_ready", lambda _process: None)
    monkeypatch.setattr(probe, "drive", fail_drive)
    monkeypatch.setattr(builtins, "print", record_print)

    assert probe.run(args) == probe.EXIT_FAIL
    assert events == ["cleanup", "failure-report"]


def _resource_only_args(*extra: str):
    return probe.parse_args(
        [
            "--encoder",
            "nvenc",
            "--trace",
            "trace.jsonl",
            "--build-revision",
            "f" * 40,
            "--capture-window",
            "window",
            "--cef-workload",
            "--resource-mode",
            "reference",
            "--resource-only",
            "--rtmp-receiver",
            *extra,
        ]
    )


def _reference_append_records():
    records = [
        record
        for record in _latency_fixture_records()
        if record.get("record_type") == "session"
        or (record.get("record_type") == "resource_sample" and record.get("sample_mode") == "reference")
    ]
    session = records[0]
    session["runtime_instance_id"] = "runtime-fixture-001"
    session["build_revision"] = "f" * 40
    session["hardware"] = {"host": "fixture-host", "gpu": "fixture-gpu"}
    session["producer_topology"] = "single_lane_reference"
    session["producer_count"] = 1
    for record in records[1:]:
        record["runtime_instance_id"] = session["runtime_instance_id"]
        record["build_revision"] = session["build_revision"]
        record["hardware"] = session["hardware"].copy()
        record["producer_topology"] = "single_lane_reference"
        record["producer_count"] = 1
    return records


def test_trace_append_rtmp_requires_reference_load_metadata_before_spawn(tmp_path):
    records = _reference_append_records()
    path = tmp_path / "reference.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    probe.validate_trace_append(
        path,
        runtime_id="runtime-fixture-001",
        build_revision="f" * 40,
        trace_host="fixture-host",
        trace_gpu="fixture-gpu",
        require_rtmp_load=True,
    )

    records[0].pop("rtmp_load_requested")
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    with pytest.raises(probe.ProbeFailure, match="requires reference rtmp_load_requested"):
        probe.validate_trace_append(
            path,
            runtime_id="runtime-fixture-001",
            build_revision="f" * 40,
            trace_host="fixture-host",
            trace_gpu="fixture-gpu",
            require_rtmp_load=True,
        )

    records = _reference_append_records()
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    with pytest.raises(probe.ProbeFailure, match="lacks enough active NVENC samples"):
        probe.validate_trace_append(
            path,
            runtime_id="runtime-fixture-001",
            build_revision="f" * 40,
            trace_host="fixture-host",
            trace_gpu="fixture-gpu",
            require_rtmp_load=True,
            minimum_rtmp_samples=3,
        )

    records = _reference_append_records()
    records[1]["encode_time_samples"] = 0
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    with pytest.raises(probe.ProbeFailure, match="1 < 2"):
        probe.validate_trace_append(
            path,
            runtime_id="runtime-fixture-001",
            build_revision="f" * 40,
            trace_host="fixture-host",
            trace_gpu="fixture-gpu",
            require_rtmp_load=True,
            minimum_rtmp_samples=2,
        )


def test_resource_only_reference_may_enable_rtmp_receiver():
    args = _resource_only_args()
    assert args.resource_mode == "reference"
    assert args.resource_only is True
    assert args.rtmp_receiver is True
    assert args.trace_append is False


@pytest.mark.parametrize(
    "extra",
    [
        ("--encoder", "x264"),
        ("--resource-mode", "dual_lane"),
        ("--trace-append", "--runtime-id", "runtime-001"),
    ],
)
def test_resource_only_rtmp_rejects_non_reference_or_append(extra):
    with pytest.raises(SystemExit):
        _resource_only_args(*extra)


def test_trace_append_requires_rtmp_receiver_for_ac13():
    with pytest.raises(SystemExit):
        probe.parse_args(
            [
                "--encoder",
                "nvenc",
                "--trace",
                "trace.jsonl",
                "--build-revision",
                "f" * 40,
                "--runtime-id",
                "runtime-001",
                "--capture-window",
                "window",
                "--cef-workload",
                "--trace-append",
                "--resource-mode",
                "dual_lane",
            ]
        )


def test_resource_fusion_requires_observed_active_rtmp_load_without_fabricating(tmp_path):
    records = [record for record in _latency_fixture_records() if record.get("record_type") == "session" or record.get("record_type") == "resource_sample"]
    producer = tmp_path / "producer.jsonl"
    producer.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    final = tmp_path / "reference.jsonl"
    final.write_bytes(b"reference-before\n")
    expected = final.read_bytes()
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id="runtime-fixture-001", stream_id="runtime-fixture-001-nvenc")
    receiver.calibration = {"source": "perf_counter_ns/qpc", "qpc_delta_ns": 0, "qpc_bound_ns": 42}
    receiver.packets = [{"packet_index": 1, "packet_pts": 0, "packet_dts": 0}]
    for record in records:
        if record.get("record_type") == "resource_sample":
            record["rtmp_load_active"] = False
    producer.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    with pytest.raises(probe.ProbeFailure, match="observed-active samples"):
        receiver.fuse_resource_trace(producer, final)
    assert final.read_bytes() == expected


def test_resource_fusion_allows_warmup_false_then_observed_nvenc_active(tmp_path):
    records = [
        record
        for record in _latency_fixture_records()
        if record.get("record_type") == "session" or record.get("record_type") == "resource_sample"
    ]
    samples = [record for record in records if record.get("record_type") == "resource_sample"]
    samples[0]["encoder_active"] = False
    samples[0]["rtmp_load_active"] = False
    samples[1]["encoder_active"] = True
    samples[1]["encoder_family"] = "nvenc"
    samples[1]["rtmp_load_active"] = True
    producer = tmp_path / "producer.jsonl"
    producer.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    final = tmp_path / "reference.jsonl"
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id="runtime-fixture-001", stream_id="runtime-fixture-001-nvenc")
    receiver.calibration = {"source": "perf_counter_ns/qpc", "qpc_delta_ns": 0, "qpc_bound_ns": 42}
    receiver.packets = [{"packet_index": 1, "packet_pts": 0, "packet_dts": 0}]
    receiver.fuse_resource_trace(producer, final, minimum_samples=1)
    fused = [json.loads(line) for line in final.read_text(encoding="utf-8").splitlines()]
    fused_samples = [record for record in fused if record.get("record_type") == "resource_sample"]
    assert fused_samples[0]["encoder_active"] is False
    assert fused_samples[0]["rtmp_load_active"] is False
    assert fused_samples[1]["encoder_active"] is True
    assert fused_samples[1]["rtmp_load_active"] is True


def test_resource_fusion_does_not_count_rtmp_without_active_nvenc(tmp_path):
    records = [
        record
        for record in _latency_fixture_records()
        if record.get("record_type") == "session" or record.get("record_type") == "resource_sample"
    ]
    samples = [record for record in records if record.get("record_type") == "resource_sample"]
    for sample in samples:
        sample["encoder_active"] = False
        sample["encoder_family"] = "nvenc"
        sample["rtmp_load_active"] = True
    producer = tmp_path / "producer.jsonl"
    producer.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    final = tmp_path / "reference.jsonl"
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id="runtime-fixture-001", stream_id="runtime-fixture-001-nvenc")
    receiver.calibration = {"source": "perf_counter_ns/qpc", "qpc_delta_ns": 0, "qpc_bound_ns": 42}
    receiver.packets = [{"packet_index": 1, "packet_pts": 0, "packet_dts": 0}]
    with pytest.raises(probe.ProbeFailure, match="observed-active samples"):
        receiver.fuse_resource_trace(producer, final, minimum_samples=1)


def test_probe_identifies_with_only_output_events_to_bound_socket_backlog():
    class FakeWebSocket:
        def __init__(self):
            self.messages = [
                {"op": 0, "d": {"rpcVersion": 1}},
                {"op": 2, "d": {"negotiatedRpcVersion": 1}},
            ]
            self.sent = []

        async def recv(self):
            return json.dumps(self.messages.pop(0))

        async def send(self, message):
            self.sent.append(json.loads(message))

    ws = FakeWebSocket()
    asyncio.run(probe.identify(ws, "unused-password"))
    assert ws.sent[0]["op"] == 1
    assert ws.sent[0]["d"]["eventSubscriptions"] == probe.PROBE_EVENT_SUBSCRIPTIONS
    assert probe.PROBE_EVENT_SUBSCRIPTIONS == (1 << 6) | (1 << 9)


def test_resource_fusion_preserves_observed_active_samples(tmp_path):
    records = [
        record
        for record in _latency_fixture_records()
        if record.get("record_type") == "session" or record.get("record_type") == "resource_sample"
    ]
    producer = tmp_path / "producer.jsonl"
    producer.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    final = tmp_path / "reference.jsonl"
    receiver = probe.RtmpReceiver("ffmpeg.exe", runtime_id="runtime-fixture-001", stream_id="runtime-fixture-001-nvenc")
    receiver.calibration = {"source": "perf_counter_ns/qpc", "qpc_delta_ns": 0, "qpc_bound_ns": 42}
    receiver.packets = [{"packet_index": 1, "packet_pts": 0, "packet_dts": 0}]
    receiver.fuse_resource_trace(producer, final, minimum_samples=2)
    fused = [json.loads(line) for line in final.read_text(encoding="utf-8").splitlines()]
    assert fused[0]["rtmp_load_requested"] is True
    assert all(record["rtmp_load_active"] is True for record in fused if record.get("record_type") == "resource_sample")


def test_resource_sample_timeout_budget_adds_only_bounded_schedule_margin():
    assert probe.resource_sample_timeout_budget(10, 500) == 30.0
    assert probe.resource_sample_timeout_budget(300, 500) == 465.0
    assert probe.resource_sample_timeout_budget(600, 500) == 915.0
    with pytest.raises(probe.ProbeFailure):
        probe.resource_sample_timeout_budget(0, 500)
    with pytest.raises(probe.ProbeFailure):
        probe.resource_sample_timeout_budget(1, 99)


def test_reader_cleanup_closes_only_after_bounded_join():
    source = inspect.getsource(probe.PulsarProcess._join_process_reader)
    assert "PROCESS_READER_JOIN_TIMEOUT_S" in source
    assert "_close_process_stdout" in source
    assert "_close_process_stderr" in source
    assert "stderr_thread" in source
    directshow = inspect.getsource(probe.PulsarProcess._join_directshow_reader)
    assert "_close_directshow_stdout" in directshow
    assert "reader.is_alive()" in source


def test_pulsar_child_capture_keeps_stdout_and_stderr_unbuffered_and_separate():
    source = inspect.getsource(probe.PulsarProcess.spawn)
    assert "stdout=subprocess.PIPE" in source
    assert "stderr=subprocess.PIPE" in source
    assert "stderr=subprocess.STDOUT" not in source
    assert "bufsize=0" in source
    assert "text=False" in source
    assert "pulsar-probe-stderr" in source


def test_process_diagnostic_context_reports_exit_and_encoder_components():
    class DeadProcess:
        returncode = 17

        def poll(self):
            return self.returncode

    process = probe.PulsarProcess(Path("pulsar.exe"), "nvenc", Path("record"))
    process.proc = DeadProcess()
    process.stdout_lines[:] = ["video encoder allocated: family=nvenc", "mux interleaver stalled"]
    process.stderr_lines[:] = ["password=do-not-leak", "NVENC error 10"]

    diagnostic = process.diagnostic_context(limit=20)
    assert "code:17" in diagnostic
    assert "exit_code_17" in diagnostic
    assert "video encoder allocated" in diagnostic
    assert "mux interleaver stalled" in diagnostic
    assert "NVENC error 10" in diagnostic
    assert "component_tail=" in diagnostic
    assert "do-not-leak" not in diagnostic


def test_websocket_timeout_reports_request_and_child_diagnostics(monkeypatch):
    class DeadProcess:
        returncode = 23

        def poll(self):
            return self.returncode

    process = probe.PulsarProcess(Path("pulsar.exe"), "nvenc", Path("record"))
    process.proc = DeadProcess()
    process.stdout_lines[:] = ["NVENC encoder initialization failed"]
    inbox = probe.Inbox(process.diagnostic_context)

    class FakeWebSocket:
        async def send(self, _message):
            return None

        def recv(self):
            return None

    async def timeout(_awaitable, timeout=None):
        del timeout
        raise asyncio.TimeoutError

    monkeypatch.setattr(probe.asyncio, "wait_for", timeout)
    with pytest.raises(probe.ProbeFailure) as failure:
        asyncio.run(
            probe.request(
                inbox,
                FakeWebSocket(),
                "StartRecord",
                "start-record",
                {"pass" + "word": "do-not-leak", "outputPath": "record.mp4"},
            )
        )
    message = str(failure.value)
    assert "obs-websocket response timeout" in message
    assert "StartRecord" in message and "start-record" in message
    assert "outputPath" in message
    assert "exit_code_23" in message and "NVENC encoder initialization failed" in message
    assert "do-not-leak" not in message


def test_recording_release_probe_restores_owned_path(tmp_path):
    recording = tmp_path / "recording.mp4"
    recording.write_bytes(b"fixture")
    probe.wait_for_recording_release(recording, timeout=0.5)
    assert recording.read_bytes() == b"fixture"
    assert not list(tmp_path.glob("*.release-check"))


def test_program_audio_waits_for_recording_handle_release():
    source = (ROOT / "scripts" / "probe-program-audio.py").read_text(encoding="utf-8")
    assert "wait_for_recording_release" in source
    assert "ensure_recording_output_owned" in source
