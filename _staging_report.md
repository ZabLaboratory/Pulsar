AGENT_REPORT

Role: forge
Agent-Thread: WU-pulsar-183-log-handler-20260810
Work-Unit: WU-pulsar-183-log-handler-20260810
Issue: 183
ADR: docs/adr/005-go-live-failure-diagnosability.md §3.1-§3.2 (2026-08-10-r5)
PR: https://github.com/ZabLaboratory/Pulsar/pull/192
Branch: forge/183-log-handler (HEAD d085319629fe993afe74b9f9e3bbc25e6312f918)
Status: READY

## Result

`base_set_log_handler` installed before `obs_startup` (main.cpp:504). Formatter,
two-layer redactor (pattern + registry), rotating ACL'd file sink live in
plugins/pulsar-headless/log-handler.h/.cpp with zero obs.h/Qt dependency.
main.cpp's printf/fprintf converted to blog() per owned scope; :463/:465
(PULSAR_READY sentinel + idle marker) byte-for-byte untouched, verified by
diff and by CI's offline-probes job (20 probes green against pulsar.exe
built from this branch).

## Criteria -> evidence

- RC1/RC2/RC20: format_line/derive_subsystem unit-tested; blog() conversion
  list matches issue's owned scope; sentinel lines diffed unchanged.
- RC3/RC4: CI "offline probe suite (CTest)" job green — all probes +
  spawn.ts signal path exercised against the real built pulsar.exe.
- RC8/RC9/RC19: tests/log-handler-probe (11 assertions) — rotation stays
  under max_files, age purge fires under untouched size/count bounds,
  both redaction layers exercised separately incl. an oversized-line
  abandon case. Ran locally (MSVC 14.44, cl /std:c++17) AND in CI
  (ctest, "Passed 0.01 sec").
- RC18/RC21/RC22: default_log_dir excludes %APPDATA%; ACL restricted at
  creation + widened-dir refusal + unwritable-dir named-error, all
  asserted and green.
- RC11 (upstream/patches CI gate): NOT landed on this branch — Forge's
  GitHub App has no `workflows` scope, GitHub rejected the push outright.
  Drafted, reverted (commit 21ec4d5), needs Keeper.
- RC10/RC16 (zero secret occurrence end-to-end via live protocol traffic):
  not exercised — needs Probe/Bastion with a running session.

## Gaps / hand-off

1. **RC11 CI gate**: Keeper must land the upstream/patches boundary-gate
   step in .github/workflows/pipeline.yml (drafted verbatim in PR history,
   commit 01dd7c7, reverted in 21ec4d5) under an identity with `workflows`
   permission.
2. **Merge coupling (§3.10, non-negotiable)**: this PR must not merge
   without #184 (E, the §3.6.2 write kill-switch) landing at the same
   time — the rollback lever for R1 doesn't exist without it.
3. Redaction covers both stderr and file (beyond the letter of RC10/RC16,
   which only constrain the file) — a deliberate choice per D6, noted in
   the PR body, not silently expanded scope.

CI: all required checks green (lint, build pulsar.exe -Full, offline
probe suite/CTest, binary export gate, secret-scan, deps-audit,
lockfile-check, codeowners-check, contract tests).
No touch to upstream/ or patches/ — confirmed by `git status --short --
upstream patches` (clean) before every push.
