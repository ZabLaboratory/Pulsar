# ADR 004 — GPU hardware encoders (NVENC / QSV / AMF) + capabilities

- **Status**: accepted
- **Date**: 2026-07-03
- **Decided**: 2026-07-03
- **Deciders**: @ClodoCapeo (maintainer)
- **Author**: Atlas (architect agent)
- **Supersedes**: —
- **Superseded by**: —

---

## 1. Context

Pulsar's headless broadcast pipeline hardwires the software x264 encoder.
`plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp:732` calls
`obs_video_encoder_create("obs_x264", ...)` with a fixed parameter set
(`rate_control="CBR"`, `keyint_sec=2`, `preset="veryfast"`, `profile="high"`,
`tune="zerolatency"`). Only `bitrate` is tunable — via `PULSAR_VIDEO_BITRATE`
at boot and via the `pulsar:SetVideoSettings` vendor request live. No hardware
encoder (NVENC / QSV / AMF) can be selected, and no encoder knob beyond bitrate
(preset / profile / rate-control / keyint) is reachable. This is the long-open
Pulsar#102 / HR-1 item that blocks Prism from exposing hardware-encode to the
operator — x264 at 1080p60 burns CPU that competes with the game being captured.

### 1.1 What the 1.1.0 bundle actually ships (diagnostic)

The `@clodocapeo/pulsar-bundle-full` 1.1.0 bundle carries the encoder-detection
probes `obs-nvenc-test.exe`, `obs-qsv-test.exe`, `obs-amf-test.exe` plus the
encoder plugin DLLs `obs-nvenc.dll` and `obs-qsv11.dll` (AMF ships inside
`obs-ffmpeg`). **These are stock upstream-OBS artifacts, not bespoke Pulsar
encoder work.** They are the standard availability probes: each encoder plugin
spawns its `*-test.exe` subprocess at plugin load, and **registers its encoder
types only if the probe passed** —

- `obs-nvenc/nvenc-helpers.c:291` runs `obs-nvenc-test.exe`, populates
  `codec_supported[]`, exposes `nvenc_supported()` / `is_codec_supported()`;
  `nvenc-compat.c` gates `obs_register_encoder(...av1...)` on it.
- `obs-qsv11/obs-qsv11-plugin-main.c:79-104` runs `check_adapters()` and only
  registers `obs_qsv11_v2` et al. when `adapter_count > 0`.
- `obs-ffmpeg/texture-amf.cpp:2547` loads `obs-amf-test.exe` as data before
  registering the AMF encoders.

**Consequence — detection is essentially free.** After `obs_module_post_load`,
`obs_enum_encoder_types()` (`upstream/libobs/obs.h:697`) already yields *exactly*
the encoder ids this machine supports; libobs did the GPU probing at load. Pulsar
does **not** need to run the test binaries itself, parse their stdout, or re-probe
the driver. The probes' presence in 1.1.0 is not evidence that upstream started
the encoder-selection layer — it is the complete stock plugin set. The
selection + control + reporting layer is unbuilt and is this ADR's scope.

### 1.2 The Prism consumer contract is already frozen (#238 / ADR 008 §3.5)

Prism has already landed the consumer side and pinned the wire shape it expects:

- `Prism/src/main/capabilities.ts` defines `PulsarCapabilitiesPayload`
  `{ encoders?: string[]; videoBitrateKbps?: {min,max}; audioBitrateKbps?: number[] }`
  and `CapabilitiesProbe { video, caps }`.
- `Prism/src/main/broadcast-engine.ts:691-726` (`refreshCapabilities`) already
  probes `GetVideoSettings` for `video` and hardcodes `caps: null` with the
  comment *"the emitting C++ layer is upstream HR-1 / Pulsar#102"* — the seam is
  pre-cut, waiting for this ADR's endpoint.
- The registry key `encoder.type` is classed `unsupported` (store refuses writes
  until this layer lands); capabilities may only **narrow, never widen**, the
  static bounds (`capabilities.ts::overlayVideoBitrate` intersects with the
  registry ceiling).

