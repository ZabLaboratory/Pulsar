#!/usr/bin/env python3
"""
Pulsar `webpage_control_level` + browser-source lifecycle probe — issue #158 /
ADR Prism 028 §3.2.

The lint gate (scripts/check-webpage-control-level.py) proves the SOURCE pins
the level everywhere. This probe proves the RUNTIME honours it, from the only
vantage point that matters: inside the page.

It serves a local page that calls `window.obsstudio` at load and reports what
it got back over HTTP, then drives a real CEF browser source at that page
through BOTH creation paths and asserts, on each, that the page is sandboxed.

Resolution criterion 2 — "a third-party page can no longer reach OBS state at
the level retained (test or documented manual proof)" — is this probe.
Criterion 3's lifecycle statement is its second half.

Assertions
----------
A. v5 `CreateInput(browser_source)` with NO `webpage_control_level` in the
   request: the page reports `getControlLevel() == 0` (None) and gets NOTHING
   from `getStatus` / `getCurrentScene` / `getScenes` — the exact three calls
   that used to hand a partner overlay this process's streaming state, scene
   list and current scene.
B. The same request ASKING for level 5 (`All`): still pinned to 0. The pin
   overrides the wire, it does not merely fill a gap — a page cannot be handed
   OBS control by whoever creates it.
C. `pulsar-scene:SetCaptureSource` — the path Prism actually uses to arm the
   antenna: still 0.
D. LIFECYCLE (D2 of the report, resolved here, not separately):
   D1. A browser source KEEPS its JS state across a program-scene change — the
       page keeps beating while it is the active capture source. That is
       deliberate: Pulsar is scene-agnostic, scene changes compose inside the
       page, and tearing CEF down on every cut would blank the antenna.
   D2. A browser source is DESTROYED, never parked, when the capture source is
       swapped — including when it was left behind on a scene the operator has
       since left. Its beats stop; the replacement's continue. Before #158 the
       sweep only visited the current frontend scene, so a stranded page kept
       running in this process indefinitely.

A page cannot be trusted to report honestly about itself, which is why every
assertion here is NEGATIVE in the direction that matters: a page that lies
about being sandboxed can only claim MORE access, and claiming more fails.

LICENSE INVARIANT (LICENSE-INVARIANTS.md #1/#2/#3): WebSocket + HTTP process
boundary only. No FFI, no ctypes, no LoadLibrary — same posture as
probe-browser-m3.py, which this probe borrows its plumbing from.

Exit codes: 0 pass · 1 fail · 2 usage/env · 3 typed skip (light build, no CEF).

Usage:
    pip install websockets
    python scripts/probe-webpage-control-level.py
    python scripts/probe-webpage-control-level.py --exe /path/to/pulsar.exe
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import http.server
import json
import os
import pathlib
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from typing import Callable, Optional

try:
    import websockets
except ImportError:
    print("error: pip install websockets (pure WS client — no native deps)")
    sys.exit(2)


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    REPO_ROOT / "upstream" / "build_x64" / "rundir" / "RelWithDebInfo"
    / "bin" / "64bit" / "pulsar.exe"
)

READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0
EVENT_SUBSCRIPTION_ALL = 0x7FF

INPUT_KIND = "browser_source"
CANVAS_W, CANVAS_H = 640, 360

# ControlLevel::None (plugins/pulsar-browser/obs-browser-source.hpp). The lint
# gate pins the C++ side to this same ordinal.
CONTROL_LEVEL_NONE = 0
# ControlLevel::All — what assertion B asks for and must NOT get.
CONTROL_LEVEL_ALL = 5

# How long to wait for a page to load, run its probes and report back. CEF's
# first paint is async and the render subprocess has to spin up first.
REPORT_DEADLINE_S = 45.0
# The page gives each obsstudio call this long to answer before recording it as
# "never answered". Generous: a callback that has not fired in 3 s under a
# levelled-down source is not going to.
PAGE_CALL_TIMEOUT_MS = 3000
BEAT_INTERVAL_MS = 400
# After a swap, how long a retired page is allowed to keep beating before we
# call it stranded. libobs may defer the destroy to a later tick, so this is
# not zero — but it is far short of "forever", which is what the bug was.
RETIRE_GRACE_S = 12.0
# ... and how long the replacement must be seen beating to prove the probe is
# measuring a live wire rather than a dead http.server.
LIVE_CONFIRM_S = 6.0


PAGE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>wcl probe</title>
<style>html,body{margin:0;height:100%%;background:#101820;color:#7ef}</style>
</head><body>
<div style="font:48px sans-serif;padding:40px">WCL PROBE %(id)s</div>
<script>
(function () {
  var ID = "%(id)s";
  var TIMEOUT = %(timeout)d;

  function beat() { fetch("/beat?id=" + ID).catch(function () {}); }
  setInterval(beat, %(beat)d);
  beat();

  // Ask obsstudio for one thing, resolve with {fired, value} or {fired:false}
  // after TIMEOUT. A level that refuses answers either by never calling back
  // or by calling back with null — both are "no data", and both pass.
  function ask(name) {
    return new Promise(function (resolve) {
      var settled = false;
      var t = setTimeout(function () {
        if (!settled) { settled = true; resolve({ fired: false, value: null }); }
      }, TIMEOUT);
      try {
        if (!window.obsstudio || typeof window.obsstudio[name] !== "function") {
          clearTimeout(t); settled = true;
          resolve({ fired: false, value: null, missing: true });
          return;
        }
        window.obsstudio[name](function (v) {
          if (settled) return;
          settled = true; clearTimeout(t);
          resolve({ fired: true, value: v === undefined ? null : v });
        });
      } catch (e) {
        if (!settled) { settled = true; clearTimeout(t);
          resolve({ fired: false, value: null, threw: String(e) }); }
      }
    });
  }

  var names = ["getControlLevel", "getStatus", "getCurrentScene", "getScenes",
               "getTransitions", "getCurrentTransition"];
  Promise.all(names.map(ask)).then(function (results) {
    var payload = { id: ID, hasObsStudio: !!window.obsstudio, calls: {} };
    names.forEach(function (n, i) { payload.calls[n] = results[i]; });
    fetch("/report?id=" + ID + "&payload=" +
          encodeURIComponent(JSON.stringify(payload))).catch(function () {});
  });
})();
</script></body></html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    store: "PageStore" = None  # type: ignore[assignment]

    def do_GET(self) -> None:  # noqa: N802 — http.server contract
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        page_id = (qs.get("id") or [""])[0]

        if parsed.path in ("/", "/page.html"):
            body = (PAGE_HTML % {
                "id": page_id,
                "timeout": PAGE_CALL_TIMEOUT_MS,
                "beat": BEAT_INTERVAL_MS,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/beat":
            self.store.beat(page_id)
            self._ok()
            return

        if parsed.path == "/report":
            raw = (qs.get("payload") or ["{}"])[0]
            try:
                self.store.report(page_id, json.loads(raw))
            except json.JSONDecodeError:
                self.store.report(page_id, {"_undecodable": raw[:400]})
            self._ok()
            return

        self.send_error(404, "not found")

    def _ok(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def log_message(self, *args) -> None:
        return


class PageStore:
    """Thread-safe collection point for what the pages report."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reports: dict[str, dict] = {}
        self.last_beat: dict[str, float] = {}
        self.beat_count: dict[str, int] = {}

    def beat(self, page_id: str) -> None:
        with self._lock:
            self.last_beat[page_id] = time.monotonic()
            self.beat_count[page_id] = self.beat_count.get(page_id, 0) + 1

    def report(self, page_id: str, payload: dict) -> None:
        with self._lock:
            self.reports[page_id] = payload

    def get_report(self, page_id: str) -> Optional[dict]:
        with self._lock:
            return self.reports.get(page_id)

    def beat_since(self, page_id: str, since: float) -> bool:
        """True if the page has beaten at least once after the monotonic
        instant `since` — i.e. it is still alive."""
        with self._lock:
            last = self.last_beat.get(page_id)
        return last is not None and last >= since

    def last_beat_age(self, page_id: str) -> Optional[float]:
        with self._lock:
            last = self.last_beat.get(page_id)
        return None if last is None else time.monotonic() - last


