#!/usr/bin/env python3
"""Validate reproducible dual-lane latency and capacity evidence.

This is the analysis half of the #246 probe.  The runtime writes newline
delimited JSON (JSONL); this program validates the records, correlates every
sample to the *same* ``TakeAccepted``/``TakeCommitted`` transaction, and
prints a deterministic p50/p95/p99 report.

The probe intentionally does not infer a frame boundary from a log line,
wall-clock time, or a downstream decoder.  A trace must contain the
frame/PTS-bearing observation emitted by the producer at one of the explicit
boundaries below:

``encoder_input_raw``
    First valid Program frame at the encoder/raw input (AC-07).
``directshow_return``
    First valid Program frame observed on the ProgramReturn DirectShow
    surface (AC-08).  This is a separate boundary, never a raw substitute.
``encoded_first_packet``
    First packet handed to the encoder-output callback.  This is a
    pre-network boundary; it is not an RTMP ingress or decoded-frame
    guarantee.  It is auxiliary diagnostics only and can never satisfy AC-12.
``rtmp_first_packet``
    First video packet observed by the dedicated loopback RTMP receiver after
    a committed Take.  This is a receiver/demux boundary, not a wire-level
    timestamp and not a decoded-frame guarantee.  The receiver record must
    carry the same video-packet index as the producer and preserve one
    constant rational FLV mux-offset interval across the stream.
``decoded_first_frame`` / ``antenna_first_frame``
    Optional diagnostic timings.  They are reported, but have no SLO here.

The accepted event and every observation use the monotonic nanosecond clock
of the runtime.  Frame IDs, PTS, revisions, command IDs, intent IDs and the
runtime ID are all checked before a latency is admitted to a percentile.
Missing evidence is reported as ``UNPROVEN`` and exits with code 3; it never
passes on synthetic or partial input.  A fixture may be used by unit tests,
but its report is ``FIXTURE_ONLY`` and cannot be a runtime acceptance.

Trace format (one session per file)::

    {"record_type":"session", "schema":"pulsar.take-latency.v1", ...}
    {"record_type":"event", "event": {<pulsar.scene-switch.v1 event>}}
    {"record_type":"observation", ...}
    {"record_type":"resource_sample", ...}

The session record documents the exact build command, hardware/workload
flags, warm-up count and the known resource reference (+0.091 ms/frame and
+3.13 MB, represented as decimal bytes).  Resource samples are collected in
``reference`` and ``dual_lane`` modes so the delta is measured rather than
declared from that reference.  Each new sample carries the actual
`encoder_active` state and `encoder_family`; only active NVENC samples can
satisfy AC-13.

Usage::

    python scripts/probe-take-latency.py --trace x264.jsonl nvenc.jsonl \
        --output latency-report.json

This module uses only the Python standard library plus the repository's
transport-neutral scene-switch validator.  It has no OBS/libobs dependency.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence


# Running the file directly puts ``scripts/`` on sys.path; importing it from
# pytest puts the repository root there instead.  Supporting both keeps this
# probe executable exactly as documented and keeps the contract validator
# authoritative when it is available.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from contracts.scene_switch_v1 import validate_event
except ImportError as exc:  # pragma: no cover - repository layout failure
    raise RuntimeError("scene-switch contract validator is unavailable") from exc


TRACE_SCHEMA = "pulsar.take-latency.v1"
REPORT_SCHEMA = "pulsar.take-latency-report.v1"
ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

BOUNDARIES = (
    "encoder_input_raw",
    "directshow_return",
    "encoded_first_packet",
    "rtmp_first_packet",
    "decoded_first_frame",
    "antenna_first_frame",
)
REQUIRED_BOUNDARIES = (
    "encoder_input_raw",
    "directshow_return",
    "rtmp_first_packet",
)
SLO_MS = {
    "encoder_input_raw": 50.0,
    "directshow_return": 75.0,
    "rtmp_first_packet": 15.0,
}
RESOURCE_REFERENCE = {
    "extra_frame_render_ms": 0.091,
    "extra_resident_bytes": 3_130_000,
}
RESOURCE_MODES = ("reference", "dual_lane")
REQUIRED_CODECS = ("x264", "nvenc")
RESOURCE_METRICS = (
    "frame_render_ms",
    "resident_bytes",
    "process_cpu_percent",
    "host_gpu_percent",
    "callback_backlog_estimate",
    "encoder_utilization_percent",
)

SESSION_REQUIRED = {
    "record_type",
    "schema",
    "runtime_instance_id",
    "session_id",
    "codec",
    "warmup_takes",
    "video",
    "workload",
    "capture_paths",
    "resource_reference",
    "build_revision",
    "command_line",
    "hardware",
    "producer_topology",
    "producer_count",
    "evidence_kind",
}
SESSION_OPTIONAL = {
    "comparison_id",
    "notes",
    "source_types",
    "rtmp_receiver",
    "rtmp_load_requested",
}
SESSION_ALLOWED = SESSION_REQUIRED | SESSION_OPTIONAL

OBSERVATION_REQUIRED = {
    "record_type",
    "boundary",
    "clock_domain",
    "runtime_instance_id",
    "command_id",
    "intent_id",
    "take_command_id",
    "revisions",
    "frame_id",
    "pts_ns",
    "observed_at_monotonic_ns",
    "valid",
    "surface",
    "consumer",
}
OBSERVATION_OPTIONAL = {
    "program_frame",
    "packet_index",
    "packet_pts",
    "packet_dts",
    "packet_timebase_num",
    "packet_timebase_den",
    "packet_identity",
    "clock_source",
    "clock_offset_ns",
    "clock_bound_ns",
    "frame_hash",
    "notes",
}
OBSERVATION_ALLOWED = OBSERVATION_REQUIRED | OBSERVATION_OPTIONAL

RESOURCE_REQUIRED = {
    "record_type",
    "sample_mode",
    "measurement_phase",
    "clock_domain",
    "runtime_instance_id",
    "observed_at_monotonic_ns",
    "build_revision",
    "hardware",
    "producer_topology",
    "producer_count",
    *RESOURCE_METRICS,
}
RESOURCE_OPTIONAL = {"encoder_active", "encoder_family", "gpu_memory_bytes", "rtmp_load_active", "notes"}
RESOURCE_ALLOWED = RESOURCE_REQUIRED | RESOURCE_OPTIONAL

RTMP_RECEIVER_REQUIRED = {
    "server_url",
    "stream_key",
    "endpoint",
    "receiver_id",
    "stream_id",
    "clock_source",
    "clock_offset_ns",
    "clock_bound_ns",
    "packet_timebase_num",
    "packet_timebase_den",
}
RTMP_CORRELATION_FIELDS = {
    "correlation_method",
    "mux_offset_min_num",
    "mux_offset_min_den",
    "mux_offset_max_num",
    "mux_offset_max_den",
    "correlated_packet_count",
}
RTMP_RECEIVER_ALLOWED = RTMP_RECEIVER_REQUIRED | RTMP_CORRELATION_FIELDS
RTMP_CLOCK_SOURCES = ("perf_counter_ns/qpc", "qpc")
RTMP_CLOCK_BOUND_MAX_NS = 5_000_000


class EvidenceError(ValueError):
    """A trace is malformed or violates a correlation invariant."""

    def __init__(self, code: str, message: str, *, line: int | None = None) -> None:
        self.code = code
        self.line = line
        suffix = f" (line {line})" if line is not None else ""
        super().__init__(f"{code}{suffix}: {message}")


@dataclass
class Trace:
    """Validated records for one runtime/session."""

    session: dict[str, Any]
    events: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    resources: list[dict[str, Any]]
    source: str


def _object(value: Any, name: str, *, line: int | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError("SCHEMA_INVALID", f"{name} must be an object", line=line)
    return value


def _exact_keys(
    value: Mapping[str, Any], required: set[str], allowed: set[str], name: str, *, line: int | None = None
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        raise EvidenceError("SCHEMA_INVALID", f"{name} is missing {missing}", line=line)
    if unknown:
        raise EvidenceError("SCHEMA_INVALID", f"{name} contains unknown fields {unknown}", line=line)


def _string(value: Any, name: str, *, identifier: bool = False, line: int | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceError("SCHEMA_INVALID", f"{name} must be a non-empty string", line=line)
    if identifier and ID_RE.fullmatch(value) is None:
        raise EvidenceError("SCHEMA_INVALID", f"{name} is not a valid identifier", line=line)
    return value


def _integer(value: Any, name: str, *, non_negative: bool = True, line: int | None = None) -> int:
    # bool is an int subclass in Python but is not a JSON integer for this
    # evidence format.  Do not silently coerce floats: nanosecond/PTS data
    # must retain its exact integer representation.
    if type(value) is not int or (non_negative and value < 0):
        qualifier = "non-negative " if non_negative else ""
        raise EvidenceError("SCHEMA_INVALID", f"{name} must be a {qualifier}integer", line=line)
    return value


def _number(value: Any, name: str, *, non_negative: bool = True, line: int | None = None) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise EvidenceError("SCHEMA_INVALID", f"{name} must be a finite number", line=line)
    if non_negative and value < 0:
        raise EvidenceError("SCHEMA_INVALID", f"{name} must be non-negative", line=line)
    return value


def _boolean(value: Any, name: str, *, line: int | None = None) -> bool:
    if type(value) is not bool:
        raise EvidenceError("SCHEMA_INVALID", f"{name} must be boolean", line=line)
    return value


def _validate_rtmp_receiver(value: Any, *, line: int | None = None) -> dict[str, Any]:
    obj = _object(value, "session.rtmp_receiver", line=line)
    _exact_keys(obj, RTMP_RECEIVER_REQUIRED, RTMP_RECEIVER_ALLOWED, "session.rtmp_receiver", line=line)
    result = dict(obj)
    for key in ("receiver_id", "stream_id"):
        _string(obj[key], f"session.rtmp_receiver.{key}", identifier=True, line=line)
    for key in ("server_url", "endpoint"):
        _string(obj[key], f"session.rtmp_receiver.{key}", line=line)
    server_url = obj["server_url"]
    endpoint = obj["endpoint"]
    if not (server_url.startswith("rtmp://127.0.0.1:") or server_url.startswith("rtmp://localhost:")):
        raise EvidenceError(
            "SCHEMA_INVALID",
            "session.rtmp_receiver.server_url must be a loopback rtmp:// endpoint",
            line=line,
        )
    _string(obj["stream_key"], "session.rtmp_receiver.stream_key", identifier=True, line=line)
    if endpoint != f"{server_url.rstrip('/')}/{obj['stream_key']}":
        raise EvidenceError(
            "CORRELATION_INVALID",
            "session.rtmp_receiver.endpoint must be server_url plus stream_key",
            line=line,
        )
    if obj["clock_source"] not in RTMP_CLOCK_SOURCES:
        raise EvidenceError("SCHEMA_INVALID", "session.rtmp_receiver.clock_source is unsupported", line=line)
    _integer(obj["clock_offset_ns"], "session.rtmp_receiver.clock_offset_ns", non_negative=False, line=line)
    bound = _integer(obj["clock_bound_ns"], "session.rtmp_receiver.clock_bound_ns", line=line)
    if bound <= 0 or bound > RTMP_CLOCK_BOUND_MAX_NS:
        raise EvidenceError(
            "SCHEMA_INVALID",
            f"session.rtmp_receiver.clock_bound_ns must be in 1..{RTMP_CLOCK_BOUND_MAX_NS}",
            line=line,
        )
    for key in ("packet_timebase_num", "packet_timebase_den"):
        value_int = _integer(obj[key], f"session.rtmp_receiver.{key}", line=line)
        if value_int <= 0:
            raise EvidenceError("SCHEMA_INVALID", f"session.rtmp_receiver.{key} must be positive", line=line)
    correlation_present = RTMP_CORRELATION_FIELDS & set(obj)
    if correlation_present and correlation_present != RTMP_CORRELATION_FIELDS:
        missing = sorted(RTMP_CORRELATION_FIELDS - correlation_present)
        raise EvidenceError(
            "SCHEMA_INVALID",
            f"session.rtmp_receiver mux correlation metadata is incomplete; missing {missing}",
            line=line,
        )
    if correlation_present:
        method = _string(
            obj["correlation_method"],
            "session.rtmp_receiver.correlation_method",
            identifier=True,
            line=line,
        )
        if method != "packet_index_constant_mux_offset_v1":
            raise EvidenceError("SCHEMA_INVALID", "unsupported RTMP packet correlation method", line=line)
        for key in ("mux_offset_min_num", "mux_offset_max_num"):
            _integer(obj[key], f"session.rtmp_receiver.{key}", non_negative=False, line=line)
        for key in ("mux_offset_min_den", "mux_offset_max_den"):
            denominator = _integer(obj[key], f"session.rtmp_receiver.{key}", line=line)
            if denominator <= 0:
                raise EvidenceError("SCHEMA_INVALID", f"session.rtmp_receiver.{key} must be positive", line=line)
        count = _integer(
            obj["correlated_packet_count"],
            "session.rtmp_receiver.correlated_packet_count",
            line=line,
        )
        if count <= 0:
            raise EvidenceError(
                "SCHEMA_INVALID",
                "session.rtmp_receiver.correlated_packet_count must be positive",
                line=line,
            )
        lower = Fraction(obj["mux_offset_min_num"], obj["mux_offset_min_den"])
        upper = Fraction(obj["mux_offset_max_num"], obj["mux_offset_max_den"])
        if lower > upper:
            raise EvidenceError("SCHEMA_INVALID", "RTMP mux offset interval is inverted", line=line)
    return result


def _revisions(value: Any, name: str, *, line: int | None = None) -> dict[str, int]:
    obj = _object(value, name, line=line)
    _exact_keys(obj, {"program", "preview", "role_map"}, {"program", "preview", "role_map"}, name, line=line)
    return {key: _integer(obj[key], f"{name}.{key}", line=line) for key in ("program", "preview", "role_map")}


def _validate_session(value: Any, *, line: int | None = None) -> dict[str, Any]:
    obj = _object(value, "session", line=line)
    _exact_keys(obj, SESSION_REQUIRED, SESSION_ALLOWED, "session", line=line)
    if obj.get("record_type") != "session" or obj.get("schema") != TRACE_SCHEMA:
        raise EvidenceError("SCHEMA_INVALID", "session record_type/schema is not pulsar.take-latency.v1", line=line)
    result = dict(obj)
    for key in ("runtime_instance_id", "session_id"):
        _string(obj[key], f"session.{key}", identifier=True, line=line)
    build_revision = _string(obj["build_revision"], "session.build_revision", line=line)
    if obj["evidence_kind"] not in ("runtime", "fixture"):
        raise EvidenceError("SCHEMA_INVALID", "session.evidence_kind must be runtime or fixture", line=line)
    if obj["evidence_kind"] == "runtime":
        if re.fullmatch(r"[0-9a-f]{40}", build_revision) is None:
            raise EvidenceError(
                "SCHEMA_INVALID",
                "runtime session.build_revision must be the exact 40-character lowercase candidate SHA",
                line=line,
            )
    _string(obj["command_line"], "session.command_line", line=line)
    if obj["codec"] not in ("x264", "nvenc"):
        raise EvidenceError("SCHEMA_INVALID", "session.codec must be x264 or nvenc", line=line)
    _integer(obj["warmup_takes"], "session.warmup_takes", line=line)
    video = _object(obj["video"], "session.video", line=line)
    _exact_keys(video, {"width", "height", "fps_num", "fps_den"}, {"width", "height", "fps_num", "fps_den"}, "session.video", line=line)
    for key in ("width", "height", "fps_num", "fps_den"):
        _integer(video[key], f"session.video.{key}", line=line)
    if video["width"] <= 0 or video["height"] <= 0 or video["fps_num"] <= 0 or video["fps_den"] <= 0:
        raise EvidenceError("SCHEMA_INVALID", "session.video dimensions and frame rate must be positive", line=line)
    workload = _object(obj["workload"], "session.workload", line=line)
    _exact_keys(workload, {"wgc", "cef", "nvenc"}, {"wgc", "cef", "nvenc"}, "session.workload", line=line)
    for key in ("wgc", "cef", "nvenc"):
        _boolean(workload[key], f"session.workload.{key}", line=line)
    if obj["codec"] == "nvenc" and not workload["nvenc"]:
        raise EvidenceError("SCHEMA_INVALID", "an nvenc session must declare workload.nvenc=true", line=line)
    source_types = obj.get("source_types")
    if obj["evidence_kind"] == "runtime":
        if not isinstance(source_types, list) or not source_types or any(
            not isinstance(source_type, str) or not source_type for source_type in source_types
        ):
            raise EvidenceError(
                "SCHEMA_INVALID",
                "runtime session.source_types must list the declared workload source kinds; the probe must separately prove their binding",
                line=line,
            )
        if len(set(source_types)) != len(source_types):
            raise EvidenceError("SCHEMA_INVALID", "session.source_types must not contain duplicates", line=line)
        if any(source_type not in ("window_capture", "browser_source") for source_type in source_types):
            raise EvidenceError("SCHEMA_INVALID", "session.source_types contains an unsupported source kind", line=line)
        if workload["wgc"] and "window_capture" not in source_types:
            raise EvidenceError("SCHEMA_INVALID", "workload.wgc=true requires window_capture in source_types", line=line)
        if workload["cef"] and "browser_source" not in source_types:
            raise EvidenceError("SCHEMA_INVALID", "workload.cef=true requires browser_source in source_types", line=line)
    paths = obj["capture_paths"]
    if not isinstance(paths, list) or not paths or any(path not in BOUNDARIES for path in paths):
        raise EvidenceError("SCHEMA_INVALID", "session.capture_paths must list supported boundaries", line=line)
    if len(set(paths)) != len(paths):
        raise EvidenceError("SCHEMA_INVALID", "session.capture_paths must not contain duplicates", line=line)
    if "rtmp_receiver" in obj:
        result["rtmp_receiver"] = _validate_rtmp_receiver(obj["rtmp_receiver"], line=line)
    if "rtmp_load_requested" in obj:
        result["rtmp_load_requested"] = _boolean(
            obj["rtmp_load_requested"], "session.rtmp_load_requested", line=line
        )
    has_rtmp_receiver = "rtmp_receiver" in obj
    has_rtmp_load_request = obj.get("rtmp_load_requested") is True
    if has_rtmp_receiver != has_rtmp_load_request:
        raise EvidenceError(
            "SCHEMA_INVALID",
            "session.rtmp_receiver and session.rtmp_load_requested must be present together",
            line=line,
        )
    if "rtmp_first_packet" in paths and not has_rtmp_receiver:
        raise EvidenceError(
            "SCHEMA_INVALID",
            "session.capture_paths includes rtmp_first_packet without rtmp_receiver metadata",
            line=line,
        )
    if "rtmp_first_packet" in paths and has_rtmp_receiver:
        receiver = result["rtmp_receiver"]
        if not RTMP_CORRELATION_FIELDS <= set(receiver):
            raise EvidenceError(
                "SCHEMA_INVALID",
                "rtmp_first_packet requires packet-index/mux-offset correlation metadata",
                line=line,
            )
    reference = _object(obj["resource_reference"], "session.resource_reference", line=line)
    _exact_keys(reference, set(RESOURCE_REFERENCE), set(RESOURCE_REFERENCE), "session.resource_reference", line=line)
    _number(reference["extra_frame_render_ms"], "resource_reference.extra_frame_render_ms", line=line)
    _integer(reference["extra_resident_bytes"], "resource_reference.extra_resident_bytes", line=line)
    if reference != RESOURCE_REFERENCE:
        raise EvidenceError(
            "SCHEMA_INVALID",
            "resource reference must remain +0.091 ms/frame and +3.13 MB (3,130,000 decimal bytes)",
            line=line,
        )
    hardware = _object(obj["hardware"], "session.hardware", line=line)
    _exact_keys(hardware, {"host", "gpu"}, {"host", "gpu"}, "session.hardware", line=line)
    for key in ("host", "gpu"):
        label = _string(hardware[key], f"session.hardware.{key}", line=line)
        if len(label) > 128 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in label):
            raise EvidenceError(
                "SCHEMA_INVALID",
                f"session.hardware.{key} must be a printable label of at most 128 characters",
                line=line,
            )
        if obj["evidence_kind"] == "runtime" and label in ("unknown-host", "unknown-gpu"):
            raise EvidenceError(
                "SCHEMA_INVALID",
                f"runtime session.hardware.{key} must identify the actual host/adapter",
                line=line,
            )
    producer_topology = obj["producer_topology"]
    if producer_topology not in ("single_lane_reference", "dual_lane_ab"):
        raise EvidenceError(
            "SCHEMA_INVALID",
            "session.producer_topology must be single_lane_reference or dual_lane_ab",
            line=line,
        )
    producer_count = _integer(obj["producer_count"], "session.producer_count", line=line)
    expected_count = 1 if producer_topology == "single_lane_reference" else 2
    if producer_count != expected_count:
        raise EvidenceError(
            "SCHEMA_INVALID",
            "session.producer_count does not match session.producer_topology",
            line=line,
        )
    return result


def _validate_event_record(value: Any, session: Mapping[str, Any], *, line: int | None = None) -> dict[str, Any]:
    obj = _object(value, "event record", line=line)
    _exact_keys(obj, {"record_type", "event"}, {"record_type", "event"}, "event record", line=line)
    if obj["record_type"] != "event":
        raise EvidenceError("SCHEMA_INVALID", "event record_type must be event", line=line)
    try:
        event = validate_event(_object(obj["event"], "event", line=line))
    except Exception as exc:
        raise EvidenceError("EVENT_INVALID", str(exc), line=line) from exc
    if event["runtime_instance_id"] != session["runtime_instance_id"]:
        raise EvidenceError("CORRELATION_INVALID", "event runtime_instance_id differs from session", line=line)
    if event["event_type"] == "TakeAborted" and event["reason"] == "queue_rejected":
        terminal_fields = ("last_committed_frame_id", "last_committed_pts_ns")
        if any(field not in event for field in terminal_fields):
            raise EvidenceError(
                "SCHEMA_INVALID",
                "queue_rejected TakeAborted must expose last committed frame/PTS together",
                line=line,
            )
    return event


def _validate_observation(value: Any, session: Mapping[str, Any], *, line: int | None = None) -> dict[str, Any]:
    obj = _object(value, "observation", line=line)
    _exact_keys(obj, OBSERVATION_REQUIRED, OBSERVATION_ALLOWED, "observation", line=line)
    result = dict(obj)
    if obj["record_type"] != "observation":
        raise EvidenceError("SCHEMA_INVALID", "observation record_type must be observation", line=line)
    if obj["boundary"] not in BOUNDARIES:
        raise EvidenceError("SCHEMA_INVALID", f"unsupported observation boundary {obj['boundary']!r}", line=line)
    if obj["clock_domain"] != "monotonic_ns":
        raise EvidenceError("SCHEMA_INVALID", "all observations must use clock_domain=monotonic_ns", line=line)
    for key in ("runtime_instance_id", "command_id", "intent_id", "take_command_id"):
        _string(obj[key], f"observation.{key}", identifier=True, line=line)
        if obj[key] != session["runtime_instance_id"] and key == "runtime_instance_id":
            raise EvidenceError("CORRELATION_INVALID", "observation runtime_instance_id differs from session", line=line)
    if obj["runtime_instance_id"] != session["runtime_instance_id"]:
        raise EvidenceError("CORRELATION_INVALID", "observation runtime_instance_id differs from session", line=line)
    result["revisions"] = _revisions(obj["revisions"], "observation.revisions", line=line)
    for key in ("frame_id", "pts_ns", "observed_at_monotonic_ns"):
        _integer(obj[key], f"observation.{key}", line=line)
    _boolean(obj["valid"], "observation.valid", line=line)
    _string(obj["surface"], "observation.surface", line=line)
    _string(obj["consumer"], "observation.consumer", line=line)
    expected = {
        "encoder_input_raw": ("ProgramView", "encoder_input"),
        "directshow_return": ("ProgramReturn", "DirectShow"),
        "encoded_first_packet": ("EncoderOutput", "encoder_callback"),
        "rtmp_first_packet": ("RTMP", "receiver"),
        "decoded_first_frame": ("RTMP", "decoder"),
        "antenna_first_frame": ("Antenna", "antenna"),
    }[obj["boundary"]]
    if (obj["surface"], obj["consumer"]) != expected:
        raise EvidenceError(
            "BOUNDARY_INVALID",
            f"{obj['boundary']} must use surface/consumer={expected!r}, got {(obj['surface'], obj['consumer'])!r}",
            line=line,
        )
    if obj["boundary"] in ("encoder_input_raw", "directshow_return"):
        if "program_frame" not in obj:
            raise EvidenceError("SCHEMA_INVALID", f"{obj['boundary']} requires program_frame", line=line)
        _boolean(obj["program_frame"], "observation.program_frame", line=line)
        if obj["valid"] and not obj["program_frame"]:
            raise EvidenceError("BOUNDARY_INVALID", "a valid raw/DirectShow sample must be a Program frame", line=line)
    if obj["boundary"] == "encoded_first_packet":
        if "packet_index" not in obj:
            raise EvidenceError("BOUNDARY_INVALID", "encoded_first_packet requires packet_index", line=line)
        _integer(obj["packet_index"], "observation.packet_index", line=line)
        # These optional fields make the producer packet usable for a receiver
        # correlation when emitted by a current runtime.  Old traces remain
        # parseable as auxiliary evidence, but can never satisfy AC-12.
        packet_fields = ("packet_pts", "packet_dts", "packet_timebase_num", "packet_timebase_den")
        if any(key in obj for key in packet_fields) and not all(key in obj for key in packet_fields):
            raise EvidenceError("SCHEMA_INVALID", "encoded packet metadata must be complete", line=line)
        for key in packet_fields:
            if key in obj:
                _integer(obj[key], f"observation.{key}", non_negative=key != "packet_dts", line=line)
        for key in ("packet_timebase_num", "packet_timebase_den"):
            if key in obj and obj[key] <= 0:
                raise EvidenceError("SCHEMA_INVALID", f"observation.{key} must be positive", line=line)
    if obj["boundary"] == "rtmp_first_packet":
        required_packet_fields = (
            "packet_index",
            "packet_pts",
            "packet_dts",
            "packet_timebase_num",
            "packet_timebase_den",
            "packet_identity",
            "clock_source",
            "clock_offset_ns",
            "clock_bound_ns",
        )
        for key in required_packet_fields:
            if key not in obj:
                raise EvidenceError("SCHEMA_INVALID", f"rtmp_first_packet requires {key}", line=line)
        _integer(obj["packet_index"], "observation.packet_index", line=line)
        _integer(obj["packet_pts"], "observation.packet_pts", line=line)
        _integer(obj["packet_dts"], "observation.packet_dts", non_negative=False, line=line)
        for key in ("packet_timebase_num", "packet_timebase_den"):
            value_int = _integer(obj[key], f"observation.{key}", line=line)
            if value_int <= 0:
                raise EvidenceError("SCHEMA_INVALID", f"observation.{key} must be positive", line=line)
        _string(obj["packet_identity"], "observation.packet_identity", identifier=True, line=line)
        if obj["clock_source"] not in RTMP_CLOCK_SOURCES:
            raise EvidenceError("SCHEMA_INVALID", "observation.clock_source is unsupported", line=line)
        _integer(obj["clock_offset_ns"], "observation.clock_offset_ns", non_negative=False, line=line)
        bound = _integer(obj["clock_bound_ns"], "observation.clock_bound_ns", line=line)
        if bound <= 0 or bound > RTMP_CLOCK_BOUND_MAX_NS:
            raise EvidenceError(
                "SCHEMA_INVALID",
                f"observation.clock_bound_ns must be in 1..{RTMP_CLOCK_BOUND_MAX_NS}",
                line=line,
            )
        receiver = session.get("rtmp_receiver")
        if receiver is not None:
            for key in ("clock_source", "clock_offset_ns", "clock_bound_ns"):
                if obj[key] != receiver[key]:
                    raise EvidenceError(
                        "CORRELATION_INVALID",
                        f"rtmp observation {key} differs from session receiver metadata",
                        line=line,
                    )
    if "frame_hash" in obj:
        _string(obj["frame_hash"], "observation.frame_hash", line=line)
    return result


def _validate_resource(value: Any, session: Mapping[str, Any], *, line: int | None = None) -> dict[str, Any]:
    obj = _object(value, "resource sample", line=line)
    _exact_keys(obj, RESOURCE_REQUIRED, RESOURCE_ALLOWED, "resource sample", line=line)
    result = dict(obj)
    if obj["record_type"] != "resource_sample":
        raise EvidenceError("SCHEMA_INVALID", "resource record_type must be resource_sample", line=line)
    if obj["sample_mode"] not in RESOURCE_MODES:
        raise EvidenceError("SCHEMA_INVALID", "resource sample_mode must be reference or dual_lane", line=line)
    if obj["measurement_phase"] != obj["sample_mode"]:
        raise EvidenceError(
            "SCHEMA_INVALID",
            "resource measurement_phase must equal sample_mode",
            line=line,
        )
    if obj["clock_domain"] != "monotonic_ns":
        raise EvidenceError("SCHEMA_INVALID", "resource samples must use clock_domain=monotonic_ns", line=line)
    _string(obj["runtime_instance_id"], "resource.runtime_instance_id", identifier=True, line=line)
    if obj["runtime_instance_id"] != session["runtime_instance_id"]:
        raise EvidenceError("CORRELATION_INVALID", "resource runtime_instance_id differs from session", line=line)
    build_revision = _string(obj["build_revision"], "resource.build_revision", line=line)
    if build_revision != session["build_revision"]:
        raise EvidenceError("CORRELATION_INVALID", "resource build_revision differs from session", line=line)
    hardware = _object(obj["hardware"], "resource.hardware", line=line)
    _exact_keys(hardware, {"host", "gpu"}, {"host", "gpu"}, "resource.hardware", line=line)
    if hardware != session["hardware"]:
        raise EvidenceError("CORRELATION_INVALID", "resource hardware identity differs from session", line=line)
    producer_topology = obj["producer_topology"]
    if producer_topology not in ("single_lane_reference", "dual_lane_ab"):
        raise EvidenceError("SCHEMA_INVALID", "resource producer_topology is unsupported", line=line)
    producer_count = _integer(obj["producer_count"], "resource.producer_count", line=line)
    expected_count = 1 if producer_topology == "single_lane_reference" else 2
    if producer_count != expected_count:
        raise EvidenceError(
            "SCHEMA_INVALID",
            "resource.producer_count does not match resource.producer_topology",
            line=line,
        )
    expected_topology = "single_lane_reference" if obj["sample_mode"] == "reference" else "dual_lane_ab"
    if producer_topology != expected_topology:
        raise EvidenceError(
            "CORRELATION_INVALID",
            "resource producer topology does not match measurement phase",
            line=line,
        )
    _integer(obj["observed_at_monotonic_ns"], "resource.observed_at_monotonic_ns", line=line)
    if "encoder_active" in obj:
        _boolean(obj["encoder_active"], "resource.encoder_active", line=line)
    if "rtmp_load_active" in obj:
        _boolean(obj["rtmp_load_active"], "resource.rtmp_load_active", line=line)
    if "encoder_family" in obj:
        _string(obj["encoder_family"], "resource.encoder_family", line=line)
        if obj["encoder_family"] not in ("x264", "nvenc"):
            raise EvidenceError(
                "SCHEMA_INVALID",
                "resource.encoder_family must be x264 or nvenc",
                line=line,
            )
    for key in RESOURCE_METRICS:
        _number(obj[key], f"resource.{key}", line=line)
    if "gpu_memory_bytes" in obj:
        _integer(obj["gpu_memory_bytes"], "resource.gpu_memory_bytes", line=line)
    return result


def parse_records(records: Iterable[Mapping[str, Any]], *, source: str = "<memory>") -> Trace:
    """Validate an iterable of decoded JSON objects as one trace."""

    session: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    for line_number, raw in enumerate(records, start=1):
        if not isinstance(raw, Mapping):
            raise EvidenceError("SCHEMA_INVALID", "each JSONL record must be an object", line=line_number)
        record_type = raw.get("record_type")
        if session is None and record_type != "session":
            raise EvidenceError("SCHEMA_INVALID", "the first record must be session", line=line_number)
        if record_type == "session":
            if session is not None:
                raise EvidenceError("SCHEMA_INVALID", "trace contains more than one session record", line=line_number)
            session = _validate_session(raw, line=line_number)
        elif record_type == "event":
            if session is None:  # guarded above; keep fail-closed under python -O too
                raise EvidenceError("SCHEMA_INVALID", "event appeared before session", line=line_number)
            events.append(_validate_event_record(raw, session, line=line_number))
        elif record_type == "observation":
            if session is None:
                raise EvidenceError("SCHEMA_INVALID", "observation appeared before session", line=line_number)
            observations.append(_validate_observation(raw, session, line=line_number))
        elif record_type == "resource_sample":
            if session is None:
                raise EvidenceError("SCHEMA_INVALID", "resource sample appeared before session", line=line_number)
            resources.append(_validate_resource(raw, session, line=line_number))
        elif record_type == "integrity_fault":
            # This record is deliberately outside the scene-switch and
            # take-latency schemas.  It is a process-integrity stop signal,
            # never admissible evidence; fail closed before any report can
            # classify the preceding exact TakeCommitted as a pass.
            raise EvidenceError(
                "INTEGRITY_FAULT",
                "runtime emitted an out-of-contract integrity fault; campaign evidence is rejected",
                line=line_number,
            )
        else:
            raise EvidenceError("SCHEMA_INVALID", f"unsupported record_type {record_type!r}", line=line_number)
    if session is None:
        raise EvidenceError("SCHEMA_INVALID", "trace is empty; session record is required")
    return Trace(session, events, observations, resources, source)


def parse_trace(path: Path) -> Trace:
    """Read and validate one UTF-8 JSONL trace."""

    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise EvidenceError("TRACE_IO", f"cannot open {path}: {exc}") from exc
    records: list[dict[str, Any]] = []
    with handle:
        for line_number, text in enumerate(handle, start=1):
            if not text.strip():
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise EvidenceError("MALFORMED_JSON", exc.msg, line=line_number) from exc
            records.append(value)
    return parse_records(records, source=str(path))


def quantile(values: Sequence[float], percentile: float) -> float:
    """Return a deterministic linear-interpolation percentile.

    The method is NumPy-compatible for the default ``method=linear``
    definition: rank is ``(n - 1) * q`` and the two surrounding values are
    interpolated.  Keeping it here avoids a hidden dependency in CI and makes
    report reproduction independent of a statistics package version.
    """

    if not values:
        raise ValueError("quantile requires at least one value")
    if not 0.0 <= percentile <= 1.0:
        raise ValueError("percentile must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _stats(values: Sequence[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50_ms": round(quantile(values, 0.50), 6) if values else None,
        "p95_ms": round(quantile(values, 0.95), 6) if values else None,
        "p99_ms": round(quantile(values, 0.99), 6) if values else None,
        "min_ms": round(min(values), 6) if values else None,
        "max_ms": round(max(values), 6) if values else None,
    }


def _resource_stats(values: Sequence[float | int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None}
    return {
        "count": len(values),
        "p50": round(quantile([float(v) for v in values], 0.50), 6),
        "p95": round(quantile([float(v) for v in values], 0.95), 6),
        "p99": round(quantile([float(v) for v in values], 0.99), 6),
    }


def _status_for_latency(
    summary: Mapping[str, Any],
    minimum_takes: int,
    boundary: str,
    *,
    uncertainty_ms: float = 0.0,
) -> str:
    if summary["count"] < minimum_takes:
        return "UNPROVEN"
    slo = SLO_MS.get(boundary)
    if slo is not None and float(summary["p95_ms"]) + uncertainty_ms > slo:
        return "FAIL"
    return "PASS"


def _packet_pts_fraction(observation: Mapping[str, Any]) -> Fraction:
    """Return a packet PTS in seconds without going through a float.

    The sender normally uses a 60/1 timebase while FLV/RTMP receivers expose
    millisecond ticks.  Keeping this as a rational is what lets the analyzer
    admit only the documented half-receiver-tick quantization, rather than
    accepting a nearby packet by wall-clock order.
    """

    return Fraction(
        int(observation["packet_pts"]) * int(observation["packet_timebase_num"]),
        int(observation["packet_timebase_den"]),
    )


def _packet_match_tolerance(receiver: Mapping[str, Any]) -> Fraction:
    """Return half of one receiver PTS tick as an exact rational."""

    return Fraction(
        int(receiver["packet_timebase_num"]),
        2 * int(receiver["packet_timebase_den"]),
    )


def _validate_rtmp_packet_correlations(
    selected: Mapping[str, Sequence[Mapping[str, Any]]],
    session: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate each receiver packet against exactly one producer packet.

    Return the unique receiver-to-encoder matches keyed by Take ID.  A trace
    containing receiver records but no complete producer packet metadata is
    rejected, because accepting it would reduce AC-12 to "next FFmpeg log".
    """

    receiver_observations = selected.get("rtmp_first_packet", ())
    if not receiver_observations:
        return {}
    encoded_observations = selected.get("encoded_first_packet", ())
    encoded_by_take: dict[str, list[dict[str, Any]]] = {}
    for observation in encoded_observations:
        if not all(
            key in observation
            for key in ("packet_pts", "packet_dts", "packet_timebase_num", "packet_timebase_den")
        ):
            raise EvidenceError(
                "CORRELATION_INVALID",
                "rtmp_first_packet requires complete encoded packet metadata for its Take",
            )
        encoded_by_take.setdefault(observation["take_command_id"], []).append(dict(observation))

    receiver = session.get("rtmp_receiver")
    if receiver is None:
        raise EvidenceError(
            "CORRELATION_INVALID",
            "rtmp_first_packet requires session.rtmp_receiver calibration metadata",
        )
    advertised_lower = Fraction(receiver["mux_offset_min_num"], receiver["mux_offset_min_den"])
    advertised_upper = Fraction(receiver["mux_offset_max_num"], receiver["mux_offset_max_den"])
    computed_lower: Fraction | None = None
    computed_upper: Fraction | None = None

    seen_identity: set[str] = set()
    matches: dict[str, dict[str, Any]] = {}
    for observation in receiver_observations:
        if observation["take_command_id"] in matches:
            raise EvidenceError(
                "CORRELATION_INVALID",
                f"more than one valid RTMP first packet for Take {observation['take_command_id']}",
            )
        identity = str(observation["packet_identity"])
        if identity in seen_identity:
            raise EvidenceError("CORRELATION_INVALID", f"duplicate RTMP packet identity {identity!r}")
        seen_identity.add(identity)
        candidates = encoded_by_take.get(observation["take_command_id"], [])
        if not candidates:
            raise EvidenceError(
                "CORRELATION_INVALID",
                f"no encoded producer packet exists for RTMP Take {observation['take_command_id']}",
            )
        if len(candidates) > 1:
            raise EvidenceError(
                "CORRELATION_INVALID",
                f"more than one valid first encoded packet for Take(s) [{observation['take_command_id']}]",
            )
        match = candidates[0]
        if observation["packet_index"] != match["packet_index"]:
            raise EvidenceError(
                "CORRELATION_INVALID",
                f"RTMP packet index for Take {observation['take_command_id']} does not match "
                "the encoded stream packet index",
            )
        receiver_pts = _packet_pts_fraction(observation)
        receiver_dts = Fraction(
            int(observation["packet_dts"]) * int(observation["packet_timebase_num"]),
            int(observation["packet_timebase_den"]),
        )
        tolerance = _packet_match_tolerance(observation)
        producer_pts = _packet_pts_fraction(match)
        producer_dts = Fraction(
            int(match["packet_dts"]) * int(match["packet_timebase_num"]),
            int(match["packet_timebase_den"]),
        )
        pair_lower = max(
            receiver_pts - producer_pts - tolerance,
            receiver_dts - producer_dts - tolerance,
        )
        pair_upper = min(
            receiver_pts - producer_pts + tolerance,
            receiver_dts - producer_dts + tolerance,
        )
        if pair_lower > pair_upper:
            raise EvidenceError(
                "CORRELATION_INVALID",
                f"RTMP PTS/DTS for Take {observation['take_command_id']} cannot share one mux offset",
            )
        computed_lower = pair_lower if computed_lower is None else max(computed_lower, pair_lower)
        computed_upper = pair_upper if computed_upper is None else min(computed_upper, pair_upper)
        if computed_lower > computed_upper:
            raise EvidenceError(
                "CORRELATION_INVALID",
                f"RTMP mux offset drifted at Take {observation['take_command_id']}",
            )
        matches[observation["take_command_id"]] = {
            "receiver_packet_identity": identity,
            "receiver_packet_index": observation["packet_index"],
            "producer_packet_index": match["packet_index"],
            "pts_delta_seconds": float(receiver_pts - _packet_pts_fraction(match)),
        }

    if receiver["correlated_packet_count"] != len(receiver_observations):
        raise EvidenceError(
            "CORRELATION_INVALID",
            "session RTMP correlated_packet_count does not match receiver observations",
        )
    if computed_lower != advertised_lower or computed_upper != advertised_upper:
        raise EvidenceError(
            "CORRELATION_INVALID",
            "session RTMP mux-offset calibration does not equal the recomputed stream interval",
        )

    # The receiver's first-packet observations must form one monotone video
    # stream.  Gaps are acceptable; regressions and duplicate indices are not.
    previous_index = -1
    previous_pts: Fraction | None = None
    for observation in sorted(receiver_observations, key=lambda item: item["observed_at_monotonic_ns"]):
        if observation["packet_index"] <= previous_index:
            raise EvidenceError("FRAME_ORDER_INVALID", "RTMP receiver packet index regressed or repeated")
        current_pts = _packet_pts_fraction(observation)
        if previous_pts is not None and current_pts < previous_pts:
            raise EvidenceError("FRAME_ORDER_INVALID", "RTMP receiver packet PTS regressed")
        previous_index = observation["packet_index"]
        previous_pts = current_pts
    return matches


