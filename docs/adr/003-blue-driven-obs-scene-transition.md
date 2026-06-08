# ADR 003 — M10: Blue-driven OBS program-scene switch with an animated transition

- **Status**: accepted
- **Date**: 2026-06-08
- **Decided**: 2026-06-08
- **Deciders**: @ClodoCapeo (maintainer), Vigil (review), Bastion (security clearance — conditional, veto R7 lifted at design level via Amendment 2)
- **Author**: Atlas (architect agent)
- **Supersedes**: —
- **Superseded by**: —

---

> **Why this ADR lives in `Pulsar/docs/adr/` and is numbered 003.** The mechanism
> this milestone designs is **OBS-native control** — `SetCurrentSceneTransition` +
> `SetCurrentProgramScene` over obs-websocket v5, against two scenes that capture
> physical monitors via `monitor_capture`. Every load-bearing artefact lives in
> Pulsar: the obs-websocket request suite
> (`plugins/pulsar-websocket/src/requesthandler/RequestHandler_Transitions.cpp`),
> the **frontend-stub that must learn to animate a transition**
> (`plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp`), and the probe
> family (`scripts/probe-*.py`). 001/002 are accepted; 003 is the next free number.
> The Blue side gains only a thin new output-type + caller (§3.5) — too small to
> own a Blue ADR, cross-referenced here. **This is a NEW mechanism, not an
> extension of M9** (Blue ADR 001): M9 repaints the **DOM inside a CEF
> browser_source** via an Orion leaf-write; M10 pilots **OBS itself** (program
> scene + native transition). They share no wire.

## 1. Context

The porteur wants a Blue blueprint to drive a scene that **captures physical
screen 1, then switches to screen 2 with an *animated* transition** — on air. This
is the first time the platform asks Blue to control **OBS composition** (which
scene is on the program output, and *how* it cuts), as opposed to controlling the
**web content rendered inside** a single OBS browser_source.

### 1.1 What already exists (verified — taken as acquired, not re-proven)

- **Capture sources are real and creatable.** `monitor_capture` (display capture),
  `window_capture`, `game_capture` are asserted `REQUIRED_KINDS` by
  `scripts/probe-source-kinds.py:63-73`, CreateInput/RemoveInput OK on the full
  build (`probe-source-kinds.py:182-197`). A scene capturing display 1 vs display 2
  is two `monitor_capture` inputs differing only by their `monitor_id` setting.
- **CreateScene + SetCurrentProgramScene work on the full build.**
  `scripts/probe-twitch-scene-switch.py:474-536` creates a second program scene,
  flips to it, and asserts the flip via `GetCurrentProgramScene` — proven live,
  mid-broadcast, at `duration/2` (`probe-twitch-scene-switch.py:599-623`).
- **The obs-websocket transition suite is present and routed.**
  `RequestHandler_Transitions.cpp` implements `GetTransitionKindList` (l.38),
  `GetSceneTransitionList` (l.60), `SetCurrentSceneTransition` (l.144),
  `SetCurrentSceneTransitionDuration` (l.174), `SetCurrentSceneTransitionSettings`
  (l.201), `TriggerStudioModeTransition` (l.277), `SetTBarPosition` (l.304); the
  table is in `RequestHandler.cpp` l.109-117. So the **control surface** for an
  animated transition exists.
- **The Blue→trigger→leaf reactive bridge (M9, Blue ADR 001) is in prod.**
  `POST /blue/api/v1/blueprints/{id}/trigger` (operator/admin) runs the
  interpreter, maps `outputs` onto `__inputs.blue.<slug>.<port>` leaves, and pushes
  them on a scoped service-token WS to Orion (`m9_setup.py:257-301`,
  Blue ADR 001 §3.2). **This bridge drives Solar (DOM in a browser_source). It does
  NOT touch OBS program scenes or transitions.**
- **The prod porteur of the obs-websocket connection is Prism's `BroadcastEngine`**
  (`Prism/src/main/broadcast-engine.ts`). It spawns `pulsar.exe` via
  `@clodocapeo/pulsar-bundle-full` (l.39-56), holds the v5 WS on a session-random
  loopback port, and already issues vendor calls — `pulsar-scene:SetCaptureSource`
  (l.349-361), `pulsar:StartDestination` (l.818) — over `ipcMain.handle("broadcast:*")`
  (l.1000-1034). **No prod code today calls `SetCurrentProgramScene` or any
  transition request** — only the probe harnesses do.

### 1.2 The trou (core finding, code-anchored)

Two distinct gaps, one of them blocking the *animated* part of the ask:

**Gap A — no caller translates a Blue trigger into OBS scene/transition requests.**
The M9 chain ends at a leaf-write that repaints a browser_source DOM. Nothing maps
a blueprint intent to `SetCurrentSceneTransition` + `SetCurrentProgramScene`.
*Designable; this ADR's main subject.*

**Gap B — the headless frontend-stub HARD-CUTS; it does not animate transitions.**
This is the decisive finding. The frontend-stub:
- creates a real `fade_transition` source at boot and tracks it as
  `currentTransition` with `transitionDuration = 300`
  (`pulsar-frontend-stub.cpp:500,532-538`);
- on a program-scene change, `obs_frontend_set_current_scene` does a **raw**
  `obs_set_output_source(0, currentScene)` (`pulsar-frontend-stub.cpp:887-899`) —
  it **never calls `obs_transition_start` / `obs_transition_set`**, so the
  configured fade is *never composited into the output*. The cut is instantaneous.
- `obs_frontend_preview_program_trigger_transition` is a **no-op `{}`**
  (`pulsar-frontend-stub.cpp:271`), and `TriggerStudioModeTransition` itself, even
  after passing its `studioMode` gate (`cpp:263`), routes to
  `obs_frontend_set_current_scene` (`RequestHandler_Transitions.cpp:282-284`) — the
  same hard-cut path. So **studio mode buys nothing visual on the current stub.**

**Consequence:** the obs-websocket *control surface* for transitions exists and is
routed, but on the **headless Prism/probe build** a `SetCurrentProgramScene`
produces a hard cut regardless of the configured transition. To deliver the
*animated* transition the porteur asked for, the frontend-stub must be taught to
**run the active transition through the program output** on a scene change
(`obs_transition_set(transition, fromScene)` + `obs_transition_start(transition,
MOVE_TO, durationMs, toScene)` + bind the transition as output source 0). This is a
**C++ change to the OBS fork**, not a wiring task. It is the load-bearing risk of
this milestone (§5 R1).

### 1.3 Mechanism contrast (why this is not M9)

