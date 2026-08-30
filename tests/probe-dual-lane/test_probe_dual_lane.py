"""Contract tests for the AC-12 RTMP receiver harness."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import subprocess
import sys

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
    assert "minimum_resource_samples=args.resource_samples" in run_source
    assert "minimum_rtmp_samples=args.resource_samples" in run_source
    wait_index = drive_source.index("wait_for_eligible_resource_samples")
    stream_stop_index = drive_source.index("await process.stop_rtmp_stream", wait_index)
    record_stop_index = drive_source.index('request(inbox, ws, "StopRecord"', wait_index)
    assert wait_index < stream_stop_index < record_stop_index

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
            "packet_index": 7,
            "packet_pts": 0,
            "packet_dts": 0,
            "packet_pts_time_ms": 0,
            "packet_dts_time_ms": 0,
            "observed_at_monotonic_ns": 1_020_000_000,
            "packet_identity": "runtime-fixture-001-nvenc-video-7-0-0",
        }
    ]
    receiver.fuse_trace(producer, final)
    fused = [json.loads(line) for line in final.read_text(encoding="utf-8").splitlines()]
    rtmp = [record for record in fused if record.get("boundary") == "rtmp_first_packet"]
    assert len(rtmp) == 1
    assert rtmp[0]["packet_identity"] == receiver.packets[0]["packet_identity"]
    assert rtmp[0]["surface"] == "RTMP"
    assert rtmp[0]["consumer"] == "receiver"


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
            "packet_index": 7,
            "packet_pts": 0,
            "packet_dts": 0,
            "packet_pts_time_ms": 0,
            "packet_dts_time_ms": 0,
            "observed_at_monotonic_ns": 1_020_000_000,
            "packet_identity": "runtime-fixture-001-nvenc-video-7-0-0",
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
