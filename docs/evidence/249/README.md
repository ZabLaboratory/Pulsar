# #249 — dual-lane core canary evidence

This directory is the compact evidence index for
`ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane-20260828` and the Keeper-owned
integration unit `ZabLaboratory/Pulsar#249`. Large traces, recordings and CI
archives remain outside Git when they contain operational noise; this index
must record their exact candidate SHA, artifact/trace SHA-256 and acceptance
status before the issue can be handed to Probe.

## Evidence rules

- Evidence is valid only when it names the exact built candidate and exact
  runtime ID. An old binary or an unbound local fixture is not a canary.
- `encoder_input_raw` and `directshow_return` are separate boundaries. A fast
  raw result cannot stand in for the DirectShow-return result.
- Encoded first packet, RTMP receiver, decoded frame and antenna/player timing
  remain separate diagnostics; none is an antenna guarantee.
- WGC+CEF screenshots must be non-black and pixel-gated. A blank frame is a
  rejected attempt, never an acceptable placeholder.
- The bounded direct-vendor idempotency cache is guarded in source and by its
  native config/unit coverage. The public rollback probe exercises the
  gateway rejection path; it does not claim a second transport that bypasses
  that gateway for direct-adapter saturation.
- No password, token, private path or unredacted process log belongs in this
  directory.

## Candidate and campaign record

The following rows are populated by Keeper after the exact candidate is built
and the commands in `docs/runbooks/pulsar-dual-lane-canary.md` complete.

| Evidence | Candidate / artifact | Runtime IDs | Status |
| --- | --- | --- | --- |
| x264, >=100 warm + measured Takes | exact SHA + artifact SHA-256 required | one fresh ID | PENDING_EXECUTION |
| NVENC, >=100 warm + measured Takes | exact SHA + artifact SHA-256 required | one fresh ID | PENDING_EXECUTION |
| raw + DirectShow-return SLO report | exact trace/report SHA-256 required | x264 + NVENC IDs | PENDING_EXECUTION |
| rollback frame-boundary drill x264 | exact SHA + recording SHA-256 required | one fresh ID | PENDING_EXECUTION |
| rollback frame-boundary drill NVENC | exact SHA + recording SHA-256 required | one fresh ID | PENDING_EXECUTION |
| namespace / DirectShow lease | exact log digest or compact summary required | distinct IDs | PENDING_EXECUTION |

The rollback harness also requires the machine-readable
`pulsar.dual-lane-rollback.v1` marker
`pulsar-dual-lane-rollback.json` beside the recording output. Its runtime ID,
frame/PTS, lane roles, frozen state and observed surface properties
(`lane_root_binding_valid`, stable Program/Preview views and
`active_video_t_rebound=false`) must match the structured rollback log and the
committed frame.

For each traced active canary, the harness must keep an actual
`Pulsar Program Return` DirectShow consumer open for the entire Take loop. A
trace without `directshow_return` observations from that consumer is
`UNPROVEN`; raw or RTMP observations cannot substitute for AC-08.

## Prior core inputs

The consolidation relies on the exact reports and merged changes from the
closed predecessor issues; these links are references, not replacements for
the fresh Keeper canary:

| Area | Input |
| --- | --- |
| scene lifecycle, stable A/B and atomic Cut | [#242](https://github.com/ZabLaboratory/Pulsar/issues/242), [#244](https://github.com/ZabLaboratory/Pulsar/issues/244) |
| runtime identity, namespace and alias isolation | [#243](https://github.com/ZabLaboratory/Pulsar/issues/243), [#246](https://github.com/ZabLaboratory/Pulsar/issues/246), [#248](https://github.com/ZabLaboratory/Pulsar/issues/248) |
| common Program audio | [#245](https://github.com/ZabLaboratory/Pulsar/issues/245) |
| runtime telemetry and distinct latency boundaries | [#246](https://github.com/ZabLaboratory/Pulsar/issues/246) |
| lifecycle, idempotence, stale, timeout and concurrency QA | [#247](https://github.com/ZabLaboratory/Pulsar/issues/247) |

The final Keeper `AGENT_REPORT` on #249 is the authoritative status for the
rows above. Until all required rows are replaced with exact observations, the
unit is not `READY_FOR_PROBE`.
