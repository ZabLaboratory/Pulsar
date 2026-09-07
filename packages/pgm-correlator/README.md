# @clodocapeo/pgm-correlator

Observes what Pulsar actually broadcasts (PGM) and what Orion's LSDP wire
says it intended, and correlates the two **by time** -- never by pixel.
Built for ZabLaboratory/Pulsar#230 (ADR-BLUE-012 R6 §16.1, invariants B17,
B22).

## Why time, not pixels

This observer does not receive scene identity inside arbitrary recorded
pixels. Pulsar 3.0.0 has separate correlated runtime telemetry, but that is
not automatically embedded into the recordings consumed by this package. Short of a
Solar burn-in (out of scope here, would be a Solar change), no
`correlation_id` is recoverable from a recorded frame. This package instead
observes two independent, real timelines and pairs them:

- **State events** -- Orion's `correlation_id` / `render_revision` /
  `scene_digest` / `runtime_instance_id` / `target`, read from a **passive,
  read-only** WS viewer connection to `/api/v1/show/stream.lsdp`. Zero
  modification of Orion: this is the connection any show-token holder can
  already open (`internal/ws/server_test.go::TestWS_ViewerCannotInput`
  enforces the read-only part server-side).
- **Visual events** -- real scene-change timestamps extracted from a real
  recorded file (via `@clodocapeo/pulsar-client`'s `RecordNamespace`,
  `StartRecord`/`StopRecord`) using `ffprobe`/`ffmpeg`.

## What "correlated" means here

`correlate()` never forces a 1:1 pairing. A record falls into exactly one
of three categories:

| Category | Meaning | Is it an error? |
|---|---|---|
| `matched` | A state event and a visual event paired within the derived threshold. | The expected common case. |
| `state_without_visual` | A delta/snapshot with no detected visual effect. | **No** -- expected when the patch didn't change anything rendered (identical value, off-screen leaf, non-visual leaf). |
| `visual_without_state` | A visual change with no preceding identity frame. | **No** -- expected for a running animation, a playing video source, or a transition that isn't itself an LSDP delta. |

Counting the last two as failures would make the artifact cry wolf on
every session and train readers to ignore it.

## The acceptance threshold is derived, not decreed

`correlate()` runs two passes. The first gathers a sample of candidate
pairings under a generous ceiling window; the second derives the real
acceptance threshold as a percentile (default: p95) of that sample's
latency distribution, and uses it for the final pairing. When the sample
is too small (`< minSampleForDerivation`, default 5) it falls back to a
documented constant and says so explicitly in
`ThresholdDerivation.fallbackUsed` / `fallbackReasonIfUsed` -- a threshold
is never silently substituted without recording that it was.

The artifact also documents, by name, the drift sources the threshold has
to absorb: encoding latency, clock skew between the observer's and the
recorder's clocks, and scene-detection sensitivity (see
`KNOWN_DRIFT_SOURCES` in `src/artifact.ts`).

## What this package proves in this repository's CI, and what it doesn't

- `orion-observer.test.ts` runs OrionObserver against a **real local
  WebSocket server** speaking the documented LSDP envelope schema. It
  proves the parser reads the wire format correctly. It is not, and does
  not claim to be, a live Orion.
- `pgm-extractor.test.ts` runs the **real** `ffmpeg`/`ffprobe` binaries
  against a **real**, freshly generated fixture video with one genuine
  scene cut. It proves the extraction command and parsing work on real
  encoded bytes. It is not a claim about a live antenna feed.
- `correlator.test.ts` / `artifact.test.ts` are deterministic unit tests
  over synthetic timelines and the filesystem.

**What is not proven here**: a live, end-to-end correlation against a real
Orion instance emitting an authentic `correlation_id`, recorded by a real
Pulsar broadcasting a real Blue scene. That requires Orion running (Postgres
via `docker compose`) plus Solar rendering plus a live Pulsar capture --
infrastructure unavailable in the sandbox this package was built in
(`docker` absent; no reachable local Orion). This is declared, not hidden,
in the PR for ZabLaboratory/Pulsar#230, in the same spirit as Pulsar#190's
accepted runtime-evidence debt for #166. `recordCorrelatedSession()` is the
real, live-capable orchestration path for whoever has that environment.

## Usage sketch

```ts
import { PulsarClient } from "@clodocapeo/pulsar-client";
import { recordCorrelatedSession } from "@clodocapeo/pgm-correlator";

const pulsar = new PulsarClient();
await pulsar.connect({
  url: process.env.PULSAR_WS_URL,
  password: process.env.PULSAR_WS_PASSWORD,
});

const { artifact, paths } = await recordCorrelatedSession({
  pulsar,
  orion: { url: "wss://orion.example/api/v1/show/stream.lsdp?token=<show-token>" },
  outputBaseDir: "_live_records",
  durationMs: 30_000,
});

console.log(artifact.counts, paths.summaryPath);
```


## 3.0.0 integration status

This package keeps its own version; it is not one of the three Pulsar 3.0.0
packages published by the standard release job. Its workspace client
dependency is aligned with the current SDK.

The limitations above describe the original #230 evidence, not a claim that
the current release environment has no Orion or Docker. No live cross-service
validation is inferred from updating the dependency or rerunning portable
tests. A real session needs authorized endpoints/credentials, exact clock
assumptions, an explicit output directory and proper recorder/client cleanup.

The usage block is an orchestration sketch; the host must validate its
environment variables, protect the show token and disconnect the client in
its own finally/cleanup path. Temporal matches express proximity under the
reported threshold, not causal or pixel-identity proof.
