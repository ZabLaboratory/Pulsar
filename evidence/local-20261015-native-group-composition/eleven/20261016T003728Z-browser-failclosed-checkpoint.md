# Browser pre-shutdown fail-closed checkpoint

Work unit: `local-20261015-native-group-composition`

Worktree: `D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition`

Branch: `eleven/local-20261015-native-group-composition`

Base before this extension: `198bf3e2a886017e82bf5bcddeb3a09117a502bc`

## Change

The two `browser_pre_shutdown_ready()` failure branches in
`plugins/pulsar-headless/main.cpp` now call a shared noreturn process-exit helper.
It preserves the WebSocket fail-closed log marker, emits a distinct browser
marker, flushes diagnostics, uses `TerminateProcess(GetCurrentProcess(), 1)` on
Windows with `_Exit(1)` fallback, and retains runtime leases and the inherited
shutdown handle until the OS ends the process. Neither failed browser-fence path
returns through C++/DLL destructors or calls frontend/libobs teardown. The
normal path and `frontend_source_cleanup_failed` branches are unchanged.

The probe gained `--expect-browser-fence-failure` and `--host-executable`. The
former deliberately omits the test-only browser proc; the latter copies the
exact just-built `pulsar.exe` into the disposable runtime and records its hash.
The source-level test requires both browser failure sites to use the fatal path
without releasing leases, closing the event, returning, or calling teardown.

This correction follows the root's explicit signed authorization
`f48f96c1` (`20261016T003000Z-browser-fence-scope-and-abi-review.json` in the
Prism project-completion evidence). Pre-edit scope is recorded in
`20261016T002856Z-browser-failclosed-extension.md`.

## Build and test evidence

The private `pulsar-headless.vcxproj` rebuild passed in
`RelWithDebInfo|x64` with zero warnings and errors. The compiled `main.cpp` copy
matched the worktree source. Link output and tlogs identify the private `obs.lib`,
`obs-frontend-api.lib`, and `pulsar-frontend-stub.lib`; the private upstream
checkout was signed commit `70a58302435308e609e0b1114efe3ef402f810b7`.
The output `pulsar.exe` SHA-256 is
`69ab22a5829bee254d53fa9edc463cfbdb33886a23781ae67fdc40fb1b36e973`.
Detailed paths, lengths, hashes, tlogs, and run linkage are in
`20261016T003624Z-browser-failclosed-provenance.json`; the raw build output is
`20261016T003208Z-browser-fence-host-build.raw.txt`.

The focused suite passed **22 tests**, with one unrelated upstream-header test
deselected because the exact pinned canonical submodule is deinitialized in this
worktree. `python -m py_compile` for the probe/test and `git diff --check` passed.
Raw pytest output is `20261016T003604Z-browser-failclosed-tests.raw.txt`.

## Runtime proof

The missing-proc run `20261016T003448Z-browser-fence-failure.json` passed with
intentional exit code 1, no `0xC0000005`, no forced termination, no explicit
runtime-lease release, no frontend/libobs teardown marker, and scratch cleanup.
Its runtime contains no browser modules and the no-browser procedure was not
registered. This proves only the host's fail-closed response when the browser
proc is absent; `cefShutdownExercised=false`.

The normal no-composition control `20261016T003456Z-group-composition-shutdown-control.json`
passed with exit 0, no forced termination, no shutdown errors, and scratch
cleanup. The full composition rerun `20261016T003503Z-group-composition-probe.json`
also passed with exit 0, active-composition cleanup, and the pending-removal
proof (`pending_before=1`, `pending_released=1`). All 33 persisted PNG hashes
were independently recomputed and matched the report. Its measured overlap
pixel is `[0,127,0,127]` (expected group-opacity alpha 127–128), the translated
local-opacity pixel is `[0,0,127,127]`, and the nested transform readback retains
the known one-level blue-channel quantization difference: actual `[0,64,127,127]`
versus expected `[0,64,128,127]`. Both normal runs use the test-only no-browser
fence and do not exercise CEF.

The earlier `20261016T003259Z-browser-fence-failure.json` remains preserved as a
probe-invocation failure: the initial runtime argument pointed to a build-cache
layout whose source checkout could not be derived, so the runner exited before
spawning a child. It is not a product result. The later runs explicitly supplied
the private source `probe-runtime` plus the newly built host executable.

## Remaining limits

This is not installed-runtime admission, and it does not establish graceful CEF
shutdown. No physical capture, public output, transition, or scene switch was
used. Native group masks, non-normal group blend modes, and affine shear remain
explicitly unsupported and fail closed before graph swap.
