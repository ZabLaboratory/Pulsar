# Pulsar 3.0.0 — documentation and release candidate review

Owner: Eleven. Work unit: local-20260907-release-3-0-0.
Authority: requested 3.0.0 release, complete changelog since v2.0.0b,
comprehensive README/libobs coverage and all secondary documentation.
PR: https://github.com/ZabLaboratory/Pulsar/pull/289

## Criteria and observed proof

| Criterion | Risk addressed | Check / proof |
|---|---|---|
| Complete release inventory | Missing changes behind squash/history | 70 reachable commit SHAs from 514f504 to integrated 3ba9ab8 enumerated; release metadata/docs identified separately. |
| Current architecture | Obsolete single Default scene and bootstrap | Matched main.cpp, frontend controller, vendors, bundles, CMake, patch stack and packaging; current ownership described. |
| All OBS changes | Treating initial patch headers as final behavior | Five fork-integrated changes plus all 52 patch filenames/affected files documented; cumulative defaults and private-helper refinement distinguished. |
| README/secondary coverage | Conflicting integration commands and promises | 53 owned Markdown files in link inventory, outside historical evidence/vendor dependencies; approved ADRs/schema records preserved, studies annotated. |
| Navigable corpus | Broken files/headings | Node filesystem/heading checker: 354 local links, zero missing targets, zero missing anchors; fenced code excluded. |
| Executable examples | Invalid JavaScript snippets | Node --input-type=module --check: 11 JS blocks across root, development, embedding and three SDK/bundle guides; zero syntax errors. |
| Package type integrity | Version/dependency drift | npm run lint: five workspace TypeScript checks pass. |
| Package behavior | Client/lifecycle/correlator regression | 104 tests pass: client 64, light 11, full 11, correlator 18 including real FFmpeg fixture tests. |
| Patch hygiene | Whitespace mistakes | git diff --check passes. |

## Preserved limits

No native implementation, workflow, contract schema or approved ADR payload
is changed by this documentation increment. The release unit separately
bumps the native/client/bundle versions and internal client ranges.

The 104 package tests do not establish native 3.0.0 hardware behavior. The
exact final PR and tag must pass their own CI; release attachment, npm
readback, hashes and an artifact smoke test remain the publication gates.
The previous fd4ac40 PR pipeline passed, but is not claimed as validation of
this later documentation commit.

Historical GPU/decoded-latency measurements retain workload bounds. Optional
NVENC experiments remain off, Preview audio/AFV remains descoped, Windows
wrapper termination is not a recording-finalization guarantee, and SDK env
unsetting is not a reliable disable mechanism for system-discovered VFX SDKs.

## Rollback and scope

Before publication, revert the coherent release candidate through normal
review if needed. After publication, retain immutable tag/assets/npm versions
and fix forward. No consumer repository upgrade is authorized by this release
alone. Preserve the unrelated dirty canonical checkout and upstream work.

## CI baseline reconciliation

Compliance run 34166453160 rejected the removed DEVELOPMENT.md example still
listed in .secrets.baseline (no new finding reported; verified-secret scan
passed). Remove only that obsolete record. Local detect-secrets 1.5.0 rescan
matches all 22 remaining findings, including line/type/verification state,
after normalizing Windows path separators. Preserve every scanner/filter
setting and the existing baseline formatting. Exact-head CI must revalidate
this metadata reconciliation; no scan is skipped or weakened.
