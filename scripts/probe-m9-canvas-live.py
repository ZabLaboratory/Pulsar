#!/usr/bin/env python3
"""Pulsar M9 live probe — a Blue **trigger** repaints a live scene on air,
proven pixel-by-pixel (ADR Blue 001 §6 criterion 4, the M9 proof).

M9 is M8's authored Canvas scene reaching the wire (the SETUP + non-blank
provenance pre-flight are reused wholesale), PLUS the reactive step ADR
Blue 001 exists to prove: firing ``POST /api/v1/blueprints/{id}/trigger``
mutates a **live** scene **with no reload**.

The decisive chain (ADR Blue 001 §3.2.5):

    trigger → Blue interprets (execute_version) → maps outputs to
    __inputs.blue.<slug>.<port> → pushes input frame on the scoped
    service-token WS → Orion CanWritePath + write leaf → recompute →
    delta → LSDP wire → Solar repaints the bound region, no reload.

THE PROOF (capture-A / fire / capture-B):

  1. SETUP (m9_setup) authors a scene whose **frame background** is bound
     to ``__inputs.blue.pulsar-m9-bg.colour``, declared as an operator-input
     with default colour **A** (#1A9E57). Orion seeds A on boot.
  2. The reused M6 CEF core points a browser_source at the live Solar URL
     (SetCaptureSource — called ONCE; never re-created between A and B, so
     the later change is a live repaint, not a reload).
  3. Pre-flight: poll the captured frame until non-blank AND its modal
     colour ≈ **A**. This is capture-A (``build/m9-before.png``) — it ties
     the on-air pixels to the seeded leaf default before any trigger.
  4. Fire ``/trigger`` with the operator Bearer **header** and
     ``inputs={"colour": B}`` (#C81E5A). ADR Blue 001 R6 — operator/admin
     only; the JWT rides as ``Authorization: Bearer`` (header, never query).
  5. Poll the *same* browser_source until its modal colour moves to ≈ **B**
     within Orion's input-to-delta budget (+ CEF render/screenshot slack).
     This is capture-B (``build/m9-after.png``).
  6. Assert **B ≈ target-B** AND **Manhattan(B, A) > REPAINT_MIN_DELTA** —
     the bound region demonstrably changed on screen, caused by the trigger,
     with no reload. A frame that never leaves A (push lost / leaf rejected /
     scene didn't bind) FAILS; a frame that changed to something other than
     B (wrong scene / ambient) FAILS.

Broadcast: M9's core IS the repaint proof and runs in the pre-flight
(``--preflight-only`` is the default-meaningful mode). The 30s Twitch
broadcast leg is reused verbatim from M6/M8 and is OPTIONAL (``--broadcast``)
— going live is not what M9 proves; the proven repaint is (so a CI run needs
no Twitch key). When broadcasting, the M6 bounded anti-boot-race
StartDestination retry applies as in M8.

SECRET HYGIENE (ADR Blue 001 R4 / R6, M8 parity — load-bearing):
  - NO token committed anywhere (no baked Solar URL / JWT).
  - The operator JWT (drives SETUP **and** the /trigger fire), the Twitch
    key, and the minted show-token all come from the étage-1 environment.
  - The operator credential is M8_OPERATOR_TOKEN (admin, short-TTL) — NOT
    ORION_OPERATOR_TOKEN. The SAME operator JWT authorises /trigger (R6).
  - Every line emitting solar_url is passed through redact_solar_url; the
    Twitch key + operator JWT + show-token are scrubbed everywhere else;
    and run-m9.ps1's grep-assert fails the run if any credential — including
    the operator JWT used on /trigger — leaks to stdout / PNG / VOD.

Usage (from the repo root, against the built -Full rundir):
    pip install websockets
    export M8_OPERATOR_TOKEN=...        # étage-1 admin JWT, short-TTL (SETUP + /trigger)
    export M8_GATEWAY_URL=http://127.0.0.1:8099   # tunnel'd gateway base
    python scripts/probe-m9-canvas-live.py                # author+push+capture-A+trigger+capture-B (the proof)
    export TWITCH_STREAM_KEY=...        # étage-1, broadcast leg only
    python scripts/probe-m9-canvas-live.py --broadcast    # + 30s Twitch broadcast

Exit codes (mirror M8):
  0  pass (repaint proven A→B; if --broadcast, live ok too)
  1  fail (setup / provenance / repaint / broadcast assertion failed)
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

# Ensure the sibling m8_setup / m9_setup modules import regardless of CWD.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Import the M6 broadcast + CEF core by path (hyphenated filename → not a
# module identifier). Reused wholesale: CEF spawn/reap, pure-stdlib PNG
# decode + analyse_frame, the broadcast loop, secret redaction.
_m6_path = SCRIPTS_DIR / "probe-m6-live.py"
_spec = importlib.util.spec_from_file_location("probe_m6_live", _m6_path)
assert _spec is not None and _spec.loader is not None
m6 = importlib.util.module_from_spec(_spec)
sys.modules["probe_m6_live"] = m6
_spec.loader.exec_module(m6)

import m8_setup  # noqa: E402
import m9_setup  # noqa: E402

try:
    import websockets  # noqa: F401  (used inside m6.run via the shared import)
except ImportError:
    print("error: pip install websockets (pure WS client — no native deps)")
    sys.exit(2)


BUILD_DIR = REPO_ROOT / "build"
PROOF_BEFORE_PNG = BUILD_DIR / "m9-before.png"
PROOF_AFTER_PNG = BUILD_DIR / "m9-after.png"
LIVE_VOD_DIR = BUILD_DIR / "m9-canvas-vod"

# Modal-colour provenance tolerance (Manhattan in RGB) — same band M8 uses:
# the CEF render + PNG re-encode shift colours slightly, but a large flat
# field's modal stays tight to the authored hex.
MODAL_COLOUR_TOL = 24

# The minimum modal delta between the pre-trigger (A) and post-trigger (B)
# frames for the repaint to count as REAL. A=(26,158,87), B=(200,30,90) are
# 305 apart, so this 80 floor is comfortably below the real signal yet far
# above any CEF/encode jitter (which stays within MODAL_COLOUR_TOL≈24). A
# frame that never repainted stays at distance ~0 and FAILS here.
REPAINT_MIN_DELTA = 80

# Budget for the repaint to land after the trigger fires. Orion's
# input→delta is ≤ 50 ms (Orion criterion 4) and the LSDP push is sub-second;
# the slack here is for CEF to render the delta + our 1 Hz screenshot poll
# cadence — NOT for the leaf write itself. A repaint that needs longer than
# this is a real failure (push lost / scene not reacting), not a slow render.
REPAINT_DEADLINE_S = 8.0
REPAINT_POLL_INTERVAL_S = 0.5


def _manhattan(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def _modal_ok(modal: Optional[tuple[int, int, int]],
              target: tuple[int, int, int]) -> tuple[bool, int]:
    """(ok, manhattan) for a modal-colour match within MODAL_COLOUR_TOL."""
    if modal is None:
        return False, 1 << 30
    dist = _manhattan(modal, target)
    return dist <= MODAL_COLOUR_TOL, dist


async def _grab_modal(inbox, ws) -> tuple[Optional[tuple[int, int, int]], Optional[bytes], dict]:
    """One GetSourceScreenshot → (modal_rgb, png_bytes, metrics).

    Targets the same managed CEF source the pre-flight created
    (CAPTURE_SOURCE_NAME). Returns (None, None, {}) when the shot is not
    ready or fails to decode — the caller polls."""
    r = await m6.request(inbox, ws, "GetSourceScreenshot", f"m9-{time.monotonic()}", {
        "sourceName": m6.CAPTURE_SOURCE_NAME,
        "imageFormat": "png",
        "imageWidth": m6.CANVAS_W,
        "imageHeight": m6.CANVAS_H,
    })
    if not r["requestStatus"]["result"]:
        return None, None, {}
    try:
        png = m6._strip_data_uri(r["responseData"]["imageData"])  # noqa: SLF001
        w, h, ch, px = m6.decode_png(png)
    except Exception:  # noqa: BLE001
        return None, None, {}
    metrics = m6.analyse_frame(w, h, ch, px)
    return metrics.get("modal"), png, metrics


async def capture_before(inbox, ws, solar_url: str,
                         rgb_a: tuple[int, int, int]) -> tuple[int, Optional[bytes]]:
    """Pre-flight: render the live Solar scene, confirm non-blank AND modal
    ≈ A, save build/m9-before.png. Returns (rc, before_png). rc=0 means the
    scene is on-air at the seeded default A — the baseline the trigger will
    move. Reuses M6's SetCaptureSource + non-blank poll (the browser_source
    is created HERE, once, and never re-created)."""
    # Point M6's module proof path + dirs at the M9 BEFORE artefact so the
    # reused non-blank core writes where the M9 wrapper/CI expect.
    m6.PROOF_PNG = PROOF_BEFORE_PNG
    m6.BUILD_DIR = BUILD_DIR

    rc, metrics = await m6.preflight_non_blank(inbox, ws, solar_url)
    if rc != 0:
        print("[M9] capture-A FAILED at the non-blank stage — the live Solar "
              "scene never produced a frame. NOT proceeding to the trigger. "
              "Diagnose: LSDP WS to the gateway failed (token/.lsdp gate?), "
              "Solar bundle 404, or the active scene never streamed. See the "
              "m6 diagnosis above + the saved PNG.")
        return 1, None

    modal = metrics.get("modal")
    ok, dist = _modal_ok(modal, rgb_a)
    print(f"[M9] capture-A modal-colour check: captured modal={modal} "
          f"target_A={rgb_a} manhattan={dist} tol={MODAL_COLOUR_TOL}")
    if not ok:
        print("[M9] capture-A FAILED — the frame is non-blank but its modal "
              "colour does NOT match the seeded leaf default A "
              f"(#{rgb_a[0]:02X}{rgb_a[1]:02X}{rgb_a[2]:02X}). The scene on "
              "air is not M9's, or the leaf was not seeded from its declared "
              "default. NOT firing the trigger. Inspect: " + str(PROOF_BEFORE_PNG))
        return 1, None

    before_png = PROOF_BEFORE_PNG.read_bytes() if PROOF_BEFORE_PNG.exists() else None
    print(f"[M9] capture-A PROVEN — non-blank AND modal ≈ seeded default A. "
          f"Baseline frame: {PROOF_BEFORE_PNG}")
    return 0, before_png


async def capture_after(inbox, ws,
                        rgb_a: tuple[int, int, int],
                        rgb_b: tuple[int, int, int]) -> tuple[int, Optional[bytes]]:
    """Post-trigger: poll the SAME browser_source until its modal colour
    moves to ≈ B, save build/m9-after.png, and assert the repaint is real.

    The assertions (the M9 proof):
      - modal_after ≈ B (within MODAL_COLOUR_TOL): the trigger's pushed
        value reached the pixels.
      - Manhattan(modal_after, A) > REPAINT_MIN_DELTA: the bound region
        DEMONSTRABLY changed from the pre-trigger baseline — not the same
        frame, not jitter.
    No SetCaptureSource is issued here: the browser_source from capture-A is
    untouched, so a change is a live LSDP repaint, not a reload.
    """
    print(f"-> polling for repaint to B={rgb_b} (deadline {REPAINT_DEADLINE_S:.0f}s; "
          f"Orion input→delta is ≤50ms, slack is CEF render + 2Hz screenshot poll) ...")
    deadline = time.monotonic() + REPAINT_DEADLINE_S
    attempt = 0
    last_modal: Optional[tuple[int, int, int]] = None
    last_png: Optional[bytes] = None
    while time.monotonic() < deadline:
        attempt += 1
        modal, png, _ = await _grab_modal(inbox, ws)
        if modal is not None:
            last_modal, last_png = modal, png
            ok_b, dist_b = _modal_ok(modal, rgb_b)
            delta_a = _manhattan(modal, rgb_a)
            if attempt == 1 or attempt % 4 == 0:
                print(f"   attempt {attempt}: modal={modal} dist_to_B={dist_b} "
                      f"delta_from_A={delta_a}")
            if ok_b and delta_a > REPAINT_MIN_DELTA:
                BUILD_DIR.mkdir(parents=True, exist_ok=True)
                if png is not None:
                    PROOF_AFTER_PNG.write_bytes(png)
                print(f"[M9] REPAINT PROVEN — modal moved A={rgb_a} → B={modal} "
                      f"(≈ target B={rgb_b}, dist_to_B={dist_b} ≤ {MODAL_COLOUR_TOL}; "
                      f"delta_from_A={delta_a} > {REPAINT_MIN_DELTA}). The Blue "
                      f"trigger repainted the bound region on screen, no reload. "
                      f"Proof PNG: {PROOF_AFTER_PNG}")
                return 0, png
        await asyncio.sleep(REPAINT_POLL_INTERVAL_S)

    # Deadline elapsed without a proven repaint — diagnose precisely.
    if last_png is not None:
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        PROOF_AFTER_PNG.write_bytes(last_png)
    print("\n[M9] REPAINT FAILED — the bound region did not move to B within "
          f"the budget. Last modal={last_modal} (target B={rgb_b}, A={rgb_a}).")
    if last_modal is not None and _manhattan(last_modal, rgb_a) <= REPAINT_MIN_DELTA:
        print("  The frame STAYED at A: the trigger's leaf write never reached "
              "the live scene. Likely: Blue's bridge is off (no BLUE_OPERATOR_TOKEN "
              "→ pushed.delivered=false), the leaf is out of scope/undeclared "
              "(Orion CanWritePath / sceneAcceptsPath rejected it — check the "
              "operator_inputs declaration), or Orion never recomputed/emitted "
              "the delta. The /trigger returned 200 regardless (best-effort, "
              "ADR Blue 001 §3.2.6), so a 200 alone does NOT prove the repaint.")
    elif last_modal is not None:
        print("  The frame CHANGED but not to B: a different value reached the "
              "leaf, or a different scene is on air. Inspect both PNGs.")
    else:
        print("  No screenshot decoded post-trigger — the browser source "
              "stopped producing frames.")
    print(f"  before={PROOF_BEFORE_PNG}  after={PROOF_AFTER_PNG}")
    return 1, last_png


async def run(ws_url: str, password: str, setup: "m9_setup.SetupResult",
              gateway_url: str, operator_token: str, scrub: list[str],
              stream_key: str, duration_sec: int, broadcast: bool,
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
                  "M9 needs a full build (scripts/build-win.ps1 -Full). "
                  "Typed skip, NOT a pass.")
            return 3
        print(f"browser_source registered ({len(kinds)} input kinds total)")

        # --- capture-A: scene on air at the seeded default A ---------------
        rc, _ = await capture_before(inbox, ws, setup.solar_url, setup.rgb_a)
        if rc != 0:
            return 1

        # --- fire the trigger (operator Bearer header, R6) -----------------
        print("\n[M9] firing the Blue trigger (the live mutation) ...")
        try:
            m9_setup.fire_trigger(
                gateway_url=gateway_url,
                operator_token=operator_token,
                secrets=scrub,
                blueprint_id=setup.blueprint_id,
                colour_b=setup.colour_b,
                log=print,
            )
        except m9_setup.SetupError as exc:
            print(f"[M9] trigger FAILED: {exc}")
            return 1

        # --- capture-B: prove the bound region repainted to B --------------
        rc, _ = await capture_after(inbox, ws, setup.rgb_a, setup.rgb_b)
        if rc != 0:
            return 1

        if not broadcast:
            print("[M9] repaint proven; --broadcast not set, skipping go-live.")
            return 0

        print("\n[M9] going live to Twitch (repaint proven) ...")
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
        description="Pulsar M9 — Blue trigger repaints a live Canvas scene, proven A→B")
    ap.add_argument("--exe", type=pathlib.Path,
                    default=pathlib.Path(os.environ.get("PULSAR_EXE", str(m6.DEFAULT_EXE))),
                    help="path to pulsar.exe (default: built rundir)")
    ap.add_argument("--gateway-url", type=str,
                    default=os.environ.get("M8_GATEWAY_URL", "http://127.0.0.1:8099"),
                    help="ZabGate base URL (default: the tunnel'd gateway). All "
                         "SETUP + /trigger HTTP is gateway-first against this base.")
    ap.add_argument("--solar-version", type=str,
                    default=os.environ.get("M8_SOLAR_VERSION",
                                           m9_setup.DEFAULT_SOLAR_VERSION),
                    help="Solar bundle version served at /static/solar/v{N}/ "
                         "(default 0.2.0 — the LSDP wire).")
    ap.add_argument("--show-stream-path", type=str,
                    default=os.environ.get("M8_SHOW_STREAM_PATH",
                                           m9_setup.DEFAULT_SHOW_STREAM_PATH),
                    choices=["stream.lsdp", "stream"],
                    help="show-stream wire path: stream.lsdp (LSDP, default) or "
                         "stream (bespoke fallback).")
    ap.add_argument("--broadcast", action="store_true",
                    help="after proving the repaint, also broadcast 30s to Twitch "
                         "(needs TWITCH_STREAM_KEY). Off by default — M9 proves "
                         "the repaint, not the go-live.")
    ap.add_argument("--duration", type=int,
                    default=int(os.environ.get("LIVE_TEST_DURATION", "30")),
                    help="broadcast duration in seconds (default 30, --broadcast only)")
    ap.add_argument("--fps", type=int,
                    default=int(os.environ.get("LIVE_TEST_FPS", "60")),
                    help="encoder fps target (default 60)")
    ap.add_argument("--ready-timeout", type=float, default=m6.READY_TIMEOUT_S)
    args = ap.parse_args()

    exe: pathlib.Path = args.exe
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        print("Build it first: scripts/build-win.ps1 -Full")
        return 2

    # The SETUP + /trigger operator credential is the dedicated short-TTL
    # admin token from étage-1 (reused from M8) — NOT ORION_OPERATOR_TOKEN.
    # The SAME JWT authorises /trigger (ADR Blue 001 R6, operator/admin).
    operator_token = os.environ.get("M8_OPERATOR_TOKEN", "").strip()
    if not operator_token:
        print("error: M8_OPERATOR_TOKEN env var is empty (required). It is the "
              "admin/operator JWT for SETUP (save/push/active-scene/mint) AND for "
              "the /trigger fire (ADR Blue 001 R6 requires operator/admin). Source "
              "it from the étage-1 secret as a SHORT-TTL admin token — do NOT reuse "
              "ORION_OPERATOR_TOKEN. Never commit it.")
        return 2
    if operator_token == os.environ.get("ORION_OPERATOR_TOKEN", "").strip() and operator_token:
        print("error: M8_OPERATOR_TOKEN must NOT equal ORION_OPERATOR_TOKEN "
              "(the long-lived exp-2027 service token). Mint a dedicated "
              "short-TTL admin token for the test run.")
        return 2

    stream_key = os.environ.get("TWITCH_STREAM_KEY", "").strip()
    if args.broadcast and not stream_key:
        print("error: --broadcast set but TWITCH_STREAM_KEY env var is empty. "
              "Set it from the étage-1 secret; never commit. (Omit --broadcast "
              "to prove the repaint without going live.)")
        return 2

    print("=== M9 SETUP leg (gateway-first authoring + activation) ===")
    print(f"  gateway: {args.gateway_url}")
    print(f"  wire: {args.show_stream_path}  solar: v{args.solar_version}")
    print(f"  M8_OPERATOR_TOKEN: <set, redacted> (SETUP + /trigger)")
    print(f"  TWITCH_STREAM_KEY: {'<set, redacted>' if stream_key else '<unset>'}"
          f"{'' if args.broadcast else ' (broadcast off)'}")

    try:
        setup = m9_setup.run_setup(
            gateway_url=args.gateway_url,
            operator_token=operator_token,
            twitch_key=stream_key,
            solar_version=args.solar_version,
            show_stream_path=args.show_stream_path,
            log=print,
        )
    except m9_setup.SetupError as exc:
        print(f"FAIL: SETUP leg failed: {exc}")
        return 1

    print("=== M9 SETUP done — scene authored, pushed, active, token minted ===")
    print(f"  scene_id={setup.scene_id}")
    print(f"  bundle_hash(H)={setup.bundle_hash}")
    print(f"  pushed_scene_version={setup.pushed_scene_version}")
    print(f"  blueprint_id={setup.blueprint_id} slug={setup.blueprint_slug}")
    print(f"  bound_leaf={setup.leaf_path}")
    print(f"  colour A(default)={setup.colour_a}  colour B(trigger)={setup.colour_b}")
    print(f"  solar_url={m9_setup.redact_solar_url(setup.solar_url)}")

    # Secrets to scrub from EVERY diagnostic line below + passed to the
    # trigger client so a 4xx body can never echo the JWT. The operator JWT
    # is now ALSO the /trigger credential — it must be redacted everywhere.
    scrub = [s for s in (stream_key, operator_token, setup.show_token) if s]

    def safe(text: str) -> str:
        return m9_setup.redact_token(text, *scrub)

    port = pick_free_port()
    password = secrets.token_urlsafe(16)
    print(f"\n=== M9 PROOF: capture-A → trigger → capture-B (reused M6 CEF core) ===")
    print(f"spawning: {exe}")
    print(f"  PULSAR_PORT={port}  PULSAR_PASSWORD=<redacted {len(password)} chars>")

    # Point the reused M6 core at the M9 artefact paths + destination name.
    m6.LIVE_VOD_DIR = LIVE_VOD_DIR
    m6.PROOF_PNG = PROOF_BEFORE_PNG
    m6.BUILD_DIR = BUILD_DIR
    m6.DESTINATION_NAME = "pulsar-m9-canvas-live"
    pulsar = m6.PulsarProcess(exe, port, password, args.fps)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        rc = asyncio.run(run(
            ws_url, sentinel_pw, setup,
            args.gateway_url, operator_token, scrub,
            stream_key, args.duration, args.broadcast, pulsar,
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
