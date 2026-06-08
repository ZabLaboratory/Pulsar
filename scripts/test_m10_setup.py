#!/usr/bin/env python3
"""Offline tests for the M10 scene-setup harness (``m10_setup.py``, #60).

These cover everything provable **without a live ``pulsar.exe``**:

  - the two OBS scene names are EXACTLY the frozen ``scene_control`` contract
    allowlist (no silent name drift vs Prism #63 / Blue #58 / the ADR);
  - the F2 Orion declaration fixture declares the canonical 3-segment leaf
    path ``__inputs.blue.m10-scene-control.scene_control`` (C-PATHREAL) and
    its seeded default is a contract-valid ``scene_control`` value, so Orion's
    boot seed is itself legal and ``sceneAcceptsPath`` will fan the delta out
    (C-FANOUT precondition, ADR §A2.3);
  - the harness's display-pinning settings keys match the fork's
    ``monitor_capture`` sources (U1 / #56).

The LIVE legs — CreateScene/CreateInput against ``pulsar.exe`` + the
distinct-monitor assertion — are exercised by running ``m10_setup.py`` on a
full build (manual / #61); they are out of scope for this unit module, which
must run on a bare runner with no OBS build.

Run:
    pytest scripts/test_m10_setup.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_HERE = pathlib.Path(__file__).resolve().parent


def _load_m10():
    spec = importlib.util.spec_from_file_location(
        "m10_setup", _HERE / "m10_setup.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m10 = _load_m10()

# The contract is the source of truth — import it the same way the harness does.
import sys  # noqa: E402

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from contracts.scene_control import (  # noqa: E402
    DEFAULT_SCENE_ALLOWLIST,
    SceneControlContractError,
    validate_scene_control,
)


def test_scene_names_match_contract_allowlist():
    """The two OBS target scenes ARE the frozen contract allowlist — the
    names are an interface shared with Prism #63 (SCENE_ALLOWLIST) and the
    ADR, never a local literal that can drift."""
    assert {m10.SCENE_SCREEN_1, m10.SCENE_SCREEN_2} == set(DEFAULT_SCENE_ALLOWLIST)
    assert m10.SCENE_SCREEN_1 == "scene-screen-1"
    assert m10.SCENE_SCREEN_2 == "scene-screen-2"


def test_leaf_path_is_canonical_three_segment():
    """F1 / C-PATHREAL: the harness anchors on the real 3-segment leaf path,
    not the rejected 2-segment form."""
    assert m10.M10_LEAF_PATH == "__inputs.blue.m10-scene-control.scene_control"
    # 4 segments joined by 3 dots: __inputs . blue . <slug> . scene_control
    # (the slug itself uses hyphens, never dots — Blue's _SEGMENT_RE).
    assert m10.M10_LEAF_PATH.count(".") == 3
    assert m10.M10_LEAF_PATH.split(".") == [
        "__inputs", "blue", "m10-scene-control", "scene_control"
    ]


def test_f2_orion_fixture_declares_the_leaf_path():
    """F2 / C-FANOUT: the Orion scene the harness authors MUST declare the
    exact scene_control leaf path, or Orion silent-drops the Blue delta and
    it never reaches Prism (ADR §A2.3)."""
    bundle = m10.build_orion_declaration()  # raises if the declaration is missing
    paths = [oi.get("path") for oi in bundle.get("operator_inputs", [])]
    assert m10.M10_LEAF_PATH in paths


def test_f2_seeded_default_is_a_valid_scene_control_value():
    """Orion seeds the declared default on boot — it must itself be a legal
    scene_control payload (round-trip through the consumer guard)."""
    bundle = m10.build_orion_declaration()
    default = next(
        oi["default"] for oi in bundle["operator_inputs"]
        if oi["path"] == m10.M10_LEAF_PATH
    )
    validated = validate_scene_control(default)
    assert validated["action"] == "switch_program_scene"
    assert validated["target_scene"] in DEFAULT_SCENE_ALLOWLIST
    assert validated["transition"]["kind"] in {"stinger", "fade"}
    # The seed carries an asset_id KEY, never a path (C-PATH).
    assert "path" not in validated["transition"]


def test_f2_rejects_a_seed_with_an_unknown_target_scene():
    """Defence: a declaration seeding an off-allowlist target_scene must be
    caught by the same guard the consumer runs (C-INJ)."""
    bad = {
        "action": "switch_program_scene",
        "target_scene": "scene-evil",
        "transition": {"kind": "stinger", "asset_id": "stinger-demo",
                       "point_ms": 0, "duration_ms": 600},
    }
    with pytest.raises(SceneControlContractError):
        validate_scene_control(bad)


def test_monitor_setting_keys_match_fork_sources():
    """U1 / #56: the harness pins via the two keys the fork's monitor_capture
    sources actually read — 'monitor_id' (DXGI duplicator string device id)
    and 'monitor' (legacy GDI integer index)."""
    assert m10.SETTING_MONITOR_ID == "monitor_id"
    assert m10.SETTING_MONITOR_IDX == "monitor"
    assert m10.MONITOR_CAPTURE_KIND == "monitor_capture"


def test_blueprint_slug_matches_contract_fixture():
    """The slug is the one Blue #58 / the contract fixture pin — the leaf
    subtree the scene_control output maps under."""
    assert m10.M10_BLUEPRINT_SLUG == "m10-scene-control"