| Axis | M9 (Blue ADR 001) | M10 (this ADR) |
|---|---|---|
| What moves | DOM inside **one** browser_source | OBS **program scene** (which sources composite) |
| Wire | Blue→Orion leaf-write→LSDP→Solar repaint | Blue→(caller)→obs-websocket v5→Pulsar |
| Animation | CSS/DOM repaint, no OBS transition | **OBS-native transition** (fade/stinger), real compositor blend |
| Capture | web content | **physical monitors** (`monitor_capture` ×2) |
| Orion | central (recompute) | **not involved** |
| Blocking unknown | none (cabled) | **frontend-stub hard-cut (Gap B)** |

## 2. Decision drivers

- **Honour the *animated* in the ask.** A hard cut is the fallback, not the goal.
  The milestone's value is a real compositor transition on air; Gap B must be
  closed or the milestone is descoped to a cut (§3.6).
- **Reuse the one proven OBS scene-switch path.** `probe-twitch-scene-switch.py`
  already creates two scenes and flips them live mid-broadcast. M10 = that, with
  (a) `monitor_capture` sources instead of HTML pages, (b) a transition set before
  the flip, (c) the flip *triggered by a Blue blueprint*, not a timer.
- **One porteur of the OBS connection.** Prism's `BroadcastEngine` already owns the
  v5 WS. A Blue→OBS path must route *through* whoever holds that socket — not open
  a second, competing obs-websocket client.
- **Do not re-route through Orion.** Orion never re-evaluates Blue hot and carries
  no OBS-scene concept; piping an OBS program-scene switch through the leaf/recompute
  machinery would abuse it (§3.4 (B) rejected).
- **Secret hygiene + gateway-first unchanged.** The obs-websocket password lives on
  loopback only; any new network hop (if the caller is not co-located with the
  socket holder) is a new control surface → Bastion (§5).

## 3. Decision

**Go — with one hard precondition (Gap B) and one product question to the porteur
(§4 Q1).** Build **M10**: a Blue blueprint trigger drives an OBS program-scene
switch between two `monitor_capture` scenes, with a native animated transition,
proven live mid-broadcast by a probe in the
`probe-twitch-scene-switch` / `probe-m9-canvas-live` lineage.

The recommended mechanism, justified in §3.4, is **Option (A): Blue emits a typed
"scene-control" trigger output; the obs-websocket socket holder (Prism
`BroadcastEngine`, or the probe harness for the first jalon) consumes it and issues
`SetCurrentSceneTransition` → `SetCurrentProgramScene`.** Orion is not on the path.

### 3.1 Scene setup — `monitor_capture` ×2, harness-authored for the first jalon

Two OBS program scenes, created over obs-websocket (the proven path,
`probe-twitch-scene-switch.py:490-536`):

- `scene-screen-1`: one `monitor_capture` input, `monitor_id` = display 1.
- `scene-screen-2`: one `monitor_capture` input, `monitor_id` = display 2.

`CreateInput(monitor_capture)` is proven creatable (§1.1). The `monitor_id` /
`monitor` setting key and value format is a **spike item** (§4 U1) — the
source-kinds probe creates with empty settings and removes; it never sets a
specific monitor. For the **first jalon, the harness creates both scenes** (façon
`m8_setup` / `m9_setup` — explicit, acceptable, stated here). Authoring these as
**Canvas scenes** is out of scope: Canvas/Orion produce *web* render bundles for a
browser_source, not OBS scene graphs with native capture sources — wiring Canvas to
emit OBS scene definitions would be a separate, large chantier and is **not** taken.

### 3.2 The transition — set it, then flip

Before the flip, configure the animated transition over the existing routed
requests:
- `SetCurrentSceneTransition{transitionName: "Fade"}` (`RequestHandler_Transitions.cpp:144`).
  The stub registers exactly one transition, `"Fade"`
  (`pulsar-frontend-stub.cpp:532`); a stinger needs a registered stinger source —
  out of scope for v1, **Fade is the v1 transition** (§4 Q1 lets the porteur ask
  for stinger).
- `SetCurrentSceneTransitionDuration{transitionDuration: <ms>}`
  (`cpp:174`, clamped 50–20000).
- Then `SetCurrentProgramScene{sceneName: "scene-screen-2"}` — which, **once Gap B
  is fixed**, runs the Fade through the program output. Studio mode + 
  `TriggerStudioModeTransition` is **not** required if `SetCurrentProgramScene`
  honours the current transition (§3.3 decides).

### 3.3 Studio mode? — VERDICT: NOT required; fix the program-scene path instead

`TriggerStudioModeTransition` gates on `obs_frontend_preview_program_mode_active()`
(`RequestHandler_Transitions.cpp:279`) and, on the stub, still ends in the hard-cut
`obs_frontend_set_current_scene` (`cpp:282-284` → `cpp:887-899`), while
`obs_frontend_preview_program_trigger_transition` is a no-op (`cpp:271`). So studio
mode adds a preview/program ceremony **and still does not animate** on the current
stub. The smaller, correct fix is to make the **single** scene-change path
(`obs_frontend_set_current_scene`) composite the active transition. Real OBS does
exactly this on a non-studio program switch. **Decision: no studio mode for v1;
close Gap B in `obs_frontend_set_current_scene` so any `SetCurrentProgramScene`
animates.** (If the porteur later wants TBar/preview control, studio mode becomes a
follow-up — `SetTBarPosition` is already routed, `cpp:304`.)

### 3.4 Where the Blue intent travels — VERDICT: (A) Blue→socket-holder, NOT via Orion

| Option | What it costs | Verdict |
|---|---|---|
| **(A) Blue typed scene-control output → obs-websocket socket holder** | Blue gains a small typed output (`scene_control`); the socket holder (Prism `BroadcastEngine` in prod, probe harness for the jalon) consumes it and issues the two obs-websocket requests. Orion: **0**. Reuses the proven OBS scene-switch path. | **CHOSEN** |
| **(B) Route through Orion (M9-style leaf)** | Orion has no OBS-scene concept; would need a new leaf semantics that means "tell Pulsar to switch program scene", a new Orion→Pulsar control egress, and abuses the recompute/delta model (which targets Solar repaint, not OBS). Orion never re-evaluates Blue hot. | Rejected — net-new Orion surface for a concern Orion does not own; two control models. |
| **(C) Blue opens its own obs-websocket client direct to Pulsar** | A second client competing with Prism's `BroadcastEngine` for the loopback socket; Blue would need the per-session random port + password (`PULSAR_READY` line, `probe-twitch-scene-switch.py:192`), which only the spawner knows. Cross-process, cross-host (Blue is a VPS container; Pulsar is the operator's Windows box) — no network path today. | Rejected — Blue cannot reach a loopback-bound socket on a different machine; duplicates the connection owner. |

**(A) is the only option coherent with "one porteur of the OBS connection"
(§1.1) and "Orion not on the path" (§2).** The honest seam: Blue says *what* should
happen (switch to scene-screen-2 with a fade); the component that already holds the
OBS socket decides *how* and *when* to issue it.

### 3.5 The Blue contract (small, new — cross-referenced, not a separate ADR)

