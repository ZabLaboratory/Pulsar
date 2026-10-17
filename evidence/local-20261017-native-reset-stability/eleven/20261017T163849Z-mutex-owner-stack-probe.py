#!/usr/bin/env python3
"""Correlate a live w32-pthreads mutex owner with one frozen minidump's stacks.

The dump must be written while the host is still alive. For each explicitly
selected waiter, this checks the known WaitForSingleObjectEx ->
pthread_mutex_lock stack witness, obtains the saved internal mutex pointer from
that waiter's stack, and verifies that pointer again with one live 8-byte RPM
read. It then reads only the PDB-typed mutex/thread fields needed for an owner
TID. Each distinct mutex read is capped at 72 bytes.

`--derive-only` exercises only the local minidump parsing and performs no
process access. `ownerThread` is never interpreted for normal (kind 0)
mutexes: the pinned source initializes it to null and the normal lock/unlock
paths do not publish or clear it.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from ctypes import wintypes

from minidump.minidumpfile import MinidumpFile

PROCESS_VM_READ = 0x0010
PROCESS_DUP_HANDLE = 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DUPLICATE_SAME_ACCESS = 0x00000002
TH32CS_SNAPTHREAD = 0x00000004
LIST_MODULES_ALL = 0x00000003
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_USER_ADDRESS = 0x00007FFFFFFFFFFF
MAX_DISCOVERED_WAITERS = 8
MAX_DISTINCT_MUTEXES = 2

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / ".cache" / "native-reset-stability"
RUNTIME_BIN = CACHE / "private-runtime-exact-20261017T155459Z" / "runtime" / "bin" / "64bit"
PLUGIN_BIN = CACHE / "plugin-build" / "RelWithDebInfo"
W32_DLL = RUNTIME_BIN / "w32-pthreads.dll"
W32_PDB = RUNTIME_BIN / "w32-pthreads.pdb"
PLUGIN_DLL = PLUGIN_BIN / "pulsar-capture-effects.dll"

EXPECTED = {
    "w32-pthreads.dll": "296dd07553f0f4293354281ce0540fe2dfe8d9a4d8a197994343e718694267c9",
    "pulsar-capture-effects.dll": "efb62a4996a7962fa65b7a28d5cc444aba07d6c21aea67a9e476a89778ae2dfa",
}
LAYOUT = {
    "pdbGuid": "0c146b86-591e-6192-622d-0e72837eebde",
    "pdbAge": 1,
    "pdbSha256": "85100dc57849c760761a64d68eaa27ad3075b513d0069617b64d520b8da9f682",
    "pthreadMutexSize": 48,
    "pthreadMutexFields": {
        "lock_idx": {"offset": 0, "type": "signed 32-bit"},
        "recursive_count": {"offset": 4, "type": "signed 32-bit"},
        "kind": {"offset": 8, "type": "signed 32-bit"},
        "ownerThread": {"offset": 16, "type": "ptw32_handle_t[16]"},
        "event": {"offset": 32, "type": "HANDLE[8]"},
        "robustNode": {"offset": 40, "type": "pointer[8]"},
    },
    "ptw32Handle": {"size": 16, "pOffset": 0, "xOffset": 8},
    "ptw32Thread": {
        "size": 432,
        "seqNumberOffset": 0,
        "threadHOffset": 8,
        "ptHandleOffset": 16,
        "stateOffset": 40,
    },
    "mutexKind": {
        "normal": 0,
        "recursive": 1,
        "errorcheck": 2,
        "robustKinds": "negative encodings",
    },
    "ownerThreadTracking": {
        "supportedKinds": "positive non-normal kinds only",
        "kindZero": "not tracked; initialized null and omitted from normal lock/unlock path",
        "negativeRobust": "unresolved by this collector",
    },
}
STACK_WITNESS = {
    "waitReturnFromRsp": 0xA0,
    "savedRbxSlotFromRsp": 0xA8,
    "pthreadMutexLockWaitReturnRva": 0x383F,
    "meaning": "KERNELBASE WaitForSingleObjectEx wait return; next qword is saved RBX (internal mutex pointer)",
}
SOURCE_PROVENANCE = {
    "checkout": "upstream-source-25ed780b7f392a1690dfca929cd57679959cf3ad-windows-checkout",
    "checkoutHead": "70a58302435308e609e0b1114efe3ef402f810b7",
    "files": {
        "deps/w32-pthreads/pthread.h": "591f146fb0d5ec6650ccf7c5bd74a8fd9726e8df5620bf2b0862a452c7a05834",
        "deps/w32-pthreads/pthread_mutex_init.c": "3078aa4c129a9fda1afb0e35d34c26e8e4900b72663fd9053e4fd0c7fb314427",
        "deps/w32-pthreads/pthread_mutex_lock.c": "80be844a36c5efd00694c3b68e630dbb5141b459d2319b9425afc94ce07caf87",
        "deps/w32-pthreads/pthread_mutex_unlock.c": "b1af1517c6fb732c598091bb54f4559488255e95c76045c26a561d1167a51312",
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


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_one_module(modules, name: str):
    matches = [module for module in modules if os.path.basename(module.name).casefold() == name.casefold()]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {name} module in minidump, found {len(matches)}")
    return matches[0]


def derive_waiter(dump: MinidumpFile, reader, tid: int) -> dict:
    threads = [thread for thread in dump.threads.threads if int(thread.ThreadId) == tid]
    if len(threads) != 1:
        raise RuntimeError(f"expected exactly one TID {tid} in minidump, found {len(threads)}")
    thread = threads[0]
    context = thread.ContextObject
    modules = list(dump.modules.modules)
    w32 = find_one_module(modules, "w32-pthreads.dll")
    ntdll = find_one_module(modules, "ntdll.dll")
    wait_return_slot = int(context.Rsp) + STACK_WITNESS["waitReturnFromRsp"]
    saved_rbx_slot = int(context.Rsp) + STACK_WITNESS["savedRbxSlotFromRsp"]
    wait_return = struct.unpack("<Q", reader.read(wait_return_slot, 8))[0]
    if not (int(ntdll.baseaddress) <= int(context.Rip) < int(ntdll.endaddress)):
        raise RuntimeError(f"waiter TID {tid} context RIP is outside ntdll.dll: {int(context.Rip):#x}")
    expected_return = int(w32.baseaddress) + STACK_WITNESS["pthreadMutexLockWaitReturnRva"]
    if wait_return != expected_return:
        raise RuntimeError(
            f"waiter TID {tid} stack does not match pinned pthread wait site: "
            f"return={wait_return:#x}, expected={expected_return:#x}"
        )
    internal_mutex = struct.unpack("<Q", reader.read(saved_rbx_slot, 8))[0]
    if internal_mutex <= 0 or internal_mutex > MAX_USER_ADDRESS or internal_mutex % 8:
        raise RuntimeError(f"waiter TID {tid} saved RBX is not a canonical aligned mutex pointer: {internal_mutex:#x}")
    return {
        "tid": tid,
        "contextRip": hex(int(context.Rip)),
        "contextRsp": hex(int(context.Rsp)),
        "waitReturnSlot": hex(wait_return_slot),
        "waitReturnAddress": hex(wait_return),
        "savedRbxSlot": hex(saved_rbx_slot),
        "internalMutexAddress": hex(internal_mutex),
    }


def dump_contexts(dump: MinidumpFile) -> dict[int, dict]:
    result = {}
    for thread in dump.threads.threads:
        context = thread.ContextObject
        result[int(thread.ThreadId)] = {"rip": hex(int(context.Rip)), "rsp": hex(int(context.Rsp))}
    return result


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
    for module in modules[: needed.value // ctypes.sizeof(HMODULE)]:
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
        raise RuntimeError(
            f"ReadProcessMemory({label}, {address:#x}, {length}) returned {received.value} bytes; "
            f"Win32 error {ctypes.get_last_error()}"
        )
    return buffer.raw[:length]


def resolve_owner_tid(process: HANDLE, remote_thread_handle: int, pid: int) -> dict:
    if not remote_thread_handle or remote_thread_handle > MAX_USER_ADDRESS:
        return {"resolved": False, "reason": "invalid_or_pseudo_thread_handle"}
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


def capture_one_mutex(process: HANDLE, pid: int, waiters: list[dict], contexts: dict[int, dict], live_tids: set[int]) -> dict:
    internal_mutex = int(waiters[0]["internalMutexAddress"], 16)
    if any(int(waiter["internalMutexAddress"], 16) != internal_mutex for waiter in waiters):
        raise RuntimeError("internal error: mutex reads must group identical addresses")
    read_bytes = [0]
    output = {
        "internalAddress": hex(internal_mutex),
        "waiterTids": [int(waiter["tid"]) for waiter in waiters],
        "stackWitnesses": waiters,
        "readBytes": None,
        "readByteLimit": 72,
    }
    try:
        representative = waiters[0]
        saved_rbx_slot = int(representative["savedRbxSlot"], 16)
        live_stack_pointer = struct.unpack(
            "<Q", read_exact(process, saved_rbx_slot, 8, "live waiter saved RBX stack slot", read_bytes)
        )[0]
        output["liveStackPointer"] = hex(live_stack_pointer)
        if live_stack_pointer != internal_mutex:
            raise RuntimeError(
                f"waiter stack changed after dump: dump={internal_mutex:#x}, live={live_stack_pointer:#x}"
            )

        metadata = read_exact(process, internal_mutex, 12, "pthread_mutex_t_ metadata", read_bytes)
        lock_idx, recursive_count, kind = struct.unpack("<iii", metadata)
        output["metadata"] = {"lock_idx": lock_idx, "recursive_count": recursive_count, "kind": kind}
        if kind == 0:
            output["ownerResolution"] = {
                "resolved": False,
                "reason": "normal_mutex_kind_zero_does_not_track_ownerThread",
            }
            output["readBytes"] = read_bytes[0]
            return output
        if kind < 0:
            output["ownerResolution"] = {
                "resolved": False,
                "reason": "robust_negative_kind_not_evaluated_by_this_probe",
            }
            output["readBytes"] = read_bytes[0]
            return output
        if lock_idx != -1 or recursive_count < 1:
            output["ownerResolution"] = {
                "resolved": False,
                "reason": "mutex_metadata_does_not_show_a_held_positive_kind_mutex",
            }
            output["readBytes"] = read_bytes[0]
            return output

        owner_bytes = read_exact(process, internal_mutex + 16, 16, "pthread_mutex_t_.ownerThread", read_bytes)
        owner_p, owner_x = struct.unpack_from("<QI", owner_bytes, 0)
        output["ownerThread"] = {"p": hex(owner_p) if owner_p else None, "x": owner_x}
        if not owner_p:
            output["ownerResolution"] = {"resolved": False, "reason": "ownerThread_is_null_on_held_positive_kind_mutex"}
            output["readBytes"] = read_bytes[0]
            return output
        if owner_p > MAX_USER_ADDRESS or owner_p % 8:
            output["ownerResolution"] = {"resolved": False, "reason": "ownerThread.p_is_not_a_valid_user_pointer"}
            output["readBytes"] = read_bytes[0]
            return output

        thread_prefix = read_exact(process, owner_p, 32, "ptw32_thread_t_ prefix", read_bytes)
        seq_number, thread_handle, self_p, self_x = struct.unpack_from("<QQQI", thread_prefix, 0)
        thread_state = struct.unpack("<I", read_exact(process, owner_p + 40, 4, "ptw32_thread_t_.state", read_bytes))[0]
        self_handle_matches = self_p == owner_p and self_x == owner_x
        tid_result = resolve_owner_tid(process, thread_handle, pid) if self_handle_matches else {
            "resolved": False,
            "reason": "embedded_ptHandle_does_not_match_mutex_ownerThread",
        }
        resolved_tid = tid_result.get("tid")
        owner_dump_context = contexts.get(resolved_tid) if resolved_tid else None
        output["ownerResolution"] = {
            "resolved": bool(tid_result.get("resolved") and resolved_tid in live_tids),
            "ownerThreadStructAddress": hex(owner_p),
            "seqNumber": seq_number,
            "threadH": hex(thread_handle) if thread_handle else None,
            "ptHandle": {"p": hex(self_p) if self_p else None, "x": self_x},
            "ptHandleMatchesMutexOwnerThread": self_handle_matches,
            "state": thread_state,
            "threadHandleResolution": tid_result,
            "tidPresentInLiveTargetSnapshot": resolved_tid in live_tids if resolved_tid else False,
            "ownerStackCorrelation": {
                "tid": resolved_tid,
                "presentInSameMinidump": owner_dump_context is not None,
                "context": owner_dump_context,
                "nextStep": "symbolize this TID from the same minidump before asserting a lock cycle",
            },
        }
        output["readBytes"] = read_bytes[0]
        if read_bytes[0] > 72:
            raise AssertionError(f"per-mutex live memory-read budget exceeded: {read_bytes[0]}")
        return output
    except Exception as error:
        output["readBytes"] = read_bytes[0]
        output["ownerResolution"] = {"resolved": False, "reason": "live_read_or_correlation_failed", "error": str(error)}
        return output


def discover_waiters(dump: MinidumpFile, reader) -> dict:
    """Find current pthread waiters from the dump, never from historical TIDs."""
    witnesses = []
    rejected = []
    for thread in dump.threads.threads:
        tid = int(thread.ThreadId)
        try:
            witnesses.append(derive_waiter(dump, reader, tid))
        except Exception as error:
            if len(rejected) < 8:
                rejected.append({"tid": tid, "reason": f"{type(error).__name__}: {error}"})
    unique_mutexes = {int(witness["internalMutexAddress"], 16) for witness in witnesses}
    return {
        "candidateCount": len(witnesses),
        "candidateTids": [int(witness["tid"]) for witness in witnesses],
        "distinctMutexCount": len(unique_mutexes),
        "candidateLimit": MAX_DISCOVERED_WAITERS,
        "distinctMutexLimit": MAX_DISTINCT_MUTEXES,
        "rejectedThreadSample": rejected,
        "rejectedThreadSampleLimit": 8,
        "witnesses": witnesses,
    }


def capture(pid: int, minidump_path: Path, waiter_tids: list[int]) -> dict:
    dump = MinidumpFile.parse(str(minidump_path))
    dump_reader = dump.get_reader()
    dump_modules = list(dump.modules.modules)
    dump_w32 = find_one_module(dump_modules, "w32-pthreads.dll")
    dump_context_map = dump_contexts(dump)
    discovery = discover_waiters(dump, dump_reader) if not waiter_tids else None
    if discovery is not None:
        if not (1 <= discovery["candidateCount"] <= MAX_DISCOVERED_WAITERS and
                1 <= discovery["distinctMutexCount"] <= MAX_DISTINCT_MUTEXES):
            return {
                "schema": "pulsar.w32-pthreads-owner-read/2",
                "capturedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "pid": pid,
                "minidump": {"path": str(minidump_path.resolve()), "sha256": sha256_file(minidump_path)},
                "waiterDiscovery": {"status": "no-witness" if not discovery["candidateCount"] else "ambiguous", **discovery},
                "mutexes": [],
                "readBudget": {"maxPerDistinctMutexBytes": 72, "distinctMutexCount": 0, "aggregateBytes": 0, "aggregateLimitForTwoMutexes": 144},
                "processMemoryRead": "not-attempted",
            }
        waiter_tids = list(discovery["candidateTids"])
    waiter_witnesses = [derive_waiter(dump, dump_reader, tid) for tid in waiter_tids]
    dump_w32_path = W32_DLL
    if not dump_w32_path.is_file() or sha256_file(dump_w32_path) != EXPECTED["w32-pthreads.dll"]:
        raise RuntimeError("pinned local w32-pthreads.dll is missing or has a different SHA-256")
    import pefile

    pe = pefile.PE(str(dump_w32_path), fast_load=True)
    if int(pe.FILE_HEADER.TimeDateStamp) != int(dump_w32.timestamp) or int(pe.OPTIONAL_HEADER.CheckSum) != int(dump_w32.checksum):
        raise RuntimeError("minidump w32-pthreads module timestamp/checksum does not match pinned DLL")

    rights = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ | PROCESS_DUP_HANDLE
    process = kernel32.OpenProcess(rights, False, pid)
    if not process:
        raise winerror("OpenProcess")
    try:
        image = process_image(process)
        paths = process_modules(process)
        identities: dict[str, dict] = {}
        for wanted, expected_hash in EXPECTED.items():
            matches = [path for path in paths if os.path.basename(path).casefold() == wanted.casefold()]
            if len(matches) != 1:
                raise RuntimeError(f"expected exactly one loaded {wanted}, found {len(matches)}")
            path = matches[0]
            actual_hash = sha256_file(path)
            identities[wanted] = {"path": path, "sha256": actual_hash, "matchesPinnedBuild": actual_hash == expected_hash}
            if actual_hash != expected_hash:
                raise RuntimeError(f"{wanted} hash mismatch: expected {expected_hash}, got {actual_hash}")
        if not W32_PDB.is_file() or sha256_file(W32_PDB) != LAYOUT["pdbSha256"]:
            raise RuntimeError("pinned exact w32-pthreads.pdb is missing or has a different SHA-256")
        identities["w32-pthreads.pdb"] = {"path": str(W32_PDB), "sha256": sha256_file(W32_PDB), "matchesPinnedBuild": True}

        live_tid_set = target_thread_ids(pid)
        grouped: dict[int, list[dict]] = {}
        for witness in waiter_witnesses:
            if witness["tid"] not in live_tid_set:
                raise RuntimeError(f"waiter TID {witness['tid']} is not present in the live target process")
            grouped.setdefault(int(witness["internalMutexAddress"], 16), []).append(witness)
        mutexes = [capture_one_mutex(process, pid, witnesses, dump_context_map, live_tid_set) for witnesses in grouped.values()]
        total = sum(int(mutex["readBytes"] or 0) for mutex in mutexes)
        if any(int(mutex["readBytes"] or 0) > 72 for mutex in mutexes):
            raise AssertionError("per-mutex live memory-read budget exceeded")
        return {
            "schema": "pulsar.w32-pthreads-owner-read/2",
            "capturedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "pid": pid,
            "processImage": image,
            "minidump": {"path": str(minidump_path.resolve()), "sha256": sha256_file(minidump_path)},
            "moduleIdentities": identities,
            "layoutProvenance": LAYOUT,
            "sourceProvenance": SOURCE_PROVENANCE,
            "stackWitness": STACK_WITNESS,
            "waiterStackWitnesses": waiter_witnesses,
            "waiterDiscovery": ({"status": "unique-witnesses", **discovery}
                                if discovery is not None else {
                                    "status": "explicit-tids",
                                    "candidateTids": list(waiter_tids),
                                }),
            "mutexes": mutexes,
            "readBudget": {
                "maxPerDistinctMutexBytes": 72,
                "distinctMutexCount": len(mutexes),
                "aggregateBytes": total,
                "aggregateLimitForTwoMutexes": 144,
            },
        }
    finally:
        kernel32.CloseHandle(process)


def derive_only(minidump_path: Path, waiter_tids: list[int], auto_discover: bool = False) -> dict:
    dump = MinidumpFile.parse(str(minidump_path))
    reader = dump.get_reader()
    discovery = discover_waiters(dump, reader) if auto_discover else None
    tids = discovery["candidateTids"] if discovery is not None else waiter_tids
    witnesses = [derive_waiter(dump, reader, tid) for tid in tids]
    return {
        "mode": "derive-only-no-process-access",
        "minidump": {"path": str(minidump_path.resolve()), "sha256": sha256_file(minidump_path)},
        "sourceProvenance": SOURCE_PROVENANCE,
        "stackWitness": STACK_WITNESS,
        "waiterStackWitnesses": witnesses,
        "waiterDiscovery": ({"status": "unique-witnesses"
                             if (1 <= len(tids) <= MAX_DISCOVERED_WAITERS and
                                 1 <= discovery["distinctMutexCount"] <= MAX_DISTINCT_MUTEXES)
                             else "ambiguous", **discovery}
                            if discovery is not None else {"status": "explicit-tids"}),
    }


def write_or_print(result: dict, output: str | None) -> None:
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if not output:
        sys.stdout.write(encoded)
        return
    evidence_dir = os.path.realpath(os.path.dirname(__file__))
    output_path = os.path.realpath(output)
    if os.path.dirname(output_path) != evidence_dir:
        raise RuntimeError("--output must be in this script's evidence directory")
    if not re.fullmatch(r"\d{8}T\d{6}Z-[a-z0-9-]+\.json", os.path.basename(output_path)):
        raise RuntimeError("--output filename must use canonical timestamp-slug.json form")
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minidump", required=True, type=Path, help="same frozen host dump containing waiter and owner stacks")
    parser.add_argument("--waiter-tid", action="append", type=int, default=[], help="blocked pthread waiter TID; may be supplied twice for a lock cycle")
    parser.add_argument("--discover-waiters", action="store_true", help="derive current waiter TIDs from exact stack witnesses in the minidump")
    parser.add_argument("--pid", type=int, help="PID of the still-live exact private host; required unless --derive-only")
    parser.add_argument("--derive-only", action="store_true", help="derive addresses from dump stacks only; do not open any process")
    parser.add_argument("--output", help="optional new JSON path in this script's evidence directory")
    args = parser.parse_args()

    if len(args.waiter_tid) > 2 or len(set(args.waiter_tid)) != len(args.waiter_tid):
        parser.error("supply one or two distinct --waiter-tid values")
    if args.discover_waiters and args.waiter_tid:
        parser.error("--discover-waiters cannot be combined with --waiter-tid")
    if not args.discover_waiters and not args.waiter_tid:
        parser.error("supply --waiter-tid or --discover-waiters")
    if not args.derive_only and not args.pid:
        parser.error("--pid is required for live collection")
    if args.derive_only and args.pid:
        parser.error("--pid is disallowed with --derive-only")
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        parser.error("this probe requires 64-bit Windows")
    try:
        result = derive_only(args.minidump, args.waiter_tid, args.discover_waiters) if args.derive_only else capture(args.pid, args.minidump, args.waiter_tid)
        write_or_print(result, args.output)
        return 0
    except Exception as error:
        sys.stderr.write(f"probe failed closed: {type(error).__name__}: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
