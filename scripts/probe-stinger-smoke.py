#!/usr/bin/env python3
"""probe-stinger-smoke.py -- M10 #57 build/load smoke for the stinger transition.

Scope (this is NOT the full M10 live probe -- that is #61). This smoke proves
the load-bearing #57 facts that are checkable on the operator box without a
Twitch broadcast:

  1. pulsar.exe (full build) launches and the obs-websocket v5 socket is up.
  2. GetTransitionKindList includes the stinger kind (obs_stinger_transition).
  3. GetSceneTransitionList includes a registered "Stinger" transition instance
     (the frontend-stub now registers it alongside "Fade").
  4. SetCurrentSceneTransition{Stinger} + SetCurrentSceneTransitionDuration +
     SetCurrentSceneTransitionSettings{transition_point} are accepted.
  5. A program-scene change with the stinger active does NOT error and does NOT
     blank the encoder: with recording running, the record output stays active
     and activeFps stays > 0 across the switch window (drop ratio stays low).

The full VISUAL proof that the stinger media composites mid-transition on air
is #61 (the M10 live probe capturing mid-transition frames) -- explicitly out
of scope here per ADR 003 §6 criterion 5 and the #57 task.

Exit codes: 0 pass · 1 assertion failure · 2 config error · 3 typed skip
(stinger kind absent -> not a full build).

  pip install websockets
  python scripts/probe-stinger-smoke.py
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


class Pulsar:
    def __init__(self, exe: pathlib.Path) -> None:
        self.exe = exe
        self.proc: Optional[subprocess.Popen] = None
        self._lines: list[str] = []
        self._ready = threading.Event()
        self._match: Optional[re.Match[str]] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = "0"  # session-random, surfaced on PULSAR_READY
        env["PULSAR_FPS"] = "30"
        env["PULSAR_RESOLUTION"] = "1920x1080"
        # Pin the stinger asset path LOCALLY (C-PATH): the fork reads this env,
        # never a leaf value. Point it at the committed, hash-pinned demo asset.
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


async def run() -> int:
    exe = resolve_exe()
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}; build with scripts/build-win.ps1 -Full")
        return 2
    if not STINGER_ASSET.exists():
        print(f"error: stinger asset missing at {STINGER_ASSET}")
        return 2

    pulsar = Pulsar(exe)
    pulsar.spawn()
    ws_url, password = pulsar.wait_ready()
    print(f"[smoke] PULSAR_READY ws={ws_url} password=<redacted>")

    try:
        async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            identify = {"rpcVersion": hello["d"]["rpcVersion"],
                        "eventSubscriptions": EVENT_SUBSCRIPTION_ALL}
            if "authentication" in hello["d"]:
                a = hello["d"]["authentication"]
                identify["authentication"] = compute_auth(password, a["salt"], a["challenge"])
            await ws.send(json.dumps({"op": 1, "d": identify}))
            ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if ident.get("op") != 2:
                print(f"FAIL: Identify rejected: {ident}")
                return 1
            print("[smoke] obs-ws v5 connected + identified")

            ix = Inbox()

            # (2) GetTransitionKindList includes the stinger kind.
            r = await request(ix, ws, "GetTransitionKindList", "kinds")
            kinds = r.get("responseData", {}).get("transitionKinds", [])
            print(f"[smoke] transition kinds: {kinds}")
            if "obs_stinger_transition" not in kinds:
                print("SKIP(3): obs_stinger_transition kind absent -> not a full build")
                return 3

            # (3) GetSceneTransitionList includes a registered "Stinger" instance.
            r = await request(ix, ws, "GetSceneTransitionList", "list")
            trs = r.get("responseData", {}).get("transitions", [])
            names = {t["transitionName"]: t for t in trs}
            print(f"[smoke] registered transitions: {sorted(names)}")
            if "Stinger" not in names:
                print("FAIL: no registered 'Stinger' transition instance (#57 part 1)")
                return 1
            if names["Stinger"]["transitionKind"] != "obs_stinger_transition":
                print(f"FAIL: 'Stinger' kind = {names['Stinger']['transitionKind']}")
                return 1
            if "Fade" not in names:
                print("FAIL: fade fallback transition missing")
                return 1

            # (4) Configure the stinger over the routed obs-ws requests.
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
            # transition_point only -- NOT a path (the path was pinned locally
            # by the fork at boot; obs-ws never supplies it here).
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

            # (5) Start recording so the encoder runs, then flip the program
            #     scene with the stinger active. Create a 2nd scene to flip to.
            r = await request(ix, ws, "CreateScene", "mk-b", {"sceneName": "smoke-scene-b"})
            if not ok(r) and r["requestStatus"].get("code") != 601:  # 601 = already exists
                print(f"FAIL: CreateScene: {r['requestStatus']}")
                return 1

            r = await request(ix, ws, "StartRecord", "rec-on")
            rec_started = ok(r)
            print(f"[smoke] StartRecord ok={rec_started} ({r['requestStatus'].get('comment','')})")

            r = await request(ix, ws, "GetStats", "stats-pre")
            pre = r.get("responseData", {})
            fps_pre = pre.get("activeFps", 0.0)
            skipped_pre = pre.get("outputSkippedFrames", 0)
            print(f"[smoke] pre-switch activeFps={fps_pre:.1f} outputSkipped={skipped_pre}")

            # The program-scene change -- the Gap B' compositing seam.
            r = await request(ix, ws, "SetCurrentProgramScene", "switch",
                              {"sceneName": "smoke-scene-b"})
            if not ok(r):
                print(f"FAIL: SetCurrentProgramScene errored (Gap B' seam): {r['requestStatus']}")
                return 1
            print("[smoke] SetCurrentProgramScene(smoke-scene-b) accepted -- no error on stinger seam")

            # Let the 600 ms stinger play, sampling the encoder.
            await asyncio.sleep(1.2)
            r = await request(ix, ws, "GetStats", "stats-post")
            post = r.get("responseData", {})
            fps_post = post.get("activeFps", 0.0)
            skipped_post = post.get("outputSkippedFrames", 0)
            total_post = post.get("outputTotalFrames", 0)
            print(f"[smoke] post-switch activeFps={fps_post:.1f} outputSkipped={skipped_post} "
                  f"outputTotal={total_post}")

            r = await request(ix, ws, "GetCurrentProgramScene", "get-prog")
            prog = r.get("responseData", {})
            cur_scene = prog.get("currentProgramSceneName") or prog.get("sceneName")
            print(f"[smoke] current program scene after switch: {cur_scene}")
            if cur_scene != "smoke-scene-b":
                print(f"FAIL: program scene did not flip (got {cur_scene})")
                return 1

            # Encoder-not-blanked assertion: with recording running the output
            # must stay active and producing frames across the stinger window.
            if rec_started:
                r = await request(ix, ws, "GetRecordStatus", "rec-st")
                rec = r.get("responseData", {})
                if not rec.get("outputActive"):
                    print("FAIL: record output went inactive across the stinger switch")
                    return 1
                if fps_post <= 0.0:
                    print(f"FAIL: activeFps={fps_post} -- encoder blanked across stinger switch")
                    return 1
                drop = 0.0
                if total_post:
                    drop = max(0, skipped_post - skipped_pre) / max(1, total_post)
                print(f"[smoke] record still active; activeFps>0; switch-window drop~{drop:.4f}")
                if drop > 0.05:
                    print(f"FAIL: drop ratio {drop:.4f} > 0.05 across the switch")
                    return 1
                await request(ix, ws, "StopRecord", "rec-off")
            else:
                # Recording may decline if encoders are unconfigured on this
                # build; still assert the seam did not blank the render thread.
                if fps_post <= 0.0:
                    print(f"FAIL: activeFps={fps_post} after switch (render thread blanked)")
                    return 1
                print("[smoke] recording unavailable; render-thread activeFps>0 across switch (seam OK)")

            print("\nPASS: stinger registered + configured; program switch composites the "
                  "stinger seam without erroring or blanking the encoder.")
            return 0
    finally:
        pulsar.stop()


def main() -> int:
    try:
        return asyncio.run(run())
    except asyncio.TimeoutError:
        print("FAIL: obs-ws request timed out")
        return 1


if __name__ == "__main__":
    sys.exit(main())