class LocalPageServer:
    def __init__(self, store: PageStore) -> None:
        _Handler.store = store
        self.httpd: Optional[http.server.ThreadingHTTPServer] = None
        self.port = 0

    def start(self) -> None:
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url_for(self, page_id: str) -> str:
        return f"http://127.0.0.1:{self.port}/page.html?id={page_id}"

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None


class PulsarProcess:
    """Spawn pulsar.exe, parse the READY sentinel, reap it. Mirrors
    probe-browser-m3.py."""

    def __init__(self, exe: pathlib.Path, port: int, password: str) -> None:
        self.exe, self.port, self.password = exe, port, password
        self.proc = None
        self._lines: list[str] = []
        self._ready = threading.Event()
        self._match: Optional[re.Match[str]] = None

    def spawn(self) -> None:
        import subprocess

        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)

        self.proc = subprocess.Popen(
            [str(self.exe)],
            cwd=str(self.exe.parent),  # libobs resolves data/ from cwd
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            self._lines.append(line)
            m = READY_RE.match(line)
            if m and not self._ready.is_set():
                self._match = m
                self._ready.set()

    def wait_ready(self, timeout: float) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        while True:
            if self._ready.wait(timeout=0.2):
                assert self._match is not None
                return self._match.group(1), self._match.group(2)
            assert self.proc is not None
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"pulsar.exe exited (code {self.proc.returncode}) before READY.\n"
                    + self.diag()
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"pulsar.exe did not signal READY within {timeout:.0f}s.\n" + self.diag()
                )

    def diag(self) -> str:
        tail = self._lines[-40:]
        body = "\n".join(f"  | {ln}" for ln in tail) if tail else "  | (no output)"
        return f"--- pulsar stdout/stderr (last {len(tail)} lines) ---\n{body}"

    def shutdown(self, grace: float = SHUTDOWN_GRACE_S) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        for step in (self.proc.terminate, self.proc.kill):
            try:
                step()
                self.proc.wait(timeout=grace)
                return
            except Exception:
                continue


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class Inbox:
    def __init__(self) -> None:
        self.responses: list[dict] = []

    async def pump(self, ws, until: Callable[["Inbox"], bool], timeout: float) -> None:
        end = asyncio.get_event_loop().time() + timeout
        while not until(self):
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
            if msg.get("op") == 7:
                self.responses.append(msg["d"])


