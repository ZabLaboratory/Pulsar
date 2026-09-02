"""Static contract checks for boot-time telemetry selection and schema safety."""

from pathlib import Path


ROOT = Path(__file__).parents[2]
HEADER = ROOT / "plugins" / "pulsar-frontend-stub" / "include" / "pulsar-runtime-telemetry-signals.h"
FRONTEND = ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp"
PROTOCOL = ROOT / "docs" / "PROTOCOL.md"


def test_selector_is_boot_fixed_and_fail_closed() -> None:
    source = FRONTEND.read_text(encoding="utf-8")
    header = HEADER.read_text(encoding="utf-8")
    assert 'std::getenv("PULSAR_TRACE_SIGNALS")' in source
    assert "parse_signal_selection" in source
    assert "signalMask_.store(signalSelection.mask" in source
    assert "signalMask_.load(std::memory_order_acquire)" in source
    for token in ("all", "none", "program", "preview", "raw", "borrowed", "gpu", "queues"):
        assert f'"{token}"' in header
    for marker in ("unknown PULSAR_TRACE_SIGNALS token", "empty CSV token", "duplicate PULSAR_TRACE_SIGNALS token"):
        assert marker in header


def test_each_signal_controls_a_real_producer_or_payload() -> None:
    source = FRONTEND.read_text(encoding="utf-8")
    for signal in ("Program", "Preview", "Raw", "Borrowed", "Gpu", "Queues"):
        assert f"Signal::{signal}" in source
    assert "if (programSelected)" in source
    assert "if (previewSelected)" in source
    assert "if (rawSelected)" in source
    assert "if (borrowedSelected)" in source
    assert "if (gpuSelected)" in source
    assert "if (queuesSelected)" in source
    assert "queuesSelected ? static_cast<uint64_t>(obs_get_lagged_frames()) : 0" in source
    assert "cpuSamplingRequired ? os_cpu_usage_info_start() : nullptr" in source
    assert "const double frameRenderMs = (programSelected || previewSelected)" in source
    assert "if ((programSelected || borrowedSelected)" in source
    assert "if ((previewSelected || borrowedSelected)" in source
    assert "if (selectedSignals != pulsar_runtime_telemetry::all_signal_mask())" in source
    assert "if (selectedSignals == 0)" in source
    assert "telemetry_signals" in source and "evidence_kind" in source
    assert "obs_video_add_borrowed_callback" in source
    assert "obs_output_add_packet_callback" in source
    filtered_start = source.index("if (selectedSignals != pulsar_runtime_telemetry::all_signal_mask())")
    filtered_end = source.index("std::ostringstream sample;", filtered_start)
    filtered = source[filtered_start:filtered_end]
    for signal in ("program", "preview", "raw", "borrowed", "gpu", "queues"):
        assert f'payload("{signal}"' in filtered
    assert '"program_mix"' not in filtered
    assert '"preview_mix"' not in filtered


def test_mandatory_lifecycle_fields_remain_outside_selector_gates() -> None:
    source = FRONTEND.read_text(encoding="utf-8")
    assert 'commonEventFields("TakeAccepted"' in source
    assert 'commonEventFields("TakeCommitted"' in source
    assert '"frame_id"' in source and '"pts_ns"' in source
    assert "telemetry_signals_mask" in source
    assert "telemetry_signals" in source
    for capture_path in (
        "encoder_input_raw",
        "directshow_return",
        "encoded_first_packet",
        "decoded_first_frame",
        "antenna_first_frame",
    ):
        assert capture_path in source
    assert "PULSAR_TRACE_SIGNALS" in PROTOCOL.read_text(encoding="utf-8")
