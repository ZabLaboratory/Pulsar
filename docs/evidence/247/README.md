# Issue #247 deterministic lifecycle evidence

This directory contains two deliberately separate evidence legs for
`ZabLaboratory/Pulsar#247` (`PUL-DL-06`) against
`ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane-20260828`.

## Contract-volume leg

`scene-switch-lifecycle-race.json` is produced by the transport-neutral
`SceneSwitchMachine` reference implementation:

```text
python -m scripts.contracts.scene_switch_v1.lifecycle_campaign \
  --cycles 128 --attempts 1024 \
  --output docs/evidence/247/scene-switch-lifecycle-race.json
```

It contains the complete command/result matrix, deterministic route snapshots,
frame witnesses, 128 repeated lifecycle sequences, 1,024 controlled duplicate,
concurrent, stale, and conflicting attempts, a distinct two-`Take` race, and
operator/shutdown/superseded/timeout abort checks.  Its frame hashes are
reference-model witnesses only; they are not claims about GPU or encoded
pixels.

## Physical runtime leg

`dual-lane-runtime.json` is produced by
`scripts/probe-247-runtime.py`, which wraps the existing public WebSocket
dual-lane probe and drives the exact built candidate:

```text
python scripts/probe-247-runtime.py \
  --exe <exact-built-pulsar.exe> --encoder x264 --takes 100 \
  --output docs/evidence/247/dual-lane-runtime.json
```

The recorded run used the Keeper baseline candidate at
`D:\Documents\Zab\.agent-tmp\keeper-246-baseline-artifact\upstream\build_x64\rundir\RelWithDebInfo\bin\64bit\pulsar.exe`,
SHA-256
`a7723c366b20405b7519ef41a134aec6039e873360b2162350bc53957d704bd3`.
The artifact retains 100 real `TakeCommitted` rows with LaneA/LaneB roots,
ProgramView/PreviewView, ProgramVideo/PreviewVideo, frame IDs and monotonic
PTS; the actual pre-commit `PREVIEW_FROZEN` WebSocket response; post-commit
Preview visibility after the 30-frame settle; the recording hash; and decoded
YUV420P plus NV12-layout SHA-256 hashes for every encoded frame.  The decoded
NV12 values are physical-output witnesses, not raw encoder-input samples.

The runtime candidate does not yet expose `pulsar.scene-switch.v1` over its
WebSocket boundary.  The artifact's `contract_surface_probe` records all three
requests (`Prepare`, `Take`, `Abort`) returning code 204 (`Your request type is
not valid`).  Consequently, the 1,024 command attempts in the contract leg
must not be presented as runtime protocol coverage.  Implementing that shared
surface is a same-issue contract/integration hand-off (`CONDUIT_REQUIRED`);
this Probe unit intentionally stops at the reproducible finding and does not
change production architecture.

## Criterion-to-proof map

| Issue #247 criterion | Proof |
| --- | --- |
| Preview mutation before commit has no effect | Contract rows' `PREVIEW_FROZEN`, immutable route snapshots; physical batch status 702 and absent frozen item |
| Only the new Preview is mutable after commit | Contract post-commit hash comparison; physical post-commit scene item visible after 30 frames while Program/Preview remain distinct |
| At least 100 lifecycle/alias sequences | Contract `cycles=128`; physical `take_committed_count=100` |
| At least 1,000 duplicate/concurrent/stale attempts | Contract `attempts=1024`, four result classes, full `command_result_matrix` |
| Double/concurrent Take and exactly one commit | Contract `concurrent_take_race` (1 accepted/1 `SERVER_SEQ_STALE`, 1 commit) plus one commit per lifecycle intent |
| Stale revisions and conflict reuse | Contract matrix `REVISION_STALE` and `IDEMPOTENCY_CONFLICT` rows with unchanged role/revision state |
| Abort mapping preservation | Contract four abort reasons with identical role map and revisions before/after |
| Stable physical route identities and frame boundary evidence | Physical `runtime_identity`, `route_assertions`, 100 parsed frame/PTS rows and recording frame hashes |
