# `scene_control` cross-service contract (M10 — overlay form)

**Owner:** Conduit (integration / contracts).
**Authority:** ADR 003 **Amendment 4 §A4.2** (Pulsar `docs/adr/003-blue-driven-obs-scene-transition.md`)
— the PIVOT-FINALISED leaf shape. §A4.2 is the authority; this package
**formalises** it, it does not re-open it. §A4.7 #70 pins the cut-window
invariant. Surface sensible (broadcast-control) → Bastion re-clearance **#80**.
**Issue:** ZabLaboratory/Pulsar#74.

## The pivot (why this shape, not the old stinger one)

The transition is **no longer an OBS-native media transition**. Our engine
(Solar/CEF) animates a full-screen **opaque overlay** layered *over* two
`monitor_capture` captures; the screen-1→screen-2 change of the content
underneath is an **instantaneous hard-cut** hidden under the overlay's opaque
plateau. So the leaf co-specifies the **overlay animation** (Solar reads) AND
the **`cut_at_ms`** (Prism executes). **Gone:** the OBS-native transition, the
media `asset_id`, any `path`, any OBS action verb (`action` /
`switch_program_scene` / `transition`). Those belong to the superseded
Amendment 1/2 OBS-native approach, now **dormant behind a flag** (Q4) and OFF
the live contract.

## What this is

The single source of truth for the `scene_control` contract that travels:

```
Blue (#71, producer)  →  Orion leaf  __inputs.blue.<slug>.scene_control  →  Solar #73 + Prism #72 + m10 probe #75
```

- `__init__.py` — the canonical **validator** (`validate_scene_control`) and
  **leaf-path** helpers (`build_leaf_path`, `assert_canonical_leaf_path`).
  Pure stdlib, no network, no OBS, no Pydantic.
- `fixtures/valid.json`, `fixtures/malicious.json` — the **shared corpus** all
  consumers reference. Drift between sides is caught here, exactly like the
  platform's `blue.models.canonical` convention.
- `test_scene_control_contract.py` — the **contract test**: round-trips the
  fixtures through Blue's *real* `leaf_mapper` (producer PATH machinery) and
  the canonical validator (consumer), proves every malicious payload is
  rejected, and asserts the cut-window invariant both directions.

## LSDP leaf transport — the value travels as a JSON **string**

