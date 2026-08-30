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

Run the two acceptance campaigns independently against the same build::

    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder x264 --takes 100
    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder nvenc --takes 100
    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder nvenc --takes 100 \
        --trace artifacts/246/nvenc.jsonl --runtime-id runtime-nvenc-001 \
        --build-revision <candidate-sha> --capture-window <visible-title:class:exe> \
        --cef-workload
    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder nvenc \
        --trace artifacts/246/nvenc.jsonl --runtime-id runtime-nvenc-001 \
        --build-revision <candidate-sha> --capture-window <visible-title:class:exe> \
        --cef-workload --resource-mode reference --resource-only
    python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder nvenc --takes 100 \
        --trace artifacts/246/nvenc.jsonl --runtime-id runtime-nvenc-001 \
        --build-revision <candidate-sha> --capture-window <visible-title:class:exe> \
        --cef-workload --trace-append --resource-mode dual_lane

The x264 trace is a latency-only campaign (AC-13 is not applicable); the
NVENC trace's reference phase must run with ``--resource-mode reference
--resource-only`` before the dual-lane append above.  The reference phase
starts and verifies a real recording so its resource samples attest an active
encoder rather than only a requested codec.

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
ProgramReturn producer for an independent DirectShow consumer.  ``--resource-mode`` enables
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
import hashlib
import http.server
import json
import os
import pathlib
import re
import secrets
import signal
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any

try:
    import websockets
except ImportError:
    print("error: pip install websockets (pure WebSocket client)", file=sys.stderr)
    raise SystemExit(2)


EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_SKIP = 3

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
        self.lines: list[str] = []
        self.condition = threading.Condition()
        self.thread: threading.Thread | None = None

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

        # Keep the child in its own Windows console process group so an
        # operational rollback probe can request Ctrl+Break and observe the
        # runtime's graceful lease release before falling back to termination.
        creationflags = (0x08000000 | 0x00000200) if os.name == "nt" else 0
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
        )
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
        pulsar_failure: str | None = None
        if self.proc is not None and self.proc.poll() is None:
            try:
                if os.name == "nt":
                    # The headless child owns a process group and releases
                    # its runtime/DirectShow leases from the graceful Ctrl+
                    # Break path. Hard termination is only the fallback.
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.proc.terminate()
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=8)
                except Exception as exc:
                    pulsar_failure = f"Pulsar process could not be killed during cleanup: {exc}"
            except Exception as exc:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    try:
                        self.proc.kill()
                        self.proc.wait(timeout=8)
                    except Exception as fallback_exc:
                        pulsar_failure = (
                            "Pulsar process termination failed during cleanup: "
                            f"graceful={exc}; fallback={fallback_exc}"
                        )
                except Exception as fallback_exc:
                    pulsar_failure = (
                        "Pulsar process termination failed during cleanup: "
                        f"graceful={exc}; fallback={fallback_exc}"
                    )
        if self.proc is not None and self.proc.poll() is None:
            pulsar_failure = pulsar_failure or "Pulsar process remained alive after cleanup"
        reader_failure = self._join_process_reader()
        if reader_failure is not None:
            pulsar_failure = pulsar_failure or reader_failure
        if directshow_failure is not None:
            if pulsar_failure:
                raise ProbeFailure(f"{directshow_failure}; {pulsar_failure}")
            raise directshow_failure
        if pulsar_failure:
            raise ProbeFailure(pulsar_failure)

    def assert_shutdown_clean(self, *, require_runtime_lease: bool = False) -> None:
        """Fail a campaign if an owned process, reader, or lease survived."""

        if self.directshow_cleanup_failure is not None:
            raise ProbeFailure(self.directshow_cleanup_failure)
        if self.directshow_proc is not None and self.directshow_proc.poll() is None:
            raise ProbeFailure("ProgramReturn DirectShow consumer is still alive after shutdown")
        if self.directshow_thread is not None and self.directshow_thread.is_alive():
            raise ProbeFailure("ProgramReturn DirectShow reader thread is still alive after shutdown")
        if self.proc is not None and self.proc.poll() is None:
            raise ProbeFailure("Pulsar process is still alive after shutdown")
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

    ready_match = process.wait_for(READY_RE, timeout=60)
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
            deadline = time.monotonic() + timeout
            count = 0
            active_count = 0
            while True:
                if process.trace_path is None:
                    raise ProbeFailure("resource sampling requires --trace")
                count = 0
                active_count = 0
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
                except FileNotFoundError:
                    count = 0
                    active_count = 0
                if active_count >= minimum_samples:
                    break
                if time.monotonic() >= deadline:
                    if process.proc is not None and process.proc.poll() is None:
                        raise ProbeSkip(
                            "native resource sampler produced no complete active-encoder samples; "
                            "verify nvidia-smi, platform counters and the recording output on this host"
                        )
                    raise ProbeFailure(
                        f"runtime exited before collecting {minimum_samples} active {mode} resource samples"
                    )
                await asyncio.sleep(0.25)

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
            f"   active encoder resource samples verified: total={count} active={active_count} "
            f"mode={mode}"
        )
        return active_count


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


