# `scene_control` cross-service contract (M10)

**Owner:** Conduit (integration / contracts).
**Authority:** ADR 003 Amendment 2 §A2.1 (Pulsar `docs/adr/003-blue-driven-obs-scene-transition.md`)
— frozen, Bastion-cleared (veto R7 lifted at design level), Vigil-reviewed.
This package **formalises** that schema; it does not re-open it.
**Issue:** ZabLaboratory/Pulsar#59.

## What this is

The single source of truth for the `scene_control` contract that travels:

```
Blue (#58, producer)  →  Orion leaf  __inputs.blue.<slug>.scene_control  →  Prism (#63) / m10 probe (#61) (consumers)
```

- `__init__.py` — the canonical **validator** (`validate_scene_control`) and
  **leaf-path** helpers (`build_leaf_path`, `assert_canonical_leaf_path`).
  Pure stdlib, no network, no OBS, no Pydantic.
- `fixtures/valid.json`, `fixtures/malicious.json` — the **shared corpus**
  both producer and consumer reference. Drift between sides is caught here,
  exactly like the platform's `blue.models.canonical` convention.
- `test_scene_control_contract.py` — the **contract test**: round-trips the
  fixtures through Blue's *real* `leaf_mapper` (producer) and the canonical
  validator (consumer), and proves every malicious payload is rejected.

## The frozen schema (leaf VALUE)

```jsonc
__inputs.blue.<slug>.scene_control = {
  "action": "switch_program_scene",      // allowlisted verb — ONLY this value
  "target_scene": "scene-screen-2",      // ∈ closed scene allowlist (#60)
  "transition": {
    "kind": "stinger",                   // ∈ {stinger, fade}
    "asset_id": "stinger-demo",          // allowlist KEY — NEVER a path
    "point_ms": 300,                     // int, 0..20000
    "duration_ms": 600                   // int, 50..20000 (obs-ws clamp)
  }
}
```

**Canonical leaf path is 3 segments** (`__inputs.blue.<slug>.scene_control`,
ADR §A2.2 / F1). The 2-segment `__inputs.blue.scene_control` is a bug and is
rejected — Blue's `leaf_mapper` (`leaf_mapper.py:38,160`) makes it structurally
impossible to write.

## Security invariants (Bastion — non-negotiable)

| Condition | The contract enforces |
|---|---|
| **C-PATH** | No `path` field anywhere. `transition` carries an allowlisted `asset_id` key only; a path-shaped id (`/`, `\`, `..`, UNC `\\`) or an off-allowlist id is rejected. The real media path is resolved **locally** by the consumer (`asset_id → ASSET_ALLOWLIST[id]`), never from the leaf. |
| **C-INJ** | `action`, `target_scene`, `transition.kind` are each validated against closed allowlists. Any miss ⇒ reject ⇒ **0 obs-ws calls**. |
| **C-PATHREAL** | The consumer matches the real 3-segment leaf path; the 2-segment / wrong-port / dotted-slug forms are rejected. |

## How #58 (Blue) and #63 (Prism) consume this WITHOUT divergence

- **Blue #58** produces the leaf VALUE shown above and writes it to
  `__inputs.blue.<slug>.scene_control` via its existing `leaf_mapper` +
  `orion_client.push_leaf`. Blue's CI imports `fixtures/valid.json` (vendor or
  git-submodule-free copy) and asserts each `value` matches what its
  scene-control blueprint emits. **Blue does not re-implement the validator** —
  it produces; this contract validates.
- **Prism #63** imports the **validation rules** of `validate_scene_control`
  (port the logic to TS as a `isSceneControl(value): value is SceneControl`
  guard, the same convention as `scene-events-bridge.ts` / `animation-bridge.ts`),
  driven by **the same `fixtures/*.json`** so the TS guard and this Python
  validator are proven to agree case-for-case. Prism owns the
  `asset_id → local absolute path` `ASSET_ALLOWLIST` map (#64) and the
  `target_scene` allowlist (#60) — it passes them into the validator.
  The fixtures are the contract; the guard is a faithful re-expression, not a
  fork.

> Promotion path (deferred, same as `canonical.py`): if a third consumer lands
> or the copies drift painfully, promote `fixtures/` + the validator to a shared
> package. Until then, shared fixtures + this contract test are the cheapest
> thing that holds the contract.

## Run

```bash
pytest scripts/contracts/scene_control/test_scene_control_contract.py -v
```

The test binds Blue's **real** `leaf_mapper` when Blue is checked out as a
sibling repo (`<Structure>/Blue`); otherwise it uses a declared in-test mirror
of the identical 3-segment rule and prints a blind-spot note to stderr.
