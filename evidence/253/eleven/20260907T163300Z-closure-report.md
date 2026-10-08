# Local delivery report — Pulsar #253

Role: Eleven. Agent thread: `01a045e2-996f-7913-99f0-2cd237d3a6f8`.
Work unit: `ZabLaboratory/Pulsar#253`.
Authority: local optimization, native libobs changes, safe validation and local delivery. No remote mutation, merge, publication, deployment or issue closure.

## Outcome

The qualified Windows 1080p60 NV12 CPU readback optimization is active by default in code/binaries at `5fba882446741f863744f68d1c5726aeb2dacc94`. Two reference and three candidate trials measure 500 Cuts after 500 warm-ups with the same native decoded-frame observer. First changed decoded-image p95 improves from 45.897–45.947 ms to 33.045–37.982 ms: approximately 8–13 ms / 17–28%. The runtime is exported, not left inside a disposable worktree.

## Criteria → evidence

| Criterion | Evidence | Result / boundary |
| --- | --- | --- |
| Actual product gain, not a receiver substitution | `20260907T163200Z-native-study.json`, x264 current/reference/default runs | PASS, same observer for both sides |
| Promotion without compression downgrade | Patches 0053/0055, retained encoder setup | CPU readback/timestamps only; no bitrate, preset, B-frame or audio format reduction |
| Decoded image identity and media time | Native receiver and exact packet-content mapping; archive native decoder/reference | 876/876 video frames match full visible-plane MD5 and PTS |
| Program audio continuity | Archive `20260907T161400Z-audio-{x264,nvenc}.{json,log}` | 100 Cuts each; AAC 48 kHz continuous monotone PTS; stable route |
| Decoded A/V alignment | Six flash/click reports, labelled baseline/experimental/aligned/default | Promoted path +5, -8.667, +15.333 ms; within one frame plus detector tick |
| Regression | Archive Python and final CTest logs | 236 Python pass / 1 skip; 21/22 CTest pass |
| GPU error lifecycle | CTest `async-gpu-pending-error` | Fatal pending error stops encoder and permits same encoder/output restart |
| Full distributable | Runtime manifest in study; packaged x264/NVENC runs | 1254 files; packaged executable/DLL hashes match build; CEF/WGC benchmark passes |
| Evidence retention | `20260907T163000Z-native-run-evidence.zip` | 197 non-media entries, each SHA-256 checked against original before relocation |

## Decisions and remaining limits

- NVENC async output remains off: lower median did not improve p95. Lower-B-frame quality experiments failed thresholds and are not promoted.
- CTest directory-hardening test fails because `SeRestorePrivilege` is absent for two ownership gestures. This requirement is not weakened; no full security-suite pass is claimed.
- NVENC capacity AC-13 remains unproven without a single-lane reference. No global issue-253 acceptance or closure is asserted from this scoped latency result.
- No physical display, remote RTMP service, 4K or untested frame-rate latency guarantee.
- Final packaged NVENC 100-Cut marker p95: 74.860 ms. Packaged x264 five-Cut smoke p95: 30.192 ms, not used to inflate the 100-Cut improvement claim.

## Preservation and rollback

Original recordings, invalid experiments and logs remain under `Artifacts/2026-09-07/pulsar-253-optimized/raw-evidence/`; prior delivery folders are untouched. Evidence HMAC key is retained only in the canonical checkout's existing ignored DPAPI file, not exported. The full runtime, evidence zip, study, documentation and signed branch Git bundle are retained before removing the owning worktree. Canonical `main` and its pre-existing changes are preserved; local candidate branch is kept because it is not merged. Explicit `PULSAR_RAW_CURRENT_READBACK=0` restores the prior CPU staging path for diagnosis; prior delivered runtimes remain available.
