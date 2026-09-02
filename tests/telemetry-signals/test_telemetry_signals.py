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
            for name in ("encoder_frame_ready", "encode_callback_enqueue", "output_mux_enqueue"):
                records.append(_signal(event, name, 2_000_000))
    trace = probe.parse_records(records, source="signal-fixture.jsonl")
    report = analyzer.analyze_trace(trace)
    assert report["stages"]["encoder_frame_ready"]["count"] == 3
    assert report["stages"]["output_mux_enqueue"]["p95_ms"] == pytest.approx(1.0)
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
