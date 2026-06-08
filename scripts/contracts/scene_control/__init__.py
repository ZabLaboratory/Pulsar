r"""Canonical `scene_control` cross-service contract (M10, ADR 003 Amendment 4 §A4.2).

This package is the **single source of truth** for the `scene_control`
contract that travels Blue (producer, #71) → an Orion leaf
(`__inputs.blue.<slug>.scene_control`) → **two** consumers:

  - **Solar** (overlay animation, #73) reads the ``overlay`` sub-object as
    an M9 reactive render input and animates an opaque cover in-DOM.
  - **Prism** (cut-only executor, #72) reads ``cut_at_ms`` + ``target_scene``
    and fires the **hard-cut** (`SetCurrentProgramScene` / `SetSceneItemEnabled`)
    on the loopback obs-ws at that offset — never an OBS-native transition.

It is owned by **Conduit** (integration / contracts tier) and formalises —
it does **not** re-open — the schema frozen by **ADR 003 Amendment 4 §A4.2**
(the PIVOT-FINALISED leaf shape; §A4.2 is the authority).

THE PIVOT (why this shape, not the old one).  The transition is no longer an
OBS-native media transition.  Our engine (Solar/CEF) animates a full-screen
**opaque overlay** layered *over* two `monitor_capture` captures, and the
screen-1→screen-2 change of the content underneath is an **instantaneous
hard-cut** hidden under the overlay's opaque plateau.  So the leaf
co-specifies the **overlay animation** (Solar reads) AND the **`cut_at_ms`**
(Prism executes).  Gone: the OBS-native transition, the media `asset_id`, any
`path`, and any OBS action verb (`action` / `switch_program_scene`).  Those
constructs (and the prior stinger contract) belong to the superseded
Amendment 1/2 OBS-native approach, now **dormant behind a flag** (Q4) and OFF
the live contract.

Why the contract lives **here** (Pulsar/scripts/contracts):
- Pulsar owns the M10 ADR, the `m10_setup` harness (#74) and the
  end-to-end probe (#75) — the *first* real consumer that round-trips this
  shape.  The contract test ships next to the code that exercises it.
- Zab has **no central schema package** (the platform's cross-service
  convention is "vendored model + shared fixtures + a drift-catching
  contract test" — see `blue.models.canonical`).  This package follows that
  exact convention: the validator + the JSON fixtures in `fixtures/` are the
  shared artefact Blue #71 and Prism #72 reference; neither side hand-rolls a
  divergent copy.
- The leaf path rule it encodes is **imposed by Blue's real `leaf_mapper`**
  (`Blue/src/blue/services/leaf_mapper.py:31,46,168`): 3 segments
  `__inputs.blue.<slug>.scene_control`, no `.` inside a segment, ``\Z``
  anchored.  F1 (the 2-segment bug, ADR §A2.2) is asserted-against here.

The **security/coherence invariants** are encoded as hard rejects in
`validate_scene_control`.  They are NOT reopened by this module — a security
invariant is Bastion's (#80) to set; Conduit only formalises and proves them.
The cut-window invariant (`reveal_ms <= cut_at_ms <= reveal_ms + hold_ms`,
§A4.2 / §A4.7) is the visual-safety core: a cut outside the opaque plateau is
*seen* on air, so a payload violating it is rejected here, not at the antenna.

No third-party imports, no network, no OBS, no Pydantic — pure stdlib so the
contract test runs on a bare ubuntu runner without the Windows OBS build (it
is *logic*, not a probe).
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Allowlists — the closed sets the contract is parameterised over.
#
# The MECHANISM (closed-set validation) is the contract; the exact scene
# names + overlay kinds are pinned by downstream issues (#74 harness scenes,
# #73 Solar overlay element). The contract ships demo defaults so the
# round-trip is self-contained, but `validate_scene_control` takes the
# allowlists as arguments so each consumer injects its OWN pinned sets — the
# names are an *interface*, not a hard-coded coupling. (Prism #72 pins
# SCENE_ALLOWLIST = {scene-screen-1, scene-screen-2}, §A4.5 R6.)
# ---------------------------------------------------------------------------

#: The only overlay kinds the contract admits. `wipe-cover` is the v1
#: authored Solar overlay (§A4.2). The closed set is what stops an arbitrary
#: overlay-element key from addressing a non-existent / unauthored element.
ALLOWED_OVERLAY_KINDS: frozenset[str] = frozenset({"wipe-cover"})

#: Demo defaults for the first jalon. The two M10 content scenes (#74).
#: Downstream consumers MUST pass their own pinned sets; this exists so the
#: contract test is self-contained.
DEFAULT_SCENE_ALLOWLIST: frozenset[str] = frozenset(
    {"scene-screen-1", "scene-screen-2"}
)

#: Bounds for the integer timing fields. The overlay timeline segments and
#: the cut offset are millisecond counts; reject non-positive and absurd
#: values up front (an overlay that never opens, or a multi-minute hold,
#: is a contract bug, not a runtime concern). `reveal_ms`/`hold_ms`/
#: `retract_ms` and `cut_at_ms` share the same sane ceiling.
TIMING_MS_MIN = 1
TIMING_MS_MAX = 20000
#: `cut_at_ms` may legitimately be 0 only if `reveal_ms` is also small; the
#: window invariant (below) is the real gate. The raw field bound is
#: [0, TIMING_MS_MAX] because the offset is measured from leaf-apply and the
#: window check (`reveal_ms <= cut_at_ms`) enforces the meaningful floor.
CUT_AT_MS_MIN = 0
CUT_AT_MS_MAX = TIMING_MS_MAX

# ---------------------------------------------------------------------------
# Canonical leaf path (C-PATHREAL, ADR §A2.2 / F1) — UNCHANGED by the pivot.
# §A4.2 keeps the leaf at `__inputs.blue.<slug>.scene_control` (3 segments).
# ---------------------------------------------------------------------------

#: The leaf is ALWAYS 3 segments: `__inputs.blue.<slug>.scene_control`.
#: `<slug>` is a single safe segment (Blue's `_SEGMENT_RE`,
#: `leaf_mapper.py:46` — lowercase alnum + `_`/`-`, no `.`). The trailing
#: port is fixed to `scene_control`. The 2-segment form
#: `__inputs.blue.scene_control` is the F1 bug and is rejected.
LEAF_PORT = "scene_control"
# `\Z` (end of string), NOT `$` — in Python `$` also matches *before* a
# trailing `\n`, so `$` would accept `...scene_control\n` / a slug `x\n`.
# A consumer-facing path matcher (#72) must reject any trailing byte,
# newline included; `\Z` anchors at the true end of string (C-PATHREAL).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\Z")
_LEAF_PATH_RE = re.compile(
    r"^__inputs\.blue\.(?P<slug>[a-z0-9][a-z0-9_-]*)\.scene_control\Z"
)


class SceneControlContractError(ValueError):
    """A `scene_control` payload or leaf path violated the contract.

    Raised (never returned) so a consumer that forgets to guard a call
    site fails loudly. The message names the violated invariant for an
    actionable audit trail.
    """


def build_leaf_path(slug: str) -> str:
    """Compose the canonical 3-segment leaf path for a blueprint ``slug``.

    Mirrors what Blue's `leaf_mapper.map_outputs` produces for the
    ``scene_control`` output port (`leaf_mapper.py:168`):
    ``__inputs.blue.<slug>.scene_control``. Raises if ``slug`` is not a
    single safe segment — the same rule Blue enforces server-side, so the
    contract and the producer cannot disagree on what a legal path is.
    """
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise SceneControlContractError(
            f"invalid blueprint slug {slug!r}: must match ^[a-z0-9][a-z0-9_-]*\\Z "
            "(no '.', no empty, no trailing newline) — C-PATHREAL"
        )
    return f"__inputs.blue.{slug}.{LEAF_PORT}"


def assert_canonical_leaf_path(path: str) -> str:
    """Assert ``path`` is the real 3-segment ``scene_control`` leaf (C-PATHREAL).

    Rejects the F1 2-segment form ``__inputs.blue.scene_control`` and any
    path whose final segment is not ``scene_control`` or whose ``<slug>``
    is unsafe. Returns the extracted ``<slug>`` on success.
    """
    if not isinstance(path, str):
        raise SceneControlContractError(
            f"leaf path {path!r} is not a string — C-PATHREAL"
        )
    match = _LEAF_PATH_RE.match(path)
    if not match:
        raise SceneControlContractError(
            f"leaf path {path!r} is not the canonical 3-segment "
            "'__inputs.blue.<slug>.scene_control' (rejects the F1 2-segment "
            "'__inputs.blue.scene_control') — C-PATHREAL"
        )
    return match.group("slug")


def _require_int_in(value: Any, *, field: str, lo: int, hi: int) -> int:
    """Return ``value`` if it is a real int in [lo, hi], else raise.

    ``bool`` is rejected even though it subclasses ``int`` — a timing
    field is a number, not a flag, and accepting ``True`` as ``1`` would
    be a silent coercion the contract forbids. Floats are rejected too: a
    millisecond offset is an integer count, not a fractional value.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise SceneControlContractError(
            f"{field} must be an integer, got {value!r}"
        )
    if not (lo <= value <= hi):
        raise SceneControlContractError(
            f"{field}={value} out of bounds [{lo}, {hi}]"
        )
    return value


