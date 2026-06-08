"""Canonical `scene_control` cross-service contract (M10, ADR 003 Amendment 2).

This package is the **single source of truth** for the `scene_control`
contract that travels Blue (producer, #58) → an Orion leaf
(`__inputs.blue.<slug>.scene_control`) → Prism (consumer, #63) / the
M10 harness (first-jalon consumer, #61). It is owned by **Conduit**
(integration / contracts tier) and formalises — it does **not** re-open —
the schema frozen by **ADR 003 Amendment 2 §A2.1** (already cleared by
Bastion + Vigil; veto R7 lifted at design level).

Why the contract lives **here** (Pulsar/scripts/contracts):
- Pulsar owns the M10 ADR, the `m10_setup` harness (#60) and the
  end-to-end probe (#61) — the *first* real consumer that round-trips
  this shape (ADR §3.5: "the probe harness fires /trigger and reads
  outputs.scene_control directly"). The contract test ships next to
  the code that exercises it.
- Zab has **no central schema package** (the platform's existing
  cross-service convention is "vendored model + shared fixtures +
  a drift-catching contract test" — see `blue.models.canonical` /
  `Blue/src/blue/models/canonical.py` module docstring). This package
  follows that exact convention: the validator + the JSON fixtures in
  `fixtures/` are the shared artefact Blue #58 and Prism #63 reference;
  neither side hand-rolls a divergent copy.
- The leaf path rule it encodes is **imposed by Blue's real
  `leaf_mapper`** (`Blue/src/blue/services/leaf_mapper.py:38,133,160`):
  3 segments `__inputs.blue.<slug>.scene_control`, no `.` inside a
  segment. F1 (the 2-segment bug, ADR §A2.2) is asserted-against here.

The **security invariants** (Bastion conditions C-PATH / C-INJ /
C-PATHREAL) are encoded as hard rejects in `validate_scene_control`.
They are NOT reopened by this module — a security invariant is Bastion's
to set; Conduit only formalises and proves them.

No third-party imports, no network, no OBS, no Pydantic — pure stdlib so
the contract test runs on a bare ubuntu runner without the Windows OBS
build (it is *logic*, not a probe).
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Allowlists — the closed sets the contract is parameterised over.
#
# The MECHANISM (closed-set validation) is the contract; the exact scene
# names + asset ids are pinned by downstream issues (#60 harness scenes,
# #64 demo asset). The contract ships demo defaults so the round-trip is
# self-contained, but `validate_scene_control` takes the allowlists as
# arguments so the consumer (#63) injects its OWN pinned sets — the names
# are an *interface*, not a hard-coded coupling.
# ---------------------------------------------------------------------------

#: The only verb the contract admits. Anything else → reject (C-INJ): no
#: dynamic dispatch to an arbitrary obs-websocket request.
ALLOWED_ACTIONS: frozenset[str] = frozenset({"switch_program_scene"})

#: The only transition kinds the contract admits.
ALLOWED_TRANSITION_KINDS: frozenset[str] = frozenset({"stinger", "fade"})

#: Demo defaults for the first jalon. The two M10 scenes (#60) and the
#: demo stinger asset key (#64). Downstream consumers MUST pass their own
#: pinned sets; these exist so the contract test is self-contained.
DEFAULT_SCENE_ALLOWLIST: frozenset[str] = frozenset(
    {"scene-screen-1", "scene-screen-2"}
)
DEFAULT_ASSET_ALLOWLIST: frozenset[str] = frozenset({"stinger-demo"})

#: Bounds for the integer timing fields. `duration_ms` is clamped
#: 50–20000 by `SetCurrentSceneTransitionDuration`
#: (`RequestHandler_Transitions.cpp:174`); the contract rejects
#: out-of-range up front rather than relying on the clamp. `point_ms`
#: (the stinger transition point) is bounded the same way.
DURATION_MS_MIN = 50
DURATION_MS_MAX = 20000
POINT_MS_MIN = 0
POINT_MS_MAX = 20000

# ---------------------------------------------------------------------------
# Canonical leaf path (C-PATHREAL, ADR §A2.2 / F1)
# ---------------------------------------------------------------------------

#: The leaf is ALWAYS 3 segments: `__inputs.blue.<slug>.scene_control`.
#: `<slug>` is a single safe segment (Blue's `_SEGMENT_RE`,
#: `leaf_mapper.py:38` — lowercase alnum + `_`/`-`, no `.`). The trailing
#: port is fixed to `scene_control`. The 2-segment form
#: `__inputs.blue.scene_control` is the F1 bug and is rejected.
LEAF_PORT = "scene_control"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_LEAF_PATH_RE = re.compile(
    r"^__inputs\.blue\.(?P<slug>[a-z0-9][a-z0-9_-]*)\.scene_control$"
)

#: Markers that make an `asset_id` look like a path rather than an
#: allowlist key. Even though `asset_id` must be IN the allowlist (a far
#: stronger check), the contract rejects path-shaped ids outright so a
#: mis-seeded allowlist can never smuggle a traversal/UNC/abs path into a
#: media-open call (C-PATH, defence in depth).
_PATH_SHAPED = ("/", "\\", "..")


class SceneControlContractError(ValueError):
    """A `scene_control` payload or leaf path violated the contract.

    Raised (never returned) so a consumer that forgets to guard a call
    site fails loudly. The message names the violated invariant
    (C-PATH / C-INJ / C-PATHREAL) for an actionable audit trail.
    """


def build_leaf_path(slug: str) -> str:
    """Compose the canonical 3-segment leaf path for a blueprint ``slug``.

    Mirrors what Blue's `leaf_mapper.map_outputs` produces for the
    ``scene_control`` output port (`leaf_mapper.py:160`):
    ``__inputs.blue.<slug>.scene_control``. Raises if ``slug`` is not a
    single safe segment — the same rule Blue enforces server-side, so the
    contract and the producer cannot disagree on what a legal path is.
    """
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise SceneControlContractError(
            f"invalid blueprint slug {slug!r}: must match ^[a-z0-9][a-z0-9_-]*$ "
            "(no '.', no empty) — C-PATHREAL"
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
    be a silent coercion the contract forbids.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise SceneControlContractError(
            f"transition.{field} must be an integer, got {value!r}"
        )
    if not (lo <= value <= hi):
        raise SceneControlContractError(
            f"transition.{field}={value} out of bounds [{lo}, {hi}]"
        )
    return value


def validate_scene_control(
    payload: Any,
    *,
    scene_allowlist: frozenset[str] = DEFAULT_SCENE_ALLOWLIST,
    asset_allowlist: frozenset[str] = DEFAULT_ASSET_ALLOWLIST,
) -> dict[str, Any]:
    """Validate one ``scene_control`` leaf VALUE against the frozen contract.

    This is the canonical validator BOTH the Prism consumer (#63) and the
    M10 probe (#61) run before issuing any obs-websocket call. A payload
    that fails ANY check raises ``SceneControlContractError`` and the
    consumer MUST issue **zero** obs-ws calls (C-INJ).

    Encodes ADR §A2.1 invariants verbatim:
      1. C-PATH   — NO ``path`` field anywhere; ``transition`` carries an
                    allowlisted ``asset_id`` key only, never a path. An
                    ``asset_id`` that is path-shaped (``/`` ``\\`` ``..``,
                    incl. UNC ``\\\\``) is rejected even before the
                    allowlist check.
      2. C-INJ    — ``action`` ∈ ALLOWED_ACTIONS; ``target_scene`` ∈
                    ``scene_allowlist``; ``transition.kind`` ∈
                    ALLOWED_TRANSITION_KINDS.
      3. (asset)  — ``asset_id`` ∈ ``asset_allowlist`` (closed set).
      4. (bounds) — ``point_ms`` / ``duration_ms`` integers, in range.
      5. (strict) — no unknown top-level or transition keys (a stray
                    ``path`` key is thus caught by BOTH this and rule 1).

    Returns the validated payload (echoed, unchanged) on success — never
    a path; the real media path is resolved LOCALLY by the consumer from
    ``asset_id`` against its pinned ``ASSET_ALLOWLIST`` map, outside this
    contract.
    """
    if not isinstance(payload, dict):
        raise SceneControlContractError(
            f"scene_control value must be an object, got {type(payload).__name__}"
        )

    # Rule 5 (top level): strict key set. A leaf-level `path` is caught here.
    allowed_top = {"action", "target_scene", "transition"}
    unknown_top = set(payload) - allowed_top
    if unknown_top:
        raise SceneControlContractError(
            f"unknown top-level key(s) {sorted(unknown_top)} — strict schema; "
            "a stray 'path' here is rejected (C-PATH)"
        )

    # Rule 2: action allowlist (C-INJ).
    action = payload.get("action")
    if action not in ALLOWED_ACTIONS:
        raise SceneControlContractError(
            f"action {action!r} not allowlisted (only {sorted(ALLOWED_ACTIONS)}) "
            "— C-INJ; consumer issues 0 obs-ws calls"
        )

    # Rule 2: target_scene allowlist (C-INJ).
    target_scene = payload.get("target_scene")
    if not isinstance(target_scene, str) or target_scene not in scene_allowlist:
        raise SceneControlContractError(
            f"target_scene {target_scene!r} not in scene allowlist "
            f"{sorted(scene_allowlist)} — C-INJ"
        )

    transition = payload.get("transition")
    if not isinstance(transition, dict):
        raise SceneControlContractError(
            f"transition must be an object, got {type(transition).__name__}"
        )

    # Rule 5 (transition level): strict key set. A `transition.path` is
    # caught here — the exact vetoed construct (ADR §A2.1) — as well as by
    # the absence of any path-honouring code path below.
    allowed_transition = {"kind", "asset_id", "point_ms", "duration_ms"}
    unknown_tr = set(transition) - allowed_transition
    if unknown_tr:
        raise SceneControlContractError(
            f"unknown transition key(s) {sorted(unknown_tr)} — strict schema; "
            "'path' is forbidden, only an allowlisted 'asset_id' (C-PATH)"
        )

    # Rule 2: transition.kind allowlist (C-INJ).
    kind = transition.get("kind")
    if kind not in ALLOWED_TRANSITION_KINDS:
        raise SceneControlContractError(
            f"transition.kind {kind!r} not allowlisted "
            f"(only {sorted(ALLOWED_TRANSITION_KINDS)}) — C-INJ"
        )

    # Rule 1: asset_id is an allowlist KEY, never a path (C-PATH).
    asset_id = transition.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id:
        raise SceneControlContractError(
            "transition.asset_id must be a non-empty string allowlist key — C-PATH"
        )
    if any(marker in asset_id for marker in _PATH_SHAPED):
        # Catches '/', '\', '..', and UNC '\\attacker\share\evil.webm'
        # (the '\\' contains '\'), C:\Users\...\id_rsa (contains '\'),
        # ./../traversal (contains '..' and '/').
        raise SceneControlContractError(
            f"transition.asset_id {asset_id!r} is path-shaped (contains one of "
            f"{_PATH_SHAPED}) — must be an opaque allowlist key, never a path "
            "(C-PATH)"
        )
    if asset_id not in asset_allowlist:
        raise SceneControlContractError(
            f"transition.asset_id {asset_id!r} not in asset allowlist "
            f"{sorted(asset_allowlist)} — C-PATH; real path resolved locally only"
        )

    # Rule 4: bounded integer timings.
    point_ms = _require_int_in(
        transition.get("point_ms"), field="point_ms", lo=POINT_MS_MIN, hi=POINT_MS_MAX
    )
    duration_ms = _require_int_in(
        transition.get("duration_ms"),
        field="duration_ms",
        lo=DURATION_MS_MIN,
        hi=DURATION_MS_MAX,
    )

    return {
        "action": action,
        "target_scene": target_scene,
        "transition": {
            "kind": kind,
            "asset_id": asset_id,
            "point_ms": point_ms,
            "duration_ms": duration_ms,
        },
    }


__all__ = [
    "ALLOWED_ACTIONS",
    "ALLOWED_TRANSITION_KINDS",
    "DEFAULT_ASSET_ALLOWLIST",
    "DEFAULT_SCENE_ALLOWLIST",
    "DURATION_MS_MAX",
    "DURATION_MS_MIN",
    "LEAF_PORT",
    "POINT_MS_MAX",
    "POINT_MS_MIN",
    "SceneControlContractError",
    "assert_canonical_leaf_path",
    "build_leaf_path",
    "validate_scene_control",
]
