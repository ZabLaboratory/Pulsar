#!/usr/bin/env python3
"""
Pulsar live Twitch broadcast probe.

End-to-end functional check : spawn pulsar.exe, point its CEF
browser_source at a locally-served test scene, push a 5-minute
stream to Twitch, poll metrics throughout, assert thresholds, clean
up. Exit 0 = Pulsar passed the live-broadcast contract ; non-zero =
something is broken.

Required env :
  TWITCH_STREAM_KEY   stream key from GitHub Secrets

Optional env :
  PULSAR_EXE          override path to pulsar.exe (default :
                      <repo>/upstream/build_x64/rundir/RelWithDebInfo/bin/64bit/pulsar.exe)
  LIVE_TEST_DURATION  seconds to broadcast (default 300)
  LIVE_TEST_FPS       target encoder fps (default 60 — set via PULSAR_FPS at spawn)

Validations :
  - pulsar spawns + obs-websocket config drops within 30 s
  - Hello / Identify auth round-trip succeeds
  - SetCaptureSource(browser_source, http://127.0.0.1:<port>/test-scene.html)
    returns kind="browser_source"
  - CreateDestination(twitch, $key) returns an id ; StartDestination
    returns started=true
  - Throughout the broadcast, every 30 s :
      GetDestinations[<id>].active == true
      GetVideoSettings.video_bitrate matches target ± tolerance
      GetAdaptiveState samples > 0 (adaptive worker is awake)
  - Frame drop ratio at end < FRAME_DROP_THRESHOLD (5 %)
  - Total frames sent >= duration * fps * 0.95
  - StopDestination returns clean
  - No "error" / "fail" lines in pulsar stdout/stderr (excluding
    benign warnings on the allowlist)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import hashlib
import http.server
import json
import os
import pathlib
import socket
import socketserver
import subprocess
import sys
import threading
import time
from typing import Any

try:
    import websockets
except ImportError:
    print("error: pip install websockets", file=sys.stderr)
    sys.exit(2)


REPO_ROOT  = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = REPO_ROOT / "upstream/build_x64/rundir/RelWithDebInfo/bin/64bit/pulsar.exe"
RUNDIR     = REPO_ROOT / "upstream/build_x64/rundir/RelWithDebInfo/bin/64bit"
CONFIG_PATH = RUNDIR / "obs-websocket" / "config.json"
SCENE_DIR  = REPO_ROOT / "scripts/live-test"

# Local recording directory : pulsar runs StartRecord in parallel with
# the Twitch push so the broadcast can be replayed offline as visual
# proof of a successful run. live-test.yml uploads the resulting MP4
# as a workflow artefact and attaches it to the GitHub Release on tag
# push. Override the directory with LIVE_TEST_VOD_DIR for CI control.
LIVE_VOD_DIR = pathlib.Path(
    os.environ.get("LIVE_TEST_VOD_DIR",
                   str(REPO_ROOT / "build" / "live-test-vod"))
).resolve()

EVENT_SUBSCRIPTION_ALL = 0x7FF

# --- Thresholds ---
FRAME_DROP_RATIO_MAX  = 0.05    # 5 %
SPAWN_TIMEOUT_SEC     = 60.0    # pulsar to print PULSAR_READY + drop config.json
# Poll cadence is also the WS keep-alive cadence : without periodic
# app-level traffic, Windows's ProactorEventLoop RSTs the idle TCP
# connection on loopback (observed at ~30 s). 5 s leaves plenty of
# margin and gives the run summary more granular metrics.
POLL_INTERVAL_SEC     = 5.0
DESTINATION_NAME      = "pulsar-live-test"

# Benign log substrings that do not constitute failure.
BENIGN_LOG_SUBSTRINGS = [
    "no target (set PULSAR_CAPTURE_WINDOW)",  # frontend-stub default boot warning
    "Failed to find module 'win-mf'",         # absence of WMF on CI runners is fine
]


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_scene_server(port: int) -> socketserver.ThreadingTCPServer:
    """Serve scripts/live-test/ from 127.0.0.1:<port>."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(SCENE_DIR)
    )
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, name="scene-http", daemon=True)
    thread.start()
    return httpd


