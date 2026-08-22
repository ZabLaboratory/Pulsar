#!/usr/bin/env python3
"""
BENCH-MEDIA-SOURCE — ADR 023 §3.2 micro-benchmark (Prism/Pulsar).

THE ONE QUESTION THIS BENCH TRANCHES
=====================================
Playing the SAME 1080p60 ~30s clip, ON THE PROGRAM (ANTENNA) SCENE of the
shared Pulsar sidecar, which costs more:
  (A) a CEF ``browser_source`` showing an HTML page with a plain ``<video>``
      element (the current CEF-default per ADR 023), or
  (B) a native ``ffmpeg_source`` pointed straight at the same file?

Measured via ``GetStats`` (obs-websocket v5): cpuUsage, activeFps,
averageFrameRenderTime, renderSkippedFrames, outputSkippedFrames, sampled
every ~0.5s across the whole clip duration, on the PROGRAM scene (not an
isolated headless capture) — a local ``StartRecord`` is used as the "antenna"
proxy: it exercises the exact same render->encode (x264) pipeline the real
RTMP output would use (same pattern as ``probe-record-m2.py`` — no VPS/Twitch
key required for a load comparison, since GetStats measures OBS's own
render/output threads regardless of the output's destination). Audio/video
drift is read back from the recorded MP4 via ffprobe (stream duration
delta).

WHAT THIS IS NOT
================
Not a Twitch-ingest test, not a network test. Doctrine (M10, SPIKE-GPU)
reserves the real-Twitch/VPS validation for the antenna run; this bench only
needs the identical PROGRAM SCENE render+encode path, which GetStats reports
on regardless of where the encoded bytes end up.

Usage:
    pip install websockets
    python scripts/bench-media-source-load.py --clip build/bench-media/clip-1080p60-30s.mp4

Exit codes:
  0  both scenarios measured, raw numbers printed (no assertion — this is a
     measurement tool, not a pass/fail gate; ADR 023 §3.2 is Atlas's call).
  1  hard failure (pulsar.exe missing, WS error, ffprobe missing and no
     fallback possible for a needed metric).
  2  config/env error.
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
import secrets as _secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Optional

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

try:
    import websockets
except ImportError:
    print("error: pip install websockets (pure WS client — no native deps)")
    sys.exit(2)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    REPO_ROOT / "node_modules" / "@clodocapeo" / "pulsar-bundle-full"
    / "binaries" / "bin" / "64bit" / "pulsar.exe"
)
DEFAULT_CLIP = REPO_ROOT / "build" / "bench-media" / "clip-1080p60-30s.mp4"
BENCH_OUT_DIR = REPO_ROOT / "build" / "bench-media" / "out"

READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")
READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0
EVENT_SUBSCRIPTION_ALL = 0x7FF
CANVAS_W = 1920
CANVAS_H = 1080

SCENE_A = "bench-browser-video"
SCENE_B = "bench-ffmpeg-source"
BROWSER_INPUT = "bench-browser-input"
FFMPEG_INPUT = "bench-ffmpeg-input"

SAMPLE_INTERVAL_S = 0.5
# Recorded window: clip is 30s; give CEF/video element ~1.5s to start
# buffering/playing before we start sampling+recording, and stop a touch
# before the clip ends so we are not measuring the "video ended" tail.
WARMUP_S = 1.5
MEASURE_S = 27.0


VIDEO_PAGE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>BENCH-MEDIA</title>
<style>
  html,body{{margin:0;padding:0;width:100%;height:100%;overflow:hidden;background:#000;}}
  video{{width:100vw;height:100vh;object-fit:cover;}}
</style></head>
<body>
  <video id="v" src="/clip.mp4" autoplay loop muted playsinline></video>
  <script>
    // muted is required for CEF autoplay policy; the load itself still
    // decodes real video+audio frames via the browser's media pipeline,
    // which is the thing under measurement (CEF decode cost), not the
    // audibility of the result.
    var v = document.getElementById('v');
    v.play().catch(function(e){{ console.log('play() rejected: ' + e); }});
  </script>
</body></html>
"""


