"""Contract checks for the opt-in NVENC ready-batch experiment.

Runtime acceptance additionally requires real NVENC, decoded B-frame ordering,
audio continuity, and paired latency runs; source checks cannot replace them.
"""
from pathlib import Path


ROOT = Path(__file__).parents[2]
PATCH = ROOT / "patches/0051-perf-nvenc-drain-ready-batches.patch"


def added_source():
    return "\n".join(line[1:] for line in PATCH.read_text().splitlines()
                     if line.startswith("+") and not line.startswith("+++"))


def test_ready_batches_require_driver_success_and_explicit_opt_in():
    source = added_source()
    assert 'getenv("PULSAR_NVENC_READY_DRAIN")' in source
    assert 'strcmp(ready_drain, "1") == 0' in source
    assert "enc->codec == CODEC_H264 && !enc->non_texture" in source
    assert "enc->ready_drain && err == NV_ENC_SUCCESS" in source
    assert "enc->buffers_ready = enc->buffers_queued" in source
    assert "!enc->buffers_ready" in source
    assert "enc->buffers_ready--" in source


def test_callback_is_optional_bounded_and_after_timing_registration():
    source = added_source()
    assert "!encoder->info.get_pending_packet" in source
    assert "i < 64" in source
    assert "pkt.timebase_num = encoder->timebase_num * encoder->frame_rate_divisor" in source
    assert "send_off_encoder_packet(encoder, success, received, &pkt)" in source
    assert "drain_encoder_packets(encoder)" in source
    assert "bool (*get_pending_packet)" in source


def test_experiment_does_not_disable_compression_tools_or_shrink_pool():
    source = added_source()
    for forbidden in ("props.bf =", "enableLookahead =", "multiPass =",
                      "averageBitRate =", "enc->buf_count ="):
        assert forbidden not in source
    assert "lock.doNotWait = true" not in source
