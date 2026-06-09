r"""Contract test — `scene_control` round-trip Blue → leaf → Solar/Prism (overlay form).

This is the **Conduit deliverable** for ZabLaboratory/Pulsar#74 (re-freeze the
`scene_control` contract on the Amendment 4 §A4.2 OVERLAY form). It proves the
cross-service contract holds end-to-end at the *shape* level, independent of
OBS, Solar's CEF runtime, the network, or a live broadcast — pure logic,
runnable on a bare ubuntu runner.

It exercises ALL THREE sides of the seam against the SAME shared fixtures
(`fixtures/valid.json`, `fixtures/malicious.json`):

  PRODUCER (Blue, #71)           LEAF                       CONSUMERS
  ----------------------         -----------------------    --------------------------------
  leaf_mapper.map_outputs   →    __inputs.blue.<slug>.  →   Solar #73 reads `overlay`
  {"scene_control": value}       scene_control              Prism #72 reads `cut_at_ms`
                                                            + `target_scene`
                                 ↑ validate_scene_control is the ONE validator both
                                   consumers agree with — no divergent copy.

Producer half — wiring to Blue's REAL code, no divergent copy:
  If Blue is checked out as a sibling repo, this test imports its REAL
  `leaf_mapper` and proves Blue composes EXACTLY the canonical path the
  consumers match. If Blue is absent (CI runs Pulsar in isolation), a faithful
  in-test mirror of the two load-bearing `leaf_mapper` rules (`_SEGMENT_RE`,
  the 3-segment `__inputs.blue.<slug>.<port>` shape) is used and a system note
  is emitted — the path RULE is identical on both sides by construction (both
  derive from ADR §A2.2, unchanged by the pivot). The mirror is a DECLARED
  blind spot when Blue is not inspectable, never a silent pass.

Note on the producer's VALUE builder: Blue's `build_scene_control`
(`Blue/src/blue/services/scene_control.py`) still emits the OLD OBS-native
stinger shape on `main` — its rewrite to the overlay value is issue #71
(Forge), out of this Conduit contract's scope. This test therefore round-trips
the FIXTURE values (the frozen overlay shape) through the real leaf_mapper PATH
machinery; it does NOT import `build_scene_control` (which would couple this
contract to a not-yet-pivoted producer). When #71 lands, Blue's own contract
test asserts its builder emits these fixtures.

Consumer half — the canonical `validate_scene_control` from this package, the
SAME validator Solar #73 / Prism #72 / probe #75 agree with. The shared
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

# Make the contract package importable when run directly (CI invokes pytest
# from the repo root; this keeps `python -m pytest <file>` working from
# anywhere too).
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from scene_control import (  # noqa: E402
    ALLOWED_OVERLAY_KINDS,
    DEFAULT_SCENE_ALLOWLIST,
    LEAF_PORT,
    SceneControlContractError,
    assert_canonical_leaf_path,
    build_leaf_path,
    decode_scene_control_leaf,
    encode_scene_control_leaf,
    validate_scene_control,
)

_FIXTURES = _HERE / "fixtures"


# ---------------------------------------------------------------------------
# PRODUCER HALF — Blue's real leaf_mapper, or a faithful mirror.
# ---------------------------------------------------------------------------

# `__inputs.blue.<slug>.<port>` with `_SEGMENT_RE = ^[a-z0-9][a-z0-9_-]*\Z`
# — these two facts ARE the contract's producer side (leaf_mapper.py:46,168),
# UNCHANGED by the overlay pivot (the path is the same; only the VALUE shape
# changed).
_MIRROR_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\Z")
_MIRROR_PREFIX = "__inputs.blue"


def _import_real_leaf_mapper() -> Any | None:
    """Return Blue's real `map_outputs` if a sibling Blue repo is present.

    Layout assumption mirrors the Zab structure: Pulsar and Blue are sibling
    repos under the same structure dir (``<Structure>/Pulsar`` and
    ``<Structure>/Blue``).
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

    Returns ``(leaf_path, leaf_value)`` exactly as the Blue→Orion push would
    carry them. Uses Blue's REAL ``map_outputs`` when available; otherwise a
    mirror enforcing the identical 3-segment rule.
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
    whether the round-trip ran against Blue's REAL leaf_mapper or the in-test
    mirror. If Blue is absent, that is a *declared* limitation, per the Conduit
    "two real sides" rule.
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
# POSITIVE — a valid payload survives the FULL round-trip, and BOTH consumers
# can read what they need.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _VALID, ids=[c["name"] for c in _VALID])
def test_valid_payload_round_trips(case: dict[str, Any]) -> None:
    """Producer → leaf → both consumers: a valid overlay payload passes.

    1. Blue composes the leaf path + value from the output port.
    2. The path is the canonical 3-segment form (consumer matcher agrees).
    3. The consumer validator accepts the value and echoes it unchanged.
    4. The echoed value carries NO OBS-native construct (path/asset_id/action).
    5. Solar (#73) can read `overlay`; Prism (#72) can read `cut_at_ms` +
       `target_scene` — both from the SAME validated value.
    """
    slug = case["slug"]
    value = case["value"]

    # (1) producer
    leaf_path, leaf_value = producer_compose_leaf(slug, value)

    # (2) the path both sides agree on (C-PATHREAL), 3 segments — unchanged.
    assert leaf_path == build_leaf_path(slug)
    recovered_slug = assert_canonical_leaf_path(leaf_path)
    assert recovered_slug == slug
    # 4 segments (__inputs, blue, <slug>, scene_control) ⇒ 3 dot separators.
    assert leaf_path.count(".") == 3
    assert len(leaf_path.split(".")) == 4

    # (3) consumer accepts
    validated = validate_scene_control(leaf_value)

    # (4) no OBS-native construct leaked through
    assert "path" not in validated
    assert "asset_id" not in validated
    assert "action" not in validated
    assert "transition" not in validated
    assert "path" not in validated["overlay"]
    assert "asset_id" not in validated["overlay"]

    # (5) Solar reads `overlay`; Prism reads `cut_at_ms` + `target_scene`.
    overlay = validated["overlay"]
    assert overlay["kind"] in ALLOWED_OVERLAY_KINDS
    assert isinstance(overlay["reveal_ms"], int)
    assert isinstance(overlay["hold_ms"], int)
    assert isinstance(overlay["retract_ms"], int)
    assert validated["target_scene"] in DEFAULT_SCENE_ALLOWLIST
    assert isinstance(validated["cut_at_ms"], int)