def _event_order(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_seq: dict[int, dict[str, Any]] = {}
    for event in events:
        seq = _integer(event["server_seq"], "event.server_seq", non_negative=False)
        if seq <= 0:
            raise EvidenceError("EVENT_ORDER_INVALID", "event.server_seq must be positive")
        if seq in by_seq and by_seq[seq] != event:
            raise EvidenceError("EVENT_ORDER_INVALID", f"two different events use server_seq={seq}")
        if seq in by_seq:
            raise EvidenceError("EVENT_ORDER_INVALID", f"duplicate event server_seq={seq}")
        by_seq[seq] = dict(event)
    ordered = [by_seq[key] for key in sorted(by_seq)]
    previous_observed = -1
    for event in ordered:
        observed = _integer(event["observed_at_monotonic_ns"], "event.observed_at_monotonic_ns")
        if observed < previous_observed:
            raise EvidenceError("CLOCK_INVALID", "event observation timestamps are not monotone by server_seq")
        previous_observed = observed
    return ordered


def analyze_trace(
    trace: Trace,
    *,
    minimum_takes: int = 100,
    minimum_warmup: int = 100,
    minimum_resource_samples: int = 10,
) -> dict[str, Any]:
    """Correlate one validated trace and construct its report."""

    if minimum_takes < 1 or minimum_warmup < 0 or minimum_resource_samples < 1:
        raise ValueError("minimum thresholds must be positive (warmup may be zero)")
    session = trace.session
    ordered_events = _event_order(trace.events)
    accepted: dict[str, dict[str, Any]] = {}
    committed: dict[str, dict[str, Any]] = {}
    aborted: dict[str, dict[str, Any]] = {}
    rejected_count = 0
    latest_committed_frame = 0
    latest_committed_pts = 0
    for event in ordered_events:
        event_type = event["event_type"]
        if event_type == "TakeAccepted":
            take_id = event["take_command_id"]
            if take_id in accepted:
                raise EvidenceError("CORRELATION_INVALID", f"TakeAccepted repeated for {take_id}")
            if event["command_id"] != take_id:
                raise EvidenceError("CORRELATION_INVALID", "TakeAccepted.command_id must equal take_command_id")
            accepted[take_id] = event
        elif event_type == "TakeCommitted":
            take_id = event["take_command_id"]
            if take_id in committed:
                raise EvidenceError("CORRELATION_INVALID", f"TakeCommitted repeated for {take_id}")
            committed[take_id] = event
            if event["frame_id"] < latest_committed_frame or event["pts_ns"] < latest_committed_pts:
                raise EvidenceError("FRAME_ORDER_INVALID", f"TakeCommitted frame ID/PTS regressed at Take {take_id}")
            latest_committed_frame = event["frame_id"]
            latest_committed_pts = event["pts_ns"]
        elif event_type == "TakeAborted":
            take_id = event["take_command_id"]
            if take_id in aborted:
                raise EvidenceError("CORRELATION_INVALID", f"TakeAborted repeated for {take_id}")
            aborted[take_id] = event
            if event["reason"] == "queue_rejected":
                terminal_frame = event["last_committed_frame_id"]
                terminal_pts = event["last_committed_pts_ns"]
                if terminal_frame != latest_committed_frame or terminal_pts != latest_committed_pts:
                    raise EvidenceError(
                        "FRAME_ORDER_INVALID",
                        f"queue_rejected TakeAborted does not expose the latest committed frame/PTS for Take {take_id}",
                    )
        elif event_type == "CommandRejected":
            rejected_count += 1

    unknown_commits = sorted(set(committed) - set(accepted))
    unknown_aborts = sorted(set(aborted) - set(accepted))
    # A physical frame queue rejection is terminal before TakeAccepted: the
    # runtime deliberately never timestamps an admission that libobs refused.
    # It is still correlated by take_command_id and must carry the terminal
    # frame/PTS, while all other aborts require a prior acceptance.
    invalid_unaccepted_aborts = sorted(
        take_id for take_id in unknown_aborts if aborted[take_id]["reason"] != "queue_rejected"
    )
    if unknown_commits or invalid_unaccepted_aborts:
        raise EvidenceError(
            "CORRELATION_INVALID",
            "outcome references unknown Take(s): "
            f"committed={unknown_commits}, aborted={invalid_unaccepted_aborts}",
        )

    unsettled: list[str] = []
    takes: dict[str, dict[str, Any]] = {}
    for take_id, take_accepted in accepted.items():
        take_commit = committed.get(take_id)
        take_abort = aborted.get(take_id)
        if take_commit is not None and take_abort is not None:
            raise EvidenceError("CORRELATION_INVALID", f"Take {take_id} has both commit and abort")
        if take_commit is None and take_abort is None:
            unsettled.append(take_id)
            continue
        outcome = take_commit if take_commit is not None else take_abort
        if outcome is None:  # guarded by the unsettled branch above
            raise EvidenceError("CORRELATION_INVALID", f"Take {take_id} has no terminal outcome")
        if outcome["command_id"] != take_accepted["command_id"] or outcome["intent_id"] != take_accepted["intent_id"]:
            raise EvidenceError("CORRELATION_INVALID", f"Take {take_id} outcome IDs do not match TakeAccepted")
        if outcome["observed_at_monotonic_ns"] < take_accepted["observed_at_monotonic_ns"]:
            raise EvidenceError("CLOCK_INVALID", f"Take {take_id} outcome precedes TakeAccepted")
        if take_commit is not None:
            if take_commit["previous_revisions"] != take_accepted["revisions"]:
                raise EvidenceError("CORRELATION_INVALID", f"Take {take_id} commit previous_revisions do not match acceptance")
            if (
                take_commit["target_lane_id"] != take_accepted["target_lane_id"]
                or take_commit["target_scene_id"] != take_accepted["target_scene_id"]
            ):
                raise EvidenceError("CORRELATION_INVALID", f"Take {take_id} commit target does not match acceptance")
            if take_commit["observed_at_monotonic_ns"] > take_accepted["freeze_until_monotonic_ns"]:
                raise EvidenceError("CLOCK_INVALID", f"Take {take_id} committed after its acceptance deadline")
            if take_commit["frame_id"] < 0 or take_commit["pts_ns"] < 0:
                raise EvidenceError("CORRELATION_INVALID", f"Take {take_id} commit has invalid frame/PTS")
        takes[take_id] = {"accepted": take_accepted, "commit": take_commit, "abort": take_abort}

    previous_commit_frame = -1
    previous_commit_pts = -1
    for take_id in sorted(
        committed,
        key=lambda key: (committed[key]["observed_at_monotonic_ns"], committed[key]["server_seq"]),
    ):
        event = committed[take_id]
        if event["frame_id"] < previous_commit_frame or event["pts_ns"] < previous_commit_pts:
            raise EvidenceError("FRAME_ORDER_INVALID", f"TakeCommitted frame ID/PTS regressed at Take {take_id}")
        previous_commit_frame = event["frame_id"]
        previous_commit_pts = event["pts_ns"]

    # ``session.warmup_takes`` is a partition of this same runtime session,
    # not a declaration that the first measurements were already warm.  The
    # driver executes those committed Takes first, then the measured sample;
    # only the suffix after the observed warm-up partition may enter SLO
    # percentiles.  Keeping the partition here prevents a trace from claiming
    # 100 warm + 100 measured while actually measuring the warm-up frames.
    committed_order = sorted(
        committed,
        key=lambda key: (committed[key]["observed_at_monotonic_ns"], committed[key]["server_seq"]),
    )
    declared_warmup = int(session["warmup_takes"])
    warmup_take_ids = set(committed_order[:declared_warmup])
    measured_take_ids = set(committed_order[declared_warmup:])

    # Observations before the accepted frame-boundary commit are deliberately
    # retained for diagnostics but never admitted as evidence.  This matters
    # in a real raw callback, which naturally sees frames continuously.
    selected: dict[str, list[dict[str, Any]]] = {boundary: [] for boundary in BOUNDARIES}
    ignored_pre_commit: dict[str, int] = {boundary: 0 for boundary in BOUNDARIES}
    for observation in trace.observations:
        if observation["boundary"] not in session["capture_paths"]:
            raise EvidenceError(
                "SCHEMA_INVALID",
                f"observation boundary {observation['boundary']!r} was not declared in session.capture_paths",
            )
        take_id = observation["take_command_id"]
        if take_id not in accepted:
            raise EvidenceError("CORRELATION_INVALID", f"observation references unknown Take {take_id}")
        take_accepted = accepted[take_id]
        if observation["command_id"] != take_accepted["command_id"] or observation["intent_id"] != take_accepted["intent_id"]:
            raise EvidenceError("CORRELATION_INVALID", f"observation IDs do not match Take {take_id}")
        take_commit = committed.get(take_id)
        if not observation["valid"]:
            continue
        if take_commit is None:
            raise EvidenceError("CORRELATION_INVALID", f"valid observation exists for non-committed Take {take_id}")
        after_commit = (
            observation["observed_at_monotonic_ns"] >= take_commit["observed_at_monotonic_ns"]
            and observation["frame_id"] >= take_commit["frame_id"]
            and observation["pts_ns"] >= take_commit["pts_ns"]
        )
        if not after_commit:
            ignored_pre_commit[observation["boundary"]] += 1
            continue
        if observation["revisions"] != take_commit["revisions"]:
            raise EvidenceError("CORRELATION_INVALID", f"observation revisions do not match committed Take {take_id}")
        selected[observation["boundary"]].append(observation)

    # A producer reset or a cross-session mix must not be hidden by choosing a
    # first sample.  Check all valid post-commit observations in each stream
    # before selecting the first one per Take.
    for boundary, observations in selected.items():
        previous_frame = -1
        previous_pts = -1
        for observation in sorted(observations, key=lambda value: value["observed_at_monotonic_ns"]):
            if observation["frame_id"] < previous_frame or observation["pts_ns"] < previous_pts:
                raise EvidenceError("FRAME_ORDER_INVALID", f"{boundary} frame ID/PTS regressed")
            previous_frame = observation["frame_id"]
            previous_pts = observation["pts_ns"]

    rtmp_matches = _validate_rtmp_packet_correlations(selected, session)

    first_by_take_boundary: dict[tuple[str, str], dict[str, Any]] = {}
    for boundary, observations in selected.items():
        if boundary in ("encoded_first_packet", "rtmp_first_packet"):
            valid_packet_counts: dict[str, int] = {}
            for observation in observations:
                take_id = observation["take_command_id"]
                valid_packet_counts[take_id] = valid_packet_counts.get(take_id, 0) + 1
            duplicate_packets = sorted(take_id for take_id, count in valid_packet_counts.items() if count > 1)
            if duplicate_packets:
                raise EvidenceError(
                    "CORRELATION_INVALID",
                    f"more than one valid first "
                    f"{'encoded' if boundary == 'encoded_first_packet' else 'RTMP'} packet "
                    f"for Take(s) {duplicate_packets}",
                )
        for observation in sorted(observations, key=lambda value: (value["observed_at_monotonic_ns"], value["frame_id"], value["pts_ns"])):
            key = (observation["take_command_id"], boundary)
            first_by_take_boundary.setdefault(key, observation)

    latency: dict[str, dict[str, Any]] = {}
    for boundary in BOUNDARIES:
        values: list[float] = []
        for (take_id, sample_boundary), observation in first_by_take_boundary.items():
            if sample_boundary != boundary:
                continue
            if take_id not in measured_take_ids:
                continue
            accepted_at = accepted[take_id]["observed_at_monotonic_ns"]
            delta_ns = observation["observed_at_monotonic_ns"] - accepted_at
            if delta_ns < 0:
                raise EvidenceError("CLOCK_INVALID", f"negative {boundary} latency for Take {take_id}")
            values.append(delta_ns / 1_000_000.0)
        summary = _stats(values)
        uncertainty_ms = 0.0
        if boundary == "rtmp_first_packet" and values:
            receiver = session.get("rtmp_receiver")
            if not isinstance(receiver, Mapping) or type(receiver.get("clock_bound_ns")) is not int:
                raise EvidenceError(
                    "CORRELATION_INVALID",
                    "rtmp_first_packet latency requires a valid receiver clock bound",
                )
            uncertainty_ms = receiver["clock_bound_ns"] / 1_000_000.0
            summary["clock_bound_ms"] = round(uncertainty_ms, 6)
            summary["p95_conservative_ms"] = round(
                float(summary["p95_ms"]) + uncertainty_ms, 6
            )
        status = (
            _status_for_latency(
                summary,
                minimum_takes if boundary in REQUIRED_BOUNDARIES else 1,
                boundary,
                uncertainty_ms=uncertainty_ms,
            )
            if values
            else "UNPROVEN"
        )
        if boundary not in REQUIRED_BOUNDARIES and not values:
            status = "NOT_REQUIRED"
        latency[boundary] = {
            **summary,
            "slo_p95_ms": SLO_MS.get(boundary),
            "status": status,
            "separate_boundary": boundary != "encoder_input_raw",
        }

    resource_report: dict[str, Any] = {
        "status": "UNPROVEN",
        "sample_counts": {},
        "active_sample_counts": {},
        "eligible_sample_counts": {},
        "inactive_sample_counts": {},
        "rtmp_load_active_sample_counts": {},
        "rtmp_eligible_sample_counts": {},
        "metrics": {},
        "comparison": {},
    }
    for mode in RESOURCE_MODES:
        samples = [sample for sample in trace.resources if sample["sample_mode"] == mode]
        active_samples = [sample for sample in samples if sample.get("encoder_active") is True]
        eligible_samples = [
            sample
            for sample in active_samples
            if sample.get("encoder_family") == "nvenc"
            and sample.get("rtmp_load_active") is True
        ]
        resource_report["sample_counts"][mode] = len(samples)
        resource_report["active_sample_counts"][mode] = len(active_samples)
        resource_report["eligible_sample_counts"][mode] = len(eligible_samples)
        resource_report["inactive_sample_counts"][mode] = len(samples) - len(active_samples)
        resource_report["rtmp_load_active_sample_counts"][mode] = sum(
            sample.get("rtmp_load_active") is True for sample in samples
        )
        resource_report["rtmp_eligible_sample_counts"][mode] = len(eligible_samples)
        admitted_samples = eligible_samples if session["codec"] == "nvenc" else active_samples
        resource_report["metrics"][mode] = {
            metric: _resource_stats([sample[metric] for sample in admitted_samples]) for metric in RESOURCE_METRICS
        }
    if session["codec"] == "x264":
        # AC-13 is a resource delta for the NVENC workload specifically.  An
        # x264 trace may carry diagnostic resource samples, but they must not
        # be allowed to satisfy or substitute this criterion.
        resource_report["status"] = "NOT_APPLICABLE"
        resource_report["reason"] = "AC-13 applies only to the NVENC resource workload; x264 samples are diagnostic only"
    else:
        reference_samples = [
            sample
            for sample in trace.resources
            if sample["sample_mode"] == "reference"
            and sample.get("encoder_active") is True
            and sample.get("encoder_family") == "nvenc"
            and sample.get("rtmp_load_active") is True
        ]
        dual_samples = [
            sample
            for sample in trace.resources
            if sample["sample_mode"] == "dual_lane"
            and sample.get("encoder_active") is True
            and sample.get("encoder_family") == "nvenc"
            and sample.get("rtmp_load_active") is True
        ]
        if (
            len(reference_samples) >= minimum_resource_samples
            and len(dual_samples) >= minimum_resource_samples
            and all(bool(session["workload"][key]) for key in ("wgc", "cef", "nvenc"))
        ):
            for metric in ("frame_render_ms", "resident_bytes"):
                reference_p50 = resource_report["metrics"]["reference"][metric]["p50"]
                dual_p50 = resource_report["metrics"]["dual_lane"][metric]["p50"]
                delta = float(dual_p50) - float(reference_p50)
                expected = RESOURCE_REFERENCE["extra_frame_render_ms"] if metric == "frame_render_ms" else RESOURCE_REFERENCE["extra_resident_bytes"]
                resource_report["comparison"][metric] = {
                    "reference_p50": reference_p50,
                    "dual_lane_p50": dual_p50,
                    "delta": round(delta, 6),
                    "known_reference_delta": expected,
                    "within_known_reference": delta <= expected,
                }
            resource_report["status"] = "MEASURED"
        else:
            resource_report["status"] = "UNPROVEN"
            resource_report["reason"] = (
                "requires both resource modes, minimum active NVENC-encoder samples, "
                "WGC+CEF+NVENC workload flags, and symmetric active RTMP receiver load"
            )

    criteria = {
        "AC-07": {"boundary": "encoder_input_raw", **latency["encoder_input_raw"]},
        "AC-08": {"boundary": "directshow_return", **latency["directshow_return"]},
        "AC-11": {
            "status": "PASS" if all(event.get("command_id") and event.get("intent_id") and event.get("revisions") for event in ordered_events) else "FAIL",
            "event_count": len(ordered_events),
            "accepted_count": len(accepted),
            "rejected_count": rejected_count,
            "unsettled_take_count": len(unsettled),
            "all_events_contract_validated": True,
        },
        "AC-12": {
            "boundary": "rtmp_first_packet",
            **latency["rtmp_first_packet"],
            "correlated_packet_count": len(rtmp_matches),
            "correlations": rtmp_matches,
            "encoder_callback_auxiliary": latency["encoded_first_packet"],
        },
        "AC-13": {
            "status": resource_report["status"],
            "resource": resource_report,
            "capacity_not_declared_from_reference": True,
            "applicable": session["codec"] == "nvenc",
        },
    }
    hard_fail = any(criteria[key]["status"] == "FAIL" for key in ("AC-07", "AC-08", "AC-11", "AC-12"))
    required_unproven = any(criteria[key]["status"] not in ("PASS", "MEASURED") for key in ("AC-07", "AC-08", "AC-12"))
    if session["codec"] == "nvenc":
        required_unproven = required_unproven or criteria["AC-13"]["status"] not in ("MEASURED",)
    if hard_fail:
        status = "FAIL"
    elif session["evidence_kind"] != "runtime":
        status = "FIXTURE_ONLY"
    elif (
        required_unproven
        or len(warmup_take_ids) < minimum_warmup
        or len(measured_take_ids) < minimum_takes
        or unsettled
    ):
        status = "UNPROVEN"
    else:
        status = "PASS"

    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "source": trace.source,
        "session": session,
        "event_coverage": {
            "validated_event_count": len(ordered_events),
            "take_accepted": len(accepted),
            "take_committed": len(committed),
            "take_aborted": len(aborted),
            "command_rejected": rejected_count,
            "unsettled_take_ids": unsettled,
            "warmup_takes_observed": len(warmup_take_ids),
            "measured_takes_observed": len(measured_take_ids),
            "server_seq": [event["server_seq"] for event in ordered_events],
        },
        "takes": {
            "accepted": len(accepted),
            "committed": len(committed),
            "aborted": len(aborted),
            "warmup_takes_declared": session["warmup_takes"],
            "warmup_takes_observed": len(warmup_take_ids),
            "measured_takes_observed": len(measured_take_ids),
            "total_committed_takes": len(committed_order),
            "minimum_warmup_required": minimum_warmup,
            "minimum_measurements_required": minimum_takes,
        },
        "latency": latency,
        "ignored_valid_samples_before_commit": ignored_pre_commit,
        "resources": resource_report,
        "criteria": criteria,
        "notes": [
            "Latency starts at TakeAccepted.observed_at_monotonic_ns and ends at the first valid post-commit observation for the named boundary.",
            "DirectShow return and encoder/raw input are separate boundaries; neither is inferred from the other.",
            "encoded_first_packet is auxiliary pre-network encoder evidence and can never satisfy AC-12.",
            "rtmp_first_packet is the first video packet observed by the loopback receiver/demux; it is not wire-level or decoded-frame evidence.",
            "Each valid RTMP packet must match the producer packet index and preserve one constant PTS/DTS mux offset within half a receiver tick.",
            "Decoded and antenna timings are diagnostic only and carry no acceptance SLO.",
            "The first session.warmup_takes committed Takes are excluded from latency percentiles; measured counts are the observed committed suffix.",
            "Resource sample counts include all records; AC-13 uses only samples with encoder_active=true and encoder_family=nvenc.",
            "AC-13 admits only the conjunction encoder_active=true, encoder_family=nvenc, and observed rtmp_load_active=true in both phases; early pre-stream samples remain diagnostic.",
            "A fixture report is never a runtime acceptance; run the same command against a runtime trace with evidence_kind=runtime.",
        ],
    }


