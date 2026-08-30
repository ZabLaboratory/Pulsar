"""Deterministic contract tests for the v5 record action boundary.

The real websocket handler is native and is exercised by the Windows CTest
campaign.  These tests keep the response contract executable without a native
build: source assertions pin the native decision order and a tiny model drives
the pause/replay/stop sequence, including the audio-first edge case.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RECORD_HANDLER = ROOT / "plugins" / "pulsar-websocket" / "src" / "requesthandler" / "RequestHandler_Record.cpp"
OUTPUT_EFFECT = ROOT / "plugins" / "pulsar-websocket" / "src" / "requesthandler" / "OutputEffect.h"
OUTPUT_HELPER = ROOT / "plugins" / "pulsar-websocket" / "src" / "utils" / "Obs_OutputHelper.cpp"
OBS_HEADER = ROOT / "plugins" / "pulsar-websocket" / "src" / "utils" / "Obs.h"


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
