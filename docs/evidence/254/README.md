# Issue #254 dual-lane soak evidence

Campaign date: 2026-09-03. This evidence uses the exact main artifact at
`8930b2a9498d2fd36986e452482957c3bd87a742`.

Artifact SHA-256:

- `pulsar.exe`: `D3AEBCF7CFADA2F08D90680762ED25695CEEC965155A322B3AAA48562DE95863`
- `obs.dll`: `64C74858E5CEF38DA560530919B72A51693B28AA96619555C725C9F4D3682C7A`
- `pulsar-browser-page.exe`: `35FFF7269F985EEDFE2466149E242E6F32F2CF8CF77F57953C2CD46BA8FC2765`

All runs were dual-lane WGC + CEF + NVENC with the local DirectShow ProgramReturn
consumer and local RTMP receiver. No single-lane run, OBS Studio GUI, live Twitch,
or product-code edit was used. Large raw traces and receiver diagnostics are kept
outside Git at `D:\Documents\Zab\.tmp\keeper254-traces-20260903`; their names and
hashes are in `summary.json`.

## Results

The five-take smoke completed 105 total Takes (100 warmup + 5 measured), with
dual-lane topology, non-black WGC/CEF sources, monotone frame/PTS, zero drops, and
recording verification at 1920x1080/60fps H.264. Its measured latency was raw
input p50 26.5334 ms, DirectShow-return p50 26.776 ms, encoded-first-packet p50
48.7397 ms, and RTMP-first-packet p50 0.3333 ms. The raw and DirectShow SLO
percentiles remain unproven because only five measured takes were retained.

Two 100-measured-take long attempts reached 200 total Takes and preserved traces,
but both failed the native resource-sampler deadline. The sampler observed 423
samples over about 349 s in the first attempt and 145 samples over the second
attempt's deadline, with average cadence about 1.388 s for the latter despite a
500 ms request. The first long run grew resident memory from about 210 MiB to
558 MiB, peaking about 609 MiB; callback backlog peaked at 656, dropped callbacks
at 0. This is evidence of growth and sampler saturation/deadline behavior, not a
passing resource SLO.

The boundary attempt stopped progressing after 19 Takes and required a forced
process kill, so it is not a valid boundary SLO run. A fresh recovery attempt
reached source readiness but cleanup failed because the Pulsar log-reader thread
did not exit. A subsequent attempt failed the WGC source gate because WGC A stayed
blank. Namespace/lease coexistence and re-acquisition are therefore only partial:
the dual-lane topology and per-user DirectShow lease path were exercised, but clean
restart/release/rebind evidence is absent. The probe's no-active-`video_t`
hot-rebind assertion passed in the smoke, but its process log was not persisted as
a standalone artifact, so this criterion is partial. Rollback validation was not
run.

## Verdict and runbook

Verdict: `NOT_READY`; closure: `NOT_CLOSABLE`. The observed blockers are the
resource sampler deadline/cadence failure, non-progress under the boundary run,
cleanup failure on recovery, and intermittent blank WGC source. Do not weaken the
checks or add infinite retries. A follow-up should first repair or separately
validate the harness sampling/cleanup path, then rerun the same exact artifact and
retain process/lease logs alongside the raw traces. Until then, treat the memory
curve, recovery time, and target-load SLOs as qualification gaps.

For a rerun: verify the artifact hashes and a fresh runtime ID; use an isolated
visible WGC target; enable only `PULSAR_DUAL_LANE_ENABLED=1`; run
`scripts/probe-dual-lane.py --resource-mode dual_lane --trace --capture-window ...
--cef-workload --rtmp-receiver`; retain the trace, producer sidecar, receiver
diagnostics, timestamps, and cleanup result; stop on a failed gate; and verify no
owned reader, mapping, or lease remains before declaring recovery.
