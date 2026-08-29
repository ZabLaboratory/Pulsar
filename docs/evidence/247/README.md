# #247 — exact runtime evidence

This directory preserves the compact, reproducible evidence produced by
**Probe-5** for the merged `pulsar.scene-switch.v1` implementation. It is a
QA record, not a release artifact or an external-delivery claim.

## Exact provenance

| Item | Value |
| --- | --- |
| Merged implementation | `main` `9b809d15eba5c7953a00b0c79aaab3c6ab0dc363` |
| Validated pre-squash tree | `248a2a20c1ccadc0bbff78925b3e7b938cd9e22e` (tree equal to `main`) |
| Actions run / build job | `33274722347` / `99159379156` |
| Artifact | `pulsar-rundir` / `9721252624` |
| Artifact SHA-256 | `1104db1c58948a2b98546b134b163ac52f6eb507e1156dbea79025f50ec1f7a5` |
| Extracted `pulsar.exe` SHA-256 | `B410F7F2FBE1E9CC0D4E44C86B67D94A78FD3B2D9D9AD923F2C870148777206A` |
| Trace SHA-256 | `50950226AB39D3A80DF559D409BF8F1632F0D84FB9885E7F980D3B83C7677B47` |

## Reproduction

Use the exact executable from the listed Actions artifact. The durable harness
is [`scripts/probe-247-runtime.py`](../../../scripts/probe-247-runtime.py).
It validates every v1 command response and `VendorEvent` payload with
`scripts/contracts/scene_switch_v1/schema.json` (Draft 2020-12), and fails if
it leaves a Pulsar process behind.

```powershell
python scripts/probe-247-runtime.py --repo . --exe <pulsar.exe> --cycles 100
python scripts/probe-247-runtime.py --repo . --exe <pulsar.exe> --cycles 0 --cache-pressure
python scripts/probe-247-runtime.py --repo . --exe <pulsar.exe> --cycles 1 --prepare-timeout
python scripts/probe-247-runtime.py --repo . --exe <pulsar.exe> --cycles 1 --abort-race
python scripts/probe-247-runtime.py --repo . --exe <pulsar.exe> --cycles 1 --freeze-race
```

`probe-dual-lane.py` supplies the separate standard-OBS 100-Take and strict
WGC+CEF producer proof. The validated run used a visible, animated local WGC
target and rejected blank frames; it observed WGC A/B and CEF A/B non-black
pixel variance and two producer instances of each kind.

[`trace-summary.json`](trace-summary.json) is a bounded, auditable derivative
of the retained 203-record x264 trace and vendor-campaign outcome. It records
the command/result matrix, exactly-one-commit evidence, stale/abort invariants,
sampled role/route/frame/PTS transitions, and strict WGC+CEF pixel evidence
without importing the temporary trace. A datum absent from retained output is
marked `NOT_RETAINED`, never reconstructed.

## Acceptance matrix

| Criterion | Exact observed result |
| --- | --- |
| Vendor lifecycle | `PrepareAccepted -> PreviewReady -> TakeAccepted -> TakeCommitted`; `PreviewReady` followed a real `previewVideo` frame. |
| Cycles / frame boundary | 100 cycles, 1,700 transmitted vendor calls; frame IDs and PTS monotone; roles swap and Program remains distinct. |
| Replay / stale / malformed | Exact duplicate/replay, conflicting reuse, stale revision/server sequence, wrong runtime, NUL, uint64 overflow, scientific numeric, invalid/cross-field and second Take all exercised. |
| Exactly one terminal result | One `TakeCommitted` per accepted intent; post-commit exact retry returns the original `TakeAccepted`. |
| Race / timeout / cache | `PREVIEW_FROZEN` mutation race, one-terminal abort race, one-shot timeout, and separate 4096-entry fail-closed cache process pass. |
| Standard OBS / audio | x264 normal 100-Take probe passed with a 1920x1080@60 H.264 recording containing one AAC Program stream. |
| WGC + CEF | Strict no-blank screenshots: WGC A 797 / CEF A 843 / WGC B 790 / CEF B 846 distinct samples; all non-black ratio 1.000. |

## Measurement boundaries

Measured evidence is runtime raw ProgramView frame/PTS, source pixel variance,
route identities, and local encoded recording. **DirectShow, RTMP, decoded
downstream, and antenna are NOT_MEASURED.** Do not reinterpret raw or encoded
recording evidence as proof of any of those boundaries.
