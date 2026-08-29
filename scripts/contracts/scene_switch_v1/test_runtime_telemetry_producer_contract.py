"""Static contract guards for the #246 runtime telemetry producer.

The upstream producer lives in the pinned submodule and is materialized by the
numbered patch chain.  These checks keep the superproject wiring and the
canonical 0010 patch visible to the scene-switch contract suite without
requiring a Windows/OBS toolchain.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys
import struct
import urllib.request
import zlib

import pytest


ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "patches" / "0010-feat-runtime-telemetry-producer.patch"
FRONTEND_CMAKE = ROOT / "plugins" / "pulsar-frontend-stub" / "CMakeLists.txt"
WEBSOCKET_CMAKE = ROOT / "plugins" / "pulsar-websocket" / "CMakeLists.txt"
FRONTEND_SOURCE = ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp"
WEBSOCKET_SOURCE = ROOT / "plugins" / "pulsar-websocket" / "src" / "requesthandler" / "RequestHandler.cpp"
ABI_HEADER = ROOT / "plugins" / "pulsar-frontend-stub" / "include" / "pulsar-runtime-telemetry-abi.h"
BRIDGE_HEADER = ROOT / "plugins" / "pulsar-frontend-stub" / "include" / "pulsar-runtime-telemetry.h"
DUAL_LANE_PROBE = ROOT / "scripts" / "probe-dual-lane.py"
TAKE_LATENCY_PROBE = ROOT / "scripts" / "probe-take-latency.py"
UPSTREAM_QUEUE_CMAKE = ROOT / "upstream" / "shared" / "obs-shared-memory-queue" / "CMakeLists.txt"
UPSTREAM_VCAM_CMAKE = ROOT / "upstream" / "plugins" / "win-dshow" / "virtualcam-module" / "CMakeLists.txt"


def test_canonical_runtime_producer_patch_is_present_and_scoped() -> None:
    text = PATCH.read_text(encoding="utf-8")
    assert text.startswith("From ")
    assert "Subject: [PATCH] feat(telemetry): emit correlated runtime trace boundaries" in text
    assert "5 files changed, 246 insertions(+), 22 deletions(-)" in text
    assert "Agent-Role: Conduit" in text
    assert "Agent-Thread: /root/conduit_246_runtime_telemetry" in text
    assert "Work-Unit: ZabLaboratory/Pulsar#246" in text
    assert "Issue: 246" in text

    paths = set(re.findall(r"^diff --git a/([^ ]+) b/[^\n]+$", text, flags=re.MULTILINE))
    assert paths == {
        "plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp",
        "plugins/win-dshow/virtualcam-module/virtualcam-filter.hpp",
        "plugins/win-dshow/virtualcam.c",
        "shared/obs-shared-memory-queue/shared-memory-queue.c",
        "shared/obs-shared-memory-queue/shared-memory-queue.h",
    }

    # The modern CMake target owns the shared queue implementation.  The patch
    # must extend that target and make virtualcam.c consume the same public
    # header; changing the duplicate legacy copy would compile no code here.
    assert "-#include \"shared-memory-queue.h\"" in text
    assert "+#include \"../../shared/obs-shared-memory-queue/shared-memory-queue.h\"" in text
    assert "struct video_queue_frame_metadata" in text
    assert "video_queue_read_ex" in text
    assert "video_queue_write_ex" in text
    assert UPSTREAM_QUEUE_CMAKE.is_file()
    assert UPSTREAM_VCAM_CMAKE.is_file()
    queue_cmake = UPSTREAM_QUEUE_CMAKE.read_text(encoding="utf-8")
    vcam_cmake = UPSTREAM_VCAM_CMAKE.read_text(encoding="utf-8")
    assert "target_sources(obs-shared-memory-queue INTERFACE shared-memory-queue.c shared-memory-queue.h)" in queue_cmake
    assert "target_include_directories(obs-shared-memory-queue INTERFACE \"${CMAKE_CURRENT_SOURCE_DIR}\")" in queue_cmake
    assert "OBS::shared-memory-queue" in vcam_cmake


def test_runtime_telemetry_headers_and_cmake_include_paths_are_wired() -> None:
    assert ABI_HEADER.is_file()
    assert BRIDGE_HEADER.is_file()

    frontend_cmake = FRONTEND_CMAKE.read_text(encoding="utf-8")
    websocket_cmake = WEBSOCKET_CMAKE.read_text(encoding="utf-8")
    assert '"${PROJECT_SOURCE_DIR}/include"' in frontend_cmake
    assert '"${PROJECT_SOURCE_DIR}/../pulsar-frontend-stub/include"' in websocket_cmake

    abi = ABI_HEADER.read_text(encoding="utf-8")
    bridge = BRIDGE_HEADER.read_text(encoding="utf-8")
    assert "PULSAR_RUNTIME_TELEMETRY_BEGIN_PROC" in abi
    assert "PULSAR_RUNTIME_TELEMETRY_SNAPSHOT_PROC" in abi
    assert "struct pulsar_runtime_frame_metadata" in abi
    assert "inline bool begin_take" in bridge
    assert "struct BeginTakeStatus" in bridge
    assert "begin_take_status" in bridge
    assert "inline bool snapshot_frame" in bridge


def test_runtime_producer_consumers_preserve_distinct_boundaries() -> None:
    frontend = FRONTEND_SOURCE.read_text(encoding="utf-8")
    websocket = WEBSOCKET_SOURCE.read_text(encoding="utf-8")
    patch = PATCH.read_text(encoding="utf-8")

    assert "pulsar_runtime_telemetry_begin_take" in frontend
    assert "pulsar_runtime_telemetry_cancel_take" in frontend
    assert "#ifdef _WIN32\n#include <windows.h>\n#endif" in frontend
    assert '\\"boundary\\":\\"encoder_input_raw\\"' in frontend
    assert '\\"boundary\\":\\"rtmp_first_packet\\"' in frontend
    assert "obs_add_raw_video_callback" in frontend
    assert "obs_output_add_packet_callback" in frontend
    assert 'obs_source_create("browser_source", "PulsarCefWorkload"' in frontend
    assert "PULSAR_CEF_URL" in frontend
    assert '\\"source_types\\":[' in frontend
    queue_start = frontend.index("bool PulsarFrontendAPI::queueDualLaneCut")
    queue = frontend[queue_start:]
    assert queue.index("g_runtimeTelemetry.accept") < queue.index("obs_view_queue_atomic_swap")
    assert 'g_runtimeTelemetry.cancelAccepted("atomic_swap_rejected")' in queue

    assert "BeginRuntimeTakeTelemetry" in websocket
    assert "pulsar_runtime_telemetry::begin_take" in websocket
    assert "pulsar_runtime_telemetry::cancel_take" in websocket
    assert "freeze_until_monotonic_ns" in websocket
    assert "called=%d available=%d accepted=%d" in websocket

    # 0010 must record the observation after the DirectShow sample has been
    # unlocked, and only for the actual ProgramReturn filter instance.
    assert "if (consumed_program_frame && program_return)" in patch
    assert "after UnlockSampleData" in patch
    assert '"boundary\\":\\"directshow_return' in patch
    assert "video_queue_read_ex" in patch
    assert "video_queue_write_ex" in patch


def test_runtime_session_line_is_valid_json_and_has_bound_source_topology() -> None:
    # This is the exact shape emitted by sessionJson(), including the fields
    # that caught the prior missing-quote regression and prevent workload flags
    # from standing in for actual source registration.
    line = (
        '{\"record_type\":\"session\",\"schema\":\"pulsar.take-latency.v1\",'
        '\"runtime_instance_id\":\"runtime-nvenc-001\",\"session_id\":\"runtime-nvenc-001-nvenc\",'
        '\"codec\":\"nvenc\",\"warmup_takes\":100,'
        '\"video\":{\"width\":1920,\"height\":1080,\"fps_num\":60,\"fps_den\":1},'
        '\"workload\":{\"wgc\":true,\"cef\":true,\"nvenc\":true},'
        '\"capture_paths\":[\"encoder_input_raw\",\"directshow_return\",\"rtmp_first_packet\",'
        '\"decoded_first_frame\",\"antenna_first_frame\"],'
        '\"source_types\":[\"window_capture\",\"browser_source\"],'
        '\"resource_reference\":{\"extra_frame_render_ms\":0.091,\"extra_resident_bytes\":3130000},'
        '\"build_revision\":\"0123456789abcdef0123456789abcdef01234567\",'
        '\"command_line\":\"scripts/probe-dual-lane.py --trace\",'
        '\"hardware\":{\"host\":\"test-host\",\"gpu\":\"adapter \\\"246\\\"\"},'
        '\"evidence_kind\":\"runtime\"}'
    )
    session = json.loads(line)
    spec = importlib.util.spec_from_file_location("probe_take_latency_contract", TAKE_LATENCY_PROBE)
    assert spec is not None and spec.loader is not None
    parser = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = parser
    spec.loader.exec_module(parser)
    validated = parser._validate_session(session)
    assert validated["source_types"] == ["window_capture", "browser_source"]
    assert validated["build_revision"] == "0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(parser.EvidenceError, match="build_revision"):
        parser._validate_session({**session, "build_revision": "local-build"})

    with pytest.raises(json.JSONDecodeError):
        gpu_value = r'"gpu":"adapter \"246\""'
        assert gpu_value in line
        malformed = line.replace(gpu_value, gpu_value[:-1], 1)
        json.loads(malformed)


def test_runtime_driver_requires_real_wgc_and_local_cef_evidence() -> None:
    driver = DUAL_LANE_PROBE.read_text(encoding="utf-8")
    assert "class DeterministicCefServer" in driver
    assert 'CAPTURE_SOURCE_NAME = "PulsarCapture"' in driver
    assert 'CEF_SOURCE_NAME = "PulsarCefWorkload"' in driver
    assert '\"GetInputList\"' in driver
    assert '\"GetInputSettings\"' in driver
    assert '\"GetSceneItemList\"' in driver
    assert '\"GetSourceScreenshot\"' in driver
    assert "frame_is_nonblack" in driver
    assert "--cef-workload requires --capture-window" in driver
    assert "BUILD_REVISION_RE.fullmatch" in driver
    assert "wait_for_trace_record" in driver


def test_driver_loopback_cef_and_frame_gate_are_executable() -> None:
    spec = importlib.util.spec_from_file_location("probe_dual_lane_contract", DUAL_LANE_PROBE)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = probe
    spec.loader.exec_module(probe)

    server = probe.DeterministicCefServer()
    server.start()
    try:
        with urllib.request.urlopen(server.url, timeout=2) as response:
            body = response.read()
        assert body == probe.CEF_PAGE_HTML
    finally:
        server.close()

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    colours = bytes(
        [
            255, 0, 0,
            0, 255, 0,
            0, 0, 255,
            255, 255, 0,
            255, 0, 255,
            0, 255, 255,
            32, 64, 96,
            96, 64, 32,
            220, 220, 220,
        ]
    )
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 3, 3, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + colours[:9] + b"\x00" + colours[9:18] + b"\x00" + colours[18:]))
        + chunk(b"IEND", b"")
    )
    width, height, channels, pixels = probe.decode_png(png)
    metrics = probe.analyse_frame(width, height, channels, pixels)
    assert (width, height, channels) == (3, 3, 3)
    assert probe.frame_is_nonblack(metrics, require_variance=True)
