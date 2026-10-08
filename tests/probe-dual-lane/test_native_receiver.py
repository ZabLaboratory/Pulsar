"""The native receiver is an observer, never a synthetic decode timestamp."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

spec = importlib.util.spec_from_file_location("native_receiver_probe", Path(__file__).parents[2] / "scripts/probe-dual-lane.py")
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


def receiver():
    return probe.RtmpReceiver("unused", runtime_id="runtime-test", stream_id="stream-test",
                              decode_frames=True, audit_marker=True, decoder_mode="native-software")


def ready(r):
    r._consume_native_line(json.dumps(dict(kind="ready", clock="libobs_qpc", clock_frequency=10_000_000,
                                          threads=1, avcodec_version=61)))


def test_native_observation_preserves_clock_and_pixels_without_pipe_timestamp():
    r = receiver()
    ready(r)
    r._consume_native_line(json.dumps(dict(kind="packet", packet_index=0, packet_pts=33, packet_dts=0,
                                          observed_at_monotonic_ns=123456789)))
    r._consume_native_line(json.dumps(dict(kind="frame", frame_index=0, pts_ms=33, y_mean=16, u_mean=128,
                                          v_mean=128, observed_at_monotonic_ns=123459789)))
    r._consume_native_line(json.dumps(dict(kind="end", packets=1, frames=1)))
    assert r.packets[0]["observed_at_monotonic_ns"] == 123456789
    assert r.decoded_frames[0]["observed_at_monotonic_ns"] == 123459789
    assert r.decoded_frames[0]["y_mean"] == 16
    assert r.metadata()["clock_bound_ns"] == 100
    assert r.metadata()["receiver_id"] == "pulsar-native-rtmp-receiver"


@pytest.mark.parametrize("record", [
    dict(kind="ready", clock="python", clock_frequency=10_000_000, threads=1),
    dict(kind="ready", clock="libobs_qpc", clock_frequency=0, threads=1),
    dict(kind="ready", clock="libobs_qpc", clock_frequency=10_000_000, threads=2),
    dict(kind="packet", packet_index=0, packet_pts=33, packet_dts=0, observed_at_monotonic_ns=1),
])
def test_invalid_or_missing_clock_identity_fails(record):
    with pytest.raises(probe.ProbeFailure):
        receiver()._consume_native_line(json.dumps(record))


@pytest.mark.parametrize("record", [
    dict(kind="packet", packet_index=1, packet_pts=33, packet_dts=0, observed_at_monotonic_ns=1),
    dict(kind="frame", frame_index=1, pts_ms=33, y_mean=16, u_mean=128, v_mean=128, observed_at_monotonic_ns=1),
    dict(kind="frame", frame_index=0, pts_ms=33, y_mean=256, u_mean=128, v_mean=128, observed_at_monotonic_ns=1),
    dict(kind="frame", frame_index=0, pts_ms=33, y_mean=16, u_mean=128, observed_at_monotonic_ns=1),
    dict(kind="packet", packet_index=0, packet_pts=33, packet_dts=0, observed_at_monotonic_ns=0),
    dict(kind="end", packets=1, frames=0), dict(kind="error", code=-1), dict(kind="unknown"),
])
def test_missing_data_and_decoder_errors_are_not_accepted(record):
    r = receiver()
    ready(r)
    with pytest.raises(probe.ProbeFailure):
        r._consume_native_line(json.dumps(record))
