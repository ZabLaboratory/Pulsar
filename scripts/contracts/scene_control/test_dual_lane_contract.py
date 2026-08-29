"""Static contract tests for the Pulsar dual-lane implementation.

The Windows/GPU probe is intentionally separate: it must exercise a built
Pulsar binary with x264 and NVENC.  These tests run without that environment
and protect the source-level invariants that make the probe meaningful:
physical A/B roots, stable downstream identities, a frame-boundary pair swap,
and a teardown barrier for an extracted (in-flight) swap.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND = _ROOT / "plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp"
_CONTROL_BRIDGE = _ROOT / "plugins/pulsar-frontend-stub/include/pulsar-dual-lane-control.h"
_WEBSOCKET_HANDLER = _ROOT / "plugins/pulsar-websocket/src/requesthandler/RequestHandler.cpp"
_DUAL_LANE_PATCH = _ROOT / "patches/0009-feat-libobs-add-frame-boundary-dual-lane-swaps.patch"
_DIRECTSHOW_NAMESPACE_PATCH = _ROOT / "patches/0010-fix-win-dshow-reject-ambiguous-queue-namespaces.patch"
_RUNTIME_PROBE = _ROOT / "scripts/probe-dual-lane.py"
_OUTPUT_EFFECT_PROBE = _ROOT / "scripts/probe-output-effect.py"


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


def test_runtime_probe_keeps_the_encoder_active_during_the_take_campaign() -> None:
    probe = _read(_RUNTIME_PROBE)
    assert "--encoder" in probe
    assert "--takes" in probe
    assert '"StartRecord"' in probe
    assert '"StopRecord"' in probe
    assert "OBS_WEBSOCKET_OUTPUT_STARTED" in probe
    assert "OBS_WEBSOCKET_OUTPUT_STOPPED" in probe
    assert "-count_frames" in probe
    assert "encoder video_t bound once to ProgramView" in probe
    assert "lane_root_binding_valid=(\\d) program_main_view_valid=(\\d)" in probe
    assert "TakeCommitted reported an invalid surface relation" in probe


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
