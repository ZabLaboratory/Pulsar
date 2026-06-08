#!/usr/bin/env python3
"""M8 SETUP leg — author a Canvas+Blue scene, push it through Orion, make
it the active show, mint a viewer show-token, and compose the Solar URL.

This is the *authoring + activation prologue* the M8 probe runs BEFORE the
(reused) M6 pre-flight + broadcast core (ADR Pulsar-002 §3.3/§3.4). The
probe itself is a thin WS-to-Pulsar client; everything in this module is
gateway-first HTTP against the deployed (or local-compose) stack.

What it does, in order (ADR Pulsar-002 §3.3, §A1):

  S2. Ensure the N (>=2) Blue blueprints exist + are published, idempotent
      by slug (POST /blue/api/v1/blueprints, POST .../versions, .../publish).
      Each is a pure compute graph (core.literal/core.math.add/core.output)
      so Orion's compiler accepts it by construction — no IMPURE_COMPUTE.
  S3. PUT the deterministic, checked-in LSML scene bundle into Canvas's A0
      content-addressed store (POST-equivalent PUT /canvas/api/v1/lsml-bundles/{H}).
      H is the bundle's own scene_version, computed here the SAME way
      lumencast-go/lsml.HashBundle computes it (sorted keys, no whitespace,
      no HTML escaping, scene_version zeroed) so the store address-check
      passes and /layouts/{H} serves the layout to Orion's compiler.
  S1. Ensure the test scene row exists in Canvas (status=ready), idempotent
      by name (POST /canvas/api/v1/scenes), capture scene_id.
  S4. Save a definition revision carrying canvas_version=H + blueprints[]
      ({key,id} for each blueprint) (POST /canvas/.../scenes/{id}/save).
  S5. Push that revision through Orion (POST /canvas/.../scenes/{id}/push,
      definition_id + lsml_bundle_hash=H). Assert 200, capture scene_version.
  S6. Drive the active-scene (POST /orion/api/v1/show/active-scene {scene_id}).
      M8 is the FIRST real driver of this endpoint (ADR §1.1 active-scene gap).
  S7. Round-trip GET /orion/api/v1/show — assert active_scene_id == scene_id
      (provenance marker 3, server-side, deterministic).
  S8. Mint a viewer show-token (POST /auth/api/v1/show-tokens, operator-only).
  S9. Compose the Solar v0.2.0 LSDP URL (getSolarSceneUrl parity).

SECRET HYGIENE (Bastion PV-1 / CC-1, ADR §A1.5):
  - The SETUP operator credential is read from the environment ONLY
    (M8_OPERATOR_TOKEN, sourced from the étage-1 secret file). It is a
    short-TTL admin token and is NEVER `ORION_OPERATOR_TOKEN` (the
    long-lived exp-2027 service token). It is never logged.
  - The minted viewer show-token is redacted by `redact_solar_url`
    (a Python port of Prism's broadcast-url.ts::redactSolarUrl) in every
    line that could carry it, and by `redact_token` everywhere else.
  - NO token is committed anywhere in this module or the fixtures.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
SCENE_BUNDLE_FIXTURE = FIXTURES_DIR / "m8-scene.lsml.json"
BLUEPRINT_FIXTURES = [
    ("score", FIXTURES_DIR / "m8-blueprint-score.json"),
    ("timer", FIXTURES_DIR / "m8-blueprint-timer.json"),
]

# The Canvas scene name M8 authors/reuses idempotently. A fixed name keeps
# the SETUP idempotent across runs (find-or-create by name).
SCENE_NAME = "Pulsar M8 — Canvas-authored live test"

# Solar bundle version the M8 probe composes its URL against (ADR §A1.2:
# the LSDP wire ships as @zablab/solar@0.2.0, served at /static/solar/v0.2.0/).
DEFAULT_SOLAR_VERSION = "0.2.0"
# Default wire path: the LSDP wire (ADR §A1.1 maximal path). The bespoke
# `stream` value stays a supported fallback (§6.6).
DEFAULT_SHOW_STREAM_PATH = "stream.lsdp"


# --------------------------------------------------------------------------
# Secret redaction — PORT of Prism/src/main/broadcast-url.ts::redactSolarUrl.
# The show-token lives url-encoded inside the ?orion= param (token%3D<...>)
# and/or as a plain ?token=<...>; strip both so a log line can show the
# page + version without leaking the credential.
# --------------------------------------------------------------------------
_TOKEN_ENC_RE = re.compile(r"token%3D[^%&]+", re.IGNORECASE)
_TOKEN_PLAIN_RE = re.compile(r"([?&]token=)[^&]+", re.IGNORECASE)


def redact_solar_url(url: str) -> str:
    """Redact the show-token out of a Solar URL for safe logging.

    Mirrors broadcast-url.ts::redactSolarUrl byte-for-byte: the nested
    url-encoded ``token%3D<...>`` (up to the next ``%26``/``&`` or EOS) and
    the plain ``?token=<...>`` form are both replaced with ``<redacted>``.
    """
    out = _TOKEN_ENC_RE.sub("token%3D<redacted>", url)
    out = _TOKEN_PLAIN_RE.sub(r"\1<redacted>", out)
    return out


def redact_token(text: str, *secrets: str) -> str:
    """Replace any non-empty secret substring with ``<redacted>``.

    Belt-and-braces alongside ``redact_solar_url``: the raw show-token, the
    operator JWT, and the Twitch key are scrubbed from any free-form line
    (exception messages, response bodies) where they could otherwise leak.
    """
    out = text
    for sec in secrets:
        if sec and sec in out:
            out = out.replace(sec, "<redacted>")
    return out


# --------------------------------------------------------------------------
# Canonical LSML hash — PORT of lumencast-go/lsml/hash.go::HashBundle.
# We never let the probe INVENT a hash: it computes the bundle's own
# scene_version exactly as Go (and @lumencast/compiler) do, so the Canvas
# A0 store's address-check (bundle.scene_version hex == {hash} path) passes
# and /layouts/{H} resolves. The same discipline that keeps Go and TS in
# byte-agreement (sorted keys, no whitespace, no HTML escaping, scene_version
# zeroed) is reproduced here.
# --------------------------------------------------------------------------
_ZERO_HASH = "sha256:" + "0" * 64


def _canonical(value: Any) -> str:
    """Emit canonical JSON for ``value`` — sorted keys at every level, no
    insignificant whitespace, and NO HTML escaping of ``&<>`` (hash.go
    marshalString disables Go's SetEscapeHTML; Python's json never escapes
    those, so the default already matches). Numbers use the shortest form
    json.dumps already produces for ints/floats.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def hash_bundle(bundle: dict[str, Any]) -> str:
    """Return the bare lowercase-hex sha256 of the canonicalised bundle,
    with ``scene_version`` replaced by the zero placeholder (hash.go rules).
    This is the store address {H} and the bundle's own scene_version hex.
    """
    work = dict(bundle)
    if "scene_version" in work:
        work["scene_version"] = _ZERO_HASH
    canon = _canonical(work).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


