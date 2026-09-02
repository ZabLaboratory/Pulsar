"""Static contract checks for the bounded DirectShow lease watcher patch."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches" / "0026-fix-win-dshow-lease-watcher.patch"


def _producer_patch() -> str:
    text = PATCH.read_text(encoding="utf-8")
    start = text.index("diff --git a/plugins/win-dshow/virtualcam.c")
    end = text.find("\ndiff --git ", start + 1)
    if end == -1:
        end = len(text)
    return text[start:end]


def test_lease_probe_is_off_the_video_callback_and_bounded() -> None:
    producer = _producer_patch()
    assert "#define PULSAR_DIRECTSHOW_LEASE_POLL_MS 20U" in producer
    assert "pthread_create(&vcam->lease_thread" in producer
    assert "os_event_timedwait(vcam->lease_wakeup, PULSAR_DIRECTSHOW_LEASE_POLL_MS)" in producer
    assert producer.count("OpenEventW(SYNCHRONIZE, FALSE, vcam->consumer_lease_name)") == 1
    # The callback is intentionally untouched by this patch; lease I/O is only
    # present in the new watcher helper hunk above it.
    assert "+static void virtual_video" not in producer
    assert "static bool return_consumer_is_active" in producer


def test_lease_lifecycle_is_fail_closed_and_joined() -> None:
    producer = _producer_patch()
    for transition in (
        "RETURN_LEASE_STARTING",
        "RETURN_LEASE_ATTACHED",
        "RETURN_LEASE_DETACHED",
        "RETURN_LEASE_RECONNECTING",
        "RETURN_LEASE_STOPPED",
    ):
        assert transition in producer
    assert "InterlockedExchange(&vcam->consumer_active, 0L)" in producer
    assert "os_atomic_set_long(&vcam->consumer_active, 0L)" in producer
    assert "pthread_join(vcam->lease_thread, NULL)" in producer
    assert "os_event_destroy(vcam->lease_wakeup)" in producer
    assert "CloseHandle(lease)" in producer


def test_lease_counters_and_ungated_compatibility_are_observable() -> None:
    producer = _producer_patch()
    for counter in ("lease_polls", "lease_hits", "lease_misses", "lease_expiry", "lease_watcher_fallback"):
        assert counter in producer
    assert "PULSAR_DIRECTSHOW_LEASE_TELEMETRY" in producer
    assert "if (!vcam->consumer_gated)" in producer
    assert "InterlockedIncrement(&vcam->lease_watcher_fallback)" in producer
    # The borrowed callback registration remains inherited from 0021/0024 and
    # must not be removed by this lifecycle-only patch.
    assert "raw_video_borrowed" not in producer