@pytest.mark.parametrize("case", _VALID, ids=[c["name"] for c in _VALID])
def test_valid_payload_satisfies_cut_window(case: dict[str, Any]) -> None:
    """Every VALID case obeys reveal_ms <= cut_at_ms <= reveal_ms+hold_ms.

    The window invariant (§A4.2 / §A4.7) is the visual-safety core: a valid
    fixture must, by construction, schedule the cut inside the opaque plateau.
    This asserts the corpus itself is coherent (a fixture that violated the
    window would be a contract-authoring bug).
    """
    overlay = case["value"]["overlay"]
    cut_at = case["value"]["cut_at_ms"]
    assert overlay["reveal_ms"] <= cut_at <= overlay["reveal_ms"] + overlay["hold_ms"]


def test_round_trip_value_is_byte_stable() -> None:
    """A valid value re-serialised post-validation is identical (no coercion)."""
    case = _VALID[0]
    _, leaf_value = producer_compose_leaf(case["slug"], case["value"])
    validated = validate_scene_control(leaf_value)
    assert json.loads(json.dumps(validated)) == case["value"]


# ---------------------------------------------------------------------------
# LSDP LEAF-TRANSPORT — the load-bearing M10 proof: the contract value is an
# OBJECT, but the LSDP wire forbids object leaf values, so the value travels
# as a JSON STRING in one scalar leaf. These tests prove the full transport
# round-trip producer(object→string) → LSDP-legal scalar leaf → consumer
# (string→object→validate), which the object-only tests above never proved.
# ---------------------------------------------------------------------------


def _leaf_value_is_lsdp_legal(v: Any) -> bool:
    """Mirror of `@lumencast/protocol` codec.ts::assertLeafValue.

    Admits exactly `str | int | float | bool | None | list[...]` (recursive)
    — objects (dict) are FORBIDDEN. This is the rule the real Solar runtime
    enforces at decode (transport/ws.js → decodeServerFrame), encoded here so
    the contract test fails the moment a leaf value would be rejected on the
    wire. (Python `bool` is a subtype of `int`; both are scalar-legal here,
    matching JS `typeof === "boolean"/"number"`.)
    """
    if v is None or isinstance(v, (str, int, float, bool)):
        return True
    if isinstance(v, list):
        return all(_leaf_value_is_lsdp_legal(item) for item in v)
    return False  # dict / object — INVALID_VALUE on the LSDP wire


