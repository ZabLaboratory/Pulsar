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
| `encoded_first_packet` | `EncoderOutput` / `encoder_callback` | First encoded video packet handed to the encoder-output callback (`packet_index=0`), before any network or RTMP receiver. | AC-12, p95 compared with `<=15 ms` baseline |
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
  "capture_paths": ["encoder_input_raw", "directshow_return", "encoded_first_packet"],
  "source_types": ["window_capture", "browser_source"],
  "resource_reference": {
    "extra_frame_render_ms": 0.091,
    "extra_resident_bytes": 3130000
  },
  "build_revision": "<40-lowercase-hex-candidate-SHA>",
  "command_line": "<redacted reproducible command>",
  "hardware": {"host": "<host>", "gpu": "<adapter/driver>"},
  "producer_topology": "dual_lane_ab",
  "producer_count": 2,
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

If the atomic queue rejects a reserved Take, the producer emits a terminal
`TakeAborted` event with `reason=queue_rejected` and the
`last_committed_frame_id`/`last_committed_pts_ns` pair. No `TakeAccepted` is
emitted for that candidate: the acceptance timestamp is assigned only after
the queue primitive returns success. The pair identifies the last committed
frame before the rejected reservation and makes the terminal path observable
without pretending that a commit occurred. Accepted and committed events are
placed in a FIFO writer queue; disk I/O runs on its worker and cannot inflate
the acceptance-to-frame latency.

If the frame-boundary callback observes a frame ID or PTS below the last
telemetry commit, the physical swap has already happened: the callback runs
after `obs_view_queue_atomic_swap`. The producer therefore reconciles its
state to the actual post-swap role map and emits a `TakeCommitted` carrying the
exact observed frame/PTS and incremented revisions; it never fabricates a
monotone boundary and never emits `TakeAborted` or claims a physical rollback.
It then emits one out-of-contract `integrity_fault` process record with the
candidate and prior frame/PTS and latches a fail-stop. The parser rejects any
trace containing that record, and subsequent Takes are rejected until a runtime
restart. This revision exposes no explicit reconciliation API; adding one would
require a separate contract decision. Duplicate callbacks for the accepted Take
are ignored; the physical role map remains the one reported by the callback.

The pre-network encoded callback uses the same correlation fields, with
`boundary=encoded_first_packet`, `surface=EncoderOutput`,
`consumer=encoder_callback`, and `packet_index=0`. It still carries the frame
ID/PTS associated with the packet; the script refuses a packet-only timestamp
that cannot be correlated to the Take frame boundary. RTMP receiver ingress is
an external Probe measurement and must not be labelled as this boundary.

Resource samples use `sample_mode=reference` and `sample_mode=dual_lane` and
record `frame_render_ms`, `resident_bytes`, `process_cpu_percent`,
`host_gpu_percent`, `callback_backlog_estimate`, and
`encoder_utilization_percent`, plus strict `encoder_active` and
`encoder_family` fields read from the actual bound encoder at sample time. The report
computes actual deltas from the two sample sets and shows them beside the
known `+0.091 ms/frame` and `+3.13 MB` references. It never declares runtime
capacity from the reference alone. Samples with `encoder_active=false` remain
diagnostic and cannot satisfy the AC-13 minimum.

The `encoder_active` and `encoder_family` fields are optional for backward
parsing of older traces; when either is absent or the family is not `nvenc`,
the sample is treated as ineligible for acceptance. This
keeps historical evidence inspectable without allowing it to satisfy the
new active-encoder resource gate.

Every resource sample also carries `measurement_phase`, the exact candidate
`build_revision`, `hardware.host`/`hardware.gpu`, and the explicit
`producer_topology`/`producer_count` for that phase. The parser rejects a
sample whose phase/topology pair is inconsistent, or whose build, hardware,
or runtime identity differs from the session, so reference and dual-lane
measurements cannot be silently mixed.

The reference driver creates one independent `window_capture` and one
independent `browser_source` producer on public scene A only. The dual-lane
driver creates two independent producers of each requested kind, one pair in
each public A/B scene. It verifies registration, settings, enabled scene
ownership, and a decoded screenshot from each source after A is Program and B
is Preview. The frontend's `Default` bootstrap sources and workload flags are
not accepted as proof of the A/B topology.

## Reproducible execution

Run each codec as an independent campaign on the exact binary and host. The
producer must complete at least 100 warm-up Takes followed by at least 100
measured Takes in the same runtime session for each required boundary. The
standard dual-lane probe executes 200 committed Takes for `--takes 100`; the
parser excludes the first 100 from latency percentiles and reports both
observed counts. Capture the source/binary revision,
redacted command line, resolution/FPS, adapter/driver, and the WGC/CEF/NVENC
flags in the session record.

```powershell
# Run the runtime producer/instrumentation for x264 and save x264.jsonl.  This
# campaign has no resource phase; AC-13 is NOT_APPLICABLE for x264.
# Repeat with PULSAR_VIDEO_ENCODER=nvenc and save nvenc.jsonl; only the NVENC
# campaign performs the reference/dual resource comparison while recording.
# The probe itself keeps the ProgramReturn DirectShow reader open; the reader
# and encoder-output callback write observations using the same runtime/session
# IDs and monotonic clock. RTMP receiver timing is an external Probe
# measurement and must not be merged from wall-clock logs.

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
  --build-revision <candidate-sha> --capture-window <visible-title:class:exe> `
  --cef-workload --resource-mode reference --resource-only
python scripts/probe-dual-lane.py --exe <pulsar.exe> --encoder nvenc --takes 100 `
  --trace artifacts/246/nvenc.jsonl --runtime-id runtime-nvenc-001 `
  --build-revision <candidate-sha> --capture-window <visible-title:class:exe> `
  --cef-workload --trace-append --resource-mode dual_lane
```

`reference` is an explicit legacy single-canvas run; it does not emit Take
events. It nevertheless starts a real local NVENC recording, waits for
`OUTPUT_STARTED`, keeps the encoder active while collecting resource samples,
then stops and verifies that recording before returning. An inactive or
missing encoder attestation cannot satisfy AC-13. Both invocations require a
real visible WGC target and the local CEF workload. The driver sets
`PULSAR_TRACE_EXTERNAL_LANE_WORKLOAD=1`, so the
frontend suppresses its `Default` bootstrap WGC/CEF sources and the probe alone
creates one producer pair on A for `reference`, or two independent pairs on A/B
for `dual_lane`. The second invocation appends its real scene-switch events and
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
