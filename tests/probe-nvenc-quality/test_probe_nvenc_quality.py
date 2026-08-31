from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe-nvenc-quality.py"
SPEC = importlib.util.spec_from_file_location("probe_nvenc_quality", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_profiles_change_only_nvenc_scheduling() -> None:
    assert MODULE.PROFILES["historical_hq"] == ("hq", "qres", "2")
    assert MODULE.PROFILES["quality_safe_ull"] == ("ull", "qres", "2")


def test_gate_accepts_bounded_metric_noise() -> None:
    baseline = MODULE.Metrics(90.0, 40.0, 0.99)
    candidate = MODULE.Metrics(89.2, 39.6, 0.9895)
    result = MODULE.evaluate("bounded", baseline, candidate, -1.0, -0.5, -0.001)
    assert result.passed


def test_gate_rejects_each_quality_regression() -> None:
    baseline = MODULE.Metrics(90.0, 40.0, 0.99)
    assert not MODULE.evaluate(
        "vmaf", baseline, MODULE.Metrics(88.9, 40.0, 0.99), -1.0, -0.5, -0.001
    ).passed
    assert not MODULE.evaluate(
        "psnr", baseline, MODULE.Metrics(90.0, 39.4, 0.99), -1.0, -0.5, -0.001
    ).passed
    assert not MODULE.evaluate(
        "ssim", baseline, MODULE.Metrics(90.0, 40.0, 0.9889), -1.0, -0.5, -0.001
    ).passed
