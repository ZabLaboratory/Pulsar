import importlib.util
from pathlib import Path
import sys

import pytest

spec = importlib.util.spec_from_file_location("marker_metadata_probe", Path(__file__).parents[2] / "scripts/probe-dual-lane.py")
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


def test_complete_metadata_survives_interspersed_demux_logs_and_uses_completion_time():
    parser = probe.DecodedMarkerMetadata()
    assert parser.feed("[debug partial] frame:0 pts:33 pts_time:0.033", 10) is None
    assert parser.feed("lavfi.signalstats.YAVG=235.02", 20) is None
    assert parser.feed("[demuxer] packet pts=66", 30) is None
    assert parser.feed("[another partial] lavfi.signalstats.UAVG=127.99", 40) is None
    frame = parser.feed("lavfi.signalstats.VAVG=128.1", 50)
    assert frame == {"frame_index": 0, "pts_ms": 33, "y_mean": 235,
                     "u_mean": 128, "v_mean": 128, "observed_at_monotonic_ns": 50}
    assert probe.decoded_marker_lane(frame) == "B"


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "256", "no"])
def test_invalid_plane_values_are_rejected(value):
    parser = probe.DecodedMarkerMetadata()
    parser.feed("frame:0 pts:33 pts_time:0.033", 1)
    with pytest.raises(probe.ProbeFailure, match="invalid"):
        parser.feed("lavfi.signalstats.YAVG=" + value, 2)


def test_incomplete_or_duplicate_frames_cannot_be_silently_skipped():
    parser = probe.DecodedMarkerMetadata()
    parser.feed("frame:0 pts:33 pts_time:0.033", 1)
    with pytest.raises(probe.ProbeFailure, match="before all three"):
        parser.feed("frame:1 pts:50 pts_time:0.050", 2)
    parser = probe.DecodedMarkerMetadata()
    with pytest.raises(probe.ProbeFailure, match="no frame identity"):
        parser.feed("lavfi.signalstats.YAVG=16", 1)
    parser.feed("frame:0 pts:33 pts_time:0.033", 2)
    parser.feed("lavfi.signalstats.YAVG=16", 3)
    with pytest.raises(probe.ProbeFailure, match="repeats"):
        parser.feed("lavfi.signalstats.YAVG=16", 4)


def test_receiver_default_does_not_bypass_software_reordering():
    receiver = probe.RtmpReceiver("ffmpeg", runtime_id="runtime-test", stream_id="stream-test", decode_frames=True)
    assert receiver.decoder_input_options() == ["-copyts", "-threads", "1"]
    receiver = probe.RtmpReceiver("ffmpeg", runtime_id="runtime-test", stream_id="stream-test", decode_frames=True,
                                  decoder_mode="nvdec-lowdelay")
    assert receiver.decoder_input_options() == ["-copyts", "-threads", "1", "-c:v", "h264_cuvid", "-flags", "low_delay"]
    with pytest.raises(probe.ProbeFailure, match="requires decoding"):
        probe.RtmpReceiver("ffmpeg", runtime_id="runtime-test", stream_id="stream-test", decoder_mode="nvdec-lowdelay")
