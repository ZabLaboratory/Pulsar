#!/usr/bin/env python3
"""Offline tests for the reusable ``zab-transition`` Canvas scene + the
smooth-fade playout helpers (Pulsar #79).

These cover everything provable **without a live ``pulsar.exe``** / VPS:

  - the ``scripts/fixtures/zab-transition.lsml.json`` scene is well-formed
    LSML 1.1, round-trips byte-for-byte, and is a STATIC render scene (no
    keyframes / no ``wipe-cover`` element / no ``scene_control`` leaf — the
    dormant ``lower_wipe_cover`` path is genuinely unused);
  - the node form matches what Orion's ``lsmlNode`` emits and the runtime
    primitives read: props SPREAD at the node top level (never nested under a
    ``props`` object), a white ``frame`` root, a centred ``image`` child;
  - the logo rides as a complete base64 data-URI in ``image.src`` (a full
    JPEG/PNG, not a truncated URI) — no asset hosting, no Solar rebuild;
  - the white-MID check helper classifies a white frame as covered and a
    black / busy frame as not (the passage is white, not magenta/black);
  - the SMOOTH-FADE helpers: ``resolve_fade_transition_name`` picks the OBS Fade
    transition (by ``fade_transition`` kind, with a name fallback), and
    ``resolve_transition_target`` drives the playout's target scene off the REAL
    Blue rule leaf in live-wire (the brief's core) vs the demo target in the dry
    path.

Run:
    pytest scripts/test_zab_transition.py -v
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import pathlib
import types

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
FIXTURE = _HERE / "fixtures" / "zab-transition.lsml.json"


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "probe_m10_canvas_live", _HERE / "probe-m10-canvas-live.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = _load_probe()


def _bundle() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_valid_lsml_1_1_and_round_trips():
    b = _bundle()
    assert b["lsml"] == "1.1"
    assert isinstance(b.get("scene_id"), str) and b["scene_id"]
    assert b["scene_version"].startswith("sha256:")
    # Byte round-trip — the bundle survives a parse → dump → parse unchanged
    # (it is plain JSON LSML, no exotic types).
    assert json.loads(json.dumps(b)) == b


def test_root_is_a_white_frame_with_props_spread_at_top_level():
    layout = _bundle()["layout"]
    assert layout["kind"] == "frame"
    assert layout["background"] == "#FFFFFF"
    assert layout["width"] == "100%" and layout["height"] == "100%"
    # Orion lsmlNode form: static props are SPREAD at the node top level, never
    # nested under a `props` object — a nested block leaves the runtime's flat
    # resolved.* lookups empty (the scene paints at defaults).
    assert "props" not in layout


def test_scene_is_static_no_keyframes_no_wipecover_no_leaf():
    """The franc passage is a STATIC scene — the dormant lower_wipe_cover path is
    genuinely unused (the brief: we do NOT use lower_wipe_cover)."""
    b = _bundle()
    assert not b.get("operator_inputs"), "static scene declares no operator_inputs"

    def walk(n):
        yield n
        for c in n.get("children", []) or []:
            yield from walk(c)

    for node in walk(b["layout"]):
        assert "keyframes" not in node, "static scene carries no keyframes"
        assert node.get("kind") != "wipe-cover", "no wipe-cover authoring element"


def test_logo_is_a_complete_embedded_data_uri_image():
    img = probe._find_node(_bundle()["layout"], "image")
    assert img is not None, "fixture must hold an `image` node for the logo"
    assert "props" not in img, "image props spread at top level (lsmlNode form)"
    src = img["src"]
    assert src.startswith("data:image/"), "logo must be an embedded data-URI"
    raw = base64.b64decode(src.split(",", 1)[1])
    # A complete JPEG (FFD8..FFD9) or PNG (signature + IEND), not truncated.
    if raw[:2] == b"\xff\xd8":
        assert raw[-2:] == b"\xff\xd9", "JPEG data-URI truncated"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        assert b"IEND" in raw[-12:], "PNG data-URI truncated"
    else:
        raise AssertionError("logo data-URI is neither JPEG nor PNG")
    # A reasonable, centred logo size (~15-20% of the 1920 canvas width).
    assert isinstance(img["width"], int) and 200 <= img["width"] <= 480
    assert img.get("fit") == "contain"


def test_logo_centred_via_a_stack():
    """The logo is centred by a `stack` (align/justify center) — the runtime's
    documented centering primitive (Solar render.test.tsx stack/align/justify)."""
    layout = _bundle()["layout"]
    stack = layout["children"][0]
    assert stack["kind"] == "stack"
    assert stack["align"] == "center" and stack["justify"] == "center"
    assert stack["children"][0]["kind"] == "image"


def test_load_transition_bundle_accepts_the_fixture():
    """The probe's in-process validator (the offline proof leg) accepts the
    shipped fixture and raises on none of its invariants."""
    msgs: list[str] = []
    bundle = probe.load_transition_bundle(lambda *a: msgs.append(" ".join(map(str, a))))
    assert bundle["lsml"] == "1.1"
    assert any("zab-transition scene OK" in m for m in msgs)


def test_white_check_classifies_white_logo_black_and_busy():
    """is_white_with_logo: a near-white field with a small centred logo is
    covered; a black frame (Solar did not paint) and a busy desktop frame
    (a visible hard cut to a capture) are NOT."""
    # Near-white with a small dark logo darkening the mean a little.
    white_mid = {"mean": (238.0, 238.0, 238.0), "distinct": 400}
    ok, _ = probe.is_white_with_logo(white_mid)
    assert ok

    black_mid = {"mean": (5.0, 5.0, 5.0), "distinct": 1}
    ok, why = probe.is_white_with_logo(black_mid)
    assert not ok and "BLACK" in why

    busy_mid = {"mean": (90.0, 70.0, 40.0), "distinct": 5000}
    ok, _ = probe.is_white_with_logo(busy_mid)
    assert not ok


def test_transition_scene_name_is_not_a_scene_control_target():
    """The transition scene is a local OBS-scene name, NOT a scene_control
    allowlist member (the allowlist is the leaf-driven target contract; the
    transition is the scene we fade THROUGH). It must stay out of the allowlist."""
    assert probe.TRANSITION_SCENE == "scene-transition"
    assert probe.TRANSITION_SCENE not in probe.SCENE_ALLOWLIST


# --------------------------------------------------------------------------
# SMOOTH FADE (#79, this branch) — the playout crossfades via the OBS Fade
# transition instead of hard-cutting, and the target scene B is driven by the
# REAL Blue rule leaf.
# --------------------------------------------------------------------------
def _silent(*_a) -> None:
    pass


def test_resolve_fade_transition_name_picks_by_kind():
    """The Fade is identified by its stable ``fade_transition`` kind, NOT a
    hard-coded display name (the name is localisable)."""
    transitions = [
        {"transitionName": "Cut", "transitionKind": "cut_transition"},
        {"transitionName": "Fondu", "transitionKind": "fade_transition"},  # localised
        {"transitionName": "Swipe", "transitionKind": "swipe_transition"},
    ]
    assert probe.resolve_fade_transition_name(transitions) == "Fondu"


def test_resolve_fade_transition_name_falls_back_to_literal_name():
    """If no entry carries the fade kind but one is literally named "Fade"
    (a build that reports an empty kind), fall back to that name."""
    transitions = [
        {"transitionName": "Cut", "transitionKind": "cut_transition"},
        {"transitionName": "Fade", "transitionKind": ""},
    ]
    assert probe.resolve_fade_transition_name(transitions) == "Fade"


def test_resolve_fade_transition_name_none_when_no_fade():
    """A stripped LIGHT build with no Fade transition yields None — the caller
    then degrades to a bare cut (the fade is the antenna run)."""
    transitions = [{"transitionName": "Cut", "transitionKind": "cut_transition"}]
    assert probe.resolve_fade_transition_name(transitions) is None
    assert probe.resolve_fade_transition_name([]) is None


def test_fade_duration_default_and_kind_constants():
    """The crossfade duration default sits in the brief's 400-500ms band, and
    the Fade kind constant is OBS's ``fade_transition``."""
    assert probe.FADE_TRANSITION_KIND == "fade_transition"
    assert 400 <= probe.FADE_DURATION_MS_DEFAULT <= 500