def validate_scene_control(
    payload: Any,
    *,
    scene_allowlist: frozenset[str] = DEFAULT_SCENE_ALLOWLIST,
    overlay_kind_allowlist: frozenset[str] = ALLOWED_OVERLAY_KINDS,
) -> dict[str, Any]:
    """Validate one ``scene_control`` leaf VALUE against the frozen contract.

    This is the canonical validator BOTH the Solar overlay consumer (#73)
    and the Prism cut executor (#72) — and the M10 probe (#75) — agree
    with before acting on the leaf. Solar reads ``overlay``; Prism reads
    ``cut_at_ms`` + ``target_scene``; both reject the SAME payloads via this
    one shape (no divergent validator). A payload that fails ANY check
    raises ``SceneControlContractError``; the cut consumer MUST then issue
    **zero** obs-ws calls and the overlay consumer MUST render nothing.

    Encodes ADR §A4.2 invariants verbatim:
      1. (no-OBS-native) — NO ``path`` ANYWHERE; NO ``asset_id`` (no OBS
                    media); NO ``action`` / OBS verb. Reject-unknown-fields
                    at BOTH levels catches any of them structurally.
      2. (scene)  — ``target_scene`` ∈ ``scene_allowlist`` (closed set; the
                    only thing the hard-cut may switch to).
      3. (overlay)— ``overlay.kind`` ∈ ``overlay_kind_allowlist`` (closed
                    set of authored Solar overlay elements).
      4. (bounds) — ``overlay.reveal_ms`` / ``hold_ms`` / ``retract_ms`` and
                    top-level ``cut_at_ms`` are strict ints (no bool/float),
                    in range.
      5. (window) — ``reveal_ms <= cut_at_ms <= reveal_ms + hold_ms``: the
                    hard-cut MUST land inside the opaque plateau or it is
                    visible on air. THIS is the visual-safety core (§A4.2).
      6. (strict) — no unknown top-level or overlay keys (a stray ``path``,
                    ``asset_id``, ``action``, or ``transition`` is thus
                    caught by BOTH rule 6 and rule 1).

    Returns the validated payload (echoed, unchanged) on success.
    """
    if not isinstance(payload, dict):
        raise SceneControlContractError(
            f"scene_control value must be an object, got {type(payload).__name__}"
        )

    # Rule 6 (top level): strict key set. A leaf-level `path` / `asset_id` /
    # `action` / `transition` is caught here — the superseded OBS-native
    # constructs are rejected as unknown keys (defence with rule 1).
    allowed_top = {"target_scene", "overlay", "cut_at_ms"}
    unknown_top = set(payload) - allowed_top
    if unknown_top:
        raise SceneControlContractError(
            f"unknown top-level key(s) {sorted(unknown_top)} — strict schema; "
            "a stray 'path'/'asset_id'/'action'/'transition' here is rejected "
            "(no OBS-native construct on the live contract — §A4.2)"
        )

    # Rule 2: target_scene allowlist (the only scene the cut may switch to).
    target_scene = payload.get("target_scene")
    if not isinstance(target_scene, str) or target_scene not in scene_allowlist:
        raise SceneControlContractError(
            f"target_scene {target_scene!r} not in scene allowlist "
            f"{sorted(scene_allowlist)} — arbitrary scene-name injection into "
            "SetCurrentProgramScene is rejected"
        )

    overlay = payload.get("overlay")
    if not isinstance(overlay, dict):
        raise SceneControlContractError(
            f"overlay must be an object, got {type(overlay).__name__}"
        )

    # Rule 6 (overlay level): strict key set. A stray `asset_id` / `path`
    # here (the old media construct) is caught as an unknown overlay key.
    allowed_overlay = {"kind", "reveal_ms", "hold_ms", "retract_ms"}
    unknown_overlay = set(overlay) - allowed_overlay
    if unknown_overlay:
        raise SceneControlContractError(
            f"unknown overlay key(s) {sorted(unknown_overlay)} — strict schema; "
            "no 'asset_id'/'path' (no OBS media) — §A4.2"
        )

    # Rule 3: overlay.kind allowlist (authored Solar overlay element).
    kind = overlay.get("kind")
    if not isinstance(kind, str) or kind not in overlay_kind_allowlist:
        raise SceneControlContractError(
            f"overlay.kind {kind!r} not allowlisted "
            f"(only {sorted(overlay_kind_allowlist)})"
        )

    # Rule 4: bounded integer overlay timeline.
    reveal_ms = _require_int_in(
        overlay.get("reveal_ms"),
        field="overlay.reveal_ms",
        lo=TIMING_MS_MIN,
        hi=TIMING_MS_MAX,
    )
    hold_ms = _require_int_in(
        overlay.get("hold_ms"),
        field="overlay.hold_ms",
        lo=TIMING_MS_MIN,
        hi=TIMING_MS_MAX,
    )
    retract_ms = _require_int_in(
        overlay.get("retract_ms"),
        field="overlay.retract_ms",
        lo=TIMING_MS_MIN,
        hi=TIMING_MS_MAX,
    )

    # Rule 4: bounded integer cut offset (may be 0; the window check is the
    # meaningful floor).
    cut_at_ms = _require_int_in(
        payload.get("cut_at_ms"),
        field="cut_at_ms",
        lo=CUT_AT_MS_MIN,
        hi=CUT_AT_MS_MAX,
    )

    # Rule 5: the cut-window invariant — the VISUAL-SAFETY core (§A4.2).
    # The hard-cut must land inside the opaque plateau [reveal, reveal+hold]
    # or the audience sees the content snap. SPIKE-CUT (#75) measures the
    # real skew margin; the contract guarantees at least that the VALUE is
    # internally coherent (the cut is scheduled inside the declared plateau).
    opaque_start = reveal_ms
    opaque_end = reveal_ms + hold_ms
    if not (opaque_start <= cut_at_ms <= opaque_end):
        raise SceneControlContractError(
            f"cut_at_ms={cut_at_ms} outside the opaque window "
            f"[reveal_ms={reveal_ms}, reveal_ms+hold_ms={opaque_end}] — the "
            "hard-cut would be visible (violates reveal_ms <= cut_at_ms <= "
            "reveal_ms + hold_ms, §A4.2 / §A4.7)"
        )

    return {
        "target_scene": target_scene,
        "overlay": {
            "kind": kind,
            "reveal_ms": reveal_ms,
            "hold_ms": hold_ms,
            "retract_ms": retract_ms,
        },
        "cut_at_ms": cut_at_ms,
    }


__all__ = [
    "ALLOWED_OVERLAY_KINDS",
    "CUT_AT_MS_MAX",
    "CUT_AT_MS_MIN",
    "DEFAULT_SCENE_ALLOWLIST",
    "LEAF_PORT",
    "TIMING_MS_MAX",
    "TIMING_MS_MIN",
    "SceneControlContractError",
    "assert_canonical_leaf_path",
    "build_leaf_path",
    "validate_scene_control",
]
