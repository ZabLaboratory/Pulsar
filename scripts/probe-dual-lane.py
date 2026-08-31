#!/usr/bin/env python3
"""Runtime probe for Pulsar's hot A/B scene lanes (issue #246).

The probe drives the public obs-websocket v5 boundary only.  It starts with
one logical scene on air, alternates a second scene into Preview, and commits
``--takes`` studio-mode Cuts.  Pulsar's structured dual-lane logs expose
computed lane/surface relationship checks and frame-boundary frame/PTS
commit; those values are checked for every Take without publishing native
addresses. It also mutates the public
Program and Preview scenes after their lane is bound, mutates the former
OnAir scene after the first Take, and checks that the logical selections stay
distinct.  The raw NV12 time-code probe remains the pixel-level proof that
those live mutations reach only their selected lane.

Optional non-traced smoke checks (no acceptance artifact) are::

    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder x264 --takes 100
    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder nvenc --takes 100

The canonical three-command acceptance sequence is::

    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder x264 --takes 100 \
        --trace artifacts/249/x264-rtmp.jsonl --runtime-id runtime-x264-001 \
        --build-revision <candidate-sha> --capture-window <visible-title:class:exe> \
        --cef-workload --rtmp-receiver
    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder nvenc \
        --trace artifacts/249/nvenc-rtmp.jsonl --runtime-id runtime-nvenc-001 \
        --build-revision <candidate-sha> --capture-window <visible-title:class:exe> \
        --cef-workload --resource-mode reference --resource-only --rtmp-receiver
    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder nvenc --takes 100 \
        --trace artifacts/249/nvenc-rtmp.jsonl --runtime-id runtime-nvenc-001 \
        --build-revision <candidate-sha> --capture-window <visible-title:class:exe> \
        --cef-workload --trace-append --resource-mode dual_lane --rtmp-receiver

The x264 trace is a latency-only campaign (AC-13 is not applicable); the
NVENC trace's reference phase must run with ``--resource-mode reference
--resource-only`` before the dual-lane append above.  The reference phase
starts and verifies a real recording so its resource samples attest an active
encoder rather than only a requested codec.

The dual-lane append keeps Stream and Record alive after the 200th Take until
the requested minimum of observed active NVENC plus RTMP resource samples is
present. This sampler wait is bounded and does not add resource records to
Take latency percentiles or scene events.

For every traced Take, dispatch is serialized behind a completeness barrier:
the same runtime/command/intent/take/frame/PTS identity must produce exactly
one valid encoder-input, DirectShow-return and encoded-packet record plus one
unique RTMP receiver packet before the next Take is sent. The barrier only
waits; it never changes observation timestamps or percentile selection.

Exit codes are 0 (pass), 1 (assertion/runtime failure), 2 (usage or missing
WebSocket dependency), and 3 (typed environment skip, for example no binary
or no NVENC device).  This probe validates the routing and identity contract;
the raw NV12 time-code probe remains the pixel-level proof for no mixed frame.

For traced runs, ``--takes 100`` executes 100 warm-up Takes followed by 100
measured Takes in the same process; the trace parser excludes the warm-up
prefix from latency percentiles.  Non-traced smoke runs execute 100 total.

The process boundary is deliberate: no libobs/OBS DLL is loaded and no native
object is accessed from Python.  Only obs-websocket v5 JSON frames and the
Pulsar child process's structured diagnostics are used.  With ``--trace``, the
same public requests carry an explicit, opt-in transaction envelope; the
runtime writes session/events/raw/encoded-output records and starts the
ProgramReturn producer for an independent DirectShow consumer. With
``--rtmp-receiver``, it also starts the native streamOutput to a real local
FFmpeg RTMP receiver and fuses receiver packet observations only after
shutdown. ``--resource-mode`` enables
the native OBS/platform resource sampler; use ``--resource-only`` for the
single-producer-pair reference phase and ``--trace-append --resource-mode dual_lane``
for the correlated two-pair A/B phase.

Normal and dual-lane canary processes explicitly set
``PULSAR_DUAL_LANE_ENABLED=1`` and require the matching startup decision in
the log.  The reference phase explicitly sets
``PULSAR_DISABLE_DUAL_LANE=1`` and enables the resource-reference mode; the
runtime reports the effective source as ``resource-reference`` because that
mode owns the compatibility-path decision.  The probe requires that effective
decision, so a probe-side environment assignment can never be mistaken for a
runtime consumer.

The --cef-workload mode starts an ephemeral loopback HTTP server for a
deterministic page and requires --capture-window to name an actual visible WGC
target. It verifies source settings, enabled scene bindings, and decoded
non-black screenshots before running Takes. The server is stopped in the same
cleanup path as the Pulsar child.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from fractions import Fraction
import hashlib
import importlib.util
import http.server
import json
import os
import pathlib
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Mapping, cast

try:
    import websockets
except ImportError:
    print("error: pip install websockets (pure WebSocket client)", file=sys.stderr)
    raise SystemExit(2)


EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_SKIP = 3

WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
WINDOWS_HANDLE_FLAG_INHERIT = 0x00000001

# The runtime's ``os_gettime_ns`` uses QueryPerformanceCounter on Windows.
# Python's monotonic clocks are not interchangeable on every supported Python
# build: this host exposed a monotonic_ns epoch about 2.6 s behind QPC while
# perf_counter_ns tracked QPC.  Deadlines crossing the WebSocket/native seam
# must therefore use the QPC-compatible source explicitly.  The two-second
# value is an intentional, bounded hand-off budget; it is never extended by
# the producer after ingress.
INT64_MAX = (1 << 63) - 1
TAKE_FREEZE_HANDOFF_BUDGET_NS = 2_000_000_000
WIRE_CLOCK_QPC_MAX_DELTA_NS = 5_000_000

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_EXE = (
    REPO_ROOT
    / "upstream"
    / "build_x64"
    / "rundir"
    / "RelWithDebInfo"
    / "bin"
    / "64bit"
    / "pulsar.exe"
)

READY_RE = re.compile(r"PULSAR_READY ws=(\S+) password=(\S+)")
SENSITIVE_LOG_VALUE_RE = re.compile(
    r"(?i)\b(password|token|secret|stream[-_ ]?key)\s*([=:])\s*[^\s,;]+"
)
# Keep accepting the bracketed diagnostics emitted by older builds while also
# accepting the logger's structured separator form.  The latter is the exact
# shape used by the current binary: ``pulsar-dual-lane | ready``.
DUAL_LANE_LOG_PREFIX = r"(?:\[pulsar-dual-lane\]|pulsar-dual-lane\s*\|)"
DUAL_READY_RE = re.compile(
    DUAL_LANE_LOG_PREFIX
    + r"\s*ready LaneA=(\S+) LaneB=(\S+) "
    r"lane_root_binding_valid=(\d) program_main_view_valid=(\d) "
    r"program_main_video_valid=(\d) preview_distinct_valid=(\d)"
)
ENCODER_RE = re.compile(r"video encoder allocated: family=(\S+) id=(\S+)")
DUAL_ACTIVATION_RE = re.compile(
    DUAL_LANE_LOG_PREFIX
    + r"\s*activation=(enabled|disabled) source=(\S+) "
    r"rollback_after_takes=(\d+) flag_resolved_at=setup"
)
BUILD_REVISION_RE = re.compile(r"[0-9a-f]{40}")
ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
ENCODER_BIND_RE = re.compile(
    DUAL_LANE_LOG_PREFIX + r"\s*encoder video_t bound once to ProgramView"
)
COMMIT_RE = re.compile(
    DUAL_LANE_LOG_PREFIX
    + r"\s*TakeCommitted count=(\d+) frame_id=(\d+) "
    r"pts_ns=(\d+) onair_lane=(-?\d+) preview_lane=(-?\d+) "
    r"lane_root_binding_valid=(\d) program_main_view_valid=(\d) "
    r"program_main_video_valid=(\d) preview_distinct_valid=(\d)"
)

CANVAS_W = 1920
CANVAS_H = 1080
SCENE_A = "probe-dual-lane-A"
SCENE_B = "probe-dual-lane-B"
INPUT_A = "probe-dual-lane-color-A"
INPUT_B = "probe-dual-lane-color-B"
INPUT_A_LIVE = "probe-dual-lane-live-A"
INPUT_B_LIVE = "probe-dual-lane-live-B"
INPUT_A_POST_TAKE = "probe-dual-lane-post-take-A"
INPUT_B_FROZEN = "probe-dual-lane-frozen-B"
COLOR_RED_ABGR = 0xFF0000FF
COLOR_GREEN_ABGR = 0xFF00FF00
COLOR_BLUE_ABGR = 0xFFFF0000
# These are deliberately different from the frontend's Default bootstrap
# inputs.  Each public lane receives its own producer instance; the probe never
# treats Default's bootstrap sources or workload flags as evidence.
LANE_SOURCE_NAMES = {
    "A": {"window_capture": "probe-dual-lane-wgc-A", "browser_source": "probe-dual-lane-cef-A"},
    "B": {"window_capture": "probe-dual-lane-wgc-B", "browser_source": "probe-dual-lane-cef-B"},
}
SOURCE_SCREENSHOT_DEADLINE_S = 20.0
SOURCE_SCREENSHOT_INTERVAL_S = 0.5
# The trace contract's warm-up count is an observed partition of the same
# process: the first 100 committed Takes are discarded from latency
# percentiles, and the following --takes commits are the measured sample.
# Non-traced smoke runs keep their historical --takes total.
TRACE_WARMUP_TAKES = 100
PRODUCER_BOUNDARIES = (
    "encoder_input_raw",
    "directshow_return",
    "encoded_first_packet",
)

# AC-12 is measured at the first video packet observed by a dedicated local
# RTMP receiver.  FFmpeg's ``-debug_ts`` reports the demuxer PTS in FLV
# millisecond ticks; the receiver-side contract deliberately records that
# rational timebase and never calls it wire-level ingress.
RTMP_PACKET_RE = re.compile(
    r"demuxer\s*->\s*ist_index:\S+\s+type:video\b.*?"
    r"pkt_pts:(-?\d+)\s+pkt_pts_time:([^\s]+)\s+"
    r"pkt_dts:(-?\d+)\s+pkt_dts_time:([^\s]+)"
)
RTMP_PACKET_TIMEBASE_NUM = 1
RTMP_PACKET_TIMEBASE_DEN = 1000


def _rtmp_packet_identity(stream_id: str, packet_index: int, pts: int, dts: int) -> str:
    """Return a deterministic packet identity within the trace ID bound."""

    digest = hashlib.sha256(f"{stream_id}|video|{packet_index}|{pts}|{dts}".encode("utf-8")).hexdigest()
    return "rtmp-" + digest


def _stream_id_for_runtime(runtime_id: str, encoder: str) -> str:
    """Build a URL-safe stream key without exceeding the evidence ID bound."""

    candidate = f"{runtime_id}-{encoder}"
    if ID_RE.fullmatch(candidate) is not None:
        return candidate
    digest = hashlib.sha256(runtime_id.encode("utf-8")).hexdigest()
    return f"stream-{encoder}-{digest}"


def _packet_int(value: object, name: str, *, non_negative: bool = True) -> int:
    """Validate packet metadata before using it in rational correlation."""

    if type(value) is not int or (non_negative and value < 0):
        qualifier = "non-negative " if non_negative else ""
        raise ProbeFailure(f"RTMP {name} must be a {qualifier}integer")
    return value


def _packet_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProbeFailure(f"RTMP {name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class RtmpPacketCandidate:
    """One exact sequence match and its admissible mux-offset interval."""

    packet: dict[str, int | str]
    offset_min: Fraction
    offset_max: Fraction


@dataclass
class RtmpPacketCorrelation:
    """Correlate encoder callbacks with the FLV sequence after mux rebasing.

    OBS rebases both FLV timestamps by ``start_dts_offset``.  That offset is
    selected from the first audio *or* video packet, so an absolute PTS match
    between the video encoder callback and the RTMP demuxer is not valid.  The
    video packet index is unchanged by the local TCP/RTMP path; use it as the
    exact identity and intersect the PTS/DTS quantization intervals to prove
    one constant mux offset for the complete stream.
    """

    used_packet_indices: set[int] = field(default_factory=set)
    offset_min: Fraction | None = None
    offset_max: Fraction | None = None

    def candidates(
        self,
        producer: Mapping[str, object],
        receiver_packets: list[dict[str, int | str]],
    ) -> list[RtmpPacketCandidate]:
        producer_index = _packet_int(producer.get("packet_index"), "producer packet_index")
        producer_pts, producer_dts = _producer_packet_times(producer)
        receiver_tick = Fraction(RTMP_PACKET_TIMEBASE_NUM, RTMP_PACKET_TIMEBASE_DEN)
        candidates: list[RtmpPacketCandidate] = []
        for packet in receiver_packets:
            index = _packet_int(packet.get("packet_index"), "receiver packet_index")
            if index != producer_index or index in self.used_packet_indices:
                continue
            receiver_pts = Fraction(
                _packet_int(packet.get("packet_pts"), "receiver packet_pts"),
                RTMP_PACKET_TIMEBASE_DEN,
            )
            receiver_dts = Fraction(
                _packet_int(packet.get("packet_dts"), "receiver packet_dts", non_negative=False),
                RTMP_PACKET_TIMEBASE_DEN,
            )
            half_tick = receiver_tick / 2
            lower = max(receiver_pts - producer_pts - half_tick, receiver_dts - producer_dts - half_tick)
            upper = min(receiver_pts - producer_pts + half_tick, receiver_dts - producer_dts + half_tick)
            if self.offset_min is not None:
                lower = max(lower, self.offset_min)
            if self.offset_max is not None:
                upper = min(upper, self.offset_max)
            if lower <= upper:
                candidates.append(RtmpPacketCandidate(packet, lower, upper))
        return candidates

    def commit(self, candidate: RtmpPacketCandidate) -> None:
        index = _packet_int(candidate.packet.get("packet_index"), "receiver packet_index")
        if index in self.used_packet_indices:
            raise ProbeFailure(f"RTMP receiver packet index {index} was correlated twice")
        self.used_packet_indices.add(index)
        self.offset_min = candidate.offset_min
        self.offset_max = candidate.offset_max

    def metadata(self) -> dict[str, int | str]:
        if self.offset_min is None or self.offset_max is None or not self.used_packet_indices:
            raise ProbeFailure("RTMP mux-offset correlation was not calibrated")
        return {
            "correlation_method": "packet_index_constant_mux_offset_v1",
            "mux_offset_min_num": self.offset_min.numerator,
            "mux_offset_min_den": self.offset_min.denominator,
            "mux_offset_max_num": self.offset_max.numerator,
            "mux_offset_max_den": self.offset_max.denominator,
            "correlated_packet_count": len(self.used_packet_indices),
        }


def _producer_packet_times(producer: Mapping[str, object]) -> tuple[Fraction, Fraction]:
    """Return producer PTS/DTS as exact seconds after strict metadata checks."""

    fields = ("packet_pts", "packet_dts", "packet_timebase_num", "packet_timebase_den")
    if any(field not in producer for field in fields):
        raise ProbeFailure(
            "encoded producer packet lacks complete PTS/timebase metadata for "
            f"{producer.get('take_command_id')}"
        )
    producer_pts_value = _packet_int(producer["packet_pts"], "producer packet_pts")
    producer_dts_value = _packet_int(
        producer["packet_dts"], "producer packet_dts", non_negative=False
    )
    producer_timebase_num = _packet_int(
        producer["packet_timebase_num"], "producer packet_timebase_num"
    )
    producer_timebase_den = _packet_int(
        producer["packet_timebase_den"], "producer packet_timebase_den"
    )
    if producer_timebase_num <= 0 or producer_timebase_den <= 0:
        raise ProbeFailure("RTMP producer packet timebase must be positive")
    producer_pts = Fraction(
        producer_pts_value * producer_timebase_num, producer_timebase_den
    )
    producer_dts = Fraction(
        producer_dts_value * producer_timebase_num, producer_timebase_den
    )
    return producer_pts, producer_dts

# The dual-lane campaign must exercise an actual browser_source, but its
# content must not depend on a public website or network availability.  This
# page is served by DeterministicCefServer below and has a deliberately
# non-black background plus high-contrast blocks so a source screenshot can
# prove that CEF painted pixels rather than merely accepting settings.
CEF_PAGE_HTML = b"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>Pulsar #246 CEF workload</title>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#132238;color:#f6fbff;font-family:Arial,sans-serif}
main{box-sizing:border-box;width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:28px}
h1{margin:0;font-size:72px;letter-spacing:5px;text-shadow:0 0 18px #38e8ff}
p{margin:0;font-size:27px;color:#9bd8e8;letter-spacing:2px}
.bar{width:62%;height:20px;border-radius:10px;background:linear-gradient(90deg,#ff3da6,#38e8ff)}
.tiles{display:flex;gap:18px}.tile{width:100px;height:54px;border-radius:8px}.a{background:#ff3da6}.b{background:#38e8ff}.c{background:#9dff6e}
</style></head><body><main><div class=\"bar\"></div><h1>PULSAR CEF #246</h1><p>deterministic local browser_source workload</p><div class=\"tiles\"><div class=\"tile a\"></div><div class=\"tile b\"></div><div class=\"tile c\"></div></div></main></body></html>"""


def cef_page_html(lane: str | None = None) -> bytes:
    """Return the deterministic page, visibly tagged for public lane A/B."""

    if lane not in ("A", "B"):
        return CEF_PAGE_HTML
    marker = f"PULSAR CEF #246 / LANE {lane}".encode("ascii")
    return CEF_PAGE_HTML.replace(b"PULSAR CEF #246</h1>", marker + b"</h1>")


