"""Source-contract guards; native video-content-runtime exercises the actual cache."""
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_readback_preserves_command_identity_and_rejects_old_content():
    patch = (ROOT / "patches/0050-perf-libobs-current-readback-content-identity.patch").read_text()
    assert "content_pts_ns < pts_ns" in patch
    assert "+\tmetadata->pts_ns = content_pts_ns" not in patch
    assert "current_content = current_content || gpu_active" in patch
    assert "frame.timestamp = vframe_info.timestamp" in patch
    assert "cfi->frame.content_pts_ns = content_pts_ns" in patch
    source = (ROOT / "plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp").read_text()
    callback = source.split("void rawFrame(struct video_data *frame)", 1)[1].split("void packet(", 1)[0]
    assert "contentPts < context->ptsNs" in callback
    assert "copyContextToEvent(event, *context)" in callback
    assert "event.ptsNs =" not in callback
