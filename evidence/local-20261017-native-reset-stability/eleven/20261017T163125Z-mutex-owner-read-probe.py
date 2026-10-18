#!/usr/bin/env python3
"""Read only the PDB-typed pthread mutex owner fields from one explicitly supplied PID.

The caller must derive --mutex-pointer-slot from the frozen waiter's stack.
This probe refuses mismatched private DLLs and never writes target memory.
It reads at most 72 bytes in six fixed-size ReadProcessMemory calls.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import struct
import sys
from datetime import datetime, timezone
from ctypes import wintypes

PROCESS_VM_READ = 0x0010
PROCESS_DUP_HANDLE = 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DUPLICATE_SAME_ACCESS = 0x00000002
TH32CS_SNAPTHREAD = 0x00000004
LIST_MODULES_ALL = 0x00000003
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_USER_ADDRESS = 0x00007FFFFFFFFFFF

EXPECTED = {
    "w32-pthreads.dll": "296dd07553f0f4293354281ce0540fe2dfe8d9a4d8a197994343e718694267c9",
    "pulsar-capture-effects.dll": "efb62a4996a7962fa65b7a28d5cc444aba07d6c21aea67a9e476a89778ae2dfa",
}
LAYOUT = {
    "pdbGuid": "0c146b86-591e-6192-622d-0e72837eebde",
    "pdbAge": 1,
    "pdbSha256": "85100dc57849c760761a64d68eaa27ad3075b513d0069617b64d520b8da9f682",
    "pthreadMutexTypedef": "pointer -> pthread_mutex_t_",
    "pthreadMutexSize": 48,
    "pthreadMutexFields": {
        "lock_idx": {"offset": 0, "type": "CodeView btLong (32-bit signed)"},
        "recursive_count": {"offset": 4, "type": "32-bit signed"},
        "kind": {"offset": 8, "type": "32-bit signed"},
        "ownerThread": {"offset": 16, "type": "ptw32_handle_t[16]"},
        "event": {"offset": 32, "type": "HANDLE[8]"},
        "robustNode": {"offset": 40, "type": "pointer[8]"},
    },
    "ptw32Handle": {
        "size": 16,
        "p": {"offset": 0, "type": "void*[8]"},
        "x": {"offset": 8, "type": "32-bit unsigned"},
    },
    "ptw32Thread": {
        "size": 432,
        "seqNumber": {"offset": 0, "type": "64-bit unsigned"},
        "threadH": {"offset": 8, "type": "HANDLE[8]"},
        "ptHandle": {"offset": 16, "type": "ptw32_handle_t[16]"},
        "state": {"offset": 40, "type": "PThreadState[4]"},
    },
}

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
HMODULE = ctypes.c_void_p

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = HANDLE
kernel32.CloseHandle.argtypes = [HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.ReadProcessMemory.argtypes = [HANDLE, ctypes.c_void_p, ctypes.c_void_p, SIZE_T, ctypes.POINTER(SIZE_T)]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.GetCurrentProcess.restype = HANDLE
kernel32.DuplicateHandle.argtypes = [HANDLE, HANDLE, HANDLE, ctypes.POINTER(HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.DuplicateHandle.restype = wintypes.BOOL
kernel32.GetThreadId.argtypes = [HANDLE]
kernel32.GetThreadId.restype = wintypes.DWORD
kernel32.GetProcessIdOfThread.argtypes = [HANDLE]
kernel32.GetProcessIdOfThread.restype = wintypes.DWORD
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = HANDLE

psapi.EnumProcessModulesEx.argtypes = [HANDLE, ctypes.POINTER(HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
psapi.EnumProcessModulesEx.restype = wintypes.BOOL
psapi.GetModuleFileNameExW.argtypes = [HANDLE, HMODULE, wintypes.LPWSTR, wintypes.DWORD]
psapi.GetModuleFileNameExW.restype = wintypes.DWORD


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


kernel32.Thread32First.argtypes = [HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32First.restype = wintypes.BOOL
kernel32.Thread32Next.argtypes = [HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32Next.restype = wintypes.BOOL


def winerror(action: str) -> RuntimeError:
    return RuntimeError(f"{action} failed with Win32 error {ctypes.get_last_error()}")


def parse_address(value: str) -> int:
    address = int(value, 0)
    if address <= 0 or address > MAX_USER_ADDRESS or address % 8:
        raise argparse.ArgumentTypeError("expected an aligned canonical 64-bit user address")
    return address


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def process_image(process: HANDLE) -> str:
    buffer = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(buffer))
    if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
        raise winerror("QueryFullProcessImageNameW")
    return buffer.value


def process_modules(process: HANDLE) -> list[str]:
    capacity = 4096
    modules = (HMODULE * capacity)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcessModulesEx(process, modules, ctypes.sizeof(modules), ctypes.byref(needed), LIST_MODULES_ALL):
        raise winerror("EnumProcessModulesEx")
    if needed.value > ctypes.sizeof(modules):
        raise RuntimeError("module table exceeded the fixed 4096-entry bound")
    paths = []
    count = needed.value // ctypes.sizeof(HMODULE)
    for module in modules[:count]:
        buffer = ctypes.create_unicode_buffer(32768)
        length = psapi.GetModuleFileNameExW(process, module, buffer, len(buffer))
        if length:
            paths.append(buffer.value)
    return paths


def target_thread_ids(pid: int) -> set[int]:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise winerror("CreateToolhelp32Snapshot")
    found: set[int] = set()
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                found.add(int(entry.th32ThreadID))
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def read_exact(process: HANDLE, address: int, length: int, label: str, counter: list[int]) -> bytes:
    buffer = ctypes.create_string_buffer(length)
    received = SIZE_T()
    ok = kernel32.ReadProcessMemory(process, ctypes.c_void_p(address), buffer, length, ctypes.byref(received))
    counter[0] += int(received.value)
    if not ok or received.value != length:
        raise RuntimeError(f"ReadProcessMemory({label}, {address:#x}, {length}) returned {received.value} bytes; Win32 error {ctypes.get_last_error()}")
    return buffer.raw[:length]


def resolve_owner_tid(process: HANDLE, remote_thread_handle: int, pid: int) -> dict:
    if not remote_thread_handle or remote_thread_handle > MAX_USER_ADDRESS:
        return {"resolved": False, "reason": "invalid_remote_thread_handle"}
    duplicate = HANDLE()
    ok = kernel32.DuplicateHandle(
        process,
        HANDLE(remote_thread_handle),
        kernel32.GetCurrentProcess(),
        ctypes.byref(duplicate),
        0,
        False,
        DUPLICATE_SAME_ACCESS,
    )
    if not ok:
        return {"resolved": False, "reason": "DuplicateHandle_failed", "win32Error": ctypes.get_last_error()}
    try:
        tid = int(kernel32.GetThreadId(duplicate))
        owner_pid = int(kernel32.GetProcessIdOfThread(duplicate))
        return {
            "resolved": bool(tid and owner_pid == pid),
            "tid": tid or None,
            "processId": owner_pid or None,
            "sameTargetProcess": owner_pid == pid,
        }
    finally:
        kernel32.CloseHandle(duplicate)


def capture(pid: int, mutex_pointer_slot: int) -> dict:
    rights = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ | PROCESS_DUP_HANDLE
    process = kernel32.OpenProcess(rights, False, pid)
    if not process:
        raise winerror("OpenProcess")
    read_bytes = [0]
    try:
        image = process_image(process)
        paths = process_modules(process)
        identities: dict[str, dict] = {}
        for wanted, expected_hash in EXPECTED.items():
            matches = [path for path in paths if os.path.basename(path).casefold() == wanted]
            if len(matches) != 1:
                raise RuntimeError(f"expected exactly one loaded {wanted}, found {len(matches)}")
            path = matches[0]
            actual_hash = sha256_file(path)
            identities[wanted] = {"path": path, "sha256": actual_hash, "matchesPinnedBuild": actual_hash == expected_hash}
            if actual_hash != expected_hash:
                raise RuntimeError(f"{wanted} hash mismatch: expected {expected_hash}, got {actual_hash}")

        mutex_pointer_bytes = read_exact(process, mutex_pointer_slot, 8, "pthread_mutex_t pointer slot", read_bytes)
        internal_mutex = struct.unpack("<Q", mutex_pointer_bytes)[0]
        if internal_mutex <= 0 or internal_mutex > MAX_USER_ADDRESS or internal_mutex % 8:
            raise RuntimeError(f"mutex pointer slot yielded invalid pointer {internal_mutex:#x}")

        lock_metadata = read_exact(process, internal_mutex, 12, "pthread_mutex_t_ metadata", read_bytes)
        lock_idx, recursive_count, kind = struct.unpack("<iii", lock_metadata)
        owner_handle_bytes = read_exact(process, internal_mutex + 16, 16, "pthread_mutex_t_.ownerThread", read_bytes)
        owner_p, owner_x = struct.unpack_from("<QI", owner_handle_bytes, 0)

        w32_path = identities["w32-pthreads.dll"]["path"]
        pdb_path = os.path.splitext(w32_path)[0] + ".pdb"
        pdb_hash = sha256_file(pdb_path)
        expected_pdb_hash = LAYOUT["pdbSha256"]
        if pdb_hash != expected_pdb_hash:
            raise RuntimeError(f"w32-pthreads.pdb hash mismatch: expected {expected_pdb_hash}, got {pdb_hash}")
        identities["w32-pthreads.pdb"] = {"path": pdb_path, "sha256": pdb_hash, "matchesPinnedBuild": True}
        output = {
            "schema": "pulsar.w32-pthreads-owner-read/1",
            "capturedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "pid": pid,
            "processImage": image,
            "moduleIdentities": identities,
            "layoutProvenance": LAYOUT,
            "addressInputs": {"pthreadMutexPointerSlot": hex(mutex_pointer_slot)},
            "readBytes": None,
            "mutex": {
                "internalAddress": hex(internal_mutex),
                "lock_idx": lock_idx,
                "recursive_count": recursive_count,
                "kind": kind,
                "ownerThread": {"p": hex(owner_p) if owner_p else None, "x": owner_x},
            },
        }

        if owner_p == 0:
            output["ownerResolution"] = {"resolved": False, "reason": "ownerThread.p_is_null"}
            output["readBytes"] = read_bytes[0]
            return output
        if owner_p > MAX_USER_ADDRESS or owner_p % 8:
            output["ownerResolution"] = {"resolved": False, "reason": "ownerThread.p_is_not_a_valid_user_pointer"}
            output["readBytes"] = read_bytes[0]
            return output

        thread_prefix = read_exact(process, owner_p, 32, "ptw32_thread_t_ prefix", read_bytes)
        seq_number, thread_handle, self_p, self_x = struct.unpack("<QQQI", thread_prefix[:28])
        thread_state_bytes = read_exact(process, owner_p + 40, 4, "ptw32_thread_t_.state", read_bytes)
        thread_state = struct.unpack("<I", thread_state_bytes)[0]
        self_handle_matches = self_p == owner_p and self_x == owner_x
        thread_ids = target_thread_ids(pid)
        tid_result = resolve_owner_tid(process, thread_handle, pid) if self_handle_matches else {
            "resolved": False,
            "reason": "embedded_ptHandle_does_not_match_mutex_ownerThread",
        }
        resolved_tid = tid_result.get("tid")
        output["ownerResolution"] = {
            "resolved": bool(tid_result.get("resolved") and resolved_tid in thread_ids),
            "ownerThreadStructAddress": hex(owner_p),
            "seqNumber": seq_number,
            "threadH": hex(thread_handle) if thread_handle else None,
            "ptHandle": {"p": hex(self_p) if self_p else None, "x": self_x},
            "ptHandleMatchesMutexOwnerThread": self_handle_matches,
            "state": thread_state,
            "threadHandleResolution": tid_result,
            "tidPresentInTargetThreadSnapshot": resolved_tid in thread_ids if resolved_tid else False,
        }
        output["readBytes"] = read_bytes[0]
        if output["readBytes"] > 72:
            raise AssertionError(f"fixed memory-read budget exceeded: {output['readBytes']}")
        return output
    finally:
        kernel32.CloseHandle(process)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True, help="PID of the exact private Pulsar host")
    parser.add_argument(
        "--mutex-pointer-slot",
        type=parse_address,
        required=True,
        help="live address of the pthread_mutex_t variable passed by the frozen waiter",
    )
    parser.add_argument("--output", help="optional new JSON file; must be in this script's evidence directory and must not exist")
    args = parser.parse_args()

    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        parser.error("this probe requires 64-bit Windows")
    try:
        result = capture(args.pid, args.mutex_pointer_slot)
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            evidence_dir = os.path.realpath(os.path.dirname(__file__))
            output_path = os.path.realpath(args.output)
            if os.path.dirname(output_path) != evidence_dir:
                raise RuntimeError("--output must be in the probe's evidence directory")
            if not __import__("re").fullmatch(r"\d{8}T\d{6}Z-[a-z0-9-]+\.json", os.path.basename(output_path)):
                raise RuntimeError("--output filename must use canonical timestamp-slug.json form")
            fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
        else:
            sys.stdout.write(encoded)
        return 0
    except Exception as error:
        sys.stderr.write(f"probe failed closed: {type(error).__name__}: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
