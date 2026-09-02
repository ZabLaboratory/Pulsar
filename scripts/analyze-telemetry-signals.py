#!/usr/bin/env python3
"""Report p50/p95/p99 for the bounded Pulsar encoder/RTMP trace signals.

The input is validated by ``probe-take-latency.py`` before any percentile is
computed.  Missing boundaries are reported as ``NOT_AVAILABLE`` rather than
being inferred from log order or wall-clock time.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
_probe_spec = importlib.util.spec_from_file_location("pulsar_probe_take_latency", SCRIPT_DIR / "probe-take-latency.py")
if _probe_spec is None or _probe_spec.loader is None:  # pragma: no cover - repository layout failure
    raise RuntimeError("probe-take-latency.py is unavailable")
probe = importlib.util.module_from_spec(_probe_spec)
sys.modules[_probe_spec.name] = probe
_probe_spec.loader.exec_module(probe)


SIGNALS = (
    "encoder_frame_ready",
    "program_return_readback",
    "encode_callback_enqueue",
    "output_mux_enqueue",
    "socket_send",
)


def _stats(values: Sequence[int]) -> dict[str, Any]:
    if not values:
        return {"status": "NOT_AVAILABLE", "count": 0}
    return {
        "status": "READY",
        "count": len(values),
        "p50_ms": probe.quantile(values, 0.50) / 1_000_000.0,
        "p95_ms": probe.quantile(values, 0.95) / 1_000_000.0,
        "p99_ms": probe.quantile(values, 0.99) / 1_000_000.0,
    }


def _signal_values(trace: Any) -> dict[str, list[int]]:
    values: dict[str, list[int]] = {signal: [] for signal in SIGNALS}
    for signal in trace.signals:
        duration = signal["end_monotonic_ns"] - signal["start_monotonic_ns"]
        if duration < 0:
            raise probe.EvidenceError("CLOCK_INVALID", "telemetry signal duration is negative")
        values[signal["signal"]].append(duration)

    # ProgramReturn already has an independently validated DirectShow timing
    # ledger.  Derive this stage from that ledger, retaining the same runtime /
    # take / frame / PTS correlation instead of guessing from record order.
    if not values["program_return_readback"]:
        for observation in trace.observations:
            if observation["boundary"] != "directshow_return":
                continue
            timing = ("frame_entry_monotonic_ns", "unlock_sample_data_completed_monotonic_ns")
            if all(key in observation for key in timing):
                values["program_return_readback"].append(observation[timing[1]] - observation[timing[0]])
    return values


def analyze_trace(trace: Any) -> dict[str, Any]:
    values = _signal_values(trace)
    declared = trace.session.get("telemetry_signals", trace.session.get("trace_signals", list(SIGNALS)))
    stages: dict[str, dict[str, Any]] = {}
    for signal in SIGNALS:
        if signal == "socket_send":
            stages[signal] = {"status": "NOT_AVAILABLE", "count": 0, "reason": "no non-intrusive libobs socket hook"}
        elif signal not in declared:
            stages[signal] = {"status": "NOT_SELECTED", "count": 0}
        else:
            stages[signal] = _stats(values[signal])
    return {
        "schema": "pulsar.telemetry-signal-report.v1",
        "runtime_instance_id": trace.session["runtime_instance_id"],
        "session_id": trace.session["session_id"],
        "trace_signals": list(declared),
        "stages": stages,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True, help="validated Pulsar JSONL trace")
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args(argv)
    report = analyze_trace(probe.parse_trace(args.trace))
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