class _ClipHandler(http.server.BaseHTTPRequestHandler):
    clip_path: pathlib.Path

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/page.html"):
            body = VIDEO_PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/clip.mp4":
            data = self.clip_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404, "not found")

    def log_message(self, *args) -> None:
        return


class LocalClipServer:
    def __init__(self, clip: pathlib.Path) -> None:
        self.httpd: Optional[http.server.ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.port = 0
        self.clip = clip

    def start(self) -> str:
        handler = type("Handler", (_ClipHandler,), {"clip_path": self.clip})
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.port}/page.html"

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None


class PulsarProcess:
    def __init__(self, exe: pathlib.Path, record_dir: pathlib.Path) -> None:
        self.exe = exe
        self.record_dir = record_dir
        self.proc: Optional[subprocess.Popen] = None
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_RESOLUTION"] = f"{CANVAS_W}x{CANVAS_H}"
        env["PULSAR_RECORD_DIR"] = str(self.record_dir)
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)
        env.pop("PULSAR_PROCESS_AUDIO_NAME", None)
        # GPU-ON (no --disable-gpu): the real antenna world, and the only
        # world where a CPU/GPU load comparison between CEF and ffmpeg_source
        # means anything (SPIKE-GPU precedent).
        argv = [str(self.exe), "--no-sandbox"]
        creationflags = 0x08000000 if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            argv, cwd=str(self.exe.parent), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            bufsize=1, text=True, encoding="utf-8", errors="replace",
            creationflags=creationflags,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            self._lines.append(line)
            m = READY_RE.match(line)
            if m is not None and not self._ready_event.is_set():
                self._ready_match = m
                self._ready_event.set()

    def wait_ready(self, timeout: float) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        while True:
            if self._ready_event.wait(timeout=0.2):
                m = self._ready_match
                assert m is not None
                return m.group(1), m.group(2)
            assert self.proc is not None
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"pulsar.exe exited (code {self.proc.returncode}) before READY.\n"
                    + self._diag()
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(f"pulsar.exe did not signal READY within {timeout:.0f}s.\n" + self._diag())

    @property
    def lines(self) -> list[str]:
        return list(self._lines)

    def _diag(self) -> str:
        tail = self._lines[-40:]
        body = "\n".join(f"  | {ln}" for ln in tail) if tail else "  | (no output)"
        return f"--- pulsar stdout/stderr (last {len(tail)} lines) ---\n{body}"

    def shutdown(self, grace: float = SHUTDOWN_GRACE_S) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=grace)
            return
        except Exception:
            pass
        try:
            self.proc.kill()
            self.proc.wait(timeout=grace)
        except Exception:
            pass


def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


class Inbox:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.responses: list[dict] = []

    async def pump(self, ws, until: Callable[["Inbox"], bool], timeout: float) -> None:
        end = asyncio.get_event_loop().time() + timeout
        while not until(self):
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            msg = json.loads(raw)
            op = msg.get("op")
            if op == 5:
                self.events.append(msg["d"])
            elif op == 7:
                self.responses.append(msg["d"])


async def request(inbox: Inbox, ws, request_type: str, request_id: str,
                   data: dict | None = None, timeout: float = 30.0) -> dict:
    body: dict = {"requestType": request_type, "requestId": request_id}
    if data is not None:
        body["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": body}))

    def has_response(ix: Inbox) -> bool:
        return any(r["requestId"] == request_id for r in ix.responses)

    await inbox.pump(ws, has_response, timeout)
    for i, r in enumerate(inbox.responses):
        if r["requestId"] == request_id:
            return inbox.responses.pop(i)
    raise RuntimeError("unreachable")


def req_ok(resp: dict) -> bool:
    return bool(resp.get("requestStatus", {}).get("result"))


def req_code(resp: dict) -> Optional[int]:
    return resp.get("requestStatus", {}).get("code")


async def wait_event(inbox: Inbox, ws, event_type: str,
                      pred: Callable[[dict], bool], timeout: float) -> dict:
    def has_it(ix: Inbox) -> bool:
        return any(e["eventType"] == event_type and pred(e["eventData"]) for e in ix.events)
    await inbox.pump(ws, has_it, timeout)
    for e in inbox.events:
        if e["eventType"] == event_type and pred(e["eventData"]):
            return e
    raise RuntimeError("unreachable")


