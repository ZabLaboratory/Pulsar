"""Measure actual decoded flash/click alignment, not merely AAC packet continuity."""
import argparse
import asyncio
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np

spec = importlib.util.spec_from_file_location("av_flash_base", Path(__file__).with_name("probe-dual-lane.py"))
base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base
spec.loader.exec_module(base)


def command(args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def inspect_sync(path):
    video = json.loads(command(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
                               "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path)]))["frames"]
    pixels = command(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-an", "-vf",
                      "scale=8:8,format=gray", "-fps_mode", "passthrough", "-f", "rawvideo", "-"])
    means = np.frombuffer(pixels, dtype=np.uint8).reshape(-1, 64).mean(axis=1)
    if len(means) != len(video):
        raise RuntimeError("decoded frame count and timestamp count disagree")
    pts = np.array([float(f["best_effort_timestamp_time"]) for f in video])
    if np.any(np.diff(pts) <= 0):
        raise RuntimeError("video PTS not strictly increasing")
    flashes = pts[np.flatnonzero((means[1:] > 220) & (means[:-1] < 32)) + 1]
    audio = json.loads(command(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_frames",
                               "-show_entries", "frame=best_effort_timestamp_time,nb_samples", "-of", "json", str(path)]))["frames"]
    audio_start = float(audio[0]["best_effort_timestamp_time"])
    for a, b in zip(audio, audio[1:]):
        expected = float(a["best_effort_timestamp_time"]) + int(a["nb_samples"]) / 48000
        if abs(float(b["best_effort_timestamp_time"]) - expected) > 0.0011:
            raise RuntimeError("audio frame PTS gap")
    pcm = np.frombuffer(command(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-vn",
                                "-ac", "1", "-ar", "48000", "-f", "f32le", "-"]), dtype="<f4")
    # 1 ms peak envelope tolerates sine zero crossings but resolves the AAC
    # onset much more accurately than one 60 Hz video frame.
    envelope = np.max(np.abs(pcm[:len(pcm)//48*48].reshape(-1, 48)), axis=1)
    if envelope.max(initial=0) < 0.2:
        raise RuntimeError("no real click signal in recorded audio")
    indices = np.flatnonzero(envelope > 0.2)
    clicks = indices[np.r_[True, np.diff(indices) > 100]] / 1000 + audio_start
    pairs = []
    for flash in flashes:
        if flash < 2 or flash > pts[-1] - 1:
            continue
        click = clicks[np.argmin(abs(clicks - flash))]
        if abs(click - flash) > 0.15:
            raise RuntimeError("flash has no paired click within 150 ms")
        pairs.append(dict(flash_s=float(flash), click_s=float(click), video_minus_audio_ms=float((flash-click)*1000)))
    if len(pairs) < 6:
        raise RuntimeError(f"only {len(pairs)} complete flash/click pairs")
    delta = [p["video_minus_audio_ms"] for p in pairs]
    return dict(pairs=pairs, count=len(pairs), min_ms=min(delta), max_ms=max(delta),
                median_ms=float(np.median(delta)), video_frames=len(video), audio_frames=len(audio),
                boundary="decoded media timestamps; not physical display or speakers")


async def drive(process, fixture, seconds):
    ready = process.wait_for(base.READY_RE, timeout=60)
    async with base.websockets.connect(ready.group(1), subprotocols=["obswebsocket.json"], open_timeout=15) as ws:
        await base.identify(ws, process.password)
        inbox = base.Inbox()
        async def request(kind, data=None):
            reply = await base.request(inbox, ws, kind, kind + str(len(process.snapshot())), data)
            base.assert_success(reply, kind)
            return reply.get("responseData", {})
        inputs = await request("GetInputList")
        for source in inputs.get("inputs", []):
            if source.get("inputKind") in ("wasapi_output_capture", "wasapi_input_capture", "wasapi_process_output_capture"):
                await request("SetInputMute", dict(inputName=source["inputName"], inputMuted=True))
        await request("CreateScene", dict(sceneName="AVFlashScene"))
        await request("CreateInput", dict(sceneName="AVFlashScene", inputName="AVFlashClick", inputKind="ffmpeg_source",
                                          inputSettings=dict(is_local_file=True, local_file=str(fixture), looping=True,
                                                             restart_on_activate=True, hw_decode=False), sceneItemEnabled=True))
        await request("SetInputMute", dict(inputName="AVFlashClick", inputMuted=False))
        await request("SetCurrentProgramScene", dict(sceneName="AVFlashScene"))
        await asyncio.sleep(2)
        await request("StartRecord")
        await base.wait_event(inbox, ws, "RecordStateChanged", lambda d: d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED")
        await asyncio.sleep(seconds)
        reply = await request("StopRecord")
        await base.wait_event(inbox, ws, "RecordStateChanged", lambda d: d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED")
        return Path(reply["outputPath"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--encoder", choices=["x264", "nvenc"], default="x264")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--current-readback", action="store_true")
    parser.add_argument("--default-readback", action="store_true")
    args = parser.parse_args()
    if args.current_readback and args.default_readback:
        parser.error("select explicit current or default readback, not both")
    args.output.mkdir(parents=True, exist_ok=False)
    fixture = args.fixture or args.output / "flash-click.mkv"
    if not args.fixture:
        command(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 "color=c=black:s=1920x1080:r=60,drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='lt(mod(t,1),0.1)'",
                 "-f", "lavfi", "-i", "aevalsrc=if(lt(mod(t\\,1)\\,0.1)\\,0.8*sin(2*PI*1000*t)\\,0):s=48000",
                 "-t", "20", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(fixture)])
    source_result = inspect_sync(fixture)
    binary_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in (args.exe, args.exe.parent / "obs.dll")}
    if args.default_readback:
        os.environ.pop("PULSAR_RAW_CURRENT_READBACK", None)
    else:
        os.environ["PULSAR_RAW_CURRENT_READBACK"] = "1" if args.current_readback else "0"
    os.environ["PULSAR_NVENC_ASYNC_OUTPUT"] = "0"
    process = base.PulsarProcess(args.exe, args.encoder, args.output / "recordings")
    try:
        process.spawn()
        recording = asyncio.run(drive(process, fixture.resolve(), 14))
    finally:
        process.shutdown()
    result = dict(schema="pulsar.flash-click.v1", encoder=args.encoder, current_readback=args.current_readback,
                  default_readback=args.default_readback,
                  source=source_result, recording=str(recording), measured=inspect_sync(recording),
                  binary_sha256=binary_hashes, fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest())
    (args.output / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
