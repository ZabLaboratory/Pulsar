# Changelog

All notable changes to Pulsar are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.4.0] - 2026-07-28

Minor: no `pulsar:*` request changed shape and no env var was renamed, but the
release **changes what the bundles contain and what `PULSAR_VIDEO_PRESET`
accepts on QSV**, so it is not a silent patch. `nv-filters` is gone from both
zips — `GetCapabilities.capabilities.filters` no longer lists its entries, so a
consumer holding a captured manifest fixture must re-capture (`npm run
manifest:capture` on Prism, ADR 027 RC 9). The QSV preset whitelist is now the
encoder's own `TU1..TU7` instead of the three aliases that were written to a key
`obs-qsv11` does not register.

**Consumers must upgrade — the source merge is not the mitigation.** NS1 is a
DLL-planting surface in the process that holds the Twitch stream key: it stays
open on every operator still running a ≤ 1.3.0 bundle, because the DLL is
removed at *packaging* time. Closing it means installing
`@clodocapeo/pulsar-bundle-full@1.4.0` (or `-bundle`), not merging #153.

### 🔒 Security

- **`nv-filters` dropped from both bundles (NS1).** The NVIDIA Audio/Video
  Effects plugin resolves its two SDKs by bare name at module load —
  `LoadLibrary(L"NVAudioEffects.dll")` / `nvcuda.dll`
  (`upstream/plugins/nv-filters/nvafx-load.h:305,307`) and
  `LoadLibrary(L"NVVideoEffects.dll")` / `NVCVImage.dll`
  (`nvvfx-load.h:689,690`), the directory coming from the environment Pulsar
  inherits from its embedder. Anything that can poison that environment gets
  code execution inside the process that holds the Twitch stream key, on every
  boot, whether or not the operator ever uses an NVIDIA effect. Same motive as
  the `obs-vst` strip, so the same remedy: `'nv-filters'` joins
  `$baseStrippedPlugins` in `scripts/package-win.ps1`, removing both the DLL
  and `data/obs-plugins/nv-filters/` from the light and full zips.

  No functional loss: the module is a self-contained set of optional filters,
  referenced nowhere else in the tree (`obs-filters`' NVAFX noise-suppression
  branch is behind `LIBNVAFX_ENABLED`, a define applied to the `nv-filters`
  target alone — `upstream/plugins/nv-filters/CMakeLists.txt:10,17` — so it is
  not compiled into the plugin we keep). `obs-nvenc` is untouched: NVENC ships
  as before.

### 🐛 Fixed

- **`PULSAR_VIDEO_PRESET` was a no-op on QSV, and unreadable there too.** The
  boot setter wrote the preset to the obs_data key `"preset"` for every family;
  `obs-qsv11` registers no such key — its knob is `target_usage`
  (`upstream/plugins/obs-qsv11/obs-qsv11.c:390`), values `TU1..TU7`
  (`QSV_Encoder.h:89`), default `TU4`. Every QSV spawn therefore encoded at TU4
  whatever the env var said, silently: nothing logged, and `GetVideoSettings`
  — which read only `"preset"` — answered `""`, so the lie was invisible from
  the wire as well.

  The preset set now carries the **property name** per family, and QSV's set is
  the encoder's own seven levels (`TU1..TU7`, default `TU4`) instead of the
  three aliases `speed`/`balanced`/`quality` written against the wrong key. That
  set is exactly what `capabilities.encoder_families` publishes from the same
  libobs property list (#148), so the boot whitelist and the manifest agree
  without either copying the other. Input matching is case-insensitive but the
  value applied — and reported — is the canonical spelling the manifest lists.
  `on_get_video_settings` reads the preset through `kPresetPropNames`
  (`preset` / `preset2` / `target_usage`) instead of the single hardcoded name.

  Proof: `scripts/probe-qsv-preset.py` asserts the boot→`GetVideoSettings`
  round-trip (x264 everywhere; QSV with a value that is *not* the encoder
  default, so a spawn that ignored the env var fails). **QSV needs an Intel QSV
  device: no runner in the fleet has one** (and `patches/0002` disables
  `obs-qsv11` outright when ATL is absent), so those legs print a named partial
  rather than a pass. The hardware-free half —
  `scripts/check-qsv-preset-contract.py`, wired into `lint` — pins the property
  name, the seven values and the default against `obs-qsv11`'s own source, and
  fails the day either side drifts.

- **`PULSAR_VIDEO_PRESET` was a no-op on NVENC too — including `jim_nvenc`, the
  id tried *first*.** Same class of bug as the QSV one above, found while fixing
  it, but the split is *inside* a family rather than between families: the knob
  is `"preset"` on the 31.0+ encoder (`obs_nvenc_h264_tex`,
  `upstream/plugins/obs-nvenc/nvenc-properties.c:142`) and `"preset2"` on the
  pre-31.0 compat shims (`jim_nvenc`, and `ffmpeg_nvenc` — the same compat
  object re-registered under the old id, `nvenc-compat.c:183`/`:397`). A single
  per-family name could not be right for all three.

  On the compat path, writing `"preset"` was worse than inert:
  `migrate_settings()` (`nvenc-compat.c:20`) copies `"preset2"` **over**
  `"preset"` before rerouting, so the encoder ran at `preset2`'s own default
  `p5` whatever the env var said. Since `resolveEncoderId` prefers `jim_nvenc`
  ahead of `obs_nvenc_h264_tex`, this was the *live* path on any build carrying
  the compat shims — not a corner case. The values (`p1..p7`) and default (`p5`)
  are identical on both sides, so only the name differed, which is why it was
  silent from the wire as well.

  The boot setter now resolves the preset property name **per encoder id**
  (`presetPropForId`) instead of per family. The read side needed no change:
  `kPresetPropNames` already carried `preset2`.

  Proof: `scripts/check-nvenc-preset-contract.py`, wired into `lint` and
  hardware-free (**no NVIDIA GPU required or used**). It reads the two property
  names, the value set and the default out of `obs-nvenc`'s own source, then
  asserts that *every* id `resolveEncoderId` can select would be written the
  name its own plugin reads. It fails on the pre-fix tree naming `jim_nvenc` and
  `ffmpeg_nvenc`, and on a partial fix that covers only `ffmpeg_nvenc`.

## [1.3.0] - 2026-07-28

Minor: the release is **additive** — no `pulsar:*` request was removed or
changed shape, no env var renamed. `GetCapabilities` grew a versioned manifest
alongside the four fields it already answered, and `capabilitiesFromWire` keeps
mapping a pre-`version` response unchanged.

**Consumers must upgrade to read the manifest.** `@clodocapeo/pulsar-client`
1.2.2 has no `version` / `capabilities` / `regimes` in
`WireGetCapabilitiesResponse`, so a 1.2.2 parser silently drops the whole
manifest off the wire even when the binary sends it: the C++ merge alone is
**not** the delivery.

### ✨ Added

- **The capability manifest declares what each encoder family offers** (#142,
  ADR Prism 027 §3.3 bloc 1). `capabilities.encoder_families` lists, for every
  family this build actually enumerates, its presets, H.264 profiles,
  rate-controls, keyint window and its own bitrate window. Prism decreed those
  presets from a table marked "hardcoded until the Pulsar C++ work lands" while
  `PULSAR_VIDEO_PRESET` had been read at boot all along — the capability was
  paid for and invisible.

  Every value is **read from that family's libobs properties** (the bound
  encoder's own properties for the active family, the registered id's for the
  others). Nothing here is a list of encoder values held in Pulsar's source: the
  only literals are the libobs property names the same knob goes by across
  plugins (`preset` / `preset2` / `target_usage`). A field the encoder does not
  advertise is omitted, and **a family the binary does not register produces no
  entry at all** — no fabricated block for an encoder that was not compiled in.

  The whole block is **`boot-fixed`**: that is the fact, not a limitation to fix
  later. Making the preset hot-settable is explicitly out of scope (§3.5).
  Profiles, rate-controls and both windows are intersected with what the boot
  setter accepts, so the manifest can only narrow. Presets are deliberately not
  intersected — the boot whitelist is a per-family table this block exists to
  stop duplicating, and it is wrong for QSV (whose knob is `target_usage` with
  values `TU1..TU7`), so narrowing by it would publish an empty set for a family
  that has seven presets. That mismatch is Pulsar's, not the manifest's, and is
  tracked separately.

