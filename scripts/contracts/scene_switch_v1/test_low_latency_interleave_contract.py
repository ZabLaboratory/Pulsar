"""Static contract for Pulsar's opt-in low-latency libobs interleaver."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "patches" / "0012-feat-libobs-add-low-latency-output-interleaving.patch"
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
