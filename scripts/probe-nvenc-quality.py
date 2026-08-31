#!/usr/bin/env python3
"""Deterministic NVENC quality A/B gate for Pulsar's ULL scheduling profile."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


CASES = {
    "broadcast": "testsrc2=size=1920x1080:rate=60",
    "complex": "mandelbrot=size=1920x1080:rate=60:maxiter=100",
    "chaotic": "life=size=1920x1080:rate=60:ratio=0.3:seed=42:mold=10",
}
PROFILES = {
    "historical_hq": ("hq", "qres", "2"),
    "quality_safe_ull": ("ull", "qres", "2"),
}


@dataclass(frozen=True)
class Metrics:
    vmaf: float
    psnr_y: float
    ssim: float


@dataclass(frozen=True)
class CaseResult:
    case: str
    historical_hq: Metrics
    quality_safe_ull: Metrics
    delta_vmaf: float
    delta_psnr_y: float
    delta_ssim: float
    passed: bool


def run(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(command)}\n{completed.stdout}")
    return completed.stdout


def require_tooling(ffmpeg: str) -> None:
    encoders = run([ffmpeg, "-hide_banner", "-encoders"])
    filters = run([ffmpeg, "-hide_banner", "-filters"])
    missing = [name for name, body in (("h264_nvenc", encoders), ("libvmaf", filters),
                                       ("psnr", filters), ("ssim", filters)) if name not in body]
    if missing:
        raise RuntimeError("required FFmpeg capabilities unavailable: " + ", ".join(missing))


def encode(ffmpeg: str, source: str, duration: float, output: Path,
           tune: str, multipass: str, bframes: str, bitrate_kbps: int) -> None:
    run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", source, "-t", str(duration),
        "-c:v", "h264_nvenc", "-preset", "p5", "-tune", tune,
        "-multipass", multipass, "-bf", bframes, "-rc", "cbr",
        "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps}k",
        "-bufsize", f"{bitrate_kbps * 2}k", "-g", "120",
        "-pix_fmt", "yuv420p", str(output),
    ])
    if not output.is_file() or output.stat().st_size < 100_000:
        raise RuntimeError(f"NVENC output missing or implausibly small: {output}")


def metric_output(ffmpeg: str, encoded: Path, source: str, duration: float,
                  filter_spec: str) -> str:
    return run([
        ffmpeg, "-hide_banner", "-i", str(encoded),
        "-f", "lavfi", "-i", source, "-t", str(duration),
        "-lavfi", f"[0:v][1:v]{filter_spec}", "-f", "null", "NUL",
    ])


def measure(ffmpeg: str, encoded: Path, source: str, duration: float,
            vmaf_subsample: int) -> Metrics:
    vmaf_text = metric_output(
        ffmpeg, encoded, source, duration, f"libvmaf=n_subsample={vmaf_subsample}"
    )
    psnr_text = metric_output(ffmpeg, encoded, source, duration, "psnr")
    ssim_text = metric_output(ffmpeg, encoded, source, duration, "ssim")
    vmaf = re.search(r"VMAF score:\s*([0-9.]+)", vmaf_text)
    psnr = re.search(r"PSNR y:([0-9.]+)", psnr_text)
    ssim = re.search(r"All:([0-9.]+)", ssim_text)
    if not (vmaf and psnr and ssim):
        raise RuntimeError("could not parse VMAF/PSNR/SSIM output")
    return Metrics(float(vmaf.group(1)), float(psnr.group(1)), float(ssim.group(1)))


def evaluate(case: str, historical: Metrics, candidate: Metrics,
             min_vmaf_delta: float, min_psnr_delta: float,
             min_ssim_delta: float) -> CaseResult:
    dv = candidate.vmaf - historical.vmaf
    dp = candidate.psnr_y - historical.psnr_y
    ds = candidate.ssim - historical.ssim
    return CaseResult(case, historical, candidate, dv, dp, ds,
                      dv >= min_vmaf_delta and dp >= min_psnr_delta and ds >= min_ssim_delta)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--duration", type=float, default=4.0)
    parser.add_argument("--bitrate-kbps", type=int, default=6000)
    parser.add_argument("--vmaf-subsample", type=int, default=5)
    parser.add_argument("--min-vmaf-delta", type=float, default=-1.0)
    parser.add_argument("--min-psnr-delta", type=float, default=-0.5)
    parser.add_argument("--min-ssim-delta", type=float, default=-0.001)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    require_tooling(args.ffmpeg)

    results: list[CaseResult] = []
    with tempfile.TemporaryDirectory(prefix="pulsar-nvenc-quality-") as tmp:
        root = Path(tmp)
        for case, source in CASES.items():
            measured: dict[str, Metrics] = {}
            for profile, (tune, multipass, bframes) in PROFILES.items():
                output = root / f"{case}-{profile}.mp4"
                encode(args.ffmpeg, source, args.duration, output, tune, multipass,
                       bframes, args.bitrate_kbps)
                measured[profile] = measure(
                    args.ffmpeg, output, source, args.duration, args.vmaf_subsample
                )
            result = evaluate(case, measured["historical_hq"],
                              measured["quality_safe_ull"], args.min_vmaf_delta,
                              args.min_psnr_delta, args.min_ssim_delta)
            results.append(result)
            print(
                f"{case}: VMAF {result.historical_hq.vmaf:.3f} -> "
                f"{result.quality_safe_ull.vmaf:.3f} ({result.delta_vmaf:+.3f}), "
                f"PSNR-Y {result.delta_psnr_y:+.3f} dB, "
                f"SSIM {result.delta_ssim:+.6f}: {'PASS' if result.passed else 'FAIL'}"
            )

    report = {
        "schema": "pulsar.nvenc-quality-gate.v1",
        "settings": {
            "resolution": "1920x1080", "fps": 60, "duration": args.duration,
            "bitrate_kbps": args.bitrate_kbps, "preset": "p5",
            "thresholds": {"vmaf_delta": args.min_vmaf_delta,
                           "psnr_y_delta_db": args.min_psnr_delta,
                           "ssim_delta": args.min_ssim_delta},
        },
        "results": [asdict(result) for result in results],
        "passed": all(result.passed for result in results),
    }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