This ADR must therefore **produce**, not redesign, that payload. The Pulsar-side
requests are new; their response fields map 1:1 onto `PulsarCapabilitiesPayload`.

### 1.3 Boot-fixed precedent

`fps` / `width` / `height` are pinned at `obs_reset_video` and cannot change
without a respawn — `pulsar:SetVideoSettings` returns a typed rejection for them
(`plugin-main.cpp:865-871`, `PULSAR_FPS` / `PULSAR_RESOLUTION`). Prism mirrors
this: `broadcast-engine.ts:443` bakes encoder geometry in *at spawn time via env
vars* and commits every encoder knob off-air before spawn (ADR 008 §3.2). The
encoder identity belongs in the same boot-fixed tier.

## 2. Decision drivers

- **No silent crash.** A requested GPU encoder that is absent (no device, stale
  driver, plugin failed to load) must degrade to x264, never abort the spawn.
  The spawn/handshake contract (PRISM-EMBEDDING) is load-bearing for Prism's
  preflight / start / prewarm.
- **Reuse the frozen Prism contract.** Do not reinvent `PulsarCapabilitiesPayload`
  or the `encoder.type` registry semantics — emit what #238 already consumes.
- **Consistency with the boot-fixed tier.** Encoder identity/geometry are chosen
  off-air; live mutation stays limited to bitrate, as today.
- **Backward-compatible protocol.** `pulsar:*` is semver'd (`PROTOCOL.md` §176):
  new requests are a minor bump, no change to existing request shapes.
- **Encode-once / fan-out invariant.** `pulsar-multi-stream` borrows the single
  video encoder the streaming output is wired to (`plugin-main.cpp:226-240`);
  the encoder swap must preserve that single-encoder-shared-by-all-destinations
  model.

## 3. Decision

### 3.1 Encoder selection is boot-time, via env (fallback-safe)

Extend `pulsar-frontend-stub` `setup()` to resolve the video encoder from new
env vars, defaulting to today's exact behaviour:

| Env var | Default | Meaning |
|---|---|---|
| `PULSAR_VIDEO_ENCODER` | `x264` | `x264` \| `nvenc` \| `qsv` \| `amf` \| `auto` |
| `PULSAR_VIDEO_PRESET` | per-encoder default | encoder-specific quality/latency preset |
| `PULSAR_VIDEO_PROFILE` | `high` | H.264 profile |
| `PULSAR_VIDEO_RATE_CONTROL` | `CBR` | `CBR` \| `VBR` \| `CQP` |
| `PULSAR_VIDEO_KEYINT_SEC` | `2` | GOP length (s) |

`PULSAR_VIDEO_ENCODER` values map to the concrete obs encoder id resolved
**against the live `obs_enum_encoder_types()` set** (never a blind string), in a
pinned preference order per family to absorb OBS-version id drift:

- `nvenc` → first available of `jim_nvenc`, `obs_nvenc_h264_tex`, `ffmpeg_nvenc`
- `qsv` → first available of `obs_qsv11_v2`, `obs_qsv11`
- `amf` → `h264_texture_amf`
- `x264` → `obs_x264`
- `auto` → best available in order nvenc → qsv → amf → x264

v0 scope is **H.264 only** (Twitch RTMP target). HEVC/AV1 ids are out of scope.

### 3.2 Fallback is mandatory and typed

Selection resolves through a single guarded path:

1. Map the family to a concrete id present in `obs_enum_encoder_types()`.
   Absent → log `[pulsar-frontend-stub] encoder '<fam>' unavailable on this
   machine, falling back to x264` and select `obs_x264`.
2. `obs_video_encoder_create(id, ...)` returns null → same typed fallback.
3. The x264 fallback path is the current code, unchanged, so a fallback spawn is
   byte-for-byte today's behaviour. **The spawn never fails on encoder choice.**

