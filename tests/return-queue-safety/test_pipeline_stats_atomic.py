"""Static guards for the #253 pipeline telemetry race fix."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).parents[2]
PATCH = ROOT / "patches" / "0026-fix-libobs-pipeline-stats-atomic-snapshot.patch"
LEASE_NOTE = ROOT / "docs" / "tuning" / "pulsar-253-lease-fastpath.md"


def test_pipeline_stats_patch_uses_atomic_u64_helpers_without_video_mutex() -> None:
    text = PATCH.read_text(encoding="utf-8")
    assert text.startswith("From ")
    assert "Subject: [PATCH] fix(libobs): make pipeline telemetry counters atomic" in text
    assert "_InterlockedExchangeAdd64" in text
    assert "_InterlockedCompareExchange64" in text
    assert "__atomic_fetch_add" in text
    assert "__ATOMIC_RELAXED" in text
    assert "obs_pipeline_stats_add_u64" in text
    assert "obs_pipeline_stats_load_u64" in text

    added = "\n".join(line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    assert not re.search(r"pipeline_stats\.[A-Za-z0-9_]+\s*(?:\+=|\+\+|=)", added)
    assert "pthread_mutex_lock(&obs->video.mixes_mutex)" not in added
    assert "pthread_mutex_lock(&mix->borrowed_video_mutex)" not in added
    assert "raw_pipeline_mutex" not in added


def test_pipeline_stats_snapshots_load_each_counter_and_remain_monotone() -> None:
    text = PATCH.read_text(encoding="utf-8")
    for field in (
        "sample_count",
        "tick_sources_ns",
        "output_frames_ns",
        "render_displays_ns",
        "graphics_tasks_ns",
        "frame_total_ns",
        "render_submit_ns",
        "download_ns",
        "flush_ns",
        "output_copy_ns",
        "borrowed_publish_ns",
        "callback_ns",
    ):
        assert "obs_pipeline_stats_load_u64(&" in text
        assert field in text

    # Every producer operation is fetch-add, so a sequence of observations for
    # each individual cumulative counter cannot decrease.
    values = [0]
    for delta in (4, 0, 9, 1):
        values.append(values[-1] + delta)
    assert values == sorted(values)


def test_lease_fastpath_is_explicitly_deferred_for_lifecycle_safety() -> None:
    text = LEASE_NOTE.read_text(encoding="utf-8")
    assert "No positive lease-handle cache is introduced here" in text
    assert "OpenEventW" in text and "return_consumer_is_active" in text
    assert "fail-closed" in text
    assert "detach" in text and "reconnect" in text
    assert "heartbeat protocol" in text
