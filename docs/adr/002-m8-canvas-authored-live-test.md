# ADR 002 — M8: end-to-end live test driven by a Canvas-authored (+Blue) scene

- **Status**: accepted
- **Date**: 2026-06-08
- **Decided**: 2026-06-08
- **Deciders**: @ClodoCapeo (maintainer), Vigil (review), Bastion (security clearance)
- **Author**: Atlas (architect agent)
- **Supersedes**: —
- **Superseded by**: —

---

## 1. Context

The repo just merged a Twitch scene-switch live smoke test that broadcasts two
**static HTML scenes** (`scene-a.html` / `scene-b.html`). The maintainer wants a
*real* test: a Twitch live where the on-air frame is a scene **actually authored
by the platform's own services** — layout by ZabCanvas, logic components by Blue,
state pushed by Orion, rendered by Solar inside Pulsar's CEF, broadcast by Pulsar.

The pipeline already exists and is wired end to end. The precedent is
`Pulsar/scripts/probe-m6-live.py` ("M6"): it broadcasts the **live Solar page
served by Orion** through an SSH tunnel, with a hardcoded viewer show-token and a
fixed `mode=broadcast` URL, pre-flighting a non-blank frame before going live.
M6 proves *a* gateway-backed Solar scene renders and broadcasts. What it does
**not** do is **author its own input**: it points at whatever scene Orion happens
to have active behind the standing tunnel, with a baked-in token. There is no
proof that *the bytes on screen are a fresh Canvas+Blue authoring this test
produced*, and no determinism (token expires 2026-06-04 already in the source;
the active scene is ambient state).

This ADR designs **M8**: a deterministic, reproducible probe that **authors a
fresh Canvas scene (with ≥1 Blue-backed logic component), pushes it through the
real Orion compile, makes it the active show scene, then runs the M6 pre-flight +
broadcast against it** — and *proves* the on-air scene is that authoring, not a
fallback. (M7 — RTMP retry classification — already exists in
`Prism/src/main/broadcast-url.ts`; M8 is the next milestone label, no collision.)

### 1.1 The real flow, cartographied from code

The chain a Canvas+Blue scene takes to the wire Solar consumes — verified, with
the contract files:

| Step | Actor | Contract / file |
|---|---|---|
| 1. Author + store LSML bundle, content-addressed by sha256 hash `H` | Prism | `Prism/src/main/.../lib/lsml/store.ts` → `PUT` ZabCanvas A0 store; hash `H` = `bundle.scene_version` |
| 2. ZabCanvas serves the layout for `H` | ZabCanvas | `GET /canvas/api/v1/layouts/{H}` → `routes/layouts.py` → `layout_adapter.adapt_bundle_to_layout` (LSML bundle adapted to Orion `CanvasLayout`) |
| 3. Save a definition revision (`canvas_version = H`) | ZabCanvas | `POST /canvas/api/v1/scenes/{id}/save` → `routes/scenes.py` |
| 4. Push the revision through Orion (`lsml_bundle_hash = H`) | ZabCanvas → Orion | `POST /canvas/.../push` → `services/orion_client.push_scene` → `POST /orion/api/v1/scenes/{id}/push` |
| 5. Orion compiles: fetch layout `H`, blueprint, components, compute-manifest → graph + render bundle; adopt-on-verify collapses `scene_version` onto `H` | Orion | `internal/api/scenes_push.go`, `internal/compiler/compile.go`, `http_fetcher.go`; envelope = `internal/compiler/types.go::PushEnvelope` `{canvas_version, blue_blueprint_id?, components[], lsml_bundle_hash?}` |
| 6. Orion loads the scene into the runtime Show | Orion | `scenes_push.go:133 deps.Show.Load(...)` |
| 7. **Set it active** so the live show streams it | Orion | `POST /orion/api/v1/show/active-scene` → `internal/api/show.go::postActiveScene` (operator role) |
| 8. Mint a **viewer show-token** | Prism → ZabAuth | `POST /auth/api/v1/show-tokens` (operator-only mint) → `ZabAuth/routes/show_tokens.py`; viewer JWT, `jti`, default 4 h |
| 9. Compose the Solar live URL | Prism | `Prism/src/main/broadcast-url.ts::getSolarSceneUrl` → `<gate>/orion/static/solar/v{N}/index.html?orion=<wss .../show/stream.lsdp?token=H_view>&mode=broadcast` |
| 10. Solar host parses `?orion`/`?token`/`?mode`, opens the LSDP WS, fetches the content-hashed render bundle, paints | Solar | `Solar/scripts/build-host-html.mjs` |
| 11. ZabGate accepts the viewer `?token=` on the show-stream path and routes to Orion | ZabGate | `auth/query_string.py` — allow-listed on **both** `/orion/api/v1/show/stream` (bespoke) and `…/stream.lsdp` (LSDP) |
| 12. Pulsar CEF browser_source renders the Solar page; pre-flight asserts non-blank; broadcast to Twitch | Pulsar probe | `scripts/probe-m6-live.py` (the M8 probe derives from this) |

