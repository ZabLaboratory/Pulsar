"""Static guards for the non-blocking libobs borrowed-video mailbox."""

from pathlib import Path


ROOT = Path(__file__).parents[2]
MAILBOX_PATCH = ROOT / "patches" / "0027-fix-libobs-borrowed-video-mailbox.patch"


def patch_text() -> str:
    """Read the captured patch; the checked-out submodule must stay at its pin."""
    return MAILBOX_PATCH.read_text(encoding="utf-8")


def test_unmap_last_surface_never_waits_for_borrowed_callback() -> None:
    text = patch_text()
    assert "-\t\t\tpthread_cond_wait(&video->borrowed_video_cond" in text
    assert "+\tobs_borrowed_video_publish(video, input_frame);" in text


def test_mailbox_owns_frame_storage_and_releases_callbacks_before_dispatch() -> None:
    text = patch_text()
    assert "OBS_BORROWED_VIDEO_SLOTS 2" in text
    assert "video_frame_copy(&video->borrowed_video_slots[slot_index].storage" in text
    worker = text[text.index("@@ -607,8 +607,10 @@ static void *borrowed_video_thread"):text.index("@@ -643,12 +658,35 @@ static bool init_borrowed_video")]
    unlock = worker.index("pthread_mutex_unlock(&video->borrowed_video_mutex);")
    callback = worker.index("cb->callback(cb->param, &video->borrowed_video_slots[slot_index].data);")
    assert unlock < callback
    assert "pthread_join(video->borrowed_video_thread, NULL);" in text


def test_mailbox_has_latest_frame_generation_and_safety_counters() -> None:
    text = patch_text()
    for marker in ("borrowed_video_generation", "borrowed_video_pending_slot", "borrowed_video_busy_slot"):
        assert marker in text
    for marker in (
        "borrowed_dropped_count",
        "borrowed_overwritten_count",
        "borrowed_wait_count",
        "borrowed_fallback_count",
    ):
        assert marker in text


def test_mailbox_allocates_before_starting_worker() -> None:
    text = patch_text()
    allocation = text.index("+\tfor (size_t i = 0; i < OBS_BORROWED_VIDEO_SLOTS; ++i) {")
    worker_start = text.index("\tif (pthread_create(&video->borrowed_video_thread", allocation)
    assert allocation < worker_start
    assert "+\t\tfor (size_t i = 0; i < OBS_BORROWED_VIDEO_SLOTS; ++i)" in text