async def request(inbox: Inbox, ws, request_type: str, request_id: str,
                  data: dict | None = None, timeout: float = 20.0) -> dict:
    body: dict = {"requestType": request_type, "requestId": request_id}
    if data is not None:
        body["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": body}))

    def has(ix: Inbox) -> bool:
        return any(r["requestId"] == request_id for r in ix.responses)

    await inbox.pump(ws, has, timeout)
    for i, r in enumerate(inbox.responses):
        if r["requestId"] == request_id:
            return inbox.responses.pop(i)
    raise RuntimeError("unreachable")


async def vendor(inbox: Inbox, ws, vendor_name: str, request_type: str,
                 request_id: str, data: dict) -> dict:
    r = await request(inbox, ws, "CallVendorRequest", request_id, {
        "vendorName": vendor_name,
        "requestType": request_type,
        "requestData": data,
    })
    if not r["requestStatus"]["result"]:
        return {"_error": r["requestStatus"]}
    return r["responseData"].get("responseData", {})


async def await_report(store: PageStore, page_id: str, deadline_s: float) -> Optional[dict]:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        rep = store.get_report(page_id)
        if rep is not None:
            return rep
        await asyncio.sleep(0.25)
    return None


# --------------------------------------------------------------------------
# The assertion that matters: what did the page actually get?
# --------------------------------------------------------------------------
# Calls that must yield NO data at ControlLevel::None. Each is a concrete
# capability the constat named: OBS state reachable from a third-party page.
SILENCED_CALLS = ("getStatus", "getCurrentScene", "getScenes",
                  "getTransitions", "getCurrentTransition")


def judge_report(label: str, rep: dict) -> list[str]:
    """Return the list of failures. Empty list = the page is sandboxed."""
    problems: list[str] = []

    if not rep.get("hasObsStudio"):
        # Not a pass: `window.obsstudio` absent would mean the page never ran
        # under CEF at all, and every "no data" below would be vacuous.
        problems.append(
            f"{label}: the page reports no `window.obsstudio` object at all — "
            "it did not run inside a CEF browser source, so this run proves "
            "nothing. Investigate the page load, do not read it as a pass."
        )
        return problems

    calls = rep.get("calls") or {}

    lvl = calls.get("getControlLevel") or {}
    if not lvl.get("fired"):
        problems.append(
            f"{label}: getControlLevel never answered. ControlLevel::None still "
            "answers it (browser-client.cpp) — that is how a page learns it is "
            "sandboxed instead of hanging. No answer means an unexpected state."
        )
    elif lvl.get("value") != CONTROL_LEVEL_NONE:
        problems.append(
            f"{label}: the page sees webpage_control_level={lvl.get('value')!r}, "
            f"expected {CONTROL_LEVEL_NONE} (None). The source was created "
            "without the pin, or the pin was overridden."
        )

    for name in SILENCED_CALLS:
        call = calls.get(name) or {}
        value = call.get("value")
        if value is not None:
            problems.append(
                f"{label}: `window.obsstudio.{name}()` returned {value!r} — a "
                "third-party page is reading this process's OBS state (#158)."
            )
    return problems