@pytest.mark.parametrize("case", _VALID, ids=[c["name"] for c in _VALID])
def test_transport_round_trip_object_to_string_to_object(case: dict[str, Any]) -> None:
    """Producer encodes object→STRING; consumer decodes STRING→object→validate.

    The decisive cross-service proof for M10:

      1. PRODUCER (Blue) calls `encode_scene_control_leaf(value)` → the leaf
         VALUE is a JSON **string**.
      2. That string is LSDP-legal (a plain object would be rejected by the
         codec with INVALID_VALUE) — asserted against the codec mirror.
      3. The string is what crosses the leaf
         `__inputs.blue.<slug>.scene_control` (the SAME canonical path; the
         path rule is unchanged — only the value envelope changed).
      4. CONSUMER (Prism / probe) calls `decode_scene_control_leaf(string)`
         → the original object, re-validated against the frozen schema.
      5. Both consumers can read what they need from the decoded object
         (Solar: `overlay`; Prism: `cut_at_ms` + `target_scene`).
    """
    slug = case["slug"]
    value = case["value"]

    # (1) producer serialises the validated object to the leaf string.
    leaf_string = encode_scene_control_leaf(value)
    assert isinstance(leaf_string, str)

    # (2) the leaf value is LSDP-legal as a STRING; the bare object is NOT.
    assert _leaf_value_is_lsdp_legal(leaf_string), (
        "encoded leaf value must be an LSDP-legal scalar string"
    )
    assert not _leaf_value_is_lsdp_legal(value), (
        "the raw object must be LSDP-ILLEGAL — that is the whole reason the "
        "string envelope exists (a dict leaf is rejected by assertLeafValue)"
    )

    # (3) the leaf path is unchanged: the canonical 3-segment form carries
    # the string value exactly as the producer's leaf_mapper composes it.
    leaf_path, mapped_value = producer_compose_leaf(slug, leaf_string)
    assert leaf_path == build_leaf_path(slug)
    assert assert_canonical_leaf_path(leaf_path) == slug
    assert mapped_value == leaf_string  # leaf_mapper carries the string as-is

    # (4) consumer decodes the string back to the object + re-validates.
    decoded = decode_scene_control_leaf(mapped_value)
    assert decoded == value

    # (5) both consumers can read their slice from the decoded object.
    assert decoded["overlay"]["kind"] in ALLOWED_OVERLAY_KINDS  # Solar
    assert decoded["target_scene"] in DEFAULT_SCENE_ALLOWLIST  # Prism
    assert isinstance(decoded["cut_at_ms"], int)  # Prism


def test_transport_encode_validates_before_emitting() -> None:
    """The producer NEVER serialises an off-contract value onto the wire.

    `encode_scene_control_leaf` runs the frozen validator before encoding, so
    a value violating any invariant raises rather than producing a string the
    consumer would have to reject post-transport.
    """
    bad = {
        "target_scene": "scene-screen-2",
        "overlay": {
            "kind": "wipe-cover",
            "reveal_ms": 250,
            "hold_ms": 200,
            "retract_ms": 250,
        },
        "cut_at_ms": 600,  # CUT-WINDOW violation (> reveal+hold = 450)
    }
    with pytest.raises(SceneControlContractError):
        encode_scene_control_leaf(bad)


def test_transport_decode_rejects_object_leaf() -> None:
    """A consumer receiving a raw OBJECT leaf (envelope bypass) fails loud.

    The contract is leaf-string-JSON; an object leaf could not have crossed
    the LSDP codec, so receiving one means a producer bypassed the envelope.
    The decoder rejects it rather than silently accepting an off-wire shape.
    """
    obj = _VALID[0]["value"]
    with pytest.raises(SceneControlContractError):
        decode_scene_control_leaf(obj)  # a dict, not the JSON string envelope


def test_transport_decode_rejects_non_json_string() -> None:
    """A malformed JSON envelope is rejected before the schema validator."""
    with pytest.raises(SceneControlContractError):
        decode_scene_control_leaf("{not valid json")


def test_transport_decode_rejects_malicious_after_parse() -> None:
    """Every malicious VALUE case is still rejected through the string envelope.

    Serialising a malicious payload to a string and decoding it MUST surface
    the SAME rejection the object validator gives — the envelope adds
    transport, it does not weaken any invariant. (PATH cases are excluded:
    they target the leaf path, not the value.)"""
    for case in _MALICIOUS:
        if "bad_path" in case:
            continue
        leaf_string = json.dumps(case["value"], sort_keys=True, separators=(",", ":"))
        with pytest.raises(SceneControlContractError):
            decode_scene_control_leaf(leaf_string)


