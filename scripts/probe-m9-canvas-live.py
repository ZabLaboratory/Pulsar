#!/usr/bin/env python3
"""Pulsar M9 live probe — a Blue **trigger** repaints a live scene **on air,
in direct**, proven pixel-by-pixel (ADR Blue 001 §6 criterion 4, the M9 proof).

M9 is M8's authored Canvas scene reaching the wire (the SETUP + non-blank
provenance pre-flight are reused wholesale), PLUS the reactive step ADR
Blue 001 exists to prove: firing ``POST /api/v1/blueprints/{id}/trigger``
mutates a **live** scene **with no reload** — and the porteur must be able to
**watch that swap happen on the broadcast**, exactly like M8's mid-stream
scene-switch (probe-twitch-scene-switch.py fires its switch at duration/2
*while live*). So in M9 the **trigger fires mid-broadcast**, not before it:
the VOD shows a hard green→magenta cut on air, not a static magenta field.

The decisive chain (ADR Blue 001 §3.2.5):

    trigger → Blue interprets (execute_version) → maps outputs to
    __inputs.blue.<slug>.<port> → pushes input frame on the scoped
    service-token WS → Orion CanWritePath + write leaf → recompute →
    delta → LSDP wire → Solar repaints the bound region, no reload.

THE PROOF — live by default (broadcast → capture-A → trigger@mid → capture-B):

  1. SETUP (m9_setup) authors a scene whose **frame background** is bound
     to ``__inputs.blue.pulsar-m9-bg.colour``, declared as an operator-input
     with default colour **A** (#1A9E57). Orion seeds A on boot.
  2. The reused M6 CEF core points a browser_source at the live Solar URL
     (SetCaptureSource — called ONCE in the pre-flight; never re-created
     between A and B, so the later change is a live repaint, not a reload).
  3. Pre-flight (before going live): poll the captured frame until non-blank
     so the green field A is demonstrably rendering.
  4. **StartDestination → the broadcast goes live on the GREEN A frame.**
  5. **capture-A** *during the live broadcast*: grab the on-air frame, assert
     modal ≈ **A**, save ``build/m9-before.png`` — the stream starts green.
  6. Poll metrics ~duration/2 (the live + VOD show green) — destination
     active, drop ratio within budget.
  7. **At t≈duration/2, fire ``/trigger``** with the operator Bearer
     **header** and ``inputs={"colour": B}`` (#C81E5A) — ADR Blue 001 R6,
     operator/admin only; the JWT rides as ``Authorization: Bearer`` (header,
     never query). The bound background flips to magenta **on air, in direct**.
  8. **capture-B** *during the live broadcast*: poll the *same* browser_source
     until its modal moves to ≈ **B**, save ``build/m9-after.png``, and assert
     **B ≈ target-B** AND **Manhattan(B, A) > REPAINT_MIN_DELTA** — the bound
     region demonstrably changed on screen, on the live wire, no reload. A
     frame that never leaves A (push lost / leaf rejected / scene didn't bind)
     FAILS; a frame that changed to something other than B FAILS.
  9. Poll ~the remaining duration (the live + VOD show magenta),
     StopDestination. **The VOD shows the green→magenta transition on air.**

The M6 bounded anti-boot-race StartDestination retry applies as in M8.

Broadcast vs proof-only: the live broadcast with the **mid-stream trigger** is
the DEFAULT (porteur preference: à l'antenne par défaut). ``--no-broadcast``
keeps the original "prove the repaint without going live" mode (pre-flight →
trigger → capture-B, no Twitch), useful for a CI run with no Twitch key.

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
    export TWITCH_STREAM_KEY=...        # étage-1 (required for the default live run)
    python scripts/probe-m9-canvas-live.py                 # LIVE: broadcast→capture-A→trigger@mid→capture-B→stop
    python scripts/probe-m9-canvas-live.py --no-broadcast  # prove the repaint without going live (no Twitch key)

Exit codes (mirror M8):
  0  pass (repaint proven A→B on air; if live, broadcast ok too)
  1  fail (setup / provenance / repaint / broadcast assertion failed)
  2  config error (no operator token, no exe, no key for the live run, bad args)
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

# Live-broadcast leg (mid-stream trigger). The trigger fires at duration/2 so
# the VOD shows roughly equal halves: green A, then magenta B. The metric poll
# cadence + the destination-active / drop-ratio thresholds are M6/M8 verbatim
# (m6.POLL_INTERVAL_SEC / m6.FRAME_DROP_RATIO_MAX), so this leg makes no new
# claim about broadcast health — it reuses the proven M8 live assertions and
# only inserts capture-A (right after going live) and the trigger@mid + capture-B.
LIVE_POLL_INTERVAL_S = m6.POLL_INTERVAL_SEC

# Minimum broadcast duration that leaves room for: go-live + capture-A, a green
# half, the trigger + capture-B, a magenta half. Below this the mid-stream swap
# has no visible runway on either side.
MIN_LIVE_DURATION_S = 12


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


async def preflight_render(inbox, ws, solar_url: str) -> int:
    """Pre-flight, BEFORE going live: create the browser_source (ONCE) and poll
    until the live Solar scene renders a non-blank frame. rc=0 means the green
    A field is on screen and ready to broadcast. No modal-A assertion here —
    that is capture-A, taken *after* the broadcast starts (so the proof is that
    the LIVE stream opened on green). The browser_source created here is never
    re-created, so the later A→B change is a repaint, not a reload."""
    # Point M6's module proof path + dirs at the M9 BEFORE artefact so the
    # reused non-blank core writes where the M9 wrapper/CI expect.
    m6.PROOF_PNG = PROOF_BEFORE_PNG
    m6.BUILD_DIR = BUILD_DIR

    rc, _metrics = await m6.preflight_non_blank(inbox, ws, solar_url)
    if rc != 0:
        print("[M9] pre-flight FAILED at the non-blank stage — the live Solar "
              "scene never produced a frame. NOT going live / NOT triggering. "
              "Diagnose: LSDP WS to the gateway failed (token/.lsdp gate?), "
              "Solar bundle 404, or the active scene never streamed. See the "
              "m6 diagnosis above + the saved PNG.")
        return 1
    print("[M9] pre-flight PASSED — the Solar scene renders non-blank; the "
          "green A field is ready to broadcast.")
    return 0


async def assert_capture_a(inbox, ws, rgb_a: tuple[int, int, int],
                           *, on_air: bool) -> tuple[int, Optional[bytes]]:
    """capture-A: grab the captured frame, assert modal ≈ A, save
    build/m9-before.png. Returns (rc, before_png). When ``on_air`` this is
    taken *during the live broadcast* — the proof that the stream STARTED green
    (the baseline the mid-stream trigger will move). No SetCaptureSource: the
    pre-flight's browser_source is untouched."""
    where = "on air (live)" if on_air else "pre-flight"
    modal, png, _ = await _grab_modal(inbox, ws)
    ok, dist = _modal_ok(modal, rgb_a)
    print(f"[M9] capture-A ({where}) modal-colour check: captured modal={modal} "
          f"target_A={rgb_a} manhattan={dist} tol={MODAL_COLOUR_TOL}")
    if not ok:
        print("[M9] capture-A FAILED — the frame's modal colour does NOT match "
              "the seeded leaf default A "
              f"(#{rgb_a[0]:02X}{rgb_a[1]:02X}{rgb_a[2]:02X}). The scene on "
              "air is not M9's, or the leaf was not seeded from its declared "
              "default. NOT firing the trigger. Inspect: " + str(PROOF_BEFORE_PNG))
        return 1, None
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    if png is not None:
        PROOF_BEFORE_PNG.write_bytes(png)
    before_png = PROOF_BEFORE_PNG.read_bytes() if PROOF_BEFORE_PNG.exists() else None
    print(f"[M9] capture-A PROVEN ({where}) — modal ≈ seeded default A. "
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


async def broadcast_with_live_trigger(
    inbox, ws, setup: "m9_setup.SetupResult", gateway_url: str,
    operator_token: str, scrub: list[str], stream_key: str,
    duration_sec: int, pulsar) -> int:
    """The LIVE M9 proof: broadcast on the GREEN A frame, capture-A on air,
    fire the trigger at t≈duration/2 so the swap happens **in direct**, then
    capture-B on air and assert the repaint. The VOD records green→magenta.

    Structure mirrors probe-twitch-scene-switch.py's broadcast(): the switch
    fires at ``duration/2`` *inside* the live poll loop. Here the "switch" is
    the Blue /trigger; capture-A / capture-B straddle it on the live wire. The
    destination lifecycle (CreateDestination, anti-boot-race StartDestination
    retry, StartRecord, GetDestinations/GetAdaptiveState poll, Stop*) is the
    M6/M8 core reused verbatim.

    The pre-flight (browser_source create + non-blank) MUST have already run
    against ``setup.solar_url`` so the green field is on screen before we go
    live."""
    import json
    trigger_at = duration_sec / 2.0
    key = stream_key

    # 1. CreateDestination(twitch) — key passed opaquely, never printed.
    r = await m6.vendor_call(inbox, ws, "create-dest", "pulsar",
        "CreateDestination", {
            "name": m6.DESTINATION_NAME, "kind": "twitch", "key": key,
        })
    dest_id = m6.vendor_response_data(r).get("id")
    if not dest_id:
        status = m6.vendor_request_status(r)
        print(f"FAIL: CreateDestination returned no id; "
              f"status={m6.redact(json.dumps(status), key)}")
        return 1
    print(f"-> CreateDestination(twitch) id={dest_id}")

    # 2. StartDestination with the bounded anti-boot-race retry (M8 parity).
    #    The broadcast goes live on the GREEN A frame (pre-flight rendered it).
    if not await m6.start_destination_with_retry(inbox, ws, dest_id, key):
        await m6.vendor_call(inbox, ws, "rm-dest", "pulsar",
            "RemoveDestination", {"id": dest_id})
        return 1

    # 2b. StartRecord — the offline VOD that will show the green→magenta cut.
    recording = False
    r = await m6.request(inbox, ws, "StartRecord", "start-rec")
    if r.get("requestStatus", {}).get("result"):
        recording = True
        print(f"-> StartRecord ok (writing under {LIVE_VOD_DIR}) — the VOD will "
              f"capture the on-air green→magenta transition")
    else:
        print(f"   warn: StartRecord declined: {r.get('requestStatus')}")

    rc = 0
    try:
        # 3. capture-A DURING the live broadcast — the stream started green.
        print("\n[M9] capturing A on the LIVE wire (the broadcast opened on green) ...")
        rc_a, _ = await assert_capture_a(inbox, ws, setup.rgb_a, on_air=True)
        if rc_a != 0:
            rc = 1

        triggered = False
        repaint_ok = False
        start_t = time.time()
        poll = 0
        adaptive_seen = 0
        while rc == 0 and time.time() - start_t < duration_sec:
            await asyncio.sleep(LIVE_POLL_INTERVAL_S)
            poll += 1
            elapsed = time.time() - start_t

            # 4. MID-STREAM TRIGGER at t≈duration/2 — the swap, on air.
            if not triggered and elapsed >= trigger_at:
                print(f"\n** BLUE TRIGGER @ t={elapsed:.1f}s (mid-stream) : firing "
                      f"/trigger {{'colour': B}} — the bound background flips "
                      f"GREEN→MAGENTA on air, in direct ...")
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
                    print(f"FAIL: [M9] trigger FAILED on air: {exc}")
                    rc = 1
                    break
                triggered = True

                # 5. capture-B DURING the live broadcast — assert the repaint
                #    happened on the wire (modal→B, Manhattan(A→B) > floor).
                print("[M9] capturing B on the LIVE wire (the repaint is on air) ...")
                rc_b, _ = await capture_after(inbox, ws, setup.rgb_a, setup.rgb_b)
                if rc_b != 0:
                    rc = 1
                    break
                repaint_ok = True

            # Live-health poll — M6/M8 verbatim assertions.
            r = await m6.vendor_call(inbox, ws, f"get-dest-{poll}", "pulsar",
                "GetDestinations", {})
            lst = m6.vendor_response_data(r).get("destinations", [])
            ours = next((d for d in lst if d.get("id") == dest_id), None)
            if not ours or not ours.get("active"):
                print(f"FAIL: destination not active at poll #{poll}: {ours}")
                await asyncio.sleep(0.3)
                rtmp = m6._scan_rtmp_diagnostic(pulsar.lines)  # noqa: SLF001
                if rtmp:
                    print("  RTMP ingest diagnostic (pulsar log):")
                    for ln in rtmp[-6:]:
                        print(f"    {m6.redact(ln, key)}")
                rc = 1
                break

            r = await m6.vendor_call(inbox, ws, f"get-adapt-{poll}", "pulsar",
                "GetAdaptiveState", {})
            adapt = m6.vendor_response_data(r)
            samples = int(adapt.get("samples", 0))
            adaptive_seen = max(adaptive_seen, samples)
            drop_ratio = float(adapt.get("last_drop_ratio", 0.0))
            cur_kbps = adapt.get("current_kbps")

            sr = await m6.request(inbox, ws, "GetStats", f"stats-{poll}")
            stats = sr.get("responseData", {}) or {}
            fps = stats.get("activeFps")
            fps_str = f"{fps:.1f}" if isinstance(fps, (int, float)) else "—"
            phase = "MAGENTA(B)" if triggered else "GREEN(A)"

            print(f"   poll #{poll} t={elapsed:.0f}s active=true phase={phase} "
                  f"samples={samples} drop_ratio={drop_ratio:.4f} "
                  f"bitrate={cur_kbps} fps={fps_str}")

            if drop_ratio > m6.FRAME_DROP_RATIO_MAX:
                print(f"FAIL: frame drop ratio {drop_ratio:.4f} > "
                      f"{m6.FRAME_DROP_RATIO_MAX} at poll #{poll}")
                rc = 1
                break

        if rc == 0 and not triggered:
            print("FAIL: broadcast ended before the mid-stream trigger fired "
                  "(duration too short?)")
            rc = 1
        elif rc == 0 and not repaint_ok:
            print("FAIL: the trigger fired but the on-air repaint was not proven")
            rc = 1
    finally:
        # 6. Stop cleanly (best-effort even on a failed poll) — leaves no orphan.
        if recording:
            try:
                r = await m6.request(inbox, ws, "StopRecord", "stop-rec")
                vod = (r.get("responseData", {}) or {}).get("outputPath")
                if vod:
                    print(f"-> StopRecord finalised: {vod}")
                    print(f"LIVE_VOD_PATH={vod}")
            except Exception as exc:  # noqa: BLE001
                print(f"   warn: StopRecord error: {exc}")
        try:
            await m6.vendor_call(inbox, ws, "stop-dest", "pulsar",
                "StopDestination", {"id": dest_id})
            print("-> StopDestination ok")
        except Exception as exc:  # noqa: BLE001
            print(f"   warn: StopDestination error: {exc}")
        try:
            await m6.vendor_call(inbox, ws, "rm-dest", "pulsar",
                "RemoveDestination", {"id": dest_id})
        except Exception:
            pass

    if rc == 0 and adaptive_seen <= 0:
        print(f"FAIL: adaptive worker never reported samples (saw {adaptive_seen})")
        rc = 1
    if rc == 0:
        print(f"-> LIVE M9 clean: broadcast opened GREEN, trigger fired mid-stream, "
              f"on-air repaint to MAGENTA proven, adaptive_samples={adaptive_seen}. "
              f"The VOD shows the green→magenta transition.")
    return rc


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

        # --- pre-flight: create the browser_source ONCE + render green A -----
        # The green field must be on screen BEFORE we go live (live path) or
        # before we trigger (no-broadcast path). SetCaptureSource happens here
        # and is never re-issued, so the later A→B change is a repaint.
        if await preflight_render(inbox, ws, setup.solar_url) != 0:
            return 1

        if broadcast:
            # === LIVE: broadcast → capture-A → trigger@mid → capture-B → stop.
            # The trigger fires IN the broadcast (t≈duration/2) so the swap is
            # visible on air + in the VOD, exactly like M8's scene-switch.
            print("\n[M9] going live to Twitch — the green A frame, then the "
                  "mid-stream Blue trigger flips it to magenta ON AIR ...")
            m6.LIVE_VOD_DIR = LIVE_VOD_DIR
            return await broadcast_with_live_trigger(
                inbox, ws, setup, gateway_url, operator_token, scrub,
                stream_key, duration_sec, pulsar)

        # === --no-broadcast: prove the repaint without going live (no Twitch).
        # capture-A → trigger → capture-B, all in the pre-flight CEF, no wire.
        print("\n[M9] --no-broadcast: proving the repaint without going live ...")
        rc, _ = await assert_capture_a(inbox, ws, setup.rgb_a, on_air=False)
        if rc != 0:
            return 1

        print("\n[M9] firing the Blue trigger (the mutation) ...")
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

        rc, _ = await capture_after(inbox, ws, setup.rgb_a, setup.rgb_b)
        if rc != 0:
            return 1
        print("[M9] repaint proven; --no-broadcast set, skipping go-live.")
        return 0


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
    ap.add_argument("--no-broadcast", dest="broadcast", action="store_false",
                    help="prove the repaint WITHOUT going live (no Twitch key): "
                         "pre-flight → capture-A → trigger → capture-B. By "
                         "default M9 goes LIVE and fires the trigger mid-stream "
                         "so the green→magenta swap is visible on air + in the VOD.")
    ap.set_defaults(broadcast=True)
    ap.add_argument("--duration", type=int,
                    default=int(os.environ.get("LIVE_TEST_DURATION", "30")),
                    help="broadcast duration in seconds (default 30); the "
                         "mid-stream trigger fires at duration/2 (live run only)")
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
        print("error: live run requires TWITCH_STREAM_KEY (it broadcasts and "
              "fires the trigger mid-stream so the swap is on air). Set it from "
              "the étage-1 secret; never commit. (Pass --no-broadcast to prove "
              "the repaint without going live, no Twitch key.)")
        return 2

    if args.broadcast and args.duration < MIN_LIVE_DURATION_S:
        print(f"error: --duration must be >= {MIN_LIVE_DURATION_S}s for the live "
              "run so the mid-stream trigger (at duration/2) has a green half "
              "before and a magenta half after.")
        return 2

    print("=== M9 SETUP leg (gateway-first authoring + activation) ===")
    print(f"  gateway: {args.gateway_url}")
    print(f"  wire: {args.show_stream_path}  solar: v{args.solar_version}")
    print(f"  mode: {'LIVE (broadcast + trigger@mid-stream, on air)' if args.broadcast else 'prove-only (--no-broadcast)'}")
    if args.broadcast:
        print(f"  duration: {args.duration}s  trigger@: {args.duration/2:.0f}s (mid-stream)")
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
    if args.broadcast:
        print("\n=== M9 PROOF (LIVE): broadcast → capture-A → trigger@mid → "
              "capture-B → stop (reused M6 CEF core) ===")
    else:
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
