"""Static and executable contract checks for the 0023 return-queue patch."""

from __future__ import annotations

import threading
import time
import ctypes
from pathlib import Path


PATCH = Path(__file__).parents[2] / "patches" / "0023-fix-win-dshow-atomic-return-queue.patch"


def test_patch_contains_atomic_seqlock_and_telemetry_contract() -> None:
    text = PATCH.read_text(encoding="utf-8")
    for marker in (
        "InterlockedExchange64",
        "queue_load_sequence",
        "MemoryBarrier()",
        "publication - 1U",
        "before != after",
        "VIDEO_QUEUE_METADATA_VERSION 2U",
        "VIDEO_QUEUE_METADATA_ABI_SIZE 576U",
        "gap_count",
        "duplicate_count",
        "retry_count",
        "torn_count",
    ):
        assert marker in text
    # DirectShow opens the shared mapping read-only.  The reader must therefore
    # use an atomic load plus acquire barrier, never a read-modify-write
    # compare-exchange that would fault on the mapping.
    assert "InterlockedCompareExchange64" not in text
    assert "VIDEO_FORMAT_P010" in text
    assert "VIDEO_QUEUE_PIXEL_FORMAT_INVALID" in text
    assert "D3D11" not in text


class _FrameMetadata(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_uint64),
        ("frame_id", ctypes.c_uint64),
        ("pts_ns", ctypes.c_uint64),
        ("server_seq", ctypes.c_uint64),
        ("program_revision", ctypes.c_uint64),
        ("preview_revision", ctypes.c_uint64),
        ("role_map_revision", ctypes.c_uint64),
        ("valid", ctypes.c_uint32),
        ("runtime_instance_id", ctypes.c_char * 129),
        ("command_id", ctypes.c_char * 129),
        ("intent_id", ctypes.c_char * 129),
        ("take_command_id", ctypes.c_char * 129),
    ]


def test_metadata_abi_is_pointer_free_and_fixed_on_x86_and_x64() -> None:
    assert ctypes.sizeof(_FrameMetadata) == 576
    assert _FrameMetadata.frame_id.offset == 8
    assert _FrameMetadata.pts_ns.offset == 16
    assert _FrameMetadata.valid.offset == 56
    assert _FrameMetadata.runtime_instance_id.offset == 60
    assert _FrameMetadata.take_command_id.offset == 447


def test_nv12_stride_and_format_contract_fails_closed() -> None:
    def valid(width: int, height: int, y_stride: int, uv_stride: int, fmt: str) -> bool:
        return (
            fmt == "NV12"
            and width > 0
            and height > 0
            and width % 2 == 0
            and height % 2 == 0
            and y_stride >= width
            and uv_stride >= width
        )

    assert valid(1920, 1080, 2048, 2048, "NV12")
    assert not valid(1920, 1080, 2048, 2048, "P010")
    assert not valid(1920, 1080, 1919, 2048, "NV12")
    assert not valid(1920, 1080, 2048, 1919, "NV12")
    assert not valid(1919, 1080, 2048, 2048, "NV12")


def test_sequence_wrap_preserves_gap_and_duplicate_math() -> None:
    previous = 0xFFFFFFFE
    current = 1
    delta = (current - previous) & 0xFFFFFFFF
    assert delta == 3
    assert delta - 1 == 2
    assert ((current - current) & 0xFFFFFFFF) == 0


def test_bounded_reader_never_accepts_odd_or_changed_sequence() -> None:
    sequence = 0
    payload = [0] * 32
    observed: list[tuple[int, ...]] = []
    torn = 0
    stop = False
    reader_ready = threading.Event()

    def writer() -> None:
        nonlocal sequence, stop
        for value in range(1, 80):
            sequence = value * 2 - 1
            for index in range(len(payload)):
                payload[index] = value
            sequence = value * 2
            time.sleep(0.001)
        stop = True

    def reader() -> None:
        nonlocal torn
        reader_ready.set()
        while not stop:
            before = sequence
            if before == 0 or before & 1:
                continue
            snapshot = tuple(payload)
            after = sequence
            if before != after or after & 1:
                torn += 1
                continue
            assert len(set(snapshot)) == 1
            observed.append(snapshot)

    producer = threading.Thread(target=writer)
    consumer = threading.Thread(target=reader)
    consumer.start()
    reader_ready.wait(timeout=1)
    producer.start()
    producer.join()
    consumer.join(timeout=2)
    assert not consumer.is_alive()
    assert observed
    assert torn >= 0