def analyze_traces(
    traces: Sequence[Trace],
    *,
    minimum_takes: int = 100,
    minimum_warmup: int = 100,
    minimum_resource_samples: int = 10,
) -> dict[str, Any]:
    """Analyze one or more independent codec/session traces."""

    if not traces:
        raise ValueError("at least one trace is required")
    reports = [
        analyze_trace(
            trace,
            minimum_takes=minimum_takes,
            minimum_warmup=minimum_warmup,
            minimum_resource_samples=minimum_resource_samples,
        )
        for trace in traces
    ]
    if len(reports) == 1:
        return reports[0]

    # Keep the campaign boundary visible in the aggregate report.  Latency
    # and resource samples are analyzed inside each Trace above; this summary
    # deliberately carries only per-session metadata and criterion statuses,
    # so a passing x264 campaign can never donate samples to NVENC's AC-13.
    codec_coverage = [
        {
            "codec": report["session"]["codec"],
            "runtime_instance_id": report["session"]["runtime_instance_id"],
            "session_id": report["session"]["session_id"],
            "status": report["status"],
            "criteria": {
                key: report["criteria"][key]["status"]
                for key in ("AC-07", "AC-08", "AC-11", "AC-12", "AC-13")
            },
            "resource_status": report["resources"]["status"],
        }
        for report in reports
    ]
    observed_codecs = sorted({entry["codec"] for entry in codec_coverage})
    complete_codec_coverage = set(REQUIRED_CODECS).issubset(observed_codecs) and all(
        entry["status"] == "PASS" for entry in codec_coverage
    )
    statuses = {report["status"] for report in reports}
    if "FAIL" in statuses:
        status = "FAIL"
    elif statuses == {"FIXTURE_ONLY"}:
        status = "FIXTURE_ONLY"
    elif not complete_codec_coverage:
        # A multi-trace acceptance report is complete only when it contains
        # one or more independent passing campaigns for both required
        # codecs.  In particular, two passing NVENC traces cannot masquerade
        # as x264 coverage, and no samples are pooled to make them pass.
        status = "UNPROVEN"
    else:
        status = "PASS"
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "campaigns": reports,
        "codec_coverage": codec_coverage,
        "required_codecs": list(REQUIRED_CODECS),
        "observed_codecs": observed_codecs,
        "complete_codec_coverage": complete_codec_coverage,
        "notes": [
            "Each campaign is an independent runtime/session; do not pool samples across codec/path boundaries.",
            "AC-13 is measured only by the NVENC reference-versus-dual resource pair; x264 reports NOT_APPLICABLE.",
        ],
    }


