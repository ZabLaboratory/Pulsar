"""Contract test — `scene_control` round-trip Blue → leaf → Prism/probe.

This is the **Conduit deliverable** for ZabLaboratory/Pulsar#59. It proves
the cross-service `scene_control` contract (ADR 003 Amendment 2 §A2.1)
holds end-to-end at the *shape* level, independent of OBS, the network,
or a live broadcast — pure logic, runnable on a bare ubuntu runner.

It exercises BOTH sides of the seam against the SAME shared fixtures
(`fixtures/valid.json`, `fixtures/malicious.json`):

  PRODUCER (Blue, #58)            LEAF                       CONSUMER (Prism #63 / probe #61)
  ----------------------         -----------------------     -------------------------------
  leaf_mapper.map_outputs   →    __inputs.blue.<slug>.   →   validate_scene_control(value)
  {"scene_control": value}       scene_control               + assert_canonical_leaf_path

Producer half — wiring to Blue's REAL code, no divergent copy:
  If Blue is checked out as a sibling repo, this test imports its REAL
  `leaf_mapper` and proves Blue composes EXACTLY the canonical path the
  consumer matches. If Blue is absent (CI runs Pulsar in isolation), a
  faithful in-test mirror of the two load-bearing `leaf_mapper` rules
  (`_SEGMENT_RE`, the 3-segment `__inputs.blue.<slug>.<port>` shape) is
  used and a system note is emitted — the path RULE is identical on both
  sides by construction (both derive from ADR §A2.2). The mirror is a
  declared blind spot when Blue is not inspectable, never a silent pass.

Consumer half — the canonical `validate_scene_control` from this package,
which is the SAME validator Prism #63 and probe #61 import. The shared
fixtures are the drift-catcher (the platform's `canonical.py` convention).

Run:  pytest scripts/contracts/scene_control/test_scene_control_contract.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

# Make the contract package importable when run directly (CI invokes
# pytest from the repo root; this keeps `python -m pytest <file>` working
# from anywhere too).
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from scene_control import (  # noqa: E402
    LEAF_PORT,
    SceneControlContractError,
    assert_canonical_leaf_path,
    build_leaf_path,
    validate_scene_control,
)

_FIXTURES = _HERE / "fixtures"


# ---------------------------------------------------------------------------
# PRODUCER HALF — Blue's real leaf_mapper, or a faithful mirror.
# ---------------------------------------------------------------------------

# `__inputs.blue.<slug>.<port>` with `_SEGMENT_RE = ^[a-z0-9][a-z0-9_-]*$`
# — these two facts ARE the contract's producer side (leaf_mapper.py:38,160).
_MIRROR_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MIRROR_PREFIX = "__inputs.blue"


def _import_real_leaf_mapper() -> Any | None:
    """Return Blue's real `map_outputs` if a sibling Blue repo is present.

    Layout assumption mirrors the Zab structure: Pulsar and Blue are
    sibling repos under the same structure dir
    (``<Structure>/Pulsar`` and ``<Structure>/Blue``).
    """
    blue_src = _HERE.parents[3] / "Blue" / "src"
    if not (blue_src / "blue" / "services" / "leaf_mapper.py").is_file():
        return None
    if str(blue_src) not in sys.path:
        sys.path.insert(0, str(blue_src))
    try:
        from blue.services.leaf_mapper import map_outputs  # type: ignore
    except Exception:  # pragma: no cover - import-environment dependent
        return None
    return map_outputs


_REAL_MAP_OUTPUTS = _import_real_leaf_mapper()
PRODUCER_IS_REAL_BLUE = _REAL_MAP_OUTPUTS is not None


def producer_compose_leaf(slug: str, value: Any) -> tuple[str, Any]:
    """Emulate Blue emitting ``outputs={"scene_control": value}`` for ``slug``.

    Returns ``(leaf_path, leaf_value)`` exactly as the Blue→Orion push
    would carry them. Uses Blue's REAL ``map_outputs`` when available;
    otherwise a mirror enforcing the identical 3-segment rule.
    """
    if _REAL_MAP_OUTPUTS is not None:
        mapped = _REAL_MAP_OUTPUTS(slug=slug, outputs={LEAF_PORT: value})
        assert len(mapped) == 1
        return mapped[0].path, mapped[0].value

    # Faithful mirror (Blue not checked out): same rule, declared.
    if not _MIRROR_SEGMENT_RE.match(slug):
        raise ValueError(f"slug {slug!r} fails Blue _SEGMENT_RE")
    return f"{_MIRROR_PREFIX}.{slug}.{LEAF_PORT}", value


def test_producer_provenance_is_declared() -> None:
    """Make the producer-half provenance explicit (declared blind spot).

    Not an assertion of correctness — a guard so a green run always states
    whether the round-trip ran against Blue's REAL leaf_mapper or the
    in-test mirror. If Blue is absent, that is a *declared* limitation, per
    the Conduit "two real sides" rule.
    """
    if not PRODUCER_IS_REAL_BLUE:
        print(
            "\n[contract] NOTE: Blue sibling repo not importable — producer half "
            "uses the in-test leaf_mapper MIRROR (same 3-segment rule, ADR §A2.2). "
            "Declared blind spot: re-run with Blue checked out to bind the REAL "
            "leaf_mapper.",
            file=sys.stderr,
        )
    assert True


# ---------------------------------------------------------------------------
# Fixture loading.
# ---------------------------------------------------------------------------


def _load(name: str) -> list[dict[str, Any]]:
    data = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    return data["cases"]


_VALID = _load("valid.json")
_MALICIOUS = _load("malicious.json")


# ---------------------------------------------------------------------------
# POSITIVE — a valid payload survives the FULL round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _VALID, ids=[c["name"] for c in _VALID])
def test_valid_payload_round_trips(case: dict[str, Any]) -> None:
    """Producer → leaf → consumer: a valid payload passes end-to-end.

    1. Blue composes the leaf path + value from the output port.
    2. The path is the canonical 3-segment form (consumer matcher agrees).
    3. The consumer validator accepts the value and echoes it unchanged.
    4. The echoed value carries NO `path` — only an `asset_id` key.
    """
    slug = case["slug"]
    value = case["value"]

    # (1) producer
    leaf_path, leaf_value = producer_compose_leaf(slug, value)

    # (2) the path both sides agree on (C-PATHREAL), 3 segments
    assert leaf_path == build_leaf_path(slug)
    recovered_slug = assert_canonical_leaf_path(leaf_path)
    assert recovered_slug == slug
    # 4 segments (__inputs, blue, <slug>, scene_control) ⇒ 3 dot separators.
    assert leaf_path.count(".") == 3
    assert len(leaf_path.split(".")) == 4

    # (3) consumer accepts
    validated = validate_scene_control(leaf_value)

    # (4) no path leaked through; asset_id stays an opaque key (C-PATH)
    assert "path" not in validated
    assert "path" not in validated["transition"]
    assert validated["transition"]["asset_id"] == value["transition"]["asset_id"]
    assert validated["action"] == "switch_program_scene"


def test_round_trip_value_is_byte_stable() -> None:
    """A valid value re-serialised post-validation is identical (no coercion)."""
    case = _VALID[0]
    _, leaf_value = producer_compose_leaf(case["slug"], case["value"])
    validated = validate_scene_control(leaf_value)
    assert json.loads(json.dumps(validated)) == case["value"]


# ---------------------------------------------------------------------------
# NEGATIVE — every malicious / malformed payload is REJECTED (0 obs-ws calls).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _MALICIOUS, ids=[c["name"] for c in _MALICIOUS])
def test_malicious_payload_is_rejected(case: dict[str, Any]) -> None:
    """C-PATH / C-INJ / C-PATHREAL: the contract rejects the case.

    A path-targeting case (`bad_path` set) must be rejected by the leaf
    PATH matcher; every other case must be rejected by the VALUE
    validator. A reject == the consumer issues zero obs-ws calls.
    """
    if "bad_path" in case:
        # C-PATHREAL — the leaf path itself is illegal; the consumer's
        # path matcher must refuse to treat it as a scene_control leaf.
        with pytest.raises(SceneControlContractError):
            assert_canonical_leaf_path(case["bad_path"])
    else:
        with pytest.raises(SceneControlContractError):
            validate_scene_control(case["value"])


def test_every_invariant_has_negative_coverage() -> None:
    """Guard: the corpus proves all three Bastion conditions, not just one.

    A regression that dropped, say, every C-PATH case would otherwise pass
    silently. This asserts each invariant label is exercised.
    """
    seen = {c["invariant"] for c in _MALICIOUS}
    for required in ("C-PATH", "C-INJ", "C-PATHREAL"):
        assert required in seen, f"no negative case for {required}"


def test_no_path_field_ever_validates() -> None:
    """Belt-and-braces: a `path` anywhere is rejected regardless of allowlist.

    Independent of the fixtures, prove the validator structurally refuses a
    `path` at both levels even when everything else is allowlisted — the
    core of the lifted veto (R7 / C-PATH).
    """
    base = {
        "action": "switch_program_scene",
        "target_scene": "scene-screen-1",
        "transition": {
            "kind": "stinger",
            "asset_id": "stinger-demo",
            "point_ms": 0,
            "duration_ms": 600,
        },
    }
    top_with_path = {**base, "path": "C:\\x"}
    tr_with_path = {
        **base,
        "transition": {**base["transition"], "path": "C:\\x"},
    }
    with pytest.raises(SceneControlContractError):
        validate_scene_control(top_with_path)
    with pytest.raises(SceneControlContractError):
        validate_scene_control(tr_with_path)


def test_consumer_injects_its_own_allowlists() -> None:
    """The allowlist is an INTERFACE, not a hard-coded coupling.

    Proves #63 can pass its own pinned scene/asset sets — the mechanism is
    the contract, the names are downstream (#60 scenes, #64 asset).
    """
    value = {
        "action": "switch_program_scene",
        "target_scene": "prod-scene-a",
        "transition": {
            "kind": "stinger",
            "asset_id": "prod-stinger",
            "point_ms": 100,
            "duration_ms": 800,
        },
    }
    # Rejected under the default demo allowlist...
    with pytest.raises(SceneControlContractError):
        validate_scene_control(value)
    # ...accepted when the consumer supplies the matching pinned sets.
    out = validate_scene_control(
        value,
        scene_allowlist=frozenset({"prod-scene-a", "prod-scene-b"}),
        asset_allowlist=frozenset({"prod-stinger"}),
    )
    assert out["target_scene"] == "prod-scene-a"
