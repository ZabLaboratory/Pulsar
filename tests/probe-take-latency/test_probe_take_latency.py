"""Regression tests for the #246 latency-evidence parser.

These tests use deliberately small ``fixture`` campaigns and lower the
minimum count so they exercise the parser without pretending to be runtime
acceptance evidence.  A fixture can never produce report status ``PASS``.
The real acceptance command keeps the defaults at 100 warm Takes and 100
measurements per required boundary.
"""

from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe-take-latency.py"
SPEC = importlib.util.spec_from_file_location("probe_take_latency", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


RUNTIME = "runtime-fixture-001"
ROLE_MAP = {"on_air": "A", "preview": "B"}


def _event_common(command_id: str, intent_id: str, seq: int, observed: int, revisions: dict[str, int], state: str) -> dict:
    return {
        "contract": "pulsar.scene-switch.v1",
        "schema_version": 1,
        "message_type": "event",
        "command_id": command_id,
        "intent_id": intent_id,
        "runtime_instance_id": RUNTIME,
        "server_seq": seq,
        "state": state,
        "previous_revisions": deepcopy(revisions),
        "revisions": deepcopy(revisions),
        "role_map": deepcopy(ROLE_MAP),
        "observed_at_monotonic_ns": observed,
        "payload_sha256": "a" * 64,
    }


def _take_records(count: int, *, evidence_kind: str = "fixture", raw_extra_ms: float = 7.0, ds_extra_ms: float = 22.0):
    session = {
        "record_type": "session",
        "schema": probe.TRACE_SCHEMA,
        "runtime_instance_id": RUNTIME,
        "session_id": "session-fixture-001",
        "codec": "nvenc",
        "warmup_takes": count,
        "video": {"width": 1920, "height": 1080, "fps_num": 60, "fps_den": 1},
        "workload": {"wgc": True, "cef": True, "nvenc": True},
        "capture_paths": list(probe.BOUNDARIES),
        "resource_reference": deepcopy(probe.RESOURCE_REFERENCE),
        "source_types": ["window_capture", "browser_source"] if evidence_kind == "runtime" else None,
        "build_revision": "f" * 40 if evidence_kind == "runtime" else "fixture-build",
        "command_line": "fixture only; never a runtime acceptance",
        "hardware": {"host": "fixture", "gpu": "fixture"},
        "producer_topology": "dual_lane_ab",
        "producer_count": 2,
        "evidence_kind": evidence_kind,
    }
    if session["source_types"] is None:
        del session["source_types"]
    records = [session]
    revisions = {"program": 0, "preview": 0, "role_map": 0}
    seq = 1
    for index in range(count):
        take_id = f"take-{index + 1:03d}"
        intent_id = f"intent-{index + 1:03d}"
        accepted_at = 1_000_000_000 + index * 100_000_000
        commit_at = accepted_at + 5_000_000
        frame_id = 100 + index
        pts_ns = 10_000_000_000 + index * 16_666_667
        accepted = _event_common(take_id, intent_id, seq, accepted_at, revisions, "take_accepted")
        accepted.update(
            {
                "event_type": "TakeAccepted",
                "take_command_id": take_id,
                "target_lane_id": "B",
                "target_scene_id": f"scene-{index + 1:03d}",
                "freeze_until_monotonic_ns": accepted_at + 1_000_000_000,
            }
        )
        records.append({"record_type": "event", "event": accepted})
        seq += 1
        committed_revisions = {"program": index + 1, "preview": 0, "role_map": index + 1}
        committed = _event_common(take_id, intent_id, seq, commit_at, revisions, "ready")
        committed.update(
            {
                "event_type": "TakeCommitted",
                "previous_revisions": deepcopy(revisions),
                "revisions": deepcopy(committed_revisions),
                "role_map": {"on_air": "B", "preview": "A"},
                "previous_role_map": deepcopy(ROLE_MAP),
                "take_command_id": take_id,
                "target_lane_id": "B",
                "target_scene_id": f"scene-{index + 1:03d}",
                "source_lane_id": "B",
                "frame_id": frame_id,
                "pts_ns": pts_ns,
                "program_lane_id": "B",
                "preview_lane_id": "A",
            }
        )
        records.append({"record_type": "event", "event": committed})
        seq += 1

        def observation(boundary: str, observed: int, frame: int, pts: int, valid: bool = True, revisions_value=None):
            surface, consumer = {
                "encoder_input_raw": ("ProgramView", "encoder_input"),
                "directshow_return": ("ProgramReturn", "DirectShow"),
                "encoded_first_packet": ("EncoderOutput", "encoder_callback"),
                "decoded_first_frame": ("RTMP", "decoder"),
                "antenna_first_frame": ("Antenna", "antenna"),
            }[boundary]
            item = {
                "record_type": "observation",
                "boundary": boundary,
                "clock_domain": "monotonic_ns",
                "runtime_instance_id": RUNTIME,
                "command_id": take_id,
                "intent_id": intent_id,
                "take_command_id": take_id,
                "revisions": deepcopy(revisions_value if revisions_value is not None else committed_revisions),
                "frame_id": frame,
                "pts_ns": pts,
                "observed_at_monotonic_ns": observed,
                "valid": valid,
                "surface": surface,
                "consumer": consumer,
            }
            if boundary in ("encoder_input_raw", "directshow_return"):
                item["program_frame"] = valid
            if boundary == "encoded_first_packet":
                item["packet_index"] = 0
            return {"record_type": "observation", **item}

        # A valid frame before the atomic commit is intentionally ignored.
        records.append(observation("encoder_input_raw", accepted_at + 1_000_000, frame_id - 1, pts_ns - 1))
        records.append(observation("encoder_input_raw", commit_at + int(raw_extra_ms * 1_000_000), frame_id, pts_ns))
        records.append(observation("directshow_return", commit_at + int(ds_extra_ms * 1_000_000), frame_id + 1, pts_ns + 1))
        records.append(observation("encoded_first_packet", commit_at + 3_000_000, frame_id, pts_ns))
        records.append(observation("decoded_first_frame", commit_at + 100_000_000, frame_id + 2, pts_ns + 2))
        records.append(observation("antenna_first_frame", commit_at + 120_000_000, frame_id + 3, pts_ns + 3))
        revisions = committed_revisions

    # Resource values are deterministic and include both modes.  The delta is
    # intentionally below the known reference so the comparison can be tested.
    for mode, render, resident in (
        ("reference", 1.000, 100_000_000),
        ("dual_lane", 1.080, 103_000_000),
    ):
        for sample_index in range(2):
            records.append(
                {
                    "record_type": "resource_sample",
                    "sample_mode": mode,
                    "clock_domain": "monotonic_ns",
                    "runtime_instance_id": RUNTIME,
                    "observed_at_monotonic_ns": 20_000_000_000 + sample_index * 1_000_000,
                    "measurement_phase": mode,
                    "build_revision": session["build_revision"],
                    "hardware": deepcopy(session["hardware"]),
                    "producer_topology": "single_lane_reference" if mode == "reference" else "dual_lane_ab",
                    "producer_count": 1 if mode == "reference" else 2,
                    "frame_render_ms": render + sample_index * 0.001,
                    "resident_bytes": resident + sample_index * 1000,
                    "process_cpu_percent": 15.0 + sample_index,
                    "host_gpu_percent": 25.0 + sample_index,
                    "callback_backlog_estimate": sample_index,
                    "encoder_utilization_percent": 4.0,
                }
            )
    return records


def _trace(count: int = 3, **kwargs):
    return probe.parse_records(_take_records(count, **kwargs), source="fixture.jsonl")


def test_quantile_is_deterministic_linear_interpolation():
    assert probe.quantile([0.0, 10.0], 0.95) == pytest.approx(9.5)
    assert probe.quantile([1.0, 2.0, 3.0, 4.0], 0.99) == pytest.approx(3.97)


def test_fixture_reports_all_boundaries_separately_and_never_runtime_pass():
    report = probe.analyze_trace(_trace(), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)
    assert report["status"] == "FIXTURE_ONLY"
    assert report["latency"]["encoder_input_raw"]["count"] == 3
    assert report["latency"]["directshow_return"]["count"] == 3
    assert report["latency"]["encoded_first_packet"]["count"] == 3
    assert report["latency"]["encoder_input_raw"]["p95_ms"] < 50
    assert report["latency"]["directshow_return"]["p95_ms"] < 75
    assert report["latency"]["encoded_first_packet"]["p95_ms"] < 15
    assert report["resources"]["status"] == "MEASURED"
    assert report["resources"]["comparison"]["frame_render_ms"]["within_known_reference"] is True
    assert report["resources"]["comparison"]["resident_bytes"]["within_known_reference"] is True
    assert report["ignored_valid_samples_before_commit"]["encoder_input_raw"] == 3


def test_runtime_report_can_pass_only_with_explicit_complete_evidence():
    trace = _trace(3, evidence_kind="runtime")
    report = probe.analyze_trace(trace, minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)
    assert report["status"] == "PASS"
    assert all(report["criteria"][criterion]["status"] in ("PASS", "MEASURED") for criterion in ("AC-07", "AC-08", "AC-11", "AC-12", "AC-13"))


def test_default_acceptance_threshold_is_unproven_for_small_fixture():
    report = probe.analyze_trace(_trace(), minimum_takes=100, minimum_warmup=100, minimum_resource_samples=10)
    assert report["status"] == "FIXTURE_ONLY"
    assert report["criteria"]["AC-07"]["status"] == "UNPROVEN"
    assert report["criteria"]["AC-08"]["status"] == "UNPROVEN"
    assert report["criteria"]["AC-12"]["status"] == "UNPROVEN"


def test_wrong_directshow_surface_is_rejected_instead_of_counted_as_raw():
    records = _take_records(3)
    for record in records:
        if record.get("record_type") == "observation" and record.get("boundary") == "directshow_return":
            record["surface"] = "ProgramView"
            break
    with pytest.raises(probe.EvidenceError, match="BOUNDARY_INVALID"):
        probe.parse_records(records)


def test_mismatched_observation_revision_is_rejected():
    records = _take_records(3)
    for record in records:
        if (
            record.get("record_type") == "observation"
            and record.get("boundary") == "encoder_input_raw"
            and record.get("valid")
            and record.get("observed_at_monotonic_ns", 0) > 1_005_000_000
        ):
            record["revisions"]["program"] += 99
            break
    with pytest.raises(probe.EvidenceError, match="CORRELATION_INVALID"):
        probe.analyze_trace(probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)


def test_first_valid_sample_must_be_after_commit_and_slo_violation_fails():
    records = _take_records(3, raw_extra_ms=60.0)
    report = probe.analyze_trace(probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)
    assert report["criteria"]["AC-07"]["status"] == "FAIL"
    assert report["status"] == "FAIL"


def test_missing_declared_boundary_is_unproven_and_not_pooled_from_another_path():
    records = _take_records(3)
    records = [
        record
        for record in records
        if not (record.get("record_type") == "observation" and record.get("boundary") == "directshow_return")
    ]
    report = probe.analyze_trace(probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)
    assert report["criteria"]["AC-07"]["status"] == "PASS"
    assert report["criteria"]["AC-08"]["status"] == "UNPROVEN"
    assert report["status"] == "FIXTURE_ONLY"


def test_frame_regression_and_commit_after_deadline_fail_closed():
    records = _take_records(3)
    commits = [
        item["event"]
        for item in records
        if item.get("record_type") == "event" and item["event"]["event_type"] == "TakeCommitted"
    ]
    commits[1]["frame_id"] = 50
    with pytest.raises(probe.EvidenceError, match="FRAME_ORDER_INVALID"):
        probe.analyze_trace(probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)

    records = _take_records(3)
    accepted = next(item["event"] for item in records if item.get("record_type") == "event" and item["event"]["event_type"] == "TakeAccepted")
    committed = next(item["event"] for item in records if item.get("record_type") == "event" and item["event"]["event_type"] == "TakeCommitted")
    accepted["freeze_until_monotonic_ns"] = committed["observed_at_monotonic_ns"] - 1
    with pytest.raises(probe.EvidenceError, match="committed after its acceptance deadline"):
        probe.analyze_trace(probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)


def test_resource_comparison_reports_over_reference_without_relabeling_it_as_capacity():
    records = _take_records(3)
    for record in records:
        if record.get("record_type") == "resource_sample" and record.get("sample_mode") == "dual_lane":
            record["frame_render_ms"] = 2.0
            record["resident_bytes"] = 200_000_000
    report = probe.analyze_trace(probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)
    assert report["resources"]["status"] == "MEASURED"
    assert report["resources"]["comparison"]["frame_render_ms"]["within_known_reference"] is False
    assert report["resources"]["comparison"]["resident_bytes"]["within_known_reference"] is False
    assert report["criteria"]["AC-13"]["capacity_not_declared_from_reference"] is True


def test_runtime_resource_identity_and_phase_are_not_vacuous():
    records = _take_records(3, evidence_kind="runtime")
    resource = next(record for record in records if record.get("record_type") == "resource_sample")
    resource["hardware"] = {"host": "other-host", "gpu": "fixture"}
    with pytest.raises(probe.EvidenceError, match="CORRELATION_INVALID"):
        probe.parse_records(records)

    report = probe.analyze_trace(
        _trace(0, evidence_kind="runtime"), minimum_takes=1, minimum_warmup=0, minimum_resource_samples=2
    )
    assert report["status"] == "UNPROVEN"
    assert report["event_coverage"]["take_accepted"] == 0
    assert report["criteria"]["AC-07"]["status"] == "UNPROVEN"


def test_queue_rejected_abort_exposes_last_committed_frame_and_pts():
    records = _take_records(1)
    records = [
        record
        for record in records
        if not (
            record.get("record_type") == "event"
            and record["event"]["event_type"] == "TakeCommitted"
        )
        and not (
            record.get("record_type") == "observation"
            and record.get("take_command_id") == "take-001"
        )
    ]
    accepted = next(record["event"] for record in records if record.get("record_type") == "event")
    aborted = _event_common(
        "take-001", "intent-001", 3, accepted["observed_at_monotonic_ns"] + 6_000_000,
        accepted["revisions"], "ready"
    )
    aborted.update(
        {
            "event_type": "TakeAborted",
            "take_command_id": "take-001",
            "reason": "queue_rejected",
            "last_committed_frame_id": 0,
            "last_committed_pts_ns": 0,
        }
    )
    records.insert(2, {"record_type": "event", "event": aborted})
    trace = probe.parse_records(records)
    report = probe.analyze_trace(trace, minimum_takes=1, minimum_warmup=0, minimum_resource_samples=2)
    assert report["status"] == "FIXTURE_ONLY"
    assert report["event_coverage"]["take_aborted"] == 1
    assert report["takes"]["committed"] == 0

    bad_records = deepcopy(records)
    bad_abort = next(
        record["event"]
        for record in bad_records
        if record.get("record_type") == "event" and record["event"]["event_type"] == "TakeAborted"
    )
    bad_abort["last_committed_frame_id"] = 99
    with pytest.raises(probe.EvidenceError, match="FRAME_ORDER_INVALID"):
        probe.analyze_trace(
            probe.parse_records(bad_records), minimum_takes=1, minimum_warmup=0, minimum_resource_samples=2
        )


def test_queue_rejected_abort_can_be_terminal_without_false_acceptance():
    records = _take_records(0)
    rejected = _event_common(
        "take-rejected-001", "intent-rejected-001", 1, 1_000_000_000,
        {"program": 0, "preview": 0, "role_map": 0}, "ready"
    )
    rejected.update(
        {
            "event_type": "TakeAborted",
            "take_command_id": "take-rejected-001",
            "reason": "queue_rejected",
            "last_committed_frame_id": 0,
            "last_committed_pts_ns": 0,
        }
    )
    records.insert(1, {"record_type": "event", "event": rejected})
    trace = probe.parse_records(records)
    report = probe.analyze_trace(trace, minimum_takes=1, minimum_warmup=0, minimum_resource_samples=2)
    assert report["event_coverage"]["take_accepted"] == 0
    assert report["event_coverage"]["take_aborted"] == 1
    assert report["takes"]["accepted"] == 0


def test_duplicate_server_sequence_and_duplicate_first_packet_are_not_silently_deduped():
    records = _take_records(3)
    event = next(item for item in records if item.get("record_type") == "event")
    records.insert(2, deepcopy(event))
    with pytest.raises(probe.EvidenceError, match="EVENT_ORDER_INVALID"):
        probe.analyze_trace(probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)

    records = _take_records(3)
    packet = next(item for item in records if item.get("record_type") == "observation" and item.get("boundary") == "encoded_first_packet")
    records.append(deepcopy(packet))
    with pytest.raises(probe.EvidenceError, match="more than one valid first encoded packet"):
        probe.analyze_trace(probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)


def test_unknown_record_and_malformed_trace_fail_closed(tmp_path):
    with pytest.raises(probe.EvidenceError, match="SCHEMA_INVALID"):
        probe.parse_records([{"record_type": "unknown"}])
    with pytest.raises(probe.EvidenceError, match="MALFORMED_JSON"):
        # parse_trace is the file-boundary parser; this uses a temporary file
        # so the malformed line cannot be mistaken for an absent record.
        path = tmp_path / "malformed-probe-trace.jsonl"
        path.write_text("{not-json}\n", encoding="utf-8")
        probe.parse_trace(path)


def test_multiple_campaigns_do_not_pool_codec_or_path_samples():
    first = _trace(3, evidence_kind="runtime")
    second_records = _take_records(3, evidence_kind="runtime")
    second_records[0]["session_id"] = "session-fixture-002"
    second = probe.parse_records(second_records, source="fixture-2.jsonl")
    report = probe.analyze_traces((first, second), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)
    assert report["status"] == "PASS"
    assert len(report["campaigns"]) == 2
    assert all(campaign["latency"]["encoder_input_raw"]["count"] == 3 for campaign in report["campaigns"])


def test_cli_writes_deterministic_report_and_returns_fixture_skip(tmp_path, capsys):
    path = tmp_path / "fixture.jsonl"
    output = tmp_path / "report.json"
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in _take_records(3)),
        encoding="utf-8",
    )
    rc = probe.main(
        [
            "--trace",
            str(path),
            "--output",
            str(output),
            "--min-takes",
            "3",
            "--min-warmup",
            "3",
            "--min-resource-samples",
            "2",
        ]
    )
    assert rc == 3
    assert "FIXTURE_ONLY" in capsys.readouterr().out
    first = output.read_text(encoding="utf-8")
    assert json.loads(first)["status"] == "FIXTURE_ONLY"
    # JSON key sorting and fixed indentation keep evidence diffs reviewable.
    rc = probe.main(
        [
            "--trace",
            str(path),
            "--output",
            str(output),
            "--min-takes",
            "3",
            "--min-warmup",
            "3",
            "--min-resource-samples",
            "2",
        ]
    )
    assert rc == 3
    assert output.read_text(encoding="utf-8") == first