class _DeterministicCefHandler(http.server.BaseHTTPRequestHandler):
    """Serve one immutable page and keep the probe's HTTP boundary quiet."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path.split("?", 1)[0] != "/pulsar-cef-246.html":
            self.send_error(404)
            return
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        lane = next((part.split("=", 1)[1] for part in query.split("&") if part.startswith("lane=")), None)
        body = cef_page_html(lane)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path.split("?", 1)[0] != "/pulsar-cef-246.html":
            self.send_error(404)
            return
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        lane = next((part.split("=", 1)[1] for part in query.split("&") if part.startswith("lane=")), None)
        body = cef_page_html(lane)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class DeterministicCefServer:
    """Ephemeral loopback HTTP source for the real CEF browser_source."""

    def __init__(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DeterministicCefHandler)
        self.server.daemon_threads = True
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/pulsar-cef-246.html"

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.server.serve_forever, name="pulsar-cef-probe-http", daemon=True)
        self.thread.start()

    def close(self) -> None:
        if self.thread is None:
            self.server.server_close()
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.thread = None


class ProbeFailure(RuntimeError):
    """A failed runtime assertion."""


class ProbeSkip(RuntimeError):
    """A reproducible environment limitation, not a product pass/fail."""


def redact_log_line(line: str) -> str:
    """Preserve diagnostics while never echoing boot credentials in failures."""
    return SENSITIVE_LOG_VALUE_RE.sub(r"\1\2[redacted]", line)


def failure_tail(lines: list[str], limit: int) -> str:
    return "\n".join(f"  | {redact_log_line(line)}" for line in lines[-limit:])


def assert_dual_lane_activation(
    process: "PulsarProcess", expected: bool, required_source: str | None = None
) -> tuple[str, str, int]:
    """Verify that the runtime consumed the boot-time topology switch.

    The probe sets ``PULSAR_DISABLE_DUAL_LANE`` for its single-canvas
    reference phase.  Resource-reference mode is the effective compatibility
    decision reported by the runtime.  A startup decision line is required
    before any source or Take evidence is accepted; an environment assignment
    alone is not evidence that the binary used it.
    """

    match = process.wait_for(DUAL_ACTIVATION_RE, timeout=60)
    enabled = match.group(1) == "enabled"
    if enabled != expected:
        raise ProbeFailure(
            "runtime dual-lane activation mismatch: "
            f"reported={match.group(1)!r} source={match.group(2)!r} expected={expected}"
        )
    if required_source is not None and match.group(2) != required_source:
        raise ProbeFailure(
            "runtime dual-lane activation source mismatch: "
            f"reported={match.group(2)!r} expected={required_source!r}"
        )
    return match.group(1), match.group(2), int(match.group(3))


def choose_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wire_monotonic_ns() -> int:
    """Return the monotonic timestamp used in envelopes crossing into libobs.

    ``perf_counter_ns`` is backed by QPC on Windows and is the source that
    shares an epoch with libobs ``os_gettime_ns``.  ``time.monotonic()`` and
    ``time.monotonic_ns()`` remain appropriate for local duration waits, but
    must not be serialized into the native deadline field.
    """

    return time.perf_counter_ns()


def _qpc_ns() -> int | None:
    """Read QueryPerformanceCounter directly when the probe runs on Windows."""

    if os.name != "nt":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    counter = ctypes.c_longlong()
    frequency = ctypes.c_longlong()
    query_counter = kernel32.QueryPerformanceCounter
    query_counter.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
    query_counter.restype = ctypes.c_int
    query_frequency = kernel32.QueryPerformanceFrequency
    query_frequency.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
    query_frequency.restype = ctypes.c_int
    if not query_frequency(ctypes.byref(frequency)) or frequency.value <= 0:
        raise ProbeFailure("QueryPerformanceFrequency is unavailable for the wire-clock preflight")
    if not query_counter(ctypes.byref(counter)):
        raise ProbeFailure("QueryPerformanceCounter is unavailable for the wire-clock preflight")
    return (counter.value * 1_000_000_000) // frequency.value


def calibrate_wire_clock(*, max_delta_ns: int = WIRE_CLOCK_QPC_MAX_DELTA_NS) -> dict[str, int | str | None]:
    """Verify the serialized clock tracks QPC before a traced campaign starts.

    The QPC samples bracket the Python call, so their midpoint bounds call
    overhead instead of treating scheduling delay as a clock offset.  A
    calibration failure is a typed probe failure: accepting a trace with
    mismatched epochs would make every freeze deadline evidence ambiguous.
    """

    if type(max_delta_ns) is not int or max_delta_ns < 0:
        raise ProbeFailure("wire-clock calibration bound must be a non-negative integer")
    before = _qpc_ns()
    wire_now = wire_monotonic_ns()
    after = _qpc_ns()
    if before is None or after is None:
        return {
            "source": "perf_counter_ns",
            "wire_now_ns": wire_now,
            "qpc_now_ns": None,
            "qpc_delta_ns": None,
        }
    qpc_midpoint = before + (after - before) // 2
    delta = wire_now - qpc_midpoint
    if abs(delta) > max_delta_ns:
        raise ProbeFailure(
            "wire clock is not aligned with QueryPerformanceCounter: "
            f"perf_counter_ns={wire_now} qpc_midpoint_ns={qpc_midpoint} delta_ns={delta} "
            f"bound_ns={max_delta_ns}"
        )
    return {
        "source": "perf_counter_ns/qpc",
        "wire_now_ns": wire_now,
        "qpc_now_ns": qpc_midpoint,
        "qpc_delta_ns": delta,
        "qpc_bound_ns": max(abs(wire_now - before), abs(after - wire_now)),
    }


def make_wire_deadline_ns(now_ns: int | None = None, *, margin_ns: int = TAKE_FREEZE_HANDOFF_BUDGET_NS) -> int:
    """Create a bounded QPC-domain deadline suitable for the native bridge."""

    now = wire_monotonic_ns() if now_ns is None else now_ns
    if type(now) is not int or now < 0 or now > INT64_MAX:
        raise ProbeFailure("wire clock value must be an integer in the signed 64-bit range")
    if type(margin_ns) is not int or margin_ns <= 0:
        raise ProbeFailure("freeze hand-off budget must be a positive integer")
    if now > INT64_MAX - margin_ns:
        raise ProbeFailure("freeze deadline would exceed the signed 64-bit native bridge range")
    return now + margin_ns


def wire_deadline_delta_ns(deadline_ns: int, now_ns: int) -> int:
    """Return signed remaining time for two values in the same wire domain."""

    if type(deadline_ns) is not int or type(now_ns) is not int:
        raise ProbeFailure("wire deadline arithmetic requires integer timestamps")
    return deadline_ns - now_ns


def wire_deadline_covers_handoff(deadline_ns: int, now_ns: int, *, handoff_ns: int = 0) -> bool:
    """Check a deadline without changing it or silently adding producer slack."""

    if type(handoff_ns) is not int or handoff_ns < 0:
        raise ProbeFailure("handoff budget must be a non-negative integer")
    return wire_deadline_delta_ns(deadline_ns, now_ns) > handoff_ns


def _valid_hardware_label(value: str | None, kind: str) -> str:
    if not value or not value.strip() or len(value) > 128 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ProbeFailure(f"trace {kind} identity must be a non-empty printable label of at most 128 characters")
    if value in ("unknown-host", "unknown-gpu"):
        raise ProbeFailure(f"trace {kind} identity must identify the actual host/adapter")
    return value


def resolve_trace_hardware(host: str | None = None, gpu: str | None = None) -> tuple[str, str]:
    """Resolve the exact host/GPU identity stamped into every resource sample."""

    resolved_host = _valid_hardware_label(host or os.environ.get("PULSAR_TRACE_HOST") or socket.gethostname(), "host")
    resolved_gpu = gpu or os.environ.get("PULSAR_TRACE_GPU")
    if not resolved_gpu:
        try:
            raw = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                stderr=subprocess.STDOUT,
                timeout=10,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ProbeSkip(f"cannot resolve a real NVIDIA GPU identity with nvidia-smi: {exc}") from exc
        resolved_gpu = next((line.strip() for line in raw.splitlines() if line.strip()), None)
    return resolved_host, _valid_hardware_label(resolved_gpu, "GPU")


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


class RtmpReceiver:
    """Own a real FFmpeg RTMP loopback receiver for AC-12 evidence.

    FFmpeg is used as the protocol/demux consumer, not as a decoder timing
    oracle.  ``-debug_ts`` packet records are timestamped when the receiver
    process emits its demux line; that is explicitly a receiver/demux
    observation and is never promoted to wire-level or decoded latency.
    """

    def __init__(self, ffmpeg: str, *, runtime_id: str, stream_id: str) -> None:
        self.ffmpeg = ffmpeg
        self.runtime_id = runtime_id
        if ID_RE.fullmatch(stream_id) is None:
            raise ProbeFailure("RTMP stream_id must be a non-empty identifier of at most 128 characters")
        self.stream_id = stream_id
        self.port = choose_port()
        self.server_url = f"rtmp://127.0.0.1:{self.port}/pulsar"
        self.stream_key = stream_id
        # The service API sends ``server`` and ``key`` separately.  FFmpeg's
        # listen endpoint must include the key path that the native
        # streamOutput actually publishes to.
        self.endpoint = f"{self.server_url}/{self.stream_key}"
        self.proc: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None
        self.lines: list[str] = []
        self.packets: list[dict[str, int | str]] = []
        self.failure: str | None = None
        self.calibration: dict[str, int | str | None] | None = None
        self.live_correlation = RtmpPacketCorrelation()
        self._lock = threading.Lock()

    def metadata(self) -> dict[str, int | str]:
        calibration = self.calibration
        if calibration is None:
            raise ProbeFailure("RTMP receiver clock was not calibrated")
        source = calibration.get("source")
        offset = calibration.get("qpc_delta_ns")
        measured_bound = calibration.get("qpc_bound_ns")
        if source not in ("perf_counter_ns/qpc", "qpc") or not isinstance(offset, int):
            raise ProbeFailure("RTMP receiver clock calibration is incomplete")
        if not isinstance(measured_bound, int) or measured_bound <= 0 or measured_bound > WIRE_CLOCK_QPC_MAX_DELTA_NS:
            raise ProbeFailure("RTMP receiver clock calibration bound is invalid")
        return {
            "server_url": self.server_url,
            "stream_key": self.stream_key,
            "endpoint": self.endpoint,
            "receiver_id": "ffmpeg-rtmp-receiver",
            "stream_id": self.stream_id,
            "clock_source": str(source),
            "clock_offset_ns": offset,
            "clock_bound_ns": measured_bound,
            "packet_timebase_num": RTMP_PACKET_TIMEBASE_NUM,
            "packet_timebase_den": RTMP_PACKET_TIMEBASE_DEN,
        }

    def start(self) -> None:
        if self.proc is not None:
            raise ProbeFailure("RTMP receiver was started twice")
        self.calibration = calibrate_wire_clock()
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "verbose",
            "-debug_ts",
            "-listen",
            "1",
            "-i",
            self.endpoint,
            "-map",
            "0:v:0",
            "-an",
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ]
        self.proc = subprocess.Popen(
            command,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.thread = threading.Thread(target=self._pump, name="pulsar-rtmp-receiver", daemon=True)
        self.thread.start()
        time.sleep(0.25)
        if self.proc.poll() is not None:
            self.stop()
            raise ProbeFailure("RTMP receiver exited before Pulsar could connect: " + " | ".join(self.lines[-20:]))

    def _pump(self) -> None:
        process = self.proc
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            received_at = wire_monotonic_ns()
            calibration = self.calibration
            offset = calibration.get("qpc_delta_ns", 0) if calibration is not None else 0
            if not isinstance(offset, int):
                with self._lock:
                    self.failure = "RTMP receiver clock offset was not an integer"
                continue
            # Runtime telemetry uses os_gettime_ns/QPC.  Normalize the
            # receiver's perf_counter timestamp into that same domain before
            # it enters the fused evidence; the raw receiver timestamp is not
            # retained as acceptance evidence.
            received_at -= offset
            clean = line.rstrip("\r\n")
            with self._lock:
                self.lines.append(clean)
            match = RTMP_PACKET_RE.search(clean)
            if match is None:
                continue
            try:
                pts_raw = int(match.group(1))
                dts_raw = int(match.group(3))
                pts_time = Decimal(match.group(2))
                dts_time = Decimal(match.group(4))
                if not pts_time.is_finite() or not dts_time.is_finite():
                    raise ValueError("non-finite packet time")
                # The raw FFmpeg packet timestamp must agree with the FLV
                # millisecond rational within half a receiver tick.  Store the
                # integer FLV tick, not a floating-point approximation.
                pts_tick = int((pts_time * RTMP_PACKET_TIMEBASE_DEN).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
                dts_tick = int((dts_time * RTMP_PACKET_TIMEBASE_DEN).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
                if abs(pts_tick - pts_raw) > 1 or abs(dts_tick - dts_raw) > 1:
                    raise ValueError("FFmpeg packet integer/time value disagrees beyond one tick")
            except (InvalidOperation, ValueError) as exc:
                with self._lock:
                    self.failure = f"RTMP packet PTS parse/calibration failure: {exc}"
                continue
            with self._lock:
                packet_index = len(self.packets)
                # Keep the identity within the trace identifier bound even
                # when an operator supplies a maximum-length runtime/stream
                # ID.  Runtime and stream IDs remain first-class correlation
                # fields; this digest is the bounded packet identity key.
                packet_identity = _rtmp_packet_identity(self.stream_id, packet_index, pts_raw, dts_raw)
                self.packets.append(
                    {
                        "packet_index": packet_index,
                        "packet_pts": pts_raw,
                        "packet_dts": dts_raw,
                        "packet_pts_time_ms": pts_tick,
                        "packet_dts_time_ms": dts_tick,
                        "observed_at_monotonic_ns": received_at,
                        "packet_identity": packet_identity,
                    }
                )

    def snapshot_packets(self) -> list[dict[str, int | str]]:
        """Return an atomic copy of receiver packet observations."""

        with self._lock:
            return list(self.packets)

    def snapshot(self) -> tuple[list[dict[str, int | str]], str | None]:
        """Return packets and receiver failure state from one locked view."""

        with self._lock:
            return list(self.packets), self.failure

    def persist_diagnostics(self, output_path: pathlib.Path) -> pathlib.Path:
        """Persist receiver state after a failed run without claiming evidence."""

        packets, failure = self.snapshot()
        with self._lock:
            line_tail = list(self.lines[-200:])
        correlation: dict[str, int | str] | None = None
        try:
            correlation = self.live_correlation.metadata()
        except ProbeFailure:
            pass
        diagnostic_path = output_path.with_name(output_path.name + ".receiver-diagnostic.json")
        temp_path = diagnostic_path.with_name(f".{diagnostic_path.name}.{os.getpid()}.tmp")
        payload = {
            "evidence_kind": "failed_run_diagnostic_only",
            "runtime_instance_id": self.runtime_id,
            "receiver": self.metadata(),
            "correlation": correlation,
            "failure": failure,
            "packet_count": len(packets),
            "packets": packets,
            "line_tail": line_tail,
        }
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, diagnostic_path)
        except Exception:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise
        return diagnostic_path

    def stop(self) -> None:
        process = self.proc
        if process is None:
            if self.thread is not None:
                self.thread.join(timeout=2)
            return
        failure: str | None = None
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception as exc:  # pragma: no cover - OS-specific cleanup
                    failure = f"RTMP receiver could not be killed: {exc}"
            except Exception as exc:  # pragma: no cover - OS-specific cleanup
                failure = f"RTMP receiver termination failed: {exc}"
        if process.poll() is None:
            failure = failure or "RTMP receiver remained alive after cleanup"
        if self.thread is not None:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                failure = failure or "RTMP receiver reader thread did not exit"
        if failure is not None:
            self.failure = failure
            raise ProbeFailure(failure)

    def _install_fused_records(self, records: list[dict[str, object]], output_path: pathlib.Path) -> None:
        """Validate a complete trace in a sibling temp and atomically install it."""

        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.fused.tmp")
        if temp_path.exists():
            raise ProbeFailure(f"RTMP fusion temporary path already exists: {temp_path}")
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

            parser_path = pathlib.Path(__file__).with_name("probe-take-latency.py")
            parser_spec = importlib.util.spec_from_file_location("pulsar_take_latency_fusion", parser_path)
            if parser_spec is None or parser_spec.loader is None:
                raise ProbeFailure("cannot load the latency parser for RTMP fusion validation")
            parser_module = importlib.util.module_from_spec(parser_spec)
            previous_module = sys.modules.get(parser_spec.name)
            sys.modules[parser_spec.name] = parser_module
            try:
                parser_spec.loader.exec_module(parser_module)
                fused_trace = parser_module.parse_trace(temp_path)
                parser_module.analyze_trace(
                    fused_trace,
                    minimum_takes=1,
                    minimum_warmup=0,
                    minimum_resource_samples=1,
                )
            except Exception as exc:
                raise ProbeFailure(f"RTMP fused trace parser validation failed: {exc}") from exc
            finally:
                if previous_module is None:
                    sys.modules.pop(parser_spec.name, None)
                else:
                    sys.modules[parser_spec.name] = previous_module
            os.replace(temp_path, output_path)
        except Exception:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise

    def _read_producer_records(self, producer_path: pathlib.Path) -> tuple[list[dict[str, object]], dict[str, object]]:
        if not producer_path.is_file():
            raise ProbeFailure(f"producer trace is missing: {producer_path}")
        try:
            records = [
                json.loads(line)
                for line in producer_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            raise ProbeFailure(f"cannot read producer trace for RTMP fusion: {exc}") from exc
        if not records or records[0].get("record_type") != "session":
            raise ProbeFailure("producer trace must begin with one session record")
        return records, dict(records[0])

    def fuse_resource_trace(
        self,
        producer_path: pathlib.Path,
        output_path: pathlib.Path,
        *,
        minimum_samples: int = 1,
    ) -> None:
        """Install reference resources collected under the active RTMP load."""

        records, session = self._read_producer_records(producer_path)
        if any(record.get("record_type") in {"event", "observation"} for record in records[1:]):
            raise ProbeFailure("resource-only RTMP fusion cannot contain Take events or observations")
        if not any(record.get("record_type") == "resource_sample" for record in records):
            raise ProbeFailure("resource-only RTMP fusion requires native resource samples")
        if minimum_samples < 1:
            raise ProbeFailure("resource-only RTMP fusion minimum must be positive")
        if not self.packets:
            raise ProbeFailure("resource-only RTMP load emitted no receiver video packets")
        session["rtmp_receiver"] = self.metadata()
        session["rtmp_load_requested"] = True
        raw_paths = session.get("capture_paths")
        if not isinstance(raw_paths, list) or any(not isinstance(path, str) for path in raw_paths):
            raise ProbeFailure("producer trace capture_paths must be a list of strings")
        # Resource-only evidence proves that an active RTMP load existed; it
        # contains no Take and therefore no rtmp_first_packet correlation.
        session["capture_paths"] = [path for path in raw_paths if path != "rtmp_first_packet"]
        resource_records = [record for record in records if record.get("record_type") == "resource_sample"]
        eligible = sum(
            record.get("encoder_active") is True
            and record.get("encoder_family") == "nvenc"
            and record.get("rtmp_load_active") is True
            for record in resource_records
        )
        if eligible < minimum_samples:
            raise ProbeFailure(
                "resource-only RTMP load did not produce enough observed-active samples: "
                f"{eligible} < {minimum_samples}"
            )
        self._install_fused_records([session, *records[1:]], output_path)

    def fuse_trace(self, producer_path: pathlib.Path, output_path: pathlib.Path) -> None:
        """Create a deterministic post-stop trace with receiver observations."""

        if self.failure:
            raise ProbeFailure(self.failure)
        records, session = self._read_producer_records(producer_path)
        session["rtmp_receiver"] = self.metadata()
        session["rtmp_load_requested"] = True
        raw_paths = session.get("capture_paths")
        if not isinstance(raw_paths, list) or any(not isinstance(path, str) for path in raw_paths):
            raise ProbeFailure("producer trace capture_paths must be a list of strings")
        paths = list(cast(list[str], raw_paths))
        if "rtmp_first_packet" not in paths:
            paths.append("rtmp_first_packet")
        session["capture_paths"] = paths

        encoded = [
            record
            for record in records
            if record.get("record_type") == "observation"
            and record.get("boundary") == "encoded_first_packet"
            and record.get("valid") is True
        ]
        if any(
            record.get("record_type") == "observation" and record.get("boundary") == "rtmp_first_packet"
            for record in records
        ):
            raise ProbeFailure("producer trace already contains RTMP observations; fusion would double-count receiver data")
        if not encoded:
            raise ProbeFailure("producer trace contains no encoded packet observations for RTMP correlation")
        receiver_packets, receiver_failure = self.snapshot()
        if not receiver_packets:
            raise ProbeFailure("RTMP receiver emitted no demuxed video packet observations")
        if receiver_failure:
            raise ProbeFailure(receiver_failure)

        correlation = RtmpPacketCorrelation()
        rtmp_observations: list[dict[str, object]] = []
        for producer in encoded:
            candidates = correlation.candidates(producer, receiver_packets)
            if len(candidates) != 1:
                raise ProbeFailure(
                    f"RTMP receiver packet correlation for {producer.get('take_command_id')} is ambiguous: "
                    f"{len(candidates)} packet-index/mux-offset candidates"
                )
            candidate = candidates[0]
            packet = candidate.packet
            correlation.commit(candidate)
            rtmp_observations.append(
                {
                    "record_type": "observation",
                    "boundary": "rtmp_first_packet",
                    "clock_domain": "monotonic_ns",
                    "runtime_instance_id": producer["runtime_instance_id"],
                    "command_id": producer["command_id"],
                    "intent_id": producer["intent_id"],
                    "take_command_id": producer["take_command_id"],
                    "revisions": producer["revisions"],
                    "frame_id": producer["frame_id"],
                    "pts_ns": producer["pts_ns"],
                    "observed_at_monotonic_ns": _packet_int(
                        packet.get("observed_at_monotonic_ns"), "receiver observed_at_monotonic_ns"
                    ),
                    "valid": True,
                    "surface": "RTMP",
                    "consumer": "receiver",
                    "packet_index": packet["packet_index"],
                    "packet_pts": packet["packet_pts"],
                    "packet_dts": packet["packet_dts"],
                    "packet_timebase_num": RTMP_PACKET_TIMEBASE_NUM,
                    "packet_timebase_den": RTMP_PACKET_TIMEBASE_DEN,
                    "packet_identity": _packet_str(packet.get("packet_identity"), "receiver packet_identity"),
                    "clock_source": self.metadata()["clock_source"],
                    "clock_offset_ns": self.metadata()["clock_offset_ns"],
                    "clock_bound_ns": self.metadata()["clock_bound_ns"],
                    "notes": "first video packet observed at FFmpeg RTMP demux; not wire-level or decoded timing",
                }
            )
        session["rtmp_receiver"] = {**self.metadata(), **correlation.metadata()}
        merged = [
            session,
            *records[1:],
            *sorted(rtmp_observations, key=lambda item: int(cast(int, item["packet_index"]))),
        ]
        self._install_fused_records(merged, output_path)


def _windows_create_inherited_shutdown_event() -> int:
    """Create an unnamed inheritable manual-reset event for one child."""

    if os.name != "nt":
        raise ProbeFailure("inherited Windows shutdown event requested on a non-Windows host")

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", ctypes.c_uint32),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", ctypes.c_int),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_event = kernel32.CreateEventW
    create_event.argtypes = [
        ctypes.POINTER(SecurityAttributes),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_wchar_p,
    ]
    create_event.restype = ctypes.c_void_p
    attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, 1)
    handle = create_event(ctypes.byref(attributes), 1, 0, None)
    if not handle:
        error = ctypes.get_last_error()
        raise ProbeFailure(f"could not create inherited shutdown event (Win32 error {error})")
    return int(handle)


def _windows_close_handle(handle: int) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    if not close_handle(ctypes.c_void_p(handle)):
        error = ctypes.get_last_error()
        raise ProbeFailure(f"could not close inherited shutdown event (Win32 error {error})")


def _windows_clear_handle_inherit(handle: int) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_handle_information = kernel32.SetHandleInformation
    set_handle_information.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    set_handle_information.restype = ctypes.c_int
    if not set_handle_information(ctypes.c_void_p(handle), WINDOWS_HANDLE_FLAG_INHERIT, 0):
        error = ctypes.get_last_error()
        raise ProbeFailure(f"could not clear inherited shutdown handle (Win32 error {error})")


def _windows_signal_shutdown_event(handle: int) -> None:
    if os.name != "nt":
        raise ProbeFailure("Windows shutdown event requested on a non-Windows host")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_event = kernel32.SetEvent
    set_event.argtypes = [ctypes.c_void_p]
    set_event.restype = ctypes.c_int
    if not set_event(ctypes.c_void_p(handle)):
        error = ctypes.get_last_error()
        raise ProbeFailure(f"could not signal inherited shutdown event (Win32 error {error})")


class PulsarProcess:
    """Spawn Pulsar and retain structured stdout for identity assertions."""

    def __init__(
        self,
        exe: pathlib.Path,
        encoder: str,
        record_dir: pathlib.Path,
        trace_path: pathlib.Path | None = None,
        runtime_id: str | None = None,
        resource_mode: str | None = None,
        trace_append: bool = False,
        resource_interval_ms: int = 500,
        capture_window: str | None = None,
        cef_workload: bool = False,
        build_revision: str | None = None,
        cef_url: str | None = None,
        trace_host: str | None = None,
        trace_gpu: str | None = None,
    ) -> None:
        self.exe = exe
        self.encoder = encoder
        self.record_dir = record_dir
        self.trace_path = trace_path
        self.runtime_id = runtime_id or f"runtime-{secrets.token_hex(8)}"
        self.resource_mode = resource_mode
        self.trace_append = trace_append
        self.resource_interval_ms = resource_interval_ms
        self.capture_window = capture_window
        self.cef_workload = cef_workload
        self.build_revision = build_revision or os.environ.get("PULSAR_BUILD_REVISION")
        self.cef_url = cef_url or os.environ.get("PULSAR_CEF_URL")
        self.trace_host = trace_host
        self.trace_gpu = trace_gpu
        self.producer_topology = (
            "single_lane_reference" if resource_mode == "reference" else "dual_lane_ab"
        )
        self.producer_count = 1 if resource_mode == "reference" else 2
        self.port = choose_port()
        self.password = secrets.token_urlsafe(24)
        self.proc: subprocess.Popen[str] | None = None
        self.directshow_proc: subprocess.Popen[str] | None = None
        self.directshow_command: list[str] | None = None
        self.directshow_startup_output = ""
        self.directshow_lines: list[str] = []
        self.directshow_thread: threading.Thread | None = None
        self.directshow_cleanup_failure: str | None = None
        self.rtmp_receiver: RtmpReceiver | None = None
        self.rtmp_stream_started = False
        self.rtmp_cleanup_failure: str | None = None
        self.rtmp_producer_trace_path: pathlib.Path | None = None
        self.rtmp_final_trace_path: pathlib.Path | None = None
        self.lines: list[str] = []
        self.condition = threading.Condition()
        self.thread: threading.Thread | None = None
        self.shutdown_event_handle: int | None = None
        self.shutdown_control_expected = False
        self.graceful_shutdown_requested = False
        self.graceful_shutdown_error: str | None = None
        self.forced_kill_used = False

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        env["PULSAR_RECORD_DIR"] = str(self.record_dir)
        env["PULSAR_VIDEO_ENCODER"] = self.encoder
        # Make the normal canary opt in explicitly.  This keeps a caller's
        # ambient disable flag from silently changing the intended topology,
        # while the reference phase below deliberately overrides it with the
        # backwards-compatible single-canvas switch.
        env["PULSAR_DUAL_LANE_ENABLED"] = "1"
        env.pop("PULSAR_DISABLE_DUAL_LANE", None)
        if self.trace_path is not None:
            if self.build_revision is None or BUILD_REVISION_RE.fullmatch(self.build_revision) is None:
                raise ProbeFailure(
                    "--trace requires --build-revision (or PULSAR_BUILD_REVISION) to be the exact "
                    "40-character lowercase candidate SHA"
                )
            env["PULSAR_TRACE_PATH"] = str(self.trace_path)
            env["PULSAR_RUNTIME_INSTANCE_ID"] = self.runtime_id
            env["PULSAR_TRACE_SESSION_ID"] = f"{self.runtime_id}-{self.encoder}"
            env["PULSAR_BUILD_REVISION"] = self.build_revision
            env["PULSAR_TRACE_HOST"] = _valid_hardware_label(self.trace_host, "host")
            env["PULSAR_TRACE_GPU"] = _valid_hardware_label(self.trace_gpu, "GPU")
            env["PULSAR_TRACE_PRODUCER_TOPOLOGY"] = self.producer_topology
            env["PULSAR_TRACE_PRODUCER_COUNT"] = str(self.producer_count)
            # The trace probe owns the public WGC/CEF producer instances it
            # creates after PULSAR_READY.  Tell the frontend not to allocate a
            # duplicate PulsarCapture/PulsarCefWorkload pair in Default; the
            # probe's registration/settings/pixel checks are the readiness
            # evidence for the declared topology.
            env["PULSAR_TRACE_EXTERNAL_LANE_WORKLOAD"] = "1"
            env["PULSAR_TRACE_WARMUP_TAKES"] = str(TRACE_WARMUP_TAKES)
            env["PULSAR_TRACE_COMMAND"] = "scripts/probe-dual-lane.py --trace"
            if self.rtmp_receiver is not None:
                # These values are copied into the final fused session after
                # both processes stop.  Passing them to the runtime keeps the
                # producer and receiver artifacts tied to one stream identity
                # without asking the runtime to write external observations.
                receiver_metadata = self.rtmp_receiver.metadata()
                env["PULSAR_TRACE_RTMP_ENABLED"] = "1"
                env["PULSAR_TRACE_RTMP_ENDPOINT"] = str(receiver_metadata["endpoint"])
                env["PULSAR_TRACE_RTMP_RECEIVER_ID"] = str(receiver_metadata["receiver_id"])
                env["PULSAR_TRACE_RTMP_STREAM_ID"] = str(receiver_metadata["stream_id"])
            # Use the runtime-specific ProgramReturn queue for the external
            # DirectShow reader.  This keeps the consumer correlated to the
            # same runtime identity and avoids a process-wide legacy alias
            # collision during a traced canary.
            env["PULSAR_DIRECTSHOW_LEGACY_ALIAS"] = "0"
            # The headless lease policy is controlled by this variable (the
            # DirectShow producer policy above is a separate derived value).
            # Pin it explicitly so an operator's ambient `required` setting
            # cannot turn a dedicated traced run into an alias holder.
            env["PULSAR_LEGACY_ALIAS"] = "disabled"
            if self.resource_mode is not None:
                env["PULSAR_TRACE_RESOURCE_MODE"] = self.resource_mode
            else:
                env.pop("PULSAR_TRACE_RESOURCE_MODE", None)
            env["PULSAR_TRACE_RESOURCE_INTERVAL_MS"] = str(self.resource_interval_ms)
            if self.trace_append:
                env["PULSAR_TRACE_APPEND"] = "1"
            else:
                env.pop("PULSAR_TRACE_APPEND", None)
            if self.resource_mode != "reference":
                env["PULSAR_PROGRAM_RETURN_AUTOSTART"] = "1"
            else:
                env.pop("PULSAR_PROGRAM_RETURN_AUTOSTART", None)
            if self.resource_mode == "reference":
                env.pop("PULSAR_DUAL_LANE_ENABLED", None)
                env["PULSAR_DISABLE_DUAL_LANE"] = "1"
            else:
                env["PULSAR_DUAL_LANE_ENABLED"] = "1"
                env.pop("PULSAR_DISABLE_DUAL_LANE", None)
        else:
            # Do not let a caller's trace-only owner flag leak into an
            # ordinary non-traced run.
            env.pop("PULSAR_TRACE_EXTERNAL_LANE_WORKLOAD", None)
        if self.encoder == "nvenc":
            # p1 is accepted by the current NVENC family and makes an
            # accidental x264 fallback visible in the boot log check below.
            env["PULSAR_VIDEO_PRESET"] = "p1"
        if self.capture_window:
            env["PULSAR_CAPTURE_WINDOW"] = self.capture_window
        else:
            env.pop("PULSAR_CAPTURE_WINDOW", None)
        if self.cef_workload:
            env["PULSAR_WORKLOAD_CEF"] = "1"
            if self.cef_url:
                env["PULSAR_CEF_URL"] = self.cef_url
            else:
                env.pop("PULSAR_CEF_URL", None)
        else:
            env.pop("PULSAR_WORKLOAD_CEF", None)
            env.pop("PULSAR_CEF_URL", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)

        # A redirected stdout pipe gives a /SUBSYSTEM:WINDOWS child no shared
        # console, so CTRL_BREAK is not a reliable control plane. On Windows
        # create one anonymous manual-reset event and pass it solely through
        # STARTUPINFOEX's explicit handle list. The parent clears the handle's
        # inherit bit immediately after CreateProcess; the numeric value is
        # never logged or written to evidence.
        startupinfo = None
        creationflags = 0x08000000 if os.name == "nt" else 0
        if os.name == "nt":
            self.shutdown_control_expected = True
            handle = _windows_create_inherited_shutdown_event()
            self.shutdown_event_handle = handle
            env["PULSAR_SHUTDOWN_EVENT_HANDLE"] = str(handle)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.lpAttributeList = {"handle_list": [handle]}
            creationflags |= WINDOWS_CREATE_NEW_PROCESS_GROUP
        try:
            self.proc = subprocess.Popen(
                [str(self.exe)],
                cwd=str(self.exe.parent),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                startupinfo=startupinfo,
                close_fds=startupinfo is not None,
            )
        except BaseException:
            if self.shutdown_event_handle is not None:
                _windows_close_handle(self.shutdown_event_handle)
                self.shutdown_event_handle = None
            raise
        if self.shutdown_event_handle is not None:
            try:
                _windows_clear_handle_inherit(self.shutdown_event_handle)
            except ProbeFailure as exc:
                self.graceful_shutdown_error = str(exc)
                raise
        self.thread = threading.Thread(target=self._pump, name="pulsar-probe-log", daemon=True)
        self.thread.start()

    def cef_url_for_lane(self, lane: str) -> str:
        if lane not in ("A", "B") or not self.cef_url:
            raise ProbeFailure(f"cannot build a CEF URL for lane {lane!r}")
        # The ephemeral loopback server uses the query marker to render a
        # visible A/B label.  An operator-supplied URL is kept byte-for-byte
        # intact: source names/items still prove duplication without silently
        # changing an external application's URL semantics.
        if "127.0.0.1:" in self.cef_url or "localhost:" in self.cef_url:
            separator = "&" if "?" in self.cef_url else "?"
            return f"{self.cef_url}{separator}lane={lane}"
        return self.cef_url

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            with self.condition:
                self.lines.append(line.rstrip("\r\n"))
                self.condition.notify_all()

    def snapshot(self) -> list[str]:
        with self.condition:
            return list(self.lines)

    def wait_for(self, pattern: re.Pattern[str], timeout: float) -> re.Match[str]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for line in self.lines:
                    match = pattern.search(line)
                    if match:
                        return match
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = self.proc.poll() if self.proc is not None else None
                    tail = failure_tail(self.lines, 40)
                    raise ProbeFailure(
                        f"timeout waiting for {pattern.pattern!r}; exit={status}\n{tail}"
                    )
                self.condition.wait(timeout=min(0.25, remaining))

    def wait_for_shutdown_control_ready(self, timeout: float) -> None:
        """Require the child ACK before accepting PULSAR_READY or campaigning."""

        if os.name != "nt":
            return
        if not self.shutdown_control_expected:
            raise ProbeFailure("Windows shutdown control was not provisioned for this child")
        ready_pattern = re.compile(
            rf"PULSAR_SHUTDOWN_CONTROL event=ready id={re.escape(self.runtime_id)} "
            r"mechanism=inherited_event$"
        )
        self.wait_for(ready_pattern, timeout=timeout)

    def assert_shutdown_control_ready(self) -> None:
        """Assert that the child-side inherited control ACK was captured."""

        if os.name != "nt":
            return
        if not self.shutdown_control_expected:
            raise ProbeFailure("Windows shutdown control was not provisioned for this child")
        ready_pattern = re.compile(
            rf"PULSAR_SHUTDOWN_CONTROL event=ready id={re.escape(self.runtime_id)} "
            r"mechanism=inherited_event$"
        )
        if not any(ready_pattern.search(line) for line in self.snapshot()):
            raise ProbeFailure("child shutdown control readiness ACK was not observed")

    def wait_for_commit(self, count: int, timeout: float) -> re.Match[str]:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for line in self.lines:
                    match = COMMIT_RE.search(line)
                    if match and int(match.group(1)) == count:
                        return match
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    tail = failure_tail(self.lines, 60)
                    raise ProbeFailure(f"TakeCommitted count={count} not observed\n{tail}")
                self.condition.wait(timeout=min(0.25, remaining))

    def start_directshow_consumer(self) -> None:
        """Open the actual ProgramReturn DirectShow filter for a trace run."""

        if self.trace_path is None or self.resource_mode == "reference":
            return
        if self.directshow_proc is not None:
            return
        ffmpeg = find_ffmpeg(self.exe.parent)
        if ffmpeg is None:
            raise ProbeSkip("ffmpeg is required to open the ProgramReturn DirectShow consumer")
        env = dict(os.environ)
        env["PULSAR_RUNTIME_INSTANCE_ID"] = self.runtime_id
        env["PULSAR_TRACE_PATH"] = str(self.trace_path)
        env["PULSAR_DIRECTSHOW_LEGACY_ALIAS"] = "0"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "dshow",
            "-video_size",
            f"{CANVAS_W}x{CANVAS_H}",
            "-framerate",
            "60",
            "-i",
            "video=Pulsar Program Return",
            "-an",
            "-f",
            "null",
            "-",
        ]
        self.directshow_command = command
        self.directshow_proc = subprocess.Popen(
            command,
            cwd=str(self.exe.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.directshow_thread = threading.Thread(
            target=self._pump_directshow,
            args=(self.directshow_proc,),
            name="pulsar-directshow-probe",
            daemon=True,
        )
        self.directshow_thread.start()
        # Device enumeration failures are immediate and typed as an
        # environment skip.  Once the filter has stayed alive past this
        # bounded startup window, an exit during Takes is a product/probe
        # failure and is reported by assert_directshow_consumer_alive().
        time.sleep(0.75)
        if self.directshow_proc.poll() is not None:
            reader_failure = self._join_directshow_reader()
            if reader_failure is not None:
                self.directshow_cleanup_failure = reader_failure
                raise ProbeFailure(reader_failure)
            output = "\n".join(self.directshow_lines)
            self.directshow_startup_output = output[-4000:]
            self.directshow_proc = None
            if any(
                marker in output.lower()
                for marker in (
                    "could not enumerate video devices",
                    "video device not found",
                    "could not find video device",
                    "no such device",
                    "i/o error",
                )
            ):
                raise ProbeSkip(
                    "ProgramReturn DirectShow filter is unavailable on this host: "
                    + self.directshow_startup_output.strip()
                )
            raise ProbeFailure(
                "ProgramReturn DirectShow consumer exited during startup: "
                + self.directshow_startup_output.strip()
            )

    def start_rtmp_consumer(self) -> None:
        """Start the dedicated loopback RTMP receiver before StartStream."""

        if self.rtmp_receiver is None:
            return
        self.rtmp_receiver.start()

    async def start_rtmp_stream(self, inbox: Inbox, ws: Any) -> None:
        """Configure and start Pulsar's native streamOutput to the receiver."""

        receiver = self.rtmp_receiver
        if receiver is None:
            return
        response = await request(
            inbox,
            ws,
            "SetStreamServiceSettings",
            "rtmp-receiver-service",
            {
                "streamServiceType": "rtmp_custom",
                "streamServiceSettings": {
                    "server": receiver.server_url,
                    # The key is a non-secret local loopback routing identifier.
                    # It is persisted in receiver metadata for correlation;
                    # it is never a real distribution/streaming credential.
                    "key": receiver.stream_key,
                },
            },
        )
        assert_success(response, "SetStreamServiceSettings(rtmp loopback)")
        service_response = await request(inbox, ws, "GetStreamServiceSettings", "get-rtmp-receiver-service")
        assert_success(service_response, "GetStreamServiceSettings(rtmp loopback)")
        service_data = service_response.get("responseData") or {}
        if service_data.get("streamServiceType") != "rtmp_custom":
            raise ProbeFailure("native streamOutput service type was not rtmp_custom")
        settings = service_data.get("streamServiceSettings") or {}
        if settings.get("server") != receiver.server_url:
            raise ProbeFailure("native streamOutput service endpoint did not round-trip exactly")
        if settings.get("key") != receiver.stream_key:
            raise ProbeFailure("native streamOutput service stream key did not round-trip exactly")
        response = await request(inbox, ws, "StartStream", "start-rtmp-receiver")
        assert_success(response, "StartStream(rtmp loopback)")
        await wait_event(
            inbox,
            ws,
            "StreamStateChanged",
            lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED",
            timeout=20,
        )
        self.rtmp_stream_started = True

    async def stop_rtmp_stream(self, inbox: Inbox, ws: Any) -> None:
        if self.rtmp_receiver is None:
            return
        if self.rtmp_stream_started:
            response = await request(inbox, ws, "StopStream", "stop-rtmp-receiver")
            assert_success(response, "StopStream(rtmp loopback)")
            await wait_event(
                inbox,
                ws,
                "StreamStateChanged",
                lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED",
                timeout=20,
            )
            self.rtmp_stream_started = False
        try:
            self.rtmp_receiver.stop()
        except ProbeFailure as exc:
            self.rtmp_cleanup_failure = str(exc)
            raise

    def finalize_rtmp_trace(self, *, resource_only: bool = False, minimum_samples: int = 1) -> None:
        if self.rtmp_receiver is None:
            return
        if self.rtmp_producer_trace_path is None or self.rtmp_final_trace_path is None:
            raise ProbeFailure("RTMP trace fusion paths were not configured")
        if resource_only:
            self.rtmp_receiver.fuse_resource_trace(
                self.rtmp_producer_trace_path,
                self.rtmp_final_trace_path,
                minimum_samples=minimum_samples,
            )
        else:
            self.rtmp_receiver.fuse_trace(self.rtmp_producer_trace_path, self.rtmp_final_trace_path)

    def _join_directshow_reader(self) -> str | None:
        """Join the bounded stdout pump, retaining state on failure."""

        if self.directshow_thread is None:
            return None
        self.directshow_thread.join(timeout=2)
        if self.directshow_thread.is_alive():
            return "ProgramReturn DirectShow reader thread did not exit"
        self.directshow_thread = None
        return None

    def assert_directshow_consumer_alive(self) -> None:
        if self.directshow_proc is None:
            return
        if self.directshow_proc.poll() is None:
            return
        reader_failure = self._join_directshow_reader()
        if reader_failure is not None:
            self.directshow_cleanup_failure = reader_failure
            raise ProbeFailure(reader_failure)
        output = "\n".join(self.directshow_lines)
        raise ProbeFailure(
            "ProgramReturn DirectShow consumer exited during campaign: " + output[-4000:]
        )

    def stop_directshow_consumer(self) -> None:
        if self.directshow_proc is None:
            reader_failure = self._join_directshow_reader()
            if reader_failure is not None:
                self.directshow_cleanup_failure = reader_failure
                raise ProbeFailure(reader_failure)
            return
        process = self.directshow_proc
        failure: str | None = None
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception as exc:
                    failure = f"ProgramReturn DirectShow consumer could not be killed: {exc}"
            except Exception as exc:
                failure = f"ProgramReturn DirectShow consumer termination failed: {exc}"
        # Keep the Popen handle until the OS confirms exit.  In particular,
        # do not turn a still-running FFmpeg into a false PASS by clearing the
        # reference before terminate/kill has been observed.
        if process.poll() is None and failure is None:
            failure = "ProgramReturn DirectShow consumer remained alive after cleanup"
        if process.poll() is None:
            failure = failure or "ProgramReturn DirectShow consumer exit was not confirmed"
        else:
            try:
                process.wait(timeout=0)
            except Exception as exc:
                failure = failure or f"ProgramReturn DirectShow consumer reap failed: {exc}"
        reader_failure = self._join_directshow_reader()
        if reader_failure is not None:
            failure = failure or reader_failure
        if failure is not None:
            self.directshow_cleanup_failure = failure
            raise ProbeFailure(failure)
        # Only release the handle after both process exit and the stdout pump
        # have been confirmed.  This leaves subsequent shutdown/retry paths
        # able to inspect the owned process when cleanup is incomplete.
        self.directshow_proc = None
        self.directshow_thread = None

    def _pump_directshow(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            self.directshow_lines.append(line.rstrip("\r\n"))

    def shutdown(self) -> None:
        # Release the external DirectShow reader before the producer so the
        # queue/filter lease is closed while the ProgramReturn output is still
        # alive.  This also prevents a stale reader from consuming a later
        # runtime's named queue.
        directshow_failure: ProbeFailure | None = None
        try:
            self.stop_directshow_consumer()
        except ProbeFailure as exc:
            directshow_failure = exc
        rtmp_failure: ProbeFailure | None = None
        if self.rtmp_receiver is not None:
            try:
                self.rtmp_receiver.stop()
            except ProbeFailure as exc:
                self.rtmp_cleanup_failure = str(exc)
                rtmp_failure = exc
        pulsar_failure: str | None = self.graceful_shutdown_error
        if self.proc is not None and self.proc.poll() is None:
            if os.name == "nt":
                try:
                    if self.shutdown_event_handle is None:
                        raise ProbeFailure(
                            "Windows Pulsar process has no inherited shutdown event"
                        )
                    _windows_signal_shutdown_event(self.shutdown_event_handle)
                    self.graceful_shutdown_requested = True
                except ProbeFailure as exc:
                    self.graceful_shutdown_error = str(exc)
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.forced_kill_used = True
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=8)
                except Exception as exc:
                    pulsar_failure = f"Pulsar process could not be killed during cleanup: {exc}"
            except Exception as exc:
                self.graceful_shutdown_error = str(exc)
                # A failed graceful request may still leave the process alive;
                # containment is permitted, but makes this cleanup a failure.
                if self.proc.poll() is None:
                    self.forced_kill_used = True
                    try:
                        self.proc.kill()
                        self.proc.wait(timeout=8)
                    except Exception as fallback_exc:
                        pulsar_failure = (
                            "Pulsar process shutdown failed during cleanup: "
                            f"graceful={exc}; fallback={fallback_exc}"
                        )
            if self.graceful_shutdown_error is not None:
                pulsar_failure = pulsar_failure or self.graceful_shutdown_error
        if self.proc is not None and self.proc.poll() is None:
            pulsar_failure = pulsar_failure or "Pulsar process remained alive after cleanup"
        reader_failure = self._join_process_reader()
        if reader_failure is not None:
            pulsar_failure = pulsar_failure or reader_failure
        if os.name == "nt" and self.shutdown_event_handle is not None:
            try:
                _windows_close_handle(self.shutdown_event_handle)
            except ProbeFailure as exc:
                pulsar_failure = pulsar_failure or str(exc)
            finally:
                self.shutdown_event_handle = None
        if os.name == "nt" and self.proc is not None and not self.graceful_shutdown_requested:
            pulsar_failure = pulsar_failure or "Windows graceful shutdown event was not signaled"
        if self.forced_kill_used:
            pulsar_failure = pulsar_failure or "forced process kill was used; cleanup is not accepted"
        if directshow_failure is not None:
            if pulsar_failure:
                raise ProbeFailure(f"{directshow_failure}; {pulsar_failure}")
            raise directshow_failure
        if rtmp_failure is not None:
            if pulsar_failure:
                raise ProbeFailure(f"{rtmp_failure}; {pulsar_failure}")
            raise rtmp_failure
        if pulsar_failure:
            raise ProbeFailure(pulsar_failure)

    def assert_shutdown_clean(self, *, require_runtime_lease: bool = False) -> None:
        """Fail a campaign if an owned process, reader, or lease survived."""

        if self.directshow_cleanup_failure is not None:
            raise ProbeFailure(self.directshow_cleanup_failure)
        if self.rtmp_cleanup_failure is not None:
            raise ProbeFailure(self.rtmp_cleanup_failure)
        if self.rtmp_receiver is not None and self.rtmp_receiver.proc is not None and self.rtmp_receiver.proc.poll() is None:
            raise ProbeFailure("RTMP receiver is still alive after shutdown")
        if self.rtmp_receiver is not None and self.rtmp_receiver.thread is not None and self.rtmp_receiver.thread.is_alive():
            raise ProbeFailure("RTMP receiver reader thread is still alive after shutdown")
        if self.directshow_proc is not None and self.directshow_proc.poll() is None:
            raise ProbeFailure("ProgramReturn DirectShow consumer is still alive after shutdown")
        if self.directshow_thread is not None and self.directshow_thread.is_alive():
            raise ProbeFailure("ProgramReturn DirectShow reader thread is still alive after shutdown")
        if self.proc is not None and self.proc.poll() is None:
            raise ProbeFailure("Pulsar process is still alive after shutdown")
        if self.forced_kill_used:
            raise ProbeFailure("forced process kill was used; graceful cleanup is not accepted")
        if (
            self.proc is not None
            and self.proc.returncode is not None
            and self.proc.returncode != 0
        ):
            raise ProbeFailure(f"Pulsar exited with non-zero status {self.proc.returncode}")
        reader_failure = self._join_process_reader()
        if reader_failure is not None:
            raise ProbeFailure(reader_failure)
        if require_runtime_lease:
            lines = self.snapshot()
            if not any("PULSAR_RUNTIME_INSTANCE runtime_dir_lease=released" in line for line in lines):
                raise ProbeFailure("runtime directory lease was not released at shutdown")
            if not any("PULSAR_RUNTIME_INSTANCE lease=released" in line for line in lines):
                raise ProbeFailure("runtime instance lease was not released at shutdown")
            if self.trace_path is not None:
                alias_pattern = rf"PULSAR_LEGACY_ALIAS lease=(disabled|refused|acquired|released) id={re.escape(self.runtime_id)}"
                alias_states = [
                    match.group(1)
                    for line in lines
                    if (match := re.search(alias_pattern, line))
                ]
                if not alias_states:
                    raise ProbeFailure(
                        "legacy DirectShow alias state was not observable as disabled/refused/released"
                    )
                if "acquired" in alias_states and "released" not in alias_states:
                    raise ProbeFailure(
                        "legacy DirectShow alias was acquired but no matching release was observed"
                    )
        if os.name == "nt" and self.shutdown_control_expected:
            lines = self.snapshot()
            if not any(
                re.search(
                    rf"PULSAR_SHUTDOWN_CONTROL event=ready id={re.escape(self.runtime_id)} "
                    r"mechanism=inherited_event$",
                    line,
                )
                for line in lines
            ):
                raise ProbeFailure("child shutdown control readiness ACK was not observed")
            if not self.graceful_shutdown_requested:
                raise ProbeFailure("Windows graceful shutdown event was not signaled")
            if self.graceful_shutdown_error is not None:
                raise ProbeFailure(self.graceful_shutdown_error)
            if not any(
                re.search(
                    rf"PULSAR_SHUTDOWN_CONTROL event=signaled id={re.escape(self.runtime_id)} "
                    r"mechanism=inherited_event$",
                    line,
                )
                for line in lines
            ):
                raise ProbeFailure("child shutdown event acknowledgement was not observed")
            if not any("[pulsar-headless] shutting down" in line for line in lines):
                raise ProbeFailure("Pulsar graceful shutdown log line was not observed")

    def _join_process_reader(self) -> str | None:
        """Join Pulsar's stdout pump after child exit, with a hard bound."""

        if self.thread is None:
            return None
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            return "Pulsar log reader thread did not exit"
        self.thread = None
        return None