Reuse the M9 `/trigger` endpoint and its `outputs` mechanism (Blue ADR 001 §3.2.1).
A scene-control blueprint emits a **typed output** rather than a leaf value:

```
POST /api/v1/blueprints/{id}/trigger
body: { inputs?: {...} }
-> outputs: { "scene_control": {
       "action": "switch_program_scene",
       "target_scene": "scene-screen-2",
       "transition": { "kind": "fade", "duration_ms": 600 }
   } }
```

- Blue does **not** push this onto an Orion leaf (it is not an `__inputs.blue.*`
  value). Instead, `/trigger`'s response carries it in `outputs`, and the
  **socket-holder** (subscribed to Blue triggers, or polling the response) issues
  the obs-websocket calls. For the **first jalon, the probe harness fires
  `/trigger` and reads `outputs.scene_control` directly**, then drives the OBS
  requests itself — mirroring how `m9_setup.fire_trigger` reads `outputs` today
  (`m9_setup.py:290-301`). Prod wiring into Prism's `BroadcastEngine` is a
  follow-up (§4 U2).
- **Schema/contract definition is a Conduit concern** (§5): the `scene_control`
  output shape is a cross-service contract between Blue (producer) and the socket
  holder (consumer). Conduit owns the typed schema + a contract test.
- Whether Blue gains a dedicated `scene_control` output **type** in its registry,
  or just emits a conventionally-shaped dict under a normal output port, is a
  small Blue design call deferred to the build (§4 U3) — both satisfy this ADR.

### 3.6 Fallback if Gap B is not closed (descope path, explicit)

If the C++ transition-compositing fix (§5 R1) is descoped or slips, M10 ships a
**hard cut** between the two `monitor_capture` scenes — the *exact* mechanism
`probe-twitch-scene-switch.py` already proves, retargeted to monitors and triggered
by Blue. This delivers the screen-1→screen-2 **switch driven by a blueprint** but
**not the animation**. This is a real, demoable result and a valid degraded jalon —
but it does **not** satisfy the porteur's "animation de transition" literally. The
porteur decides (§4 Q1) whether to gate the milestone on the animation or accept
the cut first and animate after.

## 4. Open questions & unknowns

**Product questions — DECIDED by the porteur 2026-06-08 → see `## Amendment 1`.**
Kept here for the audit trail; the binding resolution is in Amendment 1.
- **Q1 (animation vs cut, scope gate).** ~~hard cut vs animated; Fade vs stinger?~~
  **RESOLVED — Amendment 1 §A1.1:** animation is **on the critical path** (close
  Gap B before the demo); the hard cut is a dev-intermediate state, **not** the M10
  deliverable; the v1 transition is a **stinger**, not a fade. §3.6 fallback is
  **demoted** to a dev checkpoint.
