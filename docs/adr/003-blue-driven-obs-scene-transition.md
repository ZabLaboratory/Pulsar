# ADR 003 — M10: Blue-driven OBS program-scene switch with an animated transition

- **Status**: accepted
- **Date**: 2026-06-08
- **Decided**: 2026-06-09
- **Deciders**: @ClodoCapeo (maintainer), Vigil (design re-validation of Amendment 4 — pivot finalised, approved at design level; **and of Amendment 5 — the keyframed `wipe-cover` authoring maillon, Option (B), approved at design level 2026-06-09**), Bastion (security clearance — R7 veto lifted at design level via Amendment 2; R6 re-raised by Amendment 4 → re-clearance #76 required before build, extended by A5.5/A5.7 to confirm the leaf carries no node shape). The build is gated on SPIKE-LSML-HASH (A5.5, before Orion#64 merges) + SPIKE-GPU + SPIKE-CUT (A4.6) and Bastion re-clearance #76.
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

---

## Amendment 3 — PIVOT: the transition is rendered by OUR engine (Solar/CEF), not OBS-native. Supersedes the OBS-native transition mechanism of §3.2/§3.3, Amendment 1 §A1.1/§A1.2, and Amendment 2 §A2.1's obs-ws executor

- **Date**: 2026-06-08
- **Author**: Atlas (architect agent)
- **Status of the ADR**: returns to **proposed** (Vigil re-validates; Bastion
  re-clears the reduced surface). This amendment is a **mechanism pivot** ordered by
  the porteur mid-build. It **supersedes the transition-rendering mechanism** of all
  prior sections (the OBS-native `SetCurrentSceneTransition{Stinger}` +
  fork-composited stinger). It does **not** rewrite §1–§6 or Amendments 1–2 in place;
  the prior text remains the audit trail of the *abandoned* approach, and every
  superseded construct is named explicitly below. **What survives unchanged**: the
  milestone goal (Blue drives a screen-1→screen-2 transition, live, mid-broadcast,
  fired from the VPS), the VPS→operator **pull** delivery via the existing leaf
  stream, the broadcast-control authorisation chain, and the proof doctrine.

### A3.0 The porteur's correction (verbatim, 2026-06-08, mid-build)

> « D'ailleurs erreur, la transition doit aussi être gérer par nous dans ce cas.
> Sinon la preuve tout marche ne fonctionne pas. En gros animation par notre moteur
> a nous et via cef pas de natif media obs. »

**Interpretation (confirmed against the code).** The screen-1→screen-2 **animation
must be rendered by our own engine (Solar) inside a CEF `browser_source`, driven
reactively by the blueprint** — the **M9 model** (Blue→Orion leaf delta→LSDP→Solar
repaint), **not** an OBS-native transition. The point of M10 is to **prove the whole
Blue→Orion→Solar pipeline animates end-to-end on air**; an OBS-native compositor
transition proves none of that pipeline — it short-circuits our engine. Therefore the
merged OBS-native mechanism (fork stinger compositing #67, Prism obs-ws executor #63,
the `SetCurrentSceneTransition{Stinger}` path) is the **wrong** mechanism for this
milestone and is retired here.

### A3.1 The pivot is feasible and grounded — Solar already animates leaf-driven scene transitions

The mechanism the porteur points at **already exists and is proven** (M8/M9). Three
code facts decide it:

1. **Solar's runtime ships a leaf-driven transition engine.** Since ADR 007 (Lumencast
   convergence) Solar delegates to `@lumencast/runtime`, whose lifecycle is exactly
   *"subscribe → snapshot → bundle fetch → delta → scene_changed → crossfade →
   teardown"* (`Solar/src/mount.ts:22-23`). The runtime exposes a `<Crossfade
   trackKey durationMs>` that **mounts both scene roots during the transition window,
   one fading out as the other fades in, opacity-only / GPU-friendly**
   (`Solar/node_modules/@lumencast/runtime/dist/animate/crossfade.d.ts`), and a
   wire-format `TransitionSpec` (LSDP/1.1 §3.2.2) parsed into `tween | spring |
   crossfade | none` (`…/animate/transitions.d.ts`, `parseWireTransition`). **A leaf
   change → Solar runs a real animated transition. This is the engine.**
2. **Pulsar renders Solar in a CEF `browser_source` that loads the live show URL.**
   The `pulsar-scene-source` plugin owns a managed `browser_source`
   (`kCaptureSourceName = "PulsarSceneSource"`, `probe-m6-live.py:141`) whose `url` is
   the Solar live page composed from the LSDP show-stream
   (`Prism/src/main/broadcast-url.ts:55-96`, `getSolarSceneUrl` →
   `…/orion/api/v1/show/stream.lsdp?token=<viewer>` → `host.html?orion=…&mode=broadcast`).
   M6/M8/M9 all prove this CEF browser_source captures to the encoder and to Twitch.
3. **The reactive bridge that drives it is in prod (M9).** Blue writes a leaf via its
   long-lived service-token WS (`Blue/src/blue/core/orion_client.py:206-231`,
   `push_leaf` → `__inputs.blue.<slug>.<port>`); Orion fans the delta on `/show/stream`
   to all subscribers (the same fan-out §A1.3 already documents); the Solar CEF
   subscriber repaints. **M9 proved a leaf change repaints Solar live, no reload.**

**Verdict: the M10 transition is a Solar/LSDP transition, keyed and progressed by a
Blue-written leaf, rendered in the existing CEF browser_source — identical in wire and
trust to M9.** No OBS program-scene switch, no OBS-native transition, no fork
compositing change is needed for the animation.

### A3.2 The mechanism, code-anchored (supersedes §3.1, §3.2, §3.3, §A1.1, §A1.2)

**Scene composition — VERDICT: ONE CEF browser_source, NOT two `monitor_capture`
scenes with an OBS switch.** The two "screens" are **two Solar scene roots** (or two
states of one root) rendered inside the single `PulsarSceneSource` browser_source.
Solar's `<Crossfade trackKey>` mounts both during the window and animates between them.

- **On the porteur's real GPU desktop**, if a "screen" must show *actual desktop /
  monitor content* (not authored web content), the resolved primitive is Solar's
  **`Image`** leaf (`…/render/primitives/image.d.ts` — `src`, `fit`, opacity animated
  under a declared transition) fed a captured/served image, **or** the screen-1/screen-2
  visuals are authored Canvas content (the M8 path). **Which of the two the porteur
  wants is the one open product question — §A3.6 Q3.** Either way the *transition* is
  Solar's, in CEF.
- **`monitor_capture` is dropped from the transition mechanism.** Capturing physical
  monitors as OBS sources and switching OBS scenes is the abandoned approach. The two
  `monitor_capture` scenes (#68/#60) are **not** the M10 deliverable anymore (see
  §A3.4 disposition). This also **dissolves the GPU conflict** (§A3.3): with no
  `monitor_capture`, the D3D11 desktop-duplication that `--disable-gpu` broke is no
  longer on the path.

**Transition progression — VERDICT: keyed by a Blue leaf, M9-style.** The blueprint
emits a leaf that names the **target scene/state** and a **transition spec**; Solar
reads it as the `trackKey` (+ `durationMs`) of its `<Crossfade>` / `TransitionSpec`.
The leaf is an `__inputs.blue.<slug>.*` value Solar consumes as a render input —
exactly the M9 reactive input mechanism, **not** a control envelope. Solar decides
*what is visible* (the rendered crossfade/wipe), entirely in-DOM; **OBS composites
nothing transition-specific** — it just captures the CEF surface as it always has.

### A3.3 GPU reconciliation — the conflict dissolves; `--disable-gpu` stays headless-only

The block was: the probe spawns `pulsar.exe --disable-gpu`
(`probe-m10-canvas-live.py:300`, inherited M8/M9 for headless CEF), which breaks the
D3D11 `DuplicateOutput1` (`887A0004 UNSUPPORTED`) that `monitor_capture` needs → black
frames. The probe **already documents this exact failure**
(`probe-m10-canvas-live.py:749-758`: "blank capture … DXGI desktop duplication
unavailable … needs an interactive operator desktop").

**Reconciliation (decided):**
- The pivot **removes `monitor_capture` from the path** → the only GPU consumer left is
  the CEF browser_source (Solar), which M8/M9 already run successfully **with
  `--disable-gpu` in headless/CI** and would run **GPU-on on the porteur's real
  desktop**. CEF does not need DXGI desktop-duplication.
- **`--disable-gpu` is a headless/CI concern only.** On the operator's real GPU desktop
  the antenna run should spawn **GPU-on** (no `--disable-gpu`); in CI/agent headless the
  flag stays. This is a launch-flag branch in the run scripts, **not** a design risk
  anymore — because nothing on the path now needs both GPU desktop-duplication *and* a
  GPU CEF simultaneously. The original coexistence question (CEF-GPU + capture-GPU) is
  **moot** under the pivot. *(If a future milestone re-introduces real `monitor_capture`
  alongside CEF, the coexistence spike returns — out of scope here.)*

### A3.4 Disposition of the merged OBS-native work (precise: revert / dormant / reuse)

The OBS-native approach is already on `main` + Blue deployed. Recommended disposition,
per artefact:

| Artefact (merged) | What it is | Disposition | Why |
|---|---|---|---|
| **#67 — fork stinger compositing** (`pulsar-frontend-stub.cpp:471-610`: `bindTransitionOutput`, stinger source registration, transition-through-output fix) | C++ change making OBS composite a media stinger through the program output | **REVERT** (open a `forge/` revert PR; keep the commit in history). The transition-through-output change and the `obs_stinger_transition` registration are **not used** by the Solar/CEF mechanism. | The whole point of the pivot is *no native OBS transition*. Dead, load-bearing C++ on the encoder path = risk with no caller. Revert removes R1′/R7 entirely. **Exception:** if the porteur foresees a near-future native-stinger need, mark dormant behind an env flag instead — §A3.6 Q4. Default recommendation: **revert.** |
| **#64 — pinned stinger `.webm` asset** (`scripts/assets/stinger-demo.webm`) | demo media for the native stinger | **REVERT/REMOVE** with #67. No media is decoded in the Solar mechanism (Solar animates opacity/transform, no media plane). If §A3.6 Q3 picks a *media wipe* rendered by Solar, a small asset may return as a **Solar/Canvas** asset (web-served), not an OBS-decoded one — different artefact. | The R7 decoder-fuzz surface disappears with the asset. |
| **#63 — Prism obs-ws executor** (`Prism/src/main/scene-control/{consumer,executor,asset-allowlist}.ts`) | main-process consumer that reads the leaf and issues `SetCurrentSceneTransition{Stinger}` → `SetCurrentProgramScene` | **REUSE the subscriber half, REPLACE the executor half.** The `/show/stream` **subscriber** + the **C-INJ/C-PATHREAL validation gate** are exactly the VPS→operator pull consumer we still need (§A1.3 path is unchanged). But the **executor** (the four obs-ws calls) is **deleted** — under the pivot Prism issues **no OBS transition/scene call**; the leaf is consumed by **Solar in CEF**, not by Prism. *Re-scope #63 to: subscribe + validate + (if anything) forward to nothing — the leaf reaches Solar directly via the same `/show/stream` fan-out.* See §A3.5 for the sharpened seam. | Prism owning the obs-ws executor was only meaningful for a native switch. With Solar consuming the leaf, **Prism may have nothing to execute** — the consumer collapses to "Solar already gets the delta". This is the biggest simplification of the pivot; confirm scope in §A3.6 Q2. |
| **#66/#59 — frozen `scene_control` contract** (`Pulsar/scripts/contracts/scene_control/`, Blue `scene_control.py`) | the `{action:switch_program_scene, target_scene, transition:{kind:stinger,asset_id,point_ms,duration_ms}}` shape | **REPLACE the schema, KEEP the Conduit contract discipline.** `action:switch_program_scene`, `target_scene` (OBS scene name), `transition.kind:stinger`, `asset_id` no longer have meaning — there is no OBS scene to switch, no stinger, no media asset. The leaf becomes a **Solar render input** (§A3.5). The *contract machinery* (frozen shape + producer/consumer round-trip test, #59) is good and stays; its **payload** is redefined. | A contract that names OBS constructs is wrong for a Solar-rendered transition. Conduit re-freezes the new shape. |
| **#68/#60 — `m10_setup` two `monitor_capture` scenes + U1 spike (#56)** | harness creating two OBS scenes pinned to displays | **RETIRE as the M10 deliverable; KEEP as dormant reference.** The two-monitor scene harness is not used by the Solar mechanism. The U1 monitor-selection spike (#56) result stays documented for any future `monitor_capture` milestone. The M10 harness instead **declares the new Solar-input leaf on the active Orion scene** (the F2 obligation, §A2.3 — *that* part survives and is essential). | The OBS scene graph is the wrong artefact; the Orion-scene leaf declaration is the right one and is reused. |
| **#28 — Blue `scene_control` output** | Blue emits the typed output → leaf | **KEEP the leaf-write path, REDEFINE the value.** Blue still emits a typed output mapped to `__inputs.blue.<slug>.<port>` via `leaf_mapper` (unchanged, proven M9). Only `build_scene_control` (`Blue/src/blue/services/scene_control.py`) is rewritten to build the **Solar render-input** value instead of the OBS `scene_control` value. | The producer mechanism is correct (it's the M9 mechanism); the value shape pivots. |
| **#61 — `probe-m10-canvas-live.py`** | end-to-end OBS-native proof | **REWRITE around the Solar mechanism.** Drop `--disable-gpu` for the antenna run, drop the obs-ws executor calls, drop `monitor_capture` setup, drop the stinger-registered guard. Keep: VPS-fired trigger, leaf-delivery-causes-the-effect ordering, the **mid-transition blend** frame proof (now proving *Solar's* crossfade composited, captured off the CEF browser_source — the same `is_blend` analysis, just on the CEF surface), Solar-receives-and-acts, secret hygiene. | The proof skeleton (capture A / MID / B, blend assertion, live mid-broadcast) is exactly right; only the *cause* of the animation changes from OBS to Solar. |

**Net:** revert #67+#64 (native compositing + media asset), re-scope #63 to a thin
subscriber (executor deleted), re-freeze the contract (#59/#66) to a Solar-input shape,
retire the `monitor_capture` scene harness (keep the leaf-declaration half), keep Blue's
leaf-write (redefine the value), rewrite the probe (#61). This is a **surface
reduction**: the two hardest/most-dangerous builds (fork C++ on the encoder path R1′,
on-air media decode R7) are **deleted**, not rebuilt.

### A3.5 The new leaf / contract shape (supersedes §3.5, §A2.1)

The leaf stops being an OBS command and becomes a **Solar render input**, consumed
**by Solar in CEF** exactly like any M9 `__inputs.blue.*` value. The canonical path is
unchanged — `__inputs.blue.<slug>.scene_control` (the F1 3-segment form, §A2.2 still
holds; the port name may be renamed e.g. `scene` / `transition` — small Conduit call).
Indicative shape (Conduit freezes the exact one in #59):

```
__inputs.blue.<slug>.scene_control = {
    "target": "screen-2",                 // Solar scene/state key → <Crossfade trackKey>
    "transition": {
        "kind": "crossfade",              // Solar TransitionKind: none|tween|spring|crossfade
        "duration_ms": 600,               // → durationMs / TransitionSpec
        "ease": "ease-in-out"             // optional, LSDP/1.1 §3.2.2 wire easing
    }
}
```

- **No `action`, no OBS `target_scene`, no `asset_id`, no `path`.** The value is a
  render input Solar reads; it carries **no remote-control verb** and **cannot address
  OBS**. This is a *strictly smaller* trust surface than the OBS `scene_control` (which
  could switch what is on air) — see §A3.7.
- **Solar consumes it** via the runtime's leaf/`TransitionSpec` path (`parseWireTransition`,
  `<Crossfade>`); the value must conform to the runtime's `TransitionKind` /
  `TransitionSpec` (`transitions.d.ts`) — Conduit aligns the frozen Blue shape to the
  LSDP/1.1 §3.2.2 wire transition so producer (Blue) and consumer (Solar runtime)
  agree. **This is the real new contract boundary** (Blue ↔ Solar/runtime via Orion),
  replacing the Blue ↔ Prism-obs-ws boundary.
- **#56 F2 obligation survives and is essential**: the active Orion scene **must
  declare** `__inputs.blue.<slug>.scene_control` (`operator_input` / binding
  `target_path`) or `Inbox.Write` silently drops the delta (`Orion inbox.go`
  `sceneAcceptsPath`, §A2.3). The harness (#60) declares it; the probe proves no
  silent-drop.

### A3.6 Open product questions — porteur only (do not self-decide)

The pivot resolves the *mechanism*; two genuine **product** choices remain that the
architecture cannot make:

- **Q3 (what the two "screens" actually show) — PRODUCT.** Are screen-1 / screen-2
  **(a)** two authored Canvas scenes/states (pure web content, the M8 lineage — fully
  in our engine, no desktop capture), or **(b)** representations of *real desktop /
  game content* that Solar must display (via an `Image`/video leaf fed a captured
  frame/stream)? (a) is clean and proves the pipeline outright; (b) re-introduces a
  "get the desktop pixels into Solar" sub-problem (a capture→serve→`Image` path) that
  is a **separate spike** and may re-open R4 (real desktop on air) inside our engine.
  **The porteur's "screen 1 / screen 2" wording suggests real screens; confirm.** This
  is the single most load-bearing unknown of the pivot.
- **Q4 (fate of the native-stinger work) — PRODUCT/STRATEGY.** Revert #67/#64 outright
  (recommended — dead code on the encoder path), or keep them **dormant behind a flag**
  because a native OBS stinger is a foreseen *future* capability independent of M10?
  Atlas recommends **revert**; the porteur owns the roadmap call.

### A3.7 Risks (supersedes §5 R1′, R7; revises R6, R4)

- **R1′ / R7 — DELETED by the pivot.** No fork C++ change on the encoder path (R1′
  gone), no on-air media decode (R7 gone). The two heaviest engineering/security risks
  of the OBS-native approach **disappear** with #67/#64 reverted. **Net risk
  reduction.**
- **R6 (broadcast-control channel) — DOWNGRADED.** The leaf no longer carries an OBS
  remote-control verb; it is a Solar **render input** (a crossfade key + duration),
  identical in kind to every M9 `__inputs.blue.*` leaf. A forged/compromised leaf can
  at worst **change what our overlay renders** — the *same* surface M9 already accepted
  and Bastion already cleared for M9 (#54). It can **no longer switch what OBS has on
  air** (there is no OBS switch). The trigger stays operator-gated; the service token
  stays `__inputs.blue.*`-scoped; the subscription stays read-only. → **Bastion
  re-clears the reduced surface; this should be a lighter clearance than #62.**
- **R4 (real desktop on air) — CONDITIONAL on Q3.** If Q3 picks (a) authored content,
  **R4 is eliminated** (no desktop capture anywhere). If Q3 picks (b) real
  desktop/game pixels into Solar, R4 returns **inside our engine** and the porteur
  records it as before. **Bastion + porteur revisit only if Q3=(b).**
- **R-GPU (new, low, engineering) — launch-flag branch.** The antenna run must spawn
  GPU-on on the real desktop (no `--disable-gpu`), headless/CI stays `--disable-gpu`.
  A wrong flag on the operator box degrades CEF rendering but cannot blank the encoder
  the way the reverted native path could. Guarded by the probe's non-blank assertion.
- **R-SOLAR-CAP (new, spike, only if Q3=(b)) — desktop→Solar capture path.** Getting
  real monitor pixels *into* a Solar primitive (capture → serve → `Image`/video leaf)
  is unproven; it is a **spike**, not a wiring task, and may itself need GPU/perf work.
  Only opens if Q3=(b).

### A3.8 Resolution criteria delta (supersedes §6.1, §6.3, §6.5 mechanism; §A1.7)

- **C5′ (the M10 proof, rewritten).** The probe captures frames off the **CEF
  `PulsarSceneSource` browser_source** across the transition window; the MID frame is a
  **blend** (Solar's crossfade compositing in-DOM), neither pure screen-1 nor pure
  screen-2 — proving **our engine** animated the transition. Same `is_blend` analysis
  (`probe-m10-canvas-live.py:727-763`), now on the CEF surface, GPU-on on the real box.
- **C-MECH (new, the pivot's defining criterion).** The proof asserts **no OBS-native
  transition and no `SetCurrentProgramScene` is issued** during the switch — the
  animation is caused *only* by the Solar leaf delta. (Negative assertion: zero
  transition/scene-switch obs-ws calls in the run.)
- **§6.1 retired:** no two `monitor_capture` scenes required. **§6.3/§6.4 retired:** no
  `SetCurrentSceneTransition` / `SetCurrentProgramScene` flip.
- **C2 / C6 / C7 / C10 survive verbatim:** Blue drives it (leaf written, operator-gated,
  VPS-fired), live mid-broadcast, secret hygiene, pull-delivered (no inbound port,
  Solar receives the delta). **C8 (contract) survives** with the new Solar-input shape.
- **C-FANOUT survives** (§A2.3): the active scene declares the leaf path or the delta
  is silent-dropped; the probe proves end-to-end delivery.

### A3.9 Revised issue cut (for Eleven → Forge / Probe / Conduit / Bastion / Keeper)

Issues to **open**:
- **#69 (Forge) — revert #67 + #64.** Revert the fork stinger compositing and the
  pinned `.webm` from `main` (or flag-dormant if Q4 says so). CI full build green
  post-revert. *Blocks nothing; do first to shrink surface.*
- **#70 (Conduit) — re-freeze the `scene_control` contract as a Solar render input.**
  New shape (§A3.5) aligned to the runtime `TransitionSpec` / LSDP/1.1 §3.2.2;
  producer (Blue) ↔ consumer (Solar runtime) round-trip contract test; canonical
  3-segment path retained. Supersedes #59/#66 payload.
- **#71 (Forge, Blue) — rewrite `build_scene_control`** to emit the Solar-input value;
  keep the `leaf_mapper` write path (proven M9). Supersedes #58/#28 value.
- **#72 (Forge, Prism) — re-scope #63: delete the obs-ws executor;** keep only the thin
  `/show/stream` subscriber + validation if Prism needs to observe the leaf at all
  (likely **Prism does nothing** — Solar in CEF consumes the leaf directly; confirm via
  Q2/§A3.4). Possibly closes #63 as "no Prism executor needed".
- **#73 (Forge, Pulsar/Canvas) — the two Solar scene roots/states + `<Crossfade>`
  wiring** keyed by the leaf. Pending **Q3**: (a) authored Canvas content, or (b)
  desktop-pixels-into-Solar (then add the §A3.7 R-SOLAR-CAP spike as a blocker).
- **#74 (Forge, harness #60) — m10_setup declares the Solar-input leaf** on the active
  Orion scene (F2/C-FANOUT); **drop** the two `monitor_capture` scenes from the M10
  deliverable.
- **#75 (Probe, #61 rewrite) — Solar-rendered transition proof**: GPU-on antenna run,
  no `monitor_capture`, no obs-ws executor; capture A/MID/B off the CEF source, blend
  assertion (C5′), C-MECH negative assertion, VPS-fired + pull (C10), secret hygiene.
- **#76 (Bastion) — re-clearance of the REDUCED surface.** R1′/R7 deleted; R6
  downgraded to "M9-equivalent render input"; R4 conditional on Q3. Confirm the leaf
  carries no control verb and cannot address OBS. Lighter than #62.
- **#77 (Keeper) — antenna run launch-flag branch** (GPU-on real desktop vs
  `--disable-gpu` headless) + the VPS-fired trigger run on the real box.

Issues to **close/retire**: #57 (reverted by #69), #62 (superseded by #76), the
native-transition halves of #56/#60/#61. **Spike to add if Q3=(b):** desktop→Solar
capture (R-SOLAR-CAP).

---

## Amendment 4 — PIVOT FINALISED (Q3=(b), Q4=dormant): real `monitor_capture` content + Solar-overlay animation + a hidden hard-cut. Supersedes Amendment 3 §A3.2/§A3.3/§A3.4 mechanism and corrects its `monitor_capture`-is-removed hypothesis

- **Date**: 2026-06-08
- **Author**: Atlas (architect agent)
- **Status of the ADR**: stays **proposed** (Vigil re-validates; Bastion re-clears
  the surface). This amendment records the porteur's two product answers to §A3.6
  (Q3, Q4) and **finalises the coordination design** the answers force. It **does not
  rewrite** §1–§6 or Amendments 1–3 in place — those remain the audit trail. It
  **supersedes the Amendment 3 mechanism** in three precise places (named in A4.0) and
  **corrects one load-bearing A3 hypothesis** (`monitor_capture` is NOT removed). What
  survives from A3: the **animation is rendered by our engine (Solar/CEF), leaf-driven,
  M9-style**, never an OBS-native media transition; the VPS→operator **pull** delivery;
  the broadcast-control authorisation chain.

### A4.0 What A4 supersedes / corrects in Amendment 3 (precise)

| A3 construct | A4 disposition |
|---|---|
| §A3.2 "drop `monitor_capture` from the path; the two `monitor_capture` scenes are not the deliverable" | **CORRECTED — `monitor_capture` STAYS.** Under Q3=(b) the two screens show **real desktop/monitor content** via `monitor_capture`. The 2-scene `monitor_capture` harness (#68/#60) is **REUSED**, not retired. |
| §A3.2 "the leaf is the `<Crossfade trackKey>`; Solar reads the leaf as `trackKey`" | **CORRECTED (factually wrong against the runtime).** A plain `__inputs.blue.*` leaf delta goes through `onDelta`→`applyDelta` and **does NOT flip** the runtime's top-level crossfade key. `crossfadeKeySignal` flips **only on a `scene_changed`→new snapshot** (`@lumencast/runtime/dist/mount.js:122`, `${sceneId}::${sceneVersion}`). The Solar overlay animation is therefore an **in-DOM, signal-driven primitive animation** (opacity/transform under a leaf), **not** the runtime `<Crossfade>`. See A4.2. |
| §A3.3 "the GPU conflict dissolves; no `monitor_capture` → no D3D11 duplication on the path" | **CORRECTED — the GPU conflict is BACK and is verification #1.** With `monitor_capture` retained AND a CEF browser_source (Solar overlay) rendering simultaneously on a real GPU-on desktop, the D3D11 desktop-duplication ⟷ CEF-GPU coexistence is exactly the thing that failed before. A4.4 designs it and marks it the run's first gate. |
| §A3.4 "REVERT #67 (fork stinger) + #64 (asset)" | **CHANGED to FLAG-DORMANT (Q4).** No revert. Neutralise behind an inert-by-default flag. A4.3. |
| §A3.4 "REUSE the subscriber half of #63, DELETE the executor" | **REFINED — the executor RETURNS, reduced.** Prism #63 keeps the subscriber + validation AND regains a **reduced executor**: it fires the **hard-cut** (`SetCurrentProgramScene` / `SetSceneItemEnabled` — a cut, NOT a transition) at the synchronised moment. A4.2/A4.3. |
| §A3.7 "R1′/R7 DELETED; R4 conditional" | **REVISED — R1′/R7 become DORMANT-behind-flag (not deleted); R4 RETURNS (Q3=(b)), already accepted by the porteur.** A4.5. |

### A4.1 The porteur's two decisions (verbatim intent, 2026-06-08)

- **Q3 = (b).** Both screens show **real screen content via `monitor_capture`**
  (physical capture, GPU-on on the real desktop). The transition is animated by
  **Solar in a CEF browser_source layered *over* those captures**. *Corrects A3:*
  `monitor_capture` is the **content** and stays; there is **no** capture→Solar bridge
  (the A3 R-SOLAR-CAP spike for Q3=(c) is **moot** — Solar never displays the desktop
  pixels; it renders an opaque overlay *on top of* them).
- **Q4 = dormant behind a flag.** Do **not** revert the OBS-native work (#67 fork
  stinger, #64 webm asset, #128/#63 obs-ws executor). **Neutralise it behind a flag**
  (inert by default), preserved as a possible future native-stinger capability.

### A4.2 The coordination — overlay Solar (animation) ⟷ OBS content (hidden hard-cut). THE core of this finalisation.

The composition is **two planes**:

1. **Content plane (below) — OBS `monitor_capture`.** Two OBS scenes (or one scene
   with two capture sources), `scene-screen-1` / `scene-screen-2`, each pinned to a
   physical display (the #68/#60 harness, U1/#56 monitor-pinning — **reused verbatim**).
2. **Animation plane (above) — Solar in the `PulsarSceneSource` CEF browser_source.**
   Solar renders a **full-screen opaque wipe/cover** (an authored overlay element whose
   opacity/transform is animated), layered over the capture(s). **Solar composes web
   with alpha on top of OBS sources; it never reads the pixels of the captures below**
   — confirmed by the architecture (the porteur's own constraint). So the overlay can
   *cover* the content but cannot *crossfade between two captures*.

**Therefore the visible animation = the Solar overlay; the screen-1→screen-2 change of
the content underneath = an instantaneous HARD-CUT, hidden under the overlay's opaque
peak.** OBS performs only a cut (no OBS-native transition); 100 % of the visible
animation is our engine.

**The timeline (the synchro problem, solved):**

```
t0 ───────────── t_peak ───────────── t_end
 overlay reveal    overlay fully       overlay retract
 (0→opaque)        OPAQUE (covers       (opaque→0)
                   the content)
                        │
                        ▼  HARD-CUT here: SetCurrentProgramScene{scene-screen-2}
                           (or SetSceneItemEnabled toggle) — invisible, the
                           opaque overlay is covering the content at t_peak
