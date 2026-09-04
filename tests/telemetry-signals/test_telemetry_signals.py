"""Strict contract tests for the non-blocking encoder signal ledger."""

from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


probe = _load("telemetry_probe_contract", ROOT / "scripts" / "probe-take-latency.py")
analyzer = _load("telemetry_signal_analyzer", ROOT / "scripts" / "analyze-telemetry-signals.py")
fixture = _load("telemetry_probe_fixture", ROOT / "tests" / "probe-take-latency" / "test_probe_take_latency.py")


def _signal(event: dict, name: str, offset: int) -> dict:
    return {
        "record_type": "telemetry_signal",
        "signal": name,
        "clock_domain": "monotonic_ns",
        "runtime_instance_id": event["runtime_instance_id"],
        "command_id": event["command_id"],
        "intent_id": event["intent_id"],
        "take_command_id": event["take_command_id"],
        "revisions": deepcopy(event["revisions"]),
        "frame_id": event["frame_id"],
        "pts_ns": event["pts_ns"],
        "observed_at_monotonic_ns": event["observed_at_monotonic_ns"] + offset + 1_000_000,
        "start_monotonic_ns": event["observed_at_monotonic_ns"] + offset,
        "end_monotonic_ns": event["observed_at_monotonic_ns"] + offset + 1_000_000,
        "valid": True,
    }


def test_signal_stages_are_correlated_and_reported_as_percentiles():
    records = fixture._take_records(3)
    records[0]["telemetry_signals"] = list(analyzer.SIGNALS)
    for record in list(records):
        event = record.get("event")
        if event and event.get("event_type") == "TakeCommitted":
            for name in (
                "encoder_frame_ready",
                "encode_callback_enqueue",
                "output_mux_enqueue",
                "interleaver_mutex_wait",
            ):
                records.append(_signal(event, name, 2_000_000))
    trace = probe.parse_records(records, source="signal-fixture.jsonl")
    report = analyzer.analyze_trace(trace)
    assert report["stages"]["encoder_frame_ready"]["count"] == 3
    assert report["stages"]["output_mux_enqueue"]["p95_ms"] == pytest.approx(1.0)
    assert report["stages"]["interleaver_mutex_wait"]["p95_ms"] == pytest.approx(1.0)
    assert report["stages"]["program_return_readback"]["count"] == 3
    assert report["stages"]["socket_send"]["status"] == "NOT_AVAILABLE"


def test_signal_context_mismatch_fails_closed():
    records = fixture._take_records(1)
    records[0]["telemetry_signals"] = ["encoder_frame_ready"]
    committed = next(
        record["event"]
        for record in records
        if record.get("event", {}).get("event_type") == "TakeCommitted"
    )
    mismatched = _signal(committed, "encoder_frame_ready", 2_000_000)
    mismatched["frame_id"] += 1
    records.append(mismatched)
    with pytest.raises(probe.EvidenceError, match="telemetry signal does not match"):
        probe.parse_records(records)


def test_frontend_callback_contains_no_synchronous_json_or_state_lock():
    source = (ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp").read_text(
        encoding="utf-8"
    )
    callback = source[source.index("    void rawFrame("):source.index("    void snapshot(")]
    assert "std::lock_guard" not in callback
    assert "std::ostringstream" not in callback
    assert "enqueueSignal" in callback
    assert "PULSAR_TRACE_SIGNALS" in source
    assert "packet_output_enqueue_monotonic_ns" in source
    assert "packet_interleaver_mutex_wait_start_monotonic_ns" in source
    assert "packet_interleaver_mutex_acquired_monotonic_ns" in source


def test_selector_contract_keeps_legacy_names_and_canonical_session_field():
    source = (ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp").read_text(
        encoding="utf-8"
    )
    header = (ROOT / "plugins" / "pulsar-frontend-stub" / "include" / "pulsar-runtime-telemetry-signals.h").read_text(
        encoding="utf-8"
    )
    for name in ("program", "preview", "raw", "borrowed", "gpu", "queues") + analyzer.SIGNALS:
        assert f'"{name}"' in header
    assert "telemetry_signals" in source
    assert "trace_signals" not in source


def test_runtime_callback_registration_is_selector_gated():
    source = (ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp").read_text(
        encoding="utf-8"
    )
    install = source[source.index("if (g_runtimeTelemetry.enabled())") : source.index("return true;", source.index("if (g_runtimeTelemetry.enabled())"))]
    assert "const bool rawCallbackRequired = g_runtimeTelemetry.rawCallbackRequired();" in install
    assert "const bool packetCallbackRequired = g_runtimeTelemetry.packetCallbackRequired();" in install
    assert "if (rawCallbackRequired)" in install
    assert "if (packetCallbackRequired && streamOutput)" in install
    assert "if (streamOutput)\n                obs_output_add_packet_callback" not in install
    assert "signalMask_.store(0, std::memory_order_release);" in source
    assert "Signal::EncoderFrameReady" in source
    assert "Signal::OutputMuxEnqueue" in source
    assert "Signal::InterleaverMutexWait" in source
    assert "std::atomic<uint32_t> signalMask_{0};" in source
    assert "mutable std::atomic<bool> rawCaptured" in source
    assert "mutable std::atomic<bool> packetCaptured" in source


