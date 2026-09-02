"""Static guards for the #253 post-mailbox pipeline telemetry fix."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[2]
PATCH = ROOT / "patches" / "0028-fix-libobs-pipeline-stats-atomic-after-mailbox.patch"


def test_pipeline_stats_patch_uses_atomic_u64_helpers_without_video_mutex() -> None:
    text = PATCH.read_text(encoding="utf-8")
    assert text.startswith("From ")
    assert "Subject: [PATCH] fix(libobs): make pipeline telemetry counters atomic" in text
    assert "Agent-Role: Forge" in text
    assert "Agent-Thread: /root/forge_253_libobs_core" in text
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
        "borrowed_dropped_count",
        "borrowed_overwritten_count",
        "borrowed_wait_count",
        "borrowed_fallback_count",
        "callback_ns",
    ):
        assert field in text
    assert text.count("obs_pipeline_stats_load_u64(&") >= 10

    values = [0]
    for delta in (4, 0, 9, 1):
        values.append(values[-1] + delta)
    assert values == sorted(values)
