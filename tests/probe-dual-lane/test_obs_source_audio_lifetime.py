"""Probe evidence for the libobs source lifetime boundary of CEF audio."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BROWSER_CLIENT_CPP = ROOT / "plugins" / "pulsar-browser" / "browser-client.cpp"
SOURCE_HPP = ROOT / "plugins" / "pulsar-browser" / "obs-browser-source.hpp"
SOURCE_CPP = ROOT / "plugins" / "pulsar-browser" / "obs-browser-source.cpp"
UPSTREAM_SOURCE_CPP = ROOT / "upstream" / "libobs" / "obs-source.c"


@dataclass
class _LibobsLifetimeModel:
    """Minimal model of the last strong release versus a borrowed pointer."""

    strong_refs: int = 1
    source_memory_alive: bool = True
    browser_source_alive: bool = True

    def obs_source_destroy_defer_after_last_release(self) -> None:
        assert self.strong_refs == 1
        self.strong_refs = 0
        # BrowserSource::Destroy queues CEF close and returns; libobs then
        # continues obs_source_destroy_defer and frees obs_source_t.
        self.browser_source_alive = True
        self.source_memory_alive = False

    def borrowed_audio_callback_dereferences_source(self) -> bool:
        return self.browser_source_alive and not self.source_memory_alive


def test_libobs_last_release_can_free_obs_source_after_browser_destroy_returns() -> None:
    state = _LibobsLifetimeModel()
    state.obs_source_destroy_defer_after_last_release()
    assert state.borrowed_audio_callback_dereferences_source() is True


def test_audio_callback_requires_weak_to_strong_source_lease_or_sync_drain() -> None:
    client = BROWSER_CLIENT_CPP.read_text(encoding="utf-8")
    source_header = SOURCE_HPP.read_text(encoding="utf-8")
    source = SOURCE_CPP.read_text(encoding="utf-8")
    upstream = UPSTREAM_SOURCE_CPP.read_text(encoding="utf-8")

    # The exact upstream lifetime contract used by this finding: destroy
    # invokes the plugin callback and immediately continues to the deferred
    # obs_source_t cleanup; a private BrowserSource gate cannot extend it.
    destroy_start = upstream.index("void obs_source_destroy(struct obs_source *source)")
    defer_start = upstream.index("static void obs_source_destroy_defer", destroy_start)
    destroy_body = upstream[destroy_start:defer_start]
    defer_body = upstream[defer_start:]
    assert "source->info.destroy(source->context.data)" in defer_body
    assert "os_task_queue_queue_task(obs->destruction_task_thread" in destroy_body

    # A callback that uses bs->source must either promote a retained weak
    # source to a strong reference for the duration of output, or be proven to
    # drain synchronously before BrowserSource::Destroy returns.
    audio_start = client.index("void BrowserClient::OnAudioStreamPacket")
    audio_end = client.index("void BrowserClient::OnAudioStreamStopped", audio_start)
    audio_body = client[audio_start:audio_end]
    has_weak_promotion = (
        "OBSSourceAutoRelease source_ref = bs->GetStrongSource();" in audio_body
        and "obs_source_output_audio(source_ref, &audio);" in audio_body
        and "obs_source_get_weak_source" in source
        and "obs_weak_source_get_source" in source
        and "obs_weak_source_release" in source
        and "obs_weak_source_t *weak_source" in source_header
    )
    assert "obs_source_output_audio(bs->source" not in audio_body
    assert "obs_source_output_audio(source_ref" in audio_body
    has_explicit_sync_drain = "wait_for_audio" in source and "audio_callbacks" in source
    assert has_weak_promotion or has_explicit_sync_drain, (
        "OnAudioStreamPacket still lacks a safe obs_source_t lifetime bridge; "
        "add weak->strong promotion or a synchronous source drain before "
        "BrowserSource::Destroy returns"
    )