**The active-scene gap (key finding).** No service in the repo calls
`POST /orion/api/v1/show/active-scene` (`grep`: only `broadcast-url.ts` mentions
the concept, in a comment). Solar paints **Orion's active-scene state**, and on
boot Orion seeds the active scene from declared defaults (Orion criterion 11), not
from the most recent push. So a freshly pushed scene is **loaded but not
necessarily active**. M8 must drive step 7 explicitly, or it cannot prove *its*
scene is the one on air.

### 1.2 The wire-mode gap (second key finding — a deploy bloquant)

Prism's `getSolarSceneUrl` hardcodes the **`.lsdp`** wire (`…/show/stream.lsdp`).
That route is registered by Orion **only in `dual`/`lsdp` mode**
(`internal/api/public.go:81` — `LSDPHandler` is nil in bespoke). Orion's config
defaults `ORION_LSDP_MODE` to **`bespoke`** (`internal/config/config.go:41,111`),
and the deployed `.env.orion` (étage 1) **does not set the var** → the LSDP route
is absent on a default-config Orion → Solar's WS 404s at the gateway → blank frame
→ no go-live. Two consequences:

- **Either** the deployed Orion must run `ORION_LSDP_MODE=dual` (Keeper concern,
  out of my périmètre — flagged in §5/§7),
- **or** the probe targets the **bespoke** wire `…/show/stream` (which ZabGate
  *also* accepts the viewer token on). The M8 probe MUST be wire-mode aware: it
  reads which wire to use rather than assuming `.lsdp`.

The Canvas→Orion **push** path itself is mode-independent: ZabCanvas serves
`/layouts/{H}` from its own A0 store regardless of Orion's LSDP mode, and Orion's
bespoke render-bundle serve (`/show/stream` + `/render-bundle`) works in every
mode. Only the *wire Solar subscribes to* is mode-gated.

## 2. Decision drivers

- **Authenticity over convenience.** The test's value is proving *Canvas+Blue →
  Orion → Solar → Pulsar → Twitch* with the platform's own authoring, not a
  hand-fixtured URL. The scene must be produced *by this test*.
- **Determinism + reproducibility.** No reliance on ambient Orion active-scene,
  no baked expiring token, no "whatever is behind the tunnel". Same inputs → same
  on-air scene, runnable in CI and by hand.
- **Reuse the proven M6 broadcast core.** The CEF spawn/reap, non-blank
  pre-flight, RTMP metrics, secret redaction are done and reviewed. M8 adds an
  **authoring + activation prologue**, not a new broadcaster.
- **Provenance, not just non-blank.** M6 asserts "a non-blank frame". M8 must go
  further: assert the on-air pixels correspond to *the scene this test pushed*
  (the fallback/wrong-scene trap).
- **Gateway-first, secret hygiene unchanged.** Every call via ZabGate; the Twitch
  key and the show-token never logged (M6 redaction invariants carry over).

## 3. Decision

**Go.** Build **M8** as a new probe `Pulsar/scripts/probe-m8-canvas-live.py`,
derived from `probe-m6-live.py`, that authors → pushes → activates a deterministic
Canvas+Blue scene and then runs the M6 pre-flight + broadcast against the live
Solar URL **it composed itself**. The Canvas/Blue authoring + push orchestration
that the probe cannot reach (it is a thin WS-to-Pulsar client, gateway-first HTTP
is a separate concern) is provided by a **Python authoring helper** the probe
calls, OR — preferred — by reusing Prism's existing `pushSceneToOrion` chain
exposed as a headless CLI. §3.4 picks.

### 3.1 Test topology — VERDICT: VPS via tunnel (M6 parity), local compose as fallback

