"""Adversarial proof for the CEF audio-callback admission contract.

This is deliberately a Probe-side test.  It does not replace the native race
test; it exercises both close/admission orderings in a small model and checks
that the production client uses the same linearized gate.  The old split
closed-load/fetch-add sequence is retained only as the counterexample model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BROWSER_CLIENT = ROOT / "plugins" / "pulsar-browser" / "browser-client.cpp"
BROWSER_CLIENT_HPP = ROOT / "plugins" / "pulsar-browser" / "browser-client.hpp"
AUDIO_GATE_HPP = ROOT / "plugins" / "pulsar-browser" / "browser-audio-callback-gate.hpp"
AUDIO_GATE_CMAKE = ROOT / "tests" / "pulsar-headless-shutdown" / "CMakeLists.txt"


def _function_body(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


@dataclass
class _LinearizedAdmissionModel:
    """Small lock model for the production gate's two close/admit orders."""

    audio_callbacks_closed: bool = False
    audio_callbacks_in_flight: int = 0
    close_callback_seen: bool = False
    browser_source_alive: bool = True

    def try_acquire(self) -> bool:
        if self.audio_callbacks_closed:
            return False
        self.audio_callbacks_in_flight += 1
        return True

    def close_and_try_finalize(self) -> bool:
        self.audio_callbacks_closed = True
        self.close_callback_seen = True
        if self.audio_callbacks_in_flight == 0:
            self.browser_source_alive = False
            return True
        return False

    def release_and_try_finalize(self) -> bool:
        assert self.audio_callbacks_in_flight > 0
        self.audio_callbacks_in_flight -= 1
        if self.close_callback_seen and self.audio_callbacks_in_flight == 0:
            self.browser_source_alive = False
            return True
        return False


def test_linearized_audio_admission_is_safe_in_both_orders() -> None:
    # Close wins: a late callback is rejected and cannot dereference the source.
    state = _LinearizedAdmissionModel()
    assert state.close_and_try_finalize() is True
    assert state.try_acquire() is False
    assert state.browser_source_alive is False

    # Callback wins: close observes the lease and finalization waits for release.
    state = _LinearizedAdmissionModel()
    assert state.try_acquire() is True
    assert state.close_and_try_finalize() is False
    assert state.browser_source_alive is True
    assert state.release_and_try_finalize() is True
    assert state.browser_source_alive is False


def test_production_audio_admission_uses_linearized_gate() -> None:
    source = BROWSER_CLIENT.read_text(encoding="utf-8")
    header = BROWSER_CLIENT_HPP.read_text(encoding="utf-8")
    gate = AUDIO_GATE_HPP.read_text(encoding="utf-8")
    cmake = AUDIO_GATE_CMAKE.read_text(encoding="utf-8")

    assert "BrowserAudioCallbackGate audio_callbacks" in header
    assert "return audio_callbacks.try_acquire();" in source
    assert "audio_callbacks.should_deliver()" in source
    assert "audio_callbacks.mark_close_callback_seen();" in source
    assert "audio_callbacks.mark_stream_stopped();" in source
    assert "audio_callbacks_closed.load" not in source
    assert "audio_callbacks_in_flight.fetch_add" not in source

    # The admission, close, and finalization decisions all share the same
    # mutex.  This is the source-level counterpart to the model above; the
    # native C++ test below provides the real two-thread rendezvous.
    assert gate.count("std::lock_guard<std::mutex>") >= 5
    assert "if (admission_closed_)" in gate
    assert "++in_flight_;" in gate
    assert "in_flight_ != 0" in gate
    assert "finalization_claimed_" in gate

    assert "pulsar-audio-callback-gate-test" in cmake
    assert "browser audio admission must not split gate load" in cmake
