"""Offline exploration only: no runtime defaults change and no quality gate relaxation."""
import argparse
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("nvenc_quality", Path(__file__).with_name("probe-nvenc-quality.py"))
quality = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = quality
spec.loader.exec_module(quality)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=4)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    quality.require_tooling(args.ffmpeg)
    profiles = [("baseline", "p5", 2), ("p7-bf0", "p7", 0), ("p7-bf1", "p7", 1)]
    report = {"schema": "pulsar.nvenc-profile-frontier.v1", "duration": args.duration,
              "bitrate_kbps": 6000, "thresholds": {"vmaf": -1, "psnr_y": -0.5, "ssim": -0.001}, "cases": {}}
    for case, source in quality.CASES.items():
        baseline = None
        case_result = {}
        for label, preset, bf in profiles:
            path = args.output / f"{case}-{label}.mp4"
            command = [args.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source,
                       "-t", str(args.duration), "-c:v", "h264_nvenc", "-preset", preset, "-tune", "ull",
                       "-multipass", "qres", "-bf", str(bf), "-rc", "cbr", "-b:v", "6000k", "-maxrate", "6000k",
                       "-bufsize", "12000k", "-g", "120", "-pix_fmt", "yuv420p", str(path)]
            quality.run(command)
            measured = quality.measure(args.ffmpeg, path, source, args.duration, 1)
            if baseline is None:
                baseline = measured
            verdict = quality.evaluate(case, baseline, measured, -1, -0.5, -0.001)
            case_result[label] = {"metrics": asdict(measured), "delta_vmaf": verdict.delta_vmaf,
                                  "delta_psnr_y": verdict.delta_psnr_y, "delta_ssim": verdict.delta_ssim,
                                  "passed": verdict.passed, "command": command}
            print(case, label, json.dumps(case_result[label]["metrics"]), "pass", verdict.passed, flush=True)
        report["cases"][case] = case_result
        (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
