"""Static contract guards for the #246 runtime telemetry producer.

The upstream producer lives in the pinned submodule and is materialized by the
numbered patch chain.  These checks keep the superproject wiring and the
canonical 0011 patch visible to the scene-switch contract suite without
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

from . import validate_event


ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "patches" / "0011-feat-runtime-telemetry-producer.patch"
PIPELINE_PATCH = ROOT / "patches" / "0016-feat-libobs-expose-video-pipeline-stage-telemetry.patch"
PREVIEW_FASTPATH_PATCH = ROOT / "patches" / "0017-perf-libobs-pipeline-borrowed-preview-publication.patch"
PIPELINE_ACCOUNTING_PATCH = ROOT / "patches" / "0018-feat-libobs-close-video-mix-stage-accounting.patch"
PROGRAM_RETURN_FASTPATH_PATCH = ROOT / "patches" / "0019-perf-win-dshow-pipeline-program-return-publication.patch"
PREVIEW_CONSUMER_LEASE_PATCH = (
    ROOT / "patches" / "0020-perf-win-dshow-elide-unconsumed-preview-return-copies.patch"
)
PROGRAM_CONSUMER_LEASE_PATCH = (
    ROOT / "patches" / "0021-perf-win-dshow-elide-unconsumed-program-return-copies.patch"
)
QT_HOST_TOOLS_PATCH = ROOT / "patches" / "0022-fix-cmake-qt-host-tool-argument-parsing.patch"
BUILD_SCRIPT = ROOT / "scripts" / "build-win.ps1"
FRONTEND_CMAKE = ROOT / "plugins" / "pulsar-frontend-stub" / "CMakeLists.txt"
WEBSOCKET_CMAKE = ROOT / "plugins" / "pulsar-websocket" / "CMakeLists.txt"
FRONTEND_SOURCE = ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp"
HEADLESS_SOURCE = ROOT / "plugins" / "pulsar-headless" / "main.cpp"
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
    assert "5 files changed" in text
    assert "Agent-Role: Eleven" in text
    assert "Agent-Thread: /root" in text
    assert "Work-Unit: ZabLaboratory/Pulsar#246-eleven-security-bounds-20260829" in text
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
    assert "INT64_MAX" in text
    assert "rtmp_first_packet" not in text
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
    assert '\\"boundary\\":\\"encoded_first_packet\\"' in frontend
    assert '\\"packet_pts\\":' in frontend
    assert '\\"packet_timebase_num\\":' in frontend
    assert '\\"packet_cts_monotonic_ns\\":' in frontend
    assert '\\"packet_fer_monotonic_ns\\":' in frontend
    assert '\\"packet_ferc_monotonic_ns\\":' in frontend
    assert '\\"packet_pir_monotonic_ns\\":' in frontend
    assert '\\"packet_callback_monotonic_ns\\":' in frontend
    assert '\\"capture_paths\\":[\\"encoder_input_raw\\",\\"directshow_return\\",\\"encoded_first_packet\\"' in frontend
    assert "streamOutput_ = streamOutput" in frontend
    assert "rtmp_load_active" in frontend
    assert "obs_output_active(streamOutput)" in frontend
    assert "obs_video_add_borrowed_callback" in frontend
    assert "obs_video_remove_borrowed_callback" in frontend
    assert "obs_add_raw_video_callback" not in frontend
    assert "obs_output_add_packet_callback" in frontend
    assert 'obs_source_create("browser_source", "PulsarCefWorkload"' in frontend
    assert "PULSAR_CEF_URL" in frontend
    assert "PULSAR_TRACE_HOST" in frontend
    assert "PULSAR_TRACE_GPU" in frontend
    assert "fitsCalldataInt" in frontend
    assert "static_cast<uint64_t>(INT64_MAX)" in frontend
    assert "(std::numeric_limits<int64_t>::max)()" not in frontend
    assert "static_cast<uint32_t>(cefVideo.fps_num / cefVideo.fps_den)" in frontend
    assert "(std::max)(1, cefVideo.fps_num / cefVideo.fps_den)" not in frontend
    assert "process_cpu_percent" in frontend
    assert "callback_backlog_estimate" in frontend
    assert "encoder_active" in frontend
    assert "encoder_family" in frontend
    assert "obs_encoder_active(videoEncoder)" in frontend
    assert "obs_encoder_t *videoEncoder" in frontend
    assert "queue_rejected" in frontend
    assert "last_committed_frame_id" in frontend
    assert "last_committed_pts_ns" in frontend
    assert "startTraceWriter" in frontend
    assert "traceWriterLoop" in frontend
    assert "writerCv_" in frontend
    assert "writerQueue_" in frontend
    assert "producer_topology" in frontend
    assert "producer_count" in frontend
    assert '\\"source_types\\":[' in frontend
    assert "PULSAR_TRACE_EXTERNAL_LANE_WORKLOAD" in frontend
    assert "if (externalLaneWorkload)" in frontend
    assert "if (cefWorkloadRequested && !externalLaneWorkload)" in frontend
    assert "probe must bind public A/B producers" in frontend
    queue_start = frontend.index("bool PulsarFrontendAPI::queueDualLaneCut")
    queue = frontend[queue_start:]
    assert queue.index("g_runtimeTelemetry.reserve") < queue.index("obs_view_queue_atomic_swap")
    assert queue.index("obs_view_queue_atomic_swap") < queue.index("g_runtimeTelemetry.markAccepted")
    assert 'g_runtimeTelemetry.rejectReserved("atomic_swap_rejected")' in queue
    assert "reservationStillOwned" in queue
    assert "g_runtimeTelemetry.integrityFaulted()" in queue
    assert queue.index("g_runtimeTelemetry.integrityFaulted()") < queue.index("g_runtimeTelemetry.reserve")

    commit_start = frontend.index("void commit(")
    commit = frontend[commit_start : frontend.index("void rawFrame", commit_start)]
    assert "frameId < committed_.frameId || ptsNs < committed_.ptsNs" in commit
    assert "context.onAirLane = onAirLane" in commit
    assert "context.previewLane = previewLane" in commit
    assert "committed_ = context" in commit
    assert "accepted_.valid = false" in commit
    assert "degraded_ = true" in commit
    assert "integrity_fault" in commit
    assert '"physical_swap_committed\\":true' in commit
    assert 'commonEventFields("TakeCommitted"' in commit
    assert 'commonEventFields("TakeAborted"' not in commit
    assert '"reason\\":\"frame_or_pts_regression\"' not in commit
    assert commit.count('commonEventFields("TakeCommitted"') == 1
    assert commit.count("writeLine(fault.str())") == 1
    assert commit.count("lastRawTake_.clear()") == 2
    assert commit.count("lastPacketTake_.clear()") == 2

    reject_start = frontend.index("void rejectReserved(")
    reserve_start = frontend.index("bool reserve(", reject_start)
    accepted_start = frontend.index("bool markAccepted(", reserve_start)
    reject = frontend[reject_start:reserve_start]
    reserve = frontend[reserve_start:accepted_start]
    accepted = frontend[accepted_start:commit_start]
    for precommit in (reject, reserve, accepted):
        assert "lastRawTake_.clear()" not in precommit
        assert "lastPacketTake_.clear()" not in precommit
    assert "committed_ still names the previous Take" in reserve

    # The admission clock is captured by reserve() and must survive the
    # successful queue promotion.  markAccepted() may run later on the frame
    # callback, but it must never replace the causal timestamp with a second
    # nowNs() sample.  A failed enqueue still retires accepted_ below and emits
    # no TakeAccepted event, so this invariant cannot manufacture acceptance.
    assert "context.acceptedAtNs = diagnosticNowNs;" in reserve
    assert "context.acceptedAtNs = nowNs();" not in accepted
    assert "commonEventFields(\"TakeAccepted\", context, seq, \"take_accepted\", context.acceptedAtNs," in accepted
    assert accepted.index("context = reserved_;\n") < accepted.index("accepted_ = context;")

    assert "BeginRuntimeTakeTelemetry" in websocket
    assert "pulsar_runtime_telemetry::begin_take" in websocket
    assert "pulsar_runtime_telemetry::cancel_take" in websocket
    assert "freeze_until_monotonic_ns" in websocket
    assert "ingress_now_monotonic_ns" in websocket
    assert "deadline_delta_ns" in websocket
    assert "called=%d available=%d accepted=%d" in websocket

    assert "freeze_until_monotonic_ns=%lld ingress_now_monotonic_ns=%llu deadline_delta_ns=%s" in frontend
    assert "freeze_until_monotonic_ns=%llu reserve_now_monotonic_ns=%llu deadline_delta_ns=%s" in frontend
    assert "deadline >= now" in frontend

    # 0011 must record the observation after the DirectShow sample has been
    # unlocked, and only for the actual ProgramReturn filter instance.
    assert "if (consumed_program_frame && program_return)" in patch
    assert patch.index("UnlockSampleData") < patch.index("if (consumed_program_frame && program_return)")
    assert '"boundary\\":\\"directshow_return' in patch
    assert "video_queue_read_ex" in patch
    assert "-\tvideo_queue_write_ex" not in patch
    assert "copy_telemetry_counter" in patch
    assert "INT64_MAX" in patch
    assert "queue_rejected" in frontend


def test_headless_audio_is_bounded_for_live_interleaving() -> None:
    headless = HEADLESS_SOURCE.read_text(encoding="utf-8")
    reset_start = headless.index("bool reset_audio()")
    reset_end = headless.index("bool websocket_server_ready(", reset_start)
    reset = headless[reset_start:reset_end]

    assert "obs_audio_info2 oai" in reset
    assert "oai.samples_per_sec = 48000" in reset
    assert "oai.max_buffering_ms = 20" in reset
    assert "oai.fixed_buffering = true" in reset
    assert "obs_reset_audio2(&oai)" in reset
    assert "obs_reset_audio(&oai)" not in reset


def test_desktop_wasapi_uses_the_libobs_timeline_before_creation() -> None:
    frontend = FRONTEND_SOURCE.read_text(encoding="utf-8")
    settings_start = frontend.index('OBSDataAutoRelease desktopSettings = obs_data_create();')
    create_start = frontend.index(
        'obs_source_create("wasapi_output_capture", "PulsarDesktopAudio"',
        settings_start,
    )
    settings = frontend[settings_start:create_start]

    # Device-clock alignment can hold the common Program audio route behind
    # the endpoint clock.  The setting must be explicit and applied before
    # source creation, while the runtime log makes the effective policy
    # auditable in a real Pulsar launch.
    assert 'obs_data_set_bool(desktopSettings, "use_device_timing", false);' in settings
    assert "desktop audio configured use_device_timing=false" in settings
    assert settings.index('obs_data_set_bool(desktopSettings, "use_device_timing", false);') < settings.index(
        "desktop audio configured use_device_timing=false"
    )


def _bounded_identifier_model(value: bytes, capacity: int = 129) -> tuple[str, str] | None:
    """Model the DirectShow boundary contract for adversarial metadata bytes."""

    terminator = value.find(b"\0", 0, capacity)
    if terminator < 0:
        return None

    raw = value[:terminator]
    escaped: list[str] = []
    for byte in raw:
        escaped.append(
            {
                0x08: r"\b",
                0x0C: r"\f",
                0x0A: r"\n",
                0x0D: r"\r",
                0x09: r"\t",
                0x5C: r"\\",
                0x22: r'\"',
            }.get(byte, chr(byte))
        )
        if byte < 0x20 and byte not in {0x08, 0x09, 0x0A, 0x0C, 0x0D}:
            return None
    return raw.decode("latin-1"), "".join(escaped)


def _layout_model(
    available: int,
    offsets: tuple[int, int, int],
    frame_header_size: int,
    frame_size: int,
    header_size: int = 80,
) -> bool:
    if available < header_size or frame_header_size < 0 or frame_size < 0:
        return False
    previous = 0
    for index, offset in enumerate(offsets):
        if offset < header_size or offset % 32 or (index and offset <= previous):
            return False
        end = offset + frame_header_size + frame_size
        if end > available:
            return False
        previous = offset
    return True


def test_directshow_metadata_boundary_is_bounded_and_json_safe() -> None:
    patch = PATCH.read_text(encoding="utf-8")

    # These source-level guards bind the behavioral model below to the actual
    # four fixed-size fields and reject the former unbounded C-string helper.
    assert "static bool trace_prepare_identifier(const char *value, size_t capacity" in patch
    assert r"std::memchr(value, '\0', capacity)" in patch
    assert "trace_escape(" not in patch
    for field in ("runtime_instance_id", "command_id", "intent_id", "take_command_id"):
        assert f"metadata.{field}, sizeof(metadata.{field})" in patch
    assert "case '\\b': escaped += \"\\\\b\";" in patch
    assert "case '\\f': escaped += \"\\\\f\";" in patch
    assert "if (ch < 0x20)" in patch
    assert "return false;" in patch[patch.index("trace_prepare_identifier"):patch.index("append_trace_line")]
    assert "append_trace_line(observation.str(), runtime_id)" in patch
    assert "last_trace_take == take_command_id" in patch

    fields = ("runtime_instance_id", "command_id", "intent_id", "take_command_id")
    for field in fields:
        assert _bounded_identifier_model(b"A" * 129) is None, field
        assert _bounded_identifier_model(b"A" * 128 + b"\0") == ("A" * 128, "A" * 128)

    controls = b"\b\f\n\r\t\\\"ok\0"
    modeled = _bounded_identifier_model(controls)
    assert modeled is not None
    raw, escaped = modeled
    assert escaped == r"\b\f\n\r\t\\\"ok"
    parsed = json.loads('{"identifier":"' + escaped + '"}')
    assert parsed["identifier"] == raw
    assert _bounded_identifier_model(b"bad\x01\0") is None


def test_shared_queue_metadata_layout_rejects_incoherent_mappings_without_legacy_drift() -> None:
    patch = PATCH.read_text(encoding="utf-8")

    assert "#define VIDEO_QUEUE_METADATA_VERSION 1U" in patch
    assert "uint32_t frame_metadata_version;" in patch
    assert "uint32_t reserved[6];" in patch
    assert "static bool queue_mapping_size" in patch
    assert "VirtualQuery" in patch
    assert "static bool queue_metadata_layout" in patch
    assert "Never reinterpret a partially populated/unknown extension as legacy." in patch
    assert "static bool queue_layout_valid" in patch
    assert "vq->mapping_size = mapping_size" in patch
    assert "header.frame_metadata_version = metadata_enabled ? VIDEO_QUEUE_METADATA_VERSION : 0;" in patch
    assert "#define LEGACY_FRAME_HEADER_SIZE 32U" in patch
    assert (
        "const uint32_t frame_header_size = metadata_enabled ? FRAME_HEADER_SIZE : LEGACY_FRAME_HEADER_SIZE;"
        in patch
    )
    assert "vq->frame[i] = ((uint8_t *)vq->header) + off + vq->frame_header_size;" in patch
    assert "!queue_mapping_size(vq.header, &vq.mapping_size)" in patch
    assert "UnmapViewOfFile(vq.header);" in patch

    # Exercise the arithmetic used to decide whether every slot is contained
    # in the mapped view; malformed metadata must not be reinterpreted.
    valid_offsets = (96, 736, 1376)
    assert _layout_model(2048, valid_offsets, 576, 32)
    assert not _layout_model(1983, valid_offsets, 576, 32)
    assert not _layout_model(2048, (96, 96, 1376), 576, 32)
    assert not _layout_model(2048, (96, 736, 1375), 576, 32)
    assert not _layout_model(2048, valid_offsets, 576, 700)

    def metadata_mode(header_size: int, version: int) -> tuple[int, bool] | None:
        if header_size == 0 and version == 0:
            return 32, False
        if header_size == 576 and version == 1:
            return 576, True
        return None

    assert metadata_mode(0, 0) == (32, False)
    assert metadata_mode(576, 1) == (576, True)
    assert metadata_mode(576, 0) is None
    assert metadata_mode(0, 1) is None
    assert metadata_mode(32, 1) is None


def test_take_committed_runtime_json_closes_preview_lane_quote() -> None:
    frontend = FRONTEND_SOURCE.read_text(encoding="utf-8")
    commit_start = frontend.index("void commit(")
    commit = frontend[commit_start : frontend.index("void rawFrame", commit_start)]
    closing_quote = chr(92) + '"'
    assert f'laneId(context.previewLane) << "{closing_quote}";' in commit

    # This is a complete TakeCommitted record captured from the runtime shape
    # (including the observed frame/PTS boundary from the exact Probe-2 smoke).
    # Parse the line as JSON and through the v1 validator: a substring check
    # would not catch the missing closing quote that produced A}}.
    candidate_sha = "0123456789abcdef" * 4
    line = (
        '{"record_type":"event","event":{'
        '"contract":"pulsar.scene-switch.v1","schema_version":1,"message_type":"event",'
        '"event_type":"TakeCommitted","command_id":"command-001","intent_id":"intent-001",'
        '"runtime_instance_id":"runtime-qpc-001","server_seq":2,"state":"ready",'
        '"previous_revisions":{"program":0,"preview":1,"role_map":0},'
        '"revisions":{"program":1,"preview":1,"role_map":1},'
        '"role_map":{"on_air":"B","preview":"A"},'
        '"previous_role_map":{"on_air":"A","preview":"B"},'
        '"observed_at_monotonic_ns":1318528200144256,'
        f'"payload_sha256":"{candidate_sha}",'
        '"take_command_id":"take-001","target_lane_id":"B","target_scene_id":"Scene B",'
        '"source_lane_id":"B","frame_id":604,"pts_ns":1318528200144256,'
        '"program_lane_id":"B","preview_lane_id":"A"}}'
    )
    parsed = json.loads(line)
    event = parsed["event"]
    assert validate_event(event) == event
    assert event["event_type"] == "TakeCommitted"
    assert event["take_command_id"] == "take-001"
    assert event["frame_id"] == 604
    assert event["pts_ns"] == 1318528200144256
    assert event["role_map"] == {"on_air": "B", "preview": "A"}
    assert event["program_lane_id"] == "B"
    assert event["preview_lane_id"] == "A"

    malformed = line.replace('"preview_lane_id":"A"}}', '"preview_lane_id":"A}}')
    with pytest.raises(json.JSONDecodeError):
        json.loads(malformed)


def test_runtime_session_line_is_valid_json_and_has_bound_source_topology() -> None:
    # This is the exact shape emitted by sessionJson(), including the fields
    # that caught the prior missing-quote regression and prevent workload flags
    # from standing in for actual source registration.
    candidate_sha = "0123456789abcdef" * 2 + "01234567"
    line = (
        '{\"record_type\":\"session\",\"schema\":\"pulsar.take-latency.v1\",'
        '\"runtime_instance_id\":\"runtime-nvenc-001\",\"session_id\":\"runtime-nvenc-001-nvenc\",'
        '\"codec\":\"nvenc\",\"warmup_takes\":100,'
        '\"video\":{\"width\":1920,\"height\":1080,\"fps_num\":60,\"fps_den\":1},'
        '\"workload\":{\"wgc\":true,\"cef\":true,\"nvenc\":true},'
        '\"capture_paths\":[\"encoder_input_raw\",\"directshow_return\",\"encoded_first_packet\",'
        '\"decoded_first_frame\",\"antenna_first_frame\"],'
        '\"source_types\":[\"window_capture\",\"browser_source\"],'
        '\"resource_reference\":{\"extra_frame_render_ms\":0.091,\"extra_resident_bytes\":3130000},'
        f'\"build_revision\":\"{candidate_sha}\",'
        '\"command_line\":\"scripts/probe-dual-lane.py --trace\",'
        '\"hardware\":{\"host\":\"test-host\",\"gpu\":\"adapter \\\"246\\\"\"},'
        '\"producer_topology\":\"dual_lane_ab\",\"producer_count\":2,'
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
    assert validated["build_revision"] == candidate_sha
    with pytest.raises(parser.EvidenceError, match="build_revision"):
        parser._validate_session({**session, "build_revision": "local-build"})

    with pytest.raises(json.JSONDecodeError):
        gpu_value = r'"gpu":"adapter \"246\""'
        assert gpu_value in line
        malformed = line.replace(gpu_value, gpu_value[:-1], 1)
        json.loads(malformed)


def test_wire_deadline_uses_qpc_compatible_perf_counter_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("probe_dual_lane_clock_contract", DUAL_LANE_PROBE)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = probe
    spec.loader.exec_module(probe)

    # Deliberately give monotonic_ns a different epoch.  The serialized
    # deadline must follow perf_counter_ns, the QPC-compatible source used by
    # libobs, and must not be repaired by adding hidden producer slack.
    wire_now = 10_000_000_000
    monkeypatch.setattr(probe.time, "perf_counter_ns", lambda: wire_now)
    monkeypatch.setattr(probe.time, "monotonic_ns", lambda: 7_000_000_000)

    class Process:
        runtime_id = "runtime-clock-001"

    envelope = probe.take_telemetry_data(Process(), 1, "scene-clock")
    deadline = envelope["pulsarTelemetry"]["freeze_until_monotonic_ns"]
    assert probe.wire_monotonic_ns() == wire_now
    assert deadline == wire_now + probe.TAKE_FREEZE_HANDOFF_BUDGET_NS
    assert probe.wire_deadline_delta_ns(deadline, wire_now) == probe.TAKE_FREEZE_HANDOFF_BUDGET_NS
    assert probe.wire_deadline_covers_handoff(deadline, wire_now, handoff_ns=1_000_000_000)
    assert not probe.wire_deadline_covers_handoff(deadline, deadline, handoff_ns=0)
    assert probe.wire_deadline_delta_ns(deadline, deadline + 1) == -1

    with pytest.raises(probe.ProbeFailure, match="signed 64-bit"):
        probe.make_wire_deadline_ns(probe.INT64_MAX, margin_ns=1)


def test_wire_clock_calibration_brackets_qpc_and_rejects_epoch_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("probe_dual_lane_calibration_contract", DUAL_LANE_PROBE)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = probe
    spec.loader.exec_module(probe)

    # A midpoint of the bracketed QPC samples removes the deterministic call
    # overhead from this test while keeping the calibration arithmetic exact.
    qpc_samples = iter((1_000_000_000, 1_000_000_100))
    monkeypatch.setattr(probe, "_qpc_ns", lambda: next(qpc_samples))
    monkeypatch.setattr(probe, "wire_monotonic_ns", lambda: 1_000_000_050)
    calibrated = probe.calibrate_wire_clock(max_delta_ns=0)
    assert calibrated == {
        "source": "perf_counter_ns/qpc",
        "wire_now_ns": 1_000_000_050,
        "qpc_now_ns": 1_000_000_050,
        "qpc_delta_ns": 0,
        "qpc_bound_ns": 50,
    }

    qpc_samples = iter((2_000_000_000, 2_000_000_100))
    monkeypatch.setattr(probe, "_qpc_ns", lambda: next(qpc_samples))
    monkeypatch.setattr(probe, "wire_monotonic_ns", lambda: 2_000_001_000)
    with pytest.raises(probe.ProbeFailure, match="not aligned"):
        probe.calibrate_wire_clock(max_delta_ns=100)

    monkeypatch.setattr(probe, "_qpc_ns", lambda: None)
    monkeypatch.setattr(probe, "wire_monotonic_ns", lambda: 3_000_000_000)
    without_qpc = probe.calibrate_wire_clock()
    assert without_qpc["source"] == "perf_counter_ns"
    assert without_qpc["wire_now_ns"] == 3_000_000_000
    assert without_qpc["qpc_delta_ns"] is None


def test_runtime_driver_requires_real_wgc_and_local_cef_evidence() -> None:
    driver = DUAL_LANE_PROBE.read_text(encoding="utf-8")
    assert "class DeterministicCefServer" in driver
    assert '"window_capture": "probe-dual-lane-wgc-A"' in driver
    assert '"browser_source": "probe-dual-lane-cef-B"' in driver
    assert "create_public_lane_scenes" in driver
    assert "duplicated producer instances" in driver
    assert "single_lane_reference" in driver
    assert "producer_count" in driver
    assert 'lanes = ("A",) if mode == "reference" else ("A", "B")' in driver
    assert 'env["PULSAR_TRACE_EXTERNAL_LANE_WORKLOAD"] = "1"' in driver
    assert 'env.pop("PULSAR_TRACE_EXTERNAL_LANE_WORKLOAD", None)' in driver
    assert "resolve_trace_hardware" in driver
    assert "PULSAR_TRACE_HOST" in driver
    assert "PULSAR_TRACE_GPU" in driver
    assert '\"GetInputList\"' in driver
    assert '\"GetInputSettings\"' in driver
    assert '\"GetSceneItemList\"' in driver
    assert '\"GetSourceScreenshot\"' in driver
    assert "frame_is_nonblack" in driver
    assert "--cef-workload requires --capture-window" in driver
    assert "--trace requires --capture-window and --cef-workload" in driver
    assert "BUILD_REVISION_RE.fullmatch" in driver
    assert "wait_for_trace_record" in driver
    assert "Default" in driver  # explicitly documented as non-evidence
    assert "workload-default-scene-items" not in driver


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
        with urllib.request.urlopen(f"{server.url}?lane=A", timeout=2) as response:
            lane_a = response.read()
        with urllib.request.urlopen(f"{server.url}?lane=B", timeout=2) as response:
            lane_b = response.read()
        assert lane_a != lane_b
        assert b"LANE A" in lane_a
        assert b"LANE B" in lane_b
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


def test_video_pipeline_stage_telemetry_is_exported_and_lane_qualified() -> None:
    patch = PIPELINE_PATCH.read_text(encoding="utf-8")
    frontend = FRONTEND_SOURCE.read_text(encoding="utf-8")
    parser = TAKE_LATENCY_PROBE.read_text(encoding="utf-8")

    for token in (
        "obs_get_graphics_pipeline_stats",
        "obs_video_get_mix_pipeline_stats",
        "render_submit_ns",
        "download_ns",
        "flush_ns",
        "output_copy_ns",
        "tick_sources_ns",
        "render_displays_ns",
        "graphics_tasks_ns",
    ):
        assert token in patch
    assert "source_profiler_gpu_enable(true)" in frontend
    assert r'\"pipeline\"' in frontend
    assert r'\"program_mix\"' in frontend
    assert r'\"preview_mix\"' in frontend
    assert r'\"source_profile\"' in frontend
    assert "PIPELINE_STAGE_FIELDS" in parser
    assert "MIX_STAGE_FIELDS" in parser
    assert "SOURCE_PROFILE_FIELDS" in parser


def test_preview_return_fastpath_preserves_format_and_lifetime_barriers() -> None:
    patch = PREVIEW_FASTPATH_PATCH.read_text(encoding="utf-8")

    for token in (
        "raw_video_borrowed",
        "borrowed_video_thread",
        "start_borrowed_raw_video",
        "stop_borrowed_raw_video",
        "obs_video_add_borrowed_callback",
        "obs_video_remove_borrowed_callback",
        "borrowed_video_pending || video->borrowed_video_busy",
        "borrowed_publish_ns",
        "borrowed_wait_ns",
        "video_output_active(video->video)",
        "conversion->format == native->format",
        "conversion->width == native->width",
        "conversion->height == native->height",
    ):
        assert token in patch
    assert ".raw_video_borrowed = virtual_video" in patch
    assert "if (!output->borrowed_video_active)" in patch

    frontend = FRONTEND_SOURCE.read_text(encoding="utf-8")
    assert "obs_video_add_borrowed_callback(previewVideo, OnSceneSwitchPreviewVideoFrame, this)" in frontend
    assert "obs_video_remove_borrowed_callback(previewVideo, OnSceneSwitchPreviewVideoFrame, this)" in frontend
    assert "video_output_connect(previewVideo, nullptr, OnSceneSwitchPreviewVideoFrame, this)" not in frontend


def test_program_and_preview_pipeline_accounting_has_leaf_stages_and_residuals() -> None:
    patch = PIPELINE_ACCOUNTING_PATCH.read_text(encoding="utf-8")
    frontend = FRONTEND_SOURCE.read_text(encoding="utf-8")
    parser = TAKE_LATENCY_PROBE.read_text(encoding="utf-8")

    for token in (
        "render_main_ns",
        "render_setup_ns",
        "gpu_flush_ns",
        "render_teardown_ns",
        "render_scale_ns",
        "render_convert_ns",
        "gpu_encode_submit_ns",
        "raw_stage_ns",
        "borrowed_schedule_ns",
        "obs_output_get_raw_pipeline_stats",
    ):
        assert token in patch
    for token in (
        "render_unattributed_ms",
        "frame_unattributed_ms",
        "return_output_callback_ms",
        "renderResidualMs",
        "frameResidualMs",
    ):
        assert token in frontend
    assert "ACCOUNTING_COMPLETE_P95_MS" in parser
    assert '"accounting_status"' in parser


def test_program_return_uses_borrowed_worker_without_changing_other_outputs() -> None:
    patch = PROGRAM_RETURN_FASTPATH_PATCH.read_text(encoding="utf-8")

    assert "struct obs_output_info program_return_info" in patch
    assert patch.count("+\t.raw_video_borrowed = virtual_video") == 1
    assert "@@ -249,6 +249,7 @@ struct obs_output_info program_return_info" in patch
    assert "VIDEO_FORMAT_NV12 || info->format == VIDEO_FORMAT_P010" in patch
    assert "borrowed_frame.data[1] = borrowed_frame.data[0]" in patch
    assert "borrowed_frame.linesize[1] = borrowed_frame.linesize[0]" in patch


def test_preview_return_copy_is_gated_by_an_active_directshow_consumer_lease() -> None:
    patch = PREVIEW_CONSUMER_LEASE_PATCH.read_text(encoding="utf-8")

    for token in (
        'consumer_lease_name = queue_name + L".ConsumerActive"',
        "CreateEventW(nullptr, TRUE, FALSE, consumer_lease_name.c_str())",
        "OpenEventW(SYNCHRONIZE, FALSE, vcam->consumer_lease_name)",
        "CloseHandle(lease)",
        "ReleaseConsumerLease();",
        "if (!preview_consumer_is_active(vcam))",
    ):
        assert token in patch

    # ProgramReturn and the generic OBS virtual camera must keep publishing.
    assert "if (!vcam->preview_return)" in patch
    assert "return true;" in patch

    # Preview composition/readiness remains on the existing hot borrowed path;
    # only the optional shared-memory queue write is skipped.
    assert ".raw_video_borrowed = virtual_video" not in patch
    assert "video_queue_write_ex" in patch


def test_program_return_copy_uses_the_same_crash_safe_consumer_gate() -> None:
    patch = PROGRAM_CONSUMER_LEASE_PATCH.read_text(encoding="utf-8")

    for token in (
        "consumer_gated = program_return || preview_return",
        "if (consumer_gated && !queue_namespace_rejected)",
        "if (!vcam->consumer_gated)",
        "vcam->consumer_gated = vcam->program_return || vcam->preview_return",
        "if (!return_consumer_is_active(vcam))",
        'vcam->program_return ? "ProgramReturn" : "PreviewReturn"',
    ):
        assert token in patch

    # The gate surrounds only the DirectShow queue publication. Program mixing,
    # encoding and the borrowed output worker remain untouched.
    assert "-\tvideo_queue_write_ex" not in patch
    assert "obs_encoder" not in patch
    assert "raw_video_borrowed" not in patch


def test_pristine_windows_configure_keeps_architecture_out_of_keyword_parsing() -> None:
    patch = QT_HOST_TOOLS_PATCH.read_text(encoding="utf-8")

    assert "function(_handle_qt_cross_compile architecture)" in patch
    assert '-  cmake_parse_arguments(PARSE_ARGV 0 _HQCC' in patch
    assert '+  cmake_parse_arguments(PARSE_ARGV 1 _HQCC' in patch
    assert 'set(host_processor "$ENV{PROCESSOR_ARCHITEW6432}")' in patch
    assert 'set(host_processor "$ENV{PROCESSOR_ARCHITECTURE}")' in patch
    assert "Unable to determine the Windows host processor for Qt host tools" in patch
    assert "-DIRECTORY" in patch


def test_local_build_fastpath_is_guarded_complete_and_opt_in() -> None:
    text = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "[switch] $Fast" in text
    assert "$Fast -and ($Full -or $GuiBuild -or $Clean -or $Stage -eq 'configure')" in text
    assert "-Fast requires an existing compatible headless build_x64 cache" in text
    for flag in ("ENABLE_FRONTEND", "ENABLE_UI", "ENABLE_BROWSER", "ENABLE_WEBSOCKET"):
        assert f"^{flag}:BOOL=OFF\\r?$" in text
    for target in (
        "libobs",
        "win-dshow",
        "obs-virtualcam-module",
        "obs-nvenc",
        "obs-x264",
        "pulsar-headless",
    ):
        assert target in text

    # Full builds remain the default and retain the original preset paths.
    assert "& $cmake --build --preset $preset --config RelWithDebInfo --parallel" in text
    assert "& $cmake --build $pulsarBuild --config RelWithDebInfo --parallel" in text
