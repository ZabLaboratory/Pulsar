"""Durable shape guards for Probe-5's #247 reproduction record."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "probe-247-runtime.py"
EVIDENCE = ROOT / "docs" / "evidence" / "247" / "exact-run.json"
TRACE_SUMMARY = ROOT / "docs" / "evidence" / "247" / "trace-summary.json"


def load_harness_module():
    spec = importlib.util.spec_from_file_location("probe247_runtime", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe247_harness_retains_the_independent_runtime_campaigns() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for flag in ("--cache-pressure", "--abort-race", "--prepare-timeout", "--freeze-race"):
        assert flag in source
    assert "Draft202012Validator" in source
    assert "if cycles >= 100 and sent < 1000" in source
    assert "kMaxOutcomes" not in source  # the public GetState observation is the evidence.


def test_probe247_vendor_event_matcher_uses_the_obs_v5_nested_envelope() -> None:
    harness = load_harness_module()
    event = {
        "eventType": "VendorEvent",
        "eventData": {
            "vendorName": "pulsar-scene-switch",
            "eventType": "VendorEvent",
            "eventData": {"event_type": "TakeCommitted", "command_id": "take-1"},
        },
    }
    assert harness.is_scene_switch_vendor_event(event) is True
    assert harness.is_scene_switch_vendor_event({**event, "eventData": {"vendorName": "other"}}) is False
    assert harness.is_scene_switch_vendor_event({"eventType": "VendorEvent", "vendorName": "pulsar-scene-switch"}) is False


def test_probe247_exact_record_is_compact_and_does_not_overclaim_delivery() -> None:
    record = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert record["issue"] == 247
    assert record["campaign"]["cycles"] == 100
    assert record["campaign"]["vendor_attempts"] >= 1000
    assert record["campaign"]["cache_capacity"] == 4096
    assert record["not_measured"] == ["directshow", "rtmp", "decoded", "antenna"]
    assert len(record["artifact"]["sha256"]) == 64
    assert len(record["campaign"]["trace_sha256"]) == 64


def test_probe247_compact_trace_summary_preserves_auditable_invariants() -> None:
    summary = json.loads(TRACE_SUMMARY.read_text(encoding="utf-8"))
    assert summary["derivation"]["trace_records"] == 203
    assert summary["vendor_campaign"]["real_call_vendor_request_transmissions"] == 1700
    assert len(summary["vendor_campaign"]["command_result_matrix"]) >= 10
    one_commit = summary["exactly_one_commit"]
    assert one_commit["accepted_intents"] == 100
    assert one_commit["take_committed_events"] == 100
    assert one_commit["unique_committed_intents"] == 100
    assert one_commit["unsettled_take_ids"] == 0
    assert one_commit["strictly_monotonic_commit_frame_and_pts"] is True
    samples = summary["commit_samples"]
    assert [sample["take_command_id"] for sample in samples] == [
        "take-001", "take-025", "take-050", "take-075", "take-100"
    ]
    assert all(sample["frame_id"] > 0 and sample["pts_ns"] > 0 for sample in samples)
    assert all(sample["role_map"]["on_air"] == sample["program_lane_id"] for sample in samples)
    assert all(sample["role_map"]["preview"] == sample["preview_lane_id"] for sample in samples)
    pixels = summary["route_and_pixel_evidence"]["wgc_cef_strict_no_blank"]
    assert pixels["allow_blank"] is False
    assert all(pixels[source]["nonblack_fraction"] == 1.0 for source in ("wgc_a", "cef_a", "wgc_b", "cef_b"))
    hashes = summary["route_and_pixel_evidence"]["frame_hashes"]
    assert hashes["scope"].startswith("CEF-only")
    samples = hashes["sampled_boundaries"]
    assert len(samples) == 3
    assert samples[0]["program_sha256"] != samples[0]["preview_sha256"]
    assert samples[1]["program_sha256"] == samples[0]["preview_sha256"]
