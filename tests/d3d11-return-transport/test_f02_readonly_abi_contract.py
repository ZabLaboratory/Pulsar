"""Static contract for the producer-authoritative D3D11 ABI v2 boundary."""

from pathlib import Path


ROOT = Path(__file__).parents[2]
PATCH = next(ROOT.glob("patches/0041-fix-d3d11-producer-authoritative-readonly-*.patch"))


def _patch_source(path: str) -> str:
    text = PATCH.read_text(encoding="utf-8")
    marker = f"diff --git a/{path} b/{path}"
    start = text.index(marker)
    end = text.find("\ndiff --git ", start + len(marker))
    return text[start:] if end < 0 else text[start:end]


def _added_source(path: str) -> str:
    return "\n".join(
        line[1:]
        for line in _patch_source(path).splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def test_consumer_uses_readonly_abi_v2_and_rejects_mixed_versions() -> None:
    header = _patch_source("plugins/win-dshow/virtualcam-module/d3d11-return-transport.hpp")
    transport = _patch_source("plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp")

    assert "#define PULSAR_D3D11_RETURN_ABI_VERSION 2U" in header
    assert "pulsar_d3d11_return_producer_set_consumer_pid" in header
    assert "OpenFileMappingW(FILE_MAP_READ, FALSE, map_name.c_str())" in transport
    assert "mapping, FILE_MAP_READ" in transport
    assert "consumer->control->abi_version != PULSAR_D3D11_RETURN_ABI_VERSION" in transport
    assert "consumer->control->lane != static_cast<uint32_t>(lane)" in transport


def test_consumer_has_no_control_mapping_writes() -> None:
    transport = _patch_source("plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp")
    added = _added_source("plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp")
    consumer = transport[transport.index("struct pulsar_d3d11_return_consumer") :]

    assert "consumer->control->consumer_pid =" not in added
    assert "consumer->control->consumer_session =" not in added
    assert "consumer->control->consumer_ready" not in added
    assert "consumer->control->slots[i].handle.value = 0" not in added
    assert "consumer->control->consumed_sequence" not in added
    assert "consumer->control->gap_count" not in added
    assert "consumer->control->retry_count" not in added
    assert "consumer_report_fallback" in consumer


def test_producer_authorizes_pid_session_before_ring_and_pipe_gate_updates_it() -> None:
    transport = _patch_source("plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp")
    virtualcam = _patch_source("plugins/win-dshow/virtualcam.c")
    prepare = transport[transport.index("static bool producer_prepare_ring") :]

    assert "pulsar_d3d11_return_producer_set_consumer_pid" in transport
    assert "OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, FALSE, pid)" in transport
    assert "process_session(pid)" in transport
    assert "authenticated_consumer_pid" in prepare
    assert "authenticated_consumer_session" in prepare
    assert "producer->control->consumer_pid = pid" in prepare
    assert "producer->control->consumer_session = authenticated_session" in prepare
    assert "return_consumer_registration_live(vcam, &registration_pid)" in virtualcam
    assert "active ? registration_pid : 0" in virtualcam
    assert "active = active && producer_authorized" in virtualcam