```

The cut MUST land inside the opaque window `[t_opaque_start, t_opaque_end]` around
`t_peak`, or the cut is seen. **This is the hard unknown the porteur flagged.**

**The runtime gives us NO transition-lifecycle callback — verified, decisive.** I read
the runtime: the crossfade is `framer-motion` `AnimatePresence mode="sync"`, opacity-only,
**hardcoded `duration: 0.4`, no `onAnimationComplete`, no progress/midpoint event, no
emitted metric at start/peak/end** (`@lumencast/runtime/dist/app.js:27`,
`animate/crossfade.js:6-9`). `onMetric` emits only `scene_changed` *at the start* of a
snapshot swap (`mount.js:57-68`), never a transition-complete. **So Solar cannot signal
its own mid-animation to an external consumer, and the runtime crossfade isn't even
leaf-drivable** (A4.0 row 2). This kills the "Solar emits a mid-animation signal" option
outright.

**VERDICT — option (T1): a single leaf carries BOTH the overlay animation AND an
explicit cut schedule; ONE timing authority (the Prism consumer) owns the cut clock.**

The `scene_control` leaf (still `__inputs.blue.m10-scene-control.scene_control`, the F1
3-segment path, fixture-pinned) carries a self-describing overlay timing the consumer
can reproduce **without any callback from Solar**:

```
__inputs.blue.<slug>.scene_control = {
    "target_scene": "scene-screen-2",       // OBS content scene to cut to (allowlisted)
    "overlay": {                             // the Solar animation (M9 render input)
        "kind": "wipe-cover",                // authored overlay element key Solar renders
        "reveal_ms": 250,                    // 0 → fully-opaque
        "hold_ms": 200,                      // fully-opaque plateau (the cut window)
        "retract_ms": 250                    // opaque → 0
    },
    "cut_at_ms": 250                         // offset from leaf-apply: when the consumer fires the hard-cut
                                             // (must satisfy reveal_ms ≤ cut_at_ms ≤ reveal_ms+hold_ms)
}
```

- **Solar (CEF, below-the-overlay-engine) consumes the `overlay` sub-object as an M9
  reactive render input** and animates the opaque cover purely in-DOM (opacity/transform,
  GPU-friendly, the Solar conventions). No `scene_changed`, no runtime `<Crossfade>` —
  an **authored Canvas/Solar overlay element keyed off the leaf**, exactly the M9
  repaint mechanism Quasar/Blue already drive. The overlay's reveal/hold/retract is a
  declared timeline the Solar element plays on leaf-apply.
- **The Prism consumer (#63, socket holder) consumes the SAME leaf delta** off
  `/show/stream` (it is already a subscriber), reads `cut_at_ms`, and after that delay
  **fires the hard-cut on the loopback obs-ws**: `SetCurrentProgramScene{target_scene}`
  (or `SetSceneItemEnabled` toggling the two capture sources within one scene — a
  **cut**, never a transition). Because both Solar and Prism receive the **same leaf at
  ~the same instant** over the same Orion fan-out, and the overlay timeline + `cut_at_ms`
  are **co-specified in that one leaf**, the cut is scheduled relative to the overlay's
  own clock without any cross-process callback.

**Why one leaf with co-specified timings, not two signals.** The alternative ("Solar
emits a mid-animation signal that triggers the cut") is **impossible** with the current
runtime (no callback — verified above) and would require a Solar→Prism reverse channel
that does not exist. Co-specifying `reveal/hold/cut_at` in the single leaf makes the
**leaf itself the synchronisation contract**: the same authored numbers drive the
overlay (in Solar) and the cut clock (in Prism). The residual risk is **clock skew
between the two consumers' receipt of the leaf** and Solar's render latency vs Prism's
timer — bounded by `hold_ms` (the opaque plateau). Sized generously (`hold_ms` ≥ 150–200
ms vs the M9 delta→DOM budget ≤ 50 ms and a sub-frame obs-ws call), the cut lands well
inside the opaque window. **This margin is the thing the spike must measure on the real
box** (A4.6 SPIKE-CUT).

**Who fires the cut — VERDICT: Prism #63 keeps a REDUCED executor (the cut).** Prism is
the obs-ws socket holder (loopback, session-random password) — it is the *only*
component that can issue an obs-ws call. Under the pivot it issues **no transition and no
`SetCurrentSceneTransition`** (those stay dormant, A4.3); it issues **only the hard-cut**
(`SetCurrentProgramScene` / `SetSceneItemEnabled`) at `cut_at_ms`. Routing the cut
anywhere else is impossible (loopback socket, A3.4(C)/§3.4(C) unchanged). So #63 is NOT
collapsed to a pure subscriber (A3 §A3.4 was wrong on this under Q3=(b)): it is
**subscriber + validation gate + cut-only executor**.

### A4.3 Disposition revised — flag-dormant, not revert (Q4); harness reused

| Artefact | A3 said | **A4 final (Q3=(b)/Q4=dormant)** |
|---|---|---|
| #67 fork stinger compositing (`pulsar-frontend-stub.cpp` transition-through-output, stinger source registration) | revert | **FLAG-DORMANT.** Guard the stinger-source registration + the transition-through-output change behind an env flag (e.g. `PULSAR_NATIVE_STINGER`, **default off**). Inert by default → no native transition runs; the code stays in `main` for a future capability. CI full build green with the flag off (and, ideally, a smoke with it on). |
| #64 stinger `.webm` asset | revert/remove | **KEEP, referenced only under the dormant flag.** sha256-pinned; no decode happens with the flag off → R7 dormant, not live. |
| #128/#63 Prism obs-ws executor (`SetCurrentSceneTransition{Stinger}`→`SetCurrentProgramScene`) | reuse subscriber, delete executor | **KEEP subscriber + validation; REPLACE the executor body** with the **hard-cut** (`SetCurrentProgramScene`/`SetSceneItemEnabled`, no transition). The `SetCurrentSceneTransition{Stinger}` call is **behind the dormant flag** (off → never issued). |
| #68/#60 two `monitor_capture` scenes + U1/#56 | retire as deliverable | **REUSED AS THE CONTENT PLANE.** This is now load-bearing again (Q3=(b)). U1/#56 monitor-pinning is required, not archived. The harness also keeps the **Orion-scene leaf declaration** (#74/A2.3 C-FANOUT) for the overlay leaf. |
| #66/#59 frozen contract | replace schema | **RE-FREEZE (Conduit, #70) to the A4.2 shape**: `target_scene` (OBS cut target) + `overlay{kind,reveal_ms,hold_ms,retract_ms}` (Solar render input) + `cut_at_ms`. No `asset_id`/`path` in the live path (those exist only under the dormant native flag). |
| #28/#71 Blue `build_scene_control` | redefine value | **REDEFINE** to emit the A4.2 value (overlay timeline + `target_scene` + `cut_at_ms`); keep the proven `leaf_mapper` write path. |
| #61/#75 probe | rewrite | **REWRITE per A4.6 criteria**: GPU-on, `monitor_capture` content present, overlay-blend proof on the CEF surface, cut-invisibility proof, zero-native-transition assertion. |

**Net:** nothing is reverted; the native path is **dormant behind a flag**; the
`monitor_capture` harness is **reused as content**; the executor is **reduced to a cut**;
the contract is **re-frozen** to overlay-timeline + cut schedule.

### A4.4 GPU coexistence — the verification-#1 risk, code-anchored

This is what failed before and is back under Q3=(b). On the real GPU-on desktop, **two
GPU consumers run simultaneously**: (1) `monitor_capture` (DXGI/D3D11 `DuplicateOutput1`)
and (2) the CEF browser_source (Solar overlay). Code facts:

- The probe today spawns `pulsar.exe --disable-gpu` (`probe-m10-canvas-live.py:300`),
  inherited from headless M8/M9. **`--disable-gpu` breaks DXGI desktop-duplication**
  (`887A0004 UNSUPPORTED`) → `monitor_capture` returns all-black; the probe already
  documents this exact failure (`probe-m10-canvas-live.py:743-746`, 752-754: "needs an
  interactive operator desktop").
- The fork's CEF browser uses **D3D11 shared textures** (`ENABLE_BROWSER_SHARED_TEXTURE`,
  `plugins/pulsar-browser/CMakeLists.txt:129`; `browser-client.cpp:443-469`
  `gs_texture_open_nt_shared`), and only falls back to `disable-gpu-compositing` when
  shared-texture is unavailable (`browser-app.cpp:73-80`). So a healthy GPU-on CEF wants
  the GPU; a process-wide `--disable-gpu` degrades both the CEF path and kills DXGI
  duplication.

**Design (decided):** the antenna run spawns **GPU-on (no `--disable-gpu`)** on the real
interactive desktop, so DXGI duplication works for `monitor_capture` AND CEF shared-texture
compositing works for the Solar overlay. `--disable-gpu` stays **headless/CI only**
(where there is no real monitor to capture anyway → typed skip, exit 3). This is the
launch-flag branch (Keeper #77).

**RISK R-GPU (engineering, elevated to verification #1):** that `monitor_capture` D3D11
duplication and CEF D3D11 shared-texture compositing **coexist** GPU-on on the operator's
real desktop is **plausible but not proven on this fork** (it is the precise combination
the prior run never got to because `--disable-gpu` masked it). **This is the run's first
gate (A4.6 SPIKE-GPU):** before any overlay/cut work is trusted, prove on the real box
that, GPU-on, a `monitor_capture` source is non-black AND the CEF browser_source renders —
**simultaneously**, in the same `pulsar.exe`. If they cannot coexist (e.g. CEF needs
`disable-gpu-compositing` which then degrades duplication), that is a **hard finding**
that reopens the mechanism — hence it gates the build.

### A4.5 Risks (supersedes A3 §A3.7; revises §5)

- **R4 — RETURNS, already accepted.** `monitor_capture` of the real displays puts the
  operator's actual desktop on the public Twitch VOD (notifications, private windows,
  on-screen secrets). Under Q3=(b) this is **certain**, not hypothetical. **The porteur
  already accepts it** ("écran propre" — clean screen before go-live); operator
  responsibility, outside platform code. Recorded as accepted residual (A2.4 R4 text
  stands, now firmly in force). Bastion notes it; no new mitigation owed by the platform.
- **R1′ / R7 — DORMANT behind a flag, NOT deleted (Q4).** The fork stinger compositing
  (R1′) and the on-air media decode (R7) survive in `main` but are **inert by default**
  (flag off → no transition-through-output, no media decode). Live risk while off ≈ 0;
  the residual is "the flag is accidentally on in prod". Mitigation: default-off, the
  probe's C-MECH negative assertion proves no native transition fires in the M10 run, and
  CI builds/smokes with the flag off. **Bastion: confirm the dormant code path cannot be
  reached by a leaf value** (the flag is operator/env-controlled, never leaf-controlled).
- **R6 — broadcast-control channel, RE-RAISED (not the A3 downgrade).** A3 downgraded R6
  to "M9-equivalent render input" on the premise that the leaf carried *no* OBS verb.
  **Under A4 that premise is false:** the leaf again carries `target_scene` (an OBS scene
  to cut to) and the Prism consumer again issues an obs-ws call (`SetCurrentProgramScene`)
  — a real broadcast-control action, fired from the VPS. So R6 is **back at roughly its
  Amendment-2 weight**, MINUS the stinger/`path`/`asset_id` decode surface (which is now
  dormant). Mitigations unchanged and still required: operator-gated `/trigger`,
  `__inputs.blue.*`-scoped service token, read-only viewer subscription, **`target_scene`
  allowlist at the consumer** (C-INJ, `SCENE_ALLOWLIST = {scene-screen-1, scene-screen-2}`),
  rate-limit. → **Bastion clearance required (not the "lighter than #62" of A3 — closer
  to #62 itself, minus R7-live).**
- **R-CUT (new, engineering) — the hard-cut may be visible if it lands outside the opaque
  window.** If clock skew between Solar's overlay render and Prism's `cut_at_ms` timer, or
  Solar render latency, pushes the cut outside `[reveal_ms, reveal_ms+hold_ms]`, the
  audience sees the content snap. Mitigation: generous `hold_ms` (opaque plateau) sized
  against measured skew (SPIKE-CUT); the probe proves cut-invisibility by capturing a
  frame at `cut_at_ms` and asserting the overlay is opaque there (A4.6 C-CUT).
- **R-GPU (new, engineering, verification #1)** — A4.4: `monitor_capture` D3D11
  duplication ⟷ CEF shared-texture coexistence GPU-on, unproven on this fork. Gates the
  build (SPIKE-GPU).

No residual is implicit. R4 accepted (porteur). R1′/R7 dormant. R6 to Bastion. R-CUT and
R-GPU are engineering gates proven by the probe/spikes.

### A4.6 Resolution criteria (supersedes A3 §A3.8 mechanism criteria)

- **SPIKE-GPU (gate, do first).** On the real interactive GPU-on desktop, in one
  `pulsar.exe` spawned **without `--disable-gpu`**, a `monitor_capture` source returns a
  **non-black** frame AND the `PulsarSceneSource` CEF browser_source renders content —
  **simultaneously**. PASS = both planes live at once. FAIL = hard finding, reopen
  mechanism. (Owner: Keeper run + Probe assertion.)
- **SPIKE-CUT (gate, before trusting the cut).** Measure, on the real box, the skew
  between Solar overlay-opaque-onset and the Prism `cut_at_ms`-fired obs-ws cut; confirm
  the cut lands inside the opaque window for the chosen `hold_ms`. Output: a validated
  `hold_ms` floor. (Owner: Probe.)
- **C5″ (the M10 proof — overlay blend on the CEF surface).** The probe captures frames
  across the transition window off the **CEF `PulsarSceneSource`**; mid-animation the
  frame shows the **Solar overlay compositing** (the opaque cover present, not pure
  content) — proving **our engine** animated it. Same `is_blend`/modal analysis
  (`probe-m10-canvas-live.py:727-763`), GPU-on.
- **C-CUT (new — the cut is invisible).** A frame captured at `cut_at_ms` shows the
  overlay **opaque** over the content (the cut is hidden); and the content underneath,
  sampled before reveal vs after retract, has changed screen-1→screen-2 (the cut
  happened). Negative: no frame in `[reveal_ms, reveal_ms+hold_ms]` shows a content snap.
- **C-MECH (the pivot's defining criterion, retained).** The run issues **no OBS-native
  transition**: zero `SetCurrentSceneTransition`/`TriggerStudioModeTransition` /
  no transition-through-output. The only obs-ws scene call is the **hard-cut**
  (`SetCurrentProgramScene`/`SetSceneItemEnabled`). The native-stinger flag is **off**
  (asserted). Animation is caused only by the Solar overlay leaf.
- **C2 / C6 / C7 / C10 survive verbatim** (Blue drives it, operator-gated, VPS-fired;
  live mid-broadcast; secret hygiene; pull-delivered, Solar AND Prism both receive the
  leaf). **C8 (contract)** survives with the A4.2 shape. **C-FANOUT survives** (the active
  Orion scene declares `__inputs.blue.m10-scene-control.scene_control` — fixture already
  does, `scripts/fixtures/m10-orion-scene.lsml.json`).
- **C-CONTENT (new, Q3=(b)).** Both `monitor_capture` scenes pin distinct displays (U1/#56);
  `GetInputSettings` confirms distinct `monitor` targets; on a LIGHT/headless build →
  typed skip (exit 3). Reused from original §6.1.

### A4.7 Revised final issue cut (supersedes A3 §A3.9)

**Open / re-scope:**

- **#69 (Forge) — flag-dormant the native-stinger work (NOT revert, Q4).** Guard #67's
  stinger-source registration + transition-through-output and #128's
  `SetCurrentSceneTransition{Stinger}` behind `PULSAR_NATIVE_STINGER` (default off). CI
  full build green flag-off; the dormant path unreachable by any leaf value. *Do first
  (shrinks live surface without losing the code).* RC: flag off ⇒ C-MECH holds; #64 asset
  kept, sha256-pinned, decoded only under the flag.
- **#70 (Conduit) — re-freeze the `scene_control` contract to the A4.2 shape.**
  `target_scene` (OBS cut target, allowlisted) + `overlay{kind,reveal_ms,hold_ms,
  retract_ms}` (Solar render input) + `cut_at_ms`, with the invariant
  `reveal_ms ≤ cut_at_ms ≤ reveal_ms+hold_ms`. Canonical 3-segment path
  `__inputs.blue.m10-scene-control.scene_control` retained; producer (Blue) ↔ two
  consumers (Solar overlay, Prism cut) round-trip contract test. RC: contract test asserts
  the shape and the `cut_at_ms` invariant; both consumers parse it.
- **#71 (Forge, Blue) — `build_scene_control` emits the A4.2 value;** keep `leaf_mapper`.
  RC: emits overlay timeline + `target_scene` + `cut_at_ms`; operator-gated; round-trips
  through #70's contract.
- **#72 (Forge, Prism #63) — reduced executor + subscriber.** Keep the `/show/stream`
  subscriber + C-INJ validation (`SCENE_ALLOWLIST={scene-screen-1,scene-screen-2}`); the
  executor fires **only the hard-cut** (`SetCurrentProgramScene`/`SetSceneItemEnabled`) at
  `cut_at_ms`; the `SetCurrentSceneTransition{Stinger}` call only behind the dormant flag.
  RC: off-allowlist `target_scene` ⇒ 0 obs-ws calls (negative test); cut fired at
  `cut_at_ms` ±tolerance; zero native-transition call (C-MECH).
- **#73 (Forge, Solar/Canvas) — the authored overlay element (`wipe-cover`)** keyed off the
  leaf `overlay` sub-object, animating opacity/transform in-DOM (Solar GPU-only convention),
  playing reveal/hold/retract on leaf-apply. **Not** the runtime `<Crossfade>` (A4.0 row 2).
  RC: a leaf delta repaints the overlay live (M9 parity); the overlay reaches full opacity
  during `hold_ms`; tree-shakable in broadcast mode.
- **#74 (Forge, harness) — m10_setup KEEPS the two `monitor_capture` scenes (content,
  Q3=(b)) AND declares the overlay leaf on the active Orion scene** (C-FANOUT, fixture
  already declares it). RC: two scenes pin distinct displays (U1/#56); the Orion scene
  declares the leaf path; no silent-drop end-to-end.
- **#75 (Probe) — Solar-overlay + hidden-cut proof.** GPU-on antenna run; `monitor_capture`
  content present; C5″ overlay blend on CEF; **C-CUT** cut-invisibility; C-MECH
  zero-native-transition + flag-off; VPS-fired + pull (C10); secret hygiene. Owns
  **SPIKE-CUT**.
- **#76 (Bastion) — re-clearance.** R4 accepted (porteur); R6 re-raised to ~#62 weight
  (leaf carries `target_scene` + drives an obs-ws cut), minus live R7; R1′/R7 dormant —
  confirm the flag is env-controlled, never leaf-reachable; confirm `SCENE_ALLOWLIST` at
  the consumer; confirm the leaf carries **no** `path`/`asset_id` in the live (flag-off)
  path. **Not lighter than #62 — comparable, minus live decode.**
- **#77 (Keeper) — antenna run launch-flag branch (GPU-on real desktop vs `--disable-gpu`
  headless) + VPS-fired trigger on the real box.** Owns **SPIKE-GPU (verification #1)**.

**Retire/close:** the A3 plan to *revert* #67/#64 is dropped (now flag-dormant under #69);
the A3 R-SOLAR-CAP spike (capture→Solar) is **dropped as moot** (Q3=(b): Solar overlays,
never displays the desktop pixels). #56/#60/#68 (monitor_capture harness) are **retained**,
not retired.

### A4.8 Residual unknowns / spikes / product questions

- **SPIKE-GPU (verification #1, build gate)** — `monitor_capture` D3D11 duplication ⟷ CEF
  D3D11 shared-texture coexistence, GPU-on, on the real box (A4.4). **If this fails, the
  whole pivot's content plane fails** — it must be proven before #72–#75 are trusted.
- **SPIKE-CUT (build gate)** — measured skew Solar-overlay-opaque ⟷ Prism-`cut_at_ms`,
  sizing `hold_ms` so the cut is provably invisible (A4.2 / R-CUT). **Recommendation: a
  small prototype of just (overlay opacity timeline in Solar) + (a `cut_at_ms`-timed
  `SetSceneItemEnabled` from Prism) + (a CEF frame grab at `cut_at_ms`) should be built
  and measured BEFORE the full #71–#75 build** — the coordination is non-trivial enough
  (no Solar callback, two independent consumers of one leaf, frame-accurate timing) that
  proving the timing margin first de-risks the whole milestone. This is the one place I
  recommend a prototype-before-build.
- **Product questions: NONE remain.** Q1/Q2 (Amendment 1), Q3/Q4 (this amendment) are all
  decided. The mechanism, the disposition, the contract shape, and the risk acceptances are
  fully specified. What is left is **engineering proof** (the two spikes), not product
  choice.

---

## Amendment 5 — The authoring maillon for the keyframed `wipe-cover` node: corrects the inexact "exactly the M9 repaint mechanism" premise (§A4.2), pins the string-JSON leaf transport, and decides WHERE the `RenderNode.keyframes` is produced in the REAL pipeline

- **Date**: 2026-06-09
- **Author**: Atlas (architect agent)
- **Status of the ADR**: returns to **proposed** (Vigil re-validates). This amendment
  does **not** rewrite §1–§6 or Amendments 1–4 in place — they remain the audit trail.
  It **corrects two load-bearing premises in §A4.2** (the "exactly the M9 repaint
  mechanism" claim and the implicit object-shaped leaf) and **decides the missing
  authoring maillon** that lets the REAL Blue→Orion→Solar pipeline produce the keyframed
  `wipe-cover` overlay node — the node that issue **#73** assumes exists but that no
  layer of the real pipeline can currently emit. It supersedes the §A4.7 **#73** scope
  (re-scoped below) and adds issues.

### A5.0 The trou (code-anchored, verified independently of Forge's report)

The loopback M10 proof passes; the real wire cannot. The reason is a **vocabulary gap**:
the runtime can *render* a leaf-replayed keyframe sequence, but **no authoring layer can
emit one**. Verified end-to-end against the source:

| Layer | Has a `keyframes` (leaf-keyed replay) concept? | Evidence |
|---|---|---|
| **Solar runtime** `@lumencast/runtime` | **YES.** `RenderNode.keyframes?: Keyframes`; `Keyframes.key` = "LeafPath whose value-change replays the sequence"; `KeyframePlayer` remounts on `key` change. | `dist/render/bundle.d.ts:18-21`; `dist/animate/keyframes.d.ts` (`Keyframes.key`); `dist/render/keyframe-player.d.ts` |
| **`buildWipeCoverNode`** (Solar src) | **YES — emits exactly that node.** A `frame` with `keyframes:{key:leafPath, duration_ms, steps[reveal/hold/retract]}`. | `Solar/src/overlay/wipe-cover.ts:122-161` |
| **LSML authoring vocab** `@lumencast/compiler` `lsml-types.d.ts` | **NO.** `LSMLBaseNode` has `bind`, `bindStyle`, `bindUniversal`, `animate` (a **single** transition: one `transform`/`opacity`/`filter` target). **No `keyframes`, no multi-step, no leaf-keyed replay.** | `lsml-types.d.ts` (`LSMLBaseNode`, `LSMLAnimateDirective`) |
| **LSML→RenderBundle compiler** `@lumencast/compiler/compile.js` | **NO.** Emits `props`/`bindings`/`transitions` only; `node.animate → transitions`. **Zero occurrences of `keyframes`** in the whole package. | `compile.js:50-223`; `grep -rn keyframes @lumencast/compiler` = 0 hits |
| **Orion compiler** (Go) | **NO.** `LayoutNode` struct fields = `Kind,ID,Props,Bindings,Transitions,Children,ComponentArgs` — **no `Keyframes`**. `lsmlNode` emits only `kind/id/<props>/bind/animate/children/animations`; `isReservedNodeKey` doesn't even know `keyframes`. **Zero `keyframe` occurrences in all of `internal/`.** | `Orion/internal/compiler/types.go:80-92`; `emit_lsml.go:91-156` |
| **M10 fixture scene** | **NO node.** `layout` is a black `frame` + a `text` marker; it only **declares the leaf** (`operator_inputs`) so Orion fans the delta out. No wipe-cover node anywhere. | `Pulsar/scripts/fixtures/m10-orion-scene.lsml.json:23-36` |
| **M10 standin** (loopback) | **YES — hand-built, bypasses the compiler.** `build_wipe_cover_bundle` writes the `keyframes` block directly into the served bundle's `root`. **This is the only thing that makes the loopback proof work.** | `Pulsar/scripts/m10_orion_standin.py:158-225` |

**Two hard facts seal it.** (1) Go's default `json.Unmarshal` **silently drops unknown
keys**, so even a hand-authored `keyframes` block in a pushed scene definition is
**discarded at Orion ingest** before compilation. (2) The opaque `animations` pass-through
in `emit_lsml.go:137-142` is **not** a usable carrier: it is passed `nil` at the only call
site (`scenes_push.go:216`), it rides the **root node's `animations` key** (an LSML-1.0
operator-animation tree), and the Solar `KeyframePlayer` reads **per-node `keyframes`**, not
a root `animations` blob. So `buildWipeCoverNode` is called by **nobody** in the real fil —
only by the loopback standin. **`--real-orion` mode (#79) loads the bundle Orion actually
serves for the active scene, which has no keyframed node → no overlay → no MID=magenta.**

### A5.1 CORRECTION 1 — §A4.2 "exactly the M9 repaint mechanism" is INEXACT

§A4.2 (l.1164-1165) states the Solar overlay is "an **authored Canvas/Solar overlay
element keyed off the leaf**, **exactly the M9 repaint mechanism** Quasar/Blue already
drive." **This is factually wrong against the runtime**, in two ways:

1. **M9 is a `bind` (value-snap), not a keyframe replay.** The M9 background-colour repaint
   used `bind:{background:<leaf>}` — a **value-binding**: the runtime subscribes the leaf's
   signal and **assigns** the new value to the prop on each delta. It produces an
   **instantaneous snap** (optionally smoothed by a single `animate` transition). It does
   **not** replay a reveal→hold→retract timeline. The wipe-cover needs the leaf delta to
   **re-trigger a multi-step opacity sequence from t=0** — that is `RenderNode.keyframes`
   with `key=<leafPath>`, a **different runtime primitive** (`KeyframePlayer` remount), and
   a **different authoring vocabulary** than M9's `bind`.
2. **That vocabulary does not exist in the authoring chain.** LSML has `bind`/`bindStyle`/
   `bindUniversal`/`animate` (single transition) — **no keyframes** (A5.0). So "exactly the
   M9 mechanism" cannot produce the wipe-cover node: M9's mechanism (`bind`) **is** emittable
   and **is** wrong for this; the right mechanism (`keyframes`) **is** correct and **is not**
   emittable. The premise conflated "leaf-driven" (true of both) with "same mechanism"
   (false). This amendment supplies the missing maillon.

**The corrected statement:** the wipe-cover overlay is leaf-driven **like** M9, but via the
runtime's **`keyframes` replay** primitive (`Keyframes.key`), **not** M9's value-`bind`. The
authoring layer must learn to emit `RenderNode.keyframes`; it cannot today.

### A5.2 CORRECTION 2 — the `scene_control` leaf transports as a **string-JSON**, not an object (Vigil's §A4.2 finding, integrated)

§A4.2's leaf block (l.1148-1158) and the A4.2 prose describe the leaf value as an **object**
`{target_scene, overlay{…}, cut_at_ms}`. **On the wire it is a JSON string**, not a JSON
object. Verified: a raw object is **not LSDP-legal** — `assertLeafValue` rejects it with
`INVALID_VALUE`; the contract's `encode_scene_control_leaf` does
`json.dumps(validated, sort_keys=True, separators=(",",":"))` → a deterministic **string**,
and the standin mirrors it byte-for-byte (`m10_orion_standin.py:144-156`,
`encode_scene_control_leaf`). The object shape in §A4.2 is the **decoded/logical** shape; the
**transport** shape is `string`.

**Why this is benign for the replay trigger and must not regress.** The `KeyframePlayer`
replays purely on **whether the value at `keyframes.key` CHANGED** (string inequality
`lastKeyValue.current !== v`), so a stable, sorted, separator-pinned string is exactly the
right carrier: any contract-meaningful change yields a new string → a replay. **The two
consumers decode the string** (Solar parses the `overlay` sub-object; Prism parses
`target_scene`+`cut_at_ms`). **Constraint pinned:** the encoder is **canonical**
(`sort_keys=True`, compact separators) so identical logical values produce **identical bytes**
— a non-canonical re-encode would spuriously re-trigger the replay or break the consumers'
round-trip. The Conduit contract (#70, re-confirmed in #91 below) owns this canonical-string
invariant. **Solar's `keyframes.key` keys on the leaf path, and the value it compares is the
string** — no object identity is involved, so the string transport is correct, not a
workaround.

### A5.3 DECISION — the authoring maillon: **(B) a Canvas/Orion `wipe-cover` user-component-ish authoring primitive that compiles to `RenderNode.keyframes`**, NOT a general LSML keyframes extension

The maillon must turn "the active Orion scene carries a `wipe-cover` overlay keyed on the
`scene_control` leaf" into a served bundle whose `root` (or a child) **is** the keyframed
node `buildWipeCoverNode` produces. Three candidate seams were evaluated against the source.

| Option | What it touches (churn by repo) | Genericity | Coherence w/ reactive model | Risk | Verdict |
|---|---|---|---|---|---|
| **(A) General LSML keyframes vocab** — add a `keyframes{key,steps[],duration_ms,easing}` directive to `lsml-types.d.ts`, teach `@lumencast/compiler/compile.js` to emit `RenderNode.keyframes`, **and** mirror it in Orion (`LayoutNode.Keyframes` field + `lsmlNode` emit + `isReservedNodeKey`). | **@lumencast/compiler (TS): types + compile**, **Orion (Go): struct + emit + lowering**, plus a Canvas authoring affordance. **3–4 repos, two compilers in lock-step.** | **Highest** — any future keyframed widget reuses it. | High — it's the runtime's own §6.6 vocab finally surfaced to authoring; clean. | **High churn, two-compiler contract.** The TS and Go compilers must agree byte-for-byte (LSML hash reconciliation, `scenes_push.go:206-216`); a keyframes directive that one emits and the other drops re-opens `LSML_HASH_MISMATCH`. Large, and most of it unused by M10. | **Rejected for M10** (revisit as a follow-up, A5.6). |
| **(B) A `wipe-cover` authored overlay primitive that lowers to the keyframed node** — model the overlay as a **named element** (a Canvas user-component / an Orion-recognised authoring node `kind:"wipe-cover"`) whose **lowering** calls the equivalent of `buildWipeCoverNode(leafPath, overlay)` and emits the `RenderNode.keyframes` directly into the served bundle. The leaf path + timings come from the scene's declared `scene_control` operator-input. | **Orion (Go): one lowering case** that recognises the overlay element and emits the keyframed `LayoutNode` (requires adding a `Keyframes` field to `LayoutNode` + emitting it — but **scoped to this one element**, not a general directive). **Solar**: `buildWipeCoverNode` already exists (reused as the canonical shape / parity oracle). **Canvas/fixture**: author the element in the M10 scene. **2 repos (Orion + fixture), Solar unchanged.** | Medium — reusable for "opaque cover" overlays; not arbitrary keyframes. | **Highest** — the leaf-keyed `keyframes.key` is exactly the reactive trigger; the element is declared once and replays on every `scene_control` delta, matching the M9-style fan-out. | Medium — adds a `Keyframes` field to Orion's `LayoutNode` + one emit path; contained, no general two-compiler directive. **The keyframe *shape* is owned by one builder** (parity-tested against `buildWipeCoverNode`). | **CHOSEN.** |
| **(C) Hand-authored bundle served by Orion (the standin path, productised)** — give Orion a route to serve a **pre-built** bundle whose `root` is the keyframed node, bypassing LSML compilation (what the standin does). | **Orion (Go): a new "serve a pre-compiled bundle" ingest/route**, plus a builder (Solar's `buildWipeCoverNode` exported to a build step). **Bypasses the compiler/LSML-hash/identity machinery** (`scenes_push.go` adopt-on-verify). | Low — one-off escape hatch. | Low — it sidesteps the reactive authoring model entirely; the bundle is opaque to Orion, so future leaf/scene changes don't reconcile. | **High architectural** — a second, un-compiled bundle path defeats content-hash identity, LSML-hash reconciliation, and `sceneAcceptsPath` fan-out guarantees. It's the standin's shortcut promoted to prod — exactly the thing that made the loopback proof unrepresentative. | **Rejected** — re-introduces the standin's architectural shortcut as a permanent surface. |

**Why (B), precisely.** (B) is the **moindre-churn** path that stays **inside the reactive
authoring model**: it adds **one** recognised overlay element whose lowering emits the
keyframed node, rather than a general keyframes grammar that forces the TS and Go compilers
into a new lock-step contract (A's cost) or a parallel un-compiled bundle path (C's cost).
It reuses `buildWipeCoverNode` as the **canonical shape oracle** (Solar already owns it; a
parity test pins Orion's emitted shape to it), so the keyframe geometry has **one** source of
truth. It respects Solar's doctrine ("new widgets are authored compositions, not runtime
releases", Solar CLAUDE.md) — `wipe-cover` is an **authored overlay element**, and the
**runtime primitive it needs (`keyframes`) already ships** (`RenderNode.keyframes`, LSML 1.1
§6.6), so **no `@lumencast/runtime` change is required**. The only new field is Orion's
`LayoutNode.Keyframes` (so the served render bundle can carry it) + its emit; this is additive
and `omitempty`, breaking nothing.

**Net repo touch for the chosen maillon:**
- **Orion (Go)** — add `Keyframes json.RawMessage \`json:"keyframes,omitempty"\`` to
  `LayoutNode` (`types.go`); recognise the `wipe-cover` overlay element during
  lowering/expansion and emit the keyframed node (reveal/hold/retract from the declared
  `scene_control` overlay timings, `key` = the declared leaf path); carry `keyframes`
  through `lowerRenderTree`/`copyProps` so it survives lowering; **and** add it to the
  served render bundle (`render-bundle`). **It need NOT be added to the LSML 1.1 emit**
  (`lsmlNode`/`isReservedNodeKey`) for M10 — the LSML bundle is the authoring-hash artifact,
  and the wipe-cover keyframes can be confined to the **render bundle** Solar fetches (the
  LSML keyframes vocab is the deferred general work, A5.6). *If* the LSML-hash reconciliation
  (`scenes_push.go`) requires the authoring tree to round-trip the element, Orion treats the
  `wipe-cover` element as an opaque authored node (like a user-component reference) so the
  hash stays stable — Forge confirms during build (spike SPIKE-LSML-HASH, A5.5).
