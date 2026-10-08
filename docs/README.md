# Pulsar documentation — 3.0.0

Start with the [product README](../README.md). This index separates current
operational guides, component contracts, historical measurements and approved
decisions so an older study is not mistaken for today's product behavior.

## Current guides

| Document | Use it for |
|---|---|
| [Architecture](ARCHITECTURE.md) | Process/module topology, hot A/B producers, stable Program/Preview views, media flow, ownership and lifecycle. |
| [Protocol](PROTOCOL.md) | obs-websocket baseline, all three vendors, capabilities, errors, environment and tracing controls. |
| [Development](DEVELOPMENT.md) | Toolchain, patch replay, build modes, TypeScript/native gates and packaging. |
| [Embedding](PRISM-EMBEDDING.md) | Manual and Node integration, private runtime directories, authentication, distribution and shutdown. |
| [libobs changes](LIBOBS-CHANGES.md) | Fork lineage, five integrated changes, all 52 patches, defaults, experiments and rollback. |
| [Changelog](../CHANGELOG.md) / [3.0.0 notes](releases/3.0.0.md) | Release scope from the v2.0.0b commit and full commit inventory. |
| [Distribution invariants](../LICENSE-INVARIANTS.md) | Maintainer's host/native separation policy; not a legal certification. |
| [Consumer audit](../CONSUMER-AUDIT.md) | Adaptable source/import checks and the limits of static fingerprints. |

## SDKs and packages

| Package | Documentation |
|---|---|
| TypeScript client | [API, events, errors and examples](../packages/pulsar-client/README.md) |
| Light Windows wrapper | [Installation, lifecycle, env and limits](../packages/pulsar-bundle/README.md) |
| Full Windows wrapper | [CEF/full inventory, installation and lifecycle](../packages/pulsar-bundle-full/README.md) |
| PGM correlator | [Standalone correlation CLI and evidence boundary](../packages/pgm-correlator/README.md) |
| Capture/PGM compatibility | [Contract and hardware-gated compatibility harness](../packages/capture-pgm-compat/README.md) |

## Native components and assets

The [plugin inventory](../plugins/README.md) maps source directories to deployed
binaries. Component READMEs explain current responsibilities:

- [headless host](../plugins/pulsar-headless/README.md);
- [frontend controller](../plugins/pulsar-frontend-stub/README.md);
- [WebSocket fork](../plugins/pulsar-websocket/README.md);
- [browser/CEF fork](../plugins/pulsar-browser/README.md);
- [multi-stream and diagnostics](../plugins/pulsar-multi-stream/README.md);
- [legacy scene/capture vendor](../plugins/pulsar-scene-source/README.md);
- [output failure classifier](../plugins/pulsar-output-classify/README.md);
- [NVIDIA validated SDK loader](../plugins/pulsar-nv-secure-load/README.md).

See the [patch authoring guide](../patches/README.md) and
[Stinger asset guide](../scripts/assets/README.md) for their separate workflows.

## Wire and host contracts

- [Scene-switch v1](../scripts/contracts/scene_switch_v1/README.md):
  Prepare/Take/Abort envelope, events and compatibility.
- [Scene-control contract](../scripts/contracts/scene_control/README.md):
  scene description and authoring fixtures.
- [Schema comparison](../scripts/contracts/scene_switch_v1/schema-diff.md):
  historical compatibility record, not an alternative live schema.
- [T-bar UX/accessibility specification](issue-251-ux-accessibility.md):
  host UI requirements, not an implemented Pulsar desktop frontend.
- [Preview-audio / AFV descope](issue-252/preview-audio-afv-descope-v1.md):
  explicit unsupported capability; the common ProgramAudio route remains.

## Operational runbooks

| Task | Runbook |
|---|---|
| Cut and verify a release | [Release and propagation](runbooks/cut-a-release-and-propagate.md) |
| Diagnose output failure | [Failed go-live](runbooks/diagnose-a-failed-go-live.md) |
| Diagnose probe disconnect | [Offline probe failures](runbooks/offline-probe-suite-connection-refused.md) |
| Repair missing ATL toolchain | [ATL build failure](runbooks/atl-missing-build-failure.md) |
| Qualify dual-lane runtime / rollback | [Canary and rollback](runbooks/pulsar-dual-lane-canary.md) |
| Measure Take latency/capacity | [Trace and probe contract](runbooks/probe-take-latency.md) |
| Qualify optional GPU return | [Current D3D11 helper transport](runbooks/d3d11-return-transport.md) |
| Diagnose return activity | [DirectShow lease watcher](runbooks/directshow-lease-watcher.md) |
| Restore NVIDIA module strip | [nv-filters rollback](runbooks/nv-filters-rollback.md) |

## Measurement history

Read these in order when investigating why an optimization was or was not
promoted. Results are machine/workload-specific and are not automatically
re-established by a new release build.

1. [Lease fast-path plan](tuning/pulsar-253-lease-fastpath.md):
   original proposal, subsequently implemented by the watcher.
2. [First latency study](issue-253-latency-study.md):
   initial results with later-corrected decoded-content interpretation.
3. [Decoded-content audit](issue-253-decoded-audit.md):
   visible workload and first changed image corrections.
4. [NVENC chain study](issue-253-nvenc-chain-study.md):
   ready-drain/decode experiments; not promoted defaults.
5. [Native CPU readback result](issue-253-native-optimized.md):
   measured promoted optimization and qualified hardware/configuration scope.

Existing evidence indexes for [247](evidence/247/README.md),
[249](evidence/249/README.md) and [251](evidence/251/README.md) describe retained
historical artifacts. Their results and original paths are not rewritten
during a documentation refresh. New durable proof follows the repository's
current `evidence/<issue>/<role>/` convention.

## Architecture decisions — preserve approved revisions

ADRs retain the decisions and context of their approved revision. A current
guide can explain later implementation, but cannot silently amend an ADR.

- [001 — ATL and compliance](adr/001-build-atl-gate-and-ci-compliance.md)
- [002 — canvas-authored live test](adr/002-m8-canvas-authored-live-test.md)
- [003 — scene transitions](adr/003-blue-driven-obs-scene-transition.md)
- [004 — hardware encoders](adr/004-gpu-hardware-encoders.md)
- [005 — go-live diagnosability](adr/005-go-live-failure-diagnosability.md)
- [Dual-lane core](adr/ADR-PULSAR-DUAL-LANE-001.md)
- [Dual-lane Amendment 2 reference](adr/ADR-PULSAR-DUAL-LANE-001-Amendment-2-reference.md)

## Documentation review boundary

The 3.0.0 refresh reviews every repository-owned README and secondary guide
outside vendored dependencies, generated output and historical evidence.
It rewrites the obsolete single-canvas architecture; reconciles package/API,
build/packaging and module naming; documents the complete OBS stack; and
corrects operational rollback/lifecycle claims.

Approved ADR payloads, schema definitions/diffs and historical measurement
values remain intact. Study headers clarify their status. Component guides
are source descriptions, not evidence that every optional GPU/device path
was exercised on the release runner. Use the tag pipeline, downloaded assets
and workload-specific evidence for those claims.