Per-encoder settings are validated/normalised before create (an unknown preset
for the chosen encoder falls back to that encoder's default, logged) so a bad
env value never reaches `obs_video_encoder_create` as a hard error.

### 3.3 New vendor request `pulsar:GetCapabilities`

Registered in `pulsar-multi-stream` alongside the existing handlers
(`plugin-main.cpp:940-950`). Detection = enumerate `obs_enum_encoder_types()`,
filter to the H.264 streaming families, map obs ids → the short names Prism's
registry uses:

| obs encoder id(s) | reported name |
|---|---|
| `obs_x264` | `x264` |
| `jim_nvenc` / `obs_nvenc_h264_tex` / `ffmpeg_nvenc` | `nvenc` |
| `obs_qsv11_v2` / `obs_qsv11` | `qsv` |
| `h264_texture_amf` | `amf` |

Response (field naming per `PROTOCOL.md` §99 — obs_data snake/camel as the
existing handlers use):

```
GetCapabilities -> {
  encoders: ["x264", "nvenc", ...],          // maps to PulsarCapabilitiesPayload.encoders
  active_encoder: "x264",                      // the family currently bound
  video_bitrate: { min: 200, max: 50000 },     // -> videoBitrateKbps {min,max}
  audio_bitrate: [64,96,128,160,192,224,256,320], // -> audioBitrateKbps[]
  error?: string
}
```

The bitrate window mirrors the bounds `SetVideoSettings` already enforces
(`plugin-main.cpp:881`, `[200,50000]`), so Prism's intersect-only merge is a
no-op today and becomes meaningful only if a hardware encoder reports a narrower
window. `active_encoder` feeds `GetVideoSettings`' extension (§3.4).

### 3.4 `GetVideoSettings` gains `video_encoder`; `SetVideoSettings` unchanged

`on_get_video_settings` (`plugin-main.cpp:809`) adds `video_encoder`
(the active family), `video_preset`, `video_profile` so `GetVideoSettings` is a
complete off-air snapshot. `SetVideoSettings` semantics are **unchanged**: it
keeps rejecting `fps`/`width`/`height`, and it now also rejects `video_encoder`
/ `video_preset` / `video_profile` with the same typed "boot-fixed, respawn to
change" error as fps. **No live encoder swap** — a mid-stream encoder teardown
recreates the whole output binding and is exactly the fragility the boot-fixed
tier exists to avoid.

### 3.5 Client wrapper + Prism seam

- `packages/pulsar-client` gains a `CapabilitiesNamespace`
  (`client.capabilities.get()`) wrapping `pulsar:GetCapabilities`, mirroring the
  existing `VideoNamespace` / `AdaptiveNamespace` (`client.ts:56-69`).
- Prism bumps the bundle, then wires `refreshCapabilities`
  (`broadcast-engine.ts:726`) to populate `caps` from `client.capabilities.get()`
  instead of `null`, and threads `PULSAR_VIDEO_ENCODER` (+ knobs) into the spawn
  env from the unified store's `encoder.type` (flipping it from `unsupported` to
  a real enum bound driven by the reported `encoders` list). This is the mirror
  Prism issue; the wire contract does **not** change — `capabilities.ts` already
  matches §3.3.

## 4. Consequences

- Operators on NVIDIA/Intel/AMD hardware can offload H.264 encode off the CPU,
  chosen off-air in Prism and baked into the spawn — the CPU headroom the game
  needs is freed.
- The x264 default path and the entire live-mutation surface are unchanged; a
  machine with no GPU encoder behaves exactly as today.
- Detection carries zero new runtime cost (libobs already probed at load) and no
  new subprocess management in Pulsar code.
- `pulsar:*` gains one request (minor semver bump); `GetVideoSettings` grows
  additive fields; no existing shape changes → Prism ≤1.1.x clients keep working.
- Encoder identity joins fps/resolution in the boot-fixed tier: changing it is a
  respawn, matching Prism's off-air commit model — no new "live swap" state to
  reason about.

## 5. Risks