async def drive(process: PulsarProcess, takes: int, *, warmup_takes: int = 0) -> list[Commit]:
    if takes < 1 or warmup_takes < 0:
        raise ProbeFailure("takes must be positive and warmup_takes must not be negative")
    total_takes = warmup_takes + takes
    ready_match = process.wait_for(READY_RE, timeout=60)
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

        commits: list[Commit] = []
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
            if number == 1 and process.trace_path is not None:
                wait_for_trace_record(
                    process,
                    "event",
                    f"take-{number:03d}",
                    event_type="TakeAccepted",
                    timeout=10.0,
                )
            commit = parse_commit(process.wait_for_commit(number, timeout=15))
            if number == 1 and process.trace_path is not None:
                wait_for_trace_record(
                    process,
                    "event",
                    f"take-{number:03d}",
                    event_type="TakeCommitted",
                    timeout=10.0,
                )
                wait_for_trace_record(
                    process,
                    "observation",
                    f"take-{number:03d}",
                    boundary="encoder_input_raw",
                    timeout=10.0,
                )
                wait_for_trace_record(
                    process,
                    "observation",
                    f"take-{number:03d}",
                    boundary="directshow_return",
                    timeout=15.0,
                )
            validate_commit(identity, commits[-1] if commits else None, commit)
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
        help="enable native resource samples in this mode; reference is a single-canvas run",
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
        help="append to an existing runtime trace (used for reference+dual_lane campaigns)",
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
    if args.resource_only and not args.resource_mode:
        parser.error("--resource-only requires --resource-mode")
    if args.trace_append and args.trace is None:
        parser.error("--trace-append requires --trace")
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
        trace_path = args.trace.resolve() if args.trace is not None else None
        if trace_path is not None:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_host = trace_gpu = None
        if trace_path is not None:
            trace_host, trace_gpu = resolve_trace_hardware(args.trace_host, args.trace_gpu)
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
        result = EXIT_FAIL
        cleanup_failure: ProbeFailure | None = None
        try:
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
            process.spawn()
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
                commits = asyncio.run(drive(process, args.takes, warmup_takes=warmup_takes))
                print(
                    f"PASS: {len(commits)} Takes; computed lane/surface relations remained valid; "
                    "frame_id/PTS monotone"
                )
                result = 0
        except ProbeSkip as exc:
            print(f"SKIP: {exc}")
            result = EXIT_SKIP
        except (ProbeFailure, asyncio.TimeoutError, OSError, json.JSONDecodeError) as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            result = EXIT_FAIL
        finally:
            try:
                process.shutdown()
            except ProbeFailure as exc:
                cleanup_failure = exc
            if cef_server is not None:
                cef_server.close()
        if cleanup_failure is not None:
            print(f"FAIL: cleanup did not release all owned resources: {cleanup_failure}", file=sys.stderr)
            return EXIT_FAIL
        if result == 0:
            try:
                process.assert_shutdown_clean(require_runtime_lease=trace_path is not None)
            except ProbeFailure as exc:
                print(f"FAIL: cleanup verification failed: {exc}", file=sys.stderr)
                return EXIT_FAIL
        return result


def main() -> int:
    try:
        args = parse_args(sys.argv[1:])
    except SystemExit as exc:
        return int(exc.code)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
