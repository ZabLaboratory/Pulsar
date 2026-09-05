# Preview audio / AFV descope contract

Status: `DESCOPED`

This document is the versioned contract decision for
`ZabLaboratory/Pulsar#252` (`PUL-DL-11`). It is intentionally a descope, not
an AFV implementation. It records what the current producer, gateway and
accessible consumers can safely rely on and what they must not infer.

| Field | Value |
| --- | --- |
| Descope contract | `pulsar.preview-audio-afv.descope.v1` |
| ADR | `ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane-20260828` |
| Existing wire contract | `pulsar.program-audio.v1` via `pulsar:GetProgramAudioRoute` |
| Decision | Preview audio and AFV are unsupported in r2 |
| Default | One common process-wide `ProgramAudio` route; no audio role permutation on video Cut |
| Implementation | None; this change is documentation and contract-test evidence only |
| Rollback | Close/revert this documentation-only change; runtime rollback remains the ADR dual-lane compatibility path |

## Decision and authority

The authoritative runtime read is `GetProgramAudioRoute` (vendor `pulsar`,
request `GetProgramAudioRoute`). A conforming r2 response has
`schema_version=1`, `route_id=program-common`, `route_name=ProgramAudio`,
`scope=program`, `cut_audio_policy=common-program-route-unchanged`, and both
`preview_audio_supported=false` and `afv_supported=false`.

The common Program route is the only audio contract in this decision. The
frontend captures the process-wide libobs `audio_t` once at setup and binds
each frontend-owned AAC encoder to it. A video `Cut` changes only the dual-lane
video roots and role mapping at a frame boundary. It does not select an audio
source, audio bus, output slot, or mix from the Preview scene.

A consumer that cannot read this response, sees a transport `error`, sees
`stable=false`, or receives a malformed/missing required flag must fail closed:
it may report audio/route state as unknown or unavailable, but it must not
claim Preview audio or AFV and must not guess a route from the video lane.
Missing data is not proof of support.

This descope is the compatible strategy because an AFV implementation would
introduce a second mutable state machine, a new producer/gateway command
surface, and a new rollback boundary. None of those are in ADR r2 and no
independent Preview audio producer is currently exposed.

## Producer, gateway and consumer matrix

| Boundary | Current contract / behavior | Required consumer interpretation | Evidence |
| --- | --- | --- | --- |
| Frontend producer | `programAudio = obs_get_audio()` once in `setup()`; every frontend AAC encoder uses `obs_encoder_set_audio(enc, programAudio)` | Audio identity is process-wide and not derived from `OnAir`/`Preview` lane | `plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp`; `include/pulsar-program-audio.h` |
| Video Cut producer path | `TakeCommitted` swaps video roots/roles only; the Program audio route is unchanged | A successful video Cut is never an audio Cut or AFV trigger | frontend `sceneSwitch*` and `TakeCommitted` path |
| Gateway | `pulsar-multi-stream` registers `GetProgramAudioRoute`; it reads output audio identity/flags and encoder-fed mixer indexes | `audio_matches_route=true` is required for an audio-capable output; mismatches are visible as `stable=false`/`route_error` | `plugins/pulsar-multi-stream/src/plugin-main.cpp` |
| Program/Preview returns | `PulsarProgramReturn` and `PulsarPreviewReturn` are video-only and report `audio_supported=false` | Do not treat a return surface as a second audio bus | gateway output matrix and wire response |
| Encoded outputs | `stream`, `record`, and `replay` may carry the common route; each present audio output reports its identity and slots | `audio_identity` must match the route; track is `mixer_index + 1`, never an output slot | gateway observer and `GetProgramAudioRoute` |
| Typed client | `client.audio.programRoute()` decodes the existing response, including `previewAudioSupported` and `afvSupported` | A client must expose the explicit false values and never synthesize an AFV command | `packages/pulsar-client/src/{audio,wire,types}.ts` |
| Probe/test consumer | `scripts/probe-program-audio.py` samples the route after Cuts and checks identity/PTS/isolation | Runtime evidence can prove common Program audio only; it cannot prove AFV | probe and #245 exact-head evidence |
| External downstream consumers | Repositories outside Pulsar are not inspectable in this work unit | Compatibility is not assumed; they must consume the documented flags and treat missing/unknown data as unsupported | explicit inspection limit |