> **M10 transport amendment (Conduit, `conduit/scene-control-leaf-transport`).**
> The `scene_control` VALUE is an **object**, but the LSDP/1 wire **forbids
> object leaf values**. `@lumencast/protocol` `codec.ts::assertLeafValue`
> admits only `string | number | boolean | null | LeafValue[]`; a plain object
> raises `INVALID_VALUE` ("objects are forbidden in patch values, push
> leaf-grain instead") at the consumer's **decode** — in both
> `delta.patches[].value` and `snapshot.state[path]`. Solar decodes every
> inbound frame through `decodeServerFrame` (`@lumencast/runtime`
> transport/ws.js → mount.js → applyDelta), so an object leaf is rejected on
> the wire → transport error → reconnect loop → the overlay never paints. M9
> worked because its leaves were **scalar** (e.g. `…colour = "#1A9E57"`).

The object therefore travels **serialised as a JSON string** in the **same**
single scalar leaf `__inputs.blue.<slug>.scene_control`:

```
Blue: object → encode_scene_control_leaf() → JSON string  (LSDP-legal scalar)
            → leaf __inputs.blue.<slug>.scene_control (1 leaf, atomic)
Prism/probe: leaf string → decode_scene_control_leaf() → object → validate
Solar: needs no decode — KeyframePlayer replays on the value CHANGING
       (`lastKeyValue.current !== v`); a JSON string flips by !== like any
       scalar, and the overlay timings live in the compiled bundle node.
```

`encode_scene_control_leaf` **validates the object first**, so an off-contract
value never reaches the wire. `decode_scene_control_leaf` JSON-parses then runs
the **unchanged** `validate_scene_control`, so every invariant below is
preserved verbatim — only the transport envelope is added. The path rule
(C-PATHREAL, 3 segments) is **unchanged**.

## The frozen schema (leaf VALUE — object, transported as its JSON string)

The schema below is the **logical** value the validator operates on. On the
LSDP wire it is the **JSON string** of this object (see the transport section
above).

```jsonc
__inputs.blue.<slug>.scene_control = {
  "target_scene": "scene-screen-2",   // OBS content scene the hard-cut switches to;
                                       // ∈ closed scene allowlist (#74). Read by Prism #72.
  "overlay": {                         // the Solar animation (M9 render input). Read by Solar #73.
    "kind": "wipe-cover",              // ∈ closed overlay-kind allowlist (authored Solar element)
    "reveal_ms": 250,                  // int 1..20000 — 0 → fully opaque
    "hold_ms": 200,                    // int 1..20000 — fully-opaque plateau (the cut window)
    "retract_ms": 250                  // int 1..20000 — opaque → 0
  },
  "cut_at_ms": 250                     // int 0..20000 — offset from leaf-apply when Prism fires the
                                       // hard-cut. MUST satisfy reveal_ms <= cut_at_ms <= reveal_ms+hold_ms
}
```

**Canonical leaf path is 3 segments** (`__inputs.blue.<slug>.scene_control`,
ADR §A2.2 / F1 — unchanged by the pivot). The 2-segment
`__inputs.blue.scene_control` is a bug and is rejected — Blue's `leaf_mapper`
(`leaf_mapper.py:46,168`, `\Z`-anchored) makes it structurally impossible to
write.

## Invariants (encoded as hard rejects)

| Invariant | The contract enforces |
|---|---|
| **NO-OBS-NATIVE** | No `path`, no `asset_id`, no `action` / OBS verb, no old `transition` object — anywhere. Reject-unknown-fields is **strict at BOTH levels** (top + `overlay`), as Bastion validated on the prior contract; any of those constructs is caught as an unknown key. |
| **SCENE-ALLOWLIST** | `target_scene` ∈ a closed set (injected by the consumer — `{scene-screen-1, scene-screen-2}` for #72). No arbitrary scene-name into `SetCurrentProgramScene`. |
| **OVERLAY-KIND-ALLOWLIST** | `overlay.kind` ∈ a closed set (authored Solar elements; v1 `{wipe-cover}`). No unauthored overlay element key. |
| **CUT-WINDOW** | `reveal_ms <= cut_at_ms <= reveal_ms + hold_ms` — the hard-cut **must** land inside the opaque plateau, or it is **seen** on air. The visual-safety core. SPIKE-CUT (#75) measures the real skew margin; the contract guarantees at least that the VALUE is internally coherent. |
| **bounds** | `reveal_ms`/`hold_ms`/`retract_ms` ∈ [1, 20000]; `cut_at_ms` ∈ [0, 20000]; all **strict ints** (no bool, no float — no silent coercion). |
| **C-PATHREAL** | The consumer matches the real 3-segment leaf path; the 2-segment / wrong-port / dotted-slug / trailing-newline forms are rejected (`\Z` anchor). |

## How the three consumers stay aligned (no divergent validator)

- **Blue #71/#30 (producer)** rewrites `build_scene_control`
  (`Blue/src/blue/services/scene_control.py`) to build the object VALUE above,
  then **serialises it to the LSDP-legal JSON string** before it reaches the
  leaf — the leaf VALUE written to `__inputs.blue.<slug>.scene_control` via
  `leaf_mapper` + `orion_client.push_leaf` is the **string**, not the object
  (a dict leaf would be rejected on the wire with `INVALID_VALUE`). The mirror
  of `encode_scene_control_leaf` is the producer seam. Blue's CI imports
  `fixtures/valid.json` and asserts each `value` round-trips object→string→
  object. **Blue does not re-implement the validator** — it produces; this
  contract validates. *(On `main` today `build_scene_control` still emits the
  OLD stinger value AND pushes a bare object; both the overlay rewrite and the
  string-encode are #71/#30, separate from this Conduit re-freeze.)*
- **Solar #73/#77 (overlay consumer)** needs **no decode at all**: its
  `KeyframePlayer` (`@lumencast/runtime` render/keyframe-player) replays the
  reveal/hold/retract sequence purely on the leaf value **changing**
  (`lastKeyValue.current !== v`) — a JSON string flips by `!==` exactly like
  any scalar. The overlay timings live in the **compiled bundle node**
  (`buildWipeCoverNode`: `keyframes.steps`/`duration_ms`), not in the leaf, so
  Solar reads nothing out of the value. **Not** the runtime `<Crossfade>` (A4.0
  row 2: a plain leaf delta does not flip the runtime crossfade key). It does
  not need `target_scene`, `cut_at_ms`, or even to parse the string — only the
  change-detection. *(If #77 ever needs the object fields it parses the string
  with the `decode_scene_control_leaf` mirror; today it does not.)*
- **Prism #72/#130 (cut-only executor)** **JSON-parses the leaf string**
  (the mirror of `decode_scene_control_leaf`) into the object, reads
  **`cut_at_ms`** + **`target_scene`**, validates via a faithful TS
  re-expression of `validate_scene_control` driven by **the same
  `fixtures/*.json`**, then after `cut_at_ms` fires the **hard-cut**
  (`SetCurrentProgramScene{target_scene}` / `SetSceneItemEnabled`) on the
  loopback obs-ws — **never** an OBS-native transition (C-MECH). It pins
  `SCENE_ALLOWLIST = {scene-screen-1, scene-screen-2}` and passes it in.
  *(On `main` today Prism's `scene-control/{contract,executor,asset-allowlist}.ts`
  still carry the OLD stinger guard + obs-ws transition executor; the rewrite to
  the overlay guard + cut-only executor is #72, separate from this re-freeze.)*

The fixtures are the contract; each consumer guard is a faithful re-expression,
not a fork.

> Promotion path (deferred, same as `canonical.py`): if the copies drift
> painfully, promote `fixtures/` + the validator to a shared package. Until
> then, shared fixtures + this contract test are the cheapest thing that holds
> the contract.

## Run

```bash
pytest scripts/contracts/scene_control/test_scene_control_contract.py -v
```

The test binds Blue's **real** `leaf_mapper` when Blue is checked out as a
sibling repo (`<Structure>/Blue`); otherwise it uses a declared in-test mirror
of the identical 3-segment rule and prints a blind-spot note to stderr.
