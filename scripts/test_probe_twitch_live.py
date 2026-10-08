"""Offline regression coverage for the live probe's native runtime namespace."""
import importlib.util
import asyncio
import json
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
import uuid


def load_probe(monkeypatch, runtime=None):
    if runtime is None:
        monkeypatch.delenv('PULSAR_RUNTIME_DIR', raising=False)
    else:
        monkeypatch.setenv('PULSAR_RUNTIME_DIR', str(runtime))
    spec = importlib.util.spec_from_file_location(
        'probe_twitch_live_test', Path(__file__).with_name('probe-twitch-live.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_runtime_is_unique_and_not_binary_directory(monkeypatch):
    first = load_probe(monkeypatch)
    second = load_probe(monkeypatch)
    assert first.RUNTIME_DIR != second.RUNTIME_DIR
    assert first.RUNTIME_DIR != first.RUNDIR
    assert first.CONFIG_PATH == first.RUNTIME_DIR / 'obs-websocket/config.json'
    assert not first.RUNTIME_DIR.exists()


def test_explicit_runtime_is_forwarded_and_config_is_read_there(monkeypatch, tmp_path):
    probe = load_probe(monkeypatch, tmp_path / 'private runtime')
    monkeypatch.setattr(probe, 'LIVE_VOD_DIR', tmp_path / 'vod')
    spawn = Mock()
    monkeypatch.setattr(probe.subprocess, 'Popen', spawn)
    probe.spawn_pulsar(tmp_path / 'pulsar.exe', 60)
    env = spawn.call_args.kwargs['env']
    assert env['PULSAR_RUNTIME_DIR'] == str(probe.RUNTIME_DIR)
    assert env['PULSAR_FPS'] == '60'
    assert env['PULSAR_RESOLUTION'] == '1920x1080'
    assert env['PULSAR_VIDEO_BITRATE'] == '6000'
    probe.CONFIG_PATH.parent.mkdir()
    fixture_password = uuid.uuid4().hex
    probe.CONFIG_PATH.write_text(json.dumps({'server_port': 12345, 'server_password': fixture_password}))
    assert probe.wait_for_obs_websocket_config(0.1) == (12345, fixture_password)


def test_spawn_forwards_explicit_hosted_transport_profile(monkeypatch, tmp_path):
    probe = load_probe(monkeypatch, tmp_path / 'runtime')
    monkeypatch.setenv('LIVE_TEST_RESOLUTION', '1920x1080')
    monkeypatch.setenv('LIVE_TEST_BITRATE', '6000')
    monkeypatch.setenv('LIVE_TEST_ENCODER', 'x264')
    monkeypatch.setattr(probe, 'LIVE_VOD_DIR', tmp_path / 'vod')
    spawn = Mock()
    monkeypatch.setattr(probe.subprocess, 'Popen', spawn)
    probe.spawn_pulsar(tmp_path / 'pulsar.exe', 60)
    env = spawn.call_args.kwargs['env']
    assert env['PULSAR_FPS'] == '60'
    assert env['PULSAR_RESOLUTION'] == '1920x1080'
    assert env['PULSAR_VIDEO_BITRATE'] == '6000'
    assert env['PULSAR_VIDEO_ENCODER'] == 'x264'
    assert probe.live_resolution() == (1920, 1080)


def test_unsupported_fps_cannot_silently_fall_back_to_60(monkeypatch, tmp_path):
    import pytest
    probe = load_probe(monkeypatch, tmp_path / 'runtime')
    spawn = Mock()
    monkeypatch.setattr(probe.subprocess, 'Popen', spawn)
    with pytest.raises(ValueError, match='unsupported native FPS 15'):
        probe.spawn_pulsar(tmp_path / 'pulsar.exe', 15)
    spawn.assert_not_called()


def test_probe_fps_allowlist_matches_native_boot_parser(monkeypatch):
    import re
    probe = load_probe(monkeypatch)
    source = (probe.REPO_ROOT / 'plugins/pulsar-headless/main.cpp').read_text(encoding='utf-8')
    fps_block = source.split('std::getenv("PULSAR_FPS")', 1)[1].split('ovi.fps_num', 1)[0]
    assert {int(value) for value in re.findall(r'v == (\d+)', fps_block)} == probe.NATIVE_SUPPORTED_FPS


def test_native_video_profile_readback_is_authoritative(monkeypatch):
    probe = load_probe(monkeypatch)
    response = {'requestStatus': {'result': True}, 'responseData': {
        'baseWidth': 1920, 'baseHeight': 1080, 'outputWidth': 1920,
        'outputHeight': 1080, 'fpsNumerator': 60, 'fpsDenominator': 1,
    }}
    assert probe.video_profile_matches(response, 1920, 1080, 60)
    assert not probe.video_profile_matches(response, 1920, 1080, 15)
    assert not probe.video_profile_matches(response, 1280, 720, 60)
    response['responseData']['fpsDenominator'] = 0
    assert not probe.video_profile_matches(response, 1920, 1080, 60)


def test_27_fps_is_not_1080p60_qualification(monkeypatch):
    probe = load_probe(monkeypatch)
    samples = [{'active_fps': 27}] * 6
    assert not probe.cadence_below_target(samples, 60, 25)
    assert probe.cadence_below_target(samples, 60, 30)
    assert not probe.cadence_below_target(
        [{'active_fps': 1}] + [{'active_fps': 60}] * 5, 60, 30)


def test_encoder_family_attestation_is_exact(monkeypatch):
    probe = load_probe(monkeypatch)
    assert probe.wait_for_encoder_family(
        ['[pulsar] video encoder allocated: family=x264 id=obs_x264'], 'x264', timeout=0)
    assert not probe.wait_for_encoder_family(
        ['[pulsar] video encoder allocated: family=nvenc id=obs_nvenc_h264_tex'],
        'x264', timeout=0)


def run_start_sequence(monkeypatch, replies, budget=20.0):
    probe = load_probe(monkeypatch)
    clock = [0.0]
    calls = []
    sleeps = []
    monkeypatch.setattr(probe, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(probe, 'START_DEST_RETRY_BUDGET', budget)

    async def fake_vendor(*args, **kwargs):
        calls.append((args, kwargs))
        return replies[min(len(calls) - 1, len(replies) - 1)]

    async def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(probe, 'vendor_call', fake_vendor)
    monkeypatch.setattr(probe.asyncio, 'sleep', fake_sleep)
    result = asyncio.run(probe.start_destination(None, None, 'fixture-dest'))
    return result, calls, sleeps, clock[0]


def start_reply(error=None, *, accepted=True):
    return {'requestStatus': {'result': accepted}, 'responseData': {'responseData': {
        'started': error is None, **({'error': error} if error else {}),
    }}}


def test_start_destination_waits_for_output_then_encoders(monkeypatch):
    ready = start_reply()
    (reply, attempts), calls, sleeps, elapsed = run_start_sequence(monkeypatch, [
        start_reply('frontend streaming output unavailable'),
        start_reply('encoders not bound on streaming output'), ready,
    ])
    assert reply == ready and attempts == 3
    assert sleeps == [1.0, 1.0] and elapsed == 2.0
    assert [call[0][2] for call in calls] == ['start-dest-1', 'start-dest-2', 'start-dest-3']
    assert [call[1]['timeout'] for call in calls] == [20.0, 19.0, 18.0]


def test_start_destination_permanently_missing_encoder_exhausts_budget(monkeypatch):
    missing = start_reply('encoders not bound on streaming output')
    (reply, attempts), calls, sleeps, elapsed = run_start_sequence(monkeypatch, [missing], 2.5)
    assert reply == missing and attempts == 3
    assert sleeps == [1.0, 1.0, 0.5] and elapsed == 2.5
    assert [call[1]['timeout'] for call in calls] == [2.5, 1.5, 0.5]


def test_start_destination_unknown_error_fails_without_retry(monkeypatch):
    failure = start_reply('could not create rtmp_output')
    (reply, attempts), calls, sleeps, elapsed = run_start_sequence(monkeypatch, [failure])
    assert reply == failure and attempts == len(calls) == 1
    assert sleeps == [] and elapsed == 0


def test_start_destination_rejected_envelope_is_not_retried(monkeypatch):
    failure = start_reply('encoders not bound on streaming output', accepted=False)
    (reply, attempts), calls, sleeps, elapsed = run_start_sequence(monkeypatch, [failure])
    assert reply == failure and attempts == len(calls) == 1
    assert sleeps == [] and elapsed == 0


def test_sustained_fps_discards_warmup_and_exposes_starvation(monkeypatch):
    probe = load_probe(monkeypatch)
    assert probe.sustained_fps([{'active_fps': 1}, {'active_fps': 15}, {'active_fps': 15}]) == 15
    assert probe.sustained_fps([{'active_fps': 1}, {'active_fps': 16}, {'active_fps': 16}]) < 60 * probe.ACTIVE_FPS_RATIO_MIN
    assert probe.sustained_fps([{'active_fps': None}]) is None


def test_stop_record_pending_waits_for_inactive_status_and_path(monkeypatch):
    probe = load_probe(monkeypatch)
    replies = iter([
        {'requestStatus': {'result': True}, 'responseData': {'outputActive': True}},
        {'requestStatus': {'result': True}, 'responseData': {
            'outputActive': False, 'outputPath': 'C:/vod/final.mp4'}},
    ])

    async def fake_request(*args, **kwargs):
        return next(replies)

    monkeypatch.setattr(probe, 'request', fake_request)
    pending = {'requestStatus': {'result': False, 'code': 702}, 'responseData': {}}
    assert asyncio.run(probe.settle_record_stop(None, None, pending)) == 'C:/vod/final.mp4'


def test_stop_record_pending_finds_finalised_mp4_when_status_omits_path(monkeypatch, tmp_path):
    probe = load_probe(monkeypatch)
    monkeypatch.setattr(probe, 'LIVE_VOD_DIR', tmp_path)
    final = tmp_path / 'pulsar-final.mp4'
    final.write_bytes(b'finalised')

    async def fake_request(*args, **kwargs):
        return {'requestStatus': {'result': True}, 'responseData': {'outputActive': False}}

    monkeypatch.setattr(probe, 'request', fake_request)
    pending = {'requestStatus': {'result': False, 'code': 702}, 'responseData': {}}
    assert asyncio.run(probe.settle_record_stop(None, None, pending)) == str(final)


def test_stop_record_rejection_is_not_reclassified_as_pending(monkeypatch):
    probe = load_probe(monkeypatch)
    rejected = {'requestStatus': {'result': False, 'code': 500}, 'responseData': {}}
    assert asyncio.run(probe.settle_record_stop(None, None, rejected)) is None


def test_main_preserves_duration_and_caller_owned_directory(monkeypatch, tmp_path):
    probe = load_probe(monkeypatch, tmp_path / 'owned')
    probe.RUNTIME_DIR.mkdir()
    calls = []
    async def fake_probe(key, duration, fps):
        calls.append((duration, fps))
        return 7
    monkeypatch.setattr(probe, 'probe', fake_probe)
    monkeypatch.setattr(probe.sys, 'argv', ['probe', '--duration', '600', '--fps', '60'])
    assert probe.main() == 7
    assert calls == [(600, 60)]
    assert probe.RUNTIME_DIR.exists()


def test_main_cleans_only_generated_directory_on_failure(monkeypatch):
    probe = load_probe(monkeypatch)
    probe.RUNTIME_DIR.mkdir()
    async def fake_probe(*args):
        raise RuntimeError('synthetic startup failure')
    monkeypatch.setattr(probe, 'probe', fake_probe)
    monkeypatch.setattr(probe.sys, 'argv', ['probe'])
    import pytest
    with pytest.raises(RuntimeError, match='synthetic startup failure'):
        probe.main()
    assert not probe.RUNTIME_DIR.exists()
