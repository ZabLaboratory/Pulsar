"""Adversarial proof for the CEF audio-callback admission contract.

This is deliberately a Probe-side test.  It does not replace a C++ race test;
it records the exact allowed interleaving that must be prevented by the
production admission primitive and fails the candidate while that primitive
is still implemented as a split closed-load/fetch-add sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
BROWSER_CLIENT = ROOT / "plugins" / "pulsar-browser" / "browser-client.cpp"


def _function_body(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


@dataclass
class _SplitAdmissionModel:
    """Minimal model of the two independent atomics in the old admission path."""

    audio_callbacks_closed: bool = False
    audio_callbacks_in_flight: int = 0
    close_callback_seen: bool = False
    browser_source_alive: bool = True

    def admission_check(self) -> bool:
        return not self.audio_callbacks_closed

    def admission_increment(self) -> None:
        self.audio_callbacks_in_flight += 1

    def close_and_finalize_if_empty(self) -> None:
        self.audio_callbacks_closed = True
        self.close_callback_seen = True
        if self.audio_callbacks_in_flight == 0:
            self.browser_source_alive = False

    def callback_may_dereference(self, close_seen_snapshot: bool) -> bool:
        # The callback's second atomic load is allowed to observe its old value:
        # it is not synchronized with the independent gate atomic.
        return not close_seen_snapshot and self.browser_source_alive is False


def test_split_audio_admission_has_deterministic_detach_interleaving() -> None:
    state = _SplitAdmissionModel()

    # T_audio: load(audio_callbacks_closed) -> false; pause before fetch_add.
    assert state.admission_check() is True

    # T_close: close the gate, observe zero, detach the BrowserSource.
    state.close_and_finalize_if_empty()
    assert state.browser_source_alive is False

    # T_audio resumes and increments after finalization.  A permitted stale
    # close_callback_seen read then reaches the old raw BrowserSource pointer.
    state.admission_increment()
    assert state.callback_may_dereference(close_seen_snapshot=False) is True


def test_production_audio_admission_has_one_atomic_gate_and_lease() -> None:
    source = BROWSER_CLIENT.read_text(encoding="utf-8")
    body = _function_body(
        source,
        "bool BrowserClient::begin_audio_callback()",
        "void BrowserClient::end_audio_callback",
    )

    split_sequence = re.search(
        r"audio_callbacks_closed\.load\([\s\S]*?"
        r"audio_callbacks_in_flight\.fetch_add",
        body,
    )
    assert split_sequence is None, (
        "begin_audio_callback must atomically couple admission with the "
        "in-flight lease; a closed load followed by fetch_add permits "
        "detach-before-registration (see the deterministic model test)"
    )

    # A direct CAS or a mutex-protected critical section are both acceptable
    # implementation strategies.  The test intentionally does not prescribe
    # the production primitive beyond requiring one at this boundary.
    assert "compare_exchange" in body or "lock_guard" in body, (
        "begin_audio_callback has no visible atomic gate/lease primitive"
    )

