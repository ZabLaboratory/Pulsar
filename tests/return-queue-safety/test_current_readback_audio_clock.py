"""Clock compensation is restricted to the pipeline whose queue was removed."""
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_automatic_promotion_is_bounded_to_qualified_cpu_pipeline():
    patch = (ROOT / "patches/0055-perf-libobs-default-qualified-current-readback.patch").read_text()
    hardware_guard = (ROOT / "patches/0056-fix-libobs-auto-readback-hardware-gate.patch").read_text()
    assert "#ifdef _WIN32" in patch
    assert "current_readback_mode = !setting ? 2 : strcmp(setting, \"1\") == 0;" in patch
    assert "current_readback = !gpu_active" in patch
    assert "readback_info->width == 1920 && readback_info->height == 1080" in patch
    assert "readback_info->fps_num == 60 && readback_info->fps_den == 1" in patch
    assert "readback_info->format == VIDEO_FORMAT_NV12" in patch
    assert "hardware_readback_capable = gs_get_adapter_count() > 0" in hardware_guard
    assert "gs_enter_context(obs->video.graphics);" in hardware_guard
    assert hardware_guard.index("gs_enter_context(obs->video.graphics);") < hardware_guard.index(
        "hardware_readback_capable = gs_get_adapter_count() > 0"
    )
    assert "hardware_readback_capable && qualified_readback" in hardware_guard
    assert hardware_guard.count("gs_get_adapter_count()") == 1
    additions = [line for line in hardware_guard.splitlines() if line.startswith("+") and not line.startswith("+++")]
    assert not any("PULSAR_RAW_CURRENT_READBACK" in line for line in additions)


def test_ci_artifact_carries_native_d3d11_effect_bundle():
    pipeline = (ROOT / ".github/workflows/pipeline.yml").read_text()
    assert "build/tests/nv-probe/**" in pipeline
    assert "build/tests/data/libobs/**" in pipeline


def test_current_cpu_surface_advances_one_cadence_interval_without_changing_count():
    patch = (ROOT / "patches/0053-fix-libobs-current-readback-audio-alignment.patch").read_text()
    assert "+\t\tif (current_readback && !gpu_active)" in patch
    assert "+\t\t\tframe.timestamp += video_output_get_frame_time(video->video);" in patch
    additions = [line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    assert not any("vframe_info.count =" in line or "content_pts_ns =" in line for line in additions)


def test_native_gpu_failure_cannot_wait_on_its_own_inactive_barrier():
    patch = (ROOT / "patches/0054-fix-libobs-gpu-error-stop-lifecycle.patch").read_text()
    assert "pthread_equal(pthread_self(), video->gpu_encode_thread)" in patch
    assert "obs_queue_task(OBS_TASK_DESTROY, gpu_error_stop_task, ref, false)" in patch
    assert "os_atomic_exchange_bool(&encoder->gpu_error_stop_pending, true)" in patch
    assert "if (encoder_active(encoder))" in patch
    assert "obs_output_get_ref" in patch and "obs_encoder_release(encoder)" in patch
