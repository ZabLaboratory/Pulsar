"""Static contract guards for the #246 runtime telemetry producer.

The upstream producer lives in the pinned submodule and is materialized by the
numbered patch chain.  These checks keep the superproject wiring and the
canonical 0010 patch visible to the scene-switch contract suite without
requiring a Windows/OBS toolchain.
"""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "patches" / "0010-feat-runtime-telemetry-producer.patch"
FRONTEND_CMAKE = ROOT / "plugins" / "pulsar-frontend-stub" / "CMakeLists.txt"
WEBSOCKET_CMAKE = ROOT / "plugins" / "pulsar-websocket" / "CMakeLists.txt"
FRONTEND_SOURCE = ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp"
WEBSOCKET_SOURCE = ROOT / "plugins" / "pulsar-websocket" / "src" / "requesthandler" / "RequestHandler.cpp"
ABI_HEADER = ROOT / "plugins" / "pulsar-frontend-stub" / "include" / "pulsar-runtime-telemetry-abi.h"
BRIDGE_HEADER = ROOT / "plugins" / "pulsar-frontend-stub" / "include" / "pulsar-runtime-telemetry.h"
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
    assert "+#include <shared-memory-queue.h>" in text
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
    assert "inline bool snapshot_frame" in bridge


def test_runtime_producer_consumers_preserve_distinct_boundaries() -> None:
    frontend = FRONTEND_SOURCE.read_text(encoding="utf-8")
    websocket = WEBSOCKET_SOURCE.read_text(encoding="utf-8")
    patch = PATCH.read_text(encoding="utf-8")

    assert "pulsar_runtime_telemetry_begin_take" in frontend
    assert "pulsar_runtime_telemetry_cancel_take" in frontend
    assert '\\"boundary\\":\\"encoder_input_raw\\"' in frontend
    assert '\\"boundary\\":\\"rtmp_first_packet\\"' in frontend
    assert "obs_add_raw_video_callback" in frontend
    assert "obs_output_add_packet_callback" in frontend

    assert "BeginRuntimeTakeTelemetry" in websocket
    assert "pulsar_runtime_telemetry::begin_take" in websocket
    assert "pulsar_runtime_telemetry::cancel_take" in websocket
    assert "freeze_until_monotonic_ns" in websocket

    # 0010 must record the observation after the DirectShow sample has been
    # unlocked, and only for the actual ProgramReturn filter instance.
    assert "if (consumed_program_frame && program_return)" in patch
    assert "after UnlockSampleData" in patch
    assert '"boundary\\":\\"directshow_return' in patch
    assert "video_queue_read_ex" in patch
    assert "video_queue_write_ex" in patch