def _print_summary(report: Mapping[str, Any]) -> None:
    print(f"probe-take-latency: {report['status']}")
    reports = report.get("campaigns") or [report]
    for campaign in reports:
        session = campaign.get("session", {})
        codec = session.get("codec", "unknown")
        for boundary in BOUNDARIES:
            summary = campaign.get("latency", {}).get(boundary, {})
            print(
                f"  {codec}/{boundary}: n={summary.get('count', 0)} "
                f"p50={summary.get('p50_ms')}ms p95={summary.get('p95_ms')}ms "
                f"p99={summary.get('p99_ms')}ms status={summary.get('status')}"
            )
        resources = campaign.get("resources", {})
        print(
            f"  {codec}/resources: status={resources.get('status')} "
            f"samples={resources.get('sample_counts')} active={resources.get('active_sample_counts')}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Pulsar #246 latency/capacity JSONL evidence")
    parser.add_argument("--trace", nargs="+", type=Path, required=True, help="one or more runtime JSONL traces")
    parser.add_argument("--output", type=Path, help="write deterministic JSON report to this path")
    parser.add_argument("--min-takes", type=int, default=100, help="minimum valid Takes per required boundary (default: 100)")
    parser.add_argument("--min-warmup", type=int, default=100, help="minimum completed warm-up Takes (default: 100)")
    parser.add_argument("--min-resource-samples", type=int, default=10, help="minimum samples per resource mode (default: 10)")
    args = parser.parse_args(argv)
    try:
        traces = [parse_trace(path) for path in args.trace]
        report = analyze_traces(
            traces,
            minimum_takes=args.min_takes,
            minimum_warmup=args.min_warmup,
            minimum_resource_samples=args.min_resource_samples,
        )
    except (EvidenceError, OSError, ValueError) as exc:
        print(f"probe-take-latency: ERROR: {exc}", file=sys.stderr)
        return 1
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        except OSError as exc:
            print(f"probe-take-latency: ERROR: cannot write {args.output}: {exc}", file=sys.stderr)
            return 1
    _print_summary(report)
    if report["status"] == "PASS":
        return 0
    if report["status"] in ("UNPROVEN", "FIXTURE_ONLY"):
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