async def sample_stats(inbox: Inbox, ws, duration: float, interval: float) -> list[dict]:
    samples: list[dict] = []
    deadline = time.monotonic() + duration
    i = 0
    while time.monotonic() < deadline:
        i += 1
        r = await request(inbox, ws, "GetStats", f"stats-{i}-{int(time.time()*1000)}")
        if req_ok(r):
            d = r["responseData"]
            d["_t"] = time.monotonic()
            samples.append(d)
        await asyncio.sleep(interval)
    return samples


def summarize(samples: list[dict], baseline: dict) -> dict:
    if not samples:
        return {"n": 0}
    cpu = [s["cpuUsage"] for s in samples]
    fps = [s["activeFps"] for s in samples]
    rrt = [s["averageFrameRenderTime"] for s in samples]
    render_skip_delta = samples[-1]["renderSkippedFrames"] - baseline["renderSkippedFrames"]
    output_skip_delta = samples[-1]["outputSkippedFrames"] - baseline["outputSkippedFrames"]
    render_total_delta = samples[-1]["renderTotalFrames"] - baseline["renderTotalFrames"]
    output_total_delta = samples[-1]["outputTotalFrames"] - baseline["outputTotalFrames"]
    return {
        "n": len(samples),
        "cpu_avg": sum(cpu) / len(cpu),
        "cpu_max": max(cpu),
        "fps_avg": sum(fps) / len(fps),
        "fps_min": min(fps),
        "avg_render_time_ms_avg": sum(rrt) / len(rrt),
        "avg_render_time_ms_max": max(rrt),
        "render_skipped_delta": render_skip_delta,
        "output_skipped_delta": output_skip_delta,
        "render_total_delta": render_total_delta,
        "output_total_delta": output_total_delta,
        "render_skip_ratio": (render_skip_delta / render_total_delta) if render_total_delta else 0.0,
        "output_skip_ratio": (output_skip_delta / output_total_delta) if output_total_delta else 0.0,
    }


