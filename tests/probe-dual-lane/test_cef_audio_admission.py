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
SHUTDOWN_TEST = ROOT / "tests" / "pulsar-headless-shutdown" / "test_graceful_shutdown.py"


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
    stream_started: bool = False
    stream_stopped: bool = True
    browser_source_alive: bool = True

    def try_acquire(self) -> bool:
        if self.audio_callbacks_closed:
            return False
        self.audio_callbacks_in_flight += 1
        return True

    def mark_stream_started(self) -> None:
        self.stream_started = True
        self.stream_stopped = False

    def mark_stream_stopped(self) -> None:
        self.stream_stopped = True

    def _can_finalize(self) -> bool:
        return self.close_callback_seen and (
            not self.stream_started or self.stream_stopped
        ) and self.audio_callbacks_in_flight == 0

    def try_finalize(self) -> bool:
        if not self._can_finalize():
            return False
        self.browser_source_alive = False
        return True

    def close_and_try_finalize(self) -> bool:
        self.audio_callbacks_closed = True
        self.close_callback_seen = True
        return self.try_finalize()

    def release_and_try_finalize(self) -> bool:
        assert self.audio_callbacks_in_flight > 0
        self.audio_callbacks_in_flight -= 1
        return self.try_finalize()


def test_linearized_audio_admission_is_safe_in_both_orders() -> None:
    # Close wins: a late callback is rejected and cannot dereference the source.
    state = _LinearizedAdmissionModel()
    state.mark_stream_started()
    state.mark_stream_stopped()
    assert state.close_and_try_finalize() is True
    assert state.try_acquire() is False
    assert state.browser_source_alive is False

    # Callback wins: close observes the lease and finalization waits for release.
    state = _LinearizedAdmissionModel()
    state.mark_stream_started()
    assert state.try_acquire() is True
    assert state.close_and_try_finalize() is False
    assert state.browser_source_alive is True
    # This is the observed-before-stopped order; the gate must still wait.
    assert state.release_and_try_finalize() is False
    assert state.browser_source_alive is True
    state.mark_stream_stopped()
    assert state.try_finalize() is True
    assert state.browser_source_alive is False


def test_self_keepalive_spans_observed_to_source_finalize() -> None:
    source = BROWSER_CLIENT.read_text(encoding="utf-8")
    header = BROWSER_CLIENT_HPP.read_text(encoding="utf-8")
    before_close = _function_body(
        source,
        "void BrowserClient::OnBeforeClose",
        "CefRefPtr<CefResourceRequestHandler>",
    )
    finalize = _function_body(
        source,
        "void BrowserClient::finalize_browser_close",
        "CefRefPtr<CefLoadHandler>",
    )

    assert "CefRefPtr<BrowserClient> close_keepalive" in header
    assert "CefRefPtr<BrowserClient> self(this);" in before_close
    assert "close_keepalive = self;" in before_close
    assert "CefRefPtr<BrowserClient> self(this);" in finalize
    assert "BrowserSourceFinalizeBrowserClose(browser);" in finalize
    assert "close_keepalive = nullptr;" in finalize
    assert finalize.index("BrowserSourceFinalizeBrowserClose(browser);") < finalize.index(
        "close_keepalive = nullptr;"
    )


def test_linearized_audio_admission_allows_stopped_before_or_after_close() -> None:
    state = _LinearizedAdmissionModel()
    state.mark_stream_started()
    state.mark_stream_stopped()
    assert state.close_and_try_finalize() is True

    state = _LinearizedAdmissionModel()
    state.mark_stream_started()
    assert state.close_and_try_finalize() is False
    state.mark_stream_stopped()
    assert state.try_finalize() is True


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


def test_shutdown_fixture_requires_two_ids_and_accepts_both_stop_orders() -> None:
    integration = SHUTDOWN_TEST.read_text(encoding="utf-8")

    assert "len(audio_started) != 2" in integration
    assert "set(audio_started) != set(client_keepalive_acquired)" in integration
    assert "set(audio_started) != set(client_keepalive_released)" in integration
    assert "max(stopped, observed) <= quiescent" in integration
    assert "client_keepalive_acquired" in integration
