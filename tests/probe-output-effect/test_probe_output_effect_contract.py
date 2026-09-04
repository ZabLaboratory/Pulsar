"""Contracts for the output-effect probe's latency oracle."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "probe-output-effect.py"


def test_generic_positive_control_uses_non_muxer_and_record_stays_separate():
    source = SCRIPT.read_text(encoding="utf-8")
    case_start = source.index("async def case_generic_nominal")
    case_end = source.index("\ndef assert_bounded", case_start)
    case = source[case_start:case_end]

    assert 'GENERIC_RAW_OUTPUT = "PulsarVCam"' in source
    assert 'GENERIC_RAW_OUTPUT})' in case
    assert 'await c.req("StartVirtualCam")' in case
    assert 'await c.req("StartRecord")' not in case
    assert "wait_record_and_output_inactive" not in case
    assert 'await c.req("StopOutput", {"outputName": GENERIC_RAW_OUTPUT})' in case


def test_record_effect_control_and_request_latency_bound_remain_explicit():
    source = SCRIPT.read_text(encoding="utf-8")
    record_case = source[source.index("async def case_record_nominal"):source.index(
        "async def case_generic_refused")]

    assert 'await c.req("StartRecord")' in record_case
    assert 'await c.req("StopRecord")' in record_case
    assert "MAX_REQUEST_MS = 2500" in source
    assert "ffmpeg_muxer flush" in source
