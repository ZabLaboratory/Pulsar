"""Cross-boundary contract checks for issue #252's AFV descope.

The test is intentionally static: AFV is not implemented in r2 and this unit
must not add a second audio state machine.  It protects the producer -> gateway
-> client/probe boundary and makes the descope fail closed if a future edit
silently turns a video Cut into an audio permutation.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs/issue-252/preview-audio-afv-descope-v1.md"
FRONTEND = ROOT / "plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp"
HEADER = ROOT / "plugins/pulsar-frontend-stub/include/pulsar-program-audio.h"
GATEWAY = ROOT / "plugins/pulsar-multi-stream/src/plugin-main.cpp"
GATEWAY_CMAKE = ROOT / "plugins/pulsar-multi-stream/CMakeLists.txt"
CLIENT_AUDIO = ROOT / "packages/pulsar-client/src/audio.ts"
CLIENT_WIRE = ROOT / "packages/pulsar-client/src/wire.ts"
PROBE = ROOT / "scripts/probe-program-audio.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_versioned_descope_has_required_contract_sections() -> None:
    doc = read(DOC)
    required = (
        "pulsar.preview-audio-afv.descope.v1",
        "ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane-20260828",
        "Status: `DESCOPED`",
        "## Decision and authority",
        "## Producer, gateway and consumer matrix",
        "## Program / Preview behavior matrix",
        "## Normative invariants",
        "## Loss, recovery and observability",
        "## Rollout and rollback",
        "## Validation contract",
        "preview_audio_supported=false",
        "afv_supported=false",
    )
    for marker in required:
        assert marker in doc


def test_producer_only_binds_the_common_audio_bus() -> None:
    frontend = read(FRONTEND)
    header = read(HEADER)
    assert 'kRouteId[] = "program-common"' in header
    assert 'kRouteName[] = "ProgramAudio"' in header
    assert 'kCutPolicy[] = "common-program-route-unchanged"' in header
    assert "programAudio = obs_get_audio();" in frontend
    assert "obs_encoder_set_audio(enc, programAudio);" in frontend
    assert "obs_encoder_set_audio(enc, obs_get_audio())" not in frontend
    assert "preview_audio_supported=false afv_supported=false" in frontend

    # A future AFV command must not be smuggled into the existing producer
    # surface or make the video Cut path select audio by lane/scene.
    for forbidden in (
        "SetPreviewAudio",
        "SetAFV",
        "AudioFollowVideo",
        "audio_follow_video",
        "audioLane",
        "previewAudioRoute",
    ):
        assert forbidden not in frontend


def test_gateway_and_build_wiring_keep_descope_fail_closed() -> None:
    gateway = read(GATEWAY)
    cmake = read(GATEWAY_CMAKE)
    assert '#include <pulsar-program-audio.h>' in gateway
    assert '"GetProgramAudioRoute"' in gateway
    assert 'obs_websocket_vendor_register_request(g_vendor, "GetProgramAudioRoute"' in gateway
    assert '"preview_audio_supported", false' in gateway
    assert '"afv_supported", false' in gateway
    assert '"audio_matches_route"' in gateway
    assert '"route_error"' in gateway
    assert '"pts_regressions"' in gateway
    assert '"pts_monotone"' in gateway
    assert 'PULSAR_FRONTEND_STUB_HEADERS' in cmake

    # The gateway must not gain a parallel AFV endpoint under the existing
    # vendor/version boundary.
    for forbidden in (
        '"SetPreviewAudio"',
        '"SetAFV"',
        '"GetPreviewAudioRoute"',
        '"AudioFollowVideo"',
    ):
        assert forbidden not in gateway


def test_client_and_probe_consume_the_same_explicit_false_flags() -> None:
    audio = read(CLIENT_AUDIO)
    wire = read(CLIENT_WIRE)
    probe = read(PROBE)
    assert "GetProgramAudioRoute" in audio
    assert "programAudioRouteFromWire" in audio
    assert "GetProgramAudioRoute" in wire
    assert "preview_audio_supported" in wire
    assert "afv_supported" in wire
    assert "GetProgramAudioRoute" in probe
    assert "preview_audio_supported" in probe
    assert "afv_supported" in probe
    assert "WireGetProgramAudioRouteResponse" in wire
    assert "programAudioRouteFromWire" in wire
    assert "common-program-route-unchanged" in probe
    assert "pts_regressions" in probe
    assert "Preview video mutation" in probe
    assert "--takes" in probe
