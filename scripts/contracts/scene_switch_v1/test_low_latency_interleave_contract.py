"""Static contract for Pulsar's opt-in low-latency libobs interleaver."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "patches" / "0012-feat-libobs-add-low-latency-output-interleaving.patch"
VIDEO_FASTPATH_PATCH = (
    ROOT / "patches" / "0013-perf-libobs-decouple-live-video-from-audio-watermark.patch"
)
NVENC_DRAIN_PATCH = ROOT / "patches" / "0014-perf-nvenc-drain-ULL-bitstreams-immediately.patch"
FRONTEND = ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp"


def test_patch_is_opt_in_and_preserves_timestamp_admission() -> None:
    patch = PATCH.read_text(encoding="utf-8")

    assert "obs_output_set_low_latency_interleave" in patch
    assert "obs_output_get_low_latency_interleave" in patch
    assert "size_t streamable = count_streamable_frames(output);" in patch
    assert "with a higher DTS" in patch
    assert "if (os_atomic_load_bool(&output->low_latency_interleave))" in patch
    assert "while (streamable--)" in patch
    assert "} else if (streamable) {" in patch
    assert "interleaver_max_batch_size" in patch


def test_only_the_live_stream_output_opts_in() -> None:
    frontend = FRONTEND.read_text(encoding="utf-8")

    assert "obs_output_set_low_latency_interleave(streamOutput, true);" in frontend
    assert "obs_output_set_low_latency_interleave(recordOutput" not in frontend
    assert "obs_output_set_low_latency_interleave(replayOutput" not in frontend


def test_video_fastpath_keeps_audio_and_orders_video_tracks() -> None:
    patch = VIDEO_FASTPATH_PATCH.read_text(encoding="utf-8")

    assert "pkt->type == OBS_ENCODER_VIDEO" in patch
    assert "output->highest_video_ts[i] > pkt->dts_usec" in patch
    assert "Audio remains encoded" in patch
    assert "has_higher_opposing_ts(output, pkt)" in patch
    assert "obs_output_set_audio_encoder" not in patch


def test_nvenc_low_latency_profile_is_explicitly_opt_in() -> None:
    frontend = FRONTEND.read_text(encoding="utf-8")

    assert 'std::strcmp(reportFamily, "nvenc") == 0' in frontend
    assert 'std::getenv("PULSAR_NVENC_LOW_LATENCY")' in frontend
    assert "nvencLowLatency == EnvBool::Unset || nvencLowLatency == EnvBool::Enabled" in frontend
    assert 'obs_data_set_string(vEncSettings, "tune", "ull");' in frontend
    assert 'obs_data_set_string(vEncSettings, "multipass", "disabled");' not in frontend
    assert 'obs_data_set_bool(vEncSettings, "lookahead", false);' not in frontend
    assert 'obs_data_set_int(vEncSettings, "bf", 0);' not in frontend
    assert "NVENC latency profile" in frontend
    assert "NVENC quality profile preserved" in frontend


def test_nvenc_ull_drains_without_shrinking_the_surface_pool() -> None:
    patch = NVENC_DRAIN_PATCH.read_text(encoding="utf-8")

    assert "const int output_delay =" in patch
    assert "NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY" in patch
    assert "max(3, config->frameIntervalP)" in patch
    assert "max(4, config->frameIntervalP)" not in patch
    assert "max(1, config->frameIntervalP)" not in patch
    assert "enc->buf_count = buf_count;" in patch
    assert "buf_count = min(64, buf_count);" in patch
