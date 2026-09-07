import importlib.util
from copy import deepcopy
from pathlib import Path
import sys

import pytest

spec = importlib.util.spec_from_file_location("native_content_probe", Path(__file__).parents[2] / "scripts/probe-dual-lane.py")
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


def stalled_renderer_packets():
    native, received = [], []
    for index in range(19):
        content = 1_000_000_000 if index == 0 else 1_500_000_000 + max(0, index - 15) * 16_666_666
        native.append({
            "record_type": "packet_content", "clock_domain": "monotonic_ns",
            "runtime_instance_id": "runtime-test", "packet_index": index,
            "packet_pts": index, "packet_dts": index - 2,
            "packet_timebase_num": 1, "packet_timebase_den": 60,
            "packet_content_pts_monotonic_ns": content,
            "packet_cts_monotonic_ns": content - 16_666_666,
            "packet_fer_monotonic_ns": content + 100,
            "packet_ferc_monotonic_ns": content + 1000,
            "observed_at_monotonic_ns": 2_000_000_000 + index * 1_000_000,
        })
        received.append({
            "packet_index": index, "packet_pts": round(index * 1000 / 60 + 33),
            "packet_dts": round((index - 2) * 1000 / 60 + 33),
            "packet_identity": f"packet-{index}",
            "observed_at_monotonic_ns": 2_000_100_000 + index * 1_000_000,
        })
    return native, received


def test_fourteen_repeated_content_frames_use_native_identity_not_linear_extrapolation():
    native, received = stalled_renderer_packets()
    mapping = probe.correlate_native_packet_content(native, received, "runtime-test")
    exact_pts = probe.first_native_content_pts(mapping, 1_500_000_000)
    assert exact_pts == received[1]["packet_pts"] == 50
    linear_guess = received[-1]["packet_pts"] - (native[-1]["packet_content_pts_monotonic_ns"] - 1_500_000_000) / 1e6
    assert linear_guess - exact_pts > 230
    frames = [{"frame_index": i, "pts_ms": packet["packet_pts"], "y_mean": 235 if i == 0 else 16,
               "u_mean": 128, "v_mean": 128, "observed_at_monotonic_ns": 2_100_000_000 + i * 1000}
              for i, packet in enumerate(received)]
    probe.validate_decoded_sequence(frames)
    assert probe.first_changed_marker(frames, expected_pts_ms=exact_pts, old_lane="B", new_lane="A") == frames[1]
    with pytest.raises(probe.ProbeFailure, match="not the old lane"):
        probe.first_changed_marker(frames, expected_pts_ms=linear_guess, old_lane="B", new_lane="A")


@pytest.mark.parametrize("defect", ["runtime", "gap", "duplicate", "zero_content", "future_content", "mux_drift", "missing_receiver", "backward_content"])
def test_native_mapping_rejects_corrupted_or_incomplete_identity(defect):
    native, received = stalled_renderer_packets()
    if defect == "runtime":
        native[5]["runtime_instance_id"] = "other-runtime"
    elif defect == "gap":
        del native[5]
    elif defect == "duplicate":
        native.insert(5, deepcopy(native[5]))
    elif defect == "zero_content":
        native[5]["packet_content_pts_monotonic_ns"] = 0
    elif defect == "future_content":
        native[5]["packet_content_pts_monotonic_ns"] = 3_000_000_000
    elif defect == "mux_drift":
        received[5]["packet_dts"] += 10
    elif defect == "missing_receiver":
        del received[5]
    elif defect == "backward_content":
        native[5]["packet_content_pts_monotonic_ns"] -= 1
    with pytest.raises(probe.ProbeFailure):
        probe.correlate_native_packet_content(native, received, "runtime-test")


def test_only_unreceived_shutdown_tail_can_be_omitted():
    native, received = stalled_renderer_packets()
    mapping = probe.correlate_native_packet_content(native, received[:-2], "runtime-test")
    assert len(mapping) == len(received) - 2
    with pytest.raises(probe.ProbeFailure, match="pre/post-commit"):
        probe.first_native_content_pts(mapping, 3_000_000_000)


def test_presentation_order_is_independent_of_encoded_submission_order():
    native, received = stalled_renderer_packets()
    order = [0, 3, 1, 2, *range(4, 19)]
    for index, original in enumerate(order):
        native[original]["packet_index"] = received[original]["packet_index"] = index
        native[original]["packet_dts"] = index - 2
        received[original]["packet_dts"] = round((index - 2) * 1000 / 60 + 33)
    mapping = probe.correlate_native_packet_content([native[i] for i in order], [received[i] for i in order], "runtime-test")
    assert probe.first_native_content_pts(mapping, 1_500_000_000) == 50
