#!/usr/bin/env bash
# Pulsar — tests/** assert() lint guard.
#
# #220: assert() is a no-op under NDEBUG (the RelWithDebInfo CI build
# defines it), so 5 probe suites silently stopped verifying anything they
# claimed to. #221 fixed the 5 existing suites with PULSAR_CHECK (always
# evaluates, regardless of build type). This script is the guard-fou from
# #222 (finding H1, Bastion clearance on #221): it fails CI if a *new*
# assert(), <cassert> or <assert.h> is (re)introduced under tests/**,
# which would silently reproduce #220 in whichever probe uses it.
#
# Deliberately a simple grep, not a parser: lines that are pure `//`
# comments (e.g. this script's own reference lines above, or the
# NDEBUG-explainer comment left in each fixed probe) are excluded so the
# guard does not flag its own provenance trail. PULSAR_CHECK never matches
# — it does not contain the substring "assert(".

set -euo pipefail

TESTS_DIR="tests"
if [ ! -d "$TESTS_DIR" ]; then
  echo "::notice::no tests/ directory, skipping"
  exit 0
fi

SOURCE_GLOBS=(
  --include='*.cpp' --include='*.cc' --include='*.c'
  --include='*.h'   --include='*.hpp' --include='*.hh'
)

echo "::group::assert()/cassert guard under tests/** (#220, #222)"

# Match assert(...) calls and <cassert>/<assert.h> includes, but skip
# lines whose content is a pure // comment.
matches=$(grep -rn -E '\bassert[[:space:]]*\(|<cassert>|<assert\.h>' \
    "${SOURCE_GLOBS[@]}" "$TESTS_DIR" 2>/dev/null \
  | grep -v -E ':[0-9]+:[[:space:]]*//' \
  || true)

if [ -n "$matches" ]; then
  echo "$matches"
  echo "::error::assert()/<cassert>/<assert.h> found under tests/**. NDEBUG (RelWithDebInfo CI build) silently no-ops assert() — this reproduces #220. Use PULSAR_CHECK instead (see #221)."
  exit 1
fi

echo "OK"
echo "::endgroup::"
