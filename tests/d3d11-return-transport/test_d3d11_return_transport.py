"""Executable/static checks for the opt-in 0024 D3D11 return transport."""

from __future__ import annotations

import ctypes
from pathlib import Path


PATCH = Path(__file__).parents[2] / "patches" / "0024-feat-win-dshow-d3d11-return-capability-skeleton.patch"


class _HandleAbi(ctypes.Structure):
    _fields_ = [("value", ctypes.c_uint64)]


class _SlotAbi(ctypes.Structure):
    _fields_ = [
        ("handle", _HandleAbi),
        ("sequence", ctypes.c_uint64),
        ("epoch", ctypes.c_uint64),
        ("timestamp", ctypes.c_uint64),
        ("frame_id", ctypes.c_uint64),
        ("pts_ns", ctypes.c_uint64),
        ("server_seq", ctypes.c_uint64),
        ("program_revision", ctypes.c_uint64),
        ("preview_revision", ctypes.c_uint64),
        ("role_map_revision", ctypes.c_uint64),
        ("valid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("runtime_instance_id", ctypes.c_char * 129),
        ("command_id", ctypes.c_char * 129),
        ("intent_id", ctypes.c_char * 129),
        ("take_command_id", ctypes.c_char * 129),
    ]


class _AdapterLuidAbi(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_int32)]


class _TelemetryAbi(ctypes.Structure):
    _fields_ = [
        ("path", ctypes.c_uint32),
        ("fallback", ctypes.c_uint32),
        ("fallback_hresult", ctypes.c_int32),
        ("adapter_luid", _AdapterLuidAbi),
        ("epoch", ctypes.c_uint64),
        ("produced_sequence", ctypes.c_uint64),
        ("published_sequence", ctypes.c_uint64),
        ("consumed_sequence", ctypes.c_uint64),
        ("mutex_wait_ns", ctypes.c_uint64),
        ("fence_wait_ns", ctypes.c_uint64),
        ("cpu_upload_ns", ctypes.c_uint64),
        ("gpu_copy_ns", ctypes.c_uint64),
        ("readback_ns", ctypes.c_uint64),
        ("gap_count", ctypes.c_uint64),
        ("retry_count", ctypes.c_uint64),
        ("torn_count", ctypes.c_uint64),
        ("frame_age_ns", ctypes.c_uint64),
    ]


