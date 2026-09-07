"""Offline regression coverage for the live probe's native runtime namespace."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock
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