class Inbox:
    """Small v5 response/event collector for one WebSocket connection."""

    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def store(self, message: dict[str, Any]) -> None:
        if message.get("op") == 7:
            self.responses.append(message.get("d", {}))
        elif message.get("op") == 5:
            self.events.append(message.get("d", {}))

    async def receive_until_response(self, ws: Any, request_id: str) -> dict[str, Any]:
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if message.get("op") == 7:
                data = message.get("d", {})
                if data.get("requestId") == request_id:
                    return data
                self.responses.append(data)
            elif message.get("op") == 5:
                self.events.append(message.get("d", {}))

    async def receive_until_batch_response(self, ws: Any, request_id: str) -> dict[str, Any]:
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if message.get("op") == 9:
                data = message.get("d", {})
                if data.get("requestId") == request_id:
                    return data
            elif message.get("op") == 7:
                self.responses.append(message.get("d", {}))
            elif message.get("op") == 5:
                self.events.append(message.get("d", {}))


async def request(
    inbox: Inbox, ws: Any, request_type: str, request_id: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    request_body: dict[str, Any] = {
        "requestType": request_type,
        "requestId": request_id,
    }
    if data is not None:
        request_body["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": request_body}))
    return await inbox.receive_until_response(ws, request_id)


async def request_batch(
    inbox: Inbox,
    ws: Any,
    request_id: str,
    requests: list[dict[str, Any]],
    execution_type: int = 1,
) -> dict[str, Any]:
    payload = {
        "op": 8,
        "d": {
            "requestId": request_id,
            "executionType": execution_type,
            "haltOnFailure": False,
            "requests": requests,
        },
    }
    await ws.send(json.dumps(payload))
    return await inbox.receive_until_batch_response(ws, request_id)


