# Pulsar dual-lane canary and rollback runbook

This runbook is the operational companion for ADR-PULSAR-DUAL-LANE-001,
revision `draft-r2-dual-lane-20260828`, and issue #249. It covers only the
approved Cut core: two hot physical lanes, stable Program/Preview surfaces,
the common Program audio route, and a frame-boundary rollback freeze.

Fade/Stinger/T-bar, Preview audio/AFV, decoder tuning and full soak/recovery
remain separate follow-ups. A successful canary here must not be described as
an end-to-end decoded or antenna latency guarantee.

## Preconditions

1. Use the exact `pulsar-rundir` artifact produced by CI for the candidate
   commit. Record its GitHub artifact ID, SHA-256 and the extracted
   `pulsar.exe` SHA-256. Never use a stale local binary for a canary result.
2. Run on the declared 1080p60 host. For a traced WGC+CEF run, provide a
   visible, animated target descriptor and let the probe reject blank frames.
3. Use a fresh `PULSAR_RUNTIME_INSTANCE_ID` and an output directory outside
   the repository. Do not reuse a runtime directory or trace file from another
   candidate.
4. Verify the runtime/legacy-alias lease lines in the process log before
   accepting any measurements:

   ```text
   PULSAR_RUNTIME_INSTANCE ... instance_lock=acquired runtime_dir_lock=acquired
   PULSAR_LEGACY_ALIAS lease=acquired|disabled|refused ...
   ```

   A required legacy alias that is refused is not a successful alias canary;
   rerun with dedicated names or stop the conflicting holder. Do not make two
   runtimes share the historical `Program`/`Preview` aliases.

## Activation modes

The frontend resolves the dual-lane capability once during `setup()`. The
decision is process-local and cannot be changed through obs-websocket.

| Environment | Effective mode | Intended use |
| --- | --- | --- |
| unset | dual-lane enabled | normal runtime |
| `PULSAR_DUAL_LANE_ENABLED=1` | dual-lane enabled | explicit capability declaration |
| `PULSAR_DUAL_LANE_ENABLED=0` | compatibility single-canvas | controlled rollout gate |
| `PULSAR_DISABLE_DUAL_LANE=1` | compatibility single-canvas | backwards-compatible reference/rollback switch |
| malformed explicit value | disabled, fail-closed | operator configuration error |

The first relevant line must look like:

```text
[pulsar-dual-lane] activation=enabled|disabled source=... rollback_after_takes=0 flag_resolved_at=setup
```

The reference phase of `scripts/probe-dual-lane.py` sets
`PULSAR_DISABLE_DUAL_LANE=1` and `PULSAR_TRACE_RESOURCE_MODE=reference`; the
effective startup decision is logged as `source=resource-reference` because
the resource-reference mode owns the compatibility-path selection. The probe
verifies that effective decision rather than trusting either environment
assignment.

## Standard codec canaries

Run the standard campaign independently for each codec. `--takes 100` means
100 measured Takes; a traced run first executes 100 warm-up Takes in the same
runtime and records 200 committed Takes total. The warm-up prefix is excluded
by `probe-take-latency.py` from the SLO percentiles. This checks the live A/B
roots, stable surfaces, atomic frame-boundary Cut, command safety, recording
and exactly one encoder binding.

```powershell
python scripts/probe-dual-lane.py `
  --exe <exact-artifact> --encoder x264 --takes 100

python scripts/probe-dual-lane.py `
  --exe <exact-artifact> --encoder nvenc --takes 100
```

For a complete runtime trace (including the two normative latency boundaries),
run each codec with a separate trace and candidate SHA:

```powershell
python scripts/probe-dual-lane.py `
  --exe <exact-artifact> --encoder x264 --takes 100 `
  --trace <evidence-dir>\x264.jsonl `
  --runtime-id pulsar-249-x264-<unique> `
  --build-revision <40-lowercase-candidate-sha> `
  --capture-window <visible-title:class:exe> --cef-workload

# NVENC owns the resource comparison: append the dual-lane phase to the same
# reference trace/runtime after the single-canvas reference process exits.
python scripts/probe-dual-lane.py `
  --exe <exact-artifact> --encoder nvenc `
  --trace <evidence-dir>\nvenc.jsonl `
  --runtime-id pulsar-249-nvenc-<unique> `
  --build-revision <40-lowercase-candidate-sha> `
  --capture-window <visible-title:class:exe> --cef-workload `
  --resource-mode reference --resource-only