- **The capability manifest declares its inventories and its colorimetry**
  (#144, ADR Prism 027 §3.3 blocs 3 et 4). `GetCapabilities` now answers which
  **filters are registered** (`obs_enum_filter_types`), which **source kinds**
  can be instantiated (`obs_enum_input_types`), which **destination kinds** this
  binary can actually serve (the `DestinationKind` enum, gated on the obs output
  type that serves it being registered), and the **effective video colorimetry**
  read back from `obs_get_video_info`. No hard-coded list behind any of them.

  This block declares a **presence, never a permission**. Not one filter
  property bound is emitted: which filter setting may be written, and between
  which values, stays owned by Prism's closed whitelist (ADR 023 §3.3, under its
  own security clearance) — deriving a bound from this inventory would void that
  control. Likewise the destination kinds are informative only: ADR 010's
  discriminated union and strict dispatch are untouched, and a kind the consumer
  does not know stays ignorable.

  Colorimetry is `read-only`, **not** `boot-fixed`: colourspace, range and pixel
  format are pinned at `obs_reset_video` and no request *and no env var* selects
  another one, so no list of "available" spaces is published — announcing a
  choice the binary cannot honour is exactly the decree this manifest exists to
  end. An enumeration that yields nothing publishes no entry at all, so the
  consumer keeps its own static list instead of reading an empty array as "none".

  The presence-only rule is locked on **both** sides: the client decoder
  surfaces nothing but `value`, and `scripts/probe-manifest-inventories.py`
  (wired into the offline probe suite) asks a live `pulsar.exe` what it really
  puts on the wire — every inventory item must carry exactly `value`, the
  `filters` entry nothing beside `applicability`/`values`, and
  `video_colorimetry` no selectable list.