- **Solar (TS)** — **no runtime change**; `buildWipeCoverNode` is kept and **exported as the
  parity oracle** for the contract test (Orion's emitted keyframed node must match its
  shape). The `--real-orion` Solar bundle is then the **real** one Orion serves, not the
  standin's.
- **`@lumencast/compiler`** — **untouched for M10.** (Touching it is Option A, deferred.)
- **Fixture / Canvas** — the M10 Orion scene (`m10-orion-scene.lsml.json`) authors the
  `wipe-cover` overlay element above the marker, keyed on
  `__inputs.blue.m10-scene-control.scene_control`, so the served bundle carries the keyframed
  node. (It already declares the leaf for fan-out; it now also authors the node.)

**The porteur's standing permission to touch `@lumencast/*`** is acknowledged: the cheapest
*correct* maillon (B) deliberately **does not need it** — the runtime already has
`keyframes`, and the compiler change is the expensive general path (A) we defer. So M10 ships
without a Lumencast lib release; only Orion (+ fixture, + a Solar export) move.

### A5.4 Reconciliation with §A4.2 and the existing #73

§A4.2's mechanism stands **once the maillon exists**: Solar still renders the leaf-driven
opaque overlay; Prism still fires the `cut_at_ms` hard-cut; the leaf is still the synchro
contract. What A5 fixes is the **silent assumption** that the served bundle already contains
the keyframed node. **#73 is re-scoped** (A5.7): it is no longer "author the element and trust
it replays" — it is "**the Orion lowering emits `RenderNode.keyframes` for the `wipe-cover`
element, byte-shape-pinned to `buildWipeCoverNode`, and the served render bundle carries it;**
a leaf delta on the real wire replays it on the CEF surface."

