"""Deterministic contract tests for the v5 record action boundary.

The real websocket handler is native and is exercised by the Windows CTest
campaign.  These tests keep the response contract executable without a native
build: source assertions pin the native decision order and a tiny model drives
the pause/replay/stop sequence, including the audio-first edge case.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
RECORD_HANDLER = ROOT / "plugins" / "pulsar-websocket" / "src" / "requesthandler" / "RequestHandler_Record.cpp"
OUTPUT_EFFECT = ROOT / "plugins" / "pulsar-websocket" / "src" / "requesthandler" / "OutputEffect.h"
OUTPUT_HELPER = ROOT / "plugins" / "pulsar-websocket" / "src" / "utils" / "Obs_OutputHelper.cpp"
OBS_HEADER = ROOT / "plugins" / "pulsar-websocket" / "src" / "utils" / "Obs.h"
RECORD_PROBE = ROOT / "scripts" / "probe-record.py"
RECORD_M2_PROBE = ROOT / "scripts" / "probe-record-m2.py"
RECORD_SPLIT_PROBE = ROOT / "scripts" / "probe-record-split.py"


class _StopVerdict(Enum):
    LANDED = "landed"
    PENDING = "pending"
    REFUSED = "refused"


@dataclass
class _RecordModel:
    """Small state model for the observable v5 record contract."""

    active: bool = True
    paused: bool = False
    replay_active: bool = False
    output_bytes: int = 128  # audio may make this non-zero before video arrives
    output_total_frames: int = 0

    def pause(self) -> tuple[bool, int | None]:
        if self.output_total_frames == 0:
            return False, 604
        self.paused = True
        return True, None

    def resume(self) -> tuple[bool, int | None]:
        self.paused = False
        return True, None

    def stop(self, *, accepted: bool, active_samples: tuple[bool, ...]) -> _StopVerdict:
        if not accepted:
            return _StopVerdict.REFUSED
        if any(not active for active in active_samples):
            self.active = False
            return _StopVerdict.LANDED
        return _StopVerdict.PENDING


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    finish = text.index(end, begin)
    return text[begin:finish]


def test_record_stop_response_has_no_success_path_for_pending() -> None:
    handler = RECORD_HANDLER.read_text(encoding="utf-8")
    stop = _between(handler, "RequestResult RequestHandler::StopRecord", "/**\n * Toggles pause on the record output.")

    settle = stop.index("SettleRecordStop")
    refused = stop.index("verdict == ActionVerdict::Refused", settle)
    pending = stop.index("verdict == ActionVerdict::Pending", refused)
    pending_response = stop.index("OutputStopPending", pending)
    response_data = stop.index("json responseData", pending_response)
    success = stop.index("RequestResult::Success(responseData)", response_data)

    assert settle < refused < pending < pending_response < response_data < success
    assert "OutputStopPending" in OUTPUT_EFFECT.read_text(encoding="utf-8")
    assert "RequestStatus::RequestProcessingFailed" in OUTPUT_EFFECT.read_text(encoding="utf-8")
    assert "no completed path is available in this response" in OUTPUT_EFFECT.read_text(encoding="utf-8")


def test_record_stop_uses_long_but_bounded_flush_settlement() -> None:
    helper = OUTPUT_HELPER.read_text(encoding="utf-8")
    header = OBS_HEADER.read_text(encoding="utf-8")

    assert "RECORD_STOP_VERIFY_TIMEOUT_MS = 2500" in helper
    assert "RecordStopVerifyTimeoutMs()" in helper
    assert "SettleRecordStop" in helper
    assert "SettleRecordStop" in header
    assert "return settle(output, watch, false, false, Utils::Obs::OutputHelper::RecordStopVerifyTimeoutMs())" in helper


def test_pause_replay_stop_is_safe_for_audio_first_and_all_stop_verdicts() -> None:
    record = _RecordModel()

    # AAC/audio can account for bytes before the first video frame.  That
    # state must refuse PauseRecord instead of entering libobs's bad timeline.
    assert record.output_bytes > 0
    assert record.output_total_frames == 0
    assert record.pause() == (False, 604)
    assert record.paused is False

    # Once a video frame has reached the record output, the normal sequence is
    # pause -> resume while the replay buffer is armed -> stop.
    record.output_total_frames = 1
    assert record.pause() == (True, None)
    record.replay_active = True
    assert record.resume() == (True, None)
    assert record.replay_active is True
    assert record.stop(accepted=True, active_samples=(True, True)) is _StopVerdict.PENDING
    assert record.active is True

    # The accepted stop may land later; only this observation is allowed to
    # produce the completed-file Success response/path.
    assert record.stop(accepted=True, active_samples=(True, False)) is _StopVerdict.LANDED
    assert record.active is False
    assert record.stop(accepted=False, active_samples=()) is _StopVerdict.REFUSED


def test_pause_guard_and_status_expose_video_frame_readiness() -> None:
    handler = RECORD_HANDLER.read_text(encoding="utf-8")

    assert "RecordOutputHasNoVideoFramesYet" in handler
    assert "obs_output_get_total_frames(output) == 0" in handler
    assert "outputTotalFrames" in handler
    assert "@responseField outputTotalFrames" in handler
    assert "RecordOutputHasNoBytesYet" not in handler
    assert "kPauseBeforeFirstVideoFrame" in handler


def test_record_probes_drain_pending_before_shared_process_reuse() -> None:
    record_probe = RECORD_PROBE.read_text(encoding="utf-8")
    record_m2_probe = RECORD_M2_PROBE.read_text(encoding="utf-8")
    split_probe = RECORD_SPLIT_PROBE.read_text(encoding="utf-8")

    for probe in (record_probe, split_probe):
        assert "STOP_PENDING_CODE = 702" in probe
        assert "RecordStateChanged" in probe
        assert "OBS_WEBSOCKET_OUTPUT_STOPPED" in probe
        assert "GetRecordStatus" in probe
        assert "outputActive" in probe
        assert "did not emit STOPPED" in probe
        assert "outputActive stayed true" in probe

    assert "STOP_PENDING_CODE = 702" in record_m2_probe
    assert "wait_record_stop(inbox, ws, r, record_dir)" in record_m2_probe
    assert "outputPath is stale or outside this run" in record_m2_probe
    assert "outputActive stayed true" in record_m2_probe

    assert "wait_record_stop(inbox, ws, resp)" in record_probe
    assert '"stop-recovery"' in split_probe
    assert "if not await wait_record_stop" in split_probe


def _load_probe(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeWs:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = [json.dumps(message) for message in messages]

    async def send(self, _: str) -> None:
        return None

    async def recv(self) -> str:
        if not self._messages:
            raise AssertionError("fake websocket was read after the bounded test messages ended")
        return self._messages.pop(0)


def test_record_probe_pending_waits_for_late_stop_and_rechecks_state() -> None:
    probe = _load_probe(RECORD_PROBE, "record_probe_stop_contract")
    inbox = probe.Inbox()
    ws = _FakeWs(
        [
            {
                "op": 5,
                "d": {
                    "eventType": "RecordStateChanged",
                    "eventData": {
                        "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED",
                        "outputPath": "final.mp4",
                    },
                },
            },
            {
                "op": 7,
                "d": {
                    "requestId": "stop-status-1",
                    "responseData": {"outputActive": False},
                },
            },
        ]
    )
    response = {"requestStatus": {"result": False, "code": 702, "comment": "still flushing"}}

    event = asyncio.run(probe.wait_record_stop(inbox, ws, response))

    assert event is not None
    assert event["eventData"]["outputPath"] == "final.mp4"
    assert inbox.responses == []


def test_record_probe_pending_timeout_and_refusal_never_fabricate_a_path() -> None:
    probe = _load_probe(RECORD_PROBE, "record_probe_stop_contract_failure")
    probe.STOP_EVENT_TIMEOUT_SEC = 0.0

    pending = {"requestStatus": {"result": False, "code": 702, "comment": "still active"}}
    assert asyncio.run(probe.wait_record_stop(probe.Inbox(), _FakeWs([]), pending)) is None

    refused = {"requestStatus": {"result": False, "code": 500, "comment": "refused"}}
    assert asyncio.run(probe.wait_record_stop(probe.Inbox(), _FakeWs([]), refused)) is None


def test_record_m2_pending_702_requires_stopped_inactive_and_current_path(tmp_path: Path) -> None:
    probe = _load_probe(RECORD_M2_PROBE, "record_m2_stop_contract")
    output = tmp_path / "final.mp4"
    output.write_bytes(b"mp4")
    inbox = probe.Inbox()
    ws = _FakeWs(
        [
            {
                "op": 5,
                "d": {
                    "eventType": "RecordStateChanged",
                    "eventData": {
                        "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED",
                        "outputPath": str(output),
                    },
                },
            },
            {
                "op": 7,
                "d": {
                    "requestId": "stop-status-1",
                    "requestStatus": {"result": True, "code": 100},
                    "responseData": {"outputActive": False},
                },
            },
        ]
    )
    pending = {"requestStatus": {"result": False, "code": 702, "comment": "still flushing"}}

    event = asyncio.run(probe.wait_record_stop(inbox, ws, pending, tmp_path))

    assert event is not None
    assert event["eventData"]["outputPath"] == str(output)


def test_record_m2_pending_timeout_and_non_702_failure_are_fail_closed(tmp_path: Path) -> None:
    probe = _load_probe(RECORD_M2_PROBE, "record_m2_stop_contract_failure")
    original_timeout = probe.STOP_EVENT_TIMEOUT_SEC
    probe.STOP_EVENT_TIMEOUT_SEC = 0.0
    try:
        pending = {"requestStatus": {"result": False, "code": 702, "comment": "still active"}}
        assert asyncio.run(probe.wait_record_stop(probe.Inbox(), _FakeWs([]), pending, tmp_path)) is None
        refused = {"requestStatus": {"result": False, "code": 500, "comment": "refused"}}
        assert asyncio.run(probe.wait_record_stop(probe.Inbox(), _FakeWs([]), refused, tmp_path)) is None
    finally:
        probe.STOP_EVENT_TIMEOUT_SEC = original_timeout


def test_record_m2_stopped_missing_or_stale_path_is_rejected(tmp_path: Path) -> None:
    probe = _load_probe(RECORD_M2_PROBE, "record_m2_stop_contract_path_failure")
    pending = {"requestStatus": {"result": False, "code": 702, "comment": "still flushing"}}
    for output_path in ("", str(tmp_path.parent / "stale.mp4")):
        inbox = probe.Inbox()
        ws = _FakeWs(
            [
                {
                    "op": 5,
                    "d": {
                        "eventType": "RecordStateChanged",
                        "eventData": {
                            "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED",
                            "outputPath": output_path,
                        },
                    },
                }
            ]
        )
        assert asyncio.run(probe.wait_record_stop(inbox, ws, pending, tmp_path)) is None