def assert_success(response: dict[str, Any], operation: str) -> None:
    status = response.get("requestStatus") or {}
    if not status.get("result"):
        raise ProbeFailure(f"{operation} declined: {status}")


def assert_preview_frozen(response: dict[str, Any], operation: str) -> None:
    status = response.get("requestStatus") or {}
    comment = str(status.get("comment") or "")
    if status.get("result") or status.get("code") != 702 or "PREVIEW_FROZEN" not in comment:
        raise ProbeFailure(f"{operation} was not rejected as PREVIEW_FROZEN/702: {status}")


def batch_results(response: dict[str, Any], operation: str) -> list[dict[str, Any]]:
    results = response.get("results")
    if not isinstance(results, list):
        raise ProbeFailure(f"{operation} did not return a result list: {response}")
    return results


async def wait_event(
    inbox: Inbox,
    ws: Any,
    event_type: str,
    predicate: Any,
    timeout: float = 15,
) -> dict[str, Any]:
    """Wait for one v5 event while retaining unrelated messages."""

    def take_matching() -> dict[str, Any] | None:
        for index, event in enumerate(inbox.events):
            if event.get("eventType") != event_type:
                continue
            data = event.get("eventData") or {}
            if predicate is None or predicate(data):
                return inbox.events.pop(index)
        return None

    event = take_matching()
    if event is not None:
        return event

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeFailure(f"timeout waiting for event {event_type!r}")
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if message.get("op") == 5:
            inbox.events.append(message.get("d", {}))
        elif message.get("op") == 7:
            inbox.responses.append(message.get("d", {}))
        event = take_matching()
        if event is not None:
            return event


async def identify(ws: Any, password: str) -> None:
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    if hello.get("op") != 0:
        raise ProbeFailure(f"expected obs-websocket Hello, got {hello}")
    hello_data = hello.get("d") or {}
    identify_data: dict[str, Any] = {
        "rpcVersion": hello_data.get("rpcVersion", 1),
        "eventSubscriptions": 0x7FF,
    }
    auth = hello_data.get("authentication")
    if auth:
        identify_data["authentication"] = compute_auth(
            password, auth["salt"], auth["challenge"]
        )
    await ws.send(json.dumps({"op": 1, "d": identify_data}))
    identified = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    if identified.get("op") != 2:
        raise ProbeFailure(f"obs-websocket Identify failed: {identified}")


@dataclass(frozen=True)
class ReadyIdentity:
    lane_a: str
    lane_b: str
    lane_root_binding_valid: int
    program_main_view_valid: int
    program_main_video_valid: int
    preview_distinct_valid: int


@dataclass(frozen=True)
class Commit:
    count: int
    frame_id: int
    pts_ns: int
    onair_lane: int
    preview_lane: int
    lane_root_binding_valid: int
    program_main_view_valid: int
    program_main_video_valid: int
    preview_distinct_valid: int


def parse_ready(match: re.Match[str]) -> ReadyIdentity:
    return ReadyIdentity(match.group(1), match.group(2), *(int(match.group(i)) for i in range(3, 7)))


def parse_commit(match: re.Match[str]) -> Commit:
    return Commit(
        count=int(match.group(1)),
        frame_id=int(match.group(2)),
        pts_ns=int(match.group(3)),
        onair_lane=int(match.group(4)),
        preview_lane=int(match.group(5)),
        lane_root_binding_valid=int(match.group(6)),
        program_main_view_valid=int(match.group(7)),
        program_main_video_valid=int(match.group(8)),
        preview_distinct_valid=int(match.group(9)),
    )


def validate_commit(identity: ReadyIdentity, previous: Commit | None, commit: Commit) -> None:
    if commit.onair_lane not in (0, 1) or commit.preview_lane not in (0, 1):
        raise ProbeFailure(f"invalid role lanes in commit: {commit}")
    if commit.onair_lane == commit.preview_lane:
        raise ProbeFailure(f"OnAir and Preview lanes collided: {commit}")
    if (commit.lane_root_binding_valid, commit.program_main_view_valid,
        commit.program_main_video_valid, commit.preview_distinct_valid) != (1, 1, 1, 1):
        raise ProbeFailure(f"TakeCommitted reported an invalid surface relation: {commit}")
    if previous is not None:
        if commit.count != previous.count + 1:
            raise ProbeFailure(f"non-contiguous Take count: previous={previous} current={commit}")
        if commit.frame_id <= previous.frame_id:
            raise ProbeFailure(f"frame_id did not increase: previous={previous} current={commit}")
        if commit.pts_ns <= previous.pts_ns:
            raise ProbeFailure(f"PTS did not increase: previous={previous} current={commit}")


