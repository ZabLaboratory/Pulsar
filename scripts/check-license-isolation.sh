#!/usr/bin/env bash
# Pulsar — license isolation source-tree audit.
#
# Static checks that protect consumer applications from accidentally
# crossing the GPL boundary defined in LICENSE-INVARIANTS.md.
#
# Designed to run from CI (license-isolation.yml + release.yml +
# publish-npm.yml) AND from a developer workstation as a pre-commit
# spot-check. Exit code 0 means every invariant we can statically
# verify is intact ; non-zero means STOP and read LICENSE-INVARIANTS.md.
#
# Scope :
#   plugins/   Pulsar's own libobs plugins (C / C++)
#   packages/  npm tarballs that consumers install (TS + binary blob)
#   scripts/   build / packaging / probe scripts (bash / pwsh / python)
#
# Out of scope (intentionally) :
#   upstream/  vendored OBS submodule. Already GPL — checking inside
#              would only flag legitimate libobs internals.
#   docs / *.md / READMEs — these reference "electron" / "prism" by
#              name as documentation. Filtering them adds whitelist
#              churn for zero security signal.

set -euo pipefail

# Limit grep to source files. .md / .txt / .json / .yml are
# documentation or config and may legitimately mention the patterns.
SOURCE_GLOBS=(
  --include='*.cpp' --include='*.cc' --include='*.c'
  --include='*.h'   --include='*.hpp'
  --include='*.ts'  --include='*.tsx' --include='*.js' --include='*.mjs' --include='*.cjs'
)

SOURCE_DIRS=(plugins packages scripts)

# 1) DLL export macros — would expose libobs-derivative symbols to
# consumer FFI. See LICENSE-INVARIANTS.md (#3).
echo "::group::DLL export macros (#3)"
if grep -rn -E '__declspec\(dllexport\)|EXPORT_SYMBOL\b' \
    "${SOURCE_GLOBS[@]}" \
    "${SOURCE_DIRS[@]}" 2>/dev/null; then
  echo "::error::Forbidden DLL export macro detected. See LICENSE-INVARIANTS.md (#3)."
  exit 1
fi
echo "OK"
echo "::endgroup::"

# 2) Node N-API bindings — Pulsar must not expose itself as a Node
# native module. Consumers wrap the WS protocol from JS instead.
# See LICENSE-INVARIANTS.md (#3).
echo "::group::Node N-API bindings (#3)"
if grep -rn -E '\bnapi_[a-z_]+|<node_api\.h>|NAPI_MODULE|<napi\.h>|node-addon-api' \
    "${SOURCE_GLOBS[@]}" \
    "${SOURCE_DIRS[@]}" 2>/dev/null; then
  echo "::error::Node N-API reference found. See LICENSE-INVARIANTS.md (#3)."
  exit 1
fi
echo "OK"
echo "::endgroup::"

# 3) node-gyp manifests — only legit reason to ship one is to compile
# a Node native module, which invariant #3 forbids.
echo "::group::node-gyp manifests (#3)"
fail=0
for path in bindings.gyp binding.gyp; do
  if [ -f "$path" ]; then
    echo "::error::node-gyp manifest at root : $path"
    fail=1
  fi
done
hits=$(find "${SOURCE_DIRS[@]}" -maxdepth 6 -type f \
    \( -name 'bindings.gyp' -o -name 'binding.gyp' \
       -o -name '*.gyp' -o -name '*.gypi' \) \
    2>/dev/null || true)
if [ -n "$hits" ]; then
  echo "::error::node-gyp manifests found in source tree :"
  echo "$hits"
  fail=1
fi
if [ $fail -ne 0 ]; then
  echo "See LICENSE-INVARIANTS.md (#3)."
  exit 1
fi
echo "OK"
echo "::endgroup::"

# 4) Consumer-source-staging directories — top-level dirs named after
# consumer apps would mean Pulsar source is being staged for them.
# That is the literal opposite of the boundary. See LICENSE-INVARIANTS.md (#1, #4).
echo "::group::Consumer-source-staging dirs (#1, #4)"
fail=0
for dir in prism consumer embed embedding; do
  if [ -d "$dir" ]; then
    echo "::error::Suspicious top-level directory '$dir' present."
    fail=1
  fi
done
if [ $fail -ne 0 ]; then
  echo "See LICENSE-INVARIANTS.md (#1, #4)."
  exit 1
fi
echo "OK"
echo "::endgroup::"

echo ""
echo "::notice::License isolation source-tree audit passed."