# ---------------------------------------------------------------------------
# NEGATIVE — every malicious / malformed payload is REJECTED.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _MALICIOUS, ids=[c["name"] for c in _MALICIOUS])
def test_malicious_payload_is_rejected(case: dict[str, Any]) -> None:
    """The contract rejects the case (consumers issue zero obs-ws calls).

    A path-targeting case (`bad_path` set) must be rejected by the leaf PATH
    matcher; every other case must be rejected by the VALUE validator. A reject
    == the cut consumer (#72) issues zero obs-ws calls and the overlay consumer
    (#73) renders nothing.
    """
    if "bad_path" in case:
        # C-PATHREAL — the leaf path itself is illegal; the consumer's path
        # matcher must refuse to treat it as a scene_control leaf.
        with pytest.raises(SceneControlContractError):
            assert_canonical_leaf_path(case["bad_path"])
    else:
        with pytest.raises(SceneControlContractError):
            validate_scene_control(case["value"])


def test_every_invariant_has_negative_coverage() -> None:
    """Guard: the corpus proves every contract guarantee, not just one.

    A regression that dropped, say, every CUT-WINDOW case would otherwise pass
    silently. This asserts each invariant label is exercised by the corpus.
    """
    seen = {c["invariant"] for c in _MALICIOUS}
    for required in (
        "NO-OBS-NATIVE",
        "SCENE-ALLOWLIST",
        "OVERLAY-KIND-ALLOWLIST",
        "CUT-WINDOW",
        "bounds",
        "C-PATHREAL",
    ):
        assert required in seen, f"no negative case for {required}"


def test_cut_window_is_asserted_both_directions() -> None:
    """Belt-and-braces: the window invariant rejects too-early AND too-late.

    Independent of the fixtures, prove the validator structurally enforces
    BOTH bounds of `reveal_ms <= cut_at_ms <= reveal_ms+hold_ms` even when
    every other field is well-formed — the visual-safety core (§A4.2).
    """
    base = {
        "target_scene": "scene-screen-1",
        "overlay": {
            "kind": "wipe-cover",
            "reveal_ms": 250,
            "hold_ms": 200,
            "retract_ms": 250,
        },
    }
    too_early = {**base, "cut_at_ms": 249}  # < reveal_ms
    too_late = {**base, "cut_at_ms": 451}  # > reveal_ms + hold_ms
    on_lower = {**base, "cut_at_ms": 250}  # == reveal_ms (valid)
    on_upper = {**base, "cut_at_ms": 450}  # == reveal_ms + hold_ms (valid)
    with pytest.raises(SceneControlContractError):
        validate_scene_control(too_early)
    with pytest.raises(SceneControlContractError):
        validate_scene_control(too_late)
    # boundaries inclusive — accepted
    assert validate_scene_control(on_lower)["cut_at_ms"] == 250
    assert validate_scene_control(on_upper)["cut_at_ms"] == 450


def test_no_obs_native_construct_ever_validates() -> None:
    """Belt-and-braces: path/asset_id/action/transition are structurally refused.

    Independent of the fixtures, prove the validator refuses each superseded
    OBS-native construct at the level it could appear, even when everything
    else is a valid overlay payload — the pivot's defining guarantee (no
    OBS-native shape on the live contract, §A4.2).
    """
    base = {
        "target_scene": "scene-screen-1",
        "overlay": {
            "kind": "wipe-cover",
            "reveal_ms": 250,
            "hold_ms": 200,
            "retract_ms": 250,
        },
        "cut_at_ms": 250,
    }
    variants = [
        {**base, "path": "C:\\x"},
        {**base, "asset_id": "stinger-demo"},
        {**base, "action": "switch_program_scene"},
        {**base, "transition": {"kind": "stinger"}},
        {**base, "overlay": {**base["overlay"], "asset_id": "stinger-demo"}},
        {**base, "overlay": {**base["overlay"], "path": "C:\\x"}},
    ]
    for v in variants:
        with pytest.raises(SceneControlContractError):
            validate_scene_control(v)


def test_consumer_injects_its_own_allowlists() -> None:
    """The allowlist is an INTERFACE, not a hard-coded coupling.

    Proves #72/#73 can pass their own pinned scene/overlay sets — the
    mechanism is the contract, the names are downstream (#74 scenes, #73
    overlay element). Mirrors the prior contract's interface-not-coupling rule.
    """
    value = {
        "target_scene": "prod-scene-a",
        "overlay": {
            "kind": "prod-wipe",
            "reveal_ms": 250,
            "hold_ms": 200,
            "retract_ms": 250,
        },
        "cut_at_ms": 300,
    }
    # Rejected under the default demo allowlists...
    with pytest.raises(SceneControlContractError):
        validate_scene_control(value)
    # ...accepted when the consumer supplies the matching pinned sets.
    out = validate_scene_control(
        value,
        scene_allowlist=frozenset({"prod-scene-a", "prod-scene-b"}),
        overlay_kind_allowlist=frozenset({"prod-wipe"}),
    )
    assert out["target_scene"] == "prod-scene-a"
    assert out["overlay"]["kind"] == "prod-wipe"
