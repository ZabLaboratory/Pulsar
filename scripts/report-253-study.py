"""Summarize existing #253 paired runs without pooling codecs or run percentiles."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def stats(values):
    values = sorted(values)
    def percentile(q):
        if not values:
            return None
        pos = (len(values) - 1) * q
        lo = int(pos)
        hi = min(lo + 1, len(values) - 1)
        return round(values[lo] + (values[hi] - values[lo]) * (pos - lo), 6)
    return {"count": len(values), "p50_ms": percentile(.5), "p95_ms": percentile(.95), "p99_ms": percentile(.99)}


def summarize(directory, codec):
    trace_path = directory / f"{codec}.jsonl"
    report_path = directory / f"{codec}-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    # These secondary summaries do not replace the authenticated trace parser.
    # Report the trace hashes so every value can be checked against that run.
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    observations = [r for r in records if r.get("record_type") == "observation"
                    and int(r["take_command_id"].split("-")[-1]) > 100]
    encoded = [r for r in observations if r["boundary"] == "encoded_first_packet"]
    directshow = [r for r in observations if r["boundary"] == "directshow_return"]
    pts_offset_frames = Counter(round((r["packet_cts_monotonic_ns"] - r["pts_ns"]) * 60 / 1e9) for r in encoded)
    stages = {}
    for name, start, end in (
        ("entry_to_sample_lock", "frame_entry_monotonic_ns", "lock_sample_data_acquired_monotonic_ns"),
        ("queue_read", "queue_read_start_monotonic_ns", "queue_read_completed_monotonic_ns"),
        ("sample_delivery", "queue_read_completed_monotonic_ns", "unlock_sample_data_completed_monotonic_ns"),
        ("filter_total", "frame_entry_monotonic_ns", "unlock_sample_data_completed_monotonic_ns"),
    ):
        measured = [r for r in directshow if start in r and end in r]
        stages[name] = {**stats([(r[end] - r[start]) / 1e6 for r in measured]),
                        "complete_coverage": len(measured) == len(directshow),
                        "eligible_frames": len(directshow)}
    counters = {}
    for field in ("duplicate_count", "gap_count", "retry_count", "torn_count"):
        values = [r["queue_counters"][field] for r in directshow]
        counters[field] = {"first": min(values), "last": max(values), "delta": max(values) - min(values)}
    return {
        "run": directory.name, "codec": codec,
        "build_revision": report["session"]["build_revision"],
        "runtime_id": report["session"]["runtime_instance_id"],
        "hardware": report["session"]["hardware"],
        "takes": report["takes"], "latency": report["latency"],
        "ac12a": report["ac12a"], "ac12b": report["ac12b"],
        "directshow_stages": stages, "queue_counters": counters,
        "selected_packet_cts_minus_commit_pts_frames": dict(sorted(pts_offset_frames.items())),
        "trace_sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "limitations": ["Decoded boundary is selected candidate at FFmpeg showinfo, not earliest changed/displayed pixel.",
                        "DirectShow duplicate/gap counters concern the return consumer, not encoder dropped frames.",
                        "No single-lane or external network campaign; resource comparison not inferred."]
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = [summarize(directory, codec) for directory in args.runs for codec in ("x264", "nvenc")]
    args.output.write_text(json.dumps({"schema": "pulsar.issue253.study.v1", "runs": results}, indent=2) + "\n", encoding="utf-8")
    print("run,codec,raw_p95,dshow_p95,encoded_p95,decoded_candidate_p95,rtmp_callback_receiver_p95")
    for r in results:
        latency = r["latency"]
        print(",".join(map(str, [r["run"], r["codec"], *[
            latency[k]["p95_ms"] for k in ("encoder_input_raw", "directshow_return", "encoded_first_packet", "decoded_first_frame")
        ], r["ac12a"]["p95_ms"]])))
