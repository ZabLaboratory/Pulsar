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
import json
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
    scene_control payload in the re-frozen Amendment 4 OVERLAY shape (#78):
    target_scene + overlay{kind,reveal_ms,hold_ms,retract_ms} + cut_at_ms,
    round-tripped through the consumer guard. The old stinger/transition
    default is rejected by this same contract (the regression #78 fixes)."""
    bundle = m10.build_orion_declaration()
    default = next(
        oi["default"] for oi in bundle["operator_inputs"]
        if oi["path"] == m10.M10_LEAF_PATH
    )
    validated = validate_scene_control(default)
    assert validated["target_scene"] in DEFAULT_SCENE_ALLOWLIST
    assert validated["overlay"]["kind"] == "wipe-cover"
    # The cut-window invariant holds (reveal_ms <= cut_at_ms <= reveal+hold) —
    # the hard-cut lands inside the opaque plateau (§A4.2 visual-safety core).
    ov = validated["overlay"]
    assert ov["reveal_ms"] <= validated["cut_at_ms"] <= ov["reveal_ms"] + ov["hold_ms"]
    # No superseded OBS-native construct survived the migration: the strict
    # contract echoes ONLY the overlay-form keys (no action/transition/asset_id).
    assert set(validated) == {"target_scene", "overlay", "cut_at_ms"}
    assert "action" not in default
    assert "transition" not in default


def test_f2_default_is_the_overlay_form_not_the_superseded_stinger():
    """Regression guard for #78: the fixture default must NOT carry the old
    Amendment 1/2 stinger shape (action/transition/asset_id) — that form is
    rejected by the re-frozen contract and was the known regression."""
    bundle = json.loads(m10.ORION_SCENE_FIXTURE.read_text(encoding="utf-8"))
    default = next(
        oi["default"] for oi in bundle["operator_inputs"]
        if oi["path"] == m10.M10_LEAF_PATH
    )
    assert "transition" not in default
    assert "action" not in default
    assert "overlay" in default and "cut_at_ms" in default


def test_render_root_is_the_wipe_cover_authoring_element():
    """#64 / Amendment 5 §A5.3: the scene's RENDER ROOT is the `wipe-cover`
    AUTHORING element Orion's lowerWipeCover (Orion/internal/compiler/
    lower_wipe_cover.go) intercepts and lowers to a keyframed full-screen
    frame. The element carries its props SPREAD at the LSML node's top level
    (Orion's EmitLSML node form: node[k]=raw, not a nested `props` object),
    and the prop NAMES must match what lowerWipeCover reads off node.Props
    EXACTLY — leaf / reveal_ms / hold_ms / retract_ms / fill — or the lowering
    falls through to an inert node that renders nothing."""
    bundle = json.loads(m10.ORION_SCENE_FIXTURE.read_text(encoding="utf-8"))
    root = bundle["layout"]
    assert root["kind"] == "wipe-cover", (
        "render root must be the wipe-cover authoring element Orion#65 lowers "
        f"to keyframes, got kind={root.get('kind')!r}"
    )
    # The replay is keyed on the SAME leaf the scene declares (C-FANOUT) — the
    # scene_control delta remounts the KeyframePlayer.
    assert root["leaf"] == m10.M10_LEAF_PATH
    # Timings are the fixture's authored 400/500/400 (parity with the Orion
    # lower_wipe_cover_test oracle + the operator_input default overlay).
    assert root["reveal_ms"] == 400
    assert root["hold_ms"] == 500
    assert root["retract_ms"] == 400
    # The franc-magenta cover the M10 probe asserts as MID (Solar's
    # DEFAULT_COVER_FILL; the lowering honours an authored `fill`).
    assert root["fill"] == "#C81E5A"
    # Props are SPREAD at the node top level, never nested under `props` —
    # that is the LSML 1.1 node form Orion's lsmlNode emits and the compiler
    # parses back into node.Props. A nested `props` block would leave the
    # lowering's stringProp/positiveIntProp lookups empty → inert fall-through.
    assert "props" not in root, (
        "wipe-cover props must be spread at the LSML node top level, not "
        "nested under a `props` object (Orion lsmlNode form)"
    )


def test_render_root_timings_match_the_seeded_overlay_default():
    """The render-root element timings and the operator_input overlay default
    are ONE timeline — they must agree, or the seeded boot value would replay
    a different cover than the authored render root. (The render root carries
    the authoritative geometry; the leaf only triggers the replay — A5.5.)"""
    bundle = json.loads(m10.ORION_SCENE_FIXTURE.read_text(encoding="utf-8"))
    root = bundle["layout"]
    overlay = next(
        oi["default"]["overlay"] for oi in bundle["operator_inputs"]
        if oi["path"] == m10.M10_LEAF_PATH
    )
    for k in ("reveal_ms", "hold_ms", "retract_ms"):
        assert root[k] == overlay[k], (
            f"render-root {k}={root[k]} disagrees with the seeded overlay "
            f"default {k}={overlay[k]}"
        )


def test_f2_rejects_a_seed_with_an_unknown_target_scene():
    """Defence: a declaration seeding an off-allowlist target_scene must be
    caught by the same guard the consumer runs (C-INJ)."""
    bad = {
        "target_scene": "scene-evil",
        "overlay": {"kind": "wipe-cover", "reveal_ms": 250,
                    "hold_ms": 200, "retract_ms": 250},
        "cut_at_ms": 250,
    }
    with pytest.raises(SceneControlContractError):
        validate_scene_control(bad)


def test_f2_rejects_a_seed_with_the_superseded_stinger_shape():
    """The exact regression #78 fixes: the old stinger default
    (action/target_scene/transition) is rejected by the re-frozen contract —
    it is no longer a valid scene_control value."""
    stinger = {
        "action": "switch_program_scene",
        "target_scene": "scene-screen-1",
        "transition": {"kind": "stinger", "asset_id": "stinger-demo",
                       "point_ms": 0, "duration_ms": 600},
    }
    with pytest.raises(SceneControlContractError):
        validate_scene_control(stinger)


def test_monitor_setting_keys_match_fork_sources():
    """U1 / #56: the harness pins via the two keys the fork's monitor_capture
    sources actually read — 'monitor_id' (DXGI duplicator string device id)
    and 'monitor' (legacy GDI integer index)."""
    assert m10.SETTING_MONITOR_ID == "monitor_id"
    assert m10.SETTING_MONITOR_IDX == "monitor"
    assert m10.MONITOR_CAPTURE_KIND == "monitor_capture"


def test_capture_method_is_forced_to_wgc():
    """#78 pivot deblock: the harness pins the monitor_capture method to
    WGC (the integer enum METHOD_WGC=2 under the 'method' key) so capture is
    non-black in a headless / non-interactive agent context where the DXGI
    duplicator returns 887A0004 (SPIKE-GPU, #72/#77)."""
    assert m10.SETTING_METHOD == "method"
    assert m10.METHOD_WGC == 2


def test_blueprint_slug_matches_contract_fixture():
    """The slug is the one Blue #58 / the contract fixture pin — the leaf
    subtree the scene_control output maps under."""
    assert m10.M10_BLUEPRINT_SLUG == "m10-scene-control"
