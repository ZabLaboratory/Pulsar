"""Static contract tests for the Pulsar dual-lane implementation.

The Windows/GPU probe is intentionally separate: it must exercise a built
Pulsar binary with x264 and NVENC.  These tests run without that environment
and protect the source-level invariants that make the probe meaningful:
physical A/B roots, stable downstream identities, a frame-boundary pair swap,
and a teardown barrier for an extracted (in-flight) swap.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND = _ROOT / "plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp"
_CONTROL_BRIDGE = _ROOT / "plugins/pulsar-frontend-stub/include/pulsar-dual-lane-control.h"
_WEBSOCKET_HANDLER = _ROOT / "plugins/pulsar-websocket/src/requesthandler/RequestHandler.cpp"
_DUAL_LANE_PATCH = _ROOT / "patches/0009-feat-libobs-add-frame-boundary-dual-lane-swaps.patch"
_DIRECTSHOW_NAMESPACE_PATCH = _ROOT / "patches/0010-fix-win-dshow-reject-ambiguous-queue-namespaces.patch"
_RUNTIME_PROBE = _ROOT / "scripts/probe-dual-lane.py"
_ROLLBACK_PROBE = _ROOT / "scripts/probe-dual-lane-rollback.py"
_OUTPUT_EFFECT_PROBE = _ROOT / "scripts/probe-output-effect.py"
_DUAL_LANE_CONFIG = _ROOT / "plugins/pulsar-frontend-stub/include/pulsar-dual-lane-config.h"
_DUAL_LANE_CONFIG_TEST = _ROOT / "tests/dual-lane-config/dual-lane-config-probe.cpp"
_DUAL_LANE_CONFIG_CMAKE = _ROOT / "tests/dual-lane-config/CMakeLists.txt"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    end_at = source.index(end, start_at + len(start))
    return source[start_at:end_at]


def _load_probe(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_directshow_cleanup_keeps_owned_handle_and_fails_closed() -> None:
    probe = _load_probe(_RUNTIME_PROBE, "pulsar_dual_lane_probe_cleanup")

    class ExitingProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is None or timeout >= 0
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fake", timeout)
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    class StuckProcess(ExitingProcess):
        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    class StuckReader:
        def join(self, timeout: float | None = None) -> None:
            assert timeout == 2

        def is_alive(self) -> bool:
            return True

    def process_with(fake: object):
        instance = probe.PulsarProcess.__new__(probe.PulsarProcess)
        instance.directshow_proc = fake
        instance.directshow_thread = threading.Thread(target=lambda: None)
        instance.directshow_thread.start()
        instance.directshow_cleanup_failure = None
        return instance

    clean = process_with(ExitingProcess())
    clean.stop_directshow_consumer()
    assert clean.directshow_proc is None
    assert clean.directshow_thread is None

    stuck = process_with(StuckProcess())
    with pytest.raises(probe.ProbeFailure, match="remained alive|exit was not confirmed|could not be killed"):
        stuck.stop_directshow_consumer()
    assert stuck.directshow_proc is not None
    assert stuck.directshow_cleanup_failure is not None

    reader_stuck = process_with(ExitingProcess())
    reader_stuck.directshow_thread = StuckReader()
    with pytest.raises(probe.ProbeFailure, match="reader thread did not exit"):
        reader_stuck.stop_directshow_consumer()
    assert reader_stuck.directshow_proc is not None
    assert reader_stuck.directshow_thread is not None

    lease_probe = probe.PulsarProcess(
        Path("pulsar.exe"),
        "x264",
        Path("record"),
        trace_path=Path("trace.jsonl"),
        runtime_id="runtime-test",
    )
    finished = ExitingProcess()
    finished.returncode = 0
    lease_probe.proc = finished
    lease_probe.lines.append("PULSAR_RUNTIME_INSTANCE runtime_dir_lease=released id=runtime-test")
    with pytest.raises(probe.ProbeFailure, match="runtime instance lease"):
        lease_probe.assert_shutdown_clean(require_runtime_lease=True)
    lease_probe.lines.append("PULSAR_RUNTIME_INSTANCE lease=released id=runtime-test")
    with pytest.raises(probe.ProbeFailure, match="legacy DirectShow alias state"):
        lease_probe.assert_shutdown_clean(require_runtime_lease=True)
    lease_probe.lines.append("PULSAR_LEGACY_ALIAS lease=disabled id=runtime-test")
    lease_probe.assert_shutdown_clean(require_runtime_lease=True)

    log_thread_probe = probe.PulsarProcess(
        Path("pulsar.exe"),
        "x264",
        Path("record"),
        trace_path=Path("trace.jsonl"),
        runtime_id="runtime-test",
    )
    log_thread_probe.proc = finished
    log_thread_probe.lines.extend(lease_probe.lines)
    log_thread_probe.thread = StuckReader()
    with pytest.raises(probe.ProbeFailure, match="log reader thread did not exit"):
        log_thread_probe.assert_shutdown_clean(require_runtime_lease=True)
    assert log_thread_probe.thread is not None


def test_physical_roots_are_fixed_and_roles_point_at_their_lane() -> None:
    source = _read(_FRONTEND)

    assert 'obs_scene_create_private("PulsarLaneA")' in source
    assert 'obs_scene_create_private("PulsarLaneB")' in source
    assert 'obs_scene_create_private("PulsarPreviewBootstrap")' in source
    assert "obs_scene_add(laneAScene, obs_scene_get_source(templateScene))" in source
    assert "obs_scene_add(laneBScene, obs_scene_get_source(previewBootstrap))" in source
    assert "obs_scene_add(laneScene, scene)" in source
    assert "laneSources[onAirLane] == currentScene" in source
    assert "laneSources[previewLane] == previewScene" in source
    assert "laneItems[0] && laneItems[1]" in source
    assert "programSelection != previewSelection" in source
    assert "previewView && programVideo && previewVideo" in source
    assert "programView == obs_get_main_view()" in source
    assert "programVideo == obs_get_video()" in source
    assert "std::swap(self->onAirLane, self->previewLane)" in source
    assert "std::swap(self->currentScene, self->previewScene)" in source


def test_live_selected_scene_references_propagate_and_roles_swap_after_take() -> None:
    source = _read(_FRONTEND)
    setup = _between(
        source,
        "bool PulsarFrontendAPI::setupDualLane(obs_scene_t *templateScene)",
        "bool PulsarFrontendAPI::queueDualLaneCut(obs_source_t *scene)",
    )
    replace = _between(
        source,
        "bool PulsarFrontendAPI::replaceLaneCompositionLocked(int lane, obs_source_t *scene)",
        "bool PulsarFrontendAPI::setupDualLane(obs_scene_t *templateScene)",
    )
    commit = _between(
        source,
        "void PulsarFrontendAPI::OnDualLaneCutCommitted(void *param, uint64_t frameId, uint64_t ptsNs)",
        "bool PulsarFrontendAPI::setup()",
    )

    # The wrapper children are the exact scene sources, so post-bind edits to
    # each public scene remain visible in its selected lane.  Preview starts
    # on a private source only until a second public scene is selected.
    assert "obs_scene_add(laneAScene, obs_scene_get_source(templateScene))" in setup
    assert "obs_scene_add(laneBScene, obs_scene_get_source(previewBootstrap))" in setup
    assert "obs_scene_add(laneScene, scene)" in replace
    assert "if (!obs_scene_from_source(scene))" in replace
    assert "OBS_SCENE_DUP_PRIVATE_COPY" not in setup + replace
    assert "programSelection != previewSelection" in source

    # A Cut swaps the stable root roles and the logical scene refs together at
    # the frame-boundary callback; it never clones or rebinds a view.
    assert "std::swap(self->currentScene, self->previewScene)" in commit
    assert "std::swap(self->programSelection, self->previewSelection)" in commit
    assert "std::swap(self->onAirLane, self->previewLane)" in commit


def test_preview_and_direct_selection_mutate_composition_not_view_identity() -> None:
    source = _read(_FRONTEND)
    preview_setter = _between(
        source,
        "void obs_frontend_set_current_preview_scene(obs_source_t *scene) override",
        "    // ---------- screenshots ----------",
    )
    scene_setter = _between(
        source,
        "void PulsarFrontendAPI::obs_frontend_set_current_scene(obs_source_t *scene)",
        "void PulsarFrontendAPI::obs_frontend_get_transitions",
    )
    cut = _between(
        source,
        "bool PulsarFrontendAPI::queueDualLaneCut(obs_source_t *scene)",
        "bool PulsarFrontendAPI::setup()",
    )

    assert "replaceLaneCompositionLocked(previewLane, scene)" in preview_setter
    assert "replaceLaneCompositionLocked(onAirLane, scene)" in scene_setter
    assert "Program mutation rejected while Take is pending" in scene_setter
    assert "direct scene switch rejected: scene aliases Preview; use Take" in scene_setter
    assert "obs_view_set_source(programView" not in preview_setter + scene_setter + cut
    assert "obs_view_set_source(previewView" not in preview_setter + scene_setter + cut


def test_encoder_and_stable_surfaces_are_bound_only_during_setup() -> None:
    source = _read(_FRONTEND)
    assert source.count("obs_encoder_set_video(videoEncoder, programVideo)") == 1
    assert "obs_encoder_set_video(videoEncoder, obs_get_video())" not in source

    setup = _between(
        source,
        "bool PulsarFrontendAPI::setupDualLane(obs_scene_t *templateScene)",
        "bool PulsarFrontendAPI::queueDualLaneCut(obs_source_t *scene)",
    )
    cut = _between(
        source,
        "bool PulsarFrontendAPI::queueDualLaneCut(obs_source_t *scene)",
        "bool PulsarFrontendAPI::setup()",
    )
    assert "obs_view_set_source(programView, 0, currentScene)" in setup
    assert "obs_view_set_source(previewView, 0, previewScene)" in setup
    assert "obs_output_set_media(programReturnOutput, programVideo" in setup
    assert "obs_output_set_media(previewReturnOutput, previewVideo" in setup
    assert "obs_output_start(previewReturnOutput)" in setup
    assert setup.index("obs_output_set_media(previewReturnOutput, previewVideo") < setup.index(
        "obs_output_start(previewReturnOutput)"
    )
    assert "PreviewView producer started for frame-backed readiness" in setup
    assert "failed to start PreviewView producer" in setup
    assert setup.count("obs_view_create()") == 1
    assert "programView = obs_get_main_view();" in setup
    assert "programVideo = obs_get_video();" in setup
    assert "obs_view_add(programView)" not in setup
    assert "obs_view_remove(programView)" not in setup
    assert "obs_view_destroy(programView)" not in setup
    assert "obs_view_set_source(" not in cut
    assert "obs_encoder_set_video(" not in cut
    assert "obs_output_set_media(" not in cut


def test_program_and_preview_return_outputs_have_distinct_ids_and_bindings() -> None:
    patch = _read(_DUAL_LANE_PATCH)
    source = _read(_FRONTEND)

    preview_info = _between(
        patch,
        "+struct obs_output_info preview_return_info = {",
        "+};",
    )
    assert preview_info.count('.id = "preview_return_output"') == 1
    assert 'program_return_output",\n' not in preview_info
    assert patch.count("+struct obs_output_info preview_return_info = {") == 1

    assert source.count(
        'obs_output_create("program_return_output", "PulsarProgramReturn"'
    ) == 2
    assert source.count(
        'obs_output_create("preview_return_output", "PulsarPreviewReturn"'
    ) == 2
    assert 'obs_output_create("program_return_output", "PulsarPreviewReturn"' not in source
    assert "obs_output_set_media(programReturnOutput, programVideo" in source
    assert "obs_output_set_media(previewReturnOutput, previewVideo" in source

    queue_hunk = _between(
        patch,
        "static void queue_name_for_output(obs_output_t *output",
        "static const char *virtualcam_name",
    )
    assert "obs_output_get_id(output)" in queue_hunk
    assert '"program_return_output"' in queue_hunk
    assert '"preview_return_output"' in queue_hunk
    assert "PulsarPreviewReturn" in queue_hunk


def test_directshow_namespace_decision_is_shared_and_rejects_before_queue_sinks() -> None:
    patch = _read(_DIRECTSHOW_NAMESPACE_PATCH)
    header = _between(
        patch,
        "+enum directshow_queue_namespace {",
        "diff --git a/plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp",
    )
    producer = _between(
        patch,
        "diff --git a/plugins/win-dshow/virtualcam.c",
        "-- \n",
    )
    consumer = _between(
        patch,
        "diff --git a/plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp",
        "diff --git a/plugins/win-dshow/virtualcam-module/virtualcam-filter.hpp",
    )

    assert "DIRECTSHOW_QUEUE_NAMESPACE_REJECT" in header
    assert "DIRECTSHOW_QUEUE_NAMESPACE_LEGACY" in header
    assert "DIRECTSHOW_QUEUE_NAMESPACE_DEDICATED" in header
    assert "enum directshow_consumer_filter_kind" in header
    assert "DIRECTSHOW_CONSUMER_FILTER_STOCK" in header
    assert "DIRECTSHOW_CONSUMER_FILTER_PROGRAM_RETURN" in header
    assert "DIRECTSHOW_CONSUMER_FILTER_PREVIEW_RETURN" in header
    assert "GetEnvironmentVariableA" in header
    assert "runtime_id_present && !directshow_runtime_instance_id_valid(runtime_id)" in header
    assert "directshow_queue_namespace_for_consumer" in header
    assert "filter_kind == DIRECTSHOW_CONSUMER_FILTER_STOCK" in header
    assert "directshow_queue_namespace_from_environment" in producer
    assert "directshow_queue_namespace_for_consumer(filter_kind)" in consumer
    assert "OutputDebugStringW(L\"[pulsar-directshow] queue namespace rejected; consumer is disabled\\n\")" in consumer
    assert "blog(LOG_ERROR" not in consumer
    assert "+\tif (vcam->queue_namespace_rejected)\n+\t\treturn false;" in producer
    assert consumer.index("+\tif (!queue_namespace_rejected)\n+\t\tvq = video_queue_open_named") < consumer.index(
        "+\t\tvq = video_queue_open_named"
    )
    frame = _between(consumer, "void VCamFilter::Frame(uint64_t ts)", "enum queue_state state")
    assert "if (queue_namespace_rejected)" in frame
    assert frame.index("if (queue_namespace_rejected)") < frame.index(
        "+\t\tvq = video_queue_open_named"
    )

    assert "CLSID_PulsarProgramReturnVideo" in patch
    assert "CLSID_PulsarPreviewReturnVideo" in patch
    assert "new VCamFilter(filter_kind)" in patch

    root_cmake = _read(_ROOT / "CMakeLists.txt")
    assert "add_subdirectory(tests/directshow-namespace-probe)" in root_cmake


def test_take_logs_roles_frame_identity_and_stable_downstream_objects() -> None:
    source = _read(_FRONTEND)
    assert "TakeAccepted" in source
    assert "TakeCommitted" in source
    assert "frame_id=%llu" in source
    assert "pts_ns=%llu" in source
    assert "lane_root_binding_valid=%d" in source
    assert "program_main_view_valid=%d" in source
    assert "program_main_video_valid=%d" in source
    assert "preview_distinct_valid=%d" in source
    assert "laneSources[onAirLane] == currentScene" in source
    assert "programView == obs_get_main_view()" in source
    assert "programVideo == obs_get_video()" in source
    assert "programView != previewView" in source
    ready = _between(source, 'blog(LOG_INFO, "[pulsar-dual-lane] ready', "return true;")
    accepted = _between(source, 'blog(LOG_INFO, "[pulsar-dual-lane] TakeAccepted', "return true;")
    committed = _between(source, 'blog(LOG_INFO, "[pulsar-dual-lane] TakeCommitted', "self->emit(")
    assert "%p" not in ready + accepted + committed


def test_dual_lane_activation_flag_is_consumed_and_rollback_preserves_surfaces() -> None:
    source = _read(_FRONTEND)
    probe = _read(_RUNTIME_PROBE)

    # The reference campaign's environment assignment must have a matching
    # boot-time consumer.  A probe-side env var without this decision would
    # measure the wrong topology while still looking like a valid baseline.
    assert "PULSAR_DISABLE_DUAL_LANE" in source
    assert "PULSAR_DUAL_LANE_ENABLED" in source
    assert "resolve_dual_lane_activation" in source
    assert "flag_resolved_at=setup" in source
    assert "positive == EnvBool::Invalid" in source
    assert "legacyDisable == EnvBool::Invalid" in source
    assert 'return {false, "invalid-PULSAR_DUAL_LANE_ENABLED"}' in source
    assert 'return {false, "invalid-PULSAR_DISABLE_DUAL_LANE"}' in source
    assert "dualLaneEnabled = activation.enabled && !resourceReference && rollbackSetting.valid" in source
    assert '"invalid-PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES"' in source
    config = _read(_DUAL_LANE_CONFIG)
    config_test = _read(_DUAL_LANE_CONFIG_TEST)
    config_cmake = _read(_DUAL_LANE_CONFIG_CMAKE)
    rollback_parser = _between(
        source,
        "DualLaneRollbackSetting resolve_dual_lane_rollback_after_takes()",
        "class PulsarFrontendAPI;",
    )
    assert "parse_rollback_after_takes" in rollback_parser
    assert "strtoull" not in rollback_parser
    assert "ASCII digits" in config
    assert "parsed == 100000ULL / 10ULL && digitValue > 0" in config
    for value in ('""', '"0"', '"100001"', '" 1"', '"+1"', '"1.0"'):
        assert value in config_test
    for value in ('"1"', '"0001"', '"100000"'):
        assert value in config_test
    assert "add_test(NAME pulsar-dual-lane-config-probe" in config_cmake
    assert 'RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"' in config_cmake
    assert "parse_env_bool" in source
    assert "if (!*value)" in source
    assert "assert_dual_lane_activation" in probe
    assert 'required_source="resource-reference" if expected_reference else "PULSAR_DUAL_LANE_ENABLED=1"' in probe
    assert 'required_source="PULSAR_DUAL_LANE_ENABLED=1"' in probe

    # Rollback is a post-commit operational freeze.  It must leave the
    # already-selected Program route and both stable downstream identities in
    # place; changing an active video_t here would violate the ADR invariant.
    assert "PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES" in source
    assert "rollbackAfterTakes" in source
    assert "dualLaneOperational = false" in source
    assert "rollback committed at frame_id=%llu" in source
    assert "current_program_preserved=%d" in source
    assert "active_video_t_rebound=%d" in source
    assert "lane_root_binding_valid=%d" in source
    assert "program_video_stable=%d" in source
    assert "frozen=%d" in source
    assert "operational_" in source
    assert 'state_ = freezeAfterCommit ? "frozen" : "ready"' in source
    callback = _between(
        source,
        "void PulsarFrontendAPI::OnDualLaneCutCommitted",
        "bool PulsarFrontendAPI::setup()",
    )
    assert "g_dualLaneControlBridge.freeze()" in callback
    assert "g_dualLaneControlBridge.deactivate()" not in callback
    assert "obs_set_output_source(" not in callback
    assert "obs_view_set_source(" not in callback
    assert "obs_encoder_set_video(" not in callback
    rollback_probe = _read(_ROLLBACK_PROBE)
    assert "PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES" in rollback_probe
    assert "pulsar-dual-lane-rollback.json" in rollback_probe
    assert "pulsar.dual-lane-rollback.v1" in rollback_probe
    assert "current_program_preserved" in rollback_probe
    assert "stable encoder/video binding count=1" in rollback_probe
    assert "CallVendorRequest" in rollback_probe
    assert "rollback-vendor-prepare" in rollback_probe
    assert "rollback-vendor-take" in rollback_probe
    assert "rollback-vendor-dispatch" in rollback_probe
    assert "rollback-getstate-race-" in rollback_probe
    assert "ready/operational=true after the frame-boundary freeze" in rollback_probe
    assert "validate_commit(identity, None, commit)" in rollback_probe
    assert 'state_before["state"] != "frozen"' in rollback_probe
    assert "freezeAfterFrontendRollback" in source
    assert "runtime and DirectShow lease state was observable" in rollback_probe
    assert "CTRL_BREAK_EVENT" in rollback_probe
    assert "PulsarRollbackMarkerWriter" in source
    assert "g_rollbackMarkerWriter.start()" in source
    assert "g_rollbackMarkerWriter.enqueue" in source
    assert "status.dump()" in source
    assert '"runtime_instance_id", g_runtimeTelemetry.runtimeInstanceId()' in source
    callback = _between(
        source,
        "void PulsarFrontendAPI::OnDualLaneCutCommitted",
        "bool PulsarFrontendAPI::setup()",
    )
    assert "std::ofstream" not in callback
    assert "create_directories" not in callback


def test_runtime_probe_keeps_the_encoder_active_during_the_take_campaign() -> None:
    probe = _read(_RUNTIME_PROBE)
    assert "--encoder" in probe
    assert "--takes" in probe
    assert '"StartRecord"' in probe
    assert '"StopRecord"' in probe
    assert "start_directshow_consumer" in probe
    assert "Pulsar Program Return" in probe
    assert '"PULSAR_TRACE_PATH"' in probe
    assert '"PULSAR_DIRECTSHOW_LEGACY_ALIAS"' in probe
    assert '"PULSAR_LEGACY_ALIAS"] = "disabled"' in probe
    assert 'boundary="directshow_return"' in probe
    assert "assert_directshow_consumer_alive" in probe
    assert "directshow_cleanup_failure" in probe
    assert "def _join_directshow_reader" in probe
    assert "reader_failure = self._join_directshow_reader()" in probe
    assert "def _join_process_reader" in probe
    assert "assert_shutdown_clean" in probe
    assert '"PULSAR_RUNTIME_INSTANCE runtime_dir_lease=released"' in probe
    assert '"PULSAR_RUNTIME_INSTANCE lease=released"' in probe
    assert "PULSAR_LEGACY_ALIAS lease=" in probe
    assert "require_pixels=True" in probe
    assert "require_pixels=False" not in probe
    assert "OBS_WEBSOCKET_OUTPUT_STARTED" in probe
    assert "OBS_WEBSOCKET_OUTPUT_STOPPED" in probe
    assert "async def start_resource_recording" in probe
    assert "async def stop_resource_recording" in probe
    assert "active_count >= minimum_samples" in probe
    assert 'record.get("encoder_active") is True' in probe
    assert "verify_recording(output_path, ffprobe)" in probe
    resource_start = probe.index("async def start_resource_recording")
    resource_stop = probe.index("async def stop_resource_recording")
    resource_collect = probe.index("async def collect_resource_samples")
    assert resource_start < resource_stop < resource_collect
    start_helper = probe[resource_start:resource_stop]
    stop_helper = probe[resource_stop:resource_collect]
    assert start_helper.index('"StartRecord"') < start_helper.index("OBS_WEBSOCKET_OUTPUT_STARTED")
    assert stop_helper.index('"StopRecord"') < stop_helper.index("OBS_WEBSOCKET_OUTPUT_STOPPED")
    collect_body = probe[resource_collect:probe.index("async def assert_distinct_selected_scenes", resource_collect)]
    assert collect_body.index("await start_resource_recording") < collect_body.index("active_count >= minimum_samples")
    assert collect_body.index("await stop_resource_recording") < collect_body.index("verify_recording(output_path, ffprobe)")
    assert "stop_recording_after_error" in collect_body
    assert "-count_frames" in probe
    assert "encoder video_t bound once to ProgramView" in probe
    assert "lane_root_binding_valid=(\\d) program_main_view_valid=(\\d)" in probe
    assert "TakeCommitted reported an invalid surface relation" in probe
    assert "TRACE_WARMUP_TAKES = 100" in probe
    assert "total_takes = warmup_takes + takes" in probe
    assert "warmup_takes_observed" in _read(_ROOT / "scripts/probe-take-latency.py")


def test_runtime_probe_exercises_live_mutation_and_post_take_isolation() -> None:
    probe = _read(_RUNTIME_PROBE)

    assert 'INPUT_A_LIVE = "probe-dual-lane-live-A"' in probe
    assert 'INPUT_B_LIVE = "probe-dual-lane-live-B"' in probe
    assert 'INPUT_A_POST_TAKE = "probe-dual-lane-post-take-A"' in probe
    assert "await create_input(inbox, ws, SCENE_A, INPUT_A_LIVE, COLOR_BLUE_ABGR)" in probe
    assert "await create_input(inbox, ws, SCENE_B, INPUT_B_LIVE, COLOR_RED_ABGR)" in probe
    assert "await create_input(inbox, ws, SCENE_A, INPUT_A_POST_TAKE, COLOR_GREEN_ABGR)" in probe
    assert "assert_distinct_selected_scenes(" in probe
    assert "GetCurrentProgramScene" in probe
    assert "GetCurrentPreviewScene" in probe


def test_websocket_mutation_gate_is_central_and_fail_closed() -> None:
    bridge = _read(_CONTROL_BRIDGE)
    frontend = _read(_FRONTEND)
    handler = _read(_WEBSOCKET_HANDLER)

    assert 'kMutationEnterProc[] = "pulsar_dual_lane_mutation_enter"' in bridge
    assert 'kMutationLeaveProc[] = "pulsar_dual_lane_mutation_leave"' in bridge
    assert "class MutationLease" in bridge
    assert "proc_handler_call(procHandler_, kMutationEnterProc" in bridge
    assert "proc_handler_call(procHandler_, kMutationLeaveProc" in bridge
    assert "g_dualLaneControlBridge" in frontend
    assert "std::mutex dispatchMutex_" in frontend
    assert "std::atomic<bool> pending_" in frontend
    assert "Lifecycle::ShuttingDown" in frontend
    assert "out bool frozen" in frontend
    assert "bool frozen() const" in bridge
    assert "void freeze()" in frontend
    assert "g_dualLaneControlBridge.set_pending(true)" in frontend
    assert "g_dualLaneControlBridge.set_pending(false)" in frontend
    assert "g_dualLaneControlBridge.deactivate()" in frontend

    assert '#include "pulsar-dual-lane-control.h"' in handler
    assert 'requestType.rfind("Get", 0) == 0' in handler
    assert "IsControlledSceneSwitchPendingBypass" in handler
    assert 'request.RequestType != "CallVendorRequest"' in handler
    assert 'vendor->get<std::string>() != "pulsar-scene-switch"' in handler
    assert 'return nested == "Abort" || nested == "GetState"' in handler
    assert "vendor->is_string() || !nestedRequest->is_string()" in handler
    assert "json::value() here" in handler
    assert "const bool controlledSceneSwitchBypass" in handler
    assert "!IsReadOnlyRequest(request.RequestType) && !controlledSceneSwitchBypass" in handler
    assert "RequestStatus::RequestProcessingFailed" in handler
    assert "PREVIEW_FROZEN" in handler
    assert "after the dual-lane rollback freeze" in handler
    # The gate is central and acquired before handler lookup. The only pending
    # bypass is the exact vendor Abort/GetState pair; Prepare/Take/Dispatch,
    # malformed CallVendorRequest data, and every other vendor remain gated.
    assert 'nested == "Prepare"' not in handler
    assert 'nested == "Take"' not in handler
    assert 'nested == "Dispatch"' not in handler
    assert handler.index("const bool controlledSceneSwitchBypass") < handler.index("_handlerMap.at")


def test_runtime_probe_exercises_serial_frame_preview_freeze() -> None:
    probe = _read(_RUNTIME_PROBE)

    assert "async def request_batch(" in probe
    assert '"op": 8' in probe
    assert '"executionType": execution_type' in probe
    assert '"requestType": "TriggerStudioModeTransition"' in probe
    assert '"requestType": "CreateInput"' in probe
    assert 'INPUT_B_FROZEN = "probe-dual-lane-frozen-B"' in probe
    assert "assert_preview_frozen" in probe
    assert "status.get(\"code\") != 702" in probe
    assert '"PREVIEW_FROZEN" not in comment' in probe
    assert '"sleepFrames": 30' in probe
    assert "post-commit Preview after 30 frames" in probe


def test_runtime_probe_parses_bracket_and_separator_dual_lane_logs() -> None:
    probe = _load_probe(_RUNTIME_PROBE, "pulsar_dual_lane_probe_for_contract")
    fields = (
        "LaneA=lane-a LaneB=lane-b lane_root_binding_valid=1 "
        "program_main_view_valid=1 program_main_video_valid=1 preview_distinct_valid=1"
    )
    for prefix in ("[pulsar-dual-lane] ready", "pulsar-dual-lane | ready"):
        match = probe.DUAL_READY_RE.search(f"{prefix} {fields}")
        assert match is not None
        assert probe.parse_ready(match).program_main_view_valid == 1
        bind_match = probe.ENCODER_BIND_RE.search(
            f"{prefix.split(' ready', 1)[0]} encoder video_t bound once to ProgramView"
        )
        assert bind_match is not None

    commit_fields = (
        "count=1 frame_id=42 pts_ns=9001 onair_lane=0 preview_lane=1 "
        "lane_root_binding_valid=1 program_main_view_valid=1 "
        "program_main_video_valid=1 preview_distinct_valid=1"
    )
    for prefix in ("[pulsar-dual-lane] TakeCommitted", "pulsar-dual-lane | TakeCommitted"):
        match = probe.COMMIT_RE.search(f"{prefix} {commit_fields}")
        assert match is not None
        assert probe.parse_commit(match).frame_id == 42

    invalid_commit = probe.COMMIT_RE.search(
        "[pulsar-dual-lane] TakeCommitted count=1 frame_id=42 pts_ns=9001 "
        "onair_lane=0 preview_lane=1 lane_root_binding_valid=0 "
        "program_main_view_valid=1 program_main_video_valid=1 preview_distinct_valid=1"
    )
    assert invalid_commit is not None
    invalid_ready = probe.DUAL_READY_RE.search("[pulsar-dual-lane] ready " + fields)
    assert invalid_ready is not None
    with pytest.raises(probe.ProbeFailure, match="invalid surface relation"):
        probe.validate_commit(probe.parse_ready(invalid_ready), None, probe.parse_commit(invalid_commit))

    source = _read(_RUNTIME_PROBE)
    assert "ENCODER_BIND_RE.search(line)" in source
    assert '"[pulsar-dual-lane] encoder video_t bound once to ProgramView"' not in source


def test_runtime_probe_redacts_ready_credentials_from_failure_tails() -> None:
    probe = _load_probe(_RUNTIME_PROBE, "pulsar_dual_lane_probe_redaction")
    fixture_value = "controlled-redaction-fixture"
    tail = probe.failure_tail([f"PULSAR_READY ws=ws://127.0.0.1 password={fixture_value}"], 40)
    assert fixture_value not in tail
    assert "password=[redacted]" in tail


def test_output_effect_probe_settles_record_stop_before_next_case() -> None:
    source = _read(_OUTPUT_EFFECT_PROBE)
    helper = _between(source, "async def wait_record_and_output_inactive", "async def case_replay")
    nominal_record = _between(source, "async def case_record_nominal", "async def case_generic_refused")
    nominal_generic = _between(source, "async def case_generic_nominal", "def assert_bounded")

    assert "GetOutputStatus" in helper
    assert 'c.req("GetRecordStatus")' in helper
    assert "STOP_SETTLE_S" in helper
    assert "await asyncio.sleep(0.2)" in helper
    assert 'wait_record_and_output_inactive(c, GENERIC_RECORD_OUTPUT, "StopRecord(nominal)")' in nominal_record
    assert 'wait_record_and_output_inactive(c, GENERIC_RECORD_OUTPUT, "StopOutput(nominal)")' in nominal_generic


def test_core_swap_is_frame_boundary_and_rejects_concurrent_requests() -> None:
    patch = _read(_DUAL_LANE_PATCH)

    assert "pthread_cond_t atomic_swap_cond;" in patch
    assert "bool atomic_swap_initialized;" in patch
    assert "bool atomic_swap_inflight;" in patch
    assert "pthread_cond_wait(&obs->video.atomic_swap_cond" in patch
    assert "pthread_cond_broadcast(&obs->video.atomic_swap_cond)" in patch
    assert "pthread_equal(pthread_self(), obs->video.video_thread)" in patch
    assert "if (obs->video.pending_atomic_swap) {" in patch
    assert "obs->video.pending_atomic_swap || obs->video.atomic_swap_inflight" not in patch
    assert "a successor may queue while the previous callback is" in patch
    assert "+obs_view_t *obs_get_main_view(void)" in patch
    assert "+EXPORT obs_view_t *obs_get_main_view(void);" in patch

    apply_at = patch.index("obs_view_apply_pending_atomic_swap(++obs->video.video_frame_id")
    output_at = patch.index("output_frames();", apply_at)
    assert apply_at < output_at
    assert "obs->video.atomic_swap_inflight = true;" in patch
    assert "obs->video.atomic_swap_inflight = false;" in patch


def test_teardown_drains_in_flight_swap_before_destroying_views() -> None:
    source = _read(_FRONTEND)
    teardown = _between(
        source,
        "void PulsarFrontendAPI::teardown()",
        "void PulsarFrontendAPI::emit(obs_frontend_event event)",
    )

    ready_false = teardown.index("dualLaneReady = false;")
    pending_false = teardown.index("dualLaneCutPending.store(false);")
    cancel = teardown.index("obs_view_cancel_atomic_swap();")
    program_clear = teardown.index("programView = nullptr;")
    preview_destroy = teardown.index("obs_view_destroy(previewView)")
    assert ready_false < pending_false < cancel < program_clear
    assert cancel < preview_destroy
    assert "obs_view_remove(programView)" not in teardown
    assert "obs_view_destroy(programView)" not in teardown
    assert "dualLaneCutPending.store(false);" in teardown