def find_ffmpeg(exe_parent: pathlib.Path | None = None) -> str | None:
    """Locate the FFmpeg binary used for the DirectShow return consumer."""

    roots = []
    if exe_parent is not None:
        roots.append(exe_parent)
    roots.append(REPO_ROOT / "upstream/.deps")
    for root in roots:
        if not root.exists():
            continue
        direct = root / "ffmpeg.exe"
        if direct.is_file():
            return str(direct)
        for candidate in root.glob("obs-deps-*-x64/bin/ffmpeg.exe"):
            if candidate.is_file():
                return str(candidate)
    return shutil.which("ffmpeg")


def find_ffprobe() -> str | None:
    """Use the ffprobe shipped with the OBS dependencies, then PATH."""

    for candidate in (REPO_ROOT / "upstream/.deps").glob("obs-deps-*-x64/bin/ffprobe.exe"):
        return str(candidate)
    return shutil.which("ffprobe")


def verify_recording(path_text: str, ffprobe: str) -> None:
    path = pathlib.Path(path_text)
    if not path.is_file():
        raise ProbeFailure(f"recording output does not exist: {path}")
    if path.stat().st_size < 100 * 1024:
        raise ProbeFailure(f"recording output is implausibly small: {path.stat().st_size} bytes")

    try:
        raw = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        info = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ProbeFailure(f"ffprobe failed for {path}: {exc}") from exc

    streams = info.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    if len(videos) != 1:
        raise ProbeFailure(f"expected one video stream, got {len(videos)}")
    video = videos[0]
    if video.get("codec_name") != "h264":
        raise ProbeFailure(f"expected H.264 video, got {video.get('codec_name')!r}")
    if (video.get("width"), video.get("height")) != (CANVAS_W, CANVAS_H):
        raise ProbeFailure(
            f"expected {CANVAS_W}x{CANVAS_H}, got {video.get('width')}x{video.get('height')}"
        )

    rate_text = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        numerator, denominator = rate_text.split("/", 1)
        frame_rate = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        frame_rate = 0.0
    if abs(frame_rate - 60.0) > 0.5:
        raise ProbeFailure(f"expected 60fps video, got {rate_text!r}")

    frame_text = video.get("nb_read_frames") or video.get("nb_frames")
    if frame_text not in (None, "N/A") and int(frame_text) < 60:
        raise ProbeFailure(f"recording contains too few frames: {frame_text}")
    duration_text = video.get("duration") or (info.get("format") or {}).get("duration")
    if duration_text in (None, "N/A") or float(duration_text) <= 0.5:
        raise ProbeFailure(f"recording has no useful duration: {duration_text!r}")

    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(audios) != 1 or audios[0].get("codec_name") != "aac":
        raise ProbeFailure(f"expected one AAC audio stream, got {audios}")
    print(
        f"   recording verified: {path.name} {path.stat().st_size} bytes, "
        f"H.264 {CANVAS_W}x{CANVAS_H} {frame_rate:g}fps, frames={frame_text}, "
        f"duration={float(duration_text):.3f}s, AAC"
    )


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png(data: bytes) -> tuple[int, int, int, bytearray]:
    """Decode the RGB/RGBA PNG returned by GetSourceScreenshot.

    This intentionally stays stdlib-only.  OBS's screenshot encoder emits
    non-interlaced 8-bit RGB(A), and decoding the pixels here is what makes
    the WGC/CEF checks non-vacuous instead of trusting a successful RPC.
    """

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG (bad signature)")
    offset = 8
    width = height = bit_depth = colour_type = interlace = 0
    idat = bytearray()
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        body_start = offset + 8
        body_end = body_start + length
        if body_end + 4 > len(data):
            raise ValueError("truncated PNG chunk")
        body = data[body_start:body_end]
        offset = body_end + 4
        if chunk_type == b"IHDR":
            width, height, bit_depth, colour_type, _compression, _filter, interlace = struct.unpack(
                ">IIBBBBB", body
            )
        elif chunk_type == b"IDAT":
            idat += body
        elif chunk_type == b"IEND":
            break
    if bit_depth != 8 or interlace != 0:
        raise ValueError("unsupported PNG (need non-interlaced 8-bit pixels)")
    if colour_type == 2:
        channels = 3
    elif colour_type == 6:
        channels = 4
    else:
        raise ValueError(f"unsupported PNG colour type {colour_type} (want RGB/RGBA)")
    if width <= 0 or height <= 0:
        raise ValueError("PNG has invalid dimensions")

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    expected = height * (stride + 1)
    if len(raw) < expected:
        raise ValueError("PNG scanline data is truncated")
    pixels = bytearray(width * height * channels)
    previous = bytearray(stride)
    position = 0
    for row in range(height):
        filter_type = raw[position]
        position += 1
        scanline = bytearray(raw[position : position + stride])
        position += stride
        if filter_type == 1:  # Sub
            for index in range(channels, stride):
                scanline[index] = (scanline[index] + scanline[index - channels]) & 0xFF
        elif filter_type == 2:  # Up
            for index in range(stride):
                scanline[index] = (scanline[index] + previous[index]) & 0xFF
        elif filter_type == 3:  # Average
            for index in range(stride):
                left = scanline[index - channels] if index >= channels else 0
                scanline[index] = (scanline[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for index in range(stride):
                left = scanline[index - channels] if index >= channels else 0
                upper_left = previous[index - channels] if index >= channels else 0
                scanline[index] = (scanline[index] + _paeth(left, previous[index], upper_left)) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unknown PNG filter {filter_type}")
        pixels[row * stride : (row + 1) * stride] = scanline
        previous = scanline
    return width, height, channels, pixels


def analyse_frame(width: int, height: int, channels: int, pixels: bytearray) -> dict[str, Any]:
    """Return cheap non-black/variance metrics over a representative sample."""

    total = width * height
    if total <= 0:
        return {"distinct": 0, "nonblack_ratio": 0.0, "all_same": True, "sampled": 0}
    step = max(1, total // 40000)
    distinct: set[int] = set()
    nonblack = 0
    sampled = 0
    first: tuple[int, int, int] | None = None
    all_same = True
    for index in range(0, total, step):
        base = index * channels
        red, green, blue = pixels[base], pixels[base + 1], pixels[base + 2]
        sampled += 1
        distinct.add((red << 16) | (green << 8) | blue)
        if first is None:
            first = (red, green, blue)
        elif (red, green, blue) != first:
            all_same = False
        if max(red, green, blue) > 8:
            nonblack += 1
    return {
        "distinct": len(distinct),
        "nonblack_ratio": nonblack / sampled if sampled else 0.0,
        "all_same": all_same,
        "sampled": sampled,
    }


def frame_is_nonblack(metrics: dict[str, Any], *, require_variance: bool) -> bool:
    if metrics["nonblack_ratio"] < 0.005:
        return False
    if require_variance and (metrics["all_same"] or metrics["distinct"] < 8):
        return False
    return True


def _strip_data_uri(image_data: str) -> bytes:
    comma = image_data.find(",")
    payload = image_data[comma + 1 :] if comma >= 0 else image_data
    return base64.b64decode(payload, validate=True)


async def wait_for_nonblack_source(
    inbox: Inbox,
    ws: Any,
    source_name: str,
    *,
    require_variance: bool,
) -> dict[str, Any]:
    """Poll an active source until OBS returns a real, non-black frame."""

    deadline = time.monotonic() + SOURCE_SCREENSHOT_DEADLINE_S
    attempt = 0
    last_failure = "no screenshot response"
    while time.monotonic() < deadline:
        attempt += 1
        response = await request(
            inbox,
            ws,
            "GetSourceScreenshot",
            f"workload-screenshot-{source_name}-{attempt}",
            {
                "sourceName": source_name,
                "imageFormat": "png",
                "imageWidth": CANVAS_W,
                "imageHeight": CANVAS_H,
            },
        )
        status = response.get("requestStatus") or {}
        if not status.get("result"):
            last_failure = f"RPC {status}"
            await asyncio.sleep(SOURCE_SCREENSHOT_INTERVAL_S)
            continue
        try:
            image_data = (response.get("responseData") or {}).get("imageData")
            if not isinstance(image_data, str) or not image_data:
                raise ValueError("responseData.imageData missing")
            png = _strip_data_uri(image_data)
            width, height, channels, pixels = decode_png(png)
            metrics = analyse_frame(width, height, channels, pixels)
        except (TypeError, ValueError, zlib.error) as exc:
            last_failure = f"PNG decode: {exc}"
            await asyncio.sleep(SOURCE_SCREENSHOT_INTERVAL_S)
            continue
        if (width, height) != (CANVAS_W, CANVAS_H):
            last_failure = f"unexpected dimensions {width}x{height}"
        elif frame_is_nonblack(metrics, require_variance=require_variance):
            print(
                f"   source frame verified: {source_name} {width}x{height} "
                f"distinct={metrics['distinct']} nonblack={metrics['nonblack_ratio']:.3f}"
            )
            return metrics
        else:
            last_failure = f"black/blank metrics={metrics}"
        await asyncio.sleep(SOURCE_SCREENSHOT_INTERVAL_S)
    raise ProbeFailure(
        f"source {source_name!r} never produced a non-black frame within "
        f"{SOURCE_SCREENSHOT_DEADLINE_S:.0f}s ({last_failure})"
    )


async def create_input(inbox: Inbox, ws: Any, scene: str, input_name: str, color: int) -> None:
    response = await request(
        inbox,
        ws,
        "CreateInput",
        f"create-input-{input_name}",
        {
            "sceneName": scene,
            "inputName": input_name,
            "inputKind": "color_source_v3",
            "inputSettings": {"color": color, "width": CANVAS_W, "height": CANVAS_H},
            "sceneItemEnabled": True,
        },
    )
    assert_success(response, f"CreateInput({scene})")


async def create_workload_input(
    inbox: Inbox,
    ws: Any,
    scene: str,
    lane: str,
    input_kind: str,
    input_settings: dict[str, Any],
) -> str:
    """Create one real producer instance in a public A/B scene."""

    if lane not in ("A", "B") or input_kind not in ("window_capture", "browser_source"):
        raise ProbeFailure(f"invalid public workload source: lane={lane!r} kind={input_kind!r}")
    input_name = LANE_SOURCE_NAMES[lane][input_kind]
    response = await request(
        inbox,
        ws,
        "CreateInput",
        f"create-workload-{input_kind}-{lane}",
        {
            "sceneName": scene,
            "inputName": input_name,
            "inputKind": input_kind,
            "inputSettings": input_settings,
            "sceneItemEnabled": True,
        },
    )
    assert_success(response, f"CreateInput({input_kind}, lane {lane}, scene {scene})")
    return input_name


async def assert_scene_item_presence(
    inbox: Inbox, ws: Any, scene: str, input_name: str, expected: bool, operation: str
) -> None:
    response = await request(
        inbox,
        ws,
        "GetSceneItemList",
        f"scene-items-{operation}-{scene}-{input_name}",
        {"sceneName": scene},
    )
    assert_success(response, f"GetSceneItemList({operation})")
    data = response.get("responseData") or response
    scene_items = data.get("sceneItems") or []
    names = {item.get("sourceName") for item in scene_items if isinstance(item, dict)}
    present = input_name in names
    if present != expected:
        raise ProbeFailure(
            f"scene mutation visibility mismatch at {operation}: scene={scene!r} "
            f"input={input_name!r} present={present}, expected={expected}"
        )


async def create_scene(inbox: Inbox, ws: Any, scene: str, input_name: str, color: int) -> None:
    response = await request(inbox, ws, "CreateScene", f"create-scene-{scene}", {"sceneName": scene})
    assert_success(response, f"CreateScene({scene})")
    await create_input(inbox, ws, scene, input_name, color)


async def create_public_lane_scenes(
    inbox: Inbox, ws: Any, process: PulsarProcess, *, lanes: tuple[str, ...] = ("A", "B")
) -> None:
    """Create selected public lanes and duplicate real producers per lane.

    The reference topology deliberately contains only lane A.  The dual-lane
    topology contains both A and B, so source registration itself cannot
    accidentally make the single-canvas baseline pay for a hidden producer.
    """

    if lanes not in (("A",), ("A", "B")):
        raise ProbeFailure(f"public lane topology must be ('A',) or ('A', 'B'), got {lanes!r}")
    if process.producer_count != len(lanes):
        raise ProbeFailure(
            f"process topology metadata disagrees with requested lanes: "
            f"producer_count={process.producer_count}, lanes={lanes!r}"
        )

    scene_specs = {
        "A": (SCENE_A, INPUT_A, COLOR_RED_ABGR),
        "B": (SCENE_B, INPUT_B, COLOR_GREEN_ABGR),
    }
    for lane in lanes:
        scene, input_name, colour = scene_specs[lane]
        await create_scene(inbox, ws, scene, input_name, colour)
    if process.cef_workload and not process.capture_window:
        raise ProbeFailure("--cef-workload requires --capture-window for a visible WGC target")

    for lane in lanes:
        scene = scene_specs[lane][0]
        if process.capture_window:
            await create_workload_input(
                inbox,
                ws,
                scene,
                lane,
                "window_capture",
                {
                    "window": process.capture_window,
                    "method": 2,
                    "cursor": True,
                    "client_area": True,
                },
            )
        if process.cef_workload:
            await create_workload_input(
                inbox,
                ws,
                scene,
                lane,
                "browser_source",
                {
                    "url": process.cef_url_for_lane(lane),
                    "is_local_file": False,
                    "width": CANVAS_W,
                    "height": CANVAS_H,
                    "fps_custom": True,
                    "fps": 60,
                    "shutdown": False,
                    "restart_when_active": False,
                    "webpage_control_level": 0,
                },
            )
async def verify_workload_sources(
    inbox: Inbox,
    ws: Any,
    process: PulsarProcess,
    *,
    lanes: tuple[str, ...] = ("A", "B"),
    require_pixels: bool = True,
) -> None:
    """Prove distinct WGC/CEF producers are attached to the selected lanes.

    Workload flags and the frontend's Default bootstrap inputs are not evidence.
    The probe reads back every A/B input kind/settings, checks exact scene-item
    ownership, and decodes a screenshot from every producer while A is Program
    and B is Preview.  The local CEF server renders a visible lane marker, so
    the two browser producers are also distinguishable rather than merely
    duplicate registrations.
    """

    if lanes not in (("A",), ("A", "B")):
        raise ProbeFailure(f"public lane topology must be ('A',) or ('A', 'B'), got {lanes!r}")
    if process.producer_count != len(lanes):
        raise ProbeFailure(
            f"process topology metadata disagrees with verification lanes: "
            f"producer_count={process.producer_count}, lanes={lanes!r}"
        )

    required: list[tuple[str, str, str]] = []
    for lane in lanes:
        if process.capture_window:
            required.append((lane, "window_capture", LANE_SOURCE_NAMES[lane]["window_capture"]))
        if process.cef_workload:
            if not process.capture_window:
                raise ProbeFailure("--cef-workload requires --capture-window for a visible WGC target")
            required.append((lane, "browser_source", LANE_SOURCE_NAMES[lane]["browser_source"]))
    if not required:
        return

    response = await request(inbox, ws, "GetInputList", "workload-input-list-ab")
    assert_success(response, "GetInputList(A/B workload)")
    input_data = response.get("responseData") or response
    inputs = input_data.get("inputs") or []
    by_name = {
        item.get("inputName"): item
        for item in inputs
        if isinstance(item, dict) and isinstance(item.get("inputName"), str)
    }
    if lanes == ("A",):
        hidden_reference_sources = {
            LANE_SOURCE_NAMES["B"][kind]
            for kind in ("window_capture", "browser_source")
            if process.capture_window or (kind == "browser_source" and process.cef_workload)
        }
        leaked = sorted(source_name for source_name in hidden_reference_sources if source_name in by_name)
        if leaked:
            raise ProbeFailure(
                "single-lane reference unexpectedly registered hidden B producers: "
                f"{leaked!r}"
            )
    for lane, expected_kind, source_name in required:
        item = by_name.get(source_name)
        if item is None:
            raise ProbeFailure(f"runtime did not register public lane {lane} source {source_name!r}")
        actual_kind = item.get("inputKind") or item.get("unversionedInputKind")
        if actual_kind != expected_kind:
            raise ProbeFailure(
                f"source {source_name!r} kind mismatch: got {actual_kind!r}, expected {expected_kind!r}"
            )
        settings_response = await request(
            inbox,
            ws,
            "GetInputSettings",
            f"workload-settings-{lane}-{expected_kind}",
            {"inputName": source_name},
        )
        assert_success(settings_response, f"GetInputSettings({source_name})")
        settings_data = settings_response.get("responseData") or settings_response
        settings = settings_data.get("inputSettings") or {}
        if expected_kind == "window_capture":
            if settings.get("window") != process.capture_window:
                raise ProbeFailure(
                    f"WGC target was not bound exactly for lane {lane}: got {settings.get('window')!r}, "
                    f"expected {process.capture_window!r}"
                )
            if settings.get("method") not in (2, "2"):
                raise ProbeFailure(f"WGC source {source_name!r} did not retain method=2: {settings!r}")
        else:
            expected_url = process.cef_url_for_lane(lane)
            if settings.get("url") != expected_url:
                raise ProbeFailure(
                    f"CEF URL for lane {lane} was not bound exactly: got {settings.get('url')!r}, "
                    f"expected {expected_url!r}"
                )
            if settings.get("is_local_file") is True:
                raise ProbeFailure(f"CEF source {source_name!r} unexpectedly became a local file")

    items_by_scene: dict[str, set[str]] = {}
    selected_scenes = tuple(SCENE_A if lane == "A" else SCENE_B for lane in lanes)
    for scene in selected_scenes:
        scene_response = await request(
            inbox,
            ws,
            "GetSceneItemList",
            f"workload-scene-items-{scene}",
            {"sceneName": scene},
        )
        assert_success(scene_response, f"GetSceneItemList({scene} workload)")
        scene_data = scene_response.get("responseData") or scene_response
        scene_items = scene_data.get("sceneItems") or []
        items_by_scene[scene] = {
            item.get("sourceName")
            for item in scene_items
            if isinstance(item, dict) and item.get("sceneItemEnabled", True)
        }
    for lane, _kind, source_name in required:
        own_scene = SCENE_A if lane == "A" else SCENE_B
        other_scene = SCENE_B if lane == "A" else SCENE_A
        if source_name not in items_by_scene[own_scene]:
            raise ProbeFailure(f"source {source_name!r} is not an enabled item in public {lane} scene")
        if other_scene in items_by_scene and source_name in items_by_scene[other_scene]:
            raise ProbeFailure(f"source {source_name!r} leaked into the other public lane scene")

    if require_pixels:
        for lane, kind, source_name in required:
            await wait_for_nonblack_source(
                inbox,
                ws,
                source_name,
                require_variance=kind == "browser_source",
            )
    print(
        "   public workload topology verified: duplicated producer instances "
        f"WGC={sum(kind == 'window_capture' for _lane, kind, _name in required)}, "
        f"CEF={sum(kind == 'browser_source' for _lane, kind, _name in required)}; "
        f"topology={process.producer_topology} producer_count={process.producer_count} "
        f"lanes={','.join(lanes)}; scene ownership + settings + screenshots "
        "(Default bootstrap excluded)"
    )


def wait_for_trace_record(
    process: PulsarProcess,
    record_type: str,
    take_command_id: str,
    *,
    event_type: str | None = None,
    boundary: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Fail fast when an opt-in Take never reaches the JSONL producer.

    The old driver waited for the full campaign and only then discovered that
    the session line was malformed or that no event/observation crossed the
    proc boundary. A first-Take check keeps that diagnostic close to the
    ingress logs and prevents wasting a 100-take run.
    """

    if process.trace_path is None:
        raise ProbeFailure("trace record check requested without --trace")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            lines = process.trace_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        for line_number, line in enumerate(lines, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProbeFailure(
                    f"runtime trace malformed at line {line_number} while waiting for "
                    f"{record_type}: {exc}"
                ) from exc
            if not isinstance(record, dict) or record.get("record_type") != record_type:
                continue
            if record_type == "event":
                value = record.get("event") or {}
                if value.get("runtime_instance_id") != process.runtime_id:
                    continue
                if value.get("take_command_id") != take_command_id:
                    continue
                if event_type is not None and value.get("event_type") != event_type:
                    continue
                return value
            if record.get("runtime_instance_id") != process.runtime_id:
                continue
            if record.get("take_command_id") != take_command_id:
                continue
            if boundary is not None and record.get("boundary") != boundary:
                continue
            return record
        time.sleep(0.1)
    diagnostics = [line for line in process.snapshot() if "pulsar-runtime-telemetry" in line]
    diagnostic_text = " | ".join(diagnostics[-8:]) or "no runtime-telemetry ingress diagnostics"
    selector = event_type or boundary or record_type
    raise ProbeFailure(
        f"trace did not emit {selector} for {take_command_id} within {timeout:.1f}s; "
        f"diagnostics: {diagnostic_text}"
    )


async def wait_for_take_boundaries(
    process: PulsarProcess,
    receiver: RtmpReceiver,
    commit: Commit,
    correlation: RtmpPacketCorrelation,
    *,
    timeout: float = 20.0,
) -> dict[str, dict[str, Any]]:
    """Gate the next Take on one exact producer record at every boundary.

    A current runtime keeps one correlation context for the most recent Take.
    Dispatching again before the context has emitted all consumers can replace
    that context and leave a trace that looks valid but is incomplete.  This
    barrier is deliberately outside the latency calculation: it only waits for
    the four already timestamped records and never rewrites their timestamps.
    """

    if process.trace_path is None:
        raise ProbeFailure("Take boundary waiting requires --trace")
    if receiver is None:
        raise ProbeFailure("traced Take boundary waiting requires an RTMP receiver")
    if timeout <= 0:
        raise ProbeFailure("Take boundary wait timeout must be positive")
    take_id = f"take-{commit.count:03d}"
    intent_id = f"intent-{commit.count:03d}"
    deadline = time.monotonic() + timeout

    while True:
        if process.proc is not None and process.proc.poll() is not None:
            raise ProbeFailure(
                f"runtime exited before complete boundary correlation for {take_id}"
            )
        try:
            lines = process.trace_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        records: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                # The producer may be between write and newline.  The parser
                # will reject a durable malformed line after the campaign;
                # this poll simply waits for the complete record.
                continue
            if isinstance(value, dict):
                records.append(value)

        accepted_events: list[dict[str, Any]] = []
        committed_events: list[dict[str, Any]] = []
        observations: dict[str, list[dict[str, Any]]] = {
            boundary: [] for boundary in PRODUCER_BOUNDARIES
        }
        commit_time: int | None = None
        commit_revisions: object | None = None
        for record in records:
            if record.get("record_type") == "event":
                event = record.get("event")
                if not isinstance(event, dict) or event.get("take_command_id") != take_id:
                    continue
                if event.get("runtime_instance_id") != process.runtime_id:
                    raise ProbeFailure(f"{take_id} event has a stale runtime_instance_id")
                if event.get("command_id") != take_id or event.get("intent_id") != intent_id:
                    raise ProbeFailure(f"{take_id} event has mismatched command/intent identity")
                if event.get("event_type") == "TakeAccepted":
                    accepted_events.append(event)
                elif event.get("event_type") == "TakeCommitted":
                    committed_events.append(event)
                continue
            if record.get("record_type") != "observation":
                continue
            if record.get("take_command_id") != take_id:
                continue
            if record.get("runtime_instance_id") != process.runtime_id:
                raise ProbeFailure(f"{take_id} observation has a stale runtime_instance_id")
            if record.get("command_id") != take_id or record.get("intent_id") != intent_id:
                raise ProbeFailure(f"{take_id} observation has mismatched command/intent identity")
            boundary = record.get("boundary")
            if boundary not in PRODUCER_BOUNDARIES or record.get("valid") is not True:
                continue
            observations[boundary].append(record)

        if len(accepted_events) > 1:
            raise ProbeFailure(f"{take_id} has duplicate TakeAccepted records")
        if len(committed_events) > 1:
            raise ProbeFailure(f"{take_id} has duplicate TakeCommitted records")
        if committed_events:
            event = committed_events[0]
            if event.get("frame_id") != commit.frame_id or event.get("pts_ns") != commit.pts_ns:
                raise ProbeFailure(f"{take_id} commit log differs from TakeCommitted evidence")
            commit_time = event.get("observed_at_monotonic_ns")
            commit_revisions = event.get("revisions")
            if type(commit_time) is not int or not isinstance(commit_revisions, dict):
                raise ProbeFailure(f"{take_id} TakeCommitted metadata is incomplete")
            # Re-evaluate observations now that the commit timestamp is known.
            for boundary in PRODUCER_BOUNDARIES:
                observations[boundary].clear()
            for record in records:
                if (
                    record.get("record_type") != "observation"
                    or record.get("take_command_id") != take_id
                    or record.get("runtime_instance_id") != process.runtime_id
                    or record.get("boundary") not in PRODUCER_BOUNDARIES
                    or record.get("valid") is not True
                ):
                    continue
                observed_at = record.get("observed_at_monotonic_ns")
                if type(observed_at) is not int or observed_at < commit_time:
                    continue
                if record.get("command_id") != take_id or record.get("intent_id") != intent_id:
                    raise ProbeFailure(f"{take_id} observation has mismatched command/intent identity")
                if record.get("frame_id") != commit.frame_id or record.get("pts_ns") != commit.pts_ns:
                    raise ProbeFailure(
                        f"{take_id} {record.get('boundary')} frame/PTS does not match committed identity"
                    )
                if record.get("revisions") != commit_revisions:
                    raise ProbeFailure(
                        f"{take_id} {record.get('boundary')} revisions do not match commit"
                    )
                observations[record["boundary"]].append(record)

        receiver_packets, receiver_failure = receiver.snapshot()
        if receiver_failure:
            raise ProbeFailure(f"RTMP receiver failed during {take_id}: {receiver_failure}")
        for boundary in PRODUCER_BOUNDARIES:
            if len(observations[boundary]) > 1:
                raise ProbeFailure(
                    f"{take_id} has duplicate valid {boundary} records: "
                    f"{len(observations[boundary])}"
                )
        encoded_records = observations["encoded_first_packet"]
        packet_candidates: list[RtmpPacketCandidate] = []
        if len(encoded_records) == 1:
            packet_candidates = correlation.candidates(encoded_records[0], receiver_packets)
            if len(packet_candidates) > 1:
                raise ProbeFailure(
                    f"{take_id} has ambiguous RTMP packet correlation: "
                    f"{len(packet_candidates)} candidates"
                )

        complete = all(len(observations[boundary]) == 1 for boundary in PRODUCER_BOUNDARIES)
        if complete and len(packet_candidates) == 1 and accepted_events and committed_events:
            candidate = packet_candidates[0]
            packet = candidate.packet
            correlation.commit(candidate)
            return {
                **{boundary: observations[boundary][0] for boundary in PRODUCER_BOUNDARIES},
                "rtmp_first_packet": packet,
            }

        if time.monotonic() >= deadline:
            counts = ", ".join(
                f"{boundary}={len(observations[boundary])}" for boundary in PRODUCER_BOUNDARIES
            )
            counts += f", rtmp_candidates={len(packet_candidates)}, rtmp_packets={len(receiver_packets)}"
            tail = failure_tail(process.snapshot(), 8)
            raise ProbeFailure(
                f"{take_id} boundary correlation incomplete before {timeout:.1f}s: {counts}\n{tail}"
            )
        await asyncio.sleep(0.05)


def take_telemetry_data(process: PulsarProcess, number: int, target_scene: str) -> dict[str, Any]:
    """Build the opt-in #246 envelope carried through the legacy Take route."""

    command_id = f"take-{number:03d}"
    intent_id = f"intent-{number:03d}"
    target_lane = "B" if number % 2 else "A"
    # This timestamp crosses into libobs, so it must use the QPC-compatible
    # wire clock.  Keep the deadline comfortably beyond the request/graphics
    # hop while retaining a bounded, observable expiry guard in the producer.
    freeze_until = make_wire_deadline_ns()
    command = {
        "requestType": "TriggerStudioModeTransition",
        "requestData": {"sceneName": target_scene},
        "command_id": command_id,
        "intent_id": intent_id,
        "runtime_instance_id": process.runtime_id,
        "take_command_id": command_id,
        "target_lane_id": target_lane,
        "target_scene_id": target_scene,
        "freeze_until_monotonic_ns": freeze_until,
    }
    digest = hashlib.sha256(
        json.dumps(command, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "pulsarTelemetry": {
            "command_id": command_id,
            "intent_id": intent_id,
            "runtime_instance_id": process.runtime_id,
            "take_command_id": command_id,
            "target_lane_id": target_lane,
            "target_scene_id": target_scene,
            "freeze_until_monotonic_ns": freeze_until,
            "payload_sha256": digest,
        }
    }


async def start_resource_recording(inbox: Inbox, ws: Any, process: PulsarProcess) -> None:
    """Start recording and prove the setup-time encoder is the requested one."""

    response = await request(inbox, ws, "StartRecord", "resource-start-record")
    assert_success(response, "StartRecord(resource phase)")
    await wait_event(
        inbox,
        ws,
        "RecordStateChanged",
        lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED",
    )
    bind_lines = [line for line in process.snapshot() if ENCODER_BIND_RE.search(line)]
    if len(bind_lines) != 1:
        raise ProbeFailure(
            "resource phase expected exactly one setup-time encoder bind before active record, "
            f"got {len(bind_lines)}"
        )
    # Let at least a few output frames reach the active encoder before the
    # sampler window starts.  The resource records themselves still attest
    # activity from obs_encoder_active(videoEncoder), not this delay.
    await asyncio.sleep(0.5)


async def stop_resource_recording(
    inbox: Inbox, ws: Any, *, request_id: str = "resource-stop-record"
) -> str:
    """Stop the resource-phase recording and return its verified output path."""

    response = await request(inbox, ws, "StopRecord", request_id)
    assert_success(response, "StopRecord(resource phase)")
    stopped = await wait_event(
        inbox,
        ws,
        "RecordStateChanged",
        lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED",
    )
    output_path = (stopped.get("eventData") or {}).get("outputPath") or ""
    if not output_path:
        raise ProbeFailure("resource phase STOPPED event did not include outputPath")
    return output_path


async def collect_resource_samples(
    process: PulsarProcess, mode: str, minimum_samples: int, timeout: float
) -> int:
    """Keep a traced runtime alive until its native resource sampler has emitted samples.

    The resource records are produced by the runtime's OBS/platform counters and
    nvidia-smi adapter, not reconstructed from Python timing.  This helper only
    performs the lifecycle/availability check and never writes evidence itself.
    """

    ffprobe = find_ffprobe()
    if not ffprobe:
        raise ProbeSkip("ffprobe is required to attest an active NVENC resource phase")

    process.wait_for_shutdown_control_ready(timeout=60)
    ready_match = process.wait_for(READY_RE, timeout=60)
    process.assert_shutdown_control_ready()
    ws_url = ready_match.group(1)
    if ready_match.group(2) != process.password:
        raise ProbeFailure("PULSAR_READY password did not match the generated probe secret")
    expected_reference = mode == "reference"
    activation, activation_source, rollback_after_takes = assert_dual_lane_activation(
        process,
        expected=not expected_reference,
        required_source="resource-reference" if expected_reference else "PULSAR_DUAL_LANE_ENABLED=1",
    )
    print(
        "   dual-lane activation consumed: "
        f"activation={activation} source={activation_source} "
        f"rollback_after_takes={rollback_after_takes} mode={mode}"
    )

    async with websockets.connect(
        ws_url, subprotocols=["obswebsocket.json"], open_timeout=15
    ) as ws:
        await identify(ws, process.password)
        inbox = Inbox()
        lanes = ("A",) if mode == "reference" else ("A", "B")
        await create_public_lane_scenes(inbox, ws, process, lanes=lanes)
        response = await request(
            inbox,
            ws,
            "SetCurrentProgramScene",
            "resource-set-program-A",
            {"sceneName": SCENE_A},
        )
        assert_success(response, "SetCurrentProgramScene(A, resource)")
        if mode == "dual_lane":
            response = await request(
                inbox,
                ws,
                "SetStudioModeEnabled",
                "resource-enable-studio",
                {"studioModeEnabled": True},
            )
            assert_success(response, "SetStudioModeEnabled(true, resource)")
            response = await request(
                inbox,
                ws,
                "SetCurrentPreviewScene",
                "resource-set-preview-B",
                {"sceneName": SCENE_B},
            )
            assert_success(response, "SetCurrentPreviewScene(B, resource)")
            await verify_workload_sources(inbox, ws, process, lanes=lanes)
        elif process.capture_window or process.cef_workload:
            # The reference phase intentionally creates and measures one
            # producer on A.  The dual phase creates and measures both
            # producer pairs on A/B; no hidden B registration contaminates the
            # baseline.  Both paths use the same non-black pixel gate so a
            # dead/black WGC or CEF producer cannot make the resource delta
            # look like a valid topology comparison.
            await verify_workload_sources(inbox, ws, process, lanes=lanes, require_pixels=True)

        # Resource samples are admissible for AC-13 only while the requested
        # NVENC encoder is genuinely active.  The reference phase therefore
        # starts the same local recording path as the long dual-lane phase and
        # keeps it running for the entire sample window.
        encoder_match = process.wait_for(ENCODER_RE, timeout=60)
        actual_family = encoder_match.group(1).lower()
        if actual_family != process.encoder:
            if process.encoder == "nvenc":
                raise ProbeSkip(
                    f"requested NVENC but Pulsar boot selected {actual_family}; no usable NVENC device"
                )
            raise ProbeFailure(
                f"requested encoder family {process.encoder!r}, boot selected {actual_family!r}"
            )
        if mode == "reference" and process.encoder != "nvenc":
            raise ProbeFailure("AC-13 reference resource phase requires encoder=nvenc")

        recording_started = False
        recording_stopped = False
        output_path: str | None = None

        async def stop_recording_after_error() -> None:
            nonlocal recording_stopped, output_path
            if process.rtmp_stream_started:
                await process.stop_rtmp_stream(inbox, ws)
            if not recording_started or recording_stopped:
                return
            output_path = await stop_resource_recording(
                inbox, ws, request_id="resource-stop-record-cleanup"
            )
            recording_stopped = True

        try:
            # Mark the attempt before issuing StartRecord so a failure after
            # the output has actually started still enters the strict stop
            # path.  If StartRecord itself declines, the cleanup request is
            # harmlessly rejected and the original failure is preserved.
            recording_started = True
            await start_resource_recording(inbox, ws, process)
            if process.rtmp_receiver is not None:
                await process.start_rtmp_stream(inbox, ws)
            deadline = time.monotonic() + timeout
            count = 0
            active_count = 0
            rtmp_active_count = 0
            while True:
                if process.trace_path is None:
                    raise ProbeFailure("resource sampling requires --trace")
                count = 0
                active_count = 0
                rtmp_active_count = 0
                try:
                    with process.trace_path.open("r", encoding="utf-8") as handle:
                        for line in handle:
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if (
                                record.get("record_type") == "resource_sample"
                                and record.get("sample_mode") == mode
                                and record.get("runtime_instance_id") == process.runtime_id
                            ):
                                count += 1
                                if record.get("encoder_active") is True:
                                    active_count += 1
                                    if record.get("rtmp_load_active") is True:
                                        rtmp_active_count += 1
                except FileNotFoundError:
                    count = 0
                    active_count = 0
                    rtmp_active_count = 0
                if rtmp_active_count >= minimum_samples:
                    break
                if time.monotonic() >= deadline:
                    if process.proc is not None and process.proc.poll() is None:
                        raise ProbeSkip(
                            "native resource sampler produced no complete active-encoder+RTMP-load samples; "
                            "verify nvidia-smi, platform counters and the recording output on this host"
                        )
                    raise ProbeFailure(
                        f"runtime exited before collecting {minimum_samples} active {mode} resource samples"
                    )
                await asyncio.sleep(0.25)

            if process.rtmp_receiver is not None:
                await process.stop_rtmp_stream(inbox, ws)
            output_path = await stop_resource_recording(inbox, ws)
            recording_stopped = True
        except Exception:
            try:
                await stop_recording_after_error()
            except Exception as cleanup_exc:
                raise ProbeFailure(
                    f"resource sampling failed and StopRecord cleanup failed: {cleanup_exc}"
                ) from cleanup_exc
            raise

        if output_path is None:
            raise ProbeFailure("active resource phase stopped without a recording output path")
        verify_recording(output_path, ffprobe)
        print(
            f"   active encoder+RTMP resource samples verified: total={count} active={active_count} "
            f"rtmp_active={rtmp_active_count} "
            f"mode={mode}"
        )
        return rtmp_active_count


async def wait_for_eligible_resource_samples(
    process: PulsarProcess,
    mode: str,
    minimum_samples: int,
    timeout: float,
) -> int:
    """Wait for observed NVENC+RTMP samples without stopping any output.

    This is used after the final dual-lane Take and before StopStream or
    StopRecord.  The sampler cadence is independent from the Take cadence, so
    ending outputs immediately after the last Take can otherwise leave the
    trace below the AC-13 minimum.
    """

    if minimum_samples < 1:
        raise ProbeFailure("resource sample minimum must be positive")
    if timeout <= 0:
        raise ProbeFailure("resource sample wait timeout must be positive")
    deadline = time.monotonic() + timeout
    while True:
        if process.trace_path is None:
            raise ProbeFailure("resource sample waiting requires --trace")
        if process.proc is not None and process.proc.poll() is not None:
            raise ProbeFailure(
                f"runtime exited before collecting {minimum_samples} active NVENC+RTMP "
                f"{mode} resource samples"
            )
        total = 0
        active = 0
        eligible = 0
        try:
            with process.trace_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    if (
                        record.get("record_type") != "resource_sample"
                        or record.get("sample_mode") != mode
                        or record.get("runtime_instance_id") != process.runtime_id
                    ):
                        continue
                    total += 1
                    if record.get("encoder_active") is True:
                        active += 1
                    if (
                        record.get("encoder_active") is True
                        and record.get("encoder_family") == "nvenc"
                        and record.get("rtmp_load_active") is True
                    ):
                        eligible += 1
        except FileNotFoundError:
            total = active = eligible = 0
        if eligible >= minimum_samples:
            print(
                f"   post-Take resource wait: total={total} active={active} "
                f"eligible_nvenc_rtmp={eligible} mode={mode}"
            )
            return eligible
        if time.monotonic() >= deadline:
            if process.proc is not None and process.proc.poll() is None:
                raise ProbeSkip(
                    "native resource sampler did not produce enough active NVENC+RTMP samples "
                    f"before the {timeout:.1f}s post-Take deadline"
                )
            raise ProbeFailure(
                f"runtime exited before collecting {minimum_samples} active NVENC+RTMP "
                f"{mode} resource samples"
            )
        await asyncio.sleep(0.25)


async def assert_distinct_selected_scenes(
    inbox: Inbox,
    ws: Any,
    operation: str,
    expected_program: str,
    expected_preview: str,
) -> None:
    program_response = await request(inbox, ws, "GetCurrentProgramScene", f"{operation}-program")
    assert_success(program_response, f"GetCurrentProgramScene({operation})")
    program_data = program_response.get("responseData") or program_response
    program = program_data.get("currentProgramSceneName") or program_data.get("sceneName")

    preview_response = await request(inbox, ws, "GetCurrentPreviewScene", f"{operation}-preview")
    assert_success(preview_response, f"GetCurrentPreviewScene({operation})")
    preview_data = preview_response.get("responseData") or preview_response
    preview = preview_data.get("currentPreviewSceneName") or preview_data.get("sceneName")

    if (program, preview) != (expected_program, expected_preview):
        raise ProbeFailure(
            f"logical Program/Preview selection mismatch at {operation}: "
            f"got {(program, preview)!r}, expected {(expected_program, expected_preview)!r}"
        )
    if program == preview:
        raise ProbeFailure(f"logical Program and Preview scenes aliased at {operation}: {program!r}")


async def drive(
    process: PulsarProcess,
    takes: int,
    *,
    warmup_takes: int = 0,
    minimum_resource_samples: int = 0,
    resource_sample_timeout: float = 0.0,
) -> list[Commit]:
    if takes < 1 or warmup_takes < 0:
        raise ProbeFailure("takes must be positive and warmup_takes must not be negative")
    total_takes = warmup_takes + takes
    process.wait_for_shutdown_control_ready(timeout=60)
    ready_match = process.wait_for(READY_RE, timeout=60)
    process.assert_shutdown_control_ready()
    ws_url = ready_match.group(1)
    ready_password = ready_match.group(2)
    if ready_password != process.password:
        raise ProbeFailure("PULSAR_READY password did not match the generated probe secret")

    activation, activation_source, rollback_after_takes = assert_dual_lane_activation(
        process, expected=True, required_source="PULSAR_DUAL_LANE_ENABLED=1"
    )
    if rollback_after_takes:
        raise ProbeFailure(
            "ordinary Take campaign unexpectedly has rollback drill armed: "
            f"source={activation_source!r} after={rollback_after_takes}"
        )
    print(
        "   dual-lane activation consumed: "
        f"activation={activation} source={activation_source} rollback_after_takes=0"
    )

    identity = parse_ready(process.wait_for(DUAL_READY_RE, timeout=60))
    if identity.lane_a == identity.lane_b:
        raise ProbeFailure(f"LaneA and LaneB are aliased: {identity}")
    if (identity.lane_root_binding_valid, identity.program_main_view_valid,
        identity.program_main_video_valid, identity.preview_distinct_valid) != (1, 1, 1, 1):
        raise ProbeFailure(f"Dual-lane ready reported an invalid surface relation: {identity}")

    encoder_match = process.wait_for(ENCODER_RE, timeout=60)
    actual_family = encoder_match.group(1).lower()
    if actual_family != process.encoder:
        if process.encoder == "nvenc":
            raise ProbeSkip(
                f"requested NVENC but Pulsar boot selected {actual_family}; no usable NVENC device"
            )
        raise ProbeFailure(
            f"requested encoder family {process.encoder!r}, boot selected {actual_family!r}"
        )
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise ProbeSkip("ffprobe is required for the active-recording acceptance proof")
    if process.trace_path is not None:
        trace_receiver = process.rtmp_receiver
        if trace_receiver is None:
            raise ProbeFailure(
                "traced Take campaigns require --rtmp-receiver for complete boundary correlation"
            )
        process.start_directshow_consumer()

    async with websockets.connect(
        ws_url, subprotocols=["obswebsocket.json"], open_timeout=15
    ) as ws:
        await identify(ws, process.password)
        inbox = Inbox()
        await create_public_lane_scenes(inbox, ws, process, lanes=("A", "B"))

        # Establish a known program before studio mode.  The non-studio path
        # mutates the active lane composition but keeps the physical root.
        response = await request(
            inbox,
            ws,
            "SetCurrentProgramScene",
            "set-initial-program",
            {"sceneName": SCENE_A},
        )
        assert_success(response, "SetCurrentProgramScene(A)")
        # Mutate the public Program scene after the physical lane is already
        # bound.  The wrapper must retain this exact source rather than a
        # private duplicate; the raw NV12 probe verifies the resulting pixels.
        await create_input(inbox, ws, SCENE_A, INPUT_A_LIVE, COLOR_BLUE_ABGR)
        response = await request(
            inbox,
            ws,
            "SetStudioModeEnabled",
            "enable-studio",
            {"studioModeEnabled": True},
        )
        assert_success(response, "SetStudioModeEnabled(true)")
        response = await request(
            inbox,
            ws,
            "SetCurrentPreviewScene",
            "set-initial-preview",
            {"sceneName": SCENE_B},
        )
        assert_success(response, "SetCurrentPreviewScene(B)")
        await verify_workload_sources(inbox, ws, process, lanes=("A", "B"))

        # Start a real local recording before the first Cut.  This makes the
        # encoder active for the whole campaign and exercises the exact
        # constraint that no Take may call obs_encoder_set_video again.
        response = await request(inbox, ws, "StartRecord", "start-record")
        assert_success(response, "StartRecord")
        await wait_event(
            inbox,
            ws,
            "RecordStateChanged",
            lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED",
        )
        bind_lines = [
            line
            for line in process.snapshot()
            if ENCODER_BIND_RE.search(line)
        ]
        if len(bind_lines) != 1:
            raise ProbeFailure(
                f"expected exactly one setup-time encoder bind before active record, got {len(bind_lines)}"
            )
        # Give the recording path a few frames before stressing the Cut loop.
        await asyncio.sleep(0.5)
        if process.rtmp_receiver is not None:
            await process.start_rtmp_stream(inbox, ws)

        commits: list[Commit] = []
        complete_boundary_count = 0
        rtmp_correlation = trace_receiver.live_correlation if trace_receiver is not None else RtmpPacketCorrelation()
        for number in range(1, total_takes + 1):
            process.assert_directshow_consumer_alive()
            target = SCENE_B if number % 2 else SCENE_A
            response = await request(
                inbox,
                ws,
                "SetCurrentPreviewScene",
                f"set-preview-{number}",
                {"sceneName": target},
            )
            assert_success(response, f"SetCurrentPreviewScene({target})")
            if number == 1:
                # The Preview lane is now bound to B.  Apply its mutation only
                # after binding so a snapshot implementation cannot pass this
                # lifecycle exercise accidentally.
                await create_input(inbox, ws, SCENE_B, INPUT_B_LIVE, COLOR_RED_ABGR)
            await assert_distinct_selected_scenes(
                inbox,
                ws,
                f"take-{number}",
                expected_program=SCENE_A if number % 2 else SCENE_B,
                expected_preview=target,
            )
            if number == 1:
                # SerialFrame executes these requests on one graphics tick.
                # The first request publishes TakeAccepted/pending; the
                # second must be rejected before CreateInput can mutate the
                # scene that is being promoted. This is the supported
                # WebSocket control-plane proof of the AC-04 freeze.
                freeze_batch = await request_batch(
                    inbox,
                    ws,
                    "take-1-freeze-batch",
                    [
                        {
                            "requestType": "TriggerStudioModeTransition",
                            "requestData": take_telemetry_data(process, number, target),
                        },
                        {
                            "requestType": "CreateInput",
                            "requestData": {
                                "sceneName": SCENE_B,
                                "inputName": INPUT_B_FROZEN,
                                "inputKind": "color_source_v3",
                                "inputSettings": {
                                    "color": COLOR_GREEN_ABGR,
                                    "width": CANVAS_W,
                                    "height": CANVAS_H,
                                },
                                "sceneItemEnabled": True,
                            },
                        },
                    ],
                )
                freeze_results = batch_results(freeze_batch, "take-1-freeze-batch")
                if len(freeze_results) != 2:
                    raise ProbeFailure(
                        f"take-1-freeze-batch returned {len(freeze_results)} results, expected 2"
                    )
                assert_success(freeze_results[0], "TriggerStudioModeTransition(1)")
                assert_preview_frozen(freeze_results[1], "CreateInput(B during pending)")
            else:
                response = await request(
                    inbox,
                    ws,
                    "TriggerStudioModeTransition",
                    f"take-{number}",
                    take_telemetry_data(process, number, target),
                )
                assert_success(response, f"TriggerStudioModeTransition({number})")
            commit = parse_commit(process.wait_for_commit(number, timeout=15))
            validate_commit(identity, commits[-1] if commits else None, commit)
            if process.trace_path is not None:
                if trace_receiver is None:
                    raise ProbeFailure("traced Take receiver disappeared before boundary gate")
                await wait_for_take_boundaries(
                    process,
                    trace_receiver,
                    commit,
                    rtmp_correlation,
                    timeout=20.0,
                )
                complete_boundary_count += 1
            commits.append(commit)
            if number in (1, takes) or number % 25 == 0:
                print(
                    f"   Take {number:03d}: frame_id={commit.frame_id} pts_ns={commit.pts_ns} "
                    f"onair_lane={commit.onair_lane} preview_lane={commit.preview_lane}"
                )
            if number == 1:
                await assert_scene_item_presence(
                    inbox,
                    ws,
                    SCENE_B,
                    INPUT_B_FROZEN,
                    False,
                    "CreateInput(B during pending)",
                )
                # After the Cut, A is the Preview scene.  This is the
                # post-Take lifecycle proof: its producer remains the same
                # public scene and can be mutated only after commit.
                await create_input(inbox, ws, SCENE_A, INPUT_A_POST_TAKE, COLOR_GREEN_ABGR)
                await assert_scene_item_presence(
                    inbox,
                    ws,
                    SCENE_A,
                    INPUT_A_POST_TAKE,
                    True,
                    "CreateInput(A after commit)",
                )
                # Keep the post-commit Preview mutation observable for at
                # least thirty rendered frames while logical Program remains
                # the promoted B scene. The raw NV12 time-code probe supplies
                # the pixel-level lane-isolation proof.
                settle_batch = await request_batch(
                    inbox,
                    ws,
                    "take-1-post-commit-settle",
                    [
                        {"requestType": "Sleep", "requestData": {"sleepFrames": 30}},
                        {"requestType": "GetCurrentProgramScene"},
                        {"requestType": "GetCurrentPreviewScene"},
                    ],
                )
                settle_results = batch_results(settle_batch, "take-1-post-commit-settle")
                if len(settle_results) != 3:
                    raise ProbeFailure(
                        f"take-1-post-commit-settle returned {len(settle_results)} results, expected 3"
                    )
                assert_success(settle_results[0], "Sleep(30 frames)")
                assert_success(settle_results[1], "GetCurrentProgramScene(after settle)")
                assert_success(settle_results[2], "GetCurrentPreviewScene(after settle)")
                await assert_distinct_selected_scenes(
                    inbox,
                    ws,
                    "take-1-post-commit-settle",
                    expected_program=SCENE_B,
                    expected_preview=SCENE_A,
                )
                await assert_scene_item_presence(
                    inbox,
                    ws,
                    SCENE_B,
                    INPUT_B_FROZEN,
                    False,
                    "frozen input after commit",
                )
                await assert_scene_item_presence(
                    inbox,
                    ws,
                    SCENE_A,
                    INPUT_A_POST_TAKE,
                    True,
                    "post-commit Preview after 30 frames",
                )

        # The native sampler runs on its own cadence.  For the dual-lane
        # capacity append, keep both outputs alive after the final Take until
        # the requested number of observed active NVENC+RTMP samples exists.
        # These records are resource evidence only and never enter Take
        # latency/event percentiles.
        if process.trace_path is not None and complete_boundary_count != total_takes:
            raise ProbeFailure(
                "trace campaign stopped before all Take boundary gates completed: "
                f"{complete_boundary_count} < {total_takes}"
            )
        if process.resource_mode == "dual_lane" and process.rtmp_receiver is not None:
            if minimum_resource_samples < 1:
                raise ProbeFailure(
                    "dual-lane RTMP capacity append requires a positive resource sample minimum"
                )
            await wait_for_eligible_resource_samples(
                process,
                "dual_lane",
                minimum_resource_samples,
                resource_sample_timeout,
            )

        if process.rtmp_receiver is not None:
            await process.stop_rtmp_stream(inbox, ws)

        response = await request(inbox, ws, "StopRecord", "stop-record")
        assert_success(response, "StopRecord")
        stopped = await wait_event(
            inbox,
            ws,
            "RecordStateChanged",
            lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED",
        )
        output_path = (stopped.get("eventData") or {}).get("outputPath") or ""
        if not output_path:
            raise ProbeFailure("RecordStateChanged STOPPED did not include outputPath")

        all_commits = [
            parse_commit(match)
            for line in process.snapshot()
            if (match := COMMIT_RE.search(line)) is not None
        ]
        if len(all_commits) != total_takes or [commit.count for commit in all_commits] != list(range(1, total_takes + 1)):
            raise ProbeFailure(
                f"expected exactly {total_takes} contiguous TakeCommitted logs, got "
                f"{[commit.count for commit in all_commits]}"
            )
        first_commit_index = next(
            index
            for index, line in enumerate(process.snapshot())
            if COMMIT_RE.search(line)
        )
        bind_index = next(
            index
            for index, line in enumerate(process.snapshot())
            if ENCODER_BIND_RE.search(line)
        )
        if bind_index >= first_commit_index:
            raise ProbeFailure("encoder video_t bind was not completed before the first Take")
        if process.trace_path is not None:
            process.assert_directshow_consumer_alive()
            wait_for_trace_record(
                process,
                "observation",
                f"take-{total_takes:03d}",
                boundary="directshow_return",
                timeout=15.0,
            )
        verify_recording(output_path, ffprobe)

    if warmup_takes:
        if len(commits) != total_takes:
            raise ProbeFailure(
                f"warm-up accounting mismatch: collected {len(commits)} total commits, "
                f"expected {total_takes} ({warmup_takes} warm + {takes} measured)"
            )
        print(
            f"   trace partition: warmup_takes={warmup_takes} "
            f"measured_takes={len(commits) - warmup_takes} total_takes={len(commits)}"
        )
        return commits[warmup_takes:]
    return commits


def validate_trace_append(
    trace_path: pathlib.Path,
    *,
    runtime_id: str,
    build_revision: str,
    trace_host: str,
    trace_gpu: str,
    require_rtmp_load: bool = False,
    minimum_rtmp_samples: int = 1,
) -> None:
    """Validate the NVENC reference trace before allowing an append.

    The second invocation intentionally reuses the JSONL file produced by the
    reference process.  Validate its identity and phase before spawning a new
    runtime: a mismatch must fail before the new process can truncate or
    append anything.
    """

    if not trace_path.is_file():
        raise ProbeFailure(f"--trace-append requires an existing trace file: {trace_path}")

    records: list[dict[str, Any]] = []
    try:
        with trace_path.open("r", encoding="utf-8") as handle:
            for line_number, text in enumerate(handle, start=1):
                if not text.strip():
                    continue
                try:
                    value = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ProbeFailure(
                        f"--trace-append found malformed JSON at line {line_number}: {exc.msg}"
                    ) from exc
                if not isinstance(value, dict):
                    raise ProbeFailure(f"--trace-append record at line {line_number} is not an object")
                record_type = value.get("record_type")
                if record_type not in {"session", "event", "observation", "resource_sample"}:
                    raise ProbeFailure(
                        f"--trace-append found unsupported record_type={record_type!r} "
                        f"at line {line_number}"
                    )
                records.append(value)
    except OSError as exc:
        raise ProbeFailure(f"--trace-append cannot read existing trace {trace_path}: {exc}") from exc

    if not records or records[0].get("record_type") != "session":
        raise ProbeFailure("--trace-append existing trace must begin with its session record")
    sessions = [record for record in records if record.get("record_type") == "session"]
    if len(sessions) != 1:
        raise ProbeFailure(
            f"--trace-append requires exactly one existing session record, found {len(sessions)}"
        )
    session = sessions[0]
    if session.get("codec") != "nvenc":
        raise ProbeFailure(
            f"--trace-append requires an existing NVENC reference session, got {session.get('codec')!r}"
        )
    if session.get("runtime_instance_id") != runtime_id:
        raise ProbeFailure("--trace-append runtime_instance_id does not match the reference session")
    if session.get("build_revision") != build_revision:
        raise ProbeFailure("--trace-append build_revision does not match the reference session")
    if session.get("hardware") != {"host": trace_host, "gpu": trace_gpu}:
        raise ProbeFailure("--trace-append hardware identity does not match the reference session")
    if session.get("producer_topology") != "single_lane_reference" or session.get("producer_count") != 1:
        raise ProbeFailure("--trace-append reference session is not single_lane_reference/producer_count=1")
    workload = session.get("workload")
    if not isinstance(workload, dict) or workload.get("nvenc") is not True:
        raise ProbeFailure("--trace-append reference session does not declare an NVENC workload")

    resource_samples = [record for record in records if record.get("record_type") == "resource_sample"]
    if any(record.get("record_type") in {"event", "observation"} for record in records):
        raise ProbeFailure(
            "--trace-append reference file contains Take/event observations; "
            "it cannot be used as the NVENC resource baseline"
        )
    reference_samples = [sample for sample in resource_samples if sample.get("sample_mode") == "reference"]
    if not resource_samples or len(reference_samples) != len(resource_samples):
        raise ProbeFailure(
            "--trace-append requires only the NVENC reference resource phase before dual-lane append"
        )
    for sample in reference_samples:
        if sample.get("runtime_instance_id") != runtime_id:
            raise ProbeFailure("--trace-append resource runtime_instance_id does not match the session")
        if sample.get("build_revision") != build_revision:
            raise ProbeFailure("--trace-append resource build_revision does not match the session")
        if sample.get("hardware") != {"host": trace_host, "gpu": trace_gpu}:
            raise ProbeFailure("--trace-append resource hardware identity does not match the session")
        if sample.get("producer_topology") != "single_lane_reference" or sample.get("producer_count") != 1:
            raise ProbeFailure("--trace-append resource reference topology is not single-lane")
        if "encoder_active" in sample and type(sample["encoder_active"]) is not bool:
            raise ProbeFailure("--trace-append resource encoder_active must be boolean")
        if "encoder_family" in sample and sample["encoder_family"] not in ("x264", "nvenc"):
            raise ProbeFailure("--trace-append resource encoder_family is invalid")
        if sample.get("encoder_active") is True and sample.get("encoder_family") != "nvenc":
            raise ProbeFailure(
                "--trace-append reference contains an active sample without the NVENC encoder identity"
            )
    if not any(
        sample.get("encoder_active") is True and sample.get("encoder_family") == "nvenc"
        for sample in reference_samples
    ):
        raise ProbeFailure(
            "--trace-append reference phase lacks an active NVENC encoder attestation"
        )
    if require_rtmp_load:
        if minimum_rtmp_samples < 1:
            raise ProbeFailure("--trace-append RTMP minimum must be positive")
        receiver = session.get("rtmp_receiver")
        if session.get("rtmp_load_requested") is not True or not isinstance(receiver, dict):
            raise ProbeFailure(
                "--trace-append RTMP mode requires reference rtmp_load_requested=true and receiver metadata"
            )
        required_receiver_fields = (
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
        )
        if any(field not in receiver for field in required_receiver_fields):
            raise ProbeFailure("--trace-append reference RTMP receiver metadata is incomplete")
        server_url = receiver["server_url"]
        stream_key = receiver["stream_key"]
        endpoint = receiver["endpoint"]
        if (
            not isinstance(server_url, str)
            or not (server_url.startswith("rtmp://127.0.0.1:") or server_url.startswith("rtmp://localhost:"))
            or not isinstance(stream_key, str)
            or ID_RE.fullmatch(stream_key) is None
            or not isinstance(endpoint, str)
            or endpoint != f"{server_url.rstrip('/')}/{stream_key}"
        ):
            raise ProbeFailure("--trace-append reference RTMP receiver endpoint is not an exact loopback server/key")
        if any(
            not isinstance(receiver[field], str) or ID_RE.fullmatch(receiver[field]) is None
            for field in ("receiver_id", "stream_id")
        ):
            raise ProbeFailure("--trace-append reference RTMP receiver identity is invalid")
        if receiver["clock_source"] not in ("perf_counter_ns/qpc", "qpc"):
            raise ProbeFailure("--trace-append reference RTMP receiver clock source is invalid")
        if (
            type(receiver["clock_offset_ns"]) is not int
            or type(receiver["clock_bound_ns"]) is not int
            or not 0 < receiver["clock_bound_ns"] <= WIRE_CLOCK_QPC_MAX_DELTA_NS
            or type(receiver["packet_timebase_num"]) is not int
            or type(receiver["packet_timebase_den"]) is not int
            or receiver["packet_timebase_num"] <= 0
            or receiver["packet_timebase_den"] <= 0
        ):
            raise ProbeFailure("--trace-append reference RTMP receiver clock/timebase is invalid")
        eligible_reference_samples = sum(
            sample.get("encoder_active") is True
            and sample.get("encoder_family") == "nvenc"
            and sample.get("rtmp_load_active") is True
            for sample in reference_samples
        )
        if eligible_reference_samples < minimum_rtmp_samples:
            raise ProbeFailure(
                "--trace-append reference phase lacks enough active NVENC samples under observed RTMP load: "
                f"{eligible_reference_samples} < {minimum_rtmp_samples}"
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    parser.add_argument("--encoder", choices=("x264", "nvenc"), required=True)
    parser.add_argument(
        "--takes",
        type=int,
        default=100,
        help="number of measured Takes; traced runs prepend 100 warm-up Takes",
    )
    parser.add_argument(
        "--trace",
        type=pathlib.Path,
        help="opt-in #246 JSONL trace path; enables runtime event/raw/encoded-output producer hooks",
    )
    parser.add_argument(
        "--build-revision",
        default=os.environ.get("PULSAR_BUILD_REVISION"),
        help="exact 40-character lowercase candidate SHA stamped into a --trace session (or PULSAR_BUILD_REVISION)",
    )
    parser.add_argument("--runtime-id", help="runtime_instance_id for --trace (default: generated)")
    parser.add_argument(
        "--trace-host",
        default=os.environ.get("PULSAR_TRACE_HOST"),
        help="exact host label stamped into runtime resource samples (or PULSAR_TRACE_HOST)",
    )
    parser.add_argument(
        "--trace-gpu",
        default=os.environ.get("PULSAR_TRACE_GPU"),
        help="exact GPU adapter label stamped into runtime resource samples (or PULSAR_TRACE_GPU)",
    )
    parser.add_argument(
        "--resource-mode",
        choices=("reference", "dual_lane"),
        help="enable NVENC-only native resource samples; reference is a single-canvas run",
    )
    parser.add_argument(
        "--resource-only",
        action="store_true",
        help="collect native resource samples without driving scene-switch Takes",
    )
    parser.add_argument(
        "--resource-samples",
        type=int,
        default=10,
        help="minimum native resource samples for --resource-only (default: 10)",
    )
    parser.add_argument(
        "--resource-interval-ms",
        type=int,
        default=500,
        help="native resource sample interval, 100..10000 ms (default: 500)",
    )
    parser.add_argument(
        "--trace-append",
        action="store_true",
        help="append NVENC dual-lane evidence after a validated NVENC reference trace",
    )
    parser.add_argument(
        "--capture-window",
        help="visible WGC window descriptor (<title>:<class>:<exe>); required with --cef-workload",
    )
    parser.add_argument(
        "--cef-workload",
        action="store_true",
        help="create and bind a real browser_source CEF workload alongside window_capture",
    )
    parser.add_argument(
        "--rtmp-receiver",
        action="store_true",
        help="start a real FFmpeg loopback RTMP receiver and fuse correlated AC-12 evidence after shutdown",
    )
    parser.add_argument(
        "--cef-url",
        default=os.environ.get("PULSAR_CEF_URL"),
        help="URL for the --cef-workload browser_source (or PULSAR_CEF_URL; default is an ephemeral local page)",
    )
    args = parser.parse_args(argv)
    if args.takes < 1:
        parser.error("--takes must be >= 1")
    if args.runtime_id and args.trace is None:
        parser.error("--runtime-id requires --trace")
    if args.resource_mode and args.trace is None:
        parser.error("--resource-mode requires --trace")
    if args.resource_mode and args.encoder != "nvenc":
        parser.error("--resource-mode is supported only with --encoder nvenc")
    if args.resource_only and not args.resource_mode:
        parser.error("--resource-only requires --resource-mode")
    if args.trace_append and args.trace is None:
        parser.error("--trace-append requires --trace")
    if args.trace_append and args.encoder != "nvenc":
        parser.error("--trace-append is supported only with --encoder nvenc")
    if args.trace_append and not args.runtime_id:
        parser.error("--trace-append requires --runtime-id matching the reference session")
    if args.trace_append and args.resource_mode != "dual_lane":
        parser.error("--trace-append requires --resource-mode dual_lane")
    if args.trace_append and not args.rtmp_receiver:
        parser.error("--trace-append requires --rtmp-receiver for AC-13 evidence")
    if args.rtmp_receiver and args.trace is None:
        parser.error("--rtmp-receiver requires --trace")
    if args.rtmp_receiver and args.resource_only and (
        args.encoder != "nvenc" or args.resource_mode != "reference" or args.trace_append
    ):
        parser.error(
            "--rtmp-receiver --resource-only requires --encoder nvenc --resource-mode reference "
            "and cannot be combined with --trace-append"
        )
    if args.trace is not None and (
        not args.build_revision or BUILD_REVISION_RE.fullmatch(args.build_revision) is None
    ):
        parser.error("--trace requires --build-revision to be the exact 40-character lowercase candidate SHA")
    if args.cef_workload and not args.capture_window:
        parser.error("--cef-workload requires --capture-window for a visible WGC target")
    if args.trace is not None and (not args.capture_window or not args.cef_workload):
        parser.error("--trace requires --capture-window and --cef-workload for external A/B producer evidence")
    if args.resource_samples < 1:
        parser.error("--resource-samples must be >= 1")
    if not 100 <= args.resource_interval_ms <= 10000:
        parser.error("--resource-interval-ms must be between 100 and 10000")
    return args


def run(args: argparse.Namespace) -> int:
    if not args.exe.is_file():
        print(f"SKIP: Pulsar binary not found: {args.exe}")
        return EXIT_SKIP

    with tempfile.TemporaryDirectory(prefix="pulsar-dual-lane-") as record_dir_text:
        final_trace_path = args.trace.resolve() if args.trace is not None else None
        # A receiver must never append to a producer JSONL while the runtime
        # is alive.  Give the runtime a unique producer-only path and create
        # the final fused artifact only after both child processes have exited.
        trace_path = final_trace_path
        producer_trace_path = None
        if final_trace_path is not None and args.rtmp_receiver:
            producer_trace_path = final_trace_path.with_name(
                f".{final_trace_path.name}.{os.getpid()}.producer.jsonl"
            )
            trace_path = producer_trace_path
        if trace_path is not None:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_host = trace_gpu = None
        if trace_path is not None:
            trace_host, trace_gpu = resolve_trace_hardware(args.trace_host, args.trace_gpu)
        rtmp_ffmpeg = find_ffmpeg(args.exe.parent) if args.rtmp_receiver else None
        if args.rtmp_receiver and rtmp_ffmpeg is None:
            print("SKIP: ffmpeg is required for the RTMP receiver boundary")
            return EXIT_SKIP
        cef_server = None
        if args.cef_workload and not args.cef_url:
            cef_server = DeterministicCefServer()
            cef_server.start()
        cef_url = args.cef_url or (cef_server.url if cef_server is not None else None)
        process = PulsarProcess(
            args.exe.resolve(),
            args.encoder,
            pathlib.Path(record_dir_text),
            trace_path,
            args.runtime_id,
            args.resource_mode,
            args.trace_append,
            args.resource_interval_ms,
            args.capture_window,
            args.cef_workload,
            args.build_revision,
            cef_url,
            trace_host,
            trace_gpu,
        )
        if args.rtmp_receiver:
            if rtmp_ffmpeg is None:
                print("SKIP: ffmpeg is required for the RTMP receiver boundary")
                return EXIT_SKIP
            process.rtmp_receiver = RtmpReceiver(
                rtmp_ffmpeg,
                runtime_id=process.runtime_id,
                stream_id=_stream_id_for_runtime(process.runtime_id, args.encoder),
            )
            process.rtmp_producer_trace_path = producer_trace_path
            process.rtmp_final_trace_path = final_trace_path
        result = EXIT_FAIL
        cleanup_failure: ProbeFailure | None = None
        failure_message: str | None = None
        skip_message: str | None = None
        try:
            if args.trace_append:
                if final_trace_path is None or not final_trace_path.is_file():
                    raise ProbeFailure(
                        "--trace-append requires the existing reference trace at the final --trace path"
                    )
                if (
                    args.runtime_id is None
                    or args.build_revision is None
                    or trace_host is None
                    or trace_gpu is None
                ):
                    raise ProbeFailure("--trace-append preflight metadata is incomplete")
                # Check the operator-supplied reference first.  In the RTMP
                # variant the producer copy is deliberately made only after
                # this validation, preserving the reference on every failure.
                validate_trace_append(
                    final_trace_path,
                    runtime_id=args.runtime_id,
                    build_revision=args.build_revision,
                    trace_host=trace_host,
                    trace_gpu=trace_gpu,
                    require_rtmp_load=args.rtmp_receiver,
                    minimum_rtmp_samples=args.resource_samples,
                )
                if args.rtmp_receiver:
                    # Validate and copy the immutable reference before any
                    # runtime starts.  The final path is only replaced after
                    # successful receiver fusion, so a failed dual run leaves
                    # the known-good reference untouched.
                    if producer_trace_path is None:
                        raise ProbeFailure("RTMP append producer path was not configured")
                    shutil.copyfile(final_trace_path, producer_trace_path)
                    trace_path = producer_trace_path
                if args.rtmp_receiver:
                    if trace_path is None:
                        raise ProbeFailure("RTMP append producer path was not configured")
                    validate_trace_append(
                        trace_path,
                        runtime_id=args.runtime_id,
                        build_revision=args.build_revision,
                        trace_host=trace_host,
                        trace_gpu=trace_gpu,
                        require_rtmp_load=args.rtmp_receiver,
                        minimum_rtmp_samples=args.resource_samples,
                    )
            if trace_path is not None:
                calibration = calibrate_wire_clock()
                print(
                    "   wire clock preflight: "
                    f"source={calibration['source']} "
                    f"wire_now_ns={calibration['wire_now_ns']} "
                    f"qpc_now_ns={calibration['qpc_now_ns']} "
                    f"qpc_delta_ns={calibration['qpc_delta_ns']} "
                    f"bound_ns={WIRE_CLOCK_QPC_MAX_DELTA_NS}"
                )
            spawn_after_rtmp_ready(process)
            warmup_takes = TRACE_WARMUP_TAKES if trace_path is not None else 0
            print(
                f"dual-lane probe: encoder={args.encoder} takes={args.takes} "
                f"warmup_takes={warmup_takes} total_takes={args.takes + warmup_takes} exe={args.exe}"
                + (f" trace={trace_path}" if trace_path is not None else "")
                + (f" resource_mode={args.resource_mode}" if args.resource_mode else "")
            )
            if args.resource_only:
                count = asyncio.run(
                    collect_resource_samples(
                        process,
                        args.resource_mode,
                        args.resource_samples,
                        timeout=max(30.0, args.resource_samples * args.resource_interval_ms / 1000.0 + 15.0),
                    )
                )
                print(f"PASS: collected {count} native {args.resource_mode} resource samples")
                result = 0
            else:
                commits = asyncio.run(
                    drive(
                        process,
                        args.takes,
                        warmup_takes=warmup_takes,
                        minimum_resource_samples=args.resource_samples,
                        resource_sample_timeout=max(
                            30.0,
                            args.resource_samples * args.resource_interval_ms / 1000.0 + 15.0,
                        ),
                    )
                )
                print(
                    f"PASS: {len(commits)} Takes; computed lane/surface relations remained valid; "
                    "frame_id/PTS monotone"
                )
                result = 0
        except ProbeSkip as exc:
            skip_message = str(exc)
            result = EXIT_SKIP
        except (ProbeFailure, asyncio.TimeoutError, OSError, json.JSONDecodeError) as exc:
            failure_message = str(exc)
            result = EXIT_FAIL
        finally:
            try:
                process.shutdown()
            except ProbeFailure as exc:
                cleanup_failure = exc
            if cef_server is not None:
                cef_server.close()
        diagnostic_path: pathlib.Path | None = None
        diagnostic_failure: str | None = None
        if result != 0 and process.rtmp_receiver is not None and producer_trace_path is not None:
            try:
                diagnostic_path = process.rtmp_receiver.persist_diagnostics(producer_trace_path)
            except Exception as exc:
                diagnostic_failure = str(exc)
        # Emit the primary diagnostic only after every owned child and reader
        # has traversed the bounded cleanup path.  This ordering prevents a
        # strict stderr supervisor from interrupting cleanup on the first FAIL
        # line and orphaning Pulsar or FFmpeg.
        if skip_message is not None:
            print(f"SKIP: {skip_message}")
        if failure_message is not None:
            print(f"FAIL: {failure_message}", file=sys.stderr)
        if result != 0 and producer_trace_path is not None:
            print(
                f"producer trace sidecar preserved for diagnosis: {producer_trace_path}",
                file=sys.stderr,
            )
        if diagnostic_path is not None:
            print(f"RTMP receiver diagnostic preserved: {diagnostic_path}", file=sys.stderr)
        if diagnostic_failure is not None:
            print(f"FAIL: RTMP receiver diagnostic persistence failed: {diagnostic_failure}", file=sys.stderr)
        if cleanup_failure is not None:
            print(f"FAIL: cleanup did not release all owned resources: {cleanup_failure}", file=sys.stderr)
            if producer_trace_path is not None:
                print(
                    f"producer trace sidecar preserved for diagnosis: {producer_trace_path}",
                    file=sys.stderr,
                )
            return EXIT_FAIL
        if result == 0:
            try:
                process.assert_shutdown_clean(require_runtime_lease=trace_path is not None)
                if args.rtmp_receiver:
                    try:
                        process.finalize_rtmp_trace(
                            resource_only=args.resource_only,
                            minimum_samples=args.resource_samples,
                        )
                    except ProbeFailure as fusion_exc:
                        receiver_diagnostic: pathlib.Path | None = None
                        receiver_diagnostic_failure: str | None = None
                        if process.rtmp_receiver is not None and producer_trace_path is not None:
                            try:
                                receiver_diagnostic = process.rtmp_receiver.persist_diagnostics(
                                    producer_trace_path
                                )
                            except Exception as diagnostic_exc:
                                receiver_diagnostic_failure = str(diagnostic_exc)
                        if producer_trace_path is not None:
                            print(
                                f"RTMP fusion failed; producer sidecar preserved for diagnosis: {producer_trace_path}",
                                file=sys.stderr,
                            )
                        if receiver_diagnostic is not None:
                            print(
                                f"RTMP fusion receiver diagnostic preserved: {receiver_diagnostic}",
                                file=sys.stderr,
                            )
                        if receiver_diagnostic_failure is not None:
                            raise ProbeFailure(
                                f"{fusion_exc}; RTMP receiver diagnostic persistence failed: "
                                f"{receiver_diagnostic_failure}"
                            ) from fusion_exc
                        raise
                    if producer_trace_path is not None:
                        print(
                            f"RTMP fused trace committed atomically; producer sidecar preserved: {producer_trace_path}"
                        )
            except ProbeFailure as exc:
                print(f"FAIL: cleanup verification failed: {exc}", file=sys.stderr)
                return EXIT_FAIL
        return result


def spawn_after_rtmp_ready(process: PulsarProcess) -> None:
    """Make receiver readiness precede any Pulsar spawn metadata access."""

    if process.rtmp_receiver is not None:
        # Start and calibrate FFmpeg before Pulsar reads receiver metadata or
        # publishes its first stream packet.  This makes listener readiness an
        # explicit lifecycle precondition and ensures spawn failures still
        # enter the normal cleanup path.
        process.start_rtmp_consumer()
    process.spawn()


def main() -> int:
    try:
        args = parse_args(sys.argv[1:])
    except SystemExit as exc:
        return int(exc.code)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
