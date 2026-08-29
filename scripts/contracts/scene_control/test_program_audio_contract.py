"""Static contract checks for the explicit common Program audio route (#245).

The runtime campaign lives in ``scripts/probe-program-audio.py`` and must be
run against a built Pulsar process.  These checks protect the cross-module
ABI/name/schema wiring that makes that campaign meaningful without requiring a
Windows OBS build on the contract-test runner.
"""

from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_HEADER = _ROOT / "plugins/pulsar-frontend-stub/include/pulsar-program-audio.h"
_FRONTEND = _ROOT / "plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp"
_MULTI_STREAM = _ROOT / "plugins/pulsar-multi-stream/src/plugin-main.cpp"
_MULTI_CMAKE = _ROOT / "plugins/pulsar-multi-stream/CMakeLists.txt"
_PROTOCOL = _ROOT / "docs/PROTOCOL.md"
_PROBE = _ROOT / "scripts/probe-program-audio.py"
_AUDIO_CLIENT = _ROOT / "packages/pulsar-client/src/audio.ts"
_WIRE = _ROOT / "packages/pulsar-client/src/wire.ts"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_shared_route_identity_is_single_and_explicit() -> None:
    header = _read(_HEADER)
    assert "kSchemaVersion = 1" in header
    assert 'kRouteId[] = "program-common"' in header
    assert 'kRouteName[] = "ProgramAudio"' in header
    assert 'kCutPolicy[] = "common-program-route-unchanged"' in header

    frontend = _read(_FRONTEND)
    assert '#include "pulsar-program-audio.h"' in frontend
    assert "audio_t *programAudio = nullptr;" in frontend
    assert "programAudio = obs_get_audio();" in frontend
    assert "obs_encoder_set_audio(enc, programAudio);" in frontend
    assert "obs_encoder_set_audio(enc, obs_get_audio())" not in frontend
    assert "ProgramAudioRoute=%s ProgramAudio=%p" in frontend


def test_route_observer_reads_encoder_mixers_and_pts_and_unhooks() -> None:
    source = _read(_MULTI_STREAM)
    cmake = _read(_MULTI_CMAKE)
    assert '#include <pulsar-program-audio.h>' in source
    assert '"GetProgramAudioRoute"' in source
    assert "class ProgramAudioObserver" in source
    assert "obs_add_raw_audio_callback(mixer, nullptr, program_audio_callback" in source
    assert "obs_remove_raw_audio_callback(track->mixerIndex, program_audio_callback" in source
    assert '"route_id", pulsar_program_audio::kRouteId' in source
    assert '"audio_identity"' in source
    assert '"audio_matches_route"' in source
    assert '"pts_regressions"' in source
    assert '"pts_monotone"' in source
    assert '"series_ns"' in source
    assert '"preview_audio_supported", false' in source
    assert '"afv_supported", false' in source
    assert "PULSAR_FRONTEND_STUB_HEADERS" in cmake


def test_protocol_and_client_document_the_r2_audio_boundary() -> None:
    protocol = _read(_PROTOCOL)
    assert "GetProgramAudioRoute" in protocol
    assert "route_id=program-common" in protocol
    assert "Preview audio and AFV (audio-follow-video) are unsupported in r2." in protocol
    assert "audio_matches_route" in protocol
    assert "pts.series_ns" in protocol

    audio = _read(_AUDIO_CLIENT)
    wire = _read(_WIRE)
    assert '"GetProgramAudioRoute"' in audio
    assert "programAudioRouteFromWire" in audio
    assert "WireGetProgramAudioRouteResponse" in wire
    assert "programAudioRouteFromWire" in wire


def test_runtime_probe_runs_real_cuts_and_checks_audio_isolation() -> None:
    probe = _read(_PROBE)
    assert "GetProgramAudioRoute" in probe
    assert "common-program-route-unchanged" in probe
    assert "preview_audio_supported" in probe
    assert "afv_supported" in probe
    assert "pts_regressions" in probe
    assert "Preview video mutation" in probe
    assert "--takes" in probe
    assert "default=100" in probe or "default=100" in probe.replace(" ", "")
    assert "verify_recording(output_path, ffprobe)" in probe
    assert '"route_snapshots"' in probe
