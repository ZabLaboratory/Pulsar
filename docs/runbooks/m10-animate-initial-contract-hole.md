# Runbook — M10 mount-play: Orion render-bundle drops `animate_initial`

**Applies to:** the Keeper antenna run that pushes the **animated**
`zab-transition` scene to the real Orion and expects Solar to play the logo
mount ramp (`scripts/probe-m10-canvas-live.py --transition-scene --real-orion`
with `SOLAR_VPS_VERSION=0.2.3`, Pulsar PR #97 fixture).

**Status:** OPEN — antenna ramp BLOCKED. Root cause isolated to an Orion
render-bundle serialization gap (a cross-service schema mismatch). Fix lives in
**Orion** (Go render-bundle lowering), not in Pulsar/Solar/Lumencast.
Needs **Conduit** (contract realignment) + **Forge** (Orion fix).

**Run date:** 2026-06-09. Mount-play unblock sequence (Lumencast v0.3.0 →
Solar v0.2.3 → Pulsar #97) all green up to this gap.

---

## Symptom

With the full chain shipped and served:

- `@lumencast/runtime@0.3.0` published to npm (mount-play via framer `initial`
  from-state, lumencast-js #23).
- Solar `v0.2.3` built against `^0.3.0`, vendored on the VPS and **served 200**
  at `/static/solar/v0.2.3/` (generator `@zablab/solar 0.2.3`); the served
  `assets/tree-*.js` **contains** `animate_initial`.
- Pulsar #97 fixture `zab-transition.lsml.json` authored with the logo
  `animate.from {opacity:0, transform.scale:0.85}` → `{opacity:1, scale:1}`,
  550ms ease-out, and the Blue `scene_control` leaf declared.

…the logo still **snaps** instead of ramping on the antenna run, because the
render-bundle the real Orion serves carries **no `animate_initial` field**.

---

## Diagnostic (executed, not deduced)

Pushed the animated fixture to the real Orion via the etage-1
`ORION_OPERATOR_TOKEN` (`build/keeper_m10_animated_setup.py`, gitignored),
captured the rollback baseline `18fecbd4-…` first, then inspected the **served**
render-bundle at the authoritative `scene_version`:

```
GET /orion/api/v1/scenes/{id}/render-bundle?v=sha256:60553897…
-> HTTP 200, white frame + logo data-URI present
   animate_initial PRESENT = False
```

Deep-inspecting the served image node:

```json
{
  "id": "zab-logo", "kind": "image",
  "props": { "alt": "Zablab logo", "fit": "contain", "src": "data:image/jpeg;base64,…" },
  "transitions": {
    "from": { "opacity": 0, "transform": { "scale": 0.85 } },
    "opacity": 1, "transform": { "scale": 1 },
    "transition": { "easing": "…", … }
  }
}
```

The directive is **not** fully dropped: the compiler lowered `animate` into a
`transitions` map and **`from` is preserved inside it**. The gap is the
**shape**:

- **Producer (Orion render-bundle):** emits the from-state nested as
  `transitions.from`.
- **Consumer (`@lumencast/runtime@0.3.0`):** reads the mount-play initial state
  off a **flat top-level** field. Confirmed in the published bundle
  (`dist/tree-*.js`): the primitive passes `animateInitial: t.animate_initial`
  to framer-motion's `initial=`. It reads **`t.animate_initial`**, never
  `t.transitions.from`.

So the runtime gets `initial = undefined` → framer-motion mounts at the target
state → **no ramp**. This is a render-bundle **contract schema mismatch**, not a
runtime, Solar, or fixture bug.

---

## Resolution criteria (what "fixed" looks like)

The served render-bundle node must carry a flat `animate_initial` map the
runtime reads, e.g.:

```json
{ "kind": "image", "props": {…},
  "animate_initial": { "opacity": 0, "transform": { "scale": 0.85 } },
  "transitions": { … } }
```

Verify with the same probe:

```
GET …/render-bundle?v={authoritative}
-> animate_initial PRESENT = True
```

Then re-run `build/keeper_m10_animated_setup.py` (expects rc=0, not rc=3) and the
dry `probe-m10-canvas-live.py --transition-scene --real-orion --no-broadcast`
with `SOLAR_VPS_VERSION=0.2.3` must reach **ANIMATED (VISIBLE)** on the
`seq-mount-NN.png` sequence before any `--broadcast`.

---

## Fix owner & where

- **Repo:** `Zab/Orion` — the Go render-bundle lowering that serializes the
  scene node (`LayoutNode` round-trip Forge flagged pre-emptively).
- **Action:** promote the lowered from-state to a flat `animate_initial` field
  on the node (align with `@lumencast/runtime` 0.3.0's reader), or have the
  vendored `@lumencast/compiler` emit `animate_initial` and have Orion preserve
  it through the round-trip instead of folding it into `transitions.from`.
- **Owners:** **Conduit** (lock the render-bundle ↔ runtime contract for the
  mount-play field) + **Forge** (Orion implementation). This is the canonical
  cross-service contract realignment — not a Keeper hotfix.

---

## What was NOT done (and why)

- **No `--broadcast`.** The guard is explicit: ramp not proven → do not go live.
  The antenna ramp is unprovable while the served bundle lacks the field.
- **No workaround.** Not patching Solar to read `transitions.from`, not
  hand-editing the served bundle. The fix is a contract realignment in Orion;
  masking it in the consumer would re-introduce the very seam this run exposed.

---

## Rollback (done)

The animated scene was pushed active during diagnosis, then restored:

```
build/keeper_m10_rollback.py
-> active before = f00296c8-… (animated zab-transition)
-> active after  = 18fecbd4-… (baseline, restored = True)
```

Orion is on the baseline `18fecbd4-a11b-434f-9173-3041c511991a`. Solar `v0.2.3`
remains vendored/served (additive, no rollback needed — it is correct and
forward-compatible once Orion emits the field).