## Program / Preview behavior matrix

| Operation or surface | Video behavior | Audio behavior in `descope.v1` | Required result |
| --- | --- | --- | --- |
| `Prepare` Preview | Prepares the Preview video lane under the r2 revision rules | No audio route is created or changed | `ProgramAudio` snapshot unchanged |
| `Take` / `Cut` | Atomically swaps logical video roles at the committed frame | No audio permutation, crossfade, duck, or follow operation | Existing common route remains bound |
| Mutate Preview after `TakeCommitted` | Mutates only the newly available Preview video lane | Must not touch Program audio sources, identities, encoders, or PTS counters | Route identity/output key unchanged |
| `ProgramView` / `ProgramReturn` | Stable Program video surface | Program return is video-only; encoded Program outputs use common audio | `audio_supported=false` for ProgramReturn; matching route for encoded output |
| `PreviewView` / `PreviewReturn` | Stable Preview video surface | No independent Preview mix exists | `audio_supported=false` for PreviewReturn; no AFV claim |
| `stream` / `record` / `replay` | Output lifecycle is independent of video role swap | If audio-capable and present, consumes common `ProgramAudio` | `audio_matches_route=true`; otherwise fail closed |
| AFV request, selector, or route | No r2 command or route exists | Unsupported; no implicit fallback to Preview or OnAir audio | Reject at the owning future API boundary; no state mutation |

## Normative invariants

The following invariants are the acceptance boundary for this descope. A
violation is not repaired by relabelling a route or by selecting another
output slot.

1. **D1 — Single route identity.** Within one runtime process,
   `ProgramAudio` is the `audio_t` captured at setup. Every frontend-owned
   audio encoder that is present and every audio-capable output that is
   reported must read back that identity.
2. **D2 — Video-only role swap.** `TakeCommitted` may change the video role map
   and video roots only. It must not call an audio-route setter, replace an
   audio encoder binding, or derive audio from the selected Preview scene.
3. **D3 — No implicit permutation.** Across two valid snapshots bracketing a
   committed Cut, the route id, route identity, output audio identities,
   output/track/encoder mapping, and common source identities remain equal for
   outputs present in both snapshots. Output start/stop is an explicit
   lifecycle change and is not evidence of AFV.
4. **D4 — Preview isolation.** A Preview-video mutation after
   `TakeCommitted` leaves the complete common-route key unchanged. The route
   cannot be inferred from `OnAir`, `Preview`, a lane id, or channel 0 (the
   mutable video root).
5. **D5 — Explicit unsupported state.** `preview_audio_supported` and
   `afv_supported` are present and `false`. `ProgramReturn` and `PreviewReturn`
   report `audio_supported=false`. No consumer may turn absence into `true` or
   infer support from an audio-capable output elsewhere.
6. **D6 — Evidence is flow-aware.** `observed=true` is claimed only after a
   real callback has delivered frames/PTS. `pts_regressions=0` and
   `pts_monotone=true` are required before claiming monotone observed audio;
   a wiring snapshot with `observed=false` is not continuity proof.
7. **D7 — No hidden AFV state.** There is no `SetPreviewAudio`, `SetAFV`,
   audio-role revision, AFV event, or AFV retry/idempotency state in r2. A
   future API must use a new version and must not overload `Take`.
8. **D8 — Fail closed.** A route mismatch, changed process audio identity,
   absent encoder, missing callback frames, malformed response, or transport
   failure cannot trigger an audio fallback or video-lane-derived selection.
   The current video mapping remains the last committed mapping until the
   owning runtime applies its bounded rollback policy.

## Loss, recovery and observability

The route observer distinguishes wiring from flow. These states are
contract-relevant:

