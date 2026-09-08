"""Pure offline contracts for bounded Pages previews (full video remains separate)."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'pages_proof', Path(__file__).with_name('prepare-pages-proof.py'))
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def test_ten_minute_preview_has_headroom_below_git_file_limit():
    bitrate = proof.preview_video_kbps(600.3)
    assert 96 <= bitrate <= 1200
    assert (bitrate + proof.AUDIO_KBPS) * 1000 / 8 * 600.3 < proof.TARGET_BYTES
    assert proof.TARGET_BYTES < proof.MAX_FILE_BYTES < 100 * 1024 * 1024


@pytest.mark.parametrize('duration', [0, -1, float('nan'), float('inf'), 100000])
def test_invalid_or_unbudgetable_duration_fails(duration):
    with pytest.raises(ValueError):
        proof.preview_video_kbps(duration)


def test_source_is_never_its_own_preview_directory(tmp_path):
    with pytest.raises(ValueError, match='disjoint'):
        proof.prepare(tmp_path, tmp_path, 'https://example.test/run')
    with pytest.raises(ValueError, match='disjoint'):
        proof.prepare(tmp_path, tmp_path / 'nested', 'https://example.test/run')


def test_existing_preview_data_is_preserved(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    output.mkdir()
    marker = output / 'owned.txt'
    marker.write_text('preserve', encoding='utf-8')
    with pytest.raises(ValueError, match='refusing to overwrite'):
        proof.prepare(source, output, 'https://example.test/run')
    assert marker.read_text(encoding='utf-8') == 'preserve'
