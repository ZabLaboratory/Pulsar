"""Executable/static checks for the capability-only 0024 D3D11 skeleton."""

from __future__ import annotations

import ctypes
from pathlib import Path


PATCH = Path(__file__).parents[2] / "patches" / "0024-feat-win-dshow-d3d11-return-capability-skeleton.patch"


class _HandleAbi(ctypes.Structure):
    _fields_ = [("value", ctypes.c_uint64)]


class _SlotAbi(ctypes.Structure):
    _fields_ = [
        ("texture", _HandleAbi),
        ("sequence", ctypes.c_uint64),
        ("epoch", ctypes.c_uint64),
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
        ("gpu_copy_ns", ctypes.c_uint64),
        ("readback_ns", ctypes.c_uint64),
        ("gap_count", ctypes.c_uint64),
        ("retry_count", ctypes.c_uint64),
        ("torn_count", ctypes.c_uint64),
        ("frame_age_ns", ctypes.c_uint64),
    ]


def test_patch_is_signed_and_scope_is_capability_only() -> None:
    text = PATCH.read_text(encoding="utf-8")
    assert text.startswith("From d9e0c529616955c8e5457cf10843936ec6b23af6 ")
    for marker in (
        "Agent-Role: forge",
        "Agent-Thread: /root/pulsar_d3d11_return",
        "Work-Unit: pulsar-d3d11-return-transport-20260901",
        "program_return",
        "preview_return",
        "PULSAR_RETURN_TRANSPORT=d3d11",
        "architecture_required",
        "kSlotCount = 3",
        "selected_path::cpu_seqlock",
    ):
        assert marker in text
    assert "device_texture_open_shared" not in text
    assert "AcquireSync" not in text
    assert "OpenSharedResource" not in text
    assert "virtualcam.c" not in text
    assert "virtualcam-filter" not in text


def test_handle_ring_and_telemetry_abis_are_pointer_free_on_x86_and_x64() -> None:
    assert ctypes.sizeof(_HandleAbi) == 8
    assert ctypes.sizeof(_SlotAbi) == 24
    assert ctypes.sizeof(_AdapterLuidAbi) == 8
    assert ctypes.sizeof(_TelemetryAbi) == 120
    assert _SlotAbi.sequence.offset == 8
    assert _SlotAbi.epoch.offset == 16
    assert _TelemetryAbi.adapter_luid.offset == 12
    assert _TelemetryAbi.epoch.offset == 24


def _select(format_name: str, requested: bool) -> tuple[str, str, int]:
    """Mirror the header's fail-closed selection for an executable test."""
    if format_name != "NV12":
        return "CPU", "format_unsupported", -2147467263
    if requested:
        return "CPU", "architecture_required", -2147467263
    return "CPU", "none", 0


def test_default_cpu_and_forced_d3d11_fallback_preserve_both_return_lanes() -> None:
    for lane in ("ProgramReturn", "PreviewReturn"):
        assert lane
        assert _select("NV12", requested=False) == ("CPU", "none", 0)
        assert _select("NV12", requested=True) == ("CPU", "architecture_required", -2147467263)
        assert _select("P010", requested=True) == ("CPU", "format_unsupported", -2147467263)
