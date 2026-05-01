# Consumer audit — what your repo must enforce

If your application bundles Pulsar (`pulsar.exe` + the
`@clodocapeo/pulsar-bundle*` packages) **and** you intend to keep your
own license, **you must verify the four invariants of
[`LICENSE-INVARIANTS.md`](LICENSE-INVARIANTS.md) on YOUR side**.

Pulsar's CI enforces the source-side and binary-side invariants for
its own artefacts. It cannot, by construction, verify what YOU do
with those artefacts. That is what this document is about.

This is **not** an essay. Every claim below is backed by a runnable
command. The reference script + workflow at the bottom drop into any
consumer repo and exercise every check empirically. **If you have
not run the checks, you have not passed the audit** — there is no
"in spirit" path.

---

## TL;DR — drop these two files into your consumer repo

| File | What it does |
|---|---|
| [`scripts/pulsar-consumer-audit.sh`](#reference-script-pulsar-consumer-auditsh) | Single bash script. Runs every static check below. Exits 0 = clean, non-zero = at least one invariant broken. |
| [`.github/workflows/pulsar-consumer-audit.yml`](#reference-workflow-pulsar-consumer-audityml) | GitHub Actions workflow. Runs the script on every PR + push to your default branch. Add the binary-imports check on the platforms where you ship. |

Both files below are copy-pasteable and self-contained. Adapt the
language-specific checks to your stack (Node, Python, Rust, …) but
keep the **must-be-empirical** structure : every claim is a command,
every command has a pass/fail signal.

---

## The four invariants (recap)

| # | Invariant | What you must NOT do | What you may do |
|---|---|---|---|
| 1 | Process boundary | Embed Pulsar in-process. Load `obs.dll` / `obs-frontend-api.dll` / Pulsar plugin DLLs into your process. Run Pulsar logic on your threads. | Spawn `pulsar.exe` as a child process. Read its stdout. Send `SIGTERM` for shutdown. |
| 2 | WebSocket-only IPC | Shared memory, mmap, named pipes, COM/DBus/RPC native, file-based handshake-then-read-pixels. | obs-websocket loopback. Period. |
| 3 | No FFI / native bindings | `dlopen` / `LoadLibrary` of any Pulsar artefact. `node-gyp` module wrapping libobs. AOT-compiled stub linking `obs.lib` / `libobs.so`. | Use the official `@clodocapeo/pulsar-client` (MIT, WS-only). |
| 4 | No copy-paste of Pulsar source | Vendor `.cpp` / `.h` / `.ts` / `.py` from Pulsar into your tree. Read Pulsar source while typing in your repo. | Re-implement from public docs. Ask Pulsar to expose a vendor request that returns the data you need. |

Read [`LICENSE-INVARIANTS.md`](LICENSE-INVARIANTS.md) for the full
contract + the table of tempting designs to refuse on sight.

---

## Empirical checks every consumer repo must run

### Check 1 — no Pulsar / libobs C / C++ headers in your source

Pulsar / libobs publish dozens of headers (`obs.h`, `obs-frontend-api.h`,
`obs-source.h`, `media-io/audio-io.h`, …). If any of these appear in
your source, your build is consuming Pulsar's GPL surface — invariant
#4 broken, plus probably #3 once you link against the matching `.lib` /
`.so`.

```bash
# Forbidden include patterns. Add headers used by upstream OBS as your
# consumer's stack might have its own libobs build path otherwise.
grep -rn -E '#\s*include\s*[<"](obs[a-zA-Z0-9_/-]*\.h(pp)?|graphics/[a-zA-Z0-9_-]+\.h|media-io/[a-zA-Z0-9_-]+\.h|util/[a-zA-Z0-9_-]+\.h)[>"]' \
  --include='*.cpp' --include='*.cc' --include='*.c' \
  --include='*.h'   --include='*.hpp' \
  --exclude-dir='node_modules' --exclude-dir='.git' \
  src/ 2>/dev/null
```

**Pass** : no output. **Fail** : any line of output.

### Check 2 — no `bindings.gyp` / native module wrapping Pulsar

A `bindings.gyp` (or its modern equivalents `binding.gyp`, `*.gypi`,
or a `cmake-js` config) declares that Node will compile a native
addon. The only legit reason for that in a consumer repo is exactly
what invariant #3 forbids : an N-API wrapper around libobs.

```bash
# Forbidden manifests at the consumer repo root.
for f in bindings.gyp binding.gyp; do
  if [ -f "$f" ]; then
    echo "FAIL : $f present in consumer repo root."
    exit 1
  fi
done
# Forbidden manifests anywhere in tracked source.
matches=$(find . -path './node_modules' -prune -o -path './.git' -prune \
                 -o -type f \( -name 'bindings.gyp' -o -name 'binding.gyp' \
                            -o -name '*.gyp' -o -name '*.gypi' \) -print 2>/dev/null)
if [ -n "$matches" ]; then
  echo "FAIL : node-gyp manifests found:"
  echo "$matches"
  exit 1
fi
echo "OK"
```

**Pass** : `OK`. **Fail** : any manifest reported.

### Check 3 — no `LoadLibrary` / `dlopen` / `ctypes.CDLL` of Pulsar artefacts

The consumer must never reach into Pulsar's binaries at runtime. The
following patterns are the language-level fingerprints :

| Language | Pattern |
|---|---|
| Node / TS | `require('./.../pulsar.exe')`, `import('.../pulsar')`, `process.dlopen(...)` |
| Python | `ctypes.CDLL`, `cffi.dlopen`, `__import__` of any Pulsar binary |
| Rust | `libloading::Library::new(...)` referencing Pulsar |
| C / C++ | `LoadLibrary("obs.dll" / "pulsar.exe")`, `dlopen("libobs.so")` |
| Go | `syscall.LoadLibrary` / `purego.Dlopen` referencing Pulsar |

```bash
# Catch-all dynamic-load grep. Drop language patterns you don't ship.
grep -rni -E '(LoadLibrary|GetProcAddress|dlopen|CDLL|libloading|process\.dlopen)' \
  --include='*.ts' --include='*.tsx' --include='*.js' --include='*.mjs' --include='*.cjs' \
  --include='*.cpp' --include='*.cc' --include='*.c' \
  --include='*.h'   --include='*.hpp' \
  --include='*.py'  --include='*.rs'  --include='*.go' \
  --exclude-dir='node_modules' --exclude-dir='.git' \
  src/ 2>/dev/null \
| grep -iE '(obs|pulsar)' \
| grep -v '://' \
&& echo "FAIL : dynamic-load reference to obs/pulsar binaries found." && exit 1
echo "OK"
```

**Pass** : `OK`. **Fail** : matched line printed.

### Check 4 — no copy-pasted Pulsar source (fingerprints)

The most insidious failure mode : a developer reads a Pulsar source
file, "rewrites" it in the consumer with the file open, and ships
near-identical code. That is derivative work, full stop.

The pragmatic empirical check : every Pulsar plugin source file
carries a Pulsar-specific identifier (function names, log prefixes,
module IDs). Grep for those fingerprints in the consumer tree. Any
hit means either copy-paste happened, or someone wrote a comment
referencing Pulsar internals (which is also a smell).

```bash
# Pulsar-internal identifiers that should NEVER appear in consumer source.
# This list is conservative ; tailor it as Pulsar's surface evolves.
fingerprints=(
  'pulsar-multi-stream'
  'pulsar-frontend-stub'
  'pulsar-headless'
  'pulsar:BitrateAdjusted'
  'PULSAR_BUILD_HEADLESS'
  'pulsar_frontend_init'
  'pulsar_frontend_finished_loading'
)
fail=0
for fp in "${fingerprints[@]}"; do
  if grep -rn --include='*.ts' --include='*.tsx' --include='*.js' --include='*.cpp' \
       --include='*.h' --include='*.py' --include='*.rs' \
       --exclude-dir='node_modules' --exclude-dir='.git' \
       --fixed-strings "$fp" src/ 2>/dev/null \
     | grep -v -E '(README|CHANGELOG|docs/|//.*comment|#.*comment)'; then
    echo "FAIL : Pulsar-internal identifier '$fp' found in consumer source."
    fail=1
  fi
done
[ $fail -ne 0 ] && exit 1
echo "OK"
```

**Pass** : `OK`. **Fail** : matched line printed for any fingerprint.

### Check 5 — runtime imports do not link libobs (Windows)

The strongest check on the actual binary your users run. After your
build produces `<consumer>.exe`, run :

```powershell
# PowerShell, with msvc-dev-cmd or VS Build Tools on PATH.
$exe = "out/<consumer>.exe"   # adjust to your build output
$imports = & dumpbin /imports $exe 2>&1
$forbidden = @("obs.dll", "obs-frontend-api.dll", "pulsar.exe", "libobs.dll")
$leaked = @()
foreach ($name in $forbidden) {
    if ($imports -match [regex]::Escape($name)) { $leaked += $name }
}
if ($leaked.Count -gt 0) {
    Write-Error "FAIL : consumer binary imports forbidden libraries : $($leaked -join ', ')"
    exit 1
}
Write-Host "OK"
```

**Pass** : `OK`. **Fail** : any forbidden import.

### Check 6 — runtime imports do not link libobs (macOS)

```bash
# After your .app or executable is built.
EXE=path/to/your/consumer
otool -L "$EXE" | grep -iE '(libobs|obs-frontend|pulsar)' \
  && echo "FAIL : consumer binary links forbidden libraries." && exit 1
echo "OK"
```

**Pass** : `OK`. **Fail** : any matched library path.

### Check 7 — runtime imports do not link libobs (Linux)

```bash
# After your AppImage / deb / static binary is produced.
EXE=path/to/your/consumer
ldd "$EXE" 2>/dev/null | grep -iE '(libobs|obs-frontend|pulsar)' \
  && echo "FAIL : consumer binary links forbidden libraries." && exit 1
echo "OK"
```

**Pass** : `OK`. **Fail** : any matched library path.

### Check 8 — Pulsar binary is consumed as an opaque blob

The only legal way to embed Pulsar in your installer is :

1. Install the npm package `@clodocapeo/pulsar-bundle` or
   `@clodocapeo/pulsar-bundle-full` — its postinstall fetches
   `pulsar.exe` and stores it under
   `node_modules/@clodocapeo/pulsar-bundle*/bin/`.
2. Or copy the **standalone binary** `pulsar.exe` (from a GitHub
   Release zip) into a dedicated subdirectory, e.g. `resources/pulsar/`.

Anything else (e.g. a compiled-in static archive, a fat library that
embeds libobs symbols into your own binary) violates invariant #1.

```bash
# Empirical : the only files referring to "pulsar.exe" by name must be
# either the npm-bundle package directory or a dedicated resources/pulsar/
# directory. A reference from elsewhere (a static-link manifest, a
# manual install script that overwrites your binary, …) is suspicious.
grep -rni 'pulsar\.exe' \
  --exclude-dir='node_modules' --exclude-dir='.git' \
  src/ 2>/dev/null \
| grep -v -E '^[^:]+/(resources/pulsar/|@clodocapeo/pulsar-bundle)' \
&& echo "FAIL : pulsar.exe referenced outside the allowed locations." && exit 1
echo "OK"
```

**Pass** : `OK`. **Fail** : suspicious reference printed.

---

## Reference script `pulsar-consumer-audit.sh`

Drop this verbatim into your consumer repo at
`scripts/pulsar-consumer-audit.sh`. It runs Checks 1 through 4 + 8
(static, language-agnostic). Add the platform-specific binary checks
(5 / 6 / 7) into your platform-build job.

```bash
#!/usr/bin/env bash
# Consumer audit for repos that bundle Pulsar.
#
# Static checks for invariants #1, #3, #4 of LICENSE-INVARIANTS.md.
# Platform-specific binary checks (Windows dumpbin, macOS otool,
# Linux ldd) live in your platform-build job — they cannot run on a
# generic Linux runner.
#
# Drop into your repo, invoke from CI :
#   bash scripts/pulsar-consumer-audit.sh
# Exit 0 = clean, non-zero = at least one invariant broken.

set -euo pipefail

cd "$(dirname "$0")/.."   # repo root

SOURCE_DIRS=(src)         # adjust to your repo layout
EXCLUDE_GLOBS=(--exclude-dir='node_modules' --exclude-dir='.git' --exclude-dir='dist' --exclude-dir='build' --exclude-dir='out')
LANG_GLOBS=(--include='*.ts' --include='*.tsx' --include='*.js' --include='*.mjs' --include='*.cjs' \
            --include='*.cpp' --include='*.cc' --include='*.c' \
            --include='*.h'   --include='*.hpp' \
            --include='*.py'  --include='*.rs'  --include='*.go')

fail=0

echo "::group::Check 1 — no Pulsar / libobs C/C++ headers"
hits=$(grep -rn -E '#\s*include\s*[<"](obs[a-zA-Z0-9_/-]*\.h(pp)?|graphics/[a-zA-Z0-9_-]+\.h|media-io/[a-zA-Z0-9_-]+\.h|util/[a-zA-Z0-9_-]+\.h)[>"]' \
  --include='*.cpp' --include='*.cc' --include='*.c' \
  --include='*.h'   --include='*.hpp' \
  "${EXCLUDE_GLOBS[@]}" \
  "${SOURCE_DIRS[@]}" 2>/dev/null || true)
if [ -n "$hits" ]; then
  echo "FAIL : libobs headers in consumer source :"
  echo "$hits"
  fail=1
else
  echo "OK"
fi
echo "::endgroup::"

echo "::group::Check 2 — no node-gyp manifests"
m_fail=0
for f in bindings.gyp binding.gyp; do
  if [ -f "$f" ]; then echo "FAIL : $f at repo root."; m_fail=1; fi
done
matches=$(find . -path './node_modules' -prune -o -path './.git' -prune \
  -o -type f \( -name 'bindings.gyp' -o -name 'binding.gyp' \
             -o -name '*.gyp' -o -name '*.gypi' \) -print 2>/dev/null || true)
if [ -n "$matches" ]; then
  echo "FAIL : node-gyp manifests found in tracked source :"
  echo "$matches"
  m_fail=1
fi
if [ $m_fail -ne 0 ]; then fail=1; else echo "OK"; fi
echo "::endgroup::"

echo "::group::Check 3 — no dynamic-load of obs/pulsar artefacts"
hits=$(grep -rni -E '(LoadLibrary|GetProcAddress|dlopen|CDLL|libloading|process\.dlopen)' \
  "${LANG_GLOBS[@]}" "${EXCLUDE_GLOBS[@]}" \
  "${SOURCE_DIRS[@]}" 2>/dev/null || true)
hits=$(echo "$hits" | grep -iE '(obs|pulsar)' | grep -v '://' || true)
if [ -n "$hits" ]; then
  echo "FAIL : dynamic-load reference to obs/pulsar :"
  echo "$hits"
  fail=1
else
  echo "OK"
fi
echo "::endgroup::"

echo "::group::Check 4 — no Pulsar-internal identifiers (copy-paste fingerprints)"
fingerprints=(
  'pulsar-multi-stream'
  'pulsar-frontend-stub'
  'pulsar-headless'
  'pulsar:BitrateAdjusted'
  'PULSAR_BUILD_HEADLESS'
  'pulsar_frontend_init'
  'pulsar_frontend_finished_loading'
)
fp_fail=0
for fp in "${fingerprints[@]}"; do
  hits=$(grep -rn --fixed-strings "$fp" \
    "${LANG_GLOBS[@]}" "${EXCLUDE_GLOBS[@]}" \
    "${SOURCE_DIRS[@]}" 2>/dev/null || true)
  if [ -n "$hits" ]; then
    echo "FAIL : Pulsar identifier '$fp' in consumer source :"
    echo "$hits"
    fp_fail=1
  fi
done
if [ $fp_fail -ne 0 ]; then fail=1; else echo "OK"; fi
echo "::endgroup::"

echo "::group::Check 8 — pulsar.exe only referenced from approved locations"
hits=$(grep -rni 'pulsar\.exe' \
  "${EXCLUDE_GLOBS[@]}" "${SOURCE_DIRS[@]}" 2>/dev/null || true)
hits=$(echo "$hits" | grep -v -E '/(resources/pulsar/|@clodocapeo/pulsar-bundle)' || true)
if [ -n "$hits" ]; then
  echo "FAIL : pulsar.exe referenced outside allowed locations :"
  echo "$hits"
  fail=1
else
  echo "OK"
fi
echo "::endgroup::"

if [ $fail -ne 0 ]; then
  echo ""
  echo "::error::Consumer audit FAILED. See LICENSE-INVARIANTS.md (#1, #3, #4)."
  exit 1
fi

echo ""
echo "::notice::Consumer audit (static checks) passed."
echo "::notice::Don't forget the platform binary checks (dumpbin / otool / ldd)"
echo "::notice::in your platform-build job — see CONSUMER-AUDIT.md Checks 5-7."
```

---

## Reference workflow `pulsar-consumer-audit.yml`

Drop verbatim into your consumer repo at
`.github/workflows/pulsar-consumer-audit.yml`.

```yaml
name: pulsar-consumer-audit

on:
  pull_request:
  push:
    branches: [main]   # adjust to your default branch

jobs:
  static:
    name: static checks (Pulsar invariants #1 #3 #4)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run consumer audit
        run: bash scripts/pulsar-consumer-audit.sh

  windows-binary:
    # Add this job only if your consumer ships a Windows binary.
    # Wire it AFTER your build step so <consumer>.exe exists on disk.
    name: windows binary imports check
    runs-on: windows-2022
    needs: build-windows   # YOUR build job that produces the exe
    steps:
      - uses: actions/checkout@v4
      - uses: ilammy/msvc-dev-cmd@v1
        with:
          arch: x64
      - name: dumpbin /imports
        shell: pwsh
        run: |
          $exe = "<adjust>/<consumer>.exe"
          $imports = & dumpbin /imports $exe 2>&1
          $forbidden = @("obs.dll", "obs-frontend-api.dll", "pulsar.exe", "libobs.dll")
          $leaked = @()
          foreach ($name in $forbidden) {
            if ($imports -match [regex]::Escape($name)) { $leaked += $name }
          }
          if ($leaked.Count -gt 0) {
            Write-Error "FAIL : consumer binary imports forbidden libraries : $($leaked -join ', ')"
            exit 1
          }
          Write-Host "::notice::Consumer binary imports OK."

  macos-binary:
    # Add this job only if your consumer ships a macOS binary.
    name: macos binary linkage check
    runs-on: macos-latest
    needs: build-macos
    steps:
      - uses: actions/checkout@v4
      - name: otool -L
        run: |
          EXE="<adjust>/Contents/MacOS/<consumer>"
          if otool -L "$EXE" | grep -iE '(libobs|obs-frontend|pulsar)'; then
            echo "::error::FAIL : consumer binary links forbidden libraries."
            exit 1
          fi
          echo "::notice::Consumer binary linkage OK."

  linux-binary:
    # Add this job only if your consumer ships a Linux binary.
    name: linux binary linkage check
    runs-on: ubuntu-latest
    needs: build-linux
    steps:
      - uses: actions/checkout@v4
      - name: ldd
        run: |
          EXE="<adjust>/<consumer>"
          if ldd "$EXE" 2>/dev/null | grep -iE '(libobs|obs-frontend|pulsar)'; then
            echo "::error::FAIL : consumer binary links forbidden libraries."
            exit 1
          fi
          echo "::notice::Consumer binary linkage OK."
```

---

## When to update this audit

The fingerprint list (Check 4) and the allowed-locations list
(Check 8) are the parts that drift over time.

- **Fingerprint list** : if Pulsar adds a new plugin or a new vendor
  request, append its identifier to `fingerprints` in your local
  copy of the script. Keep your script in sync with the latest
  `LICENSE-INVARIANTS.md` references.
- **Allowed-locations list** : if you change where you stage
  `pulsar.exe` in your bundle (e.g. you move from
  `resources/pulsar/` to `assets/binaries/`), update Check 8's
  whitelist accordingly.

---

## Self-attestation is not the audit

If you do not run these scripts in your CI, you have not done the
audit. There is no "we read the doc and we're confident" path. The
whole point of these checks is that they're empirical : a file
exists / doesn't exist, a binary imports / doesn't import. Either
the script returns 0 in your CI or it doesn't.

If a check would normally fail but you have a documented exception
(e.g. you do legitimately need a library named `libobs-something`
that has nothing to do with OBS), update the script's regex
allow-list with a comment explaining why. **Don't disable the
script.** Don't skip jobs. The audit is the only thing that keeps
your license intact ; treating it as optional defeats it.
