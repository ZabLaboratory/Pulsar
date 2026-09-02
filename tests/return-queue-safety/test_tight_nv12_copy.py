"""Static contract for the bounded tight-stride NV12 writer fastpath."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[2]
PATCH = ROOT / "patches" / "0029-perf-win-dshow-bulk-copy-tight-nv12.patch"


def test_tight_stride_fastpath_preserves_padded_fallback() -> None:
    text = PATCH.read_text(encoding="utf-8")
    assert text.startswith("From ")
    assert "Subject: [PATCH] perf(win-dshow): bulk-copy tightly packed NV12 returns" in text
    assert "if (linesize[0] == cx && linesize[1] == cx)" in text
    assert "const size_t y_size = (size_t)cx * cy;" in text
    assert "memcpy(destination, data[0], y_size);" in text
    assert "memcpy(destination + y_size, data[1], y_size / 2U);" in text
    assert "for (uint32_t row = 0; row < cy; ++row)" in text
    assert "data[0] + (size_t)row * linesize[0]" in text


def test_tight_stride_fastpath_does_not_change_queue_contract() -> None:
    text = PATCH.read_text(encoding="utf-8")
    added = "\n".join(line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    assert "queue_validate_nv12" not in added
    assert "video_queue_write_ex" not in added
    assert "metadata" not in added
    assert "CloseHandle" not in added
