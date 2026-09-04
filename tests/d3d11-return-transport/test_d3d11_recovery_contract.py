"""Static guard for crash recovery ordering in the D3D11 return transport."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATCH = next(ROOT.glob("patches/0039-fix-d3d11-invalidate-registration-before-session-*.patch"))
PREVIOUS_PATCH = next(ROOT.glob("patches/0038-fix-d3d11-stale-registration-invalidation.patch"))


def test_liveness_cleanup_precedes_session_lookup_on_consumer_exit() -> None:
    """A dead PID must be cleaned up even when ProcessIdToSessionId fails."""
    text = PATCH.read_text(encoding="utf-8")

    liveness = text.index("WaitForSingleObject(producer->consumer_process, 0)")
    session_lookup = text.index("+\tconst uint32_t consumer_session = process_session(pid);")

    assert liveness < session_lookup
    assert text.index("-\tconst uint32_t consumer_session = process_session(pid);") < session_lookup
    assert "static bool producer_invalidate_consumer_registration" in text
    assert "if (consumer_session == UINT32_MAX)" in text
    assert "if (producer_invalidate_consumer_registration(producer, pid))" in text
    assert text.index("InterlockedCompareExchange((volatile LONG *)&producer->control->consumer_pid") < text.index(
        "InterlockedExchange((volatile LONG *)&producer->control->consumer_ready, 0);"
    )

    previous = PREVIOUS_PATCH.read_text(encoding="utf-8")
    assert "InterlockedCompareExchange((volatile LONG *)&producer->control->consumer_pid" in previous
    assert "InterlockedExchange((volatile LONG *)&producer->control->consumer_session, 0);" in previous
    assert "producer_release_ring(producer);" in previous