def test_callback_selector_cases_are_fail_closed_and_stage_specific():
    source = (ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp").read_text(
        encoding="utf-8"
    )
    header = (ROOT / "plugins" / "pulsar-frontend-stub" / "include" / "pulsar-runtime-telemetry-signals.h").read_text(
        encoding="utf-8"
    )
    raw_method = source[source.index("bool rawCallbackRequired") : source.index("bool packetCallbackRequired")]
    packet_method = source[source.index("bool packetCallbackRequired") : source.index("void updateMixRoots")]
    # none: initialization clears the atomic mask, so both registration
    # predicates are false before the enabled() installation block.
    assert "signalMask_.store(0, std::memory_order_release);" in source
    assert "result.mask = 0;" in header
    # stage-only: packet registration includes each encoder stage, while raw
    # registration is owned exclusively by the established Program selector.
    assert "Signal::EncoderFrameReady" in packet_method
    assert "Signal::EncodeCallbackEnqueue" in packet_method
    assert "Signal::OutputMuxEnqueue" in packet_method
    assert "Signal::InterleaverMutexWait" in packet_method
    assert "Signal::Program" in raw_method
    assert "Signal::Program" in packet_method
    # all: the selector header expands all bits, and installation is guarded
    # only by the two required predicates.
    assert "result.mask = all_signal_mask();" in header
    assert "if (rawCallbackRequired)" in source
    assert "if (packetCallbackRequired && streamOutput)" in source


def test_trace_writer_surfaces_io_fault_and_stops_accepting_evidence():
    source = (ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp").read_text(
        encoding="utf-8"
    )
    assert "integrity_fault=1 reason=trace_write_failed" in source
    assert "output.flush();" in source
    assert "success = success && !output.fail();" in source
    writer = source[source.index("void traceWriterLoop()"):source.index("bool enqueueLine", source.index("void traceWriterLoop()"))]
    assert "writerAccepting_ = false;" in writer
    assert "writerQueue_.clear();" in writer
    assert "signalMask_.store(0, std::memory_order_release);" in writer


def test_trace_writer_queue_is_bounded_and_overflow_fails_closed():
    source = (ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp").read_text(
        encoding="utf-8"
    )
    assert "kWriterQueueCapacity = 4096" in source
    enqueue = source[source.index("bool enqueueLine"):source.index("void writeLine", source.index("bool enqueueLine"))]
    assert "writerQueue_.size() >= kWriterQueueCapacity" in enqueue
    assert "traceIntegrityFault_.compare_exchange_strong" in enqueue
    assert "reason=trace_queue_overflow" in enqueue
    assert "writerAccepting_ = false;" in enqueue
    assert "writerStopping_ = true;" in enqueue
    assert "return false;" in enqueue
    assert "traceIntegrityFault_.store(false, std::memory_order_release);" in source
    integrity = source[source.index("bool integrityFaulted()"):source.index("bool environmentTruthy", source.index("bool integrityFaulted()"))]
    assert "degraded_ || traceIntegrityFault_.load(std::memory_order_acquire)" in integrity


def test_callback_backlog_is_an_interval_baseline_with_session_reset():
    source = (ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp").read_text(
        encoding="utf-8"
    )
    helper_start = source.index("static uint64_t callbackBacklogEstimate")
    helper_end = source.index("template <typename T> struct StubCallback", helper_start)
    helper = source[helper_start:helper_end]
    assert "CounterBacklogBaseline" in helper
    assert "if (!sampleEligible)" in helper
    assert "baseline = {};" in helper
    assert "if (!baseline.valid || rawFrames < baseline.rawFrames || encodedFrames < baseline.encodedFrames)" in helper
    assert "return rawDelta > encodedDelta ? rawDelta - encodedDelta : 0;" in helper
    assert "counterBacklogBaseline = {};" in source
    assert "counterBaselineRuntime != runtime || counterBaselineMode != mode" in source
    assert "callbackBacklogEstimate(" in source

    # Contract model: first active sample and every inactive/counter-reset
    # transition are unmeasured (zero); only monotone consecutive active
    # counters contribute, with encoded work faster than raw clamped to zero.
    baseline = None
    session = None

    def estimate(raw, encoded, active, current_session="session-a"):
        nonlocal baseline, session
        if current_session != session:
            baseline = None
            session = current_session
        if not active or baseline is None or raw < baseline[0] or encoded < baseline[1]:
            baseline = (raw, encoded) if active else None
            return 0
        result = max((raw - baseline[0]) - (encoded - baseline[1]), 0)
        baseline = (raw, encoded)
        return result

    assert estimate(100, 100, True) == 0
    assert estimate(160, 140, True) == 20
    assert estimate(220, 260, True) == 0
    assert estimate(0, 0, False) == 0
    assert estimate(9, 4, True) == 0
    assert estimate(14, 7, True) == 2
    assert estimate(20, 9, True, "session-b") == 0
    assert estimate(30, 14, True, "session-b") == 1
