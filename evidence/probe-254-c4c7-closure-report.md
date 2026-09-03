# CLOSURE_REPORT — Probe-254 c4c7 continuation

Exact candidate `c4c7bbc5c9cba5f20cd583693617562108869900`; CI `33783285508` + compliance `33783285497` SUCCESS; artifact `9904790004`, 290020139 B, SHA-256 `c2c019904f07cf076626b2dde63bec1fda543fe4ec773b8c17299180cc75c38d`; executable SHA-256 `38e5f4d702cdb70b93c9d6f9f6f3f20954d5bdbf15d56211d3b97e266d1e5b62`.

End-to-end x264/NVENC dual-lane campaigns were attempted with the required 100 warmup + 100 measured and stopped at the mandatory WGC gate: source A remained black for 20 s (`distinct=1`, `nonblack_ratio=0.0`). No SLO/200-Take evidence is promoted. This is a reproducible host capture blocker affecting both codecs.

Independent c4c7 evidence: rollback/no-hot-rebind/lease release PASS for x264 and NVENC; replay PASS; capability contract PASS 54/137; Program audio PASS for both codecs with AAC 48 kHz and monotone PTS; NVENC quality A/B PASS; clean process shutdown PASS. Resource growth/saturation, AC-12a/12b, and AC-13 remain UNPROVEN for this candidate. Stale/squatted ConsumerActive is contract/static-covered, not hostile-runtime-injected.

Full paths, commands, evidence hashes and criteria matrix are in `probe-254-c4c7-final-report.md`. Candidate is not promotable for end-to-end issue SLO on this host; no superseded e028 evidence reused.
