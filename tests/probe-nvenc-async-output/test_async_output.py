"""Complement real GPU/NVENC tests with fail-closed scheduling guards."""
from pathlib import Path

ROOT = Path(__file__).parents[2]
PATCH = ROOT / "patches/0052-perf-nvenc-service-asynchronous-output.patch"


def added_source():
    return "\n".join(line[1:] for line in PATCH.read_text().splitlines()
                     if line.startswith("+") and not line.startswith("+++"))


def test_completion_requires_driver_event_before_nonblocking_lock():
    source = added_source()
    assert 'getenv("PULSAR_NVENC_ASYNC_OUTPUT")' in source
    assert "NV_ENC_CAPS_ASYNC_ENCODE_SUPPORT" in source
    assert "enc->params.enableEncodeAsync = 1" in source
    assert "nv.nvEncRegisterAsyncEvent" in source
    assert "params.completionEvent = bs->completion_event" in source
    assert "WaitForSingleObject(bs->completion_event" in source
    assert "bs->completion_ready = true" in source
    assert "lock.doNotWait = enc->async_output && !must_wait" in source
    assert "NV_ENC_ERR_LOCK_BUSY" in source
    assert "nv.nvEncUnregisterAsyncEvent" in source


def test_timed_completion_service_preserves_frame_queue_and_retirement():
    source = added_source()
    timeout = source.index("if (wait_result == ETIMEDOUT)")
    tail = source.index("if (wait_result != 0)", timeout)
    timed_service = source[timeout:tail]
    assert "deque_pop_front" not in timed_service
    assert "video_output_inc_texture_frames" not in timed_service
    assert "os_event_reset(video->gpu_encode_inactive)" in timed_service
    assert "pthread_mutex_lock(&video->gpu_encoder_mutex)" in timed_service
    assert "drain_encoder_packets" in timed_service
    assert "os_event_signal(video->gpu_encode_inactive)" in timed_service
    assert "obs_encoder_release" in timed_service
    assert "Asynchronous input surface has not been retired before reuse" in source
    assert "enc->buffers_queued >= (int)enc->buf_count" in source
    for forbidden in ("props.bf =", "averageBitRate =", "enableLookahead =", "enc->buf_count ="):
        assert forbidden not in source
