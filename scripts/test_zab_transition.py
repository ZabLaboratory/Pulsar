#!/usr/bin/env python3
"""Offline tests for the reusable ``zab-transition`` Canvas scene + the
animated smooth-fade playout helpers (Pulsar #79 → animated).

These cover everything provable **without a live ``pulsar.exe``** / VPS:

  - the ``scripts/fixtures/zab-transition.lsml.json`` scene is well-formed
    LSML 1.1, round-trips byte-for-byte;
  - it ANIMATES the logo on mount: the logo carries an ``animate`` directive
    (a single transition — ``transition{duration,easing}`` + opacity /
    ``transform.scale``), the authored mount fade/scale (NO keyframes block on
    the render root, NO ``wipe-cover`` render element — the root stays
    white+logo, never magenta);
  - it DECLARES the ``scene_control`` leaf in ``operator_inputs`` so the active
    white+logo scene fans the Blue delta out (Orion ``sceneAcceptsPath`` / F2)
    instead of silent-dropping — no more magenta-scene workaround;
  - the node form matches what Orion's ``lsmlNode`` emits and the runtime
    primitives read: props SPREAD at the node top level (never nested under a
    ``props`` object), a white ``frame`` root, a centred ``image`` child;
  - the logo rides as a complete base64 data-URI in ``image.src`` (a full
    JPEG/PNG, not a truncated URI) — no asset hosting, no Solar rebuild;
  - ``animate_mount_params`` derives the page's CSS mount keyframes from the
    directive, and ``prove_mount_ramp`` proves a captured sequence is a RAMP
    (the anti-faux-positif — neither blank nor already-settled);
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


def test_render_root_stays_white_logo_no_keyframes_no_wipecover():
    """The render ROOT stays white+logo: NO keyframes block on any node and NO
    wipe-cover render element — the leaf is DECLARED for fan-out, NOT lowered to a
    magenta cover, so the passage is white, never magenta."""
    b = _bundle()

    def walk(n):
        yield n
        for c in n.get("children", []) or []:
            yield from walk(c)

    for node in walk(b["layout"]):
        assert "keyframes" not in node, "render root carries no keyframes block"
        assert node.get("kind") != "wipe-cover", "no wipe-cover render element"


def test_scene_declares_the_scene_control_leaf_for_fanout():
    """The white+logo scene DECLARES the scene_control leaf in operator_inputs so
    Orion fans the Blue delta out while THIS scene is active (no silent-drop, no
    magenta-scene workaround). The path must equal the canonical 3-segment leaf."""
    b = _bundle()
    oinputs = b.get("operator_inputs")
    assert isinstance(oinputs, list) and oinputs, "must declare operator_inputs"
    paths = {oi["path"] for oi in oinputs}
    assert probe.M10_LEAF_PATH in paths, (
        f"must declare {probe.M10_LEAF_PATH!r} (got {sorted(paths)})")
    # The declaration is the SAME canonical leaf the m10-orion-scene fixture uses
    # and the probe consumes — they cannot drift.
    assert probe.M10_LEAF_PATH == "__inputs.blue.m10-scene-control.scene_control"


def test_logo_carries_an_animate_mount_directive():
    """The logo carries an ``animate`` directive — a SINGLE transition (the
    authored mount play): transition{duration(ms),easing} + opacity/transform.scale.
    No multi-step keyframes (no compiler). The duration is in MS (>= a visible
    band) and the easing is a known LSML easing token."""
    img = probe._find_node(_bundle()["layout"], "image")
    animate = img.get("animate")
    assert isinstance(animate, dict), "logo must carry an `animate` directive"
    tx = animate.get("transition")
    assert isinstance(tx, dict) and tx.get("duration"), "non-zero transition.duration"
    # MS unit (compiler reads duration_ms = transition.duration; runtime /1000).
    assert tx["duration"] >= 300, "a visible mount fade is >= ~300ms"
    assert tx.get("easing") in {"linear", "ease-in", "ease-out", "ease-in-out",
                                "spring"}
    # The directive animates at least opacity or scale (the fade/scale-in).
    assert animate.get("opacity") is not None or (
        animate.get("transform") or {}).get("scale") is not None


def test_logo_animate_authors_a_from_initial_state():
    """The directive carries an authored ``from`` initial state (LSML 1.1 /
    @lumencast 0.3.0) strictly below the settled targets — the SAME `from` the
    Solar runtime hands framer-motion as `initial`, so the scene mount-plays
    NATIVELY in the real bundle (no remount-CSS trick needed for visibility)."""
    img = probe._find_node(_bundle()["layout"], "image")
    frm = img["animate"].get("from")
    assert isinstance(frm, dict), "animate must author a `from` initial state"
    assert frm.get("opacity", 1) < img["animate"].get("opacity", 1), \
        "from.opacity strictly below the settled opacity (the fade-in)"
    scale_from = (frm.get("transform") or {}).get("scale", 1)
    scale_to = (img["animate"].get("transform") or {}).get("scale", 1)
    assert scale_from < scale_to, "from scale strictly below settled (the scale-up)"


def test_animate_mount_params_derives_a_visible_ramp():
    """``animate_mount_params`` turns the directive into the page's CSS mount
    keyframes: opacity 0→settled, scale <1→settled, the directive's duration+easing.
    A FROM start strictly below the TO target is what makes the mount VISIBLE."""
    img = probe._find_node(_bundle()["layout"], "image")
    mp = probe.animate_mount_params(img)
    assert mp["duration_ms"] >= 300
    assert mp["opacity_from"] < mp["opacity_to"], "the logo FADES in"
    assert mp["scale_from"] < mp["scale_to"], "the logo SCALES up"
    assert mp["easing"] in {"linear", "ease-in", "ease-out", "ease-in-out",
                            "cubic-bezier(0.34,1.56,0.64,1)"}


def test_animate_mount_params_falls_back_without_from():
    """A directive WITHOUT ``from`` keeps the prior hidden-start defaults
    (opacity 0 → settled, scale 0.85 → settled) so older fixtures keep their
    visible ramp (no regression on the pre-`from` authoring)."""
    mp = probe.animate_mount_params({
        "kind": "image", "src": "data:image/png,x",
        "animate": {"transition": {"duration": 400, "easing": "ease-out"},
                    "opacity": 1, "transform": {"scale": 1}},
    })
    assert mp["opacity_from"] == 0.0 and mp["opacity_to"] == 1.0
    assert mp["scale_from"] == 0.85 and mp["scale_to"] == 1.0


def test_animate_mount_params_noop_without_directive():
    """A logo with no ``animate`` directive yields a no-op (duration 0) so the page
    degrades to the prior static render — the helper never invents an animation."""
    mp = probe.animate_mount_params({"kind": "image", "src": "data:image/png,x"})
    assert mp["duration_ms"] == 0


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


# --------------------------------------------------------------------------
# ANIMATION proof (anti-faux-positif) — prove_mount_ramp + the reload primitive.
# --------------------------------------------------------------------------
def _seq(footprints: list[float]) -> list[dict]:
    """Build a fake capture sequence from a list of nonbg_ratio footprints."""
    return [
        {"i": i, "t_ms": i * 80.0, "nonbg_ratio": f, "distinct": int(2 + f * 1000),
         "mean": (255 - f * 80, 255 - f * 80, 255 - f * 80), "path": f"seq-{i}.png"}
        for i, f in enumerate(footprints)
    ]


def test_prove_mount_ramp_accepts_a_real_ramp():
    """A footprint that grows from ~0 to a settled peak (the logo materialising)
    is a RAMP — animation VISIBLE."""
    ok, why = probe.prove_mount_ramp(
        _seq([0.0, 0.02, 0.08, 0.16, 0.22, 0.25, 0.25, 0.25]), settled_white=True)
    assert ok, why
    assert "RAMP proven" in why


def test_prove_mount_ramp_rejects_already_settled():
    """Every frame identical/settled ⇒ the animation is NOT visible — the helper
    must say so (no faux-positif)."""
    ok, why = probe.prove_mount_ramp(
        _seq([0.25, 0.25, 0.25, 0.25, 0.25]), settled_white=True)
    assert not ok
    assert "already-settled" in why or "did not RAMP" in why


def test_prove_mount_ramp_rejects_all_blank():
    """A flat empty field (the logo never rendered — blank CEF) is not a ramp."""
    ok, why = probe.prove_mount_ramp(
        _seq([0.0, 0.0, 0.0, 0.0]), settled_white=False)
    assert not ok
    assert "never rendered" in why or "FLAT field" in why


def test_prove_mount_ramp_rejects_too_few_frames():
    """Fewer than 3 frames cannot prove a ramp — be honest, don't guess."""
    ok, why = probe.prove_mount_ramp(_seq([0.0, 0.25]), settled_white=True)
    assert not ok
    assert "too few" in why