### A5.5 Risks (extends §A4.5)

- **R-AUTH (new, engineering) — the maillon is a compiler/lowering change on the broadcast
  render path.** Adding a `Keyframes` field + an overlay-lowering case to Orion touches the
  bundle Solar renders on air. A wrong emit (bad `times[]`, wrong `key`, dropped on lowering)
  = no replay or a broken overlay mid-broadcast. Guarded by: the parity contract test against
  `buildWipeCoverNode` (#90), the probe's real-wire MID=magenta proof (#75/#92), and the
  lowering round-trip test. *Engineering, not security.*
- **SPIKE-LSML-HASH (new, build gate, small).** Confirm that authoring a `wipe-cover` overlay
  element does **not** break the LSML-hash reconciliation (`scenes_push.go:206-216`,
  `LSML_HASH_MISMATCH` / adopt-on-verify): either the element round-trips through `lsmlNode`
  as an opaque authored node, or the render-bundle-only keyframes path is confirmed not to
  feed the LSML hash. **If it breaks the hash, the maillon must also touch the LSML emit
  (drifting toward Option A) — measure first.** (Owner: Forge spike, Conduit confirms the
  contract.)
- **No new security surface.** The maillon changes **how the served bundle is built**, not
  **what crosses the network** nor any auth/transport. The leaf, the fan-out, the
  `target_scene` allowlist, the operator-gated trigger, the read-only viewer subscription —
  **all unchanged from A4**. R6 (broadcast-control) and R4 (desktop capture) are **not
  re-weighted** by A5. → Bastion: confirm the maillon introduces no path by which a **leaf
  value** can inject arbitrary `keyframes`/`key`/node shape into the bundle (the keyframe
  geometry is **authored in the scene**, the leaf supplies only the *replay trigger* and the
  decoded timings the consumers read — the served node's `keyframes.steps` come from the
  **scene's declared overlay**, never from the live leaf value). This is the A5 analogue of
  the A2.1 "leaf carries no `path`" invariant: **the leaf carries no node shape.**

### A5.6 Deferred — the general LSML keyframes vocab (Option A) as a follow-up

Surfacing the runtime's §6.6 `keyframes` into the LSML authoring vocab + both compilers is a
**real, generic capability** (any future leaf-replayed multi-step animation). It is **out of
scope for M10** (Option A's churn) but **recorded as the principled successor**: when a second
keyframed widget is needed, promote (B)'s one-element lowering to (A)'s general directive. A
follow-up ADR (or an amendment here) owns it; M10 does not pay for it.

### A5.7 Issue delta (supersedes the §A4.7 #73 scope; adds the parity / contract / real-wire issues)

> **Filed issue numbers (the §A4.7 #73 re-scope + three new):** Orion emit =
> **ZabLaboratory/Orion#64** (the re-scoped #73); Pulsar parity oracle =
> **Pulsar#89**; Pulsar canonical-string contract = **Pulsar#90**; Pulsar real-wire
> proof = **Pulsar#91**. (The §A5.7 mnemonic labels #73/#90/#91/#92 below map to those.)

- **#73 → Orion#64 (Forge, Orion + fixture) — RE-SCOPED: emit the keyframed `wipe-cover`
  node in the REAL pipeline.** Add `LayoutNode.Keyframes` (`json:"keyframes,omitempty"`,
  `types.go`);
  recognise the `wipe-cover` overlay authoring element and lower it to a `RenderNode` whose
  `keyframes` = `{key:<declared scene_control leaf path>, duration_ms:reveal+hold+retract,
  easing:"ease-in-out", steps:[0→0, revealAt→1, holdEndAt→1, 1→0]}` — **byte-shape identical
  to `Solar/src/overlay/wipe-cover.ts::buildWipeCoverNode`**; carry `keyframes` through
  lowering (`lowerRenderTree`/`copyProps`) and into the served `render-bundle`; author the
  element in `fixtures/m10-orion-scene.lsml.json`. **RC:** (a) `POST /scenes/{id}/push` of
  the M10 scene yields a `render-bundle` whose tree contains a node with
  `keyframes.key == "__inputs.blue.m10-scene-control.scene_control"` and the 4-step
  reveal/hold/retract `steps`; (b) the emitted node matches `buildWipeCoverNode`'s output for
  the same overlay timings (parity test #90); (c) a leaf delta on `--real-orion` replays the
  overlay on the CEF surface (no standin); (d) Go `json.Unmarshal` round-trips `keyframes`
  (no silent drop); (e) `go test ./...` + `golangci`/`staticcheck` green.
- **#90 → Pulsar#89 (Probe / Conduit — parity oracle test).** A cross-repo test asserting Orion's emitted
  keyframed node is **shape-identical** to `buildWipeCoverNode(leafPath, overlay)` for a
  battery of overlay timings (incl. the fixture's 400/500/400). Pins the single-source-of-truth
  invariant (A5.3). **RC:** parity holds for ≥3 timing tuples incl. edge `reveal=hold=retract`;
  the test fails loudly if Orion's `times[]`/`key`/`duration_ms` drift from the builder.
- **#91 → Pulsar#90 (Conduit — re-confirm the canonical string-JSON leaf transport, A5.2).** The
  `scene_control` contract (#70) explicitly pins: leaf value is a **canonical JSON string**
  (`sort_keys`, compact separators); a raw object is `INVALID_VALUE`; identical logical values
  ⇒ identical bytes. **RC:** contract test asserts encode→decode round-trip and byte-stability;
  a non-canonical re-encode is rejected/normalised; Solar and Prism both decode the string.
- **#92 → Pulsar#91 (Probe — real-wire keyframes proof, folds into #75).** Extends the #75 antenna run:
  in `--real-orion`, the CEF loads the **Orion-served** bundle (the standin is used **only**
  for the loopback regression, never as the `--real-orion` source); assert the served bundle
  carries the keyframed node and that a Blue-pushed leaf delta (VPS-origin) replays it
  (MID frame = magenta cover) — proving the **whole real fil**, not the hand-built bundle.
  **RC:** with `--real-orion`, MID=magenta sourced from the Orion bundle; a `--standin` run
  remains green as the isolated runtime regression. SPIKE-LSML-HASH (A5.5) lifted before #73
  merges.
- **SPIKE-LSML-HASH (Forge, before #73 merge)** — A5.5: confirm authoring the overlay element
  doesn't break `LSML_HASH_MISMATCH`/adopt-on-verify; decide render-bundle-only vs
  LSML-emit-too.
- **#76 (Bastion) — extends:** confirm the A5 maillon adds no path for a **leaf value** to
  inject node shape/`keyframes`/`key` (geometry is scene-authored, leaf is replay-trigger +
  decoded timings only — the "leaf carries no node shape" invariant, A5.5). No re-weight of
  R4/R6; A5 introduces no new network/auth surface.

### A5.8 Residual unknowns

- **SPIKE-LSML-HASH** (A5.5) — the one build-gate unknown: does the overlay-element authoring
  perturb the LSML content-hash reconciliation? Small, must be lifted before #73 merges.
- **No product questions.** A5 is a pure mechanism/authoring correction; nothing for the
  porteur to decide. The maillon (B), the two §A4.2 corrections, and the deferral of the
  general LSML vocab (A) are architectural calls recorded here for Vigil's re-validation.
