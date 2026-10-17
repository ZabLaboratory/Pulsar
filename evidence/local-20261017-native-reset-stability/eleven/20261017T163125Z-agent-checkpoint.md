# Checkpoint: bounded mutex-owner probe prepared

Date: 2026-10-17 16:31 UTC
Repository: D:\Documents\Zab\Pulsar
Worktree: D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-native-reset-stability
Branch: eleven/local-20261017-native-reset-stability
HEAD before this evidence-only checkpoint: 94b4396982c4729f11cae3c08ed232d4b20692e7

No product source changed. No build, test, host, GPU, or live-process collection was run in this pass.

## Exact PDB layout now available

The dump from the second gen-2 freeze remains 20261017T155459Z-gen2-freeze.dmp, SHA-256 9f3ecf3c1d7abc1d2226993dcc2f19cbb94ca7a62db94d4bebd58b8852c9e298. The exact matched w32-pthreads.dll / PDB identities and prior stack evidence are recorded in 20261017T162111Z-agent-report.md. Reproducible DbgHelp readers are 20261017T163125Z-w32-layout-fields.py (SHA-256 89f90d5823e5fe5bb7b3a5b5b64da7b60a5509f00e45f7b46c77d1daf087e393) and 20261017T163125Z-w32-type-chain.py (SHA-256 c8249a35e61ec6fabd3fa4cdf7624e4fc03dc8f9e94bef1474ff6cc668a6e191); both parse the saved dump and load the adjacent exact w32 PDB.

Using DbgHelp SymEnumTypesW and SymGetTypeInfo against that exact PDB:

- pthread_mutex_t (type 173) → pointer (174) → pthread_mutex_t_ (494), 48 bytes.
- pthread_mutex_t_: lock_idx +0, CodeView btLong / signed 32-bit; recursive_count +4 and kind +8, signed 32-bit; ownerThread +16 as ptw32_handle_t (16 bytes); event +32; robustNode +40.
- ptw32_handle_t (293), 16 bytes: p +0 is an 8-byte void *; x +8 is a 4-byte unsigned integer; the last 4 bytes are padding.
- ptw32_thread_t_ (198), 432 bytes: seqNumber +0 (8 bytes), threadH +8 (8-byte handle represented in PDB as void *), ptHandle +16 (ptw32_handle_t), and state +40 (PThreadState, 4 bytes).

PDB gives ptw32_handle_t.p the generic type void *; it does not by itself declare that p points to ptw32_thread_t_. The prepared probe therefore treats ownerThread.p only as a candidate. It resolves a TID only after the candidate's embedded ptHandle.p/x equals the mutex's ownerThread.p/x, threadH duplicates as a Windows thread handle in the target PID, GetThreadId returns a TID present in a fresh Toolhelp thread snapshot, and the pinned DLL/PDB hashes match. A mismatch remains unresolved rather than being inferred as a mutex owner.

## Prepared collector

20261017T163125Z-mutex-owner-read-probe.py has SHA-256 8fced17be14dd0886f2b231e9960c1ad982fb2abc922254cfef3929a02a62842. It passed a Python AST syntax parse only; the probe was not executed.

The caller supplies the frozen waiter's live pthread_mutex_t * argument slot address derived from its stack and the exact private host PID. The helper pins w32-pthreads.dll SHA-256 296dd07553f0f4293354281ce0540fe2dfe8d9a4d8a197994343e718694267c9, adjacent PDB SHA-256 85100dc57849c760761a64d68eaa27ad3075b513d0069617b64d520b8da9f682, and plugin SHA-256 efb62a4996a7962fa65b7a28d5cc444aba07d6c21aea67a9e476a89778ae2dfa. It reads only the pointer slot, 12 metadata bytes, 16 ownerThread bytes, 32 thread-prefix bytes, and 4 state bytes: at most 72 bytes through ReadProcessMemory. It requests only query, VM-read, and handle-duplicate rights; it never requests write or thread-creation rights. The event and robust-node fields are not read. Its optional JSON output must use a new canonical filename in this evidence directory.

No owner was recovered from the saved dump: it omits the mutex and thread heap pages. This checkpoint prepares the next discriminating live read; it does not prove the owner, the lock holder's call path, a deadlock cycle, or a product defect. Any source correction remains premature.

## Preservation and next collection

Keep the exact PDB and DLL at .cache/native-reset-stability/private-runtime-exact-20261017T155459Z/runtime/bin/64bit/w32-pthreads.dll and .pdb, and the qualified plugin image .cache/native-reset-stability/plugin-build/RelWithDebInfo/pulsar-capture-effects.dll until a coordinated frozen-host collection is complete. No host or child process was started by this pass.

At the next authorized freeze, derive the current argument-slot address from that waiter's stack, then invoke the helper against the still-live private PID. Do not reuse the prior dump's process addresses. A failed read, changed owner handle, mismatched image hash, or failed TID correlation is recorded as unresolved, not worked around.
