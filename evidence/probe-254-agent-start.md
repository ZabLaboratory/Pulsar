AGENT_START

Role: probe
Display name: Probe-254-Final-Rerun
Work unit: ZabLaboratory/Pulsar#254
Thread: probe-254-final-rerun
ADR: ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane-20260828
Revision: 883f4017c808f8cfef2973408f0f88bbb8e9b702

Scope: independent final QA campaign against the exact CI artifact pipeline 33771130027 / artifact 9909966658. Validate dual-lane-only x264 and NVENC, WGC+CEF gates, 100 warmup + 100 measured takes, correlated raw/DirectShow/RTMP metrics, monotonic frame/PTS, quality/audio, rollback/replay/capability, stop/recovery/lease/mapping/liveness/no hot-rebind, and clean shutdown. Separate issue criteria from global AC-12/AC-13.

Exclusions: no product-code edits, no merge, no deployment, no single-lane campaign.
