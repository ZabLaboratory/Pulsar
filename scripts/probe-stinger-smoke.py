#!/usr/bin/env python3
"""probe-stinger-smoke.py -- M10 stinger build/load smoke, FLAG-AWARE for the
Solar/CEF pivot (ADR 003 Amendments 3 & 4 / #71, #79).

THE PIVOT MAKES THE NATIVE STINGER DORMANT (#73 / #83).
  The Amendment 1/2 OBS-native stinger transition is no longer the live M10
  mechanism. The visible transition is now a Solar/CEF *opaque overlay* over
  two monitor_capture scenes, and the screen-1->screen-2 change underneath is
  an INSTANTANEOUS hard-cut hidden under the overlay's opaque plateau (no
  OBS-native transition at all). The native stinger compositing therefore
  ships DORMANT behind the build/runtime flag ``PULSAR_NATIVE_STINGER``,
  default OFF (#73). This smoke is the load-bearing check on the operator box
  that the dormancy is correct.

THE FLAG GATES WHICH WORLD WE ASSERT (we never weaken -- we BRANCH).
  PULSAR_NATIVE_STINGER unset / "0" / "off"  (the DEFAULT, live pivot world):
    - NO "Stinger" transition instance is registered, and the
      obs_stinger_transition kind is NOT advertised (or, if the kind is still
      compiled in, no instance is wired). The default current transition is a
      plain cut/Fade -- never a Stinger.
    - A SetCurrentProgramScene is an INSTANTANEOUS HARD-CUT: it must not error,
      must flip the program scene, and must NOT blank the encoder. We prove
      "encoder not blanked" off ``outputTotalFrames`` GROWING across the
      switch window (the record-only encoder reports activeFps==0 even when
      healthy -- handoff #73 -- so activeFps is NOT a liveness signal here;
      outputTotalFrames is).
  PULSAR_NATIVE_STINGER == "1" / "on" / "true":
    - The #57 world: obs_stinger_transition kind present, a registered
      "Stinger" instance, transition-config requests accepted, and a program
      switch with the stinger active composites the seam without erroring and
      keeps the record output active across the (animated) switch window.

The full VISUAL proof of the OVERLAY blend mid-transition + the invisible
hard-cut skew is the M10 live probe (probe-m10-canvas-live.py, #79), out of
scope here -- this is a wiring/seam smoke runnable without Twitch or the VPS.

Exit codes: 0 pass * 1 assertion failure * 2 config error * 3 typed skip
(the build can't exercise the asserted world -- e.g. NATIVE_STINGER=1 on a
LIGHT build with no obs_stinger_transition kind).

  pip install websockets
  python scripts/probe-stinger-smoke.py                    # default: pivot (OFF)
  PULSAR_NATIVE_STINGER=1 python scripts/probe-stinger-smoke.py   # #57 world
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
from typing import Callable, Optional

try:
    import websockets
except ImportError:
    print("error: pip install websockets")
    sys.exit(2)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    REPO_ROOT / "upstream" / "build_x64" / "rundir" / "RelWithDebInfo"
    / "bin" / "64bit" / "pulsar.exe"
)
STINGER_ASSET = REPO_ROOT / "scripts" / "assets" / "stinger-demo.webm"

READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
READY_TIMEOUT_S = 60.0
EVENT_SUBSCRIPTION_ALL = 0x7FF

# The flag that gates the native stinger (#73 / #83). Default OFF == the live
# Solar/CEF pivot world; "1"/"on"/"true" == the dormant #57 world re-armed.
NATIVE_STINGER_ENV = "PULSAR_NATIVE_STINGER"


def native_stinger_enabled() -> bool:
    """True iff the dormant native stinger is armed by the flag. Default OFF."""
    return os.environ.get(NATIVE_STINGER_ENV, "").strip().lower() in (
        "1", "on", "true", "yes",
    )


class Pulsar:
    def __init__(self, exe: pathlib.Path, *, native_stinger: bool) -> None:
        self.exe = exe
        self.native_stinger = native_stinger
        self.proc: Optional[subprocess.Popen] = None
        self._lines: list[str] = []
        self._ready = threading.Event()
        self._match: Optional[re.Match[str]] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = "0"  # session-random, surfaced on PULSAR_READY
        env["PULSAR_FPS"] = "30"
        env["PULSAR_RESOLUTION"] = "1920x1080"
        # Propagate the flag's resolved value so the spawned fork sees exactly
        # the world this run asserts (a stray parent-shell value can't drift
        # the child from the probe's branch decision).
        env[NATIVE_STINGER_ENV] = "1" if self.native_stinger else "0"
        # Pin the stinger asset path LOCALLY (C-PATH): the fork reads this env,
        # never a leaf value. Harmless when the native stinger is dormant.
        env["PULSAR_STINGER_ASSET"] = str(STINGER_ASSET)
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)
        env.pop("PULSAR_PROCESS_AUDIO_NAME", None)
        env["PULSAR_RECORD_DIR"] = str(REPO_ROOT / "build" / "stinger-smoke-vod")
        (REPO_ROOT / "build" / "stinger-smoke-vod").mkdir(parents=True, exist_ok=True)

        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [str(self.exe)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, creationflags=creationflags,
            cwd=str(self.exe.parent),  # libobs resolves data/ from cwd (bin/64bit)
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self._lines.append(line)
            m = READY_RE.match(line)
            if m and not self._ready.is_set():
                self._match = m
                self._ready.set()

    def wait_ready(self) -> tuple[str, str]:
        if not self._ready.wait(timeout=READY_TIMEOUT_S):
            print("error: pulsar.exe never printed PULSAR_READY")
            print("\n".join(self._lines[-40:]))
            sys.exit(2)
        assert self._match
        return self._match.group(1), self._match.group(2)

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode()).digest()
    ).decode()
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode()).digest()
    ).decode()


class Inbox:
    def __init__(self) -> None:
        self.responses: list[dict] = []

    async def pump(self, ws, until: Callable[["Inbox"], bool], timeout: float) -> None:
        end = asyncio.get_event_loop().time() + timeout
        while not until(self):
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            msg = json.loads(raw)
            if msg.get("op") == 7:
                self.responses.append(msg["d"])


async def request(inbox: Inbox, ws, rtype: str, rid: str,
                  data: dict | None = None, timeout: float = 20.0) -> dict:
    body: dict = {"requestType": rtype, "requestId": rid}
    if data is not None:
        body["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": body}))
    await inbox.pump(ws, lambda ix: any(r["requestId"] == rid for r in ix.responses), timeout)
    return next(r for r in inbox.responses if r["requestId"] == rid)


def ok(resp: dict) -> bool:
    return bool(resp.get("requestStatus", {}).get("result"))


def resolve_exe() -> pathlib.Path:
    # --exe <path> (as run-probes.ps1 passes) > PULSAR_EXE env > default.
    argv = sys.argv[1:]
    if "--exe" in argv:
        i = argv.index("--exe")
        if i + 1 < len(argv):
            return pathlib.Path(argv[i + 1])
    return pathlib.Path(os.environ.get("PULSAR_EXE", str(DEFAULT_EXE)))


# --------------------------------------------------------------------------
# Shared: identify, create a 2nd scene, run the program switch, sample the
# encoder. Returns the (pre, post) GetStats responseData dicts + the flipped
# scene name so each world asserts its own invariants on the same evidence.
# --------------------------------------------------------------------------
async def connect_identify(ws, password: str) -> Inbox:
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    identify = {"rpcVersion": hello["d"]["rpcVersion"],
                "eventSubscriptions": EVENT_SUBSCRIPTION_ALL}
    if "authentication" in hello["d"]:
        a = hello["d"]["authentication"]
        identify["authentication"] = compute_auth(password, a["salt"], a["challenge"])
    await ws.send(json.dumps({"op": 1, "d": identify}))
    ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    if ident.get("op") != 2:
        raise RuntimeError(f"Identify rejected: {ident}")
    print("[smoke] obs-ws v5 connected + identified")
    return Inbox()


async def list_transition_state(ix: Inbox, ws) -> tuple[list[str], dict[str, dict]]:
    """Return (transition_kinds, {name: transition}) for the current fork."""
    r = await request(ix, ws, "GetTransitionKindList", "kinds")
    kinds = r.get("responseData", {}).get("transitionKinds", [])
    r = await request(ix, ws, "GetSceneTransitionList", "list")
    trs = r.get("responseData", {}).get("transitions", [])
    names = {t["transitionName"]: t for t in trs}
    return kinds, names


async def run_program_switch(ix: Inbox, ws, *, target_scene: str) -> tuple[dict, dict, str]:
    """Create target_scene, start recording, sample stats pre/post a program
    switch. Returns (pre_stats, post_stats, current_scene_after)."""
    r = await request(ix, ws, "CreateScene", "mk-b", {"sceneName": target_scene})
    if not ok(r) and r["requestStatus"].get("code") != 601:  # 601 = already exists
        raise RuntimeError(f"CreateScene: {r['requestStatus']}")

    r = await request(ix, ws, "StartRecord", "rec-on")
    rec_started = ok(r)
    print(f"[smoke] StartRecord ok={rec_started} ({r['requestStatus'].get('comment','')})")

    r = await request(ix, ws, "GetStats", "stats-pre")
    pre = r.get("responseData", {})
    print(f"[smoke] pre-switch activeFps={pre.get('activeFps', 0.0):.1f} "
          f"outputTotal={pre.get('outputTotalFrames', 0)} "
          f"outputSkipped={pre.get('outputSkippedFrames', 0)}")

    r = await request(ix, ws, "SetCurrentProgramScene", "switch",
                      {"sceneName": target_scene})
    if not ok(r):
        raise RuntimeError(f"SetCurrentProgramScene errored: {r['requestStatus']}")
    print(f"[smoke] SetCurrentProgramScene({target_scene}) accepted -- no error on switch")

    # Let frames flow across the switch window, then re-sample.
    await asyncio.sleep(1.2)
    r = await request(ix, ws, "GetStats", "stats-post")
    post = r.get("responseData", {})
    print(f"[smoke] post-switch activeFps={post.get('activeFps', 0.0):.1f} "
          f"outputTotal={post.get('outputTotalFrames', 0)} "
          f"outputSkipped={post.get('outputSkippedFrames', 0)}")

    r = await request(ix, ws, "GetCurrentProgramScene", "get-prog")
    prog = r.get("responseData", {})
    cur_scene = prog.get("currentProgramSceneName") or prog.get("sceneName")
    print(f"[smoke] current program scene after switch: {cur_scene}")

    post["_rec_started"] = rec_started  # piggyback for the caller's record check
    if rec_started:
        r = await request(ix, ws, "GetRecordStatus", "rec-st")
        post["_rec_active"] = bool(r.get("responseData", {}).get("outputActive"))
        await request(ix, ws, "StopRecord", "rec-off")
    return pre, post, cur_scene


def assert_encoder_not_blanked(pre: dict, post: dict, *, label: str) -> Optional[str]:
    """The pivot liveness assertion (handoff #73). The record-only encoder
    reports activeFps==0 even when healthy, so liveness is proven off
    outputTotalFrames GROWING across the switch window -- NOT activeFps.
    Returns an error string on failure, None on success."""
    total_pre = int(pre.get("outputTotalFrames", 0) or 0)
    total_post = int(post.get("outputTotalFrames", 0) or 0)
    rec_started = bool(post.get("_rec_started"))

    if rec_started:
        if not post.get("_rec_active"):
            return f"{label}: record output went INACTIVE across the switch (encoder blanked)"
        if total_post <= total_pre:
            return (f"{label}: outputTotalFrames did not grow across the switch "
                    f"({total_pre} -> {total_post}) -- encoder blanked (the cut "
                    "stalled the output, NOT a hard-cut)")
        skipped = max(0, int(post.get("outputSkippedFrames", 0) or 0)
                      - int(pre.get("outputSkippedFrames", 0) or 0))
        drop = skipped / max(1, total_post)
        print(f"[smoke] {label}: outputTotalFrames grew {total_pre}->{total_post} "
              f"(record active; switch-window drop~{drop:.4f}); activeFps NOT used "
              "as a liveness signal (record-only encoder reports 0 -- handoff #73)")
        if drop > 0.05:
            return f"{label}: drop ratio {drop:.4f} > 0.05 across the switch"
        return None

    # Recording declined (encoders unconfigured on this build): we cannot read
    # outputTotalFrames growth, so fall back to "the switch did not error and
    # the render thread is alive". A bare activeFps>0 is acceptable evidence
    # ONLY in this no-record fallback (not the primary signal).
    if float(post.get("activeFps", 0.0) or 0.0) <= 0.0 and total_post <= total_pre:
        return (f"{label}: recording unavailable AND no render/output progress "
                "across the switch -- cannot prove the seam stayed live")
    print(f"[smoke] {label}: recording unavailable; render/output progressed "
          "across the switch (seam OK, degraded evidence)")
    return None


# --------------------------------------------------------------------------
# World A -- PIVOT (PULSAR_NATIVE_STINGER OFF, the default).
# --------------------------------------------------------------------------
async def run_pivot_off(ix: Inbox, ws) -> int:
    print(f"[smoke] world: PIVOT ({NATIVE_STINGER_ENV} OFF) -- native stinger "
          "DORMANT; the live transition is the Solar/CEF overlay + an invisible "
          "hard-cut (this probe asserts the hard-cut, NOT the overlay -- #79).")
    kinds, names = await list_transition_state(ix, ws)
    print(f"[smoke] transition kinds: {kinds}")
    print(f"[smoke] registered transitions: {sorted(names)}")

    # ASSERT: NO Stinger instance is registered when the flag is OFF. The
    # obs_stinger_transition kind MAY still be compiled in (the code exists,
    # only dormant), but NO "Stinger" instance may be wired into the live set.
    if "Stinger" in names:
        print("FAIL: a 'Stinger' transition instance IS registered while "
              f"{NATIVE_STINGER_ENV} is OFF -- the native stinger is NOT dormant "
              "(pivot regression). Expected zero Stinger instances by default.")
        return 1
    print("[smoke] OK: no 'Stinger' transition instance registered (native "
          "stinger dormant by default -- the pivot invariant).")

    # The default current transition must be a plain cut/Fade, never a Stinger.
    r = await request(ix, ws, "GetCurrentSceneTransition", "get-tr")
    cur = r.get("responseData", {})
    print(f"[smoke] current transition: {cur.get('transitionName')} "
          f"kind={cur.get('transitionKind')} dur={cur.get('transitionDuration')}")
    if cur.get("transitionKind") == "obs_stinger_transition":
        print("FAIL: the default current transition is a stinger while the flag "
              "is OFF -- a hard-cut must not route through a stinger.")
        return 1

    # The hard-cut: a program switch must not error and must not blank the
    # encoder (proven off outputTotalFrames growth -- handoff #73).
    pre, post, cur_scene = await run_program_switch(ix, ws, target_scene="smoke-scene-b")
    if cur_scene != "smoke-scene-b":
        print(f"FAIL: program scene did not flip on the hard-cut (got {cur_scene})")
        return 1
    err = assert_encoder_not_blanked(pre, post, label="hard-cut")
    if err:
        print(f"FAIL: {err}")
        return 1

    print("\nPASS: native stinger dormant ({0} OFF); a program switch is an "
          "instantaneous HARD-CUT that does not error and does not blank the "
          "encoder (outputTotalFrames grew across the switch). The visible "
          "overlay transition is Solar/CEF -- proven by probe-m10-canvas-live "
          "(#79).".format(NATIVE_STINGER_ENV))
    return 0


# --------------------------------------------------------------------------
# World B -- #57 native stinger re-armed (PULSAR_NATIVE_STINGER=1).
# --------------------------------------------------------------------------
async def run_native_on(ix: Inbox, ws) -> int:
    print(f"[smoke] world: NATIVE STINGER ({NATIVE_STINGER_ENV}=1) -- the #57 "
          "OBS-native compositing path re-armed by the flag.")
    kinds, names = await list_transition_state(ix, ws)
    print(f"[smoke] transition kinds: {kinds}")
    print(f"[smoke] registered transitions: {sorted(names)}")

    if "obs_stinger_transition" not in kinds:
        print("SKIP(3): obs_stinger_transition kind absent -> not a full build; "
              f"cannot exercise the {NATIVE_STINGER_ENV}=1 world here.")
        return 3
    if "Stinger" not in names:
        print(f"FAIL: {NATIVE_STINGER_ENV}=1 but no registered 'Stinger' transition "
              "instance (#57 part 1) -- the flag did not arm the native stinger.")
        return 1
    if names["Stinger"]["transitionKind"] != "obs_stinger_transition":
        print(f"FAIL: 'Stinger' kind = {names['Stinger']['transitionKind']}")
        return 1
    if "Fade" not in names:
        print("FAIL: fade fallback transition missing")
        return 1

    # Configure the stinger over routed obs-ws requests (no media path on the
    # wire -- pinned locally by the fork; obs-ws sends only transition_point).
    r = await request(ix, ws, "SetCurrentSceneTransition", "set-tr",
                      {"transitionName": "Stinger"})
    if not ok(r):
        print(f"FAIL: SetCurrentSceneTransition(Stinger): {r['requestStatus']}")
        return 1
    r = await request(ix, ws, "SetCurrentSceneTransitionDuration", "set-dur",
                      {"transitionDuration": 600})
    if not ok(r):
        print(f"FAIL: SetCurrentSceneTransitionDuration: {r['requestStatus']}")
        return 1
    r = await request(ix, ws, "SetCurrentSceneTransitionSettings", "set-set",
                      {"transitionSettings": {"transition_point": 300, "tp_type": 0}})
    if not ok(r):
        print(f"FAIL: SetCurrentSceneTransitionSettings: {r['requestStatus']}")
        return 1
    r = await request(ix, ws, "GetCurrentSceneTransition", "get-tr")
    cur = r.get("responseData", {})
    print(f"[smoke] current transition: {cur.get('transitionName')} "
          f"kind={cur.get('transitionKind')} dur={cur.get('transitionDuration')}")
    if cur.get("transitionName") != "Stinger":
        print("FAIL: current transition is not Stinger after set")
        return 1

    # Program switch with the stinger active: must composite without erroring
    # and keep the record output alive across the animated window.
    pre, post, cur_scene = await run_program_switch(ix, ws, target_scene="smoke-scene-b")
    if cur_scene != "smoke-scene-b":
        print(f"FAIL: program scene did not flip (got {cur_scene})")
        return 1
    err = assert_encoder_not_blanked(pre, post, label="stinger-switch")
    if err:
        print(f"FAIL: {err}")
        return 1

    print("\nPASS: stinger registered + configured; program switch composites the "
          "stinger seam without erroring or blanking the encoder.")
    return 0


async def run() -> int:
    exe = resolve_exe()
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}; build with scripts/build-win.ps1 -Full")
        return 2
    if not STINGER_ASSET.exists():
        print(f"error: stinger asset missing at {STINGER_ASSET}")
        return 2

    native = native_stinger_enabled()
    print(f"[smoke] {NATIVE_STINGER_ENV}={os.environ.get(NATIVE_STINGER_ENV, '<unset>')!r} "
          f"-> asserting the {'NATIVE-STINGER (#57)' if native else 'PIVOT (hard-cut)'} world")

    pulsar = Pulsar(exe, native_stinger=native)
    pulsar.spawn()
    ws_url, password = pulsar.wait_ready()
    print(f"[smoke] PULSAR_READY ws={ws_url} password=<redacted>")

    try:
        async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
            ix = await connect_identify(ws, password)
            if native:
                return await run_native_on(ix, ws)
            return await run_pivot_off(ix, ws)
    finally:
        pulsar.stop()


def main() -> int:
    try:
        return asyncio.run(run())
    except asyncio.TimeoutError:
        print("FAIL: obs-ws request timed out")
        return 1
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