| Condition | Observable signal | Required action | Recovery gate |
| --- | --- | --- | --- |
| Initial observer warm-up | `observed=false` and `route_error="ProgramAudio callback has not observed a frame yet"` | Do not call this a loss or a pass; retain the route snapshot and wait for a bounded sample | Later snapshot has `observed=true`, positive samples/frames and monotone PTS |
| No frontend audio encoder | `route_error="no frontend-owned audio encoder is bound to ProgramAudio"` | Keep AFV unsupported; do not manufacture a track or rebind to Preview | Restart/configure the owning output, then re-read the route |
| Output identity mismatch | `stable=false`, `route_error` starts `ProgramAudio mismatch:` | Stop claiming common-route continuity; do not select another slot or scene | All present audio-capable outputs match the captured identity and `stable=true` |
| Audio bus unavailable or transport failure | Top-level `error` or no valid response | Preserve the current video role map, record the error with runtime/session/command correlation, and fail closed | Fresh setup/reconnect returns the complete required schema and flags false |
| PTS regression / non-monotone flow | Track `pts_regressions > 0` or `pts_monotone=false` | Treat audio continuity as invalid; no AFV fallback and no silent retry loop | New bounded observation has real frames, zero regressions and monotone PTS |
| Process audio identity changes | `audio_identity` differs from the setup identity | Fail stopped for audio claims; never hot-rebind an active encoder | Restart the runtime and re-run the common-route probe |

Every loss/recovery record should include `runtime_instance_id`, session id,
route id, audio identity when available, output/track identity, command or
Take id when applicable, monotonic observation time, `route_error`/`error`, and
the resulting `stable`, `observed`, and PTS fields. These signals are
diagnostic evidence; they do not create AFV support.

## Rollout and rollback

This change has no runtime or wire mutation. Rollout is therefore:

1. Review and merge the documentation/test change against the exact ADR r2
   revision.
2. Keep the existing `GetProgramAudioRoute` response and client behavior
   unchanged. Consumers continue to see explicit false Preview/AFV flags.
3. If product requirements later demand AFV, first approve a new ADR/revision
   and additive capability contract. Then implement producer, gateway and
   client support in that order with default `false`; only after read-back and
   loss/recovery evidence may a consumer opt in.

Before merge, rollback is closing the draft PR. After merge, revert the signed
documentation commit. Neither action changes a running process.

If the underlying dual-lane runtime must be rolled back for an invariant
violation, use the existing bounded runbook: stop new Takes, preserve the
committed Program at a frame boundary, mark pending commands cancelled or
expired, and restart with `PULSAR_DISABLE_DUAL_LANE=1` as the explicitly
labelled compatibility/degraded path. Never rebind an active `video_t` and
never use rollback to imply Preview audio/AFV support. Preserve the trace,
route errors, revisions and rollback reason.

## Validation contract

The companion test `test_252_preview_audio_descope_contract.py` is a static
cross-boundary guard. It checks, on one revision, that:

- the versioned descope and all matrix/invariant/recovery/rollback sections
  exist;
- the frontend producer captures one process-wide audio bus and the Cut path
  does not expose an audio setter or AFV command;
- the gateway registers `GetProgramAudioRoute` and emits the explicit false
  flags, route mismatch and flow-aware PTS signals;
- the typed client and real probe consume the same response fields; and
- the CMake/header wiring keeps the producer/gateway contract reachable.

The static check is necessary but not sufficient for runtime acceptance. A
representative runtime smoke campaign remains the already-validated #245
probe against an exact built artifact: at least 100 committed Cuts per codec,
stable route/output/source keys, Preview-video mutation isolation, and
monotone observed AAC PTS. That campaign proves common Program audio only.
It deliberately cannot turn this descope into AFV support.

## Future AFV entry criteria (out of scope)

An AFV proposal may not reuse this descope as an implementation shortcut. It
must provide a new versioned producer/gateway/client contract with at least:

- independent Preview and Program audio roots and explicit ownership;
- revisioned, idempotent, ordered commands separate from video `Take`;
- a Program/Preview/AFV state matrix including pending, committed, abort and
  stale-command behavior;
- PTS continuity, loss/recovery and bounded retry semantics for every route;
- output/encoder rebinding rules that never hot-rebind an active encoder;
- observability correlating runtime, command, revisions, route identities and
  PTS; and
- independent integration evidence from producer, gateway and every
  inspectable consumer, plus a rollback that returns to this exact common-route
  contract.

Until those gates are approved and proven, the only valid r2 statement is:
`preview_audio_supported=false; afv_supported=false`.
