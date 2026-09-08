"""Create a size-bounded Pages preview without changing the full broadcast proof."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess

TARGET_BYTES = 60 * 1024 * 1024
MAX_FILE_BYTES = 90 * 1024 * 1024  # margin below GitHub's 100 MiB hard limit
AUDIO_KBPS = 96


def preview_video_kbps(duration: float) -> int:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('video duration must be positive and finite')
    available = math.floor(TARGET_BYTES * 8 / duration / 1000) - AUDIO_KBPS - 32
    if available < 96:
        raise ValueError('video is too long for the bounded preview budget')
    return min(1200, available)


def media_info(path: Path) -> dict:
    return json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path),
    ]))


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare(source_dir: Path, output_dir: Path, run_url: str) -> dict:
    source_dir, output_dir = source_dir.resolve(), output_dir.resolve()
    if source_dir == output_dir or source_dir in output_dir.parents or output_dir in source_dir.parents:
        raise ValueError('source and preview directories must be disjoint')
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError('preview directory must be empty; refusing to overwrite files')
    source = source_dir / 'pulsar-live-broadcast-proof.mp4'
    info = media_info(source)
    duration = float(info['format']['duration'])
    bitrate = preview_video_kbps(duration)
    source_hash = sha256(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    preview = output_dir / source.name
    subprocess.run([
        'ffmpeg', '-v', 'error', '-n', '-i', str(source),
        '-map', '0:v:0', '-map', '0:a?',
        '-vf', 'fps=15,scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2',
        '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
        '-b:v', f'{bitrate}k', '-maxrate', f'{bitrate}k', '-bufsize', f'{bitrate * 2}k',
        '-c:a', 'aac', '-b:a', f'{AUDIO_KBPS}k', '-movflags', '+faststart', str(preview),
    ], check=True)
    if preview.stat().st_size >= MAX_FILE_BYTES:
        raise ValueError('preview exceeds the safe Pages file-size limit')
    preview_info = media_info(preview)
    preview_duration = float(preview_info['format']['duration'])
    if abs(preview_duration - duration) > 1.0:
        raise ValueError('preview duration differs from the full proof')
    if sha256(source) != source_hash:
        raise ValueError('full source proof changed during preview generation')
    for versioned in source_dir.glob('pulsar-live-broadcast-proof-*.mp4'):
        if sha256(versioned) != source_hash:
            raise ValueError('versioned source is not identical to stable proof')
        shutil.copyfile(preview, output_dir / versioned.name)
    diagnostic = source_dir / 'diagnostic.json'
    if diagnostic.exists():
        shutil.copyfile(diagnostic, output_dir / diagnostic.name)
    manifest = {
        'schema': 'pulsar-pages-proof/v1', 'variant': 'preview-not-full-proof',
        'full_proof_run_url': run_url,
        'source': {'sha256': source_hash, 'bytes': source.stat().st_size,
                   'duration': duration, 'streams': info['streams']},
        'preview': {'sha256': sha256(preview), 'bytes': preview.stat().st_size,
                    'duration': preview_duration, 'video_kbps_budget': bitrate,
                    'streams': preview_info['streams']},
    }
    (output_dir / 'pages-preview-manifest.json').write_text(
        json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    (output_dir / 'README.txt').write_text(
        'These MP4 files are size-bounded viewing previews, not full-quality qualification proof.\n'
        f'The complete broadcast video and diagnostic are retained in this run: {run_url}\n'
        'diagnostic.json describes the full source recording; preview details are in pages-preview-manifest.json.\n',
        encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--run-url', required=True)
    args = parser.parse_args()
    result = prepare(args.source_dir, args.output_dir, args.run_url)
    print(json.dumps({'source_bytes': result['source']['bytes'],
                      'preview_bytes': result['preview']['bytes'],
                      'duration': result['preview']['duration']}))
