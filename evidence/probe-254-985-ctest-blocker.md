# AGENT_CHECKPOINT — Probe-254-Final-Rerun

- role/display/thread: `probe` / `Probe-254-Final-Rerun` / `probe-254-final-rerun`
- work unit: `ZabLaboratory/Pulsar#254`
- candidate: `985ed89916f92e1e1103077e84b1fa76a8da541d`
- artifact: `pulsar-rundir` id `9902952449`, 290140834 bytes, SHA-256 `3098bba95b18ab3c77646e8250fa7eb247519ea0b9597bc283d32dbe71383d63`
- executable SHA-256: `71AF6A548D2AC6CA7786C9C23C23C4F03202581190D1725DF88A1C36BBCFE179`
- CI: pipeline `33778433025`; build/export/CEF jobs succeeded, exact offline CTest is failed.

## Gate result

No hardware campaign was executed on this candidate because the required exact CTest gate is red.

`pulsar-offline-probes` failed in `probe-output-effect.py` (93% passed, 1/15 failed). In positive control E, `StartRecord` succeeded and recording became active, then `StopRecord` returned error `702`: the request was accepted but did not settle within 2500 ms; `outputActive` remained true and no completed path was available in that response.

Expected: an active recording stops and settles inactive within the contract, with the shared probe suite continuing.

Observed: the active output remains true after the bounded stop wait, causing the effect probe to abort before the shared suite.

Scope: exact candidate `985ed899`; this blocks promotion and all dual-lane-only hardware evidence for this candidate. No prior candidate evidence is promoted.

Next action: await a newer exact head and matching artifact with green downstream CTest; re-verify provenance before running the final x264/NVENC campaign.
