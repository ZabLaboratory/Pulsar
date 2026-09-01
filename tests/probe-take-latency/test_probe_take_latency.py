"""Regression tests for the #246 latency-evidence parser.

These tests use deliberately small ``fixture`` campaigns and lower the
minimum count so they exercise the parser without pretending to be runtime
acceptance evidence.  A fixture can never produce report status ``PASS``.
The real acceptance command keeps the defaults at 100 warm Takes and 100
measurements per required boundary.
"""

from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe-take-latency.py"
TELEMETRY_PATCH = Path(__file__).resolve().parents[2] / "patches" / "0011-feat-runtime-telemetry-producer.patch"
SPEC = importlib.util.spec_from_file_location("probe_take_latency", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


RUNTIME = "runtime-fixture-001"
ROLE_MAP = {"on_air": "A", "preview": "B"}


def _event_common(
    command_id: str,
    intent_id: str,
    seq: int,
    observed: int,
    revisions: dict[str, int],
    state: str,
    *,
    runtime_id: str = RUNTIME,
) -> dict:
    return {
        "contract": "pulsar.scene-switch.v1",
        "schema_version": 1,
        "message_type": "event",
        "command_id": command_id,
        "intent_id": intent_id,
        "runtime_instance_id": runtime_id,
        "server_seq": seq,
        "state": state,
        "previous_revisions": deepcopy(revisions),
        "revisions": deepcopy(revisions),
        "role_map": deepcopy(ROLE_MAP),
        "observed_at_monotonic_ns": observed,
        "payload_sha256": "a" * 64,
    }


def _take_records(
    count: int,
    *,
    evidence_kind: str = "fixture",
    raw_extra_ms: float = 7.0,
    ds_extra_ms: float = 22.0,
    codec: str = "nvenc",
    runtime_id: str = RUNTIME,
    include_resources: bool = True,
    resource_encoder_active: bool = True,
):
    session = {
        "record_type": "session",
        "schema": probe.TRACE_SCHEMA,
        "runtime_instance_id": runtime_id,
        "session_id": f"session-{runtime_id}",
        "codec": codec,
        # Runtime fixtures model the same two-phase campaign as the real
        # driver: an observed warm-up prefix followed by measured Takes.
        # Small fixture-only traces keep the historical compact shape so the
        # tests can focus on parser invariants without fabricating a capacity
        # claim.
        "warmup_takes": count if evidence_kind == "runtime" else 0,
        "video": {"width": 1920, "height": 1080, "fps_num": 60, "fps_den": 1},
        "workload": {"wgc": True, "cef": True, "nvenc": codec == "nvenc"},
        "capture_paths": list(probe.BOUNDARIES),
        "resource_reference": deepcopy(probe.RESOURCE_REFERENCE),
        "rtmp_receiver": {
            "server_url": "rtmp://127.0.0.1:19350/pulsar",
            "stream_key": f"stream-{runtime_id}",
            "endpoint": f"rtmp://127.0.0.1:19350/pulsar/stream-{runtime_id}",
            "receiver_id": "ffmpeg-receiver",
            "stream_id": f"stream-{runtime_id}",
            "clock_source": "perf_counter_ns/qpc",
            "clock_offset_ns": 0,
            "clock_bound_ns": 5_000_000,
            "packet_timebase_num": 1,
            "packet_timebase_den": 1000,
        },
        "rtmp_load_requested": True,
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
    warmup_count = count if evidence_kind == "runtime" else 0
    total_count = warmup_count + count
    for index in range(total_count):
        take_id = f"take-{index + 1:03d}"
        intent_id = f"intent-{index + 1:03d}"
        accepted_at = 1_000_000_000 + index * 100_000_000
        commit_at = accepted_at + 5_000_000
        frame_id = 100 + index
        pts_ns = 10_000_000_000 + index * 16_666_667
        accepted = _event_common(
            take_id,
            intent_id,
            seq,
            accepted_at,
            revisions,
            "take_accepted",
            runtime_id=runtime_id,
        )
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
        committed = _event_common(
            take_id,
            intent_id,
            seq,
            commit_at,
            revisions,
            "ready",
            runtime_id=runtime_id,
        )
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
                "rtmp_first_packet": ("RTMP", "receiver"),
                "decoded_first_frame": ("RTMP", "decoder"),
                "antenna_first_frame": ("Antenna", "antenna"),
            }[boundary]
            item = {
                "record_type": "observation",
                "boundary": boundary,
                "clock_domain": "monotonic_ns",
                "runtime_instance_id": runtime_id,
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
            if boundary == "directshow_return":
                item.update(
                    {
                        "frame_entry_monotonic_ns": observed - 5_000_000,
                        "lock_sample_data_acquired_monotonic_ns": observed - 4_000_000,
                        "queue_read_start_monotonic_ns": observed - 3_000_000,
                        "queue_read_completed_monotonic_ns": observed - 2_000_000,
                        "unlock_sample_data_completed_monotonic_ns": observed,
                        "emission_monotonic_ns": observed + 1_000_000,
                    }
                )
            if boundary == "encoded_first_packet":
                item.update(
                    {
                        "packet_index": index,
                        "packet_pts": index,
                        "packet_dts": index,
                        "packet_timebase_num": 1,
                        "packet_timebase_den": 60,
                        "packet_cts_monotonic_ns": observed - 6_000_000,
                        "packet_fer_monotonic_ns": observed - 5_000_000,
                        "packet_ferc_monotonic_ns": observed - 2_000_000,
                        "packet_pir_monotonic_ns": observed,
                        "packet_callback_monotonic_ns": observed + 100_000,
                    }
                )
            if boundary == "rtmp_first_packet":
                # RTMP/FLV timestamps are millisecond ticks.  Round to the
                # nearest tick so the parser exercises its documented
                # half-receiver-tick rational matching rule.
                receiver_pts = 1500 + round(index * 1000 / 60)
                item.update(
                    {
                        "packet_index": index,
                        "packet_pts": receiver_pts,
                        "packet_dts": receiver_pts,
                        "packet_timebase_num": 1,
                        "packet_timebase_den": 1000,
                        "packet_identity": f"stream-{runtime_id}-video-{index}",
                        "clock_source": "perf_counter_ns/qpc",
                        "clock_offset_ns": 0,
                        "clock_bound_ns": 5_000_000,
                        "receiver_observed_normalized_ns": observed,
                    }
                )
            return {"record_type": "observation", **item}

        # A valid frame before the atomic commit is intentionally ignored.
        records.append(observation("encoder_input_raw", accepted_at + 1_000_000, frame_id - 1, pts_ns - 1))
        records.append(observation("encoder_input_raw", commit_at + int(raw_extra_ms * 1_000_000), frame_id, pts_ns))
        records.append(observation("directshow_return", commit_at + int(ds_extra_ms * 1_000_000), frame_id + 1, pts_ns + 1))
        records.append(observation("encoded_first_packet", commit_at + 3_000_000, frame_id, pts_ns))
        records.append(observation("rtmp_first_packet", commit_at + 5_000_000, frame_id, pts_ns))
        records.append(observation("decoded_first_frame", commit_at + 100_000_000, frame_id + 2, pts_ns + 2))
        records.append(observation("antenna_first_frame", commit_at + 120_000_000, frame_id + 3, pts_ns + 3))
        revisions = committed_revisions

    encoded_packets = [
        record for record in records if record.get("record_type") == "observation" and record.get("boundary") == "encoded_first_packet"
    ]
    receiver_packets = [
        record for record in records if record.get("record_type") == "observation" and record.get("boundary") == "rtmp_first_packet"
    ]
    if receiver_packets:
        lower: Fraction | None = None
        upper: Fraction | None = None
        for encoded, receiver_packet in zip(encoded_packets, receiver_packets, strict=True):
            producer_pts = Fraction(
                encoded["packet_pts"] * encoded["packet_timebase_num"],
                encoded["packet_timebase_den"],
            )
            receiver_pts = Fraction(
                receiver_packet["packet_pts"] * receiver_packet["packet_timebase_num"],
                receiver_packet["packet_timebase_den"],
            )
            half_tick = Fraction(
                receiver_packet["packet_timebase_num"],
                2 * receiver_packet["packet_timebase_den"],
            )
            pair_lower = receiver_pts - producer_pts - half_tick
            pair_upper = receiver_pts - producer_pts + half_tick
            lower = pair_lower if lower is None else max(lower, pair_lower)
            upper = pair_upper if upper is None else min(upper, pair_upper)
        assert lower is not None and upper is not None and lower <= upper
        session["rtmp_receiver"].update(
            {
                "correlation_method": "packet_index_constant_mux_offset_v1",
                "mux_offset_min_num": lower.numerator,
                "mux_offset_min_den": lower.denominator,
                "mux_offset_max_num": upper.numerator,
                "mux_offset_max_den": upper.denominator,
                "correlated_packet_count": len(receiver_packets),
            }
        )
    else:
        session["capture_paths"].remove("rtmp_first_packet")

    if include_resources:
        # Resource values are deterministic and include both modes.  The
        # delta is intentionally below the known reference so the comparison
        # can be tested.  x264 samples, if requested by a negative test, are
        # never admissible evidence for AC-13.
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
                        "runtime_instance_id": runtime_id,
                        "observed_at_monotonic_ns": 20_000_000_000 + sample_index * 1_000_000,
                        "measurement_phase": mode,
                        "build_revision": session["build_revision"],
                        "hardware": deepcopy(session["hardware"]),
                        "producer_topology": "single_lane_reference" if mode == "reference" else "dual_lane_ab",
                        "producer_count": 1 if mode == "reference" else 2,
                        "encoder_active": resource_encoder_active,
                        "encoder_family": codec,
                        "rtmp_load_active": True,
                        "frame_render_ms": render + sample_index * 0.001,
                        "resident_bytes": resident + sample_index * 1000,
                        "process_cpu_percent": 15.0 + sample_index,
                        "host_gpu_percent": 25.0 + sample_index,
                        "callback_backlog_estimate": sample_index,
                        "dropped_frames": sample_index,
                        "missed_frames": sample_index,
                        "encode_time_ms": 1.5 + sample_index * 0.1,
                        "encode_time_samples": 100 + sample_index,
                        "encoder_utilization_percent": 4.0,
                        "pipeline": {
                            "tick_sources_ms": 0.20 + sample_index * 0.01,
                            "output_frames_ms": render + sample_index * 0.001,
                            "render_displays_ms": 0.02,
                            "graphics_tasks_ms": 0.01,
                            "frame_total_ms": render + 0.25 + sample_index * 0.001,
                        },
                        "program_mix": {
                            "width": 1920,
                            "height": 1080,
                            "fps_num": 60,
                            "fps_den": 1,
                            "render_submit_ms": 0.50 + sample_index * 0.01,
                            "render_setup_ms": 0.0,
                            "render_main_ms": 0.20 + sample_index * 0.005,
                            "render_scale_ms": 0.02,
                            "render_convert_ms": 0.15,
                            "gpu_flush_ms": 0.0,
                            "gpu_encode_submit_ms": 0.05,
                            "raw_stage_ms": 0.06,
                            "render_teardown_ms": 0.0,
                            "render_unattributed_ms": 0.02 + sample_index * 0.005,
                            "download_ms": 0.20,
                            "flush_ms": 0.05,
                            "output_copy_ms": 0.10,
                            "borrowed_schedule_ms": 0.0,
                            "borrowed_publish_ms": 0.0,
                            "borrowed_wait_ms": 0.0,
                            "return_output_callback_ms": 0.08,
                            "frame_unattributed_ms": 0.05,
                            "frame_total_ms": 0.90 + sample_index * 0.01,
                        },
                        "preview_mix": {
                            "active": mode == "dual_lane",
                            "width": 1920,
                            "height": 1080,
                            "fps_num": 60,
                            "fps_den": 1,
                            "render_submit_ms": 0.35 if mode == "dual_lane" else 0.0,
                            "render_setup_ms": 0.0,
                            "render_main_ms": 0.15 if mode == "dual_lane" else 0.0,
                            "render_scale_ms": 0.02 if mode == "dual_lane" else 0.0,
                            "render_convert_ms": 0.10 if mode == "dual_lane" else 0.0,
                            "gpu_flush_ms": 0.0,
                            "gpu_encode_submit_ms": 0.0,
                            "raw_stage_ms": 0.06 if mode == "dual_lane" else 0.0,
                            "render_teardown_ms": 0.0,
                            "render_unattributed_ms": 0.02 if mode == "dual_lane" else 0.0,
                            "download_ms": 0.18 if mode == "dual_lane" else 0.0,
                            "flush_ms": 0.04 if mode == "dual_lane" else 0.0,
                            "output_copy_ms": 0.09 if mode == "dual_lane" else 0.0,
                            "borrowed_schedule_ms": 0.001 if mode == "dual_lane" else 0.0,
                            "borrowed_publish_ms": 0.45 if mode == "dual_lane" else 0.0,
                            "borrowed_wait_ms": 0.01 if mode == "dual_lane" else 0.0,
                            "return_output_callback_ms": 0.43 if mode == "dual_lane" else 0.0,
                            "frame_unattributed_ms": 0.039 if mode == "dual_lane" else 0.0,
                            "frame_total_ms": 0.70 if mode == "dual_lane" else 0.0,
                        },
                        "source_profile": {
                            "program_valid": True,
                            "program_tick_cpu_ms": 0.08,
                            "program_render_cpu_ms": 0.40,
                            "program_render_gpu_ms": 0.55,
                            "preview_valid": mode == "dual_lane",
                            "preview_tick_cpu_ms": 0.06 if mode == "dual_lane" else 0.0,
                            "preview_render_cpu_ms": 0.30 if mode == "dual_lane" else 0.0,
                            "preview_render_gpu_ms": 0.45 if mode == "dual_lane" else 0.0,
                        },
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
    assert report["criteria"]["AC-12"]["ac12b"]["count"] == 3
    assert report["resources"]["status"] == "MEASURED"
    assert report["resources"]["accounting_status"] == "COMPLETE"
    assert report["resources"]["accounting"]["dual_lane"]["program_mix"]["status"] == "COMPLETE"
    assert report["resources"]["accounting"]["dual_lane"]["preview_mix"]["status"] == "COMPLETE"
    assert report["resources"]["comparison"]["frame_render_ms"]["within_known_reference"] is True
    assert report["resources"]["comparison"]["resident_bytes"]["within_known_reference"] is True
    assert report["resources"]["metrics"]["dual_lane"]["dropped_frames"]["p50"] == pytest.approx(0.5)
    assert report["resources"]["metrics"]["dual_lane"]["missed_frames"]["p95"] == pytest.approx(0.95)
    assert report["resources"]["metrics"]["dual_lane"]["encode_time_ms"]["p95"] == pytest.approx(1.595)
    assert report["resources"]["stage_metrics"]["dual_lane"]["preview_mix"]["frame_total_ms"]["p50"] == pytest.approx(0.7)
    assert report["resources"]["comparison"]["stages"]["preview_mix"]["render_submit_ms"]["dual_lane_p50"] == pytest.approx(0.35)
    assert report["resources"]["mix_formats"]["dual_lane"]["preview"] == {
        "active": True,
        "width": 1920,
        "height": 1080,
        "fps_num": 60,
        "fps_den": 1,
    }
    assert report["ignored_valid_samples_before_commit"]["encoder_input_raw"] == 3


def test_runtime_report_can_pass_only_with_explicit_complete_evidence():
    trace = _trace(3, evidence_kind="runtime")
    report = probe.analyze_trace(trace, minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)
    assert report["status"] == "PASS"
    assert report["takes"]["warmup_takes_observed"] == 3
    assert report["takes"]["measured_takes_observed"] == 3
    assert report["takes"]["total_committed_takes"] == 6
    assert report["latency"]["encoder_input_raw"]["count"] == 3
    assert all(report["criteria"][criterion]["status"] in ("PASS", "MEASURED") for criterion in ("AC-07", "AC-08", "AC-11", "AC-12", "AC-13"))


def test_stage_accounting_surfaces_large_positive_residual_as_incomplete():
    trace = _trace(3, evidence_kind="runtime")
    for sample in trace.resources:
        if sample["sample_mode"] == "dual_lane":
            sample["preview_mix"]["frame_unattributed_ms"] = 0.5

    report = probe.analyze_trace(trace, minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)
    assert report["resources"]["status"] == "MEASURED"
    assert report["resources"]["accounting_status"] == "INCOMPLETE"
    assert report["resources"]["accounting"]["dual_lane"]["preview_mix"]["status"] == "INCOMPLETE"


def test_runtime_x264_without_resources_passes_and_marks_ac13_not_applicable():
    trace = _trace(
        3,
        evidence_kind="runtime",
        codec="x264",
        runtime_id="runtime-x264-no-resources",
        include_resources=False,
    )
    report = probe.analyze_trace(trace, minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)

    assert report["status"] == "PASS"
    assert all(
        report["criteria"][criterion]["status"] == "PASS"
        for criterion in ("AC-07", "AC-08", "AC-11", "AC-12")
    )
    assert report["criteria"]["AC-13"]["status"] == "NOT_APPLICABLE"
    assert report["criteria"]["AC-13"]["applicable"] is False
    assert report["resources"]["status"] == "NOT_APPLICABLE"
    assert "x264" in report["resources"]["reason"]


def test_runtime_x264_resource_samples_cannot_substitute_ac13():
    trace = _trace(3, evidence_kind="runtime", codec="x264", runtime_id="runtime-x264-with-resources")
    report = probe.analyze_trace(trace, minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)

    assert report["status"] == "PASS"
    assert report["criteria"]["AC-13"]["status"] == "NOT_APPLICABLE"
    assert report["resources"]["status"] == "NOT_APPLICABLE"
    assert report["resources"]["comparison"] == {}


def test_runtime_nvenc_without_reference_or_dual_resources_remains_unproven():
    trace = _trace(
        3,
        evidence_kind="runtime",
        codec="nvenc",
        runtime_id="runtime-nvenc-no-resources",
        include_resources=False,
    )
    report = probe.analyze_trace(trace, minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)

    assert report["status"] == "UNPROVEN"
    assert report["criteria"]["AC-07"]["status"] == "PASS"
    assert report["criteria"]["AC-08"]["status"] == "PASS"
    assert report["criteria"]["AC-11"]["status"] == "PASS"
    assert report["criteria"]["AC-12"]["status"] == "PASS"
    assert report["criteria"]["AC-13"]["status"] == "UNPROVEN"
    assert report["resources"]["status"] == "UNPROVEN"


def test_runtime_nvenc_resource_samples_without_active_encoder_are_not_evidence():
    trace = _trace(
        3,
        evidence_kind="runtime",
        codec="nvenc",
        runtime_id="runtime-nvenc-inactive-resources",
        resource_encoder_active=False,
    )
    report = probe.analyze_trace(trace, minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)

    assert report["status"] == "UNPROVEN"
    assert report["resources"]["sample_counts"] == {"reference": 2, "dual_lane": 2}
    assert report["resources"]["active_sample_counts"] == {"reference": 0, "dual_lane": 0}
    assert report["resources"]["inactive_sample_counts"] == {"reference": 2, "dual_lane": 2}
    assert report["criteria"]["AC-13"]["status"] == "UNPROVEN"


def test_ac13_requires_symmetric_active_rtmp_load_in_reference_and_dual_phases():
    records = _take_records(3, evidence_kind="runtime", codec="nvenc", runtime_id="runtime-nvenc-asym-rtmp")
    reference = next(
        record
        for record in records
        if record.get("record_type") == "resource_sample" and record.get("sample_mode") == "reference"
    )
    reference["rtmp_load_active"] = False
    report = probe.analyze_trace(
        probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
    )
    assert report["criteria"]["AC-13"]["status"] == "UNPROVEN"
    assert report["resources"]["rtmp_load_active_sample_counts"]["reference"] == 1
    assert report["resources"]["rtmp_load_active_sample_counts"]["dual_lane"] == 2


def test_ac13_uses_conjoint_active_rtmp_samples_and_keeps_early_false_diagnostic():
    records = _take_records(3, evidence_kind="runtime", codec="nvenc", runtime_id="runtime-nvenc-early-rtmp")
    first_by_mode = set()
    for record in records:
        if record.get("record_type") != "resource_sample":
            continue
        mode = record["sample_mode"]
        if mode not in first_by_mode:
            first_by_mode.add(mode)
            record["encoder_active"] = True
            record["encoder_family"] = "nvenc"
            record["rtmp_load_active"] = False
    report = probe.analyze_trace(
        probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=1
    )
    assert report["criteria"]["AC-13"]["status"] == "MEASURED"
    assert report["resources"]["eligible_sample_counts"] == {"reference": 1, "dual_lane": 1}
    assert report["resources"]["rtmp_eligible_sample_counts"] == {"reference": 1, "dual_lane": 1}
    assert report["resources"]["rtmp_load_active_sample_counts"] == {"reference": 1, "dual_lane": 1}


def test_runtime_nvenc_resource_samples_from_another_codec_are_not_evidence():
    records = _take_records(
        3,
        evidence_kind="runtime",
        codec="nvenc",
        runtime_id="runtime-nvenc-wrong-family",
    )
    for record in records:
        if record.get("record_type") == "resource_sample":
            record["encoder_family"] = "x264"

    trace = probe.parse_records(records)
    report = probe.analyze_trace(trace, minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)

    assert report["status"] == "UNPROVEN"
    assert report["resources"]["active_sample_counts"] == {"reference": 2, "dual_lane": 2}
    assert report["resources"]["eligible_sample_counts"] == {"reference": 0, "dual_lane": 0}
    assert report["criteria"]["AC-13"]["status"] == "UNPROVEN"


def test_legacy_resource_samples_without_encoder_attestation_remain_unproven():
    records = _take_records(
        3,
        evidence_kind="runtime",
        codec="nvenc",
        runtime_id="runtime-nvenc-legacy-resources",
    )
    for record in records:
        if record.get("record_type") == "resource_sample":
            record.pop("encoder_active", None)

    trace = probe.parse_records(records)
    report = probe.analyze_trace(trace, minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)

    assert report["status"] == "UNPROVEN"
    assert report["resources"]["sample_counts"] == {"reference": 2, "dual_lane": 2}
    assert report["resources"]["active_sample_counts"] == {"reference": 0, "dual_lane": 0}
    assert report["criteria"]["AC-13"]["status"] == "UNPROVEN"


def test_default_acceptance_threshold_is_unproven_for_small_fixture():
    report = probe.analyze_trace(_trace(), minimum_takes=100, minimum_warmup=100, minimum_resource_samples=10)
    assert report["status"] == "FIXTURE_ONLY"
    assert report["criteria"]["AC-07"]["status"] == "UNPROVEN"
    assert report["criteria"]["AC-08"]["status"] == "UNPROVEN"
    assert report["criteria"]["AC-12"]["status"] == "UNPROVEN"


def test_encoded_only_evidence_can_never_satisfy_ac12():
    records = [
        record
        for record in _take_records(3, evidence_kind="runtime", include_resources=False)
        if not (record.get("record_type") == "observation" and record.get("boundary") == "rtmp_first_packet")
    ]
    report = probe.analyze_trace(
        probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
    )
    assert report["criteria"]["AC-07"]["status"] == "PASS"
    assert report["criteria"]["AC-08"]["status"] == "PASS"
    assert report["criteria"]["AC-12"]["status"] == "UNPROVEN"
    assert report["criteria"]["AC-12"]["boundary"] == "rtmp_first_packet"
    assert report["criteria"]["AC-12"]["encoder_callback_auxiliary"]["count"] == 3
    assert report["status"] == "UNPROVEN"


def test_rtmp_receiver_requires_complete_session_and_packet_metadata():
    records = _take_records(3)
    session = records[0]
    session.pop("rtmp_receiver")
    with pytest.raises(probe.EvidenceError, match="must be present together"):
        probe.analyze_trace(
            probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
        )

    records = _take_records(3)
    encoded = next(
        item
        for item in records
        if item.get("record_type") == "observation" and item.get("boundary") == "encoded_first_packet"
    )
    encoded.pop("packet_timebase_den")
    with pytest.raises(probe.EvidenceError, match="encoded packet metadata must be complete"):
        probe.parse_records(records)


def test_encoder_pipeline_timing_is_complete_ordered_and_diagnostic_only():
    records = _take_records(3)
    encoded_packets = [
        item
        for item in records
        if item.get("record_type") == "observation"
        and item.get("boundary") == "encoded_first_packet"
    ]
    for packet in encoded_packets:
        cts = packet["observed_at_monotonic_ns"] - 6_000_000
        packet.update(
            {
                "packet_cts_monotonic_ns": cts,
                "packet_fer_monotonic_ns": cts + 1_000_000,
                "packet_ferc_monotonic_ns": cts + 3_000_000,
                "packet_pir_monotonic_ns": packet["observed_at_monotonic_ns"],
                "packet_callback_monotonic_ns": packet["observed_at_monotonic_ns"] + 100_000,
            }
        )

    report = probe.analyze_trace(
        probe.parse_records(records),
        minimum_takes=3,
        minimum_warmup=3,
        minimum_resource_samples=2,
    )
    pipeline = report["encoder_pipeline"]
    assert pipeline["status"] == "MEASURED"
    assert pipeline["sample_count"] == 3
    assert pipeline["encode_request_to_complete"]["p95_ms"] == 2.0
    assert pipeline["encode_complete_to_interleave"]["p95_ms"] == 3.0
    assert pipeline["interleave_to_callback"]["p95_ms"] == 0.1
    assert pipeline["acceptance_boundary_unchanged"] is True
    assert report["criteria"]["AC-12"]["boundary"] == "rtmp_first_packet"

    broken = _take_records(3)
    packet = next(
        item
        for item in broken
        if item.get("record_type") == "observation"
        and item.get("boundary") == "encoded_first_packet"
    )
    packet["packet_cts_monotonic_ns"] = packet["observed_at_monotonic_ns"] - 1
    with pytest.raises(probe.EvidenceError, match="CTS <= FER <= FERC <= PIR <= callback"):
        probe.parse_records(broken)

    packet.update(
        {
            "packet_fer_monotonic_ns": packet["observed_at_monotonic_ns"] - 3,
            "packet_ferc_monotonic_ns": packet["observed_at_monotonic_ns"] - 2,
            "packet_pir_monotonic_ns": packet["observed_at_monotonic_ns"],
            "packet_callback_monotonic_ns": packet["observed_at_monotonic_ns"] + 1,
        }
    )
    with pytest.raises(probe.EvidenceError, match="CTS <= FER <= FERC <= PIR <= callback"):
        probe.parse_records(broken)


def test_rtmp_load_request_and_receiver_metadata_are_an_atomic_session_pair():
    records = _take_records(1)
    records[0].pop("rtmp_load_requested")
    with pytest.raises(probe.EvidenceError, match="must be present together"):
        probe.parse_records(records)

    records = _take_records(1)
    records[0]["rtmp_load_requested"] = False
    with pytest.raises(probe.EvidenceError, match="must be present together"):
        probe.parse_records(records)


def test_rtmp_capture_path_requires_receiver_metadata():
    records = _take_records(1)
    records[0].pop("rtmp_receiver")
    records[0].pop("rtmp_load_requested")
    with pytest.raises(probe.EvidenceError, match="capture_paths includes rtmp_first_packet"):
        probe.parse_records(records)


def test_rtmp_packet_must_preserve_one_constant_mux_offset():
    records = _take_records(3)
    rtmp = next(
        item
        for item in records
        if item.get("record_type") == "observation" and item.get("boundary") == "rtmp_first_packet"
    )
    rtmp["packet_pts"] += 1
    with pytest.raises(probe.EvidenceError, match="mux offset|PTS/DTS"):
        probe.analyze_trace(
            probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
        )


def test_rtmp_packet_index_and_advertised_mux_calibration_are_fail_closed():
    records = _take_records(3)
    rtmp = next(
        item
        for item in records
        if item.get("record_type") == "observation" and item.get("boundary") == "rtmp_first_packet"
    )
    rtmp["packet_index"] += 1
    with pytest.raises(probe.EvidenceError, match="packet index"):
        probe.analyze_trace(
            probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
        )

    records = _take_records(3)
    records[0]["rtmp_receiver"]["correlated_packet_count"] -= 1
    with pytest.raises(probe.EvidenceError, match="correlated_packet_count"):
        probe.analyze_trace(
            probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
        )

    records = _take_records(3)
    records[0]["rtmp_receiver"]["mux_offset_min_num"] += 1
    with pytest.raises(probe.EvidenceError, match="calibration"):
        probe.analyze_trace(
            probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
        )

    records = _take_records(3)
    records[0]["rtmp_receiver"].pop("mux_offset_max_den")
    with pytest.raises(probe.EvidenceError, match="metadata is incomplete"):
        probe.parse_records(records)


def test_rtmp_slo_includes_declared_receiver_clock_bound_conservatively():
    records = _take_records(3, raw_extra_ms=7.0)
    report = probe.analyze_trace(
        probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
    )
    rtmp = report["criteria"]["AC-12"]
    assert rtmp["p95_ms"] == pytest.approx(1.9)
    assert rtmp["clock_bound_ms"] == pytest.approx(5.0)
    assert rtmp["p95_conservative_ms"] == pytest.approx(6.9)
    assert rtmp["status"] == "PASS"
    assert rtmp["ac12a"]["boundary"] == (
        "packet_callback_monotonic_ns_to_receiver_observed_normalized_ns"
    )
    assert rtmp["ac12b"]["status"] == "PASS"
    assert rtmp["ac12b"]["p95_ms"] == pytest.approx(10.0)
    assert set(rtmp["ac12b"]["stage_distributions"]) == set(probe.AC12B_STAGE_NAMES)

    records = _take_records(3)
    for record in records:
        if record.get("record_type") == "observation" and record.get("boundary") == "rtmp_first_packet":
            record["observed_at_monotonic_ns"] += 10_000_000
            record["receiver_observed_normalized_ns"] = record["observed_at_monotonic_ns"]
    report = probe.analyze_trace(
        probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
    )
    conservative = report["criteria"]["AC-12"]
    assert conservative["p95_ms"] == pytest.approx(11.9)
    assert conservative["p95_conservative_ms"] == pytest.approx(16.9)
    assert conservative["status"] == "FAIL"


def test_ac12a_and_ac12b_report_distinct_boundaries_without_pooling():
    records = _take_records(3, evidence_kind="runtime", include_resources=False)
    report = probe.analyze_trace(
        probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
    )
    criterion = report["criteria"]["AC-12"]
    assert criterion["status"] == "PASS"
    assert criterion["ac12a"]["status"] == "PASS"
    assert criterion["ac12a"]["count"] == 3
    assert criterion["ac12b"]["status"] == "PASS"
    assert criterion["ac12b"]["same_packet_count"] == 3
    assert criterion["ac12b"]["count"] == criterion["ac12b"]["same_packet_count"]
    assert criterion["ac12b"]["p95_ms"] > criterion["ac12a"]["p95_ms"]
    assert report["ac12a"] == criterion["ac12a"]
    assert report["ac12b"] == criterion["ac12b"]


def test_missing_callback_or_normalized_receiver_timestamp_is_unproven():
    records = _take_records(3, evidence_kind="runtime", include_resources=False)
    for record in records:
        if record.get("record_type") == "observation" and record.get("boundary") == "encoded_first_packet":
            for key in probe.ENCODER_TIMING_FIELDS:
                record.pop(key, None)
    report = probe.analyze_trace(
        probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
    )
    assert report["criteria"]["AC-12"]["ac12a"]["status"] == "UNPROVEN"
    assert report["criteria"]["AC-12"]["ac12b"]["status"] == "UNPROVEN"
    assert report["status"] == "UNPROVEN"

    records = _take_records(3, evidence_kind="runtime", include_resources=False)
    for record in records:
        if record.get("record_type") == "observation" and record.get("boundary") == "rtmp_first_packet":
            record.pop("receiver_observed_normalized_ns", None)
    report = probe.analyze_trace(
        probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
    )
    assert report["criteria"]["AC-12"]["status"] == "UNPROVEN"
    assert report["criteria"]["AC-12"]["ac12b"]["reason"]


def test_ac12b_has_no_second_latency_threshold_but_rejects_callback_after_receiver():
    records = _take_records(3, evidence_kind="runtime", include_resources=False)
    # Delay only the accepted event.  The exact packet remains correlated and
    # AC-12a remains low, while AC-12b visibly reports the larger end-to-end
    # duration without inventing a second threshold.
    delayed = False
    for record in records:
        if (
            not delayed
            and record.get("record_type") == "event"
            and record["event"]["event_type"] == "TakeAccepted"
            and record["event"]["take_command_id"] == "take-004"
        ):
            # Keep server-sequence order while making the first measured Take
            # start immediately after the preceding commit.
            record["event"]["observed_at_monotonic_ns"] = 1_205_000_000
            record["event"]["freeze_until_monotonic_ns"] = 2_205_000_000
            delayed = True
    report = probe.analyze_trace(
        probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
    )
    assert report["criteria"]["AC-12"]["ac12a"]["status"] == "PASS"
    assert report["criteria"]["AC-12"]["ac12b"]["status"] == "PASS"
    assert report["criteria"]["AC-12"]["ac12b"]["p95_ms"] > 90.0

    records = _take_records(3, evidence_kind="runtime", include_resources=False)
    for record in records:
        if record.get("record_type") == "observation" and record.get("boundary") == "encoded_first_packet":
            record["packet_callback_monotonic_ns"] = record["observed_at_monotonic_ns"] + 20_000_000
    with pytest.raises(probe.EvidenceError, match="negative rtmp_first_packet latency|AC-12b stages are not ordered"):
        probe.analyze_trace(
            probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
        )


def test_declared_warmup_does_not_create_observed_warmup_or_measured_takes():
    records = _take_records(3, evidence_kind="runtime", include_resources=False)
    records[0]["warmup_takes"] = 100
    report = probe.analyze_trace(
        probe.parse_records(records), minimum_takes=3, minimum_warmup=100, minimum_resource_samples=2
    )
    assert report["takes"]["warmup_takes_declared"] == 100
    assert report["takes"]["warmup_takes_observed"] == 6
    assert report["takes"]["measured_takes_observed"] == 0
    assert report["status"] == "UNPROVEN"


def test_rtmp_receiver_clock_mismatch_and_packet_identity_are_rejected():
    records = _take_records(3)
    rtmp = next(
        item
        for item in records
        if item.get("record_type") == "observation" and item.get("boundary") == "rtmp_first_packet"
    )
    rtmp["clock_source"] = "qpc"
    with pytest.raises(probe.EvidenceError, match="clock_source differs"):
        probe.parse_records(records)

    records = _take_records(3)
    rtmp_records = [
        item
        for item in records
        if item.get("record_type") == "observation" and item.get("boundary") == "rtmp_first_packet"
    ]
    rtmp_records[1]["packet_identity"] = rtmp_records[0]["packet_identity"]
    with pytest.raises(probe.EvidenceError, match="duplicate RTMP packet identity"):
        probe.analyze_trace(
            probe.parse_records(records), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2
        )


def test_wrong_directshow_surface_is_rejected_instead_of_counted_as_raw():
    records = _take_records(3)
    for record in records:
        if record.get("record_type") == "observation" and record.get("boundary") == "directshow_return":
            record["surface"] = "ProgramView"
            break
    with pytest.raises(probe.EvidenceError, match="BOUNDARY_INVALID"):
        probe.parse_records(records)


def test_directshow_stage_timing_is_optional_for_legacy_traces():
    records = _take_records(3)
    directshow = next(
        item
        for item in records
        if item.get("record_type") == "observation" and item.get("boundary") == "directshow_return"
    )
    for key in probe.DIRECTSHOW_TIMING_FIELDS:
        directshow.pop(key)
    parsed = probe.parse_records(records)
    report = probe.analyze_trace(parsed, minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)
    assert report["latency"]["directshow_return"]["count"] == 3


def test_telemetry_patch_captures_directshow_stage_timing_without_changing_boundary():
    patch_text = TELEMETRY_PATCH.read_text(encoding="utf-8")
    for field in probe.DIRECTSHOW_TIMING_FIELDS:
        assert field in patch_text
    assert "observed_at_monotonic_ns" in patch_text
    assert "EmitDirectShowObservation(metadata, timing)" in patch_text


def test_directshow_stage_timing_requires_complete_strictly_ordered_metadata():
    records = _take_records(3)
    directshow = next(
        item
        for item in records
        if item.get("record_type") == "observation" and item.get("boundary") == "directshow_return"
    )
    directshow.pop("queue_read_start_monotonic_ns")
    with pytest.raises(probe.EvidenceError, match="metadata must be complete"):
        probe.parse_records(records)

    records = _take_records(3)
    directshow = next(
        item
        for item in records
        if item.get("record_type") == "observation" and item.get("boundary") == "directshow_return"
    )
    directshow["queue_read_completed_monotonic_ns"] = directshow["queue_read_start_monotonic_ns"]
    with pytest.raises(probe.EvidenceError, match="strictly ordered"):
        probe.parse_records(records)

    records = _take_records(3)
    directshow = next(
        item
        for item in records
        if item.get("record_type") == "observation" and item.get("boundary") == "directshow_return"
    )
    directshow["observed_at_monotonic_ns"] += 1
    with pytest.raises(probe.EvidenceError, match="equal unlock completion"):
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


def test_post_swap_regression_records_exact_commit_but_fault_rejects_campaign():
    records = _take_records(2)
    commits = [
        item["event"]
        for item in records
        if item.get("record_type") == "event" and item["event"]["event_type"] == "TakeCommitted"
    ]
    prior = commits[0]
    regressed = commits[1]
    candidate_frame = prior["frame_id"] - 1
    candidate_pts = prior["pts_ns"] - 1
    regressed["frame_id"] = candidate_frame
    regressed["pts_ns"] = candidate_pts

    # The v1 event schema validates shape and correlation, not the cross-event
    # monotonic invariant.  Preserve the exact values so the analyzer is the
    # component that rejects the inter-commit regression.
    parsed = probe.parse_records(records)
    parsed_regressed = next(
        event for event in parsed.events if event["event_type"] == "TakeCommitted" and event["take_command_id"] == "take-002"
    )
    assert parsed_regressed["frame_id"] == candidate_frame
    assert parsed_regressed["pts_ns"] == candidate_pts
    assert not any(event["event_type"] == "TakeAborted" for event in parsed.events)
    with pytest.raises(probe.EvidenceError, match="FRAME_ORDER_INVALID"):
        probe.analyze_trace(parsed, minimum_takes=2, minimum_warmup=0, minimum_resource_samples=2)

    fault = {
        "record_type": "integrity_fault",
        "fault_type": "frame_or_pts_regression",
        "runtime_instance_id": RUNTIME,
        "command_id": regressed["command_id"],
        "intent_id": regressed["intent_id"],
        "take_command_id": regressed["take_command_id"],
        "observed_frame_id": candidate_frame,
        "observed_pts_ns": candidate_pts,
        "last_committed_frame_id": prior["frame_id"],
        "last_committed_pts_ns": prior["pts_ns"],
        "physical_swap_committed": True,
        "fail_stop": True,
    }
    records_with_fault = deepcopy(records)
    records_with_fault.append(fault)
    assert sum(record.get("record_type") == "integrity_fault" for record in records_with_fault) == 1
    with pytest.raises(probe.EvidenceError, match="INTEGRITY_FAULT"):
        probe.parse_records(records_with_fault)


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
    first = _trace(
        3,
        evidence_kind="runtime",
        codec="x264",
        runtime_id="runtime-x264-independent",
        include_resources=False,
    )
    second = _trace(3, evidence_kind="runtime", codec="nvenc", runtime_id="runtime-nvenc-independent")
    report = probe.analyze_traces((first, second), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)
    assert report["status"] == "PASS"
    assert len(report["campaigns"]) == 2
    assert all(campaign["latency"]["encoder_input_raw"]["count"] == 3 for campaign in report["campaigns"])
    assert report["required_codecs"] == ["x264", "nvenc"]
    assert report["observed_codecs"] == ["nvenc", "x264"]
    assert report["complete_codec_coverage"] is True
    coverage = {entry["codec"]: entry for entry in report["codec_coverage"]}
    assert coverage["x264"]["resource_status"] == "NOT_APPLICABLE"
    assert coverage["nvenc"]["resource_status"] == "MEASURED"


def test_multiple_nvenc_campaigns_without_x264_are_unproven():
    first = _trace(3, evidence_kind="runtime", codec="nvenc", runtime_id="runtime-nvenc-duplicate-a")
    second = _trace(3, evidence_kind="runtime", codec="nvenc", runtime_id="runtime-nvenc-duplicate-b")
    report = probe.analyze_traces((first, second), minimum_takes=3, minimum_warmup=3, minimum_resource_samples=2)

    assert report["status"] == "UNPROVEN"
    assert report["complete_codec_coverage"] is False
    assert report["observed_codecs"] == ["nvenc"]
    assert len(report["codec_coverage"]) == 2
    assert all(entry["status"] == "PASS" for entry in report["codec_coverage"])
    assert all(entry["resource_status"] == "MEASURED" for entry in report["codec_coverage"])


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