def load_scene_bundle() -> tuple[dict[str, Any], str]:
    """Load the checked-in scene bundle, stamp its real scene_version, and
    return ``(bundle, H)``. The fixture ships with a zeroed scene_version
    placeholder; we compute H and seal it so the stored bundle
    self-certifies its identity (lsml_bundle_service.bundle_hash).
    """
    bundle = json.loads(SCENE_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    h = hash_bundle(bundle)
    bundle["scene_version"] = "sha256:" + h
    return bundle, h


# --------------------------------------------------------------------------
# Minimal gateway-first HTTP client (stdlib only — no extra probe deps).
# All calls go through ZabGate; the operator JWT rides as a Bearer header
# (the gateway validates it + injects X-Authenticated-Role for Orion's
# requireOperator / ZabAuth require_operator).
# --------------------------------------------------------------------------
class SetupError(RuntimeError):
    """A SETUP-leg failure with a redacted, self-explanatory message."""


@dataclass
class GatewayClient:
    base_url: str           # e.g. http://127.0.0.1:8099  (tunnel'd gateway)
    operator_token: str     # admin/operator JWT (étage-1, never logged)
    secrets: list[str] = field(default_factory=list)
    timeout: float = 30.0

    def _request(
        self, method: str, path: str, *, body: Optional[dict] = None,
        auth: bool = True, expect: tuple[int, ...] = (200, 201),
    ) -> tuple[int, dict]:
        url = self.base_url.rstrip("/") + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth and self.operator_token:
            req.add_header("Authorization", f"Bearer {self.operator_token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise SetupError(
                redact_token(f"{method} {path} unreachable: {exc}", *self.secrets)
            ) from None
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"_raw": raw}
        if status not in expect:
            safe = redact_token(raw[:400], *self.secrets)
            raise SetupError(
                f"{method} {path} -> {status} (want {expect}): {safe}"
            )
        return status, payload if isinstance(payload, dict) else {"_list": payload}

    # -- Blue ------------------------------------------------------------
    def find_blueprint_by_slug(self, slug: str) -> Optional[dict]:
        _, payload = self._request("GET", "/blue/api/v1/blueprints", expect=(200,))
        items = payload.get("_list", payload)
        if isinstance(items, list):
            for bp in items:
                if isinstance(bp, dict) and bp.get("slug") == slug:
                    return bp
        return None

    def ensure_blueprint(self, fixture: dict) -> str:
        """Idempotently create + publish one Blue blueprint from a fixture;
        return its id. Re-runs are no-ops (find-by-slug short-circuits)."""
        spec = fixture["blueprint"]
        slug = spec["slug"]
        existing = self.find_blueprint_by_slug(slug)
        if existing is not None and int(existing.get("current_version", 0)) >= 1:
            return str(existing["id"])

        if existing is None:
            _, bp = self._request("POST", "/blue/api/v1/blueprints", body={
                "slug": slug,
                "name": spec["name"],
                "kind": spec["kind"],
                "tags": spec.get("tags", []),
                "interface": spec.get("interface", {}),
            }, expect=(201,))
        else:
            bp = existing
        bp_id = str(bp["id"])

        # Create a draft version carrying the compute graph, then publish so
        # current_version advances to 1 (Orion's FetchBlueprint reads
        # GET /versions/{current_version}).
        _, ver = self._request(
            "POST", f"/blue/api/v1/blueprints/{bp_id}/versions",
            body={"graph": fixture["graph"], "interface": spec.get("interface", {})},
            expect=(201,),
        )
        version_n = int(ver["version"])
        self._request(
            "POST", f"/blue/api/v1/blueprints/{bp_id}/versions/{version_n}/publish",
            expect=(200,),
        )
        return bp_id

    # -- Canvas ----------------------------------------------------------
    def find_scene_by_name(self, name: str) -> Optional[dict]:
        _, payload = self._request(
            "GET",
            "/canvas/api/v1/scenes?q=" + urllib.parse.quote(name),
            expect=(200,),
        )
        items = payload.get("_list", payload)
        if isinstance(items, list):
            for sc in items:
                if isinstance(sc, dict) and sc.get("name") == name:
                    return sc
        return None

    def ensure_scene(self, name: str) -> str:
        existing = self.find_scene_by_name(name)
        if existing is not None:
            return str(existing["id"])
        _, sc = self._request("POST", "/canvas/api/v1/scenes", body={
            "name": name,
            "status": "ready",
        }, expect=(201,))
        return str(sc["id"])

    def put_lsml_bundle(self, bundle: dict, h: str) -> None:
        """Store the LSML bundle under its content address {H} (idempotent;
        200 on re-PUT, 201 first store). archive is the bundle JSON bytes —
        the store keeps it byte-faithfully but addresses by scene_version,
        not by sha256(archive)."""
        import base64
        archive_b64 = base64.b64encode(
            json.dumps(bundle).encode("utf-8")
        ).decode("ascii")
        self._request(
            "PUT", f"/canvas/api/v1/lsml-bundles/{h}",
            body={"bundle": bundle, "archive": archive_b64},
            expect=(200, 201),
        )

    def save_definition(
        self, scene_id: str, canvas_version: str, blueprints: list[dict],
    ) -> str:
        """Append a definition revision binding the N blueprints (blueprints[]
        = [{key,id}, ...]) at canvas_version=H. Returns the definition id."""
        _, rev = self._request(
            "POST", f"/canvas/api/v1/scenes/{scene_id}/save",
            body={
                "canvas_version": canvas_version,
                "blueprints": blueprints,
                "components": [],
            },
            expect=(200, 201),
        )
        return str(rev["id"])

    def push_definition(
        self, scene_id: str, definition_id: str, lsml_bundle_hash: str,
    ) -> dict:
        """Push the revision through Orion. Returns the push response
        {scene_version, diagnostics{errors,warnings}}. Raises on
        non-200 or non-empty diagnostics.errors."""
        _, resp = self._request(
            "POST", f"/canvas/api/v1/scenes/{scene_id}/push",
            body={"definition_id": definition_id, "lsml_bundle_hash": lsml_bundle_hash},
            expect=(200,),
        )
        diags = resp.get("diagnostics") or {}
        errors = diags.get("errors") or []
        if errors:
            raise SetupError(
                f"push diagnostics.errors non-empty: {json.dumps(errors)[:400]}"
            )
        return resp

    # -- Orion show ------------------------------------------------------
    def set_active_scene(self, scene_id: str) -> None:
        self._request(
            "POST", "/orion/api/v1/show/active-scene",
            body={"scene_id": scene_id}, expect=(200,),
        )

    def get_show(self) -> dict:
        # Operator-gated on the public gateway: GET /orion/api/v1/show is 401
        # without a Bearer, 200 with the operator Bearer. Ride the operator
        # JWT (auth=True) like every other SETUP leg.
        _, show = self._request("GET", "/orion/api/v1/show", auth=True, expect=(200,))
        return show

    # -- ZabAuth ---------------------------------------------------------
    def mint_show_token(self, ttl_s: int = 14400) -> str:
        _, tok = self._request(
            "POST", "/auth/api/v1/show-tokens",
            body={"ttl_s": ttl_s}, expect=(201,),
        )
        access = tok.get("access_token")
        if not isinstance(access, str) or not access:
            raise SetupError("show-token mint returned no access_token")
        return access