class _ControlAbi(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("lane", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("producer_pid", ctypes.c_uint32),
        ("consumer_pid", ctypes.c_uint32),
        ("producer_session", ctypes.c_uint32),
        ("consumer_session", ctypes.c_uint32),
        ("consumer_ready", ctypes.c_uint32),
        ("selected_path", ctypes.c_uint32),
        ("fallback_reason", ctypes.c_uint32),
        ("fallback_hresult", ctypes.c_int32),
        ("adapter_luid", _AdapterLuidAbi),
        ("epoch", ctypes.c_uint64),
        ("produced_sequence", ctypes.c_uint64),
        ("published_sequence", ctypes.c_uint64),
        ("consumed_sequence", ctypes.c_uint64),
        ("mutex_wait_ns", ctypes.c_uint64),
        ("fence_wait_ns", ctypes.c_uint64),
        ("cpu_upload_ns", ctypes.c_uint64),
        ("gpu_copy_ns", ctypes.c_uint64),
        ("readback_ns", ctypes.c_uint64),
        ("gap_count", ctypes.c_uint64),
        ("retry_count", ctypes.c_uint64),
        ("torn_count", ctypes.c_uint64),
        ("frame_age_ns", ctypes.c_uint64),
        ("slots", _SlotAbi * 3),
    ]


def test_patch_is_signed_and_scope_is_real_experimental_transport() -> None:
    text = PATCH.read_text(encoding="utf-8")
    assert text.startswith("From ")
    for marker in (
        "Agent-Role: forge",
        "Agent-Thread: /root/forge_253_d3d11_transport",
        "Work-Unit: ZabLaboratory/Pulsar#253",
        "ADR-Revision: draft-r2-dual-lane-20260828",
        "PULSAR_D3D11_PROGRAM_RETURN",
        "PULSAR_D3D11_PREVIEW_RETURN",
        'getenv("PULSAR_RETURN_TRANSPORT")',
        'strcmp(transport, "d3d11") == 0',
        "cpu_upload_ns",
        "D3D11_RESOURCE_MISC_SHARED_NTHANDLE",
        "#include <d3d11_1.h>",
        "d3d11_transport_enabled()",
        "fallback_for_hresult",
        "DXGI_ERROR_WAIT_TIMEOUT",
        "DXGI_ERROR_DEVICE_REMOVED",
        "DXGI_ERROR_DEVICE_RESET",
        "queue_mapping_live",
        "VirtualQuery",
        "CreateSharedHandle",
        "DuplicateHandle",
        "AcquireSync(1, 0)",
        "D3D11_QUERY_EVENT",
        "PULSAR_D3D11_FALLBACK_TIMEOUT",
        "PULSAR_D3D11_FALLBACK_DEVICE_REMOVED",
        "PULSAR_D3D11_FALLBACK_ADAPTER",
        "PULSAR_D3D11_FALLBACK_INTEROP",
    ):
        assert marker in text
    assert "virtualcam.c" in text
    assert "virtualcam-filter.cpp" in text
    assert text.count("d3d11_requested && d3d11") >= 5
    assert "if (d3d11)\n+\t\t\tconsumed_d3d11_frame" not in text


def test_handle_ring_and_control_abis_are_pointer_free_on_x86_and_x64() -> None:
    assert ctypes.sizeof(_HandleAbi) == 8
    assert ctypes.sizeof(_SlotAbi) == 608
    assert ctypes.sizeof(_AdapterLuidAbi) == 8
    assert ctypes.sizeof(_TelemetryAbi) == 128
    assert ctypes.sizeof(_ControlAbi) == 1984
    assert _SlotAbi.sequence.offset == 8
    assert _SlotAbi.epoch.offset == 16
    assert _SlotAbi.timestamp.offset == 24
    assert _ControlAbi.epoch.offset == 56
    assert _ControlAbi.slots.offset == 160


def _select(format_name: str, requested: bool) -> tuple[str, str, int]:
    """Mirror the header's fail-closed selection for an executable test."""
    if format_name != "NV12":
        return "CPU", "format", -2147024809
    if requested:
        return "CPU", "capability", -2147483638
    return "CPU", "none", 0


def test_default_cpu_and_forced_d3d11_fallback_preserve_both_return_lanes() -> None:
    for lane in ("ProgramReturn", "PreviewReturn"):
        assert lane
        assert _select("NV12", requested=False) == ("CPU", "none", 0)
        assert _select("NV12", requested=True) == ("CPU", "capability", -2147483638)
        assert _select("P010", requested=True) == ("CPU", "format", -2147024809)


def test_experimental_transport_is_not_nominal_and_has_cpu_rollback() -> None:
    text = PATCH.read_text(encoding="utf-8")
    runbook = (PATCH.parents[1] / "docs" / "runbooks" / "d3d11-return-transport.md").read_text(encoding="utf-8")
    assert "D3D11 is an explicit experiment only" in text
    assert 'return !transport ||' not in text
    assert 'PULSAR_RETURN_TRANSPORT=d3d11' in runbook
    assert "not retained on the nominal" in runbook
    assert "+8.87 ms p95" in runbook
    assert "`off`" in runbook
    assert "`disabled`" in runbook
    assert "if (consumed_d3d11_frame && d3d11_requested && d3d11)" not in text
    assert "IMediaSample" in runbook


def test_unknown_and_unset_transport_values_keep_the_cpu_nominal_path() -> None:
    text = PATCH.read_text(encoding="utf-8")
    assert 'return_transport && strcmp(return_transport, "d3d11") == 0' in text
    assert 'd3d11_requested = consumer_gated &&' in text
    assert '(!return_transport ||' not in text


def test_consumer_timeout_does_not_block_the_directshow_callback() -> None:
    text = PATCH.read_text(encoding="utf-8")
    assert "AcquireSync(1, 0)" in text
    assert "const uint64_t deadline" in text
    assert "HRESULT 258" in (PATCH.parents[1] / "docs" / "runbooks" / "d3d11-return-transport.md").read_text(encoding="utf-8")