- **R1 — GPU encoder unavailable at spawn (device absent / driver stale / plugin
  DLL missing).** Mitigated by §3.2 mandatory typed fallback to x264; RC 3 tests
  it. Residual: an operator who *expects* NVENC silently gets x264 — surfaced,
  not hidden, because `GetVideoSettings.video_encoder` + `GetCapabilities.
  active_encoder` report the *actual* bound encoder, so Prism can show "requested
  nvenc, running x264". **Bastion:** no attack surface (env in, enumerated ids
  only); no secret, no network. Not a security gate — flagged for completeness.
- **R2 — Multi-platform.** The bundle is Windows-only today. NVENC and QSV exist
  on Windows+Linux, AMF on Windows+Linux; macOS hardware encode is VideoToolbox
  (`com.apple.videotoolbox` ids), entirely out of scope. The env→id mapping is
  enumerated against the live set, so a non-Windows build simply reports whatever
  `obs_enum_encoder_types()` yields and falls back cleanly — no platform
  assumption is hardcoded.
- **R3 — Capabilities widening.** A rogue/over-wide `GetCapabilities` payload
  cannot lift Prism's ceiling: `capabilities.ts` intersects every window with the
  static registry bound (ADR 008 §3.4). Reflective only.
- **R4 — Encoder id drift across OBS versions** (`jim_nvenc` vs
  `obs_nvenc_h264_tex`). Mitigated by resolving each family against the live
  `obs_enum_encoder_types()` set through a pinned preference list, never a single
  hardcoded id.
- **R5 — Preset vocabulary differs per encoder** (x264 `veryfast` vs NVENC
  `p1..p7`/`quality` vs QSV `balanced`). §3.2 normalises unknown presets to the
  encoder's default rather than failing create. v0 exposes a conservative,
  validated preset set per encoder; the full matrix is a follow-up.

## 6. Resolution criteria (testable)

1. **RC1 — Selection.** Booting with `PULSAR_VIDEO_ENCODER=nvenc` on an
   NVENC-capable machine binds an `nvenc` family encoder to the streaming output;
   `GetVideoSettings.video_encoder == "nvenc"`. A 30 s Twitch smoke (per ADR 001
   §pattern) produces a valid H.264 RTMP stream.
2. **RC2 — Default parity.** Booting with no new env var creates `obs_x264` with
   the byte-identical settings of today (`CBR`, `keyint=2`, `veryfast`, `high`,
   `zerolatency`); the existing frontend-stub tests pass unchanged.
3. **RC3 — Fallback, no crash.** `PULSAR_VIDEO_ENCODER=nvenc` on a machine where
   `obs_enum_encoder_types()` lacks any nvenc id (simulated) logs the typed
   fallback and boots on `obs_x264`; the spawn/handshake completes and
   `GetVideoSettings.video_encoder == "x264"`. No non-zero exit, no hung spawn.
4. **RC4 — Capabilities shape.** `pulsar:GetCapabilities` returns `encoders`
   (⊇ `["x264"]`), `active_encoder`, `video_bitrate {min,max}`, `audio_bitrate[]`;
   the payload deserialises into Prism's `PulsarCapabilitiesPayload` with no
   field coercion (a `capabilities.ts` fixture test proves the mapping).
5. **RC5 — Live-mutation rejection.** `pulsar:SetVideoSettings { video_encoder }`
   (or `video_preset`/`video_profile`) returns the typed boot-fixed rejection and
   changes nothing; `video_bitrate` mutation still works as before.
6. **RC6 — Client wrapper.** `client.capabilities.get()` round-trips
   `GetCapabilities` and returns the typed shape; a `pulsar-client` unit test
   asserts the wire→typed mapping against a stubbed vendor response.
7. **RC7 — Protocol doc.** `docs/PROTOCOL.md` documents `GetCapabilities` and the
   extended `GetVideoSettings`/`SetVideoSettings` fields; `CHANGELOG.md`
   `[Unreleased]` records the minor bump. CI green (build + compliance).