- **Q2 (target host & who switches in prod).** ~~Prism button vs VPS-fired trigger?~~
  **RESOLVED — Amendment 1 §A1.3:** the trigger is fired **from the VPS**, not the
  local Prism button. The VPS→operator control path that "does not exist today"
  (§3.4 (C)) is **designed in Amendment 1 §A1.3** (Prism subscribes to the
  `scene_control` leaf on Orion's existing `/show/stream` — pull, no inbound hop).

**Spike / unknowns to lift before build (no design blocker, but build-blocking):**
- **U1 (monitor selection).** The exact `monitor_capture` settings key + value to
  pin display 1 vs display 2 (`monitor_id` GUID? index? `monitor` string?) on the
  Pulsar fork. The source-kinds probe never sets it (`probe-source-kinds.py:188`).
  **Spike:** `GetInputPropertiesListPropertyItems` / create + read settings on the
  full build to enumerate available monitors. Probe owns this spike.
- **U2 (prod wiring of the socket holder).** How `BroadcastEngine` subscribes to /
  is handed a Blue `scene_control` output (new IPC `broadcast:scene-control`? a
  poll of `/trigger`?). Designable but unspecified; first jalon sidesteps it via the
  harness. Conduit + a follow-up issue.
- **U3 (Blue output typing).** Dedicated registry type vs conventional dict (§3.5).
  Small Blue call, deferred to build.
- **U4 (transition honouring after the C++ fix).** Confirm that, post-fix,
  `SetCurrentProgramScene` alone animates (no studio mode) and that the fade is
  visible on the captured CEF frame across the transition window — proven by the
  probe capturing *mid-transition* frames (§6 criterion 5).

## 5. Risks

Security-surfaced risks → **Bastion** (do not self-clear).

- **R1 — C++ change to the OBS fork (frontend-stub transition compositing).**
  *Not a security risk — the load-bearing engineering risk.* Closing Gap B means
  editing `obs_frontend_set_current_scene` (`pulsar-frontend-stub.cpp:887-899`) to
  run the active transition (`obs_transition_set` + `obs_transition_start` + bind
  the transition as output source 0) instead of the raw
  `obs_set_output_source(0, scene)`. This touches the compositing path that feeds
  the encoder; a mistake can blank the output or crash libobs. Must be guarded by
  the probe's existing non-blank + drop-ratio assertions and a clean reap. **This
  is the milestone's primary risk; Q1 decides if it is on the critical path.**
- **R2 — obs-websocket password / new control surface.** The v5 socket is
  loopback-bound with a session-random password surfaced on the `PULSAR_READY` line
  (`probe-twitch-scene-switch.py:192`). The first jalon stays on loopback (no new
  surface). **Any prod path where a non-co-located component (VPS Blue) drives the
  OBS switch is a NEW network/control surface and a NEW credential hop → Bastion
  clearance required** before such a path is built (ties to Q2 / §3.4 (C)).
  **Residual to clear if Q2 chooses a remote driver.**
- **R3 — `scene_control` is a remote-control primitive.** A Blue trigger that can
  switch what is **on air** is a broadcast-control action. The `/trigger` endpoint
  is already operator/admin-gated (Blue ADR 001 R6, `m9_setup.py:270-272`), which
  is the right floor. → Bastion: confirm no lower-privileged path can emit a
  `scene_control` output that reaches the OBS socket; confirm the socket holder
  validates the `target_scene` against a known-scene allowlist (no arbitrary
  scene-name injection into `SetCurrentProgramScene`). **Residual to clear.**
- **R4 — Capturing physical monitors = capturing whatever is on the operator's
  screens.** `monitor_capture` of display 1/2 puts the operator's **actual desktop
  content** on air — a data-exposure surface entirely outside the platform
  (notifications, private windows, secrets on screen). → Bastion + porteur: this is
  inherent to the feature, not introduced by the wiring; document it as an operator
  responsibility / accepted operational risk. Distinct from the platform's own
  authored content (M8/M9) which never showed the raw desktop. **Accepted-risk
  candidate; record once the porteur acknowledges.**
- **R5 — Twitch stream key + show-token hygiene (inherited).** The probe broadcasts
  to Twitch; the M6/M8 redaction invariants (`probe-twitch-scene-switch.py:161-164`)
  carry over verbatim. → Bastion: confirm the M10 probe redacts the key and any
  token in every log line. **Low — reuses proven redaction.**

No residual risk is pre-accepted; the porteur records R4 (and R2 if Q2 picks a
remote driver) before merge.

## 6. Resolution criteria

Testable, aligned with the M-series convention and the org gates
(`docs/rules/git.md §1`). M10 is resolved when:

1. **Two monitor-capture scenes.** The harness creates `scene-screen-1` and
   `scene-screen-2`, each with one `monitor_capture` input pinned to display 1 vs
   display 2 (U1 resolved); `GetInputSettings` confirms distinct `monitor` targets;
   `CreateInput`/scene creation succeed on the full build (a LIGHT build → typed
   skip, exit 3, per the probe family convention).
2. **Blue drives the switch.** A `POST /blue/api/v1/blueprints/{id}/trigger`
   (operator Bearer header) returns `200` with `outputs.scene_control` carrying
   `{action: switch_program_scene, target_scene, transition}`; the harness reads it
   and issues the obs-websocket requests (no timer-driven switch — the switch is
   *caused by* the trigger, asserted by ordering).
3. **Transition configured.** `SetCurrentSceneTransition{Fade}` +
   `SetCurrentSceneTransitionDuration` return success; `GetCurrentSceneTransition`
   reflects the set transition + duration.
4. **Program scene flips.** Post-trigger, `GetCurrentProgramScene` ==
   `scene-screen-2`; pre-trigger it was `scene-screen-1` (asserted before/after, as
   `probe-twitch-scene-switch.py:582-623` does).
5. **Animation proven (the M10 proof — gated on R1/Q1).** With Gap B closed: the
   probe captures frames **across the transition window** and asserts the program
   output is a **blend** (neither pure screen-1 nor pure screen-2) at
   ~`duration/2 + duration_ms/2`, i.e. the fade is visibly compositing on air — not
   an instantaneous cut. If Q1 descopes to a cut, criterion 5 is **deferred
   (recorded, not failed)** and criterion 4 (the flip) is the proof.
6. **Live, mid-broadcast (doctrine).** The switch fires **during** the broadcast
   (at `duration/2`), not in pre-flight — the VOD shows screen-1 → (transition) →
   screen-2 on air. Destination `active=true`, `drop_ratio ≤ 0.05` across the poll
   window, clean stop + reap (no orphan `pulsar.exe`), offline VOD written
   (`probe-twitch-scene-switch.py:625-688`).
7. **Secret hygiene.** No stream key, show-token, operator JWT, or obs-websocket
   password in any stdout/log/PNG/VOD (grep-asserted); Bastion clearance on
   R2 (if remote driver)/R3/R4/R5.
8. **Contract (Conduit).** The `scene_control` output schema is defined and a
   contract test asserts the producer (Blue) and consumer (socket holder / probe)
   agree on the shape (target_scene, transition.kind, transition.duration_ms).
9. **Org gates.** CI green (the probe is Python — ruff/mypy where applicable; the
   C++ fork change builds full via `scripts/build-win.ps1 -Full`; trufflehog,
   lockfile, CODEOWNERS); Vigil review approved; Bastion clearance since the change
   touches broadcast-control + (conditionally) a new control surface.

---

## Amendment 1 — Porteur decisions locked: stinger, animation-first, VPS-fired trigger

- **Date**: 2026-06-08
- **Author**: Atlas (architect agent)
- **Status of the ADR**: still **proposed** (Vigil flips `accepted`). This amendment
  records the porteur's binding answers to §4 Q1/Q2 and the design the third answer
  forces. It does **not** rewrite §1–§6; it supersedes the §4 questions and §3.6
  fallback, and extends §3, §5, §6 by the sections below.

The porteur made three firm calls. They move the milestone's centre of gravity from
"obs-websocket wiring" to **two real builds**: a stinger-compositing fork change
**and** a brand-new VPS→operator control path. Both are designed here, code-anchored.

### A1.1 Q1 RESOLVED — animation is on the critical path; transition is a **stinger**

**Decision.** Gap B (frontend-stub transition compositing, §1.2 / §5 R1) is closed
**before the demo**. The animated transition is the M10 deliverable; the hard cut is
only a dev-intermediate checkpoint, **not** a shippable jalon. **§3.6 is demoted**:
it remains a useful "does the program-scene flip at all" checkpoint during dev, but
it is **no longer a valid M10 exit** — criterion 5 (§6) is now mandatory, not
deferrable.

**And the v1 transition is a stinger, not a fade.** This is materially more than the
§3.2 fade, and the ADR's original §3.2/§3.3 understated it. Measured against the
fork source:

| Axis | Fade (original §3.2) | **Stinger (decided)** |
|---|---|---|
| Transition source kind | `fade_transition`, already registered (`pulsar-frontend-stub.cpp:532`) | `obs_stinger_transition` — **not registered today**; the stub registers *exactly one* transition (the fade) and tracks a single `currentTransition` (`cpp:474,531-538`) |
| Settings to drive it | duration only (`SetCurrentSceneTransitionDuration`, `RequestHandler_Transitions.cpp:174`) | a `path` (media file) + `transition_point` + `tp_type` via `SetCurrentSceneTransitionSettings` (`RequestHandler_Transitions.cpp:201`) — the request is routed, but the **target source must exist** |
| Media plane | none — pure alpha blend libobs computes | a **video decoder** (the stinger `.webm`/`.mov`) must decode and composite over the program output during the switch window |
| Asset | none | a **stinger media asset** must exist on the operator box and be referenced by `path` |
| Gap B fix scope | run *one* registered transition through the output on scene change | run the **active** transition through the output — same compositing fix, but it must hold for a media-backed stinger source, whose `obs_transition_start` drives a decoder, not just an alpha curve |

**Consequence — Gap B' (stinger).** The §1.2 / §5 R1 fix is unchanged in *shape*
(make `obs_frontend_set_current_scene` composite the **active** transition instead of
the raw `obs_set_output_source(0, scene)` at `cpp:887-899`), but its *acceptance* now
requires a **stinger** active transition to play through the output — i.e. the fix
must be validated against a registered stinger source, not just the fade. The
frontend-stub must additionally **register a stinger transition source** (a second
entry in `transitions`, created `obs_source_create_private("stinger_transition", …)`
with a `path` setting) so `SetCurrentSceneTransition{name:"Stinger"}` resolves. This
is the increment over the fade-only plan; it is folded into issue #57 (re-scoped).

**Stinger asset — who provides it (decided).** For **M10 a demo asset suffices**: a
short royalty-free stinger `.webm` with alpha, checked in under
`scripts/assets/stinger-demo.webm` (or fetched by the harness), referenced by an
absolute `path` the stub can decode. A *production-grade* branded stinger (logo
sweep) is a **post-M10 asset-authoring task** owned by the porteur/design, out of
scope here. The probe pins the demo asset; the contract carries the `path` as an
operator-resolved local file, never a VPS URL (the media stays local to the box that
composites it — see §A1.4 surface notes).

### A1.2 §3.3 revisited under the stinger decision — still no studio mode

The §3.3 verdict (no studio mode; fix the single program-scene path) **holds for the
stinger**. A stinger in real OBS plays on a non-studio program switch exactly as a
fade does — it is the *active transition* run through the output. Nothing about the
stinger needs preview/program ceremony. The Gap B' fix is the same single seam.

### A1.3 §3.4 Q2 RESOLVED — VPS-fired trigger reaches the operator via **Prism subscribing to the `scene_control` leaf** (pull, no inbound hop)

This is the load-bearing new design. The porteur fires the blueprint **from the VPS**
(not the local Prism button). §3.4 (C) correctly noted there is **no network path
today** from a VPS Blue container to the loopback obs-websocket socket Prism holds on
the operator's Windows box. We design that path here, choosing **moindre privilège
réseau** — and we ground it in the existing reactive pipe rather than inventing a new
one.

**The seam already exists and is pull-based.** Three facts from the source decide it:

1. **Blue already holds a long-lived, service-token WS to Orion's `/show/stream`** and
   pushes raw `__inputs.blue.*` **leaf** frames over it
   (`Blue/src/blue/core/orion_client.py` module docstring + `__inputs.blue` write
   path; M9 / Blue ADR 001 §3.2). The WS targets the **gateway** URL (`BLUE_ORION_WS_URL`),
   never `orion:4007` directly; auth is the service token in the `Authorization`
   header.
2. **Orion's `/show/stream` fans every leaf delta to *all* live subscribers**
   (`Orion/internal/ws/server.go:114-149`, `runShowConnection` → `s.Show.SubscribeLive`
   → snapshot + deltas). A subscriber receives **leaf state**, not arbitrary control
   envelopes — exactly the M9 Solar-repaint mechanism. Roles `viewer`,`operator`,
   `service`,`admin` may subscribe (`server.go:50-55`).
3. **Prism already anticipates a main-process live-event consumer.** `animation-bridge.ts`
   (`Prism/src/main/animation-bridge.ts:9-13,56-59`) literally documents a future
   "Blue WS bridge / EventSource pump" that fans Blue live events into the renderer.
   And `broadcast-engine.ts` already composes and subscribes to the Orion show-stream
   URL for the Solar capture path (`broadcast-url.ts:61-64`, the `wss://…/show/stream.lsdp?token=…`
   inner URL). **Prism already knows how to open an Orion show-stream WS.**

**Decision — (A′): `scene_control` rides the existing leaf pipe; Prism becomes a
`/show/stream` consumer that executes the OBS command locally.**

```
Operator (or scheduler) fires the blueprint  ── on the VPS ──►
  POST /blue/api/v1/blueprints/{id}/trigger            [operator/admin JWT @ ZabGate]
      │  Blue interpreter runs, produces outputs.scene_control
      ▼
  Blue writes a LEAF:  __inputs.blue.scene_control = { action, target_scene, transition{kind,path,point_ms,duration_ms} }
      │  over Blue's existing service-token WS → ZabGate → Orion  (orion_client.py)
      ▼
  Orion /show/stream  ── fans the leaf delta to ALL live subscribers ──►  (server.go:114-149)
      │
      ├──► Solar (CEF, viewer show-token)  — IGNORES scene_control (not a Solar-bound leaf)
      │
      └──► Prism BroadcastEngine  — NEW: subscribes to /show/stream as a SERVICE or VIEWER
               consumer; on a __inputs.blue.scene_control delta it executes LOCALLY:
                 1. SetCurrentSceneTransition{ "Stinger" }            (loopback obs-ws)
                 2. SetCurrentSceneTransitionSettings{ path, transition_point }
                 3. SetCurrentSceneTransitionDuration{ duration_ms }
                 4. SetCurrentProgramScene{ target_scene }            → stinger composites on air
```

**This is pull, not push.** No inbound connection is ever opened toward the operator
box. Prism (the socket holder) **subscribes outbound** to a VPS endpoint it already
reaches through the gateway, and the only thing crossing the network is a **leaf
state delta** — identical in transport and trust to the M9 Solar repaint. The
operator box exposes **no new listening port**.

**Reconciliation with the M9 note "Orion ne pilote pas la scène OBS, il repeint
Solar".** Honoured, and sharpened: **Orion is a dumb fan-out channel** for the
`scene_control` leaf — it stores a leaf value and broadcasts the delta, exactly as it
does for any `__inputs.blue.*` leaf. **Orion still does not interpret OBS semantics.**
The OBS *meaning* of `scene_control` (issue the four obs-websocket requests) is
executed **only by Prism**, the socket holder — never by Solar, never by Orion.
Solar receives the same delta and ignores it (it is not a Solar render input). So the
M9 invariant stands: the payload *transits* Orion as opaque leaf state; the
*semantics* live at the consumer (Prism), not in the runtime. The seam is the same
"Blue says *what*, the socket holder decides *how/when*" honesty from §3.4.

**Why not the alternatives (re-decided for the VPS case):**

| Option | Cost | Verdict |
|---|---|---|
| **(A′) Prism subscribes to the `scene_control` leaf on `/show/stream`** | Reuses Blue's existing service-token push, Orion's existing fan-out, and Prism's existing show-stream client. Net-new: a Blue leaf write + a Prism consumer + the obs-ws executor. **Zero new inbound surface.** | **CHOSEN** |
| (B′) Inbound tunnel / reverse channel to the operator box (ngrok-style, VPS→box push) | Opens a listening surface on the operator's machine for a VPS to push broadcast-control commands. New port, new credential, new exposed control plane on a residential box. | Rejected — maximal network surface for the most sensitive action (what's on air); violates moindre privilège. |
| (C′) Blue opens a direct obs-ws client to Pulsar across the WAN | Same as original §3.4 (C): the socket is loopback-bound on the operator box, the per-session port+password are known only to the spawner (Prism). Cross-host impossible without exposing the loopback socket. | Rejected — unchanged from §3.4 (C). |
| (D′) A dedicated Blue control channel (separate WS, not the leaf stream) | A second push fabric parallel to `/show/stream`, with its own auth, fan-out, reconnect, buffering — re-implementing what `orion_client.py` + `server.go` already do. | Rejected — duplicates a proven pipe; more surface, no benefit. The leaf stream already carries exactly-once-ish ordered deltas with reconnect/replay-snapshot semantics. |

