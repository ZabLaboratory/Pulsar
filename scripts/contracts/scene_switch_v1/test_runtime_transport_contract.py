"""Static integration gate for the production scene-switch vendor adapter."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp"
PROTOCOL = ROOT / "docs" / "PROTOCOL.md"


def test_runtime_vendor_is_dedicated_and_only_uses_call_vendor_surface() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    protocol = PROTOCOL.read_text(encoding="utf-8")

    assert 'kVendorName = "pulsar-scene-switch"' in source
    for request_type in ("Prepare", "Take", "Abort", "Dispatch", "GetState"):
        assert f'register_request(vendor_, "{request_type}"' in source
    assert 'register_request(vendor_, "GetState"' in source
    assert "obs_websocket_vendor_emit_event" in source
    assert "pulsar-scene-switch" in protocol
    assert "not** top-level obs-websocket requests" in protocol


def test_runtime_adapter_keeps_protocol_and_physical_boundaries_explicit() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    for required in (
        "IDEMPOTENCY_CONFLICT",
        "REVISION_STALE",
        "SERVER_SEQ_STALE",
        "PREVIEW_FROZEN",
        "PREVIEW_NOT_READY",
        "sceneSwitchPrepare",
        "sceneSwitchTake",
        "sceneSwitchAbort",
        "OnSceneSwitchPreviewVideoFrame",
        "video_output_connect(previewVideo",
        "takeCommitted(sceneSwitchTakeId, frameId, ptsNs)",
        "obs_view_cancel_atomic_swap()",
        "BCryptFinishHash",
    ):
        assert required in source

    # #245's Program route remains the only audio binding and the new adapter
    # must not introduce any encoder-video rebinding site.
    adapter = source[source.index("class PulsarSceneSwitchVendor") : source.index("namespace {", source.index("class PulsarSceneSwitchVendor") + 1)]
    assert "obs_encoder_set_video" not in adapter
    assert "obs_output_set_media" not in adapter


def test_runtime_adapter_guards_the_exact_v1_lifecycle_edges() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert '"preview_ready")' in source
    assert "Prepare command contains unknown or cross-type fields" in source
    assert "Take command contains unknown or cross-type fields" in source
    assert "Abort command contains unknown or cross-type fields" in source
    assert 'allowed.insert("last_committed_frame_id")' in source
    assert 'allowed.insert("last_committed_pts_ns")' in source
    assert 'in["reason"] == "queue_rejected"' in source
    assert "size() > 256" in source
    assert "required command_type disagree" in source
    assert 'if (pendingTake_ || state_ == "take_accepted")' in source
    assert "Abort intent_id differs from Take" in source
    assert 'const json previous = revisions_;' in source
    assert "sceneSwitchClearPrepared" in source