async def run(url: str, password: str, server: LocalPageServer, store: PageStore) -> int:
    failures: list[str] = []

    async with websockets.connect(url, subprotocols=["obswebsocket.json"],
                                  open_timeout=10) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if hello.get("op") != 0:
            print(f"error: expected Hello (op=0), got {hello}")
            return 1
        identify: dict = {"rpcVersion": hello["d"]["rpcVersion"],
                          "eventSubscriptions": EVENT_SUBSCRIPTION_ALL}
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify["authentication"] = compute_auth(password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify}))
        ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ident.get("op") != 2:
            print(f"error: identify failed: {ident}")
            return 1
        print("identified")

        inbox = Inbox()

        r = await request(inbox, ws, "GetInputKindList", "kinds", {})
        if INPUT_KIND not in set(r["responseData"]["inputKinds"]):
            print(
                f"SKIP: input kind {INPUT_KIND!r} is NOT registered — this is a "
                "LIGHT build (no CEF). The pin is source-gated regardless by "
                "scripts/check-webpage-control-level.py in the lint job; the "
                "RUNTIME half needs a -Full build. Typed skip, NOT a pass."
            )
            return 3
        print("browser_source registered")

        base_scene = "probe-wcl-base"
        r = await request(inbox, ws, "CreateScene", "cs0", {"sceneName": base_scene})
        if not r["requestStatus"]["result"]:
            print(f"error: CreateScene declined: {r['requestStatus']}")
            return 1
        await request(inbox, ws, "SetCurrentProgramScene", "sps0", {"sceneName": base_scene})

        # ---- A + B : the v5 CreateInput path -----------------------------
        for label, asked, page_id, input_name in (
            ("A (CreateInput, no level asked)", None, "A", "probe-wcl-a"),
            (f"B (CreateInput asking level {CONTROL_LEVEL_ALL}=All)",
             CONTROL_LEVEL_ALL, "B", "probe-wcl-b"),
        ):
            settings = {
                "url": server.url_for(page_id),
                "is_local_file": False,
                "width": CANVAS_W, "height": CANVAS_H,
                "fps_custom": True, "fps": 15,
                "shutdown": False, "restart_when_active": False,
                "reroute_audio": False,
            }
            if asked is not None:
                settings["webpage_control_level"] = asked

            print(f"\n-> {label}")
            r = await request(inbox, ws, "CreateInput", f"ci-{page_id}", {
                "sceneName": base_scene,
                "inputName": input_name,
                "inputKind": INPUT_KIND,
                "inputSettings": settings,
                "sceneItemEnabled": True,
            })
            if not r["requestStatus"]["result"]:
                print(f"error: CreateInput declined: {r['requestStatus']}")
                return 1

            rep = await await_report(store, page_id, REPORT_DEADLINE_S)
            if rep is None:
                failures.append(
                    f"{label}: the page never reported back within "
                    f"{REPORT_DEADLINE_S:.0f}s. CEF did not load it, or the local "
                    "http.server was unreachable — no verdict either way."
                )
            else:
                print(f"   page report: {json.dumps(rep.get('calls', {}), sort_keys=True)}")
                problems = judge_report(label, rep)
                failures += problems
                if not problems:
                    print(f"   {label}: sandboxed (level {CONTROL_LEVEL_NONE}, no OBS state)")

            await request(inbox, ws, "RemoveInput", f"ri-{page_id}",
                          {"inputName": input_name})

        # ---- C + D : the pulsar-scene:SetCaptureSource path ---------------
        print("\n-> C (pulsar-scene:SetCaptureSource)")
        res = await vendor(inbox, ws, "pulsar-scene", "SetCaptureSource", "sc-c", {
            "kind": "browser_source",
            "url": server.url_for("C"),
            "width": CANVAS_W, "height": CANVAS_H, "fps": 15,
        })
        if "_error" in res or res.get("error"):
            print(f"error: SetCaptureSource declined: {res}")
            return 1

        rep = await await_report(store, "C", REPORT_DEADLINE_S)
        if rep is None:
            failures.append(
                f"C: the SetCaptureSource page never reported back within "
                f"{REPORT_DEADLINE_S:.0f}s — no verdict."
            )
        else:
            print(f"   page report: {json.dumps(rep.get('calls', {}), sort_keys=True)}")
            failures += judge_report("C (SetCaptureSource)", rep)

        # D1 — the page survives a PROGRAM-SCENE CHANGE with its JS state.
        # It is the active capture source and Pulsar composes scene changes
        # inside it; tearing it down here would blank the antenna.
        other_scene = "probe-wcl-other"
        await request(inbox, ws, "CreateScene", "cs1", {"sceneName": other_scene})
        await request(inbox, ws, "SetCurrentProgramScene", "sps1",
                      {"sceneName": other_scene})
        print(f"\n-> D1: program scene -> {other_scene!r}; page C must keep running")
        mark = time.monotonic()
        await asyncio.sleep(LIVE_CONFIRM_S)
        if not store.beat_since("C", mark) or (store.last_beat_age("C") or 99) > 3.0:
            failures.append(
                "D1: page C stopped beating after a program-scene change. The "
                "active capture source must KEEP its JS state across a cut "
                "(shutdown=false) — losing it blanks the antenna on every "
                "scene change."
            )
        else:
            print("   D1 OK: the active capture page kept running across the cut")

        # D2 — swapping the capture source retires the previous page EVEN
        # THOUGH it sits on a scene we have left. This is the regression test
        # for the single-scene sweep.
        print("\n-> D2: SetCaptureSource -> page E (page C is stranded on "
              f"{base_scene!r}); page C must DIE")
        res = await vendor(inbox, ws, "pulsar-scene", "SetCaptureSource", "sc-e", {
            "kind": "browser_source",
            "url": server.url_for("E"),
            "width": CANVAS_W, "height": CANVAS_H, "fps": 15,
        })
        if "_error" in res or res.get("error"):
            print(f"error: second SetCaptureSource declined: {res}")
            return 1
        print(f"   removed_prior={res.get('removed_prior')}")

        swap = time.monotonic()
        await asyncio.sleep(RETIRE_GRACE_S)
        # Silence is measured at the END of the grace window: the page must have
        # sent nothing for several beat intervals, not merely have slowed down.
        c_age = store.last_beat_age("C")
        quiet_for = RETIRE_GRACE_S / 3.0
        if c_age is None or c_age < quiet_for:
            failures.append(
                f"D2: page C is STILL beating {RETIRE_GRACE_S:.0f}s after the "
                f"capture source was swapped (last beat {c_age}s ago, expected "
                f"silence for >= {quiet_for:.0f}s). A retired third-party page "
                "is still running in this process, keeping its JS state — that "
                "is exactly the stranding #158 closes. Check that the sweep "
                "visits EVERY scene, not just the current frontend one."
            )
        else:
            print(f"   D2 OK: page C fell silent (last beat {c_age:.1f}s ago)")

        # ... and the live one really is live, so D2 is not passing because
        # the http.server died.
        if not store.beat_since("E", swap):
            failures.append(
                "D2 control: the REPLACEMENT page E never beat either. The "
                "silence of page C therefore proves nothing about the sweep — "
                "diagnose the page load before reading D2."
            )
        else:
            print("   D2 control OK: the replacement page is beating")

        failures += judge_report("E (post-swap SetCaptureSource)",
                                 await await_report(store, "E", REPORT_DEADLINE_S) or
                                 {"hasObsStudio": False})

        await ws.close(code=1000, reason="wcl probe complete")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nOK — a third-party page loaded in a Pulsar browser source sees")
    print("     webpage_control_level=None on every creation path, reads no OBS")
    print("     state, keeps its JS across a program-scene change, and is")
    print("     destroyed (not parked) when the capture source is swapped.")
    return 0


def pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar webpage_control_level probe (#158)")
    ap.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE,
                    help="path to pulsar.exe (default: built rundir)")
    ap.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S)
    args = ap.parse_args()

    exe: pathlib.Path = args.exe
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        print("Build it first: scripts/build-win.ps1 -Full")
        return 2

    store = PageStore()
    server = LocalPageServer(store)
    server.start()
    print(f"local page server on 127.0.0.1:{server.port}")

    port = pick_free_port()
    password = secrets.token_urlsafe(16)
    pulsar = PulsarProcess(exe, port, password)
    rc = 1
    try:
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        rc = asyncio.run(run(ws_url, sentinel_pw, server, store))
    except KeyboardInterrupt:
        rc = 130
    except Exception as exc:  # noqa: BLE001 — top-level probe diagnostic
        print(f"FAIL: {exc}")
        if pulsar.proc is not None:
            print(pulsar.diag())
        rc = 1
    finally:
        pulsar.shutdown()
        if pulsar.proc is not None and pulsar.proc.poll() is None:
            print("error: pulsar.exe still running after shutdown attempt")
            rc = rc or 1
        else:
            print("pulsar.exe reaped cleanly")
        server.stop()

    print("PASS" if rc == 0 else (f"SKIPPED (exit {rc})" if rc == 3 else f"FAILED (exit {rc})"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
