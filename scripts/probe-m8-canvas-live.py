#!/usr/bin/env python3
"""Pulsar M8 live probe — a Canvas+Blue scene THIS test authored, on air to
Twitch, over the LSDP wire, with provenance (ADR Pulsar-002, ADR Orion-001).

M8 is M6 (the real Solar page in Pulsar's CEF → Twitch, with a non-blank
pre-flight) but the on-air scene is **authored by M8 itself**: a multi-
blueprint (N>=2) rich Canvas layout + Blue logic, pushed through the real
Orion compile, made the active show, and *proven* to be the scene on screen
— not whatever was ambient behind a tunnel, not a fallback.

Two things make M8 strictly stronger than M6:

  1. AUTHORING. The SETUP leg (scripts/m8_setup.py) creates the N Blue
     blueprints, stores the deterministic checked-in LSML scene, pushes it,
     drives the active-scene (M8 is the FIRST real driver of
     POST /orion/api/v1/show/active-scene), and mints a FRESH viewer
     show-token (no baked, expiring token in source — Bastion PV-1).

  2. PROVENANCE. The on-air pixels are tied to the server state two ways:
       (3) ROUND-TRIP: GET /orion/api/v1/show -> active_scene_id == scene_id
           (deterministic, server-side; done in SETUP S7), and the push
           response's scene_version is captured + reported.
       (1) MODAL-COLOUR: the captured CEF frame's modal colour ≈ the scene's
           known unusual background (#1A9E57) within tolerance — this binds
           the SERVER state to the PIXELS. A blank / wrong-colour / fallback
           frame fails it => NO GO (no broadcast).

Everything below the SETUP/provenance is the proven M6 broadcast core,
reused verbatim by importing probe-m6-live.py: CEF spawn/reap, pure-stdlib
PNG decode + analyse_frame, the RTMP metrics loop, secret redaction. The
bounded anti-boot-race StartDestination retry lives in that shared core
(probe-m6-live.py's start_destination_with_retry, ported from
probe-twitch-live.py's START_DEST_* pattern): m6.broadcast goes live through
it, so the M8 broadcast path retries the exact transient
'frontend streaming output unavailable' error within a bounded budget and
hard-fails on anything else.

SECRET HYGIENE (Bastion PV-1 / CC-1, ADR §A1.5 — load-bearing):
  - NO token committed anywhere (no DEFAULT_SOLAR_URL with a baked JWT, the
    M6 trap — see probe-m6-live.py:125).
  - The Twitch key, the operator JWT, and the minted show-token all come
    from the étage-1 environment.
  - The operator credential is M8_OPERATOR_TOKEN (admin, short-TTL) — NOT
    ORION_OPERATOR_TOKEN (the long-lived service token), CC-1.
  - Every line that emits solar_url is passed through redact_solar_url
    (the redactSolarUrl port); the Twitch key + JWT + show-token are
    scrubbed everywhere else; and a grep-assert in the run wrapper
    (run-m8.ps1 / the CI job) fails the run if any credential leaks to
    stdout / the proof PNG / the VOD.

Wire = LSDP by default (--show-stream-path stream.lsdp), Solar v0.2.0.

Usage (from the repo root, against the built -Full rundir):
    pip install websockets
    export M8_OPERATOR_TOKEN=...        # étage-1 admin JWT, short-TTL, NEVER committed
    export TWITCH_STREAM_KEY=...        # étage-1, NEVER committed (broadcast leg only)
    export M8_GATEWAY_URL=http://127.0.0.1:8099   # tunnel'd gateway base
    python scripts/probe-m8-canvas-live.py --preflight-only   # author+push+prove, no go-live
    python scripts/probe-m8-canvas-live.py                    # + broadcast

Exit codes (mirror M6):
  0  pass (provenance pre-flight confirmed; if not --preflight-only, live ok)
  1  fail (setup / provenance / broadcast assertion failed — NO blank go-live)
  2  config error (no operator token, no exe, no key for broadcast, bad args)
  3  typed skip (browser_source not registered — LIGHT build, needs -Full)
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import pathlib
import secrets
import socket
import sys
import time
from typing import Optional

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent

# Ensure the sibling m8_setup module is importable regardless of the CWD the
# probe is launched from (the run wrapper / CI invoke it from the repo root).
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Import the M6 broadcast core. probe-m6-live.py has a hyphen in its name
# (not a valid module identifier), so load it by path — we reuse its CEF
# spawn/reap, PNG decode, analyse_frame, broadcast loop and redaction
# WHOLESALE rather than fork 900 lines (ADR §3.5: "M8 imports the broadcast
# core; the non-blank predicate is EXTENDED with the modal-colour assertion").
_m6_path = SCRIPTS_DIR / "probe-m6-live.py"
_spec = importlib.util.spec_from_file_location("probe_m6_live", _m6_path)
assert _spec is not None and _spec.loader is not None
m6 = importlib.util.module_from_spec(_spec)
sys.modules["probe_m6_live"] = m6
_spec.loader.exec_module(m6)

import m8_setup  # noqa: E402  (after sys.path is the scripts dir by __file__)

try:
    import websockets  # noqa: F401  (used inside m6.run via the shared import)
except ImportError:
    print("error: pip install websockets (pure WS client — no native deps)")
    sys.exit(2)


BUILD_DIR = REPO_ROOT / "build"
PROOF_PNG = BUILD_DIR / "m8-canvas-scene.png"
LIVE_VOD_DIR = BUILD_DIR / "m8-canvas-vod"

# Modal-colour provenance tolerance (Manhattan distance in RGB). The CEF
# render + PNG re-encode shift colours slightly (anti-aliasing at edges,
# sub-pixel blending), but the dominant background is a large flat field —
# its modal colour stays within a tight band of the authored hex. A
# fallback / blank / different scene lands far outside.
MODAL_COLOUR_TOL = 24


def _provenance_modal_ok(modal: Optional[tuple[int, int, int]],
                         target: tuple[int, int, int]) -> tuple[bool, int]:
    """Return (ok, manhattan_distance) for the modal-colour provenance check."""
    if modal is None:
        return False, 1 << 30
    dist = abs(modal[0] - target[0]) + abs(modal[1] - target[1]) + abs(modal[2] - target[2])
    return dist <= MODAL_COLOUR_TOL, dist


async def preflight_provenance(inbox, ws, solar_url: str,
                               target_rgb: tuple[int, int, int],
                               show_token: str) -> int:
    """M6 non-blank pre-flight EXTENDED with the modal-colour provenance
    assertion (marker 1). Returns 0 only when the captured frame is both
    non-blank AND its modal colour ≈ the test background. Saves the proof
    PNG to build/m8-canvas-scene.png. A blank or wrong-colour frame => 1
    (NO GO), with a typed diagnosis. Reuses m6.preflight_non_blank's render
    + poll machinery by pointing its PROOF_PNG at the M8 path first.
    """
    # Point M6's module-level proof path + capture dirs at the M8 artefacts
    # so the reused core writes where the M8 wrapper/CI expect them.
    m6.PROOF_PNG = PROOF_PNG
    m6.BUILD_DIR = BUILD_DIR

    rc, metrics = await m6.preflight_non_blank(inbox, ws, solar_url)
    if rc != 0:
        # m6 already printed the non-blank diagnosis + saved the last frame.
        print("[M8] pre-flight FAILED at the non-blank stage — NOT going live. "
              "Diagnose: LSDP WS to the gateway failed (token/.lsdp gate?), "
              "Solar v0.2.0 bundle 404 at /static/solar/v0.2.0/, or the active "
              "scene never streamed. See the m6 diagnosis above + the saved PNG.")
        return 1

    # Non-blank confirmed; now the load-bearing provenance: modal ≈ target.
    modal = metrics.get("modal")
    ok, dist = _provenance_modal_ok(modal, target_rgb)
    print(f"[M8] provenance modal-colour check: captured modal={modal} "
          f"target={target_rgb} manhattan={dist} tol={MODAL_COLOUR_TOL}")
    if not ok:
        print("[M8] provenance FAILED — the frame is non-blank but its modal "
              "colour does NOT match the scene background this test authored "
              f"(#{target_rgb[0]:02X}{target_rgb[1]:02X}{target_rgb[2]:02X}). "
              "This is the fallback / wrong-scene trap: Pulsar rendered SOME "
              "Solar scene, but not the one M8 pushed + activated. NOT going "
              "live. Inspect the proof PNG: " + str(PROOF_PNG))
        return 1

    print("[M8] PROVENANCE PROVEN — non-blank AND modal colour matches the "
          "authored background; the on-air pixels are M8's pushed scene. "
          f"Proof PNG: {PROOF_PNG}")
    return 0


async def run(ws_url: str, password: str, setup: "m8_setup.SetupResult",
              stream_key: str, duration_sec: int, preflight_only: bool,
              pulsar) -> int:
    import json
    print(f"connecting: {ws_url}")
    async with websockets.connect(
        ws_url, subprotocols=["obswebsocket.json"], max_size=2**24,
        ping_interval=None, close_timeout=15, open_timeout=10,
    ) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if hello.get("op") != 0:
            print(f"error: expected Hello (op=0), got {hello}")
            return 1
        identify_d: dict = {
            "rpcVersion": hello["d"]["rpcVersion"],
            "eventSubscriptions": m6.EVENT_SUBSCRIPTION_ALL,
        }
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify_d["authentication"] = m6.compute_auth(
                password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ident.get("op") != 2:
            print(f"error: identify failed: {ident}")
            return 1
        print("identified (v5 auth OK)")

        inbox = m6.Inbox()

        resp = await m6.request(inbox, ws, "GetInputKindList", "kinds", {})
        kinds = set(resp["responseData"]["inputKinds"])
        if "browser_source" not in kinds:
            print("SKIP: browser_source NOT registered — LIGHT build (no CEF). "
                  "M8 needs a full build (scripts/build-win.ps1 -Full). "
                  "Typed skip, NOT a pass.")
            return 3
        print(f"browser_source registered ({len(kinds)} input kinds total)")

        target_rgb = m8_setup.hex_to_rgb(setup.test_background)
        rc = await preflight_provenance(
            inbox, ws, setup.solar_url, target_rgb, setup.show_token)
        if rc != 0:
            return 1

        if preflight_only:
            print("[M8] --preflight-only set: skipping broadcast.")
            return 0

        # Broadcast core reused verbatim from M6. m6.broadcast now goes live
        # through start_destination_with_retry (the bounded anti-boot-race
        # StartDestination retry ported from probe-twitch-live.py), so this
        # M8 path is robust to the post-scene-switch boot race observed in CI.
        print("\n[M8] going live to Twitch (provenance proven) ...")
        m6.LIVE_VOD_DIR = LIVE_VOD_DIR
        return await m6.broadcast(inbox, ws, stream_key, duration_sec, pulsar)


def pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pulsar M8 Canvas+Blue authored scene -> Solar LSDP -> Twitch probe")
    ap.add_argument("--exe", type=pathlib.Path,
                    default=pathlib.Path(os.environ.get("PULSAR_EXE", str(m6.DEFAULT_EXE))),
                    help="path to pulsar.exe (default: built rundir)")
    ap.add_argument("--gateway-url", type=str,
                    default=os.environ.get("M8_GATEWAY_URL", "http://127.0.0.1:8099"),
                    help="ZabGate base URL (default: the tunnel'd gateway). All "
                         "SETUP HTTP is gateway-first against this base.")
    ap.add_argument("--solar-version", type=str,
                    default=os.environ.get("M8_SOLAR_VERSION",
                                           m8_setup.DEFAULT_SOLAR_VERSION),
                    help="Solar bundle version served at /static/solar/v{N}/ "
                         "(default 0.2.0 — the LSDP wire).")
    ap.add_argument("--show-stream-path", type=str,
                    default=os.environ.get("M8_SHOW_STREAM_PATH",
                                           m8_setup.DEFAULT_SHOW_STREAM_PATH),
                    choices=["stream.lsdp", "stream"],
                    help="show-stream wire path: stream.lsdp (LSDP, default, "
                         "needs Orion in dual/lsdp mode) or stream (bespoke "
                         "fallback).")
    ap.add_argument("--duration", type=int,
                    default=int(os.environ.get("LIVE_TEST_DURATION", "30")),
                    help="broadcast duration in seconds (default 30)")
    ap.add_argument("--fps", type=int,
                    default=int(os.environ.get("LIVE_TEST_FPS", "60")),
                    help="encoder fps target (default 60)")
    ap.add_argument("--preflight-only", action="store_true",
                    help="author+push+activate+prove, but do NOT broadcast")
    ap.add_argument("--ready-timeout", type=float, default=m6.READY_TIMEOUT_S)
    args = ap.parse_args()

    exe: pathlib.Path = args.exe
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        print("Build it first: scripts/build-win.ps1 -Full")
        return 2

    # CC-1: the SETUP operator credential is a dedicated short-TTL admin
    # token from étage-1 — NOT ORION_OPERATOR_TOKEN (the long-lived service
    # mint). We read M8_OPERATOR_TOKEN ONLY and refuse to fall back.
    operator_token = os.environ.get("M8_OPERATOR_TOKEN", "").strip()
    if not operator_token:
        print("error: M8_OPERATOR_TOKEN env var is empty (required). It is the "
              "admin/operator JWT for the SETUP leg (save/push/active-scene/mint). "
              "Source it from the étage-1 secret as a SHORT-TTL admin token — do "
              "NOT reuse ORION_OPERATOR_TOKEN (Bastion CC-1). Never commit it.")
        return 2
    if operator_token == os.environ.get("ORION_OPERATOR_TOKEN", "").strip() and operator_token:
        print("error: M8_OPERATOR_TOKEN must NOT equal ORION_OPERATOR_TOKEN "
              "(the long-lived exp-2027 service token) — Bastion CC-1. Mint a "
              "dedicated short-TTL admin token for the test run.")
        return 2

    stream_key = os.environ.get("TWITCH_STREAM_KEY", "").strip()
    if not args.preflight_only and not stream_key:
        print("error: TWITCH_STREAM_KEY env var is empty (required unless "
              "--preflight-only). Set it from the étage-1 secret; never commit.")
        return 2

    print("=== M8 SETUP leg (gateway-first authoring + activation) ===")
    print(f"  gateway: {args.gateway_url}")
    print(f"  wire: {args.show_stream_path}  solar: v{args.solar_version}")
    print(f"  M8_OPERATOR_TOKEN: <set, redacted>")
    print(f"  TWITCH_STREAM_KEY: {'<set, redacted>' if stream_key else '<unset>'}")

    try:
        setup = m8_setup.run_setup(
            gateway_url=args.gateway_url,
            operator_token=operator_token,
            twitch_key=stream_key,
            solar_version=args.solar_version,
            show_stream_path=args.show_stream_path,
            log=print,
        )
    except m8_setup.SetupError as exc:
        # SetupError messages are already redacted at construction.
        print(f"FAIL: SETUP leg failed: {exc}")
        return 1

    print("=== M8 SETUP done — scene authored, pushed, active, token minted ===")
    print(f"  scene_id={setup.scene_id}")
    print(f"  bundle_hash(H)={setup.bundle_hash}")
    print(f"  pushed_scene_version={setup.pushed_scene_version}")
    print(f"  blueprints={setup.blueprint_ids}")
    print(f"  background(provenance target)={setup.test_background}")
    print(f"  solar_url={m8_setup.redact_solar_url(setup.solar_url)}")

    # Secrets to scrub from EVERY diagnostic line below (M6's redact() only
    # covered the stream key; M8 adds the operator JWT + show-token).
    scrub = [s for s in (stream_key, operator_token, setup.show_token) if s]

    def safe(text: str) -> str:
        return m8_setup.redact_token(text, *scrub)

    port = pick_free_port()
    password = secrets.token_urlsafe(16)
    print(f"\n=== M8 PRE-FLIGHT + BROADCAST (reused M6 core) ===")
    print(f"spawning: {exe}")
    print(f"  PULSAR_PORT={port}  PULSAR_PASSWORD=<redacted {len(password)} chars>")

    # Point the reused M6 core at the M8 artefact paths + destination name
    # BEFORE spawn (PulsarProcess.spawn reads LIVE_VOD_DIR for PULSAR_RECORD_DIR).
    m6.LIVE_VOD_DIR = LIVE_VOD_DIR
    m6.PROOF_PNG = PROOF_PNG
    m6.BUILD_DIR = BUILD_DIR
    m6.DESTINATION_NAME = "pulsar-m8-canvas-live"
    pulsar = m6.PulsarProcess(exe, port, password, args.fps)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        rc = asyncio.run(run(
            ws_url, sentinel_pw, setup, stream_key,
            args.duration, args.preflight_only, pulsar,
        ))
    except KeyboardInterrupt:
        print("interrupted")
        rc = 130
    except Exception as exc:  # noqa: BLE001 — top-level probe diagnostic
        print(f"FAIL: {safe(str(exc))}")
        if pulsar.proc is not None:
            print(safe(pulsar.diag()))
        rc = 1
    finally:
        if rc != 0:
            tail = pulsar.lines[-80:]
            if tail:
                print("\n---- pulsar stdout (last 80 lines, redacted) ----")
                for ln in tail:
                    print(f"  {safe(ln)}")
                print("---- end pulsar stdout ----\n")
        pulsar.shutdown()
        if pulsar.proc is not None and pulsar.proc.poll() is None:
            print("error: pulsar.exe still running after shutdown attempt")
            rc = rc or 1
        else:
            print("pulsar.exe reaped cleanly")

    print("PASS" if rc == 0 else (f"SKIPPED (exit {rc})" if rc == 3
                                  else f"FAILED (exit {rc})"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
