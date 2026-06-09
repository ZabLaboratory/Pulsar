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
    that MUST be the REAL Orion /show/stream over the gateway (wss), so the CEF
    receives the real M10 scene + the real Blue-VPS leaf delta — not a loopback
    stand-in. ``mode`` is broadcast exactly as on the loopback path."""
    url = probe.build_real_orion_overlay_url(
        gateway_url="https://zabgate.cyell.dev", show_token="t", solar_version="v0.2.2")
    q = up.parse_qs(up.urlparse(url).query)
    assert q["mode"] == ["broadcast"]
    # orion= is the gateway WS show/stream (decoded — parse_qs unquotes it).
    assert q["orion"] == ["wss://zabgate.cyell.dev/orion/api/v1/show/stream"]


def test_real_orion_url_carries_the_viewer_show_token():
    """The viewer show-token authenticates the read-only subscription. It is a
    VIEWER token (the runtime never sends input on it)."""
    url = probe.build_real_orion_overlay_url(
        gateway_url="https://zabgate.cyell.dev", show_token="viewer-abc123",
        solar_version="v0.2.2")
    q = up.parse_qs(up.urlparse(url).query)
    assert q["token"] == ["viewer-abc123"]


def test_real_orion_url_http_gateway_yields_ws_orion_scheme():
    """A plain-http gateway (a local dev gateway) maps orion= to ws:// so
    mount()'s deriveBaseUrl resolves the bundle fetch back to the same origin."""
    url = probe.build_real_orion_overlay_url(
        gateway_url="http://127.0.0.1:8099", show_token="t")
    q = up.parse_qs(up.urlparse(url).query)
    assert q["orion"] == ["ws://127.0.0.1:8099/orion/api/v1/show/stream"]


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
    assert "token=<redacted>" in redacted


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