def _args(**kw) -> types.SimpleNamespace:
    base = dict(delivery="loopback-leaf", gateway_url="", blueprint_id="")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_resolve_transition_target_uses_demo_target_on_the_dry_path():
    """--loopback-leaf / no-VPS: target B falls back to the demo leaf target
    (the VPS-less structural proof) — NO Blue trigger fired."""
    target, source = asyncio.run(probe.resolve_transition_target(
        args=_args(delivery="loopback-leaf"),
        redactor=probe.Redactor(), log=_silent))
    assert target == probe.DEMO_SCENE_CONTROL_VALUE["target_scene"]
    assert "demo leaf" in source


def test_resolve_transition_target_consumes_the_real_blue_leaf(monkeypatch):
    """--live-wire: target B is the ``target_scene`` the REAL Blue rule leaf
    emits (read off /show/stream), validated through the frozen contract — NOT a
    probe constant. We stub deliver_leaf to return the LSDP string-JSON leaf the
    real Blue-VPS pushes, and assert the validated rule target flows through."""
    rule_ctrl = {
        "target_scene": "scene-screen-1",  # the RULE chose screen-1, not the demo's screen-2
        "overlay": {"kind": "wipe-cover", "reveal_ms": 250, "hold_ms": 200,
                    "retract_ms": 250},
        "cut_at_ms": 250,
    }
    wire_value = json.dumps(rule_ctrl)  # the LSDP string envelope Blue emits (#94/#31)

    async def fake_deliver_leaf(*, args, redactor, log, leaf_state, orion):
        return 0.0, wire_value

    monkeypatch.setattr(probe, "deliver_leaf", fake_deliver_leaf)
    target, source = asyncio.run(probe.resolve_transition_target(
        args=_args(delivery="live-wire", gateway_url="https://gw", blueprint_id="bp"),
        redactor=probe.Redactor(), log=_silent))
    # The target is the RULE's choice, decoded+validated — distinct from the demo.
    assert target == "scene-screen-1"
    assert target != probe.DEMO_SCENE_CONTROL_VALUE["target_scene"]
    assert "Blue rule leaf" in source


def test_resolve_transition_target_rejects_an_invalid_blue_leaf(monkeypatch):
    """A Blue leaf that fails the frozen contract must NOT silently pick a
    target — resolve_transition_target raises rather than fading to an
    unvalidated scene (C-INJ posture for the target)."""
    async def fake_deliver_leaf(*, args, redactor, log, leaf_state, orion):
        return 0.0, json.dumps({"target_scene": "rogue-scene"})  # not in the allowlist

    monkeypatch.setattr(probe, "deliver_leaf", fake_deliver_leaf)
    with pytest.raises(RuntimeError):
        asyncio.run(probe.resolve_transition_target(
            args=_args(delivery="live-wire", gateway_url="https://gw",
                       blueprint_id="bp"),
            redactor=probe.Redactor(), log=_silent))