python scripts/probe-dual-lane.py `
  --exe <exact-artifact> --encoder nvenc --takes 100 `
  --trace <evidence-dir>\nvenc.jsonl `
  --runtime-id pulsar-249-nvenc-<unique> `
  --build-revision <40-lowercase-candidate-sha> `
  --capture-window <visible-title:class:exe> --cef-workload `
  --trace-append --resource-mode dual_lane
```

The trace must contain at least 100 observed warm-up Takes followed by 100
observed measured Takes for each codec (200 committed Takes total with the
standard command). Validate it with the boundary-aware parser, which reports
the two observed counts and excludes the warm-up prefix:

```powershell
python scripts/probe-take-latency.py `
  --trace <evidence-dir>\x264.jsonl <evidence-dir>\nvenc.jsonl `
  --output <evidence-dir>\latency-report.json
```

The x264 campaign has no resource phase: its AC-13 status is explicitly
`NOT_APPLICABLE` because AC-13 measures the WGC+CEF+NVENC resource delta;
resource samples from an x264 trace can never substitute for that proof. The
NVENC trace must contain both the reference and dual-lane resource phases and
is the only campaign that may report AC-13 as `MEASURED`. Its reference
`--resource-only` invocation starts a real NVENC recording before counting
samples, requires `OUTPUT_STARTED`, and stops/verifies that recording before
returning. Samples without the observed `encoder_active=true` state or the
matching `encoder_family=nvenc` identity remain diagnostic and cannot satisfy
AC-13. The aggregate report is `PASS` only when
independent passing x264 and NVENC campaigns are both present; it exposes
per-codec/session coverage and never pools samples across traces.

The report is acceptable only when `encoder_input_raw`, `directshow_return`,
and the distinct `rtmp_first_packet` receiver boundary are present with their
own counts and p95 values. The limits are raw p95 `<=50 ms`, DirectShow-return
p95 `<=75 ms`, and RTMP receiver/demux p95 `<=15 ms`. The pre-network
`encoded_first_packet` callback is auxiliary and can never satisfy AC-12.
RTMP receiver evidence is not wire-level and never substitutes for raw or
DirectShow; decoded/player/antenna values are diagnostic only.

If the machine cannot supply a real WGC/CEF producer, record a typed skip;
never convert the color-source or resource-only fixture into a production
capacity claim. The resource reference/dual-lane method and the no-blank WGC
gate are described in `docs/runbooks/probe-take-latency.md` and were first
exercised by issues #246 and #247.

## Frame-boundary rollback drill

The rollback drill is intentionally separate from the long canary. It arms a
bounded boot-time trigger and performs one real Cut:

```powershell
python scripts/probe-dual-lane-rollback.py `
  --exe <exact-artifact> --encoder x264

python scripts/probe-dual-lane-rollback.py `
  --exe <exact-artifact> --encoder nvenc
