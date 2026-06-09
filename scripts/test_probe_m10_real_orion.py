#!/usr/bin/env python3
"""Offline tests for the M10 live probe's ``--real-orion`` true-wire mode
(``probe-m10-canvas-live.py``, #79).

These cover everything provable **without a live ``pulsar.exe`` and without the
VPS**: the URL the CEF browser_source loads in ``--real-orion`` mode points at
the REAL VPS Solar bundle wired to the REAL Orion ``/show/stream``, and the
viewer show-token it carries is redacted in logs (C-SEC).

The on-air leg — the real CEF subscribing to the real Orion, receiving the real
M10 scene, and replaying the overlay off the real Blue-VPS leaf — is Keeper's /
Eleven's antenna run; it is out of scope here.

Run:
    pytest scripts/test_probe_m10_real_orion.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import urllib.parse as up

_HERE = pathlib.Path(__file__).resolve().parent


def _load_probe():
    """Load the hyphenated probe module by path (mirrors test_m10_setup's
    importlib pattern). Importing runs only module-level code — never main()."""
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    spec = importlib.util.spec_from_file_location(
        "probe_m10_canvas_live", _HERE / "probe-m10-canvas-live.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = _load_probe()


def test_real_orion_url_loads_the_vps_solar_bundle():
    """--real-orion: the browser_source loads the REAL Solar bundle Orion
    static-serves at /orion/static/solar/v{N}/index.html — NOT a local page."""
    url = probe.build_real_orion_overlay_url(
        gateway_url="https://zabgate.cyell.dev",
        show_token="viewer-token-xyz",
        solar_version="v0.2.2",
    )
    parsed = up.urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "zabgate.cyell.dev"
    assert parsed.path == "/orion/static/solar/v0.2.2/index.html"


def test_real_orion_url_points_mount_at_the_real_orion_show_stream():
    """The bundle's mount() reads ``orion=`` as its orionUrl. In true-wire mode
    that MUST be the REAL Orion /show/stream.lsdp over the gateway (wss), so the
    CEF receives the real M10 scene + the real Blue-VPS leaf delta — not a
    loopback stand-in. ``mode`` is broadcast exactly as on the loopback path.

    Mirrors Prism's getSolarSceneUrl contract test
    (Prism/src/main/broadcast-url.test.ts:53-59): orion= decodes to the wss LSDP
    endpoint carrying the viewer show-token as ITS OWN query."""
    url = probe.build_real_orion_overlay_url(
        gateway_url="https://zabgate.cyell.dev", show_token="t", solar_version="v0.2.2")
    parsed = up.urlparse(url)
    q = up.parse_qs(parsed.query)
    assert q["mode"] == ["broadcast"]
    # orion= is the gateway WS show-stream — the .lsdp endpoint (decoded; parse_qs
    # unquotes the outer param), with the token nested IN its own query.
    orion = up.urlparse(q["orion"][0])
    assert orion.scheme == "wss"
    assert orion.netloc == "zabgate.cyell.dev"
    assert orion.path == "/orion/api/v1/show/stream.lsdp"
    assert up.parse_qs(orion.query)["token"] == ["t"]


def test_real_orion_url_nests_token_inside_orion_param_not_top_level():
    """BUG FIX (#79): the viewer show-token MUST ride inside the ``orion=`` WS
    URL's own query (so ZabGate's ``?token=`` gate is satisfied at WS upgrade),
    and there must be NO top-level ``&token=`` (the runtime opens the bare orion=
    URL; a top-level token never reaches the handshake → 403 loop).

    Mirrors Prism/src/main/broadcast-url.test.ts:53-59,83 — the nested-token
    contract getSolarSceneUrl enforces."""
    url = probe.build_real_orion_overlay_url(
        gateway_url="https://zabgate.cyell.dev", show_token="show-secret",
        solar_version="v0.2.2")
    host_q = up.parse_qs(up.urlparse(url).query)
    # No top-level token on the host URL — the only token lives inside orion=.
    assert "token" not in host_q
    # The orion= param (url-decoded) carries token=<show> on a .lsdp endpoint.
    orion = up.urlparse(host_q["orion"][0])
    assert orion.path == "/orion/api/v1/show/stream.lsdp"
    assert up.parse_qs(orion.query)["token"] == ["show-secret"]


def test_real_orion_url_encodes_token_with_reserved_chars():
    """A token with reserved chars (&, =, ?, /) must be url-encoded so the nested
    query survives re-parsing — mirrors broadcast-url.test.ts:74-81."""
    url = probe.build_real_orion_overlay_url(
        gateway_url="https://zabgate.cyell.dev", show_token="a&b=c?d/e",
        solar_version="v0.2.2")
    orion = up.urlparse(up.parse_qs(up.urlparse(url).query)["orion"][0])
    assert up.parse_qs(orion.query)["token"] == ["a&b=c?d/e"]


def test_real_orion_url_http_gateway_yields_ws_orion_scheme():
    """A plain-http gateway (a local dev gateway) maps orion= to ws:// so
    mount()'s deriveBaseUrl resolves the bundle fetch back to the same origin —
    mirrors broadcast-url.test.ts:62-72 (http → ws)."""
    url = probe.build_real_orion_overlay_url(
        gateway_url="http://127.0.0.1:8099", show_token="t")
    orion = up.urlparse(up.parse_qs(up.urlparse(url).query)["orion"][0])
    assert orion.scheme == "ws"
    assert orion.netloc == "127.0.0.1:8099"
    assert orion.path == "/orion/api/v1/show/stream.lsdp"


def test_real_orion_url_normalises_a_trailing_slash_gateway():
    """A trailing-slash gateway must not yield a double slash in the path."""
    url = probe.build_real_orion_overlay_url(
        gateway_url="https://zabgate.cyell.dev/", show_token="t",
        solar_version="v0.2.2")
    # No `//orion` after the scheme's `://`.
    assert "/orion/static/solar/v0.2.2/index.html" in url
    assert url.count("://") == 1
    assert "dev//orion" not in url


def test_real_orion_show_token_is_redacted_in_logs():
    """C-SEC: the overlay URL is logged (it is built + logged in main() before
    deliver_leaf runs), so the embedded viewer token MUST be scrubbed by the
    same redactor the loopback show-stream URL uses."""
    url = probe.build_real_orion_overlay_url(
        gateway_url="https://zabgate.cyell.dev", show_token="SUPER-SECRET-VIEWER",
        solar_version="v0.2.2")
    redacted = probe.redact_show_stream_url(url)
    assert "SUPER-SECRET-VIEWER" not in redacted
    # The token now rides INSIDE the url-encoded ``orion=`` param, so it is the
    # percent-encoded ``token%3D…`` form the redactor scrubs (mirrors Prism's
    # redactSolarUrl, which targets the nested encoded token).
    assert "token%3D<redacted>" in redacted


def test_solar_vps_version_is_pinned_and_overridable():
    """The static Solar bundle version is pinned (tracks the M10 antenna
    bundle) and overridable via SOLAR_VPS_VERSION so a Solar release does not
    require a probe edit."""
    assert probe.SOLAR_VPS_VERSION  # non-empty default
    # The default builder uses the module constant when no version is passed.
    url = probe.build_real_orion_overlay_url(
        gateway_url="https://zabgate.cyell.dev", show_token="t")
    assert f"/orion/static/solar/{probe.SOLAR_VPS_VERSION}/index.html" in url


def test_real_orion_is_a_distinct_delivery_mode():
    """--real-orion is its own delivery const, distinct from --loopback-leaf
    and --live-wire — so the standin-vs-VPS branch in main() is unambiguous."""
    # The three delivery modes the probe knows.
    src = (_HERE / "probe-m10-canvas-live.py").read_text(encoding="utf-8")
    assert 'const="real-orion"' in src
    assert 'const="live-wire"' in src
    assert 'const="loopback-leaf"' in src
