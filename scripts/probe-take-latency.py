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
    guarantee.  RTMP reception remains an external Probe measurement.
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
declared from that reference.

Usage::

    python scripts/probe-take-latency.py --trace x264.jsonl nvenc.jsonl \
        --output latency-report.json

This module uses only the Python standard library plus the repository's
transport-neutral scene-switch validator.  It has no OBS/libobs dependency.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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
    "decoded_first_frame",
    "antenna_first_frame",
)
REQUIRED_BOUNDARIES = (
    "encoder_input_raw",
    "directshow_return",
    "encoded_first_packet",
)
SLO_MS = {
    "encoder_input_raw": 50.0,
    "directshow_return": 75.0,
    "encoded_first_packet": 15.0,
}
RESOURCE_REFERENCE = {
    "extra_frame_render_ms": 0.091,
    "extra_resident_bytes": 3_130_000,
}
RESOURCE_MODES = ("reference", "dual_lane")
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
SESSION_OPTIONAL = {"comparison_id", "notes", "source_types"}
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
OBSERVATION_OPTIONAL = {"program_frame", "packet_index", "frame_hash", "notes"}
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
RESOURCE_OPTIONAL = {"gpu_memory_bytes", "notes"}
RESOURCE_ALLOWED = RESOURCE_REQUIRED | RESOURCE_OPTIONAL


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
        if "packet_index" not in obj or _integer(obj["packet_index"], "observation.packet_index", line=line) != 0:
            raise EvidenceError("BOUNDARY_INVALID", "encoded_first_packet requires packet_index=0", line=line)
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


def _status_for_latency(summary: Mapping[str, Any], minimum_takes: int, boundary: str) -> str:
    if summary["count"] < minimum_takes:
        return "UNPROVEN"
    slo = SLO_MS.get(boundary)
    if slo is not None and float(summary["p95_ms"]) > slo:
        return "FAIL"
    return "PASS"


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
        outcome = take_commit or take_abort
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

    first_by_take_boundary: dict[tuple[str, str], dict[str, Any]] = {}
    for boundary, observations in selected.items():
        if boundary == "encoded_first_packet":
            valid_packet_counts: dict[str, int] = {}
            for observation in observations:
                take_id = observation["take_command_id"]
                valid_packet_counts[take_id] = valid_packet_counts.get(take_id, 0) + 1
            duplicate_packets = sorted(take_id for take_id, count in valid_packet_counts.items() if count > 1)
            if duplicate_packets:
                raise EvidenceError(
                    "CORRELATION_INVALID",
                    f"more than one valid first encoded packet for Take(s) {duplicate_packets}",
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
            accepted_at = accepted[take_id]["observed_at_monotonic_ns"]
            delta_ns = observation["observed_at_monotonic_ns"] - accepted_at
            if delta_ns < 0:
                raise EvidenceError("CLOCK_INVALID", f"negative {boundary} latency for Take {take_id}")
            values.append(delta_ns / 1_000_000.0)
        summary = _stats(values)
        status = _status_for_latency(summary, minimum_takes if boundary in REQUIRED_BOUNDARIES else 1, boundary) if values else "UNPROVEN"
        if boundary not in REQUIRED_BOUNDARIES and not values:
            status = "NOT_REQUIRED"
        latency[boundary] = {
            **summary,
            "slo_p95_ms": SLO_MS.get(boundary),
            "status": status,
            "separate_boundary": boundary != "encoder_input_raw",
        }

    resource_report: dict[str, Any] = {"status": "UNPROVEN", "sample_counts": {}, "metrics": {}, "comparison": {}}
    for mode in RESOURCE_MODES:
        samples = [sample for sample in trace.resources if sample["sample_mode"] == mode]
        resource_report["sample_counts"][mode] = len(samples)
        resource_report["metrics"][mode] = {
            metric: _resource_stats([sample[metric] for sample in samples]) for metric in RESOURCE_METRICS
        }
    reference_samples = [sample for sample in trace.resources if sample["sample_mode"] == "reference"]
    dual_samples = [sample for sample in trace.resources if sample["sample_mode"] == "dual_lane"]
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
        resource_report["reason"] = "requires both resource modes, minimum samples, and WGC+CEF+NVENC workload flags"

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
        "AC-12": {"boundary": "encoded_first_packet", **latency["encoded_first_packet"]},
        "AC-13": {
            "status": resource_report["status"],
            "resource": resource_report,
            "capacity_not_declared_from_reference": True,
        },
    }
    hard_fail = any(criteria[key]["status"] == "FAIL" for key in ("AC-07", "AC-08", "AC-11", "AC-12"))
    required_unproven = any(criteria[key]["status"] not in ("PASS", "MEASURED") for key in ("AC-07", "AC-08", "AC-12", "AC-13"))
    if hard_fail:
        status = "FAIL"
    elif session["evidence_kind"] != "runtime":
        status = "FIXTURE_ONLY"
    elif required_unproven or session["warmup_takes"] < minimum_warmup or unsettled:
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
            "server_seq": [event["server_seq"] for event in ordered_events],
        },
        "takes": {
            "accepted": len(accepted),
            "committed": len(committed),
            "aborted": len(aborted),
            "warmup_takes_declared": session["warmup_takes"],
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
            "encoded_first_packet is the pre-network encoder callback; RTMP receiver ingress is an external Probe boundary.",
            "Decoded and antenna timings are diagnostic only and carry no acceptance SLO.",
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
    statuses = {report["status"] for report in reports}
    if "FAIL" in statuses:
        status = "FAIL"
    elif statuses == {"PASS"}:
        status = "PASS"
    elif "FIXTURE_ONLY" in statuses:
        status = "FIXTURE_ONLY"
    else:
        status = "UNPROVEN"
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "campaigns": reports,
        "notes": ["Each campaign is an independent runtime/session; do not pool samples across codec/path boundaries."],
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
        print(f"  {codec}/resources: status={resources.get('status')} samples={resources.get('sample_counts')}")


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