def test_prove_mount_ramp_rejects_blank_to_settled_without_intermediate():
    """THE prior faux positif (Keeper's real run): pre-paint blanks (the CEF had
    not painted yet) then frames already settled — ZERO frame caught the ramp in
    flight. The old checker read the blank as the bottom of a ramp and PASSED;
    the hardened checker must FAIL with the capture-hole diagnostic."""
    ok, why = probe.prove_mount_ramp(
        _seq([0.0, 0.0, 0.0, 0.25, 0.25]), settled_white=True)
    assert not ok, "blank→settled with no intermediate frame must NOT pass"
    assert "ZERO intermediate" in why or "capture hole" in why


def test_prove_mount_ramp_requires_a_true_intermediate_frame():
    """A near-jump where the only non-settled frames sit below 15% of the peak
    (still effectively blank) is NOT in-flight evidence either."""
    ok, why = probe.prove_mount_ramp(
        _seq([0.0, 0.01, 0.02, 0.25, 0.25, 0.25]), settled_white=True)
    assert not ok
    assert "ZERO intermediate" in why or "capture hole" in why


def test_prove_mount_ramp_accepts_dense_in_flight_intermediates():
    """The fixed harness (paint-gated cut + ~70ms dense sampling over a 1200ms
    ramp) yields SEVERAL true intermediate frames between blank and settled —
    that is what ANIMATED (VISIBLE) must look like now."""
    ok, why = probe.prove_mount_ramp(
        _seq([0.0, 0.01, 0.05, 0.09, 0.13, 0.17, 0.21, 0.24, 0.25, 0.25]),
        settled_white=True)
    assert ok, why
    assert "RAMP proven" in why and "IN FLIGHT" in why