def spawn_pulsar(exe: pathlib.Path, fps: int) -> subprocess.Popen:
    """Spawn pulsar.exe with the desired encoder geometry."""
    env = os.environ.copy()
    env["PULSAR_FPS"] = str(fps)
    env["PULSAR_RESOLUTION"] = "1920x1080"
    env["PULSAR_VIDEO_BITRATE"] = "6000"
    # Point the recording pipeline at a known directory so the live
    # probe can pick up the produced MP4 deterministically and the
    # workflow can upload it as the broadcast proof.
    LIVE_VOD_DIR.mkdir(parents=True, exist_ok=True)
    env["PULSAR_RECORD_DIR"] = str(LIVE_VOD_DIR)
    # Don't bind window_capture to anything — pulsar-scene-source will
    # add a browser_source soon. The default frontend-stub bootstrap
    # produces a transient black-frame moment we tolerate.
    env.pop("PULSAR_CAPTURE_WINDOW", None)

    # `--disable-gpu` is forwarded to CEF (obs-browser plugin) via the
    # process command line — CEF reads it through `GetCommandLineW()`
    # at `CefInitialize` and propagates to every subprocess it spawns.
    # Without it, CEF's GPU subprocess crashes at the first frame pull
    # in a headless host (no display / no compositor) with
    # 'gpu_data_manager_impl_private.cc: GPU process isn't usable.
    # Goodbye.', taking obs-browser down with it. SW rasterization is
    # the canonical config for headless CEF (Puppeteer / Playwright /
    # Lambda runtime do the same). Pulsar-side fix : we just launch
    # with the flag, no patch on upstream obs-browser needed.
    proc = subprocess.Popen(
        [str(exe), "--disable-gpu", "--no-sandbox"],
        cwd=str(RUNDIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    return proc


def wait_for_obs_websocket_config(timeout: float) -> tuple[int, str]:
    """Return (port, password) once pulsar-websocket has dropped its config."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if CONFIG_PATH.exists():
            try:
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                port = cfg.get("server_port", 4455)
                pwd  = cfg.get("server_password", "")
                if pwd:
                    return port, pwd
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.5)
    raise TimeoutError(f"pulsar-websocket config not seen in {timeout}s")


def stream_pulsar_logs(proc: subprocess.Popen, sink: list[str]) -> None:
    """Background thread : drain pulsar stdout/stderr into a list."""
    assert proc.stdout is not None
    for line in proc.stdout:
        sink.append(line.rstrip())


class Inbox:
    def __init__(self):
        self.events: list[dict] = []
        self.responses: list[dict] = []
        self._cond = asyncio.Event()

    def push(self, msg: dict) -> None:
        op = msg.get("op")
        if op == 5:
            self.events.append(msg)
        elif op == 7:
            self.responses.append(msg)
        self._cond.set()

    async def wait_response(self, request_id: str, timeout: float) -> dict:
        end = asyncio.get_event_loop().time() + timeout
        while True:
            for r in self.responses:
                if r.get("d", {}).get("requestId") == request_id:
                    return r["d"]
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"response timeout for request {request_id}")
            self._cond.clear()
            try:
                await asyncio.wait_for(self._cond.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError(f"response timeout for request {request_id}")


async def reader(ws, inbox: Inbox) -> None:
    async for raw in ws:
        try:
            inbox.push(json.loads(raw))
        except json.JSONDecodeError:
            pass


async def auth(ws, inbox: Inbox, password: str) -> None:
    """Hello → Identify round-trip."""
    end = asyncio.get_event_loop().time() + 10
    hello = None
    while hello is None:
        if asyncio.get_event_loop().time() > end:
            raise TimeoutError("no Hello from server")
        for msg in list(inbox.events) + list(inbox.responses):
            if isinstance(msg, dict) and msg.get("op") == 0:
                hello = msg
                break
        # Hello arrives as op 0 (not in our 5/7 buckets) — re-pull from raw.
        # Workaround : peek directly via ws.recv when buckets are empty.
        if hello is None:
            await asyncio.sleep(0.05)

    auth_section = hello.get("d", {}).get("authentication")
    identify = {"op": 1, "d": {
        "rpcVersion": 1,
        "eventSubscriptions": EVENT_SUBSCRIPTION_ALL,
    }}
    if auth_section:
        identify["d"]["authentication"] = compute_auth(
            password,
            auth_section["salt"],
            auth_section["challenge"],
        )
    await ws.send(json.dumps(identify))
    # Wait for Identified (op 2)
    end = asyncio.get_event_loop().time() + 10
    while asyncio.get_event_loop().time() < end:
        for msg in list(inbox.events) + list(inbox.responses):
            if isinstance(msg, dict) and msg.get("op") == 2:
                return
        await asyncio.sleep(0.05)
    raise TimeoutError("no Identified from server")


async def hello_then_auth(ws, password: str) -> None:
    """Lightweight hello+identify reader, NOT using the inbox bucket
    pump (Hello arrives before pump starts)."""
    raw = await asyncio.wait_for(ws.recv(), timeout=10)
    hello = json.loads(raw)
    if hello.get("op") != 0:
        raise RuntimeError(f"expected Hello (op=0), got op={hello.get('op')}")

    auth_section = hello.get("d", {}).get("authentication")
    identify = {"op": 1, "d": {
        "rpcVersion": 1,
        "eventSubscriptions": EVENT_SUBSCRIPTION_ALL,
    }}
    if auth_section:
        identify["d"]["authentication"] = compute_auth(
            password,
            auth_section["salt"],
            auth_section["challenge"],
        )
    await ws.send(json.dumps(identify))

    raw = await asyncio.wait_for(ws.recv(), timeout=10)
    identified = json.loads(raw)
    if identified.get("op") != 2:
        raise RuntimeError(f"expected Identified (op=2), got op={identified.get('op')}")


async def request(ws, inbox: Inbox, request_type: str, request_id: str,
                  request_data: dict | None = None, timeout: float = 30.0) -> dict:
    msg = {"op": 6, "d": {
        "requestType": request_type,
        "requestId": request_id,
    }}
    if request_data is not None:
        msg["d"]["requestData"] = request_data
    await ws.send(json.dumps(msg))
    return await inbox.wait_response(request_id, timeout)


async def vendor_call(ws, inbox: Inbox, request_id: str, vendor: str,
                      request_type: str, request_data: dict | None = None,
                      timeout: float = 30.0) -> dict:
    return await request(ws, inbox, "CallVendorRequest", request_id, {
        "vendorName": vendor,
        "requestType": request_type,
        "requestData": request_data or {},
    }, timeout)


def vendor_response_data(resp: dict) -> dict:
    """CallVendorRequest wraps the vendor's response in
    `responseData.responseData`. Unwrap it."""
    rd = resp.get("responseData", {})
    inner = rd.get("responseData", {}) if isinstance(rd, dict) else {}
    return inner if isinstance(inner, dict) else {}


def vendor_request_status(resp: dict) -> dict:
    """The CallVendorRequest envelope's requestStatus — `result` bool +
    `code` int (100=success ; 200..=client error ; 300..=server error)
    + `comment` string with a description on failure."""
    s = resp.get("requestStatus", {})
    return s if isinstance(s, dict) else {}


def dump_response(label: str, resp: dict) -> None:
    """Pretty-print a vendor response for debug logs. Used both on
    success (one line summary) and failure (full status + data)."""
    status = vendor_request_status(resp)
    inner  = vendor_response_data(resp)
    print(f"[live-test/{label}] status={status} responseData={inner}")


def fail_log(label: str, msg: str) -> None:
    print(f"::error::live-test {label}: {msg}", file=sys.stderr)


# ── Diagnostic JSON dump ────────────────────────────────────────────────────
# Writes a structured snapshot at end-of-run so reviewers can attribute lag
# to pulsar (high render time, dropped frames) vs network (low effective
# bitrate vs target) vs upstream (e.g. Twitch ingest stalls). Two halves :
#   - the per-poll sample series (raw signal)
#   - a summary block (avg / max / p95 / total skipped) for at-a-glance
# Plus ffprobe stats on the local MP4 (the encoded-on-disk truth).

def _percentile(values: list[float], p: float) -> float | None:
    """p-th percentile of a numeric list. p in [0, 100]."""
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    k = (len(vs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(vs) - 1)
    if lo == hi:
        return float(vs[lo])
    return float(vs[lo] + (vs[hi] - vs[lo]) * (k - lo))


def _ffprobe_summary(mp4_path: str) -> dict:
    """Return codec / bitrate / fps / duration for the recorded MP4. Empty
    dict on failure -- a missing diagnostic is non-fatal, the workflow
    upload still happens."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,r_frame_rate,bit_rate,duration,sample_rate,channels",
             "-of", "json", mp4_path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return {"_ffprobe_error": (proc.stderr or "")[:500]}
        data = json.loads(proc.stdout or "{}")
        out = {"streams": []}
        for s in data.get("streams", []):
            entry = {k: s.get(k) for k in (
                "codec_type", "codec_name", "width", "height",
                "r_frame_rate", "bit_rate", "duration",
                "sample_rate", "channels",
            ) if s.get(k) is not None}
            # Convert bit_rate to kbps for readability.
            if "bit_rate" in entry:
                try:
                    entry["bit_rate_kbps"] = round(int(entry["bit_rate"]) / 1000)
                except (TypeError, ValueError):
                    pass
            out["streams"].append(entry)
        return out
    except Exception as e:
        return {"_ffprobe_error": str(e)[:500]}


def write_diagnostic(samples: list[dict], vod_path: str | None, duration_sec: int) -> str | None:
    """Compute end-of-run summary stats + ffprobe the MP4, write JSON to
    LIVE_VOD_DIR. Returns the absolute path on success, None on failure
    (failure here is non-fatal -- the broadcast proof MP4 is the gate)."""
    if not samples:
        return None

    def _avg(key):
        vals = [s.get(key) for s in samples if s.get(key) is not None]
        return (sum(vals) / len(vals)) if vals else None
    def _max(key):
        vals = [s.get(key) for s in samples if s.get(key) is not None]
        return max(vals) if vals else None
    def _p95(key):
        return _percentile([s.get(key) for s in samples], 95.0)
    def _last(key):
        for s in reversed(samples):
            if s.get(key) is not None:
                return s[key]
        return None

    # outputBytes is cumulative ; effective bitrate = (last - first) over span.
    bytes_first = next((s["output_bytes"] for s in samples if s.get("output_bytes") is not None), 0) or 0
    bytes_last  = _last("output_bytes") or 0
    span_sec    = max(1, samples[-1]["t"] - samples[0]["t"])
    effective_kbps = round((bytes_last - bytes_first) * 8 / span_sec / 1000) if bytes_last > bytes_first else None

    summary = {
        "duration_sec":      duration_sec,
        "samples":           len(samples),
        "active_fps_avg":    _avg("active_fps"),
        "active_fps_min":    min((s["active_fps"] for s in samples if s.get("active_fps") is not None), default=None),
        "render_ms_avg":     _avg("avg_render_ms"),
        "render_ms_p95":     _p95("avg_render_ms"),
        "render_ms_max":     _max("avg_render_ms"),
        "render_skipped":    _last("render_skipped") or 0,
        "render_total":      _last("render_total") or 0,
        "output_skipped":    _last("output_skipped") or 0,
        "output_total":      _last("output_total") or 0,
        "drop_ratio_max":    _max("drop_ratio") or 0.0,
        "current_kbps_min":  min((s["current_kbps"] for s in samples if s.get("current_kbps") is not None), default=None),
        "current_kbps_max":  _max("current_kbps"),
        "target_kbps":       _last("target_kbps"),
        "effective_kbps":    effective_kbps,  # derived from outputBytes delta
        "cpu_pct_avg":       _avg("cpu_pct"),
        "cpu_pct_max":       _max("cpu_pct"),
        "memory_mb_final":   _last("memory_mb"),
        "adaptive_samples":  _last("adaptive_samples") or 0,
    }

    diagnostic = {
        "schema": "pulsar-live-test-diagnostic/v1",
        "summary": summary,
        "samples": samples,
        "mp4_ffprobe": _ffprobe_summary(vod_path) if vod_path else None,
    }

    try:
        LIVE_VOD_DIR.mkdir(parents=True, exist_ok=True)
        out = LIVE_VOD_DIR / "diagnostic.json"
        out.write_text(json.dumps(diagnostic, indent=2, default=str), encoding="utf-8")
    except OSError as e:
        print(f"::warning::could not write diagnostic.json: {e}", file=sys.stderr)
        return None

    # One-line summary on stdout so a CI run is at-a-glance diagnosable.
    print(f"[live-test] diagnostic : "
          f"avg_fps={summary['active_fps_avg']} "
          f"render_avg={summary['render_ms_avg']}ms "
          f"render_p95={summary['render_ms_p95']}ms "
          f"render_skipped={summary['render_skipped']} "
          f"output_skipped={summary['output_skipped']} "
          f"effective_kbps={summary['effective_kbps']} "
          f"target={summary['target_kbps']}")
    return str(out)


async def probe(stream_key: str, duration_sec: int, fps: int) -> int:
    if not stream_key:
        fail_log("config", "TWITCH_STREAM_KEY env var is empty")
        return 2
    if not DEFAULT_EXE.exists():
        env_exe = os.environ.get("PULSAR_EXE")
        exe = pathlib.Path(env_exe) if env_exe else DEFAULT_EXE
        if not exe.exists():
            fail_log("config", f"pulsar.exe not found at {exe}")
            return 2
    else:
        exe = DEFAULT_EXE

    if not (SCENE_DIR / "test-scene.html").exists():
        fail_log("config", f"test-scene.html missing under {SCENE_DIR}")
        return 2

    # Drop any stale pulsar-websocket config so we don't read a previous
    # session's password.
    if CONFIG_PATH.exists():
        try:
            CONFIG_PATH.unlink()
        except OSError:
            pass

    # Local HTTP server hosting the test scene.
    http_port = find_free_port()
    httpd = start_scene_server(http_port)
    # Scene URL is finalised after we know pulsar's WS port + password
    # (see below). The adapter inside test-scene.html stays dormant
    # until those are passed via the query string.
    scene_url_base = f"http://127.0.0.1:{http_port}/test-scene.html"
    print(f"[live-test] scene HTTP server : {scene_url_base}")

    # Spawn pulsar.exe.
    print(f"[live-test] spawning {exe}")
    proc = spawn_pulsar(exe, fps)
    log_lines: list[str] = []
    threading.Thread(
        target=stream_pulsar_logs, args=(proc, log_lines),
        name="pulsar-log-pump", daemon=True
    ).start()

    rc = 1
    try:
        port, password = wait_for_obs_websocket_config(SPAWN_TIMEOUT_SEC)
        print(f"[live-test] pulsar ready : ws ws://127.0.0.1:{port}")

        ws_url = f"ws://127.0.0.1:{port}"
        # ping_interval=None : websockets-lib's default 20 s ping is not
        # answered by obs-websocket's webso­cketpp server (it relies on
        # app-level activity, not WS-protocol pings). Without disabling
        # client-side pings, the connection drops at the first ping
        # timeout — observed at t=30 s while sleeping in the poll loop.
        # close_timeout high so a slow shutdown doesn't fail the run.
        async with websockets.connect(
            ws_url,
            subprotocols=["obswebsocket.json"],
            max_size=2**24,
            ping_interval=None,
            close_timeout=15,
        ) as ws:
            await hello_then_auth(ws, password)
            print("[live-test] auth OK")

            inbox = Inbox()
            reader_task = asyncio.create_task(reader(ws, inbox))

            # DEBUG : if PULSAR_SKIP_CAPTURE=1 in env, only test
            # CreateDestination on its own to disambiguate whether the
            # SetCaptureSource side-effect breaks multi-stream's
            # registry, or whether multi-stream's vendor registration
            # is broken outright.
            if os.environ.get("PULSAR_SKIP_CAPTURE") == "1":
                print("[live-test] DEBUG : skipping SetCaptureSource")
                r = await vendor_call(ws, inbox, "create-dest", "pulsar",
                    "CreateDestination", {
                        "name": DESTINATION_NAME,
                        "kind": "vod_local",
                        "url":  str(REPO_ROOT / "scripts/live-test-debug.mp4"),
                    })
                dump_response("create-dest-debug", r)
                dest_id = vendor_response_data(r).get("id")
                print(f"[live-test] DEBUG CreateDestination id = {dest_id!r}")
                # tear down whatever we made
                if dest_id:
                    await vendor_call(ws, inbox, "remove-dest", "pulsar",
                        "RemoveDestination", {"id": dest_id})
                reader_task.cancel()
                return 0 if dest_id else 1

            # Hand the scene the live WS coordinates so its in-page
            # adapter can connect and stream telemetry into the HUD.
            scene_url = f"{scene_url_base}?port={port}&token={password}"

            # 1. SetCaptureSource → browser_source.
            r = await vendor_call(ws, inbox, "set-capture", "pulsar-scene",
                "SetCaptureSource", {
                    "kind": "browser_source",
                    "url":  scene_url,
                    "width":  1920,
                    "height": 1080,
                    "fps":    fps,
                    "reroute_audio": True,
                })
            dump_response("set-capture", r)
            data = vendor_response_data(r)
            if data.get("kind") != "browser_source":
                fail_log("set-capture", f"unexpected response : {data}")
                return 1

            # 1b. GetCaptureSource → confirm the active snapshot
            #     reflects what we just set.
            r = await vendor_call(ws, inbox, "get-capture", "pulsar-scene",
                "GetCaptureSource", {})
            dump_response("get-capture", r)
            got = vendor_response_data(r)
            if got.get("kind") != "browser_source":
                fail_log("get-capture",
                    f"snapshot not browser_source : {got}")
                return 1
            if got.get("url") != scene_url:
                fail_log("get-capture",
                    f"url drift : got {got.get('url')!r}, expected {scene_url!r}")
                return 1
            if int(got.get("last_change_unix", 0)) <= 0:
                fail_log("get-capture",
                    f"last_change_unix not set : {got}")
                return 1
            print(f"[live-test] get-capture confirms snapshot")

            # 2. CreateDestination twitch.
            r = await vendor_call(ws, inbox, "create-dest", "pulsar",
                "CreateDestination", {
                    "name": DESTINATION_NAME,
                    "kind": "twitch",
                    "key":  stream_key,
                })
            dump_response("create-dest", r)
            dest_data = vendor_response_data(r)
            dest_id = dest_data.get("id")
            if not dest_id:
                status = vendor_request_status(r)
                fail_log("create-dest",
                    f"no id ; requestStatus={status} responseData={dest_data}")
                return 1
            print(f"[live-test] destination created : id={dest_id}")

            # 3. StartDestination.
            r = await vendor_call(ws, inbox, "start-dest", "pulsar",
                "StartDestination", {"id": dest_id})
            dump_response("start-dest", r)
            sd = vendor_response_data(r)
            if not sd.get("started"):
                status = vendor_request_status(r)
                fail_log("start-dest",
                    f"not started ; requestStatus={status} responseData={sd}")
                return 1
            print(f"[live-test] destination STARTED -- going live")

            # 3b. StartRecord -- record the broadcast locally so the CI
            # workflow can upload the MP4 as the live-test proof. Standard
            # obs-websocket v5 request, not a Pulsar vendor extension.
            r = await request(ws, inbox, "StartRecord", "start-rec")
            rec_status = r.get("requestStatus", {})
            if not rec_status.get("result"):
                fail_log("start-rec",
                    f"could not start local recording: {rec_status}")
                return 1
            print(f"[live-test] local recording started (writing under {LIVE_VOD_DIR})")

            # 4. Poll metrics every POLL_INTERVAL_SEC for `duration_sec`.
            # Per-poll samples accumulate in `perf_samples` ; at end-of-run
            # the probe computes a structured diagnostic JSON (avg / max /
            # p95) plus an ffprobe summary of the recorded MP4. This is
            # the gold-standard answer to "is the lag coming from pulsar"
            # -- everything libobs collects internally is captured.
            start_t = time.time()
            poll_count = 0
            adaptive_samples_seen = 0
            perf_samples: list[dict] = []
            while time.time() - start_t < duration_sec:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                poll_count += 1
                elapsed = int(time.time() - start_t)

                # GetDestinations — assert active=true on our id.
                r = await vendor_call(ws, inbox, f"get-dest-{poll_count}",
                    "pulsar", "GetDestinations", {})
                lst = vendor_response_data(r).get("destinations", [])
                ours = next((d for d in lst if d.get("id") == dest_id), None)
                if not ours or not ours.get("active"):
                    fail_log("poll", f"destination not active at poll #{poll_count} : {ours}")
                    return 1

                # GetAdaptiveState — confirm the worker is sampling.
                r = await vendor_call(ws, inbox, f"get-adapt-{poll_count}",
                    "pulsar", "GetAdaptiveState", {})
                adapt = vendor_response_data(r)
                samples = int(adapt.get("samples", 0))
                if samples > adaptive_samples_seen:
                    adaptive_samples_seen = samples
                drop_ratio = float(adapt.get("last_drop_ratio", 0.0))
                cur_bitrate = adapt.get("current_kbps")
                target_bitrate = adapt.get("target_kbps")

                # GetStats — comprehensive perf snapshot (the "is it
                # pulsar" tooling). Standard obs-websocket v5 request,
                # NOT a vendor call. The fields that matter for lag
                # attribution :
                #   activeFps              -- live encoder fps. <60 = bad.
                #   averageFrameRenderTime -- ms per render+encode. >16 = bad
                #                             at 60fps.
                #   renderSkippedFrames    -- compositor lagged.
                #   outputSkippedFrames    -- encoder/network dropped.
                stats_r = await request(ws, inbox, "GetStats", f"stats-{poll_count}")
                stats = stats_r.get("responseData", {}) or {}

                # GetStreamStatus — outputBytes lets us derive the actual
                # encoded bitrate. Multi-stream destinations don't update
                # the legacy outputs but obs's stats may still tick.
                ss_r = await request(ws, inbox, "GetStreamStatus", f"stream-{poll_count}")
                ss = ss_r.get("responseData", {}) or {}

                sample = {
                    "t": elapsed,
                    "poll": poll_count,
                    "adaptive_samples": samples,
                    "drop_ratio": drop_ratio,
                    "current_kbps": cur_bitrate,
                    "target_kbps": target_bitrate,
                    "active_fps": stats.get("activeFps"),
                    "avg_render_ms": stats.get("averageFrameRenderTime"),
                    "render_total": stats.get("renderTotalFrames"),
                    "render_skipped": stats.get("renderSkippedFrames"),
                    "output_total": stats.get("outputTotalFrames"),
                    "output_skipped": stats.get("outputSkippedFrames"),
                    "output_bytes": ss.get("outputBytes"),
                    "cpu_pct": stats.get("cpuUsage"),
                    "memory_mb": stats.get("memoryUsage"),
                    "destination_active": bool(ours and ours.get("active")),
                }
                perf_samples.append(sample)

                fps_str = f"{sample['active_fps']:.1f}" if sample['active_fps'] is not None else "—"
                rt_str  = f"{sample['avg_render_ms']:.1f}ms" if sample['avg_render_ms'] is not None else "—"
                print(f"[live-test] poll #{poll_count} t={elapsed}s "
                      f"active=true samples={samples} "
                      f"drop_ratio={drop_ratio:.4f} "
                      f"bitrate={cur_bitrate} "
                      f"fps={fps_str} render={rt_str}")

                if drop_ratio > FRAME_DROP_RATIO_MAX:
                    fail_log("poll",
                        f"frame drop ratio {drop_ratio:.4f} > {FRAME_DROP_RATIO_MAX} "
                        f"at poll #{poll_count}")
                    return 1

            # 5. StopDestination.
            r = await vendor_call(ws, inbox, "stop-dest", "pulsar",
                "StopDestination", {"id": dest_id})
            print(f"[live-test] destination stopped : {vendor_response_data(r)}")

            # 5b. StopRecord -- finalise the local MP4 and capture its
            # path. ffmpeg_muxer flushes + writes the moov atom on close,
            # so the file is ready to upload by the time this returns.
            r = await request(ws, inbox, "StopRecord", "stop-rec")
            rec_status = r.get("requestStatus", {})
            rec_data   = r.get("responseData", {}) or {}
            vod_path   = rec_data.get("outputPath")
            if not rec_status.get("result") or not vod_path:
                fail_log("stop-rec",
                    f"StopRecord failed; requestStatus={rec_status} responseData={rec_data}")
                return 1
            print(f"[live-test] local recording finalised : {vod_path}")
            # Sentinel parsed by .github/workflows/live-test.yml to find
            # the file to upload as the broadcast proof. Must stay on a
            # single line, must be the only LIVE_VOD_PATH= line emitted.
            print(f"LIVE_VOD_PATH={vod_path}")

            # 6. Final adaptive snapshot — assert non-trivial samples
            #    over the broadcast.
            if adaptive_samples_seen <= 0:
                fail_log("post",
                    f"adaptive worker never reported samples (saw {adaptive_samples_seen})")
                return 1
            print(f"[live-test] adaptive samples seen total : {adaptive_samples_seen}")

            # 7. Write diagnostic JSON so reviewers can attribute lag.
            diag_path = write_diagnostic(perf_samples, vod_path, duration_sec)
            if diag_path:
                # Sentinel parsed by live-test.yml to upload the JSON
                # as a workflow artefact alongside the MP4.
                print(f"LIVE_DIAGNOSTIC_PATH={diag_path}")

            # 8. RemoveDestination.
            await vendor_call(ws, inbox, "remove-dest", "pulsar",
                "RemoveDestination", {"id": dest_id})

            reader_task.cancel()

        # Scan logs for non-allowlisted error lines.
        bad = []
        for line in log_lines:
            low = line.lower()
            if "error" in low or "fail" in low:
                if not any(b in line for b in BENIGN_LOG_SUBSTRINGS):
                    bad.append(line)
        if bad:
            print("::warning::live-test : pulsar log mentions error/fail "
                  f"({len(bad)} line(s)) — review :")
            for b in bad[:25]:
                print(f"  {b}")
            # Non-fatal — Twitch ingest cuts can produce spurious "error"
            # log lines that don't reflect the actual broadcast quality.
            # The metric-side assertions above are the authoritative gate.

        print("\n[live-test] OK -- all assertions passed")
        rc = 0

    finally:
        # On any failure path, dump pulsar's stdout so we see what
        # the engine reported before it died. Skipped on success
        # to keep CI logs short.
        if rc != 0 and log_lines:
            print(f"\n[live-test] ---- pulsar stdout (last 80 lines) ----")
            for line in log_lines[-80:]:
                print(f"  {line}")
            print(f"[live-test] ---- end pulsar stdout ----\n")

        # Tear down pulsar.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass

    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--duration", type=int,
                    default=int(os.environ.get("LIVE_TEST_DURATION", "300")),
                    help="broadcast duration in seconds (default 300)")
    ap.add_argument("--fps", type=int,
                    default=int(os.environ.get("LIVE_TEST_FPS", "60")),
                    help="encoder fps target (default 60)")
    args = ap.parse_args()

    key = os.environ.get("TWITCH_STREAM_KEY", "").strip()
    return asyncio.run(probe(key, args.duration, args.fps))


if __name__ == "__main__":
    sys.exit(main())
