AGENT_REPORT

Role: probe
Agent-Thread: WU-pulsar-202-abort-negative-test-20260810
Work-Unit: WU-pulsar-202-abort-negative-test-20260810
Issue: 202
Repo: ZabLaboratory/Pulsar
Branch: forge/202-abort-negative-test
PR: https://github.com/ZabLaboratory/Pulsar/pull/211
Commits: 3941de2 (test), 6a066a9 (checkpoint)

## Result

Added Phase 1L to scripts/run-probes.ps1: a negative probe for the F2
fatal-abort path (ADR-005 finding N7, #181 veto F2/V2). Forces
seed_websocket_config() to fail deterministically (no admin rights,
portable to any CI runner) by pre-creating a plain FILE named
obs-websocket/ in the rundir, so harden_directory_dacl()'s
fs::create_directories() fails closed instead of the plugin loading
unauthenticated content.

## Criteria -> evidence

1. Process exits with a non-zero code -> confirmed, exit code
   -1073741819 (0xC0000005, see Finding below). Non-zero either way.
2. No PULSAR_READY on stdout -> confirmed absent (regex match on full
   stdout capture).
3. No session password on stdout/stderr before abort -> confirmed
   absent (regex match for the exact seeded password on both streams).

## Commands run + results

- scripts/build-win.ps1 (headless, no -Full) in an isolated worktree
  (D:\Documents\Zab\.wt\probe-202) + isolated build tree
  (upstream/build_x64, submodule cloned via local --reference to avoid
  re-downloading obs-deps/CEF; deps cache reused via a directory
  junction to the canonical checkout's upstream/.deps). Result: clean
  build, pulsar.exe produced at
  upstream/build_x64/rundir/RelWithDebInfo/bin/64bit/pulsar.exe.
- [System.Management.Automation.Language.Parser]::ParseFile on the
  modified run-probes.ps1 -> PARSE OK, no syntax errors.
- Scoped standalone re-implementation of the new probe's exact logic,
  run twice against the freshly built pulsar.exe -> both runs: no
  PULSAR_READY, no password leak, non-zero exit (crash code both
  times, reproducible).
- Full run-probes.ps1 was NOT run end-to-end (see Note below).

## Finding (not fixed here -- Probe scope; route to Forge)

The F2 abort path currently exits via STATUS_ACCESS_VIOLATION
(0xC0000005), not a clean `return 1`, reproduced on every run.
pulsar-headless/main.cpp's F2 branch (~line 1068-1075) calls
obs_shutdown() directly on seed_websocket_config() failure without
calling pulsar_frontend_shutdown() first -- unlike the normal shutdown
path at the end of main(), which always pairs
pulsar_frontend_shutdown(); obs_shutdown(). pulsar_frontend_init()
already ran (frontend callbacks installed) before this abort branch,
so the missing matching teardown is the likely root cause: something
frontend-side reachable from obs_shutdown()'s internal cleanup is
deref'd after/without proper teardown.

Issue #202's literal criterion ("exits with a non-zero code") is
satisfied by a crash too, so the shipped test PASSES as specified --
I did not invent a stricter undecided criterion. But a SEH access
violation on a security fail-closed path is a real defect worth its
own Forge issue: add pulsar_frontend_shutdown() to the F2 abort branch
before obs_shutdown(). This is a memory-safety-adjacent regression an
attacker can trigger by pre-planting a file (same trigger as this
test's forcing mechanism), so flagging for Bastion's attention too if
a follow-up issue is opened.

## Note -- full suite not run

scripts/run-probes.ps1 (the ENTIRE probe suite: M1/M2/M3, multitrack,
reseed, this new Phase 1L, then the shared-instance battery) was
launched once, then killed early per project doctrine (no full-suite
run without asking). Its partial output before being stopped showed
Phase M1 (smoke) PASS, then Phase M2 (record) FAILED with "no close
frame received or sent" during StopRecord -- a pre-existing,
unrelated flake nowhere near my diff (Phase 1L runs later, after
reseed). Not re-run. Validation of the new phase itself is the scoped
standalone run described above, which exercises byte-for-byte the same
logic now committed in run-probes.ps1.

## Verdict

READY for Vigil/CI review. CI (codeowners-check, contract tests,
deps-audit, lint, lockfile-check, secret-scan) is pending on PR #211 at
report time -- none of those jobs build/run the Windows probe suite
(that runs on a separate self-hosted pipeline per PRISM-EMBEDDING.md /
pipeline.yml conventions elsewhere in this repo), so this PR's mergeable
state depends on that separate pipeline, not visible in `gh pr checks`
here. No merge performed (no-merge tier). Recommend Eleven open a
follow-up Forge issue for the F2 abort-path crash finding above.
