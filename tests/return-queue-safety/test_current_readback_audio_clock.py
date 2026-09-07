"""Clock compensation is restricted to the pipeline whose queue was removed."""
from pathlib import Path

ROOT = Path(__file__).parents[2]


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
