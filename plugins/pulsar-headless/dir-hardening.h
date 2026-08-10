// #201 (ADR-005 §5 R10, Amendment 1): the four named hardening gestures
// for obs-websocket/config.json's directory and temp-file lifecycle.
//
// This header and its .cpp intentionally never include obs.h or Qt --
// same convention as log-handler.h/.cpp -- so tests/dir-hardening-probe
// links the REAL translation unit, not a mirror of it. Only main.cpp
// (which already depends on obs.h/Qt) wires this into
// seed_websocket_config().
#pragma once

#include <string>

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif

namespace pulsar_dir {

#if defined(_WIN32)

// N1 (three of the four named gestures): ensures `dir` exists and
// carries a protected, current-user-only DACL.
//
//  1. Created via CreateDirectoryA with the protected DACL already
//     attached through SECURITY_ATTRIBUTES -- not created with the
//     inherited DACL and hardened afterwards. The prior two-step
//     sequence left a window where an attacker who pre-created `dir`
//     (or a junction in its place) kept OWNERSHIP permanently, which
//     grants WRITE_DAC regardless of any DACL we later impose.
//  2. Opened with FILE_FLAG_OPEN_REPARSE_POINT so a junction/symlink
//     planted in place of `dir` is opened AS the reparse point itself
//     rather than transparently followed; refused if
//     FILE_ATTRIBUTE_REPARSE_POINT is set.
//  3. The effective owner of the (possibly pre-existing) handle is
//     verified against our own token BEFORE the directory is trusted
//     for anything; an attacker's pre-created directory fails this
//     check and the whole call fails closed.
//
// Only once creation-or-verified-ownership succeeds is the protected
// DACL (re-)applied by handle -- self-healing for a directory
// surviving from a prior boot, a no-op for one just created. Fails
// closed throughout: any step that cannot be completed refuses rather
// than return true for a directory not fully verified end to end.
bool create_directory_hardened(const std::string &dir, const std::string &context);

// N1 (fourth named gesture): re-verifies, by handle, that `path`
// (typically config.json right after MoveFileExA published it) is
// still owned by the current token. Defense in depth against a
// substitution the three gestures above did not anticipate.
bool verify_owned_by_current_token(const std::string &path, const std::string &context);

// N2: creates a fresh, randomly-named `<dir>/config.<random>.tmp` with
// CREATE_NEW and the same protected, current-user-only DACL, and
// returns the STILL-OPEN handle via `out_handle` -- the caller writes
// through it directly and closes it once done. No close-then-reopen
// TOCTOU window between creation and the write.
bool create_protected_temp_file(const std::string &dir, const std::string &context, std::string &out_path,
                                 HANDLE &out_handle);

// N3: deletes every `<dir>/config.*.tmp` orphan (best-effort; a file
// that cannot be enumerated or deleted does not fail the call). Call
// once, before allocating a new temp file, so an orphan surviving a
// prior crashed boot (carrying the session password in clear) never
// lingers past the next successful one.
void sweep_orphaned_temp_files(const std::string &dir, const std::string &context);

#endif // _WIN32

} // namespace pulsar_dir