| Option | For | Against | Verdict |
|---|---|---|---|
| **A. VPS via SSH tunnel** (M6 model) | Real deployed stack, exact prod contracts, M6 already proves the tunnel + CEF path | ZabCanvas **not in the VPS seed list** (blue/orion/quasar/zabauth/zabgate/zablab/zabranking/zabtruth) — must confirm it is deployed; Orion LSDP mode must be `dual` | **CHOSEN as primary**, conditional on the two Keeper confirmations (§5/§7) |
| **B. Local `docker compose`** (Canvas+Blue+Orion+ZabGate+ZabAuth + their PGs) | Hermetic, no VPS state, full control of `ORION_LSDP_MODE`, no dependency on ZabCanvas being deployed | Five services + 3 PGs to stand up; not the prod contract; more moving parts in CI | **CHOSEN as fallback / CI-hermetic variant** |

**Hypothesis (explicit, non-blocking):** ZabCanvas *is* reachable on the VPS via
ZabGate `/canvas/*` even though it is absent from the seed list (the seed list is
about DB fixtures, not service presence; Canvas has a `deploy.yml`). If Keeper
confirms ZabCanvas is **not** deployed, M8 falls back to topology B without design
change — the probe targets a configurable gateway base URL either way. **This is
the single hardest external fact; it is a Keeper confirmation, not a blocker on
writing the design.**

