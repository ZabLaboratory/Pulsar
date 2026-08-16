#!/usr/bin/env bash
# Pulsar — npm tarball content audit.
#
# Runs `npm pack --dry-run --json` on every workspace under packages/
# and fails if the resulting tarball would contain :
#
#   - C / C++ source files (`.cpp` / `.cc` / `.c` / `.h` / `.hpp`)
#   - node-gyp manifests (`bindings.gyp` / `binding.gyp` / `*.gyp` / `*.gypi`)
#   - native module entry points (`binding.cc`, `node-addon-api` imports)
#
# Why this is critical : these tarballs are what consumer apps install
# directly from npm. A package that ships GPL source (or a native
# binding compiled from GPL source) drags GPL into the consumer's
# build product, contaminating its license. See LICENSE-INVARIANTS.md
# (#3, #4).
#
# Designed to run from license-isolation.yml + publish-npm.yml.

set -euo pipefail

PKG_DIRS=(
  packages/pulsar-client
  packages/pgm-correlator
  packages/pulsar-bundle
  packages/pulsar-bundle-full
)

# Patterns that must not appear in any tarball file path.
FORBIDDEN_PATTERNS=(
  '\.cpp$'
  '\.cc$'
  '\.c$'
  '\.h$'
  '\.hpp$'
  'bindings\.gyp$'
  'binding\.gyp$'
  '\.gyp$'
  '\.gypi$'
  'binding\.cc$'
)

overall_fail=0

for pkg_dir in "${PKG_DIRS[@]}"; do
  if [ ! -d "$pkg_dir" ]; then
    echo "::warning::Skipping $pkg_dir — directory missing."
    continue
  fi
  echo "::group::npm pack audit — $pkg_dir"
  # `npm pack --dry-run --json` emits a JSON array describing what
  # would land in the tarball. Each entry has a `files` array of
  # `{path, size, mode}`. Parse with python3 (always present on the
  # ubuntu-latest runner — safer than awk for JSON).
  files=$(cd "$pkg_dir" && npm pack --dry-run --json 2>/dev/null \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
for entry in data:
    for f in entry.get('files', []):
        print(f['path'])
" || echo "")

  if [ -z "$files" ]; then
    echo "::error::npm pack returned no files for $pkg_dir — inspect package.json/files."
    overall_fail=1
    echo "::endgroup::"
    continue
  fi

  pkg_fail=0
  for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
    matches=$(echo "$files" | grep -E "$pattern" || true)
    if [ -n "$matches" ]; then
      echo "::error::Tarball $pkg_dir would contain files matching '$pattern' :"
      echo "$matches"
      pkg_fail=1
    fi
  done

  if [ $pkg_fail -ne 0 ]; then
    overall_fail=1
  else
    file_count=$(echo "$files" | wc -l)
    echo "OK — $file_count file(s) in tarball, no forbidden patterns."
  fi
  echo "::endgroup::"
done

if [ $overall_fail -ne 0 ]; then
  echo ""
  echo "::error::npm tarball audit failed. See LICENSE-INVARIANTS.md (#3, #4)."
  exit 1
fi

echo ""
echo "::notice::npm tarball audit passed for all $((${#PKG_DIRS[@]})) workspaces."
