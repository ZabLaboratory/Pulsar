# #246 latency and capacity evidence

`scripts/probe-take-latency.py` is the strict, offline half of the #246
probe. It consumes a JSONL trace produced by the runtime instrumentation and
does not manufacture observations from log arrival order. The script is
deliberately usable without OBS/libobs so malformed traces and boundary
confusion can be regression-tested on every host.

## Normative boundaries

All timestamps below are `uint64` nanoseconds from the runtime's one
monotonic clock (`clock_domain=monotonic_ns`). Percentiles use linear
interpolation over the sorted sample values; the rank is `(n - 1) * q`.

| Trace boundary | Surface/consumer | Meaning | Acceptance |
| --- | --- | --- | --- |
| `encoder_input_raw` | `ProgramView` / `encoder_input` | First valid Program frame received at the encoder/raw input. | AC-07, p95 `<=50 ms` |
| `directshow_return` | `ProgramReturn` / `DirectShow` | First valid Program frame observed by the DirectShow return consumer. | AC-08, p95 `<=75 ms` |
| `rtmp_first_packet` | `RTMP` / `rtmp` | First packet on the RTMP output (`packet_index=0`). | AC-12, p95 compared with `<=15 ms` baseline |
| `decoded_first_frame` | `RTMP` / `decoder` | First decoded frame, diagnostic only. | No SLO |
| `antenna_first_frame` | `Antenna` / `antenna` | First antenna/player frame, diagnostic only. | No SLO |

The latency clock starts at
`TakeAccepted.observed_at_monotonic_ns`. A valid observation is admitted only
when it is post-commit, has the same `runtime_instance_id`, `command_id`,
`intent_id`, and post-commit `revisions`, and has frame ID/PTS greater than or
equal to the `TakeCommitted` frame boundary. Frames seen continuously before
the commit are retained as diagnostics and excluded from the percentile.

The DirectShow result is never substituted with the raw result. RTMP and
decoded/player results are also separate, so a decoder delay cannot be
misreported as a Cut failure or hidden by a fast first packet.

## Trace contract

One file describes one runtime/session. The first JSONL record is the session
metadata:

```json
{
  "record_type": "session",
  "schema": "pulsar.take-latency.v1",
  "runtime_instance_id": "runtime-001",
  "session_id": "latency-nvenc-20260829",
  "codec": "nvenc",
  "warmup_takes": 100,
  "video": {"width": 1920, "height": 1080, "fps_num": 60, "fps_den": 1},
  "workload": {"wgc": true, "cef": true, "nvenc": true},
  "capture_paths": ["encoder_input_raw", "directshow_return", "rtmp_first_packet"],
  "source_types": ["window_capture", "browser_source"],
  "resource_reference": {
    "extra_frame_render_ms": 0.091,
    "extra_resident_bytes": 3130000
  },
  "build_revision": "0123456789abcdef0123456789abcdef01234567",
  "command_line": "<redacted reproducible command>",
  "hardware": {"host": "<host>", "gpu": "<adapter/driver>"},
  "evidence_kind": "runtime"
}
```

For every accepted Take, the runtime emits the exact validated
`pulsar.scene-switch.v1` event envelope in an event record. `TakeAccepted` and
`TakeCommitted` must use the same command/intent/runtime identifiers; the
commit's `previous_revisions` must equal the acceptance's `revisions` and its
frame ID/PTS are the atomic frame boundary.

An observation record has the following required fields (plus optional
`frame_hash`, `packet_index`, and `notes`):

```json
{
  "record_type": "observation",
  "boundary": "directshow_return",
  "clock_domain": "monotonic_ns",
  "runtime_instance_id": "runtime-001",
  "command_id": "take-001",
  "intent_id": "intent-001",
  "take_command_id": "take-001",
  "revisions": {"program": 1, "preview": 1, "role_map": 1},
  "frame_id": 501,
  "pts_ns": 8333333333,
  "observed_at_monotonic_ns": 4240000000,
  "valid": true,
  "program_frame": true,
  "surface": "ProgramReturn",
  "consumer": "DirectShow"
}
```

The first RTMP packet uses the same correlation fields, with
`boundary=rtmp_first_packet`, `surface=RTMP`, `consumer=rtmp`, and
`packet_index=0`. It still carries the frame ID/PTS associated with the packet;
the script refuses a packet-only timestamp that cannot be correlated to the
Take frame boundary.

Resource samples use `sample_mode=reference` and `sample_mode=dual_lane` and
record process/render, WGC, CEF, NVENC, and encoder queue metrics. The report
computes actual deltas from the two sample sets and shows them beside the
known `+0.091 ms/frame` and `+3.13 MB` references. It never declares runtime
capacity from the reference alone.

## Reproducible execution

Run each codec as an independent campaign on the exact binary and host. The
producer must complete at least 100 warm-up Takes and retain at least 100
measured Takes for each required boundary. Capture the source/binary revision,
redacted command line, resolution/FPS, adapter/driver, and the WGC/CEF/NVENC
flags in the session record.

```powershell
# Run the runtime producer/instrumentation for x264 and save x264.jsonl.
# Repeat with PULSAR_VIDEO_ENCODER=nvenc and save nvenc.jsonl.
# The DirectShow reader and RTMP packet hook must write observations using the
# same runtime/session IDs and monotonic clock; do not merge wall-clock logs.

python scripts/probe-take-latency.py `
  --trace artifacts/246/x264.jsonl artifacts/246/nvenc.jsonl `
  --output artifacts/246/latency-report.json
```

The default gate is 100 measured Takes, 100 warm-up Takes, and 10 resource
samples per resource mode. Use smaller thresholds only in unit tests; fixture
campaigns are reported as `FIXTURE_ONLY` and can never be a runtime pass.

The runtime resource producer samples OBS's platform process counters and the
driver's `nvidia-smi` counters. To measure both topologies in one correlated
trace, run a short reference process first, then append the dual-lane campaign
with the same runtime ID:

```powershell
python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder nvenc `
  --trace artifacts/246/nvenc.jsonl --runtime-id runtime-nvenc-001 `
  --resource-mode reference --resource-only
python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder nvenc --takes 100 `
  --trace artifacts/246/nvenc.jsonl --runtime-id runtime-nvenc-001 `
  --trace-append --resource-mode dual_lane
```

`reference` is an explicit legacy single-canvas run; it does not emit Take
events. The second invocation appends its real scene-switch events and
dual-lane observations without adding a second session record. If the platform
or GPU counters are unavailable, the native producer leaves the resource set
incomplete and the parser reports `UNPROVEN` rather than filling zeroes.

Exit codes are:

- `0`: all required runtime criteria pass;
- `1`: malformed/correlated evidence or an SLO failure;
- `3`: incomplete/partial evidence (`UNPROVEN`), missing host capability, or
  fixture-only input.

## Current hand-offs

The legacy obs-websocket path in the base build only logs physical pointers
and `TakeCommitted(frame_id, pts_ns)`. It does not carry the
`pulsar.scene-switch.v1` command/intent/revision envelope required by AC-11,
so it cannot produce an admissible #246 trace without the Conduit-owned event
adapter. This is a `CONDUIT_REQUIRED` hand-off, not a reason to invent IDs in
the probe.

The DirectShow return reader, exact WGC+CEF+NVENC host, and a built binary are
runtime dependencies. If they are unavailable, report `KEEPER_REQUIRED` with
the host/build/reader evidence; do not call a parser fixture a capacity or
SLO result.
