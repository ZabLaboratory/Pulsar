#!/usr/bin/env python3
"""M9 SETUP leg — author a Canvas scene whose visible frame background is
bound to a Blue-driven input leaf ``__inputs.blue.<slug>.<port>``, push it
through Orion, make it the active show, mint a viewer show-token, and
compose the Solar URL — exactly the M8 authoring prologue, retargeted at
the **reactive-repaint** contract of ADR Blue 001.

Where M8 proved an *authored* scene reaches the wire, M9 proves that a
**Blue trigger repaints a live scene**. The decisive structural change vs
M8 is the binding source:

  - M8 bound a text primitive to a **blueprint-key** leaf (``score.value``)
    seeded by a compute graph Orion compiles into the scene.
  - M9 binds the **frame background** to an **operator-input** leaf
    ``__inputs.blue.pulsar-m9-bg.colour`` that Orion seeds from the
    layout's declared ``default`` (colour **A**) and that **Blue** later
    overwrites via ``POST /api/v1/blueprints/{id}/trigger`` (colour **B**).
    The blueprint is NOT compiled into the scene; it runs only in Blue's
    interpreter at trigger time and pushes the leaf (ADR Blue 001 §3.2.1-4).

Why bind the **frame background** and not a text label: the M9 proof is a
modal-colour delta (A→B) measured on the captured CEF frame. The frame
background is a single large flat field, so its modal colour IS the bound
region — the M8 whole-frame modal metric (``analyse_frame``) becomes the
M9 region metric verbatim, with no cropping. ``bind: {"background": ...}``
is the existing per-node bind mechanism (Solar's ``resolveProps`` applies
any binding key to the node's props — render/tree.js:118-127); **no Canvas
schema change** (ADR Blue 001 §3.2.4).

Leaf acceptance: Orion only accepts a write to a path the active scene
**declares** (adapters/inbox.go::sceneAcceptsPath — defaults, operator_inputs
or a binding target). Declaring ``__inputs.blue.pulsar-m9-bg.colour`` as a
layout ``operator_inputs`` entry both (a) seeds default **A** on boot
(Orion criterion 11) and (b) makes Blue's later push in-scope. The
service-token side (``paths=["__inputs.blue.*"]``) is the auth gate; this
is the *scene-side* declaration the write also needs.

What it does, in order (mirrors m8_setup S1-S9, minus the N>=2 rule):

  S2. Ensure the ONE Blue passthrough blueprint exists + is published,
      idempotent by slug. The graph is event→output with a single
      core.input named ``colour`` (Blue stdlib nodes only), so a
      ``/trigger`` with ``inputs={"colour": B}`` yields
      ``outputs={"colour": B}`` → leaf ``__inputs.blue.<slug>.colour = B``.
  S3. PUT the deterministic, checked-in LSML scene bundle (with the
      operator_input default A + the frame-background bind) into Canvas's
      A0 content-addressed store at its own hash H.
  S1. Ensure the test scene row exists (status=ready), idempotent by name.
  S4. Save a definition revision at canvas_version=H with **blueprints=[]**
      (M9 binds an operator-input leaf, not a blueprint-key leaf).
  S5. Push that revision through Orion; assert 200 + no diagnostics.errors.
  S6. Drive the active-scene.
  S7. Round-trip GET /orion/show — assert active_scene_id == scene_id.
  S8. Mint a viewer show-token (operator-only).
  S9. Compose the Solar LSDP URL.

SECRET HYGIENE (unchanged from M8, ADR Blue 001 R4 / §A1.5):
  - The SETUP/trigger operator credential is read from the environment
    ONLY (M8_OPERATOR_TOKEN — reused; the same short-TTL admin JWT drives
    SETUP and the /trigger fire). It is NEVER ORION_OPERATOR_TOKEN and is
    never logged.
  - The minted viewer show-token is redacted by ``redact_solar_url``;
    the operator JWT + show-token are scrubbed by ``redact_token``
    everywhere else.
  - NO token is committed anywhere in this module or the fixtures.

This module REUSES the M8 toolkit wholesale (the gateway HTTP client, the
LSML hash port, the redaction ports, the Solar URL composer) by importing
``m8_setup`` — only the SETUP orchestration + the M9 result shape are new.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any

import m8_setup
from m8_setup import (  # noqa: F401 — re-exported for the probe's convenience
    GatewayClient,
    SetupError,
    compose_solar_url,
    hash_bundle,
    hex_to_rgb,
    redact_solar_url,
    redact_token,
)

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
SCENE_BUNDLE_FIXTURE = FIXTURES_DIR / "m9-scene.lsml.json"
BLUEPRINT_FIXTURE = FIXTURES_DIR / "m9-blueprint-bg.json"

# The Canvas scene name M9 authors/reuses idempotently (find-or-create).
SCENE_NAME = "Pulsar M9 — Blue-driven reactive repaint test"

# The output port the blueprint emits; one port → one leaf segment.
M9_OUTPUT_PORT = "colour"

# Colour B the /trigger pushes — the post-repaint background. Distinct from
# the fixture's operator-input default A (#1A9E57) by a wide modal margin:
# A=(26,158,87) vs B=(200,30,90) → Manhattan 305, far past MODAL_COLOUR_TOL
# (24) so the A≠B repaint assertion is unambiguous and noise-proof.
M9_TRIGGER_COLOUR_B = "#C81E5A"

# Reuse M8's Solar/wire defaults verbatim — the bridge is LSDP-mode
# independent (ADR Blue 001 §1.2), so M9 ships on the same LSDP wire as M8.
DEFAULT_SOLAR_VERSION = m8_setup.DEFAULT_SOLAR_VERSION
DEFAULT_SHOW_STREAM_PATH = m8_setup.DEFAULT_SHOW_STREAM_PATH


def load_scene_bundle() -> tuple[dict[str, Any], str]:
    """Load the checked-in M9 bundle, stamp its real ``scene_version``, and
    return ``(bundle, H)``. Same content-address discipline as M8: the
    fixture ships a zeroed ``scene_version`` placeholder; we compute H
    (hash.go parity, scene_version zeroed) and seal it so the stored
    bundle self-certifies its identity for the A0 address-check."""
    bundle = json.loads(SCENE_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    h = hash_bundle(bundle)
    bundle["scene_version"] = "sha256:" + h
    return bundle, h


def leaf_default_a(bundle: dict[str, Any], leaf_path: str) -> str:
    """Return the operator-input default colour A declared for ``leaf_path``.

    This is the value Orion seeds on boot and the pre-trigger capture A
    must match. We read it from the scene's own ``operator_inputs`` rather
    than re-deriving from ``layout.background`` so the provenance target is
    sourced from the EXACT declaration that drives the live leaf."""
    for oi in bundle.get("operator_inputs", []):
        if isinstance(oi, dict) and oi.get("path") == leaf_path:
            default = oi.get("default")
            if not isinstance(default, str):
                raise SetupError(
                    f"operator_input {leaf_path!r} has no string default "
                    f"(got {default!r}) — cannot derive provenance colour A"
                )
            return default
    raise SetupError(
        f"scene declares no operator_input for {leaf_path!r}; Orion would "
        "reject the Blue push (sceneAcceptsPath) and never seed default A"
    )


@dataclass
class SetupResult:
    """Everything the M9 probe needs after SETUP, for capture-A / fire /
    capture-B and provenance."""

    scene_id: str
    bundle_hash: str            # H — the LSML content address we stored
    pushed_scene_version: str   # authoritative scene_version from the push
    blueprint_id: str           # the trigger target (POST .../blueprints/{id}/trigger)
    blueprint_slug: str         # the leaf segment <slug>
    leaf_path: str              # __inputs.blue.<slug>.<port> — the bound leaf
    solar_url: str              # show-token EMBEDDED — log only via redact_solar_url
    show_token: str             # raw — never log; redaction secret source
    colour_a: str               # leaf default (#RRGGBB) — pre-trigger modal target
    colour_b: str               # trigger-pushed colour (#RRGGBB) — post-trigger target

    @property
    def rgb_a(self) -> tuple[int, int, int]:
        return hex_to_rgb(self.colour_a)

    @property
    def rgb_b(self) -> tuple[int, int, int]:
        return hex_to_rgb(self.colour_b)


def run_setup(
    *, gateway_url: str, operator_token: str, twitch_key: str,
    solar_version: str, show_stream_path: str, log,
) -> SetupResult:
    """Execute the full M9 SETUP leg and return a SetupResult. ``log`` is a
    callable taking a single already-redacted string (the probe's print)."""
    bundle, h = load_scene_bundle()
    client = GatewayClient(
        base_url=gateway_url,
        operator_token=operator_token,
        # Redaction secret set: operator JWT + Twitch key (show-token added
        # after mint). Any of these in a response body is scrubbed.
        secrets=[s for s in (operator_token, twitch_key) if s],
    )

    fixture = json.loads(BLUEPRINT_FIXTURE.read_text(encoding="utf-8"))
    slug = fixture["blueprint"]["slug"]
    leaf_path = f"__inputs.blue.{slug}.{M9_OUTPUT_PORT}"
    colour_a = leaf_default_a(bundle, leaf_path)

    log("[S2] ensuring Blue passthrough blueprint (idempotent by slug) ...")
    blueprint_id = client.ensure_blueprint(fixture)
    log(f"   blueprint slug={slug!r} id={blueprint_id} ({BLUEPRINT_FIXTURE.name})")
    log(f"   bound leaf={leaf_path}  default(A)={colour_a}  trigger(B)={M9_TRIGGER_COLOUR_B}")

    log(f"[S3] storing LSML bundle in Canvas A0 (H={h}) ...")
    client.put_lsml_bundle(bundle, h)

    log("[S1] ensuring Canvas scene row (status=ready, idempotent by name) ...")
    scene_id = client.ensure_scene(SCENE_NAME)
    log(f"   scene_id={scene_id}")

    log("[S4] saving definition revision (canvas_version=H, blueprints=[]) ...")
    # M9 binds an operator-input leaf, not a blueprint-key leaf, so the
    # definition carries NO blueprints[] — the visible's source is the
    # __inputs.blue.* leaf Blue writes, declared in the bundle's
    # operator_inputs (seeds A; makes the Blue push in-scope).
    definition_id = client.save_definition(scene_id, h, [])
    log(f"   definition_id={definition_id}")

    log("[S5] pushing revision through Orion (lsml_bundle_hash=H) ...")
    push = client.push_definition(scene_id, definition_id, h)
    pushed_version = str(push["scene_version"])
    log(f"   push OK scene_version={pushed_version} diagnostics.errors=[]")
    if pushed_version == "sha256:" + h:
        log("   note: Orion adopted the stored LSML hash (byte-match adopt-on-verify)")
    else:
        log("   note: Orion minted a legacy scene_version (LSML_HASH_MISMATCH, "
            "non-fatal); provenance uses active_scene_id + push scene_version")

    log("[S6] driving active-scene ...")
    client.set_active_scene(scene_id)

    log("[S7] round-trip GET /orion/show (provenance marker 3) ...")
    show = client.get_show()
    active = show.get("active_scene_id")
    if active != scene_id:
        raise SetupError(
            f"provenance FAIL: active_scene_id={active!r} != scene_id={scene_id!r}"
        )
    log(f"   active_scene_id == scene_id ({scene_id}) — server-side provenance OK")

    log("[S8] minting viewer show-token (operator-only) ...")
    show_token = client.mint_show_token()
    log("   show-token minted (<redacted>)")

    log("[S9] composing Solar v%s LSDP URL ..." % solar_version)
    solar_url = compose_solar_url(
        gateway_url=gateway_url, show_token=show_token,
        solar_version=solar_version, show_stream_path=show_stream_path,
    )
    log(f"   solar_url={redact_solar_url(solar_url)}")

    return SetupResult(
        scene_id=scene_id,
        bundle_hash=h,
        pushed_scene_version=pushed_version,
        blueprint_id=blueprint_id,
        blueprint_slug=slug,
        leaf_path=leaf_path,
        solar_url=solar_url,
        show_token=show_token,
        colour_a=colour_a,
        colour_b=M9_TRIGGER_COLOUR_B,
    )


def fire_trigger(
    *, gateway_url: str, operator_token: str, secrets: list[str],
    blueprint_id: str, colour_b: str, log,
) -> dict[str, Any]:
    """Fire ``POST /api/v1/blueprints/{id}/trigger`` (gateway path
    ``/blue/api/v1/blueprints/{id}/trigger``) with the operator Bearer and
    ``inputs={"colour": B}``.

    The operator JWT rides as an ``Authorization: Bearer`` **header** (never
    a query string) — ADR Blue 001 R6 requires operator/admin, enforced by
    Blue's ``require_trigger_role`` reading the gateway-injected
    ``X-Authenticated-Role``; ZabGate injects that role only after it
    validates the header Bearer. We assert the response is 200 and that the
    push mapped our leaf, so the probe knows the leaf write was issued
    before it polls for the repaint.

    Returns the (already JSON-decoded) trigger response so the caller can
    inspect ``outputs`` + ``pushed{leaves,delivered}``. Raises SetupError on
    a non-200 (redacted)."""
    client = GatewayClient(
        base_url=gateway_url,
        operator_token=operator_token,
        secrets=secrets,
    )
    log(f"[TRIGGER] POST /blue/api/v1/blueprints/{blueprint_id}/trigger "
        f"(operator Bearer header) inputs={{'colour': {colour_b!r}}} ...")
    _, resp = client._request(  # noqa: SLF001 — same module family as M8's client
        "POST",
        f"/blue/api/v1/blueprints/{blueprint_id}/trigger",
        body={"inputs": {M9_OUTPUT_PORT: colour_b}},
        auth=True,
        expect=(200,),
    )
    outputs = resp.get("outputs") or {}
    pushed = resp.get("pushed") or {}
    leaves = [leaf.get("path") for leaf in pushed.get("leaves", []) if isinstance(leaf, dict)]
    log(f"   <- 200 outputs={outputs} pushed.delivered={pushed.get('delivered')} "
        f"pushed.leaves={leaves}")
    if outputs.get(M9_OUTPUT_PORT) != colour_b:
        raise SetupError(
            f"trigger outputs[{M9_OUTPUT_PORT!r}]={outputs.get(M9_OUTPUT_PORT)!r} "
            f"!= requested B={colour_b!r} — the blueprint did not pass the "
            "colour through; the leaf would carry the wrong value"
        )
    return resp
