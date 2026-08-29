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


_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND = _ROOT / "plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp"
_DUAL_LANE_PATCH = _ROOT / "patches/0009-feat-libobs-add-frame-boundary-dual-lane-swaps.patch"
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
    assert "OBS_SCENE_DUP_PRIVATE_COPY" in source
    assert "obs_scene_duplicate(obs_scene_from_source(scene)" in source
    assert "obs_scene_add(laneScene, contentSource)" in source
    assert "laneSources[onAirLane] == currentScene" in source
    assert "laneSources[previewLane] == previewScene" in source
    assert "laneItems[0] && laneItems[1]" in source
    assert "previewView && programVideo && previewVideo" in source
    assert "programView == obs_get_main_view()" in source
    assert "programVideo == obs_get_video()" in source
    assert "std::swap(self->onAirLane, self->previewLane)" in source
    assert "std::swap(self->currentScene, self->previewScene)" in source


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


def test_take_logs_roles_frame_identity_and_stable_downstream_objects() -> None:
    source = _read(_FRONTEND)
    assert "TakeAccepted" in source
    assert "TakeCommitted" in source
    assert "frame_id=%llu" in source
    assert "pts_ns=%llu" in source
    assert "OnAirRoot=%p PreviewRoot=%p" in source
    assert "ProgramView=%p PreviewView=%p ProgramVideo=%p PreviewVideo=%p " in source
    assert "MainView=%p MainVideo=%p" in source
    assert "obs_get_main_view()" in source
    assert "obs_get_video()" in source


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
    assert "MainView=(\\S+) MainVideo=(\\S+)" in probe
    assert "ProgramView is not the libobs main view" in probe


def test_runtime_probe_parses_bracket_and_separator_dual_lane_logs() -> None:
    probe = _load_probe(_RUNTIME_PROBE, "pulsar_dual_lane_probe_for_contract")
    fields = (
        "LaneA=0x1 LaneB=0x2 ProgramView=0x3 PreviewView=0x4 "
        "ProgramVideo=0x5 PreviewVideo=0x6 MainView=0x3 MainVideo=0x5"
    )
    for prefix in ("[pulsar-dual-lane] ready", "pulsar-dual-lane | ready"):
        match = probe.DUAL_READY_RE.search(f"{prefix} {fields}")
        assert match is not None
        assert probe.parse_ready(match).program_view == "3"

    commit_fields = (
        "count=1 frame_id=42 pts_ns=9001 onair_lane=0 preview_lane=1 "
        "OnAirRoot=0x1 PreviewRoot=0x2 ProgramView=0x3 PreviewView=0x4 "
        "ProgramVideo=0x5 PreviewVideo=0x6 MainView=0x3 MainVideo=0x5"
    )
    for prefix in ("[pulsar-dual-lane] TakeCommitted", "pulsar-dual-lane | TakeCommitted"):
        match = probe.COMMIT_RE.search(f"{prefix} {commit_fields}")
        assert match is not None
        assert probe.parse_commit(match).frame_id == 42


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
    assert "obs->video.pending_atomic_swap || obs->video.atomic_swap_inflight" in patch
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