**One hard unknown surfaced (spike, see issue).** Prism's `BroadcastEngine` today is
**command-only** — it *issues* obs-ws vendor calls but **does not subscribe** to any
Orion stream (`broadcast-engine.ts` has no `/show/stream` consumer; only
`broadcast-url.ts` *composes* the URL Pulsar's CEF loads). `animation-bridge.ts` is a
**stub** that stands up only the IPC contract and explicitly defers the transport
("the channel transport is still under design", `animation-bridge.ts:11`). **So a
main-process Orion `/show/stream` subscriber that reads `scene_control` deltas and
drives `BroadcastEngine` does not exist yet — it must be built.** This is a real
chantier, not a wiring tweak. Scoped as **issue #63** (spike + consumer); the stinger
demo asset is **issue #64**.

### A1.4 Auth / surface of the VPS→operator control path (for Bastion)

End-to-end identity & authorisation chain for one `scene_control` switch — the new
**broadcast-control** path. Bastion threat-models this next; this section is the map.

1. **Trigger authorisation (VPS ingress).** `POST /blue/api/v1/blueprints/{id}/trigger`
   is **operator/admin-gated at ZabGate** (Blue ADR 001 R6; M9 `m9_setup.py:270-272`).
   The right floor: only an operator/admin JWT can fire a blueprint that emits a
   `scene_control` output. **No lower-privileged path may produce one** — Bastion to
   confirm no viewer/anon trigger route exists.
2. **Leaf-write authorisation (Blue→Orion).** Blue writes `__inputs.blue.scene_control`
   on its **service token**, whose `paths` claim scopes it to `__inputs.blue.*`
   (`Orion/internal/auth/identity.go:82-96`, `CanWritePath` → `RoleService` prefix
   match). The write only crosses via **ZabGate**, which injects
   `X-Authenticated-Role: service` (Orion never re-validates — `server.go:9-12`).
   `__inputs.blue.scene_control` falls **inside** the existing `__inputs.blue.*`
   grant → no new Orion scope is needed. Bastion to confirm the service-token grant
   is not *broadened* and that `scene_control` cannot be written by a non-Blue
   identity (operator JWT can write anywhere — §A1.4(5)).
3. **Subscription authorisation (Orion→Prism).** Prism subscribes to `/show/stream`
   as a **service or viewer** role. Viewer/operator/service/admin may all subscribe
   (`server.go:50-55`); **subscription is read-only** — a subscriber cannot write
   leaves except via the same `CanWritePath` gate. **Decision: Prism subscribes with
   a least-privilege credential** — a **viewer show-token** (the same kind Prism
   already mints for the Solar capture, `broadcast-engine.ts:945-972`) is sufficient
   to *receive* deltas and **cannot write back**. Bastion to confirm a viewer token
   can subscribe to `/show/stream` and that read-only is enforced.
4. **Local execution authorisation (Prism→Pulsar).** The four obs-ws requests run on
   the **loopback** socket with the session-random password Prism already holds
   (`broadcast-engine.ts` owns the `SpawnedPulsar` client). **No new credential, no
   new port** on the operator box. The control command arrived as inbound *state*,
   but is *executed* on the pre-existing loopback trust.
5. **Target validation (anti-injection).** Before issuing `SetCurrentProgramScene`,
   the Prism consumer **must validate `target_scene` against a known-scene allowlist**
   (the two scenes the harness/operator created) — no arbitrary scene-name from a leaf
   value flows into the obs-ws call. Same for the stinger `path`: it must resolve to a
   **pinned local asset**, never an arbitrary path from the leaf (an attacker-influenced
   `path` = arbitrary local file read into the media decoder / SSRF-to-disk). Bastion
   owns confirming both allowlists. (This is the §5 R3 concern, now concrete for the
   VPS path.)

### A1.5 §3.6 fallback — DEMOTED

The original §3.6 hard-cut fallback is **no longer an M10 deliverable** (A1.1). It
survives only as a **dev checkpoint** ("does the program scene flip at all, pre-Gap-B'").
M10 does not exit on a cut.

### A1.6 New & re-scoped risks (extends §5)

- **R1′ (supersedes R1) — stinger compositing, not just fade.** Gap B' is R1 plus a
  **media-decoder transition**: the fork must register a `stinger_transition` source
  and run it through the program output. A decoder fault can stall or blank the
  output mid-switch — wider failure surface than the alpha-only fade. Guarded by the
  probe's non-blank + drop-ratio assertions across the **stinger** window. *Engineering
  risk, not security.* Now firmly on the critical path (A1.1).
- **R6 (new, security) — a new broadcast-control channel reachable from the VPS.**
  `scene_control` is a remote primitive that changes **what is on air**, now fired
  **from the VPS** (not a local button). Even though transport is pull (no inbound
  hop), a compromised Blue / a forged `__inputs.blue.scene_control` leaf / an
  over-broad service-token grant could switch the program scene of a live broadcast.
  Mitigations: operator-gated trigger (A1.4.1), `__inputs.blue.*`-scoped service token
  (A1.4.2), read-only viewer subscription (A1.4.3), **target_scene allowlist** +
  **pinned stinger `path`** at the consumer (A1.4.5). → **Bastion clearance required;
  residual to record.**
- **R7 (new, security) — the stinger media asset is *executed* (decoded) on air.** The
  stinger `path` points at a media file the libobs decoder runs during every switch.
  An attacker-controlled `path` or a malicious media file is a code-exec/decoder-fuzz
  surface on the operator box, and a path-traversal/local-file-read vector. Mitigation:
  the `path` is a **pinned local asset**, never taken from the leaf value (A1.4.5);
  the demo asset is checked in and hash-pinned. → **Bastion: confirm the consumer
  never honours a leaf-supplied `path`.**
- **R4 reaffirmed — physical monitor capture = real desktop on air.** Unchanged from
  §5 R4, but now **certain** (the milestone ships the animation, so the capture is
  real, not hypothetical). Operator-responsibility accepted-risk; porteur to record.

### A1.7 Resolution criteria delta (extends §6)

- **§6 criterion 5 is now MANDATORY** (was "deferrable to a cut"). The probe must
  prove the **stinger** composites on air mid-transition — frames across the switch
  window show the stinger media (neither pure screen-1 nor pure screen-2) at the
  transition point. No cut-only exit.
- **New criterion 10 — VPS-fired, pull-delivered.** The blueprint is fired by a
  `POST …/trigger` **call distinct from the box running OBS** (the probe simulates the
  VPS origin); the switch reaches Prism's obs-ws **only** through a `/show/stream`
  `__inputs.blue.scene_control` leaf delta the Prism consumer reads — asserted by:
  (a) no inbound port opened on the operator box, (b) the obs-ws calls are caused by
  the leaf delta (ordering), (c) Solar receives the same delta and does **not** switch
  the program scene.
- **New criterion 11 — anti-injection.** A `scene_control` leaf with an unknown
  `target_scene` or an off-allowlist `path` is **rejected by the consumer** (no obs-ws
  call issued); asserted by a negative test.
- **Criterion 7 (secret hygiene)** extends: the viewer show-token Prism uses to
  subscribe is redacted in every log line (reuse `redactSolarUrl`,
  `broadcast-engine.ts:346-350`).

---

## Amendment 2 — Bastion clearance: leaf carries no `path` (veto R7 lifted), canonical leaf path corrected, scene-declaration constraint pinned

- **Date**: 2026-06-08
- **Author**: Atlas (architect agent)
- **Status of the ADR**: still **proposed** (Vigil flips `accepted` once the veto is
  cleared at design level). This amendment integrates the corrections Bastion's
  threat-model demanded at the `/feature` stage. Bastion returned a **conditional
  clearance with one targeted VETO (R7)**; this amendment removes the vetoed
  construct from the design so Vigil can flip `accepted`. It does **not** rewrite
  §1–§6 nor Amendment 1; it **supersedes the leaf shape** of §A1.3 (the block at
  l.463 and the executor steps l.473), **corrects the canonical leaf path** stated
  in §A1.3/§A1.4(2), **adds a fan-out precondition**, and **records three residual
  risks verbatim** plus the per-condition issue mapping.

### A2.1 VETO R7 lifted — the `scene_control` leaf carries **no `path`**; only an allowlisted `asset_id`

**The vetoed construct.** §A1.3 (leaf block l.463 and executor step 2, l.473) made the
`scene_control` leaf carry `transition.path` — a media file path — which the Prism
consumer would feed into `SetCurrentSceneTransitionSettings`. Because the leaf
originates on the VPS (Blue writes it, Orion fans it out), a consumer that honours
that `path` opens a **local-file-read + media-decoder-fuzz primitive at the antenna**
on the operator's Windows box: a leaf carrying `\\attacker\share\evil.webm` (UNC
network path), `C:\Users\…\id_rsa`, or any traversal would be opened and decoded by
libobs on air. This directly contradicts §A1.4(5) ("pinned local asset, never from
the leaf"). **The contract carrying a free `path` is the single blocking item.**

**Correction (this supersedes the §A1.3 leaf shape).** The `scene_control` leaf
carries **no `path` field at all**. At most it carries an **`asset_id`** — an opaque
key validated at the consumer against a **fixed allowlist** of known stinger assets.
The **real media path is resolved 100 % locally** at the consumer (Prism), pinned,
and **never** read from the leaf. The new leaf shape (replaces the l.463 block):

```
__inputs.blue.<slug>.scene_control = {
    "action": "switch_program_scene",          // allowlisted verb (only this value)
    "target_scene": "scene-screen-2",          // validated against the 2-scene allowlist
    "transition": {
        "kind": "stinger",                     // allowlisted enum (stinger | fade)
        "asset_id": "stinger-demo",            // allowlist KEY only — NOT a path
        "point_ms": 300,                        // transition_point, integer, bounded
        "duration_ms": 600                      // clamped 50–20000 by SetCurrentSceneTransitionDuration
    }
}
```

The consumer executor (replaces the l.473 step 2) becomes:

```
on __inputs.blue.<slug>.scene_control delta:
  1. assert action == "switch_program_scene"                    (C-INJ; else 0 obs-ws calls)
  2. assert target_scene ∈ {scene-screen-1, scene-screen-2}     (C-INJ; else 0 obs-ws calls)
  3. assert transition.asset_id ∈ ASSET_ALLOWLIST               (C-PATH; else 0 obs-ws calls)
  4. local_path = ASSET_ALLOWLIST[asset_id]                     ← pinned local file, resolved at the consumer
  5. SetCurrentSceneTransition{ "Stinger" }                     (loopback obs-ws)
  6. SetCurrentSceneTransitionSettings{ path: local_path, transition_point: point_ms }
  7. SetCurrentSceneTransitionDuration{ duration_ms }
  8. SetCurrentProgramScene{ target_scene }                     → stinger composites on air
```

`ASSET_ALLOWLIST` is a `{asset_id → absolute local path}` map pinned in the Prism
consumer (#63), keyed only on the demo asset (#64) for v1. **No leaf value ever
reaches an `obs_*` media-open call as a path.** This is what lifts the veto.

**Coherence check with #57 (fork side) — verified, no hypothesis broken.** Removing
`path` from the leaf does **not** strand the stinger. Issue #57 already registers the
stinger transition source on the fork with its **own** `path` setting pointing at the
**pinned demo asset** (`obs_source_create_private("stinger_transition","Stinger",
settings)` with a `path` setting → #64), resolved **locally on the operator box**, not
from any leaf. So the media path is local-by-construction on both planes: the fork
registers it from the pinned asset (#64), and the Prism consumer resolves
`asset_id → local_path` from the same pinned asset. The leaf's `asset_id` only *selects
which* allowlisted asset, never *supplies* a path. The `asset_id → path` indirection is
therefore fully coherent with what #57 needs to register the stinger transition; no
contract gap is introduced.

### A2.2 F1 — canonical leaf path corrected to `__inputs.blue.<slug>.scene_control`

§A1.3 (leaf block, §A1.4(2), and the executor) wrote the leaf as
`__inputs.blue.scene_control` (2 segments). **This is inexact.** The `leaf_mapper`
forces every output onto `__inputs.blue.<slug>.<port>` (3 segments) and
`_SEGMENT_RE = ^[a-z0-9][a-z0-9_-]*$` **rejects `.` inside a segment**
(`Blue/src/blue/services/leaf_mapper.py:38,133,147,160`); `_resolve_override_leaf`
re-anchors any override to `__inputs.blue.<slug>.<last-segment>` server-side
(`leaf_mapper.py:99-123`). So an operator **cannot** make Blue write a 2-segment
`__inputs.blue.scene_control`; the real path is always
**`__inputs.blue.<slug>.scene_control`** where `<slug>` is the scene-control
blueprint's own stable slug.

**Correction.** Everywhere this ADR named `__inputs.blue.scene_control`, read
**`__inputs.blue.<slug>.scene_control`**. The Prism consumer (#63) and the
anti-injection allowlist anchor on **that** path. **Positive security consequence:**
the write is structurally confined to the blueprint's own subtree
(`trigger.py:228` — "a binding can never write outside
`__inputs.blue.<its-blueprint-slug>.*`"); a crafted port/override collapses to the
last segment under this slug (`leaf_mapper.py:121-123`). Blast radius is one
blueprint's subtree, not the whole `__inputs.blue.*` namespace.

### A2.3 F2 — fan-out requires the active scene to **declare** the path

`Inbox.Write` fans a leaf delta out **only** to scenes that declare the path in
their `defaults`, `operator_inputs`, or `bindings.target_paths`; a path no loaded
scene declares is **silently dropped** (`Orion/internal/adapters/inbox.go:51-54,
78-87, 102-124` — `sceneAcceptsPath`). Therefore
`__inputs.blue.<slug>.scene_control` reaches Prism's `/show/stream` subscription
**only if the active M10 scene declares that exact path**.

**Constraint added to the design.** The M10 scene created by the `m10_setup` harness
(#60) **MUST declare** `__inputs.blue.<slug>.scene_control` as an
`operator_input` (or a binding `target_path`), or the delta is silent-dropped and
never reaches Prism. The end-to-end probe (#61/#63) **must prove the delta reaches
the consumer without silent-drop**. This is a Resolution criterion of **#60** (scene
declares the path) and **#63/#61** (C-FANOUT, no silent-drop end-to-end).

### A2.4 §risques — three residual texts recorded verbatim (Bastion)

> **R6 (résiduel accepté) — primitive de contrôle d'antenne télécommandable depuis le VPS.** Un opérateur/admin légitime, un Blue compromis, ou un opérateur écrivant le leaf en direct (`CanWritePath` = true pour operator/admin, `Orion/internal/auth/identity.go:82-85`) peut basculer la scène program d'un live. Inhérent à un primitive de remote-control ; non éliminable. Accepté sous mitigations : trigger operator-gated (`trigger.py:81-94`), service-token scope-pinné `__inputs.blue.*` fail-closed (`config.py:99-117`), abonnement Prism read-only (viewer token), allowlist `action`+`target_scene` au consumer (#63), rate-limit `/trigger` (429). Confiance haute. Repose sur la segmentation réseau : Orion joignable via ZabGate uniquement (C-NET).

> **R7 (résiduel — conditionnel à C-PATH) — asset stinger décodé à l'antenne.** Le média stinger est décodé par libobs à chaque switch. Le `path` est épinglé local côté consumer et jamais lu du leaf ; le leaf ne porte au plus qu'un `asset_id` d'allowlist (#63) ; asset démo sha256-pinné (#64). Risque résiduel : fuzz du décodeur sur un asset local malveillant — borné par le pinning + checksum. Tant que le contrat porte un `path` libre, non accepté (veto).

> **R4 (résiduel accepté — responsabilité opérateur) — capture moniteur = bureau réel à l'antenne.** `monitor_capture` des écrans 1/2 met le bureau réel de l'opérateur (notifications, fenêtres privées, secrets affichés) sur la VOD publique Twitch. Inhérent à la feature, hors périmètre platform-code. Accepté : responsabilité opérationnelle de l'opérateur (écran propre avant go-live). Le porteur acte ce risque.

**Veto status.** With A2.1 in force (the contract carries `asset_id`, not a free
`path`), the **R7 condition C-PATH is met at design level** → the veto is **lifted at
design level**. R7 survives as the residual above (decoder fuzz on a *pinned* asset,
bounded by checksum), conditional on C-PATH being enforced at the consumer (#63) and
the asset being sha256-pinned (#64). R6 and R4 are accepted residuals — R4 acted by
the porteur. No residual is implicit; all three are recorded here.

### A2.5 Bastion clearance conditions → issue mapping

Each clearance condition is anchored to the issue that carries it as a Resolution
criterion:

| Condition | Meaning | Carried by issue(s) |
|---|---|---|
| **C-PATH** | The consumer honours **no** `path` from the leaf; leaf carries at most an allowlisted `asset_id`; real path resolved locally. **Lifts the veto.** | **#63** (consumer), **#58/#59** (leaf/contract has no `path`), **#64** (pinned local asset) |
| **C-INJ** | `action` allowlisted (only `switch_program_scene`); `target_scene` ∈ {the 2 scenes}; off-allowlist leaf ⇒ **0 obs-ws calls** (negative test) | **#63** (consumer), **#61** (probe negative test) |
| **C-PATHREAL** | Consumer matches the **real** path `__inputs.blue.<slug>.scene_control` (F1), not the 2-segment form | **#63** (consumer), **#58** (Blue writes that path), **#59** (contract pins it) |
| **C-FANOUT** | End-to-end leaf delta reaches the consumer **without silent-drop** (F2): active scene declares the path | **#60** (scene declares the path), **#61/#63** (end-to-end proof) |
| **C-SEC** | The 2nd subscription URL `/show/stream` carrying the viewer-token is redacted in **every** log line; `PULSAR_READY` password redacted | **#63** (consumer logging), **#61** (probe grep-assert) |
| **C-NET** | Orion reachable via **ZabGate only**; **no inbound port** opened on the operator box (pull-only) | **#62** (Bastion clearance), **Keeper/Conduit** (network topology), **#63** (outbound-only consumer) |
| **C-SCANS** | `trufflehog` + `pip-audit` (Blue) + `npm audit` (Prism) green on the **real diff**; lockfiles pinned | **CI on #57/#63/#64** |

### A2.6 Issue Resolution-criteria deltas (Bastion → issue)

- **#58 / #59 (Blue output + Conduit contract).** The `scene_control` schema carries
  **no `path`** — `transition.asset_id` (allowlist key) only, plus
  `kind/point_ms/duration_ms`. Round-trip producer (Blue) → leaf → consumer (Prism)
  agrees on the shape. Canonical path is **`__inputs.blue.<slug>.scene_control`**
  (C-PATHREAL); the contract test asserts that exact 3-segment path. (C-PATH,
  C-PATHREAL)
- **#63 (Prism consumer).** C-INJ (only `switch_program_scene`; `target_scene`
  validated against the 2-scene allowlist; off-allowlist leaf ⇒ **0 obs-ws calls**,
  negative test). C-PATH (no `path` honoured from the leaf; `asset_id → local_path`
  via the pinned `ASSET_ALLOWLIST`). C-PATHREAL (matches
  `__inputs.blue.<slug>.scene_control`). C-FANOUT (end-to-end, no silent-drop).
  C-SEC (the `/show/stream` URL bearing the viewer-token is redacted in **every**
  log line; `PULSAR_READY` password redacted). Pull-only — **no inbound port**
  (C-NET, consumer side).
- **#60 (m10_setup harness).** The M10 scene **MUST declare**
  `__inputs.blue.<slug>.scene_control` (`operator_input` or binding `target_path`),
  else F2 silent-drops the delta (C-FANOUT precondition).
- **#64 (stinger demo asset).** sha256-pinned; absolute local path pinned in the
  consumer's `ASSET_ALLOWLIST`; clean licence; **no large binary in git** (fetched
  by the harness or Git-LFS-free small asset). (C-PATH, C-SCANS)
- **CI (#57 / #63 / #64).** C-SCANS (`trufflehog` + `pip-audit` Blue + `npm audit`
  Prism green on the real diff, lockfiles pinned). C-NET (Orion reachable via
  ZabGate only, no inbound port on the operator box — owned by Keeper/Conduit,
  cleared under #62).