The probe takes `--gateway-url` (default the tunnel'd gateway) and
`--show-stream-path` (`stream.lsdp` | `stream`) so the same code runs against VPS
or local compose, and against either Orion wire mode.

### 3.2 The deterministic Canvas+Blue scene — VERDICT: a dedicated, seeded test scene

A fixed, named test scene authored idempotently — **not** a reused production
scene (which drifts) and **not** an over-rich scene (which makes the
provenance assertion fragile). Minimal-but-real:

- **Layout (Canvas):** a full-frame solid background of a **known, unusual colour**
  (e.g. `#1A9E57`) + a text primitive + **one Blue-backed component**.
- **Blue component (≥1, the brief's floor):** a trivial **pure** compute the
  compiler accepts — e.g. `core.math.add@1` or `core.literal@1` feeding a text
  binding — so the on-air text shows a **deterministic computed value** (e.g.
  `"M8 OK 42"`). This exercises the real Blue fetch (`FetchBlueprint` two-call +
  `FetchComputeManifest`) and the compiler's blueprint validation path, while
  staying pure/bounded (compiler criteria 17/18 satisfied by construction).
- The scene is created with `status=ready` (go-live requires `ready`), authored
  via the real save→store→push chain, then activated.

**Provenance markers (how M8 proves it is THIS scene, not a fallback):**
1. The **known background colour** — the pre-flight analyser (already decodes the
   PNG, computes modal colour) asserts the **modal colour ≈ the test colour**
   within a tolerance, in addition to M6's generic non-blank check. A fallback /
   blank / different scene fails this.
2. The **deterministic Blue-computed text** — optional stronger marker: OCR is
   out of scope; instead the probe asserts a **content signature** (distinct
   colour count + a region-of-interest colour-presence check around the text
   band) consistent with the rendered text. The colour marker (1) is the primary,
   load-bearing assertion; (2) is corroborating.
3. **Round-trip identity** — after push, the probe reads back
   `GET /orion/api/v1/show` and asserts `active_scene_id == the test scene id` and
   the scene's `scene_version == H` (the hash the probe stored). This proves
   server-side that the active show is exactly the pushed authoring, independent
   of pixels.

Markers (3) is the deterministic, non-flaky core; (1) ties the *server state* to
the *on-air pixels*; (2) is best-effort. Resolution criteria (§6) require (3) +
(1); (2) is advisory.

### 3.3 Test sequence

```
SETUP (gateway-first HTTP, operator JWT):
  S1. Ensure test scene exists (idempotent): GET /canvas/scenes?q=<name>;
      create if absent (status=ready), capture scene_id.
  S2. Ensure Blue blueprint exists (idempotent): the trivial pure blueprint;
      capture blue_blueprint_id.
  S3. Author + store LSML bundle for the fixed scene graph → hash H
      (Prism lsml/store chain, or the Python helper). Deterministic graph ⇒
      deterministic H.
  S4. POST /canvas/scenes/{id}/save  (canvas_version=H, blue_blueprint_id) → def_id.
  S5. POST /canvas/scenes/{id}/push  (definition_id=def_id, lsml_bundle_hash=H)
      → assert 200, scene_version == H, diagnostics.errors == [].
  S6. POST /orion/api/v1/show/active-scene {scene_id} → assert 200.
  S7. GET  /orion/api/v1/show → assert active_scene_id == scene_id.   [marker 3]
  S8. Mint viewer show-token: POST /auth/api/v1/show-tokens (operator).
  S9. Compose Solar URL = getSolarSceneUrl(scene_id, showToken, gateway,
      solarVersion, wire=<stream|stream.lsdp>).

PRE-FLIGHT + BROADCAST (reuse M6 core, scripts/probe-m6-live.py):
  P1. Spawn pulsar.exe (-Full build), v5 auth.
  P2. SetCaptureSource(browser_source, Solar URL).
  P3. Poll GetSourceScreenshot until non-blank AND modal≈test-colour.  [markers 1+2]
      → save proof PNG (build/m8-canvas-scene.png). Blank/wrong colour ⇒ NO GO.
  P4. If --preflight-only: stop here (exit 0 on pass).
  P5. CreateDestination(twitch, $TWITCH_STREAM_KEY) → StartDestination → live.
      StartRecord for an offline VOD proof.
  P6. Poll ~25-30 s: active, drop_ratio ≤ 0.05, bitrate, fps.
  P7. Stop + RemoveDestination + reap pulsar. Idempotent, no orphan.

TEARDOWN: leave the test scene in place (idempotent reuse) but
  POST /canvas/scenes/{id}/stop is NOT required (active-scene is Orion-side
  show state, reset on Orion restart). Optionally restore the prior
  active scene captured at S0 (best-effort).
```

### 3.4 The authoring helper — VERDICT: a thin Python setup module beside the probe

The probe is a WS-to-Pulsar client; the SETUP leg is gateway-first HTTP +
LSML emit. Two ways to get the LSML bundle hashed and stored:

| Option | Verdict |
|---|---|
| (a) **Reuse Prism's TS chain** (`pushSceneToOrion`) via a headless Node CLI | Rejected for M8 v1 — pulls Electron/Node build into a Python probe's setup; heavier CI |
| (b) **Python setup module** `scripts/m8_setup.py` that emits the fixed LSML bundle (the bundle shape is small and deterministic for the chosen scene), PUTs it to Canvas A0, then drives save/push/active-scene/mint over `httpx` | **CHOSEN** — keeps M8 a self-contained Python probe; the LSML bundle for the fixed scene is authored once as a checked-in fixture (`scripts/fixtures/m8-scene.lsml.json`) so the hash `H` is stable and reviewable |

The fixture is the **single source of the scene's determinism**: the graph is a
checked-in JSON, `H` is computed from it the same way Canvas/Orion compute it
(`ZabCanvas/src/zabcanvas/services/content_hash.py` is the reference algorithm —
the helper must reproduce it byte-for-byte, or call Canvas's store which computes
it server-side; **prefer the latter** — PUT the bundle, let Canvas return/confirm
`H`, no client-side hash reimplementation). This sidesteps the C4 hash-mismatch
class (Orion `LSML_HASH_MISMATCH`) by never having the probe invent a hash.

### 3.5 What is reused vs new

- **Reused, untouched:** `probe-m6-live.py` CEF spawn/reap, PNG decode,
  `analyse_frame`, broadcast/metrics, secret redaction. M8 imports or forks the
  broadcast core; the non-blank predicate is **extended** with the modal-colour
  assertion (markers 1).
- **New:** `scripts/m8_setup.py` (authoring + push + activate + mint),
  `scripts/fixtures/m8-scene.lsml.json` (the deterministic scene),
  `scripts/probe-m8-canvas-live.py` (orchestrates setup → M6 core), wire-mode +
  gateway-url flags, the provenance assertions, a CI job mirroring the M6 job
  shape (typed skip on LIGHT build, exit 3).

## 4. Consequences

- A repeatable, CI-runnable proof that the **platform's own authoring pipeline**
  (Canvas layout + Blue logic + Orion compile + Solar render) reaches Twitch — the
  strongest integration test the stack has.
- Forces the **active-scene** call into an exercised, documented path (today it is
  dead code from the repo's perspective). Likely surfaces real bugs in
  `postActiveScene` / `Show.SetActive` under a real push.
- Establishes a **checked-in deterministic scene fixture** other tests can reuse.
- Couples M8's green to the deployed Orion's **LSDP mode** (or forces the bespoke
  wire) — makes the mode an explicit, tested operational fact rather than ambient.
- Adds a Python HTTP-setup surface to a previously WS-only probe family — small
  scope creep, contained to `scripts/`.

## 5. Risks

Security-surfaced risks → **Bastion** (do not self-clear). M8 introduces no new
auth primitive; it **exercises** existing ones, so the surface is the union of
M6's plus the authoring leg:

- **R1 — Twitch stream key.** Unchanged from M6: env-only, never logged, redacted.
  → Bastion: confirm M6 redaction invariants hold in the M8 wrapper.
- **R2 — Viewer show-token.** M8 **mints fresh** (`POST /auth/api/v1/show-tokens`,
  operator-only) instead of baking one — strictly better than M6's hardcoded
  expiring token. The minted token rides inside the Solar `?orion=` URL handed to
  Pulsar over loopback WS; redact via `redactSolarUrl` parity in the Python probe.
  → Bastion: confirm the Python probe redacts the token in every log/diag line
  (M6's `redact()` only covers the stream key; the show-token needs its own
  redaction, mirroring `broadcast-url.ts::redactSolarUrl`). **Residual to clear.**
- **R3 — Operator JWT for setup.** The SETUP leg (save/push/active-scene/mint)
  requires an **operator/admin** JWT (Orion `requireOperator`, ZabAuth
  `require_operator`). The probe must source it from étage-1 secrets, never commit
  it, never log it. `.env.orion` currently carries a long-lived `ORION_OPERATOR_TOKEN`
  (exp 2027) — the probe must NOT reuse Orion's service-mint token; it needs its
  own operator credential. → Bastion: how the M8 operator JWT is provisioned +
  scoped (ideally a short-TTL token minted for the test run). **Residual to clear.**
- **R4 — Network exposure in test.** Tunnel (M6 model, loopback-bound) or local
  compose (host-bound PGs). No service gains public exposure for M8. → Bastion:
  confirm the tunnel binds loopback only (M6 parity) and local compose does not
  publish the internal APIs beyond what dev already does.
- **R5 — Writing to a real (VPS) Canvas/Orion.** M8 SETUP **creates/pushes a
  scene and flips the active show scene** on the targeted stack. Against the VPS
  this mutates live show state — a broadcast could be interrupted if one is
  running. → Bastion + Eleven: M8 against VPS must be gated (off-hours / a
  dedicated test channel) OR run against local compose. **Accepted-risk candidate
  to write here once decided.**

Residual risks accepted by the maintainer must be recorded here before merge
(none pre-accepted yet).

## 6. Resolution criteria

Testable, aligned with the M-series convention and the project CLAUDE.md gates.
M8 is resolved when:

1. **Setup determinism.** `m8_setup.py` against a clean target produces the same
   scene `H` on repeat runs (idempotent author/store); `POST /canvas/.../push`
   returns `200` with `scene_version == H` and `diagnostics.errors == []`.
2. **Blue exercised.** The push compiles a scene carrying ≥1 Blue blueprint
   component (`blue_blueprint_id` non-empty, ≥1 `core.*` compute node); Orion's
   compile does **not** emit `COMPILE_FAILED` / `CYCLIC_COMPONENT` /
   `IMPURE_COMPUTE` (the scene is pure/bounded by construction).
3. **Activation proven (marker 3).** `POST /orion/.../show/active-scene` returns
   `200`; `GET /orion/.../show` reports `active_scene_id ==` the test scene id.
4. **Provenance pre-flight (markers 1+2).** Pulsar CEF renders the Solar page
   non-blank **and** the captured frame's modal colour matches the test scene
   background within tolerance; proof PNG saved (`build/m8-canvas-scene.png`). A
   blank or wrong-colour frame ⇒ **NO GO** (no broadcast), with a typed diagnosis.
5. **Broadcast (when not `--preflight-only`).** Destination goes `active=true`;
   `drop_ratio ≤ 0.05` across the poll window; clean stop + reap, no orphan
   pulsar; offline VOD written.
6. **Wire-mode robustness.** The probe runs green against the configured wire
   (`stream.lsdp` when Orion is `dual`/`lsdp`, or `stream` bespoke), selected by
   flag — no hardcoded assumption.
7. **Secret hygiene.** No stream key, show-token, or operator JWT appears in any
   stdout/log/PNG/VOD artefact (grep-asserted in the test wrapper); Bastion
   clearance on R1–R5 obtained.
8. **CI shape.** A CI job mirrors the M6 job: typed **skip (exit 3)** on a LIGHT
   (no-CEF) build; **fail (1)** on any assertion; **pass (0)** only on a confirmed
   provenance pre-flight (broadcast leg gated behind the key being present).
9. **Org gates (CLAUDE.md / git.md §1).** CI green (ruff/mypy/pytest where the
   probe is Python, build, audits, trufflehog); review approved by Vigil; Bastion
   clearance since the change touches tokens/secrets/network surface.

---

## Amendment 1 — 2026-06-08 — Maximal path: LSDP/dual wire + N-blueprint rich scene

**Author:** Atlas. **Status of amendment:** proposed (Vigil flips the ADR to
`accepted` at the end of `/feature`). **Trigger:** the maintainer chose the
*maximal* path for the live test — (a) the **LSDP/dual** wire end-to-end (not the
bespoke fallback §1.2 left open), and (b) a **rich scene = N distinct Blue
blueprints** (not a single blueprint-graph). Two investigations (Conduit on the
contract, Bastion on security) returned the constraints; this amendment records
the decisions they force. It **does not rewrite** §1–§6 — those stay as the
baseline design; this section selects among the options they left open and adds
the new dependencies.

### A1.1 Wire decision — LSDP/dual is now load-bearing (resolves the §1.2 gap)

§1.2 left two ways to make Solar's WS reach Orion: flip the deployed Orion to
`dual`, **or** target the bespoke `…/show/stream` wire. The maintainer picks the
**LSDP/dual** path. Consequences, taken as acquired from Conduit:

1. **Solar v0.1.1 deployed does NOT speak LSDP.** The deployed bundle is the
   pre-adapter Solar (bespoke `v=1` transport, `src/transport/*`). The LSDP code
   exists only at Solar's **untagged HEAD** (the thin `@lumencast/runtime` adapter,
   post-ADR-007-B). Verified: `git diff v0.1.1..HEAD` **deletes** Solar's entire
   `src/transport/` (codec/protocol/reconnect/sequence/ws) plus `src/render/`,
   `src/state/`, `src/modes/`, delegating the wire to `@lumencast/runtime ^0.2.0`.
   The wire dialect the served bundle speaks therefore **changes** at HEAD.
   → The LSDP path **requires releasing a new Solar** (bump + tag + build, Forge)
   and **deploying that bundle** under `ORION_SOLAR_ROOT` (Keeper). Until then,
   `/static/solar/v{N}/index.html` serves a bespoke runtime that cannot subscribe
   to `…/show/stream.lsdp`.

2. **`ORION_LSDP_MODE=dual` is non-regressive** (Conduit, proven): additive,
   mirrors best-effort, bespoke/Prism unaffected. The flip is a Keeper concern
   (`.env.orion` at étage-1 + redeploy), confirmed safe for the deployed stack.

3. The M8 probe **keeps** the `--show-stream-path` flag from §3.1/§6.6. With the
   maximal path the **default becomes `stream.lsdp`**, but the bespoke value
   stays a supported fallback so the probe is not bricked if a target lags on the
   Solar redeploy.

### A1.2 Solar versioning — VERDICT: **0.2.0**, not 0.1.2

The question handed to me: tag the LSDP Solar as `0.1.2` (patch — `mount()` API
is stable) or `0.2.0` (minor — the wire dialect changes). **I tranche `0.2.0`.**

| Driver | 0.1.2 | 0.2.0 |
|---|---|---|
| `mount()`/public embed API surface | stable → argues patch | stable (not the deciding axis) |
| **Wire dialect the served bundle speaks** | bespoke `v=1` → **changes** to LSDP | the change semver must signal |
| Internal architecture | — | whole `src/transport`/`render`/`state` deleted, now a `@lumencast/runtime` adapter |
| `@lumencast/runtime` dependency | absent at v0.1.1 | `^0.2.0`, now load-bearing |
| Observable contract for a consumer (Pulsar CEF / Prism webview) | — | **the protocol on the WS flips** |

**Rationale.** SemVer keys on the *observable contract*, not just the typed export
surface. The `mount()` signature being unchanged is real but it is **not** the
contract that matters here: a consumer that pins Solar gets a bundle that, post-HEAD,
**speaks a different wire** and pulls a new runtime dependency. A patch bump (`0.1.2`)
would silently change wire behaviour under a "bugfix" label — exactly the trap the
LSDP migration must not set. `0.2.0` is the honest signal: minor (pre-1.0, additive
public API, breaking internal/wire reorganisation). It also pairs cleanly with
`@lumencast/runtime ^0.2.0`. **Decision: release `@zablab/solar@0.2.0`.** The
static-serve path is `/static/solar/v0.2.0/*` (immutable, long-TTL — Orion criterion
12), so the new bundle coexists with v0.1.1 rather than overwriting it; the M8 probe
composes its Solar URL against `v0.2.0`.

### A1.3 Rich scene = N distinct Blue blueprints — depends on a NEW envelope schema

The maintainer's "rich scene = N distinct blueprints" collides with a **schema
wall** Conduit found: `Orion/internal/compiler/types.go::PushEnvelope` carries
`BlueBlueprintID string` (**singular**), and `compile.go:55-63` fetches **one**
blueprint. The §3.2 baseline scene (a single trivial blueprint) fits the current
schema; the **rich** scene does not. Extending the envelope to a **list** of
blueprints, propagated through Orion (compile/validator/fetcher) + the Canvas
client + the component↔blueprint binding, is **the breaking structural change**
this amendment depends on. It is **not** designed here — it is large enough to own
its own decision. It is split into a **separate ADR**: **ADR Orion 001 —
Multi-blueprint push envelope** (`Orion/docs/adr/001-multi-blueprint-push-envelope.md`,
status `proposed`). M8's rich-scene fixture (§A1.4) **blocks on that ADR landing**.

The §3.2 single-blueprint scene remains the **valid minimal floor**: if the
multi-blueprint envelope slips, M8 can still ship its provenance proof with one
blueprint (degraded richness, full pipeline coverage). The rich fixture is the
*target*, the single-blueprint fixture is the *fallback* — both author through the
same chain.

### A1.4 Active-scene in dual mode

Conduit confirms `Wire.SetActive` flips **both** wires in dual mode, and
`POST /orion/api/v1/show/active-scene` (§1.1 step 7, the activation gap) is still
unexercised by any service — M8 is its first driver, unchanged from the baseline.
No new design; noted so the dual-wire activation is an explicit tested fact.

### A1.5 Risks — Bastion clearance conditions (supersede the §5 "to clear" markers)

Bastion returned the clearance conditions. These **resolve** the §5 residuals
(R2/R3/R5) with concrete requirements and add the dual-flip surface check (CC-2).

- **PV-1 (pre-veto, ties to §5 R2).** The ancestor probe
  `Pulsar/scripts/probe-m6-live.py:125-133` commits a **viewer JWT in clear** and
  its redaction covers only the Twitch key. The M8 probe MUST: carry **zero token
  in source**, **port `redactSolarUrl`** (from `Prism/src/main/broadcast-url.ts`)
  to strip both `?token=` and nested url-encoded `token%3D`, and **grep-assert**
  no credential substring in any stdout/log/PNG/VOD. *Follow-up (Forge, out of M8
  scope):* the committed M6 token (expired ~2026-06-06) is **revoked + purged**
  from history.
- **CC-1 (resolves §5 R3).** The SETUP operator credential MUST be an **admin,
  short-TTL token, sourced from étage-1**, and **NOT** `ORION_OPERATOR_TOKEN` (the
  long-lived exp-2027 service token). Redacted exactly like the show-token.
- **CC-2 (new, dual-flip surface).** The `dual` flip introduces **no new auth
  surface**: identical gate on both wires, bundle is content-addressed LSML.
  Clearance is **conditioned on Keeper confirming** the LSDP endpoint is
  **gateway-only** and carries the **same access control** as the bespoke wire.
- **R5 (accepted, conditional) — verbatim from Bastion:** « M8 SETUP flippe la
  scène active sur la stack ciblée. Contre le VPS de prod, cela mute l'état show
  live et peut interrompre un broadcast en cours. Sévérité moyenne (impact
  opérationnel). Mitigation : M8-contre-VPS exécuté off-hours ou sur canal de test
  dédié ; défaut = topologie locale hermétique si dispo. Risque résiduel accepté
  par le mainteneur. » Orion VPS is currently **empty** (no live show → present
  risk nil), but the acceptance is recorded now so it holds the day the VPS carries
  a live show.

### A1.6 Resolution criteria — amendments to §6

Additive to §6 (the baseline criteria still hold). M8 on the maximal path is
resolved when, **in addition**:

- **A1-RC-1 (Solar).** `@zablab/solar@0.2.0` is tagged, built, and its bundle is
  served at `/static/solar/v0.2.0/*` on the M8 target; the probe composes its
  Solar URL against `v0.2.0`.
- **A1-RC-2 (wire).** The probe runs green with `--show-stream-path stream.lsdp`
  against a `dual`-mode Orion (the bespoke value remains a supported fallback,
  §6.6).
- **A1-RC-3 (rich scene).** When ADR Orion 001 has landed, the M8 fixture carries
  **N ≥ 2 distinct Blue blueprints** bound to distinct components, and the push
  compiles clean (no `COMPILE_FAILED`). If ADR Orion 001 has **not** landed, M8
  ships with the §3.2 single-blueprint floor and A1-RC-3 is deferred (recorded,
  not failed).
- **A1-RC-4 (security).** PV-1 + CC-1 satisfied; CC-2 confirmed by Keeper; R5
  accepted as written. Bastion clearance obtained on the union.

### A1.7 Open questions — RESOLVED by the porteur (2026-06-08)

The two questions below were genuinely blocking at the time of writing; the
maintainer (@ClodoCapeo) tranched both on 2026-06-08 at acceptance. The original
questions are kept for the record, each followed by its **DECISION**.

1. **Target for the maximal run** (R5 / §3.1): VPS (off-hours / dedicated test
   channel) **or** local hermetic compose? Drives the default `--gateway-url` and
   whether the Keeper Solar-redeploy + dual-flip happen against the VPS or a local
   stack. *Hypothesis if unanswered:* local hermetic compose for the first green,
   VPS as a follow-up gated run.

   **DECISION (porteur, 2026-06-08): the maximal run targets the VPS de prod, and
   the bring-up is CI-automated, not a one-off manual Keeper action.** The default
   `--gateway-url` is the prod gateway (via the M6 tunnel model). Consequences that
   override the hypothesis above:
   - The infra-precondition track **Pulsar #48** (« M8 §1.2 — Deploy precondition:
     Orion LSDP wire mode + ZabCanvas on the target ») must deliver a **deploy CI
     workflow**, not just an ad-hoc Keeper step: it (a) builds + deploys the
     `@zablab/solar@0.2.0` bundle under `ORION_SOLAR_ROOT` on the VPS, and (b)
     flips `ORION_LSDP_MODE=dual` on the deployed Orion (`.env.orion` at étage-1 +
     redeploy). Keeper owns the workflow; the flip stays the non-regressive,
     additive change Conduit cleared (A1.1.2). The reproducibility requirement of
     §2 now extends to the **deploy** itself — the dual/Solar-0.2.0 bring-up is a
     versioned CI artefact, replayable, not tribal.
   - R5 (writing to a real VPS Canvas/Orion, A1.5) is therefore **in force, not
     avoided**: the run mutates live VPS show state. The accepted-risk wording of
     A1.5 R5 governs (off-hours / dedicated test channel; Orion VPS currently empty
     so present risk nil, acceptance recorded for the day it carries a live show).
   - CC-2 (A1.5) — Keeper must confirm the LSDP endpoint is gateway-only with the
     same access control as the bespoke wire — is a **precondition of the deploy
     workflow**, gating Bastion's clearance on the dual surface.

2. **Sequencing of A1-RC-3 vs go-live**: does M8 **wait** on ADR Orion 001 +
   multi-blueprint build before its first broadcast, or ship the
   single-blueprint floor first and upgrade to N-blueprint after? *Hypothesis:*
   ship the floor first (unblocks the broadcast proof), upgrade to rich once the
   envelope lands — keeps the critical path short.

   **DECISION (porteur, 2026-06-08): rich directly — no single-blueprint floor
   first.** The first LSDP broadcast IS the rich N-blueprint scene. This
   **overrides the §A1.3 "floor first" fallback and the hypothesis above**: the
   multi-blueprint envelope (Orion **#50** + Canvas **#30**, per Orion ADR 001) is
   now **on the critical path before go-live**. Consequences:
   - **A1-RC-3 is promoted from deferred-target to a hard go-live gate.** M8's first
     broadcast does not ship until ADR Orion 001 has landed (envelope + compiler +
     Canvas client) and the M8 fixture carries N ≥ 2 distinct Blue blueprints bound
     to distinct components, compiling clean.
   - The §A1.3 / §6 single-blueprint scene remains a **valid degraded fallback only
     if the envelope work slips and the porteur re-authorises a reduced first
     broadcast** — it is no longer the planned first step.
   - Build ordering: Orion #50 and Canvas #30 (multi-blueprint) sequence **ahead of**
     the M8 rich-fixture issue (Pulsar #44) and the broadcast issue (Pulsar #46);
     Pulsar #48 (deploy: Solar 0.2.0 + dual) runs in parallel as the infra track.