- **The capability manifest declares the audio block** (#143, ADR Prism 027 §3.3
  bloc 2). Four new entries — `audio_monitoring`, `audio_tracks`,
  `audio_sample_rate`, `audio_speaker_layout` — each with its own regime.

  The one that matters is the first. Prism offers three headphone-monitoring
  keys as `applyClass: live`, "verified by read-back" of a state nobody ever
  established comes back — and indeed it does not: **nothing in Pulsar calls
  `obs_set_audio_monitoring_device()`**, so no device is ever bound and there is
  no write path at all. The manifest now says so out loud: `available` and
  `device_bound` are **always** emitted, `true` or `false`, and the regime is
  `read-only`, not `live`. A "no" you can read beats a silence you have to guess
  at. The device id/name appear only when a device is genuinely bound — libobs
  seeds its own `"Default"` / `"default"` placeholder in `obs_init_audio()` and
  Pulsar refuses to republish that seed as a binding.

  Tracks, sample rate and speaker layout are **read** from libobs
  (`MAX_AUDIO_MIXES`, the streaming output's mixer slots, `obs_get_audio_info()`),
  never written as constants — a `SPEAKERS_UNKNOWN` layout is declared absent
  rather than published as `"unknown"`, and the bound-track count is omitted
  off-air. `@clodocapeo/pulsar-client` exposes the block as `capabilities.audio`
  and, per the §3.2 rule, keeps a *missing* block absent instead of decoding it
  into a "no".

- **`GetCapabilities` becomes a versioned capability manifest** (#141, ADR Prism
  027 §3.1/§3.2 — P1 socle). The response now carries a `version` and a
  `capabilities` map in which **every entry declares its application regime**
  (`live` / `boot-fixed` / `read-only`) next to its values. That second field is
  the point of the change: Prism *derives* its apply-class from what Pulsar says
  instead of decreeing one, which is what made a preset silently unsettable and
  a replay bound silently wrong.

  The two hard-coded bounds are gone. The video bitrate window and the audio
  bitrate ladder are **read from the active encoder's libobs properties**
  (`obs_encoder_properties` off the streaming output, or
  `obs_get_encoder_properties` on the registered id when off-air), then narrowed
  by Pulsar's own setter policy — so the manifest can only ever announce
  something `SetVideoSettings` would accept. It may narrow, never widen.

  A window libobs does not expose is **declared absent**: the key is omitted, it
  is not replaced by a plausible constant. Absence is a positive answer and a
  consumer reads it as "keep your own static bound".

  Backward compatible in both directions: the pre-#141 top-level keys are still
  emitted verbatim, a client that ignores `capabilities` keeps working, and
  `@clodocapeo/pulsar-client` tolerates entries and regime strings it has never
  heard of. The encoder / audio / inventory / video blocks of ADR 027 §3.3 land
  separately (#142/#143/#144) — this change is their scaffolding, not them.

- **The v5 capability contract is a blocking CI gate** (#121, ADR Prism 026
  §3.3 palier 3). `scripts/probe-capability-contract.py` — until now an
  instrument you ran by hand (#116) — is wired into `scripts/run-probes.ps1`
  (Phase 1h), so `ctest` and the `offline probe suite (CTest)` job now fail on
  any v5 request that answers `result: true` without the effect it promises.
  No `continue-on-error`. It asks a question no other probe asks: not "does
  this feature work" but "does this request tell the truth". An **explicit
  refusal passes** — refusing honestly is correct behaviour; only a success
  that did not happen fails.

  The order matters and is visible in the history: the gate went in *after*
  #117, #119, #120 and #127 turned it green on its perimeter. Wiring it first
  would have made it red on day one, then ignored, then removed.

  The gate is **by tiers**, not all-or-nothing. **33 request types across 13
  families** (General, Scenes, Inputs, SceneItems, Filters, Transitions,
  Stream, Record, ReplayBuffer, VirtualCam, Outputs, StudioMode, Canvases) —
  54 of the 137 advertised types once the independent re-queries are counted,
  up from the 15 of the first pass. The list is frozen in the versioned
  artefact `scripts/contracts/capability-coverage.json` and cross-checked in
  both directions on every run: a subject that stops being driven fails the
  probe, and a subject driven but not declared fails it too. Widening it is a
  separate job; narrowing it is a diff a human has to approve. Families
  outside the list are measured ignorance, not failures.

  The pass turned up three defects it deliberately did **not** gate onto
  (wiring a gate onto a known red is what ADR Prism 026 §3.3 forbids); they
  were routed out as issues rather than silenced, and all three are fixed
  below (#129, #130, #131). The gate closed over them in the same release:
  `RemoveInput` is now a gated subject, `PauseRecord`'s two refusal
  preconditions are driven for real, and `StartStream` is judged on the
  `StreamStateChanged` event it puts on the wire.

- `pulsar-frontend-stub`: the **replay buffer is wired** (#117, ADR Prism 024
  §3.1). `replayOutput` was created at boot but nothing was attached to it —
  no encoders, no settings — so `obs_output_start` declined, and
  `GetLastReplayBufferReplay` returned an empty string forever. It now:
  - **borrows** the very same video + audio encoders already bound to the
    record and stream outputs (encode-once / fan-out, the pattern
    `pulsar-multi-stream::ensure_output` already runs). Arming the buffer
    adds **no** encoder to the process — the replay MP4 comes out at the
    same 6 000 kbps h264 as the recording written beside it;
  - carries real settings: `directory` = `recordDir`, filename template
    `pulsar-replay-%CCYY%MM%DD-%hh%mm%ss.mp4`, `max_time_sec`
    (`PULSAR_REPLAY_MAX_TIME_SEC`, 10..300, default 30) and `max_size_mb`
    (`PULSAR_REPLAY_MAX_SIZE_MB`, 16..8192, default 512);
  - fills `lastReplay` from the output's `get_last_replay` proc handler on
    the `saved` signal, so `GetLastReplayBufferReplay` returns a real path;
  - **refuses an off-air arm, loudly**. The buffer lives off the shared
    encoders; arming it while they are idle would spin one up for a
    partial, invisible pipeline. No replay off-air — an explicit no-go, not
    an oversight.

  The six v5 baseline requests were already compiled — no new `pulsar:*`
  request. `scripts/probe-replay.py` (offline suite, Phase 1d-bis) proves
  the whole round-trip: refused off-air, active on-air, a readable h264+aac
  MP4 on disk at the path the server reports.

### 🐛 Fixed

- `pulsar-frontend-stub`: **the v5 `StartStream` path is wired at last**
  (#131). `SetStreamServiceSettings` faithfully updated the stub's
  `streamService` — `GetStreamServiceSettings` read it back — but
  `obs_frontend_streaming_start()` never called `obs_output_set_service()`,
  and `obs_output_start()` refuses a service-flagged `rtmp_output` on its
  very first line when `output->service` is `NULL`. The v5 single-stream
  path was therefore **structurally unreachable**: no sequence of requests
  could make it work. The encoders had been attached all along; the whole
  defect was one missing binding. This applies the doctrine the fork already
  ratified in Phase 7 ("Approach A": the v5 `StartStream` / `StartRecord`
  path keeps working for Stream Deck / Companion / Streamer.bot, and
  multi-destination is purely additive) instead of revoking it.

  Same commit, the honesty corollary: `OBS_FRONTEND_EVENT_STREAMING_STARTING`
  was emitted **before** `obs_output_start()`, unconditionally, so a refused
  start still put a `StreamStateChanged: OBS_WEBSOCKET_OUTPUT_STARTING` on
  the wire — a #120-class lie, on the event channel instead of the response.
  It now follows a start libobs really took. That ordering is what makes the
  CI gate deterministic without a network: an rtmp output whose connect
  thread is still in flight legitimately reads `outputActive: false`, so the
  `STARTING` event — impossible to produce without the service binding — is
  the evidence the contract probe requires.

  `DescribeOutputRefusal` gained the matching cause: an output that *has* a
  service which cannot be connected to (`rtmp_common` with no stream key,
  say) is now named as such instead of falling back to the generic "the
  output is not configured".

- `pulsar-frontend-stub`: **`RemoveInput` actually removes the input and its
  scene items** (#129) — the same defect family as #119, on a different
  loop. `obs_source_remove()` only flags the source and fires the global
  `source_remove` signal; the scene items holding the last references are
  dropped by `obs_scene_prune_sources()`, which libobs calls **only** from
  `scene_video_render` — that is, only for a scene it is actually rendering.
  obs-studio closes that loop in its frontend; Pulsar's frontend stub did
  not, so `RemoveInput` answered `result: true` and the input stayed in
  `GetInputList` with its item in `GetSceneItemList` indefinitely (any scene
  that is not the program scene, forever). The stub now connects
  `source_remove` in `setup()`, disconnects it in `teardown()`, and prunes
  every scene in **two phases** — collect the scene refs during
  `obs_enum_scenes`, prune once the enumeration has returned — so the source
  list mutex and the scene video lock are never nested.

- `pulsar-websocket`: **`PauseRecord` / `ToggleRecordPause` refuse instead of
  wedging the muxer** (#130). Two guards, both previously absent:
  - pausing an output that is **not recording** answered `Success()`.
    `obs_output_pause()` returns false there and
    `obs_frontend_recording_pause()` is `void`, so the refusal was thrown
    away — the #120 lie, on a request #120 did not cover. Now
    `OutputNotRunning` (501).
  - pausing **before the muxer wrote its first byte** (`outputBytes == 0`)
    wedged `ffmpeg_muxer` permanently: `SaveReplayBuffer` then produced
    nothing and `StopRecord` / `StopReplayBuffer` answered `Success()` while
    `outputActive` stayed true. The root cause is upstream — libobs computes
    the pause window from `pause->last_video_ts`, which is still `0` until
    the first encoded frame, so the resume condition (an exact timestamp
    match) is never met. Patching libobs for an exotic trigger is outside
    this fork's mandate, so the websocket layer — the only layer that can
    *name* a cause — refuses the precondition with
    `InvalidResourceState` (604) and a comment saying exactly that.
    `outputBytes > 0` is a conservative proxy: it can refuse a legitimate
    pause for the few tens of milliseconds after `StartRecord`, never the
    reverse, and the client lifts the condition itself with
    `GetRecordStatus`.

- `pulsar-websocket`: the four output families no longer report an effect
  they never observed (#120, ADR Prism 026 §3.2). `StartReplayBuffer`,
  `StartRecord`, `StartVirtualCam`, `StartStream` and their `Stop*` /
  `Toggle*` counterparts called a `void` `obs-frontend-api` entry point and
  returned `Success()` unconditionally; libobs declines silently on an
  unconfigured output, so a client was told "started" while the next
  `GetXStatus` reported `outputActive: false`. Each handler now re-reads the
  real state after the action and answers an explicit error —
  `OutputNotRunning` (501) for a start, `OutputRunning` (500) for a stop —
  carrying the cause read off the server: `obs_output_get_last_error()` when
  libobs recorded one, otherwise the structural state that made it refuse
  (no service bound on a service output, no encoder bound on an encoded
  one). **No signature change**: no new request, no new status enum, no new
  response field.
  The refusal is decided from the output's own `"starting"`/`"stopping"`
  signal, which libobs emits only when it actually took the action — so an
  asynchronous completion still in flight (an rtmp connect thread, an
  `ffmpeg_muxer` flush) is reported as success, not as a failure, and no
  request ever waits for activation. The residual state poll is bounded by
  `PULSAR_OUTPUT_VERIFY_MS` (250 ms default) and is not reached on the
  nominal path. `scripts/probe-output-effect.py` drives a real refusal in
  each family plus a positive control and a latency bound.
  Integration of #117 with #120: the off-air replay refusal is Pulsar's own
  policy, taken *before* `obs_output_start`, so libobs recorded no cause and
  the verification could only answer the generic "the output is not
  configured" — false, since the encoders *are* attached, just idle. The stub
  now publishes the cause through `obs_output_set_last_error()` at the point
  of refusal, so #120 names it like any libobs cause. Nothing has to clear
  it: `obs_output_actual_start()` wipes `last_error_message` on the next real
  start.
- `pulsar-websocket`: the **by-name generic outputs** carry the same
  verification (follow-up to #120, same defect class, left out of scope
  there). `StartOutput`, `StopOutput` and `ToggleOutput` called
  `obs_output_start` / `obs_output_stop` and returned `Success()`
  unconditionally — reachable on every output the stub creates
  (`PulsarStream`, `PulsarRecord`, `PulsarReplay`, `PulsarVCam`) and on every
  `pulsar-multi-stream` destination. They now reuse
  `Utils::Obs::OutputHelper` (the #120 helper, not a second mechanism) and
  answer the same way; the refusal `comment` additionally **names the
  output** — ``The output `PulsarStream` did not start: no streaming service
  is configured …``. `ToggleOutput`'s `outputActive` response field is now
  the state read back from libobs, not `!wasActive`.
  `GetOutputStatus.outputReconnecting` was audited under the same rule and
  left unchanged: it is a straight read of libobs' reconnect atomic
  (`os_atomic_load_bool(&output->reconnecting)`), with no server-side mirror
  that could drift. What it inherits and is now documented is the libobs
  pairing — `obs_output_active` is `active || reconnecting`, so a
  reconnecting output reports both flags true.
  `scripts/probe-output-effect.py` gains cases F (real refusal on
  `PulsarStream`, no service bound), G (positive control: a legitimate
  generic stop of a live `PulsarRecord` still succeeds and the effect is
  real on both views) and H (the `outputReconnecting` invariants, asserted
  on every `GetOutputStatus` it issues).

### 🔒 Security

- **The obs-websocket server binds the loopback, not every interface**
  (#134, Bastion on the PR #133 revalidation). `WebSocketServer::Start`
  called `listen(port)` (the v6 any-address, dual-stack) or
  `listen(tcp::v4(), port)` (`0.0.0.0`) — upstream's default for a desktop
  app the user opts into exposing. In Pulsar it meant the entire v5
  surface, **including the stream-egress path `#131` had just made live**,
  was reachable from the LAN behind nothing but the session password —
  a password the CI failure-log artefact carries in clear
  (`pipeline.yml`, 7-day retention). Nothing needed that reach: every
  consumer connects to `127.0.0.1` (`packages/pulsar-bundle*/src/spawn.ts`,
  the `PULSAR_READY` sentinel, every `scripts/probe-*.py`), and
  `docs/PROTOCOL.md` already **claimed** loopback-only — the code now
  makes that claim true rather than the doc describe a wish.

  The address is a single config field (`Config::BindAddress`, default
  `127.0.0.1`), widened only by an explicit `PULSAR_WS_BIND`, which logs a
  warning when it is not a loopback address. Env-only, deliberately: it is
  the parent process that decides how far the server reaches, and keeping
  it out of the persisted `config.json` means a stale or tampered config
  cannot silently widen the bind on a later boot. `--websocket_ipv4_only`
  is subsumed (the address decides the family) and says so when it
  contradicts an explicit override.

- **The v5 egress refusal covers every `rtmp_common` service, not just
  Twitch** (#135, Bastion on the PR #133 revalidation). The rule the guard
  states is "this path never resolves an ingest URL out of a downloaded
  list we do not control" — but its predicate matched the literal service
  name `Twitch`, so YouTube, Kick, Trovo and the hundreds of other
  `rtmp_common` entries went through the very same `update_ingest`
  resolution, out of the very same list, several of them with cleartext
  `rtmp://` servers. The dangerous mechanism is the resolution, not the
  platform. `IsTwitchCommonService(type, settings)` becomes
  `IsRtmpCommonService(type)`: the **type alone** decides, at both seams,
  and the refusal message names the mechanism instead of one platform.
  No capability is lost — `rtmp_custom` with an explicit server already
  covers the legitimate operator need, in the clear, which is the residual
  ADR 010 §5 accepts.

- **The boot placeholder is neutral** (#136). The stub created its
  placeholder streaming service as `rtmp_common` / "Twitch", so the safety
  of the state Pulsar boots in — before any operator configuration — rested
  on the egress gate **refusing** it. Correct, but by rebuttal. It is now
  an empty `rtmp_custom`: it names no platform, resolves nothing out of any
  list, and simply has no destination to connect to. The default path is
  safe by construction, with nothing to refuse; the gate still holds for
  whatever a v5 client pushes afterwards, but is no longer what makes the
  default safe.

- **The v5 stream path can no longer send to Twitch in cleartext** — the
  regression `#131` would otherwise have introduced against `#114` /
  `1.2.2` (Bastion C1 on PR #133, form **(b)**). Binding `streamService` to
  `streamOutput` (above) is what made the v5
  `SetStreamServiceSettings` + `StartStream` path a **live egress** for the
  first time; `#114` had closed the cleartext hole partly by leaving that
  path dead. An `rtmp_common` service named `Twitch` resolves its ingest
  through upstream's `update_ingest`
  (`upstream/plugins/rtmp-services/rtmp-common.c`), which falls back to the
  bundled default `rtmp://live.twitch.tv/app`
  (`service-specific/twitch.c:45`) whenever the downloaded ingest list is
  absent — first run, cold cache, offline. A v5 client pushing a Twitch
  service plus a key and calling `StartStream` would have put that key on
  the wire unencrypted, on a path whose twin `pulsar:StartDestination`
  guarantees `rtmps://` by `static_assert`. Worse, the stub's own **boot
  placeholder** is exactly that service, so no request was even needed to
  arm the gun.

  **Twitch is now barred from the v5 single-stream path**, at both seams:
  `SetStreamServiceSettings` refuses `rtmp_common` + `service: "Twitch"`
  with `InvalidRequestField` (400), and `obs_frontend_streaming_start()`
  refuses to bind such a service at all — including the boot placeholder —
  so the refusal holds whatever the caller did upstream of it. Twitch
  egress goes through `pulsar:StartDestination`, which already carries the
  compile-time `rtmps://` guarantee. The v5 path stays alive for
  `rtmp_custom`, which is the Stream Deck / Companion compatibility `#131`
  exists for. *(#135, above, has since widened this refusal from Twitch to
  the whole `rtmp_common` type — the entry is kept as the record of how the
  rule was first drawn.)*

  Two other forms were considered and rejected. *(a)* refusing any resolved
  `rtmp://` URL would break parity with the twin, which deliberately
  accepts `rtmp://` for operator-supplied endpoints (a LAN relay), or force
  two divergent scheme policies onto one product. *(c)* patching
  `twitch.c:45` in the vendored submodule would bury a Pulsar security
  invariant inside upstream code — re-litigated at every submodule bump,
  invisible to anyone reading Pulsar's own sources. Form (b) keeps one
  rule, "Twitch egress is the multi-stream plugin's job", enforced where
  the egress is decided.

- **`SetStreamServiceSettings` validates its destination like
  `pulsar:StartDestination` does** (Bastion C2 on PR #133). The v5 request
  applied no schema at all: any service type, any settings. Its twin
  front-loads `is_rtmp_scheme()` + non-empty-key validation before any
  `obs_output_*` allocation (`pulsar-multi-stream/src/plugin-main.cpp:100-121`).
  A newly live egress path must not be more permissive than its twin, so
  the same two rules now apply: the resolved server must be `rtmp://` or
  `rtmps://` and the stream key must be non-empty, else the request answers
  `InvalidRequestField` (400) naming the cause and **applies nothing**
  (validated on a throwaway private service, so a refusal cannot half-write
  the frontend's). Same-type calls still merge onto the current settings —
  and it is the **merged** result that is validated, so a partial update
  cannot inherit its way past the rules.

  The predicate lives in one header-only file,
  `plugins/pulsar-frontend-stub/include/pulsar-stream-egress.h`, shared
  verbatim by the configuration seam (`pulsar-websocket`) and the start seam
  (`pulsar-frontend-stub`): two link units, one rule, no drift.

### ✅ Tested

- `scripts/probe-loopback-bind.py` — the executable form of #134, at the
  **socket** layer (no v5 request: the property is about who can open the
  socket at all). Wired into the offline suite (Phase 1f-ter). Three legs:
  the loopback still accepts and still completes a v5 `Identify` (a bind
  fix that broke *that* would be worse than the exposure); a TCP connect to
  this host's own non-loopback address, on the same port, must **fail** —
  it succeeds on the pre-fix binary; and `PULSAR_WS_BIND=0.0.0.0` does
  re-open it, so the loopback default is a decision and not an accident.
  The probe never touches the network (it asks the routing table which
  local address would be used, via a UDP `connect()` to TEST-NET-1 that
  sends no packet) and degrades to exit 3 (typed skip) on a host with no
  routable address, where the boundary is unobservable.

- `scripts/probe-stream-egress-guard.py` — the executable form of both
  guards above, wired into the offline suite as a **blocking** gate
  (Phase 1f-bis, no skip path, no `continue-on-error`: a security invariant
  with a tolerated red is not an invariant). Widened with #135/#136: the
  **boot placeholder** leg now requires a *neutral* placeholder (not
  `rtmp_common`, no server, no key) and a `StartStream` refused for want of
  a destination rather than by rebuttal, and the configuration leg drives
  four more `rtmp_common` payloads — `YouTube - RTMPS`, `Kick`, `Trovo`
  with a cleartext server, and one with **no** `service` setting at all —
  each of which the #133 binary accepted. Both changes are red on that
  binary and green here. The original cases stand: the Twitch
  configuration refusal in both spellings (`Twitch`, `twitch`) and its
  atomicity (the refused key must not be written anyway); a non-`rtmp`
  scheme; an
  empty key; the **merge** path (pushing only `server` onto a service whose
  key is still empty must stay refused); and the non-regression leg —
  `#131`'s own nominal `rtmp_custom` destination still configures **and**
  still reaches `StreamStateChanged: OBS_WEBSOCKET_OUTPUT_STARTING`, so the
  guard did not re-kill the path it protects. Every refusal must carry a
  **named** cause; a bare code would be the `#120` defect wearing a security
  hat. No network: the nominal destination is a deliberately unreachable
  `rtmp://127.0.0.1:1`.

- `scripts/probe-vcam-scene-mode.py` — **#119 resolution criterion 3**
  (virtual cam source mode, `VCAM_SCENE`) was reasoned about but never
  exercised. The probe creates `ZabVirtualCamSource` over the **wire**,
  asserts `GetSceneList` now lists it (the scene mirror used to hide exactly
  that), puts a *different* scene on program so a silent fallback to the
  program mix would show, then starts the cam and demands both the real
  effect (`GetVirtualCamStatus.outputActive`) and the stub's own
  `virtual cam SOURCE mode -> 'ZabVirtualCamSource'` log line. Wired into
  the offline suite (Phase 1g). The criterion is split along what each
  machine can actually answer: the **scene resolve** — the only half the
  mirror removal could have broken, and it runs before the device is
  touched — is asserted everywhere, including on a CI runner; the **device**
  leg needs a real virtual-camera DirectShow filter (libobs gates the whole
  `virtualcam_output` type on it, `win-dshow/dshow-plugin.cpp:48`) and
  degrades to exit 3 (typed skip) with what to install printed out, rather
  than passing silently. Exercised end to end, device included, on a
  workstation that has the OBS virtual camera installed.
- both probes now force `errors="replace"` on their own stdout/stderr: on a
  non-English Windows the libobs log lines they quote are cp1252-hostile,
  and a failure used to surface as an `UnicodeEncodeError` traceback that
  hid the assertion which actually fired.

## [1.2.2] - 2026-07-26

### 🔒 Security

- `pulsar-multi-stream`: the `twitch` destination kind no longer puts the
  Twitch stream key on the wire in cleartext (#113, PR #114). The pinned
  ingest URL moves from `rtmp://live.twitch.tv/app/` to
  `rtmps://ingest.global-contribute.live-video.net/app/` — the
  `url_template_secure` of the `Default` entry of
  <https://ingest.twitch.tv/ingests>. The stream key is a bearer
  credential and travels inside the RTMP connect handshake, so every
  go-live previously exposed it to anyone on the path; the legacy
  `live.twitch.tv` host is not in the published ingest list and refuses
  TLS on :1935, so switching the scheme alone was not an option.
  mbedTLS/librtmp already carry the TLS path (`obs-outputs` built with
  `USE_MBEDTLS`) and librtmp derives the transport from the scheme — no
  extra service setting. There is no cleartext downgrade: a failed
  handshake or a failed certificate verification aborts in
  `RTMP_Connect1`. A `static_assert` pins the scheme at compile time and
  `scripts/probe-twitch-rtmps.py` guards it (real ingest passes the TLS
  stage; two negative controls prove a TLS failure is loud and fatal).
  Refs ADR 021 (Prism) palier 1.

  **Consumers must upgrade** — the fix lives in the compiled plugin, so
  a Prism/embedder still on `1.2.1` keeps streaming in cleartext even
  with this source merged. Bump to `@clodocapeo/pulsar-bundle-full`
  `1.2.2` (postinstall pulls `pulsar-windows-x64-full-v1.2.2.zip`).

## [1.2.1] - 2026-07-05

### 🐛 Fixed

- `pulsar-scene-source`: repeated `pulsar-scene:SetCaptureSource` calls no
  longer strand stale `browser_source` items on the program scene (#110). The
  new source was created (canonical name `PulsarSceneSource`) before the old
  one was removed, so libobs de-duped the fresh instance to
  `PulsarSceneSource 2`; the exact-`strcmp` cleanup then missed every numbered
  variant, leaving them accreting on the scene indefinitely and letting a
  name-based consumer (Prism's `findBrowserSourceName`) lock onto a stale
  instance from the 3rd re-point on. The cleanup now runs before the fresh
  source is added; the outgoing managed source is renamed out of the
  canonical name **synchronously** (`obs_source_set_name` updates libobs's
  global name table under lock, whereas scene-item removal only *schedules*
  the source's deferred destruction — relying on that release was an
  intermittent race), so the fresh source can then reliably reclaim the
  canonical name; and the managed-item matcher recognises libobs de-dup
  variants (`base <n>`) so any pre-existing drift is swept too.
  Regression-guarded by `scripts/probe-scene-name-drift.py` (24 rapid
  re-points) in the offline probe suite.

## [1.2.0] - 2026-07-03

### ✨ Added

- `pulsar-frontend-stub`: boot-time GPU video-encoder selection (ADR 004
  §3.1-3.2). New env vars `PULSAR_VIDEO_ENCODER` (`x264`/`nvenc`/`qsv`/`amf`/
  `auto`), `PULSAR_VIDEO_PRESET`, `PULSAR_VIDEO_PROFILE`,
  `PULSAR_VIDEO_RATE_CONTROL`, `PULSAR_VIDEO_KEYINT_SEC`, all resolved against
  the live `obs_enum_encoder_types()` set (H.264 only) with a mandatory typed
  fallback to `obs_x264` — an absent family, a null `create()`, or an invalid
  knob degrade silently (logged) to today's byte-identical x264 path; the spawn
  never fails on encoder choice. Encoder identity is boot-fixed (no live swap),
  same tier as `PULSAR_FPS`/`PULSAR_RESOLUTION`.
- `pulsar-multi-stream`: new `pulsar:GetCapabilities` vendor request (ADR 004
  §3.3) — enumerates the encoder families this build exposes (mapped from
  `obs_enum_encoder_types()` to the whitelisted short names `x264`/`nvenc`/
  `qsv`/`amf`, never a raw obs id) plus `active_encoder`, the `video_bitrate`
  `{min,max}` window and the `audio_bitrate` ladder. `GetVideoSettings` gains
  `video_encoder`/`video_preset`/`video_profile` for a complete off-air
  snapshot; `SetVideoSettings` now rejects those three fields with the same
  typed boot-fixed error as `fps` (no live encoder swap — ADR 004 §3.4).
- `@clodocapeo/pulsar-client`: `pulsar.capabilities` namespace
  (`client.capabilities.get()` → typed `PulsarCapabilities`) wrapping
  `pulsar:GetCapabilities`; `VideoSettings` gains `videoEncoder`/`videoPreset`/
  `videoProfile`.
- `@clodocapeo/pulsar-client`: `pulsar.audio` namespace — stream-level mic
  control (mute/unmute/toggle, device enumeration + selection via
  `SetInputSettings.device_id`) wrapping the native obs-websocket v5 `Input*`
  requests, no vendor plugin involved. Mute state lives on the mic input
  itself, not on any scene, so cockpit mic controls survive scene switches.
  Adds the typed `inputMuteStateChanged` event.

## [1.1.0] - 2026-06-10

Operability + M10 transition groundwork release. Builds on the V1 headless
broadcast engine with: a governance/merge-gate CI surface, a frozen
cross-service `scene_control` contract (Blue → Orion leaf → Solar/Prism),
the M10 "blue-driven scene transition" harness and probes, and a dormant
native-stinger compositing capability gated OFF by default. No change to the
public spawn/handshake (PRISM-EMBEDDING) or the obs-websocket request surface
— all additions are backward-compatible (minor bump per `docs/PROTOCOL.md`).

### ✨ Added

- **CI compliance workflow (`compliance.yml`).** Org merge-gate conformance,
  kept separate from the build pipeline: `secret-scan` (trufflehog verified
  history+filesystem scan **+** detect-secrets audit against
  `.secrets.baseline`), `deps-audit` (npm `--omit=dev --audit-level=high`,
  high/critical CVE blocks), `lockfile-check` (`npm ci --dry-run`, no drift,
  stray-`yarn.lock` guard) and `codeowners-check` (structural CODEOWNERS
  validation). No error-suppression toggles — every job can turn a PR red.
- **`scene_control` cross-service contract** (`scripts/contracts/scene_control/`).
  Single source of truth for the leaf that travels Blue → `Orion leaf` →
  Solar/Prism/probe, with a schema validator, valid/malicious fixtures and a
  contract test (`test_scene_control_contract.py`) bound to a mirror of Blue's
  leaf_mapper. Wired into CI as the `contract tests (scene_control)` job.
- **M10 transition harness** (`scripts/m10_setup.py`, `run-m10.ps1`,
  `run-m10-live.ps1`, `m10_orion_standin.py`) creating the two
  `monitor_capture` scenes and the Solar/CEF overlay used for the
  blue-driven scene transition, plus the loopback Orion-WS stand-in.
- **Native stinger compositing**, gated behind the `PULSAR_NATIVE_STINGER`
  env flag (**default OFF / dormant**). Adds a fade + stinger transition pair
  bound as the encoder output source, with `PULSAR_STINGER_ASSET` to pin a
  **local-only** demo asset path. The flag is resolved once at boot from the
  process environment and is **never** reachable from a leaf / obs-websocket /
  network value (Bastion invariant, ADR 003 §A4.5).
- **OBS version tagging** — `OBS_VERSION` is suffixed with a `pulsar` marker
  so a built binary is identifiable as the fork (`patches/0001-…`).
- **ATL-dependent plugin build gate** behind `PULSAR_HAVE_ATL`
  (`patches/0002-…`), with a runbook for the missing-build failure mode.
- **New probe suite** — 30 s Twitch scene-switch probe, M1/M2/M3/M6 milestone
  probes (binary smoke, media-output→MP4, CEF browser-source capture, real
  Solar scene on air), the M10 Canvas-live probe, the GPU-coexistence spike
  (`monitor_capture` + CEF `browser_source` on GPU) and a flag-aware stinger
  smoke probe.
- **Pinned stinger demo asset** generator (`scripts/assets/`,
  `generate-stinger-demo.ps1` + manifest) for the dormant native path.

### ♻️ Changed

- **Pivot to a Solar/CEF overlay transition (M10).** The transition is no
  longer an OBS-native media transition: Solar/CEF animates a full-screen
  opaque overlay over the two captures and the underlying screen change is an
  instantaneous hard-cut hidden under the overlay plateau. The leaf
  co-specifies the overlay animation (Solar) and the `cut_at_ms` (Prism); the
  OBS-native form (media `asset_id` / `path` / action verbs) is superseded and
  kept only behind the dormant flag.
- **Harness capture method forced to WGC** (Windows Graphics Capture) to prove
  `monitor_capture` coexists with the CEF browser source headless; Orion scene
  default migrated to the overlay shape.
- **Transition output binding moved out of `setup()`** — the encoder output
  source is now driven by the transition (passthrough when idle, blend
  mid-switch) rather than a raw scene bind.
- **Docs refresh** — full `docs/` set re-synced (ARCHITECTURE, DEVELOPMENT,
  PROTOCOL, PRISM-EMBEDDING) and `CLAUDE.md` untracked (now local-only).

### 📝 Docs

- **ADR 001** — ATL build gate + CI compliance, accepted.
- **ADR 002** — M8 Canvas-authored live test, accepted.
- **ADR 003** — blue-driven OBS scene transition (M10), accepted, through
  Amendment 5 (Solar/CEF overlay pivot, Orion wipe-cover authoring link,
  M9-premise/transport corrections).
- **Runbook** — `docs/runbooks/atl-missing-build-failure.md`.
- Package READMEs expanded (`pulsar-client`, `pulsar-bundle`,
  `pulsar-bundle-full`, `pulsar-frontend-stub`).

### 🔧 CI / Build

- `pipeline.yml` gains the `contract tests (scene_control)` job (ubuntu,
  parallel to lint) plus offline M10 harness/probe tests.
- New `scripts/build-win.ps1` build entrypoint.
- `.gitignore` now excludes the local `/CLAUDE.md` agent constitution.

### ⚠️ Notes

- `PULSAR_NATIVE_STINGER` is **dormant** in this release: unset (the default)
  keeps OBS doing a raw hard cut, the stinger source is never registered and
  no media is decoded. It exists in `main` as a future capability only.
- The live M10 transition on-air leg (overlay actually compositing, leaf read
  off `/show/stream`) is proven by the CTest integration suite + an operator
  antenna run, not by the offline CI contract/probe tests.

## [1.0.0] - 2026-05-02

V1 — first stable release. Pulsar is now a production-grade headless
broadcast engine that Prism (and any future consumer) can bundle and
spawn confidently. The full set of changes squashed into the V1
readiness commits on `main`, summarised :

### Added

- **CEF browser_source via the pulsar-browser fork.** Forked obs-browser,
  dropped the `obs_browser_initialize` FFI surface, co-located the
  helper exe with `libcef.dll` so Windows resolves CEF imports
  without manual staging. browser_source now renders HTML/CSS/JS
  scenes into the encode pipeline, used by the live-broadcast probe.
- **Session credentials seeded at boot.** `PULSAR_PORT` and
  `PULSAR_PASSWORD` env vars override any persisted obs-websocket
  config before plugins load ; a `PULSAR_READY ws=… password=…`
  sentinel is emitted on stdout for the spawning process to parse.
  No more disk race against `obs-websocket/config.json`.
- **/SUBSYSTEM:WINDOWS pulsar.exe** with `AttachConsole(ATTACH_PARENT_PROCESS)`.
  Spawn from Prism / scripts no longer allocates a visible cmd.exe
  window ; direct invocation from a real terminal still prints to
  the operator's console.
- **`docs/PRISM-EMBEDDING.md`** consumer spawn / handshake / lifecycle
  contract (mandatory `cwd`, `windowsHide:true`, READY sentinel
  parse loop, shutdown protocol).
- **Live-broadcast proof on every release.** The pipeline pushes a
  10 min Twitch broadcast, records locally via `StartRecord`,
  re-encodes with ffmpeg CRF 23 (~5-25× smaller than source CBR),
  publishes the MP4 to GitHub Pages so the README `<video>` plays
  inline, and attaches the same MP4 to the GitHub Release.
- **Lag-attribution diagnostic JSON** (`diagnostic.json`). Per-poll
  perf samples (active_fps, render_ms, output_skipped, effective
  bitrate) + summary stats + ffprobe of the MP4. Uploaded as
  workflow artefact on every run.
- **Apple-keynote test scene.** Hand-coded `test-scene.html` shell
  + `prism-v2-app.jsx` React app. Six telemetry stats bound to
  `pulsar:GetAdaptiveState`, Web Audio sound design (event-only,
  no background music), `Introducing Pulsar` letter-cascade intro
  with the `Pulsar` word warming to SF System Orange.
- **CTest-driven offline probe suite**, wired into the pipeline as
  the `offline probe suite` job. Probes : websocket, source-kinds
  (input kind inventory smoke), events, adaptive, record.
- **Binary-export gate broadened** to every Pulsar plugin DLL, not
  just `pulsar.exe`. `pulsar.exe` + `pulsar-browser-page.exe` must
  export zero symbols ; plugin DLLs may export only the OBS module
  ABI (`obs_module_load`, `obs_module_set_pointer`, …).
- **`pulsar-multi-stream.samples` counter** — the adaptive worker
  exposes a monotonic sample count so external observers can
  confirm liveness without waiting for a bitrate adjustment.

### Changed

- **CI consolidated from 6 workflows into 1 `pipeline.yml`** with
  9 isolated jobs sharing a single `pulsar-rundir` artefact. No
  more parallel rebuilds doing the same work. `concurrency:
  pipeline-<ref>` with `cancel-in-progress: true` cancels in-flight
  runs on the same ref.
- Pipeline trigger matrix : push branch / PR = 60 s smoke ; push to
  `main` = 10 min release-grade broadcast + gh-pages publish ; push
  tag `v*.*.*` = + package + GitHub Release attach + npm publish ;
  workflow_dispatch = configurable.
- **WASAPI mic source is opt-in** via `PULSAR_MIC_DEVICE_ID`. Hosts
  without a default input device (CI runners, servers) no longer
  spam `Device '' invalidated. Retrying` every 2 s.
- `pulsar-multi-stream::release_destination_handles_locked` does a
  graceful stop + 500 ms drain tail before release. Avoids a
  use-after-free between `obs_output_release` and the worker
  thread on the rtmp_output ECONNREFUSED-fast path.

### Fixed

- CEF GPU subprocess `Reason: '63'` crash on launch — root cause
  was the helper exe living in `bin/64bit/` while libobs's plugin
  loader expected it in `obs-plugins/64bit/` next to `libcef.dll`.
- Black-screen broadcast on the 30-min main run when unpkg.com
  flaked. React + ReactDOM + Babel are now vendored under
  `scripts/live-test/vendor/`.
- The `pulsar-live-broadcast-proof.mp4` (stable name) was missing
  from the gh-pages upload due to a too-narrow glob ; the README
  inline player no longer 404s.
- pulsar-scene-source vendor namespace renamed (`pulsar` →
  `pulsar-scene`) to disambiguate from `pulsar-multi-stream`.

### Skipped (tracked as TODO upstream-obs)

- `probe-multi-stream.py` is excluded from the offline suite. The
  destination lifecycle has known race-condition crash paths in
  obs upstream (rtmp_output worker vs ECONNREFUSED, ffmpeg_muxer
  flush vs Stop, service-ref vs worker exit ordering). The
  `pulsar:CallVendorRequest` API contract is exercised by the
  live-broadcast probe against a real Twitch ingest. Tracked as
  TODOs in `run-probes.ps1`, the multi-stream plugin source, and
  the probe.
- The two specific sub-tests inside probe-multi-stream that
  exercise the racey paths (`StartDestination` on a dead RTMP
  address + `RemoveDestination` while active) are commented out
  with TODO(upstream-obs) markers for when the upstream fixes land.

## [0.2.1] - 2026-04-30

### Added

- `LICENSE` file shipped inside each npm package tarball — previously
  the `files[]` arrays referenced a `LICENSE` that did not exist on
  disk, so published tarballs had no licence text. Each package now
  ships its own copy.
- README "License" section expanded on all three packages to clarify
  what users actually receive on disk and how the GPL applies.

### Changed

- `@clodocapeo/pulsar-bundle` and `@clodocapeo/pulsar-bundle-full`
  `package.json` `license` field corrected from `MIT` to
  `GPL-2.0-or-later`. The bundles ship `pulsar.exe` (libobs + Pulsar
  plugins, GPL-2.0-or-later); declaring them MIT was misleading and
  hid the GPL §3 source-distribution obligation that flows to
  redistributors. The aggregate is GPL.
- `@clodocapeo/pulsar-client` stays MIT — it contains no libobs code,
  links nothing GPL, and speaks obs-websocket v5 over a WebSocket.
  The README now states this explicitly so consumers building
  proprietary tools on top of the protocol know the wrapper is safe
  to embed.

## [Unreleased - older entries]

### Added

- Initial repository scaffold: README, CLAUDE.md, top-level
  CMakeLists.txt skeleton, docs (architecture, protocol, development),
  patches/ + plugins/ + scripts/ placeholders, GitHub Actions workflow
  skeletons.
- LICENSE — GPL-2.0-or-later, verbatim text from gnu.org.
- `upstream/` git submodule wiring to `ZabLaboratory/obs-studio`
  (fork of `obsproject/obs-studio`), pinned to tag **32.1.2**
  (commit `fb4d98bf88fae5fc85cb11fc57f7c5e309282194`, released
  2026-04-21).
- Patch pipeline — `scripts/build-win.ps1` now resets `upstream/`
  to the recorded submodule SHA (read via `git submodule status
  --cached`) and applies every `patches/*.patch` via `git am` in
  lexical order before configure. Idempotent: each run starts from
  the pinned commit + N patches.
- `patches/0001-build-tag-OBS_VERSION-with-pulsar-suffix.patch` —
  appends a `-pulsar` suffix to the runtime `OBS_VERSION` string
  so any binary built from this fork is observable as such (window
  title, About dialog, log preamble). First demonstrator that the
  patch lifecycle works end-to-end. Validated runtime: window title
  reads `OBS 32.1.2-1-g<sha>-pulsar`.
- Headless build mode (Phase 3a). `scripts/build-win.ps1` defaults
  to disabling Qt frontend and the CEF browser source plugin via
  `-DENABLE_FRONTEND=OFF -DENABLE_UI=OFF -DENABLE_BROWSER=OFF`.
  No `obs64.exe`, no Qt6 DLLs in the rundir, libobs core + 25
  modules build. `User Interface` and `Browser sources are not
  enabled by default` listed under Disabled Features at configure
  time. Pass `-GuiBuild` to opt back in to the full obs-studio
  build for debugging or comparison.
- `-Clean` switch on `build-win.ps1` — wipes `upstream/build_x64`
  before configure so stale artefacts from a previous mode (e.g.
  obs64.exe + Qt6 DLLs left in rundir by a GUI build) do not
  contaminate a subsequent headless run. Dep caches under
  `upstream/.deps/` are preserved.
- **Phase 3b — first headless run.** New `pulsar.exe` executable
  built from `plugins/pulsar-headless/main.cpp` (37 KB), linked
  against the libobs that upstream produced. Phase 3b proof of
  life: the binary calls `obs_startup`, libobs initialises (CPU
  detection, default video canvas creation, etc.), prints
  `pulsar-headless: libobs 32.1.2-1-g<sha>-pulsar initialised`,
  then `obs_shutdown` cleans up. Exit code 0, no Qt loaded.
- **Phase 4a — long-running headless service.** `pulsar.exe`
  becomes a real service: configures default video (1080p30
  NV12 D3D11) via `obs_reset_video`, audio (48 kHz stereo) via
  `obs_reset_audio`, lets libobs's default module paths
  discover plugins, calls `obs_load_all_modules` +
  `obs_post_load_modules`. Wires `SetConsoleCtrlHandler` so
  Ctrl+C / window close / system shutdown flips an atomic
  `g_running` flag, then a 100 ms idle loop polls it. Graceful
  `obs_shutdown` on exit. ~66 MB RAM idling, vs 216 MB for the
  Qt obs64.exe. **20 plugins loaded exactly once** (not the
  duplicate registrations the first attempt produced).
- obs-deps runtime staging in `build-win.ps1`. Upstream's CMake
  copies FFmpeg / zlib DLLs to the rundir as part of the
  frontend build steps; with `ENABLE_FRONTEND=OFF` the copy
  never happens and `pulsar.exe` fails to load with
  `STATUS_DLL_NOT_FOUND` (0xC0000135) as soon as the loader
  resolves obs.dll's imports. The build script now stages every
  `*.dll` from `upstream/.deps/obs-deps-*/bin/` into
  `rundir/bin/64bit/` after the Pulsar plugin build.
- **`obs_add_module_path` removed from `pulsar-headless`.** libobs's
  built-in `add_default_module_paths()` (in
  `upstream/libobs/obs-windows.c:43`) already registers
  `../../obs-plugins/64bit/` and `../../data/obs-plugins/%module%/`
  on Windows. Calling `obs_add_module_path` ourselves with the
  same paths was duplicating every module load.
- **Phase 4b — Qt infrastructure for libobs Qt-linked plugins.**
  `pulsar-headless` now constructs a `QApplication` early in `main`
  with `QT_QPA_PLATFORM=minimal`, so libobs plugins that link
  against Qt6 (notably the upstream obs-websocket) can be loaded
  without crashing on `QObject` machinery. The build script stages
  the Qt6 runtime (`Qt6Core`, `Qt6Gui`, `Qt6Widgets`,
  `Qt6Network`, `Qt6Svg`, `Qt6Xml`) plus the `qminimal` /
  `qwindows` platform plugins from the `obs-deps-qt6-*-x64`
  tarball into the rundir. RAM cost: ~5 MB (66 MB → 71 MB idle).
- **Upstream obs-websocket disabled.** Pass
  `-DENABLE_WEBSOCKET=OFF` to upstream's CMake. The upstream plugin
  hardcodes Qt UI dependencies (`forms/SettingsDialog`,
  `forms/ConnectInfo`) and calls `obs_frontend_get_current_profile_path`
  in its `obs_module_load` migration path, which crashes under our
  headless service because no frontend has registered. Phase 4c
  introduces a `plugins/pulsar-websocket/` vendor fork with Qt UI
  and frontend-api migration calls stripped.
- **Phase 4c — pulsar-websocket vendor fork (source).** The
  obs-websocket plugin source tree (v5.7.3) is vendored under
  `plugins/pulsar-websocket/` with three intentional differences
  from upstream:
  1. `src/forms/` removed entirely (no Qt SettingsDialog /
     ConnectInfo / images / resources.qrc).
  2. `src/obs-websocket.cpp`'s `obs_module_load` no longer
     constructs `SettingsDialog` and no longer registers a Tools
     menu entry. The `forms/SettingsDialog.h` include and the
     `_settingsDialog` global are gone.
  3. `src/Config.cpp`'s `MigrateGlobalConfigData()` and
     `MigratePersistentData()` are reduced to no-ops. Pulsar starts
     from a clean state -- there are no legacy obs-studio
     obs-websocket configs to migrate, so the calls into
     `obs_frontend_get_app_config()` and
     `obs_frontend_get_current_profile_path()` (which return null
     under headless and crash the migrate path) are not made.

  The CMakeLists.txt is a stub that aborts with `FATAL_ERROR` if
  `PULSAR_BUILD_WEBSOCKET=ON` is set. Phase 4d adds the actual
  build wiring (sources, deps from `upstream/.deps/`,
  Qt6::Core + Qt6::Network link, `obs.lib` link, output target).
- **Phase 4d -- pulsar-websocket builds + listens.** The plugin
  CMakeLists is now a real build target. Output:
  `obs-websocket.dll` (2.8 MB) dropped into
  `rundir/obs-plugins/64bit/`, en-US locale staged into
  `rundir/data/obs-plugins/obs-websocket/locale/`. Linkage:
  `obs.lib` + `obs-frontend-api.lib` from
  `upstream/build_x64/`, `Qt6::Core` + `Qt6::Gui` +
  `Qt6::Widgets` + `Qt6::Network` from the obs-deps Qt6 tarball,
  `nlohmann_json::nlohmann_json` from obs-deps. Header-only Asio
  + websocketpp resolved via include path on the
  `obs-deps-*-x64/include/` directory.

  Two extra patches were needed beyond the Phase 4c source changes
  to make it actually load + listen under headless:

  - **`src/Config.h` -- `ServerEnabled` defaults to `true` (was
    `false` upstream).** Upstream relied on `SettingsDialog`
    consenting to start the server. With the dialog removed, the
    server is the entire reason the plugin exists, so it must be
    enabled out of the box.
  - **`src/obs-websocket.cpp` -- include `forms/SettingsDialog.h`
    and the `_settingsDialog` global removed, alongside the
    `obs_frontend_*` Tools-menu wiring inside `obs_module_load`.**
    These were applied in Phase 4c but documenting them here too
    since they are part of the runtime story.

  Also: `Qt6::Widgets` and `Qt6::Gui` had to come back into the
  link list (Phase 4b's plan was Core+Network only) -- the source
  pulls in `QSystemTrayIcon`, `QImageWriter`, `QGuiApplication`,
  `QMainWindow`, etc. across `utils/Platform.h`,
  `requesthandler/RequestHandler_Config.cpp`,
  `requesthandler/RequestHandler_General.cpp`,
  `requesthandler/RequestHandler_Sources.cpp`,
  `requesthandler/RequestHandler_Ui.cpp`. The widgets are never
  instantiated at runtime in headless mode (no menu, no UI
  request paths exercised) but the headers must compile and the
  symbols must link.

  Validated end-to-end: `pulsar.exe` starts, `obs-websocket.dll`
  loads, `[Config::Load] Existing configuration not found, using
  defaults.`, `(FirstLoad) Generating new server password.`,
  `obs_module_post_load: WebSocket server is enabled, starting...`,
  and `netstat` shows `0.0.0.0:4455 LISTENING` + `[::]:4455
  LISTENING`.

  Phase 4e: validate v5 round-trip from an external client.
- **Phase 4e -- v5 round-trip validated.** A v5 client
  (`scripts/probe-websocket.py`, ~140 lines, depends on the
  `websockets` Python package) now successfully connects to
  `ws://127.0.0.1:4455`, completes the obs-websocket v5 handshake
  (Hello -> auth challenge -> Identify -> Identified), issues a
  `GetVersion` request, and receives a full response listing 137
  available v5 requests, the libobs version (32.1.2), the
  obs-websocket version (5.7.3), platform info, and supported
  image formats. Disconnection is clean. Pulsar speaks v5 end-to-
  end.

  One additional fork patch was needed: `WebSocketServer._obsReady`
  defaults to `true` (vs `false` upstream). The upstream gate is
  flipped by `OBS_FRONTEND_EVENT_FINISHED_LOADING`, fired by the
  Qt frontend when its event loop has settled. With no frontend in
  Pulsar the event never fires, the gate never opens, and every
  request returned `RequestStatus::NotReady` (code 207). Pulsar
  has no "loading phase" to wait for -- libobs is initialised by
  pulsar-headless before the websocket server starts accepting
  connections, so requests are valid from the first `Identify`.
- Top-level `CMakeLists.txt` rewritten as a real build entry: the
  Pulsar root project now adds `plugins/pulsar-headless/` (via
  `PULSAR_BUILD_HEADLESS=ON` default) and reserves slots for
  `pulsar-websocket` (Phase 4) and `pulsar-multi-stream` (Phase 4)
  plugins. Top-level configure happens AFTER upstream finishes its
  own build — this CMakeLists does not build upstream/.
- `scripts/build-win.ps1` extended with a Pulsar-side build stage:
  after upstream's RelWithDebInfo build completes, configures and
  builds the top-level Pulsar CMake project. Output `pulsar.exe`
  lands next to `obs.dll` in
  `upstream/build_x64/rundir/RelWithDebInfo/bin/64bit/` so the
  Windows loader resolves libobs without touching `PATH`.
