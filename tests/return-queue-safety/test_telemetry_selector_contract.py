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