```

The harness sets `PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES=1` for the child. The
expected sequence is:

1. A/B are ready and the encoder is bound once to the stable Program surface.
2. Take 1 is accepted and committed at `(frame_id, pts_ns)`.
3. The rollback log reports the same frame/PTS and the observed surface
   properties: `current_program_preserved=1`, `active_video_t_rebound=0` and
   `new_takes_enabled=0`. These values are derived from live identities, not
   asserted constants.
4. Program remains the committed scene and Preview remains the other scene.
5. A subsequent Preview/scene mutation receives `PREVIEW_FROZEN` and does not
   alter the scene graph; reads remain available.
6. The recording remains valid and the encoder-binding count stays exactly
   one.

The harness also calls the versioned vendor API after the freeze. `GetState`
is still available as an observation path and reports `state=frozen` with
`operational=false`; valid `Prepare`, `Take`, and `Dispatch` payloads are
rejected by the closed mutation gateway with `PREVIEW_FROZEN` before the vendor
state machine can mutate anything. A before/after `GetState` comparison proves
that state, `server_seq`, revisions and role mapping are unchanged. This
public probe intentionally claims gateway coverage only: the direct vendor
adapter keeps the same fail-closed guard and bounded cache, but the websocket
transport does not expose a second route that bypasses the gateway for a
separate direct-adapter saturation claim.

The marker is written asynchronously by a process-lifetime worker so the
graphics callback performs no directory creation or file I/O. The probe waits
for and validates the marker before stopping the process.

This is an in-process operational freeze. It does not pretend to convert the
already-live two-view topology into a single view. Once the current Program
has drained at the committed boundary, stop outputs, restart the process with
`PULSAR_DISABLE_DUAL_LANE=1`, and explicitly label that process as the
compatibility/degraded path. At no point does rollback call a view/source
setter or rebind the active `video_t`.

If rollback is triggered by an integrity fault, preserve the trace and logs,
keep the runtime fail-stopped, and escalate the candidate for investigation.
Do not clear the flag, retry indefinitely or overwrite the evidence.

`PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES` is a bounded drill trigger, not the
activation safety flag. Only ASCII decimal digits representing `1..100000` are
accepted. A malformed value, an explicitly empty value, whitespace, a sign,
zero, overflow, or a value above `100000` is rejected and fails closed to the
compatibility single-canvas path; it must never leave dual-lane active while
silently disarming the drill. Such a run is not a rollback proof. Malformed
`PULSAR_DUAL_LANE_ENABLED` or `PULSAR_DISABLE_DUAL_LANE` values likewise fail
closed to the compatibility path.

## Namespace and lease checks

For an isolation check, launch runtimes with distinct IDs and distinct
directories. Confirm distinct configuration/log/record paths, ports and
internal queue names. For the historical DirectShow names, either:

```powershell
$env:PULSAR_LEGACY_ALIAS = "required"   # exactly one holder
$env:PULSAR_RUNTIME_INSTANCE_ID = "pulsar-249-alias-holder"
```

or use:

```powershell
$env:PULSAR_LEGACY_ALIAS = "dedicated"  # no historical singleton alias
```

The second holder must report `lease=refused` and continue only with its
validated dedicated mapping; silent alias takeover is a failure. The complete
runtime identity and DirectShow lease rules are in `docs/ARCHITECTURE.md` and
`docs/PRISM-EMBEDDING.md`, with the implementation/QA history in #243, #246,
and #248.

## Evidence ledger for core closure

This issue consolidates the already-validated core inputs; it does not
recreate them from prose.

| ADR area | Existing evidence | Keeper #249 proof to attach |
| --- | --- | --- |
| I1-I3, hot roots and role permutation | #242 / #244; `docs/evidence/247/` | exact x264/NVENC 100-Take logs and surface identities |
| I4-I6, stable surfaces and no active `video_t` rebind | #242, #244, #247 | one setup bind, unchanged through both canaries and rollback |
| I7-I9, atomic Cut and Preview lifecycle | #242 / #247 | commit frame/PTS, monotone roles, rollback boundary |
| I10-I11, ordering and idempotence | #247 | runtime contract/QA results; no new contract implementation here |
| I12, common Program audio | #245 | common route/PTS result from #245; Preview audio remains unsupported |
| I13, runtime namespace and alias lease | #243 / #246 / #248 | startup lease lines, distinct runtime IDs and dedicated/refused second holder |
| I14, separate measurements | #246 + Conduit AC-12 boundary | independent raw, DirectShow-return, RTMP receiver/demux and optional decoded fields |

| Acceptance criterion | Required Keeper evidence |
| --- | --- |
| AC-01/02 | 100 warm + 100 measured Takes per codec; stable A/B and Program/Preview surfaces |
| AC-03/04 | frame/PTS commit, no mixed-frame result, freeze and post-Take scene isolation |
| AC-05/06/11 | existing #247 contract/race evidence plus exact-candidate trace correlation |
| AC-07 | `probe-take-latency.py` raw p95 report, count >=100 per codec |
| AC-08 | independent DirectShow-return p95 report, count >=100 per codec |
| AC-09 | #245 common Program audio evidence; no Preview/AFV claim |
| AC-10 | runtime/alias lease logs and #243/#246/#248 evidence |
| AC-12 | `rtmp_first_packet` from the real loopback receiver, correlated by rational packet PTS/timebase; encoded callback remains auxiliary |
| AC-13 | NVENC-only reference versus dual-lane resource delta under WGC+CEF+NVENC; x264 is explicitly `NOT_APPLICABLE` |
| AC-14 | rollback probe output with matching committed frame/PTS and one encoder bind |

The evidence directory should contain only compact, deterministic summaries
and hashes for large traces/recordings; do not commit secrets, WebSocket
passwords or unredacted process logs.