class GpuSampler:
    """Polls nvidia-smi (utilization.gpu / utilization.decoder / utilization.encoder,
    percent) once a second in a background thread, for the "charge GPU si
    observable" leg of the bench. No-op (silently absent) if nvidia-smi is not
    on PATH or the query fields aren't supported (older driver) — reported as
    NOT MEASURED, never invented.
    """

    QUERY = "utilization.gpu,utilization.memory,memory.used"

    def __init__(self) -> None:
        self.available = shutil.which("nvidia-smi") is not None
        self.proc: Optional[subprocess.Popen] = None
        self.samples: list[tuple[float, float, float]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not self.available:
            return
        try:
            self.proc = subprocess.Popen(
                ["nvidia-smi", f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits", "-l", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except Exception:
            self.available = False
            return
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            if self._stop.is_set():
                break
            parts = [p.strip() for p in line.strip().split(",")]
            if len(parts) != 3:
                continue
            try:
                gpu_u, mem_u, mem_used = (float(p) for p in parts)
            except ValueError:
                continue
            self.samples.append((gpu_u, mem_u, mem_used))

    def stop_and_summarize(self) -> Optional[dict]:
        self._stop.set()
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if not self.available or not self.samples:
            return None
        gpu = [s[0] for s in self.samples]
        memu = [s[1] for s in self.samples]
        return {
            "n": len(self.samples),
            "gpu_util_avg": sum(gpu) / len(gpu),
            "gpu_util_max": max(gpu),
            "gpu_memutil_avg": sum(memu) / len(memu),
        }


def ffprobe_stream_durations(path: pathlib.Path) -> Optional[dict]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        data = json.loads(out.stdout)
    except Exception as exc:  # noqa: BLE001
        print(f"   ffprobe failed on {path}: {exc}")
        return None
    v_dur = a_dur = None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and s.get("duration"):
            v_dur = float(s["duration"])
        elif s.get("codec_type") == "audio" and s.get("duration"):
            a_dur = float(s["duration"])
    if v_dur is None or a_dur is None:
        return None
    return {"video_duration_s": v_dur, "audio_duration_s": a_dur, "drift_s": abs(v_dur - a_dur)}


async def run_scenario(ws, inbox: Inbox, label: str, scene_name: str,
                        build_input: Callable[[], Any], record_dir: pathlib.Path) -> dict:
    print(f"\n==== scenario {label}: scene={scene_name!r} ====")
    r = await request(inbox, ws, "CreateScene", "cs", {"sceneName": scene_name})
    if not req_ok(r) and req_code(r) != 601:
        raise RuntimeError(f"CreateScene declined: {r.get('requestStatus')}")

    await build_input()

    print(f"-> SetCurrentProgramScene {scene_name!r}")
    r = await request(inbox, ws, "SetCurrentProgramScene", "sps", {"sceneName": scene_name})
    if not req_ok(r):
        raise RuntimeError(f"SetCurrentProgramScene declined: {r.get('requestStatus')}")

    # Warm-up: let CEF/ffmpeg_source start decoding before we baseline+record.
    await asyncio.sleep(WARMUP_S)

    base_r = await request(inbox, ws, "GetStats", "base")
    baseline = base_r["responseData"]
    print(f"   baseline: cpu={baseline['cpuUsage']:.1f}% fps={baseline['activeFps']:.2f} "
          f"renderSkipped={baseline['renderSkippedFrames']} outputSkipped={baseline['outputSkippedFrames']}")

    gpu_sampler = GpuSampler()
    gpu_sampler.start()

    print("-> StartRecord (antenna-path proxy: same render+encode pipeline as RTMP output)")
    r = await request(inbox, ws, "StartRecord", "start")
    if not req_ok(r):
        raise RuntimeError(f"StartRecord declined: {r.get('requestStatus')}")
    await wait_event(inbox, ws, "RecordStateChanged",
                      lambda d: d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED", 15.0)

    print(f"-> sampling GetStats every {SAMPLE_INTERVAL_S}s for {MEASURE_S}s ...")
    samples = await sample_stats(inbox, ws, MEASURE_S, SAMPLE_INTERVAL_S)

    gpu_summary = gpu_sampler.stop_and_summarize()

    print("-> StopRecord")
    r = await request(inbox, ws, "StopRecord", "stop")
    if not req_ok(r):
        raise RuntimeError(f"StopRecord declined: {r.get('requestStatus')}")
    evt = await wait_event(inbox, ws, "RecordStateChanged",
                            lambda d: d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED", 15.0)
    output_path = evt["eventData"].get("outputPath") or ""
    print(f"   <- recorded to {output_path}")

    summary = summarize(samples, baseline)
    drift = None
    if output_path:
        out_p = pathlib.Path(output_path)
        if out_p.exists():
            drift = ffprobe_stream_durations(out_p)
            dest = record_dir / f"{label}.mp4"
            try:
                shutil.copy2(out_p, dest)
            except Exception:
                pass

    print(f"   raw samples (cpuUsage%): {[round(s['cpuUsage'], 1) for s in samples]}")
    print(f"   raw samples (activeFps): {[round(s['activeFps'], 2) for s in samples]}")
    print(f"   raw samples (avgRenderTimeMs): {[round(s['averageFrameRenderTime'], 3) for s in samples]}")

    if gpu_summary is None:
        print("   GPU: NOT MEASURED (nvidia-smi absent, unsupported query fields, or no samples)")
    else:
        print(f"   GPU: util avg={gpu_summary['gpu_util_avg']:.1f}% max={gpu_summary['gpu_util_max']:.1f}% "
              f"memutil avg={gpu_summary['gpu_memutil_avg']:.1f}% (n={gpu_summary['n']})")

    return {"summary": summary, "drift": drift, "output_path": output_path, "gpu": gpu_summary}


async def bench(ws_url: str, password: str, page_url: str, clip_path: pathlib.Path,
                 record_dir: pathlib.Path) -> tuple[int, dict]:
    async with websockets.connect(
        ws_url, subprotocols=["obswebsocket.json"], max_size=2**24,
        ping_interval=None, close_timeout=15, open_timeout=10,
    ) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if hello.get("op") != 0:
            print(f"error: expected Hello (op=0), got {hello}")
            return 1, {}
        identify_d: dict = {"rpcVersion": hello["d"]["rpcVersion"], "eventSubscriptions": EVENT_SUBSCRIPTION_ALL}
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ident.get("op") != 2:
            print(f"error: identify failed: {ident}")
            return 1, {}
        print("identified (v5 auth OK)")

        inbox = Inbox()

        resp = await request(inbox, ws, "GetInputKindList", "kinds", {})
        kinds = set(resp["responseData"]["inputKinds"])
        missing = {"browser_source", "ffmpeg_source"} - kinds
        if missing:
            print(f"SKIP: input kind(s) {sorted(missing)} NOT registered — LIGHT build. "
                  "Needs a full build (CEF + obs-ffmpeg).")
            return 3, {}
        print("both source kinds registered (browser_source, ffmpeg_source)")

        results: dict[str, Any] = {}

        # --- Scenario A: CEF browser_source, <video> element ---
        async def build_browser() -> None:
            r = await request(inbox, ws, "CreateInput", "ci-br", {
                "sceneName": SCENE_A, "inputName": BROWSER_INPUT, "inputKind": "browser_source",
                "inputSettings": {
                    "url": page_url, "is_local_file": False,
                    "width": CANVAS_W, "height": CANVAS_H,
                    "fps_custom": True, "fps": 60,
                    "reroute_audio": True,  # route the <video> audio into the mix,
                                            # same as the real CEF-default path
                    "shutdown": False, "restart_when_active": False,
                },
                "sceneItemEnabled": True,
            })
            if not req_ok(r) and req_code(r) != 601:
                raise RuntimeError(f"CreateInput(browser_source) declined: {r.get('requestStatus')}")

        results["browser_cef_video"] = await run_scenario(
            ws, inbox, "browser_cef_video", SCENE_A, build_browser, record_dir)

        # --- Scenario B: native ffmpeg_source ---
        async def build_ffmpeg() -> None:
            r = await request(inbox, ws, "CreateInput", "ci-ff", {
                "sceneName": SCENE_B, "inputName": FFMPEG_INPUT, "inputKind": "ffmpeg_source",
                "inputSettings": {
                    "local_file": str(clip_path), "is_local_file": True,
                    "looping": True, "restart_on_activate": True,
                    "hw_decode": True,
                },
                "sceneItemEnabled": True,
            })
            if not req_ok(r) and req_code(r) != 601:
                raise RuntimeError(f"CreateInput(ffmpeg_source) declined: {r.get('requestStatus')}")

        results["ffmpeg_source_native"] = await run_scenario(
            ws, inbox, "ffmpeg_source_native", SCENE_B, build_ffmpeg, record_dir)

        # --- teardown ---
        for name, rid in ((BROWSER_INPUT, "ri-br"), (FFMPEG_INPUT, "ri-ff")):
            try:
                await request(inbox, ws, "RemoveInput", rid, {"inputName": name})
            except Exception:
                pass
        for scene, rid in ((SCENE_A, "rs-a"), (SCENE_B, "rs-b")):
            try:
                await request(inbox, ws, "RemoveScene", rid, {"sceneName": scene})
            except Exception:
                pass

        return 0, results


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ap = argparse.ArgumentParser(description="BENCH-MEDIA-SOURCE — CEF <video> vs ffmpeg_source (ADR 023 §3.2)")
    ap.add_argument("--exe", type=pathlib.Path, default=pathlib.Path(os.environ.get("PULSAR_EXE", str(DEFAULT_EXE))))
    ap.add_argument("--clip", type=pathlib.Path, default=DEFAULT_CLIP)
    ap.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S)
    args = ap.parse_args()

    exe: pathlib.Path = args.exe
    clip: pathlib.Path = args.clip
    if not exe.exists():
        print(f"error: pulsar.exe not found at {exe}")
        return 2
    if not clip.exists():
        print(f"error: clip not found at {clip} — generate one first, e.g.:\n"
              f"  ffmpeg -f lavfi -i testsrc2=size=1920x1080:rate=60:duration=30 "
              f"-f lavfi -i sine=frequency=1000:duration=30 -c:v libx264 -c:a aac {clip}")
        return 2

    BENCH_OUT_DIR.mkdir(parents=True, exist_ok=True)
    record_dir = pathlib.Path(tempfile.mkdtemp(prefix="pulsar-bench-record-"))

    server = LocalClipServer(clip)
    page_url = server.start()
    print(f"local clip page served at: {page_url} (clip={clip}, {clip.stat().st_size:,} bytes)")

    port = _free_port()
    password = _secrets.token_urlsafe(16)
    pulsar = PulsarProcess(exe, record_dir)

    rc = 1
    results: dict = {}
    try:
        os.environ["PULSAR_PORT"] = str(port)
        os.environ["PULSAR_PASSWORD"] = password
        pulsar.spawn()
        ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
        print(f"READY: {ws_url}")
        rc, results = asyncio.run(bench(ws_url, sentinel_pw, page_url, clip, record_dir))
    except KeyboardInterrupt:
        print("interrupted")
        rc = 130
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc}")
        rc = 1
    finally:
        if rc not in (0, 3):
            for ln in pulsar.lines[-60:]:
                print(f"  | {ln}")
        pulsar.shutdown()
        server.stop()

    print("\n" + "=" * 72)
    print("BENCH-MEDIA-SOURCE — RAW VERDICT (ADR 023 §3.2 input, not a gate)")
    print("=" * 72)
    if rc == 3:
        print("SKIPPED: light build (no CEF or no obs-ffmpeg). Cannot measure.")
        return rc
    if rc != 0:
        print(f"FAILED (exit {rc}) — see diagnostic above. No numbers to report.")
        return rc

    for label in ("browser_cef_video", "ffmpeg_source_native"):
        r = results.get(label)
        if r is None:
            print(f"{label}: NO DATA")
            continue
        s = r["summary"]
        print(f"\n-- {label} --")
        if s.get("n", 0) == 0:
            print("  NO SAMPLES (GetStats never succeeded during the window)")
            continue
        print(f"  samples: {s['n']}")
        print(f"  cpuUsage%      avg={s['cpu_avg']:.2f}  max={s['cpu_max']:.2f}")
        print(f"  activeFps      avg={s['fps_avg']:.2f}  min={s['fps_min']:.2f}")
        print(f"  avgRenderTimeMs avg={s['avg_render_time_ms_avg']:.3f}  max={s['avg_render_time_ms_max']:.3f}")
        print(f"  renderSkippedFrames delta={s['render_skipped_delta']}  "
              f"(of {s['render_total_delta']} rendered, ratio={s['render_skip_ratio']*100:.3f}%)")
        print(f"  outputSkippedFrames delta={s['output_skipped_delta']}  "
              f"(of {s['output_total_delta']} output, ratio={s['output_skip_ratio']*100:.3f}%)")
        drift = r.get("drift")
        if drift is None:
            print("  a/v drift: NOT MEASURED (ffprobe missing or recorded file unreadable)")
        else:
            print(f"  a/v drift: video={drift['video_duration_s']:.3f}s audio={drift['audio_duration_s']:.3f}s "
                  f"|drift|={drift['drift_s']*1000:.1f}ms")
        gpu = r.get("gpu")
        if gpu is None:
            print("  GPU: NOT MEASURED")
        else:
            print(f"  GPU util avg={gpu['gpu_util_avg']:.1f}% max={gpu['gpu_util_max']:.1f}% "
                  f"memutil avg={gpu['gpu_memutil_avg']:.1f}%")
        print(f"  recording: {r.get('output_path')}")

    a = results.get("browser_cef_video", {}).get("summary", {})
    b = results.get("ffmpeg_source_native", {}).get("summary", {})
    if a.get("n") and b.get("n"):
        print("\n-- DELTA (browser_cef_video minus ffmpeg_source_native) --")
        print(f"  cpuUsage avg delta:        {a['cpu_avg'] - b['cpu_avg']:+.2f} pts")
        print(f"  avgRenderTimeMs avg delta: {a['avg_render_time_ms_avg'] - b['avg_render_time_ms_avg']:+.3f} ms")
        print(f"  renderSkipped delta ratio: {(a['render_skip_ratio'] - b['render_skip_ratio'])*100:+.3f} pts")
        print(f"  outputSkipped delta ratio: {(a['output_skip_ratio'] - b['output_skip_ratio'])*100:+.3f} pts")

    return 0


if __name__ == "__main__":
    sys.exit(main())