def test_prove_mount_ramp_rejects_falling_intermediates():
    """Intermediate footprints that FALL over time (a blend/teardown artefact,
    not a logo growing in) are not a mount ramp — monotone-ish rise required."""
    ok, why = probe.prove_mount_ramp(
        _seq([0.0, 0.20, 0.05, 0.25, 0.25]), settled_white=True)
    assert not ok
    assert "FALL" in why


def test_prove_mount_ramp_excludes_crossfade_blend_frames():
    """Fade #1 blend frames (screen-1 content still dissolving — a FOREIGN modal,
    far from the scene's white field) must be excluded from the footprint signal,
    not mistaken for ramp evidence. A real ramp behind them still proves."""
    blend = {"i": 0, "t_ms": 0.0, "nonbg_ratio": 0.40, "distinct": 900,
             "mean": (90.0, 110.0, 130.0), "modal": (30, 60, 90),
             "path": "seq-0.png"}
    ramp = _seq([0.0, 0.05, 0.12, 0.19, 0.25, 0.25])
    for f in ramp:
        f["modal"] = (255, 255, 255)  # the scene's own white field
        f["i"] += 1
        f["t_ms"] += 80.0
    ok, why = probe.prove_mount_ramp([blend] + ramp, settled_white=True)
    assert ok, why
    assert "1 blend frame(s) excluded" in why


def test_mount_anim_ms_pins_the_fixture_duration():
    """probe.MOUNT_ANIM_MS (the --hold-ms clamp + capture-window sizing) must
    mirror the fixture's authored animate.transition.duration — if one moves
    without the other, the hold/capture window and the real ramp drift apart."""
    img = probe._find_node(_bundle()["layout"], "image")
    assert probe.MOUNT_ANIM_MS == img["animate"]["transition"]["duration"]


def test_mount_duration_survives_the_paint_gate_latency():
    """#79 ramp-visibility: the paint-gated cut still loses ~100-400ms of ramp to
    detection→cut latency, so the authored duration must be generous (>= 1000ms)
    for the ramp to stay clearly visible ON AIR."""
    img = probe._find_node(_bundle()["layout"], "image")
    assert img["animate"]["transition"]["duration"] >= 1000


def test_dense_capture_window_covers_the_full_ramp():
    """The dense sampler must (a) poll at the brief's ~60-80ms cadence and (b)
    span at least the full ramp + margin, so several frames land INSIDE the
    animation window instead of straddling it."""
    assert probe.MOUNT_SEQ_INTERVAL_S <= 0.08
    nominal_window_ms = probe.MOUNT_SEQ_FRAMES * probe.MOUNT_SEQ_INTERVAL_S * 1000
    assert nominal_window_ms >= probe.MOUNT_ANIM_MS + 600


def test_bust_url_forces_a_distinct_url():
    """The reload primitive appends a distinct `_replay` param so obs-browser's
    same-URL early-return does NOT fire (a fresh CEF load → the mount replays)."""
    u0 = probe._bust_url("http://127.0.0.1:9/zab-transition.html", 1)
    u1 = probe._bust_url("http://127.0.0.1:9/zab-transition.html", 2)
    assert u0 != u1, "different tokens → different URLs (forces a reload each time)"
    assert "_replay=1" in u0 and "_replay=2" in u1
    # An existing query keeps its params (we append with &).
    u2 = probe._bust_url("http://h/solar?orion=wss://x&mode=broadcast", 7)
    assert u2.endswith("&_replay=7") and "orion=" in u2


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