# --------------------------------------------------------------------------
# Solar URL composition — PORT of broadcast-url.ts::getSolarSceneUrl, with
# the wire path parameterised (stream.lsdp default, stream bespoke fallback).
# --------------------------------------------------------------------------
def gateway_to_ws_origin(gateway_url: str) -> str:
    u = urllib.parse.urlparse(gateway_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    return f"{scheme}://{u.netloc}"


def compose_solar_url(
    *, gateway_url: str, show_token: str, solar_version: str, show_stream_path: str,
) -> str:
    """Compose the Solar live-page URL Pulsar's CEF browser_source loads.

    Shape (broadcast-url.ts::getSolarSceneUrl, wire-path parameterised):
      <gate>/orion/static/solar/v{N}/index.html
        ?orion=<url-encoded ws(s)://<gate>/orion/api/v1/show/{path}?token=<show>>
        &mode=broadcast

    The inner ``?token=<show>`` is url-encoded as the value of the outer
    ``?orion=`` param so the nested query survives verbatim. The show is
    selected by Orion's active-scene state (driven in S6) — Solar is told
    WHICH server to talk to, the server decides the scene.
    """
    ws_origin = gateway_to_ws_origin(gateway_url)
    lsdp_url = (
        f"{ws_origin}/orion/api/v1/show/{show_stream_path}"
        f"?token={urllib.parse.quote(show_token, safe='')}"
    )
    http = urllib.parse.urlparse(gateway_url)
    page_origin = f"{http.scheme}://{http.netloc}"
    solar_page = f"{page_origin}/orion/static/solar/v{solar_version}/index.html"
    return (
        f"{solar_page}?orion={urllib.parse.quote(lsdp_url, safe='')}"
        f"&mode=broadcast"
    )


@dataclass
class SetupResult:
    """Everything the probe needs after SETUP, for pre-flight + provenance."""

    scene_id: str
    bundle_hash: str            # H — the LSML content address we stored
    pushed_scene_version: str   # authoritative scene_version from the push
    blueprint_ids: dict[str, str]
    solar_url: str              # show-token EMBEDDED — log only via redact_solar_url
    show_token: str             # raw — never log; redaction secret source
    test_background: str        # the known unusual bg colour (provenance marker 1)


def background_rgb(bundle: dict[str, Any]) -> tuple[int, int, int]:
    """Extract the frame background colour from the scene bundle as an RGB
    triple — the modal-colour the pre-flight ties the on-air pixels to."""
    bg = bundle.get("layout", {}).get("background", "")
    return hex_to_rgb(bg)


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    s = value.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise SetupError(f"background colour {value!r} is not a #RRGGBB hex")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def run_setup(
    *, gateway_url: str, operator_token: str, twitch_key: str,
    solar_version: str, show_stream_path: str, log,
) -> SetupResult:
    """Execute the full SETUP leg and return a SetupResult. ``log`` is a
    callable taking a single already-redacted string (the probe's print)."""
    bundle, h = load_scene_bundle()
    bg_rgb = background_rgb(bundle)
    client = GatewayClient(
        base_url=gateway_url,
        operator_token=operator_token,
        # Redaction secret set: operator JWT + Twitch key (show-token added
        # after mint). Any of these in a response body is scrubbed.
        secrets=[s for s in (operator_token, twitch_key) if s],
    )

    log("[S2] ensuring Blue blueprints (idempotent by slug) ...")
    blueprint_ids: dict[str, str] = {}
    blueprints_wire: list[dict] = []
    for key, fixture_path in BLUEPRINT_FIXTURES:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        bp_id = client.ensure_blueprint(fixture)
        blueprint_ids[key] = bp_id
        blueprints_wire.append({"key": key, "id": bp_id})
        log(f"   blueprint key={key!r} id={bp_id} ({fixture_path.name})")
    if len({b["id"] for b in blueprints_wire}) < 2:
        raise SetupError("fixtures did not yield >=2 DISTINCT blueprint ids")
    if len({b["key"] for b in blueprints_wire}) != len(blueprints_wire):
        raise SetupError("blueprint keys are not unique")

    log(f"[S3] storing LSML bundle in Canvas A0 (H={h}) ...")
    client.put_lsml_bundle(bundle, h)

    log("[S1] ensuring Canvas scene row (status=ready, idempotent by name) ...")
    scene_id = client.ensure_scene(SCENE_NAME)
    log(f"   scene_id={scene_id}")

    log("[S4] saving definition revision (canvas_version=H, blueprints[]) ...")
    definition_id = client.save_definition(scene_id, h, blueprints_wire)
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

    log("[S6] driving active-scene (M8 is the first real driver) ...")
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
        blueprint_ids=blueprint_ids,
        solar_url=solar_url,
        show_token=show_token,
        test_background="#%02X%02X%02X" % bg_rgb,
    )
