# Owner-accepted local proof and disabled automated broadcasts

Owner: Eleven. Thread: `01a045e2-996f-7913-99f0-2cee24ee86ee`.
Work unit: `local-20260908-twitch-x264`.
Base: main `b6cb978ce08b7ab635aff14f51cf7e2f841c3d64` (PR #295, verified merge).

## Authority and boundary

The owner explicitly requested stopping the local test after observing sufficient
proof, disabling both `live broadcast (Twitch)` and `publish proof to gh-pages`,
then dispatching a run with release publication. This supersedes the preceding
proposal to skip only GPU-less hosts. Both jobs are unconditionally disabled;
their retained probe code is not a claim of automated coverage.

The existing signed v3.0.0 tag and assets remain immutable. Publication on main
selects an existing draft explicitly and never rebuilds, uploads, clobbers or
retags that release. Future tag packaging remains separate and no longer depends
on a nonexistent live artifact. No npm republish is requested.

## Evidence

- Hosted run [34189919375](https://github.com/ZabLaboratory/Pulsar/actions/runs/34189919375)
  applied 1920x1080@60, x264 and 6000 kbps. No physical display GPU was detected.
  Its live job correctly failed after 30 seconds at 11.6915 sustained FPS
  (83.1-87.1 ms render observations), despite zero network-drop ratio.
  The run was subsequently cancelled, including CTest; it is not a green run.
- PR #295's preceding full pipeline [34189140698](https://github.com/ZabLaboratory/Pulsar/actions/runs/34189140698)
  and compliance passed before integration; merge tree equals its tested head.
- Local explicit x264 run used the exact extracted v3.0.0 release executable,
  SHA256 `1bf5635387ef290a11665d3804bddf023839e761ef5ff9a9ef5e4b2a523ab825`.
  The current strict probe confirmed native 1920x1080@60, x264 and accelerated
  browser_source. All 40 five-second observations through 200 seconds reported
  60.0 FPS, 6000 kbps and zero drop ratio. Adaptive sample count reached 99.
- On the owner's stop request, StopRecord finalized the MP4, then
  StopDestination returned `stopped=true`. The probe's next poll correctly
  rejected the externally stopped destination and exited 1. This is an
  owner-interrupted observation, **not a completed 600-second x264 test**.
- Recording: 202.048 seconds, H.264 1920x1080 60/1 plus AAC, 154,385,495 bytes;
  SHA256 `34964a25ab5b765b8410081875866fcc0f9f3e592ab9a25704a251386ed98896`.
  Local artifact: `Artifacts/2026-09-08/pulsar-3.0.0-x264-1080p60/` under Zab.
- The preceding separate exact-tag local hardware proof remains attached to the
  draft: 600 seconds, 120 observations at 60 FPS, 6000 kbps, zero output-skipped
  frames and zero adaptive drop ratio. It is not relabeled as this x264 run.

## Criterion -> risk -> validation

| Criterion | Risk | Validation |
| --- | --- | --- |
| Both automated jobs disabled | Accidental broadcast or Pages write | YAML contract requires literal `if: false` on both jobs |
| Release does not depend on disabled jobs | Permanently skipped release | Tag attach depends only on package; existing-draft publisher depends on all six non-live build/test jobs |
| Main cannot overwrite an immutable tag release | Wrong binary provenance | Explicit existing semantic tag; authenticated draft lookup by release id; downloads and verifies every size/SHA256; verifies runtime manifest and stable tag/notes/assets before and after publication |
| No accidental publication during inspection | Read-only check publishes | Script is verify-only unless `--publish`; regression tests inspect the exact gh mutation |
| Draft quirks handled | Public endpoint/temporary URL mismatch | Authenticated paginated release list includes drafts; manifest retains eventual tag URL instead of GitHub's `untagged-*` draft URL |
| Regressions guarded | Runtime/probe/preview behavior weakened | 51 focused workflow/publisher/probe/cache/preview tests passed locally; no native product code changed |

The publisher's complete verify-only path was also run against the real draft:
five existing assets verified, notes/tag preserved, and `draft=True` read back.
No external mutation was performed by this verification.

CI on this final change and the release-enabled dispatch remain pending at commit
time. The local owner-accepted observation does not convert either cancelled CI
or skipped live jobs into successful tests.

Rollback: revert this workflow/publisher/documentation change to re-arm the earlier
workflow policy. Existing release assets, notes, tag and npm packages are preserved.
