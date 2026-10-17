#!/usr/bin/env python3
"""Profile native group composition using OBS render statistics only.

Starts an isolated copy of an already-qualified private runtime, drives one
synthetic scene over loopback obs-websocket, and samples OBS GetStats at
1920x1080/60. It never starts output, recording, streaming, physical capture,
or scene transitions. Runtime copies and generated fixtures are retained in
this worktree's .cache for inspection.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import statistics
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image, ImageDraw
import websockets

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence" / "local-20261017-composition-gpu-profile" / "eleven"
CACHE = ROOT / ".cache" / "native-composition-performance"
VIEWPORT = (1920, 1080)
TARGET_FPS = 60.0
TARGET_FRAME_MS = 1000.0 / TARGET_FPS
QUALIFIED_HASHES = {
    "pulsar.exe": "69ab22a5829bee254d53fa9edc463cfbdb33886a23781ae67fdc40fb1b36e973",
    "obs.dll": "891f6c58b1a9e865836fb94f03f8d90b238198b55c70d2656214cde4790478b1",
    "pulsar-capture-effects.dll": "9b17835e8db5ed452fd59e0fbe31d07f12e022df888540db6986e3a73918d093",
    "obs-websocket.dll": "d3ecaf55d8adc4a9e593589d0ec232ac4974db00980a41361a3fb8df989ef26b",
    "z-group-composition-shutdown-test-helper.dll": "f83277e677f6ecb28b12f5efc5234ed21176cfb4eb519cc1ce99e1d51d991eff",
}
SUPPORTED_BLEND_MODES = [
    "normal", "multiply", "screen", "hard-light", "soft-light", "overlay",
    "darken", "lighten", "difference", "exclusion", "color-dodge",
    "color-burn", "hue", "saturation", "color", "luminosity",
]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def numeric_stats(values: list[float]) -> dict:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "median": statistics.median(values) if values else None,
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
    }


def write_mask_fixture(path: pathlib.Path, phase: int) -> dict:
    """Two full-canvas alpha masks whose left/right coverage visibly flips."""
    image = Image.new("RGBA", VIEWPORT, (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    left_alpha, right_alpha = (255, 0) if phase == 0 else (0, 255)
    draw.rectangle((0, 0, VIEWPORT[0] // 2 - 1, VIEWPORT[1] - 1),
                   fill=(255, 255, 255, left_alpha))
    draw.rectangle((VIEWPORT[0] // 2, 0, VIEWPORT[0] - 1, VIEWPORT[1] - 1),
                   fill=(255, 255, 255, right_alpha))
    # Small diagonal strips keep the full atlas active without introducing
    # gradients or fractional alpha into the visual correctness check.
    strip_alpha = 255 if phase == 0 else 0
    for x in range(0, VIEWPORT[0], 128):
        draw.rectangle((x, 0, min(x + 15, VIEWPORT[0] - 1), VIEWPORT[1] - 1),
                       fill=(255, 255, 255, strip_alpha))
    image.save(path, format="PNG", optimize=True)
    return {
        "path": str(path), "sha256": sha256_file(path),
        "dimensions": [image.width, image.height],
        "alphaSamples": {
            "left": image.getpixel((VIEWPORT[0] // 4, VIEWPORT[1] // 2))[3],
            "right": image.getpixel((3 * VIEWPORT[0] // 4, VIEWPORT[1] // 2))[3],
        },
    }


class ProcessSampler:
    """Read process working-set/private bytes and CPU time using Windows APIs."""

    def __init__(self, pid: int):
        self.handle = None
        self.available = False
        self.error = None
        if os.name != "nt":
            self.error = "Windows process counters unavailable on this platform"
            return

        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        class MemoryCountersEx(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_uint32), ("page_fault_count", ctypes.c_uint32),
                ("peak_working_set_size", ctypes.c_size_t),
                ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                ("quota_non_paged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t),
                ("peak_pagefile_usage", ctypes.c_size_t),
                ("private_usage", ctypes.c_size_t),
            ]

        self.FileTime = FileTime
        self.MemoryCountersEx = MemoryCountersEx
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.kernel32 = kernel32
        self.psapi = psapi
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(FileTime), ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime), ctypes.POINTER(FileTime),
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(MemoryCountersEx), ctypes.c_uint32,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        self.handle = kernel32.OpenProcess(0x0410, 0, pid)  # QUERY_INFORMATION | VM_READ
        if not self.handle:
            self.error = f"OpenProcess failed ({ctypes.get_last_error()})"
            return
        self.available = True

    @staticmethod
    def _filetime_seconds(value) -> float:
        return ((int(value.high) << 32) | int(value.low)) / 10_000_000.0

    def sample(self) -> dict | None:
        if not self.available:
            return None
        memory = self.MemoryCountersEx()
        memory.cb = ctypes.sizeof(memory)
        if not self.psapi.GetProcessMemoryInfo(
                self.handle, ctypes.byref(memory), memory.cb):
            return None
        creation, exit_time = self.FileTime(), self.FileTime()
        kernel, user = self.FileTime(), self.FileTime()
        if not self.kernel32.GetProcessTimes(
                self.handle, ctypes.byref(creation), ctypes.byref(exit_time),
                ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return {
            "workingSetMiB": memory.working_set_size / 1024 / 1024,
            "privateMiB": memory.private_usage / 1024 / 1024,
            "cpuSeconds": self._filetime_seconds(kernel) + self._filetime_seconds(user),
        }

    def close(self) -> None:
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


class GpuSampler:
    """Sample first GPU's system-wide load; OBS-specific usage is unavailable."""

    def __init__(self):
        self.executable = shutil.which("nvidia-smi")
        self.samples: list[dict] = []
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None

    def start(self):
        if not self.executable:
            self.error = "nvidia-smi not found"
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        query = [self.executable, "--query-gpu=utilization.gpu,utilization.memory,memory.used",
                 "--format=csv,noheader,nounits"]
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                result = subprocess.run(query, capture_output=True, text=True,
                                        timeout=2.0, check=True,
                                        creationflags=0x08000000 if os.name == "nt" else 0)
                first = result.stdout.strip().splitlines()[0].split(",")
                self.samples.append({
                    "t": time.monotonic(), "gpuUtilPercent": float(first[0].strip()),
                    "memoryUtilPercent": float(first[1].strip()),
                    "gpuMemoryMiB": float(first[2].strip()),
                })
            except Exception as error:
                self.error = f"{type(error).__name__}: {error}"
            self.stop_event.wait(max(0.0, 1.0 - (time.monotonic() - started)))

    def stop(self) -> dict:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        return {
            "scope": "system-wide first NVIDIA GPU; not attributable to Pulsar",
            "sampleCount": len(self.samples), "error": self.error,
            "gpuUtilPercent": numeric_stats(
                [s["gpuUtilPercent"] for s in self.samples]),
            "memoryUtilPercent": numeric_stats(
                [s["memoryUtilPercent"] for s in self.samples]),
            "gpuMemoryMiB": numeric_stats(
                [s["gpuMemoryMiB"] for s in self.samples]),
            "samples": self.samples,
        }


def mask_ref(atlas_id: int) -> dict:
    return {
        "sceneItemId": atlas_id,
        "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
        "rect": {"x": 0, "y": 0, "width": VIEWPORT[0], "height": VIEWPORT[1]},
    }


def make_capture(gp, item_id: int, blend: str = "normal", opacity: float = 1.0,
                 mask: dict | None = None) -> dict:
    return {
        "kind": "capture", "sceneItemId": item_id,
        "affine": gp.affine(),
        "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
        "localOpacity": opacity, "visible": True,
        "blendMode": blend, "mask": mask,
    }


def make_plan(gp, scene: str, runtime_id: str, generation: int,
              replaced_ids: list[int], atlas_id: int,
              spec: dict) -> dict:
    """Build one version-2 global-texel graph for the described owners."""
    root_id = gp.ROOT_GROUP_ID
    root_children = []
    groups = [{
        "id": root_id, "parentId": None, "order": 0,
        "opacity": spec.get("rootOpacity", 1.0), "visible": True,
        "blendMode": spec.get("rootBlend", "normal"),
        "bounds": {"x": 0, "y": 0, "width": VIEWPORT[0], "height": VIEWPORT[1]},
        "affine": gp.affine(), "children": root_children,
        "mask": mask_ref(atlas_id) if spec.get("rootMask") else None,
    }]
    owner_defs = spec["owners"]
    for index, owner in enumerate(owner_defs):
        owner_id = f"perf-owner-{index}"
        root_children.append({"kind": "group", "id": owner_id})
        children = [make_capture(gp, item_id, capture.get("blend", "normal"),
                                 capture.get("opacity", 1.0),
                                 mask_ref(atlas_id) if capture.get("mask") else None)
                    for item_id, capture in zip(owner["items"], owner["captures"])]
        groups.append({
            "id": owner_id, "parentId": root_id, "order": index,
            "opacity": owner.get("opacity", 1.0), "visible": True,
            "blendMode": owner.get("blend", "normal"),
            "bounds": {"x": 0, "y": 0, "width": VIEWPORT[0], "height": VIEWPORT[1]},
            "affine": gp.affine(), "children": children,
            "mask": mask_ref(atlas_id) if owner.get("mask") else None,
        })
    return gp.make_global_v2_plan(scene, runtime_id, generation, groups,
                                  VIEWPORT, replaced_ids)


def single_owner_spec(items: list[int], **owner_args) -> dict:
    """Keep root fields on the root group and owner fields on the child group."""
    root_args = {}
    for key in ("rootMask", "rootBlend", "rootOpacity"):
        if key in owner_args:
            root_args[key] = owner_args.pop(key)
    return {**root_args, "owners": [{
        "items": items,
        "captures": [owner_args.pop("capture0", {}),
                     owner_args.pop("capture1", {})][:len(items)],
        **owner_args,
    }]}


def validate_plan_builder(gp) -> None:
    """Fail closed if the benchmark no longer exercises root and child masks."""
    spec = single_owner_spec(
        [101, 102], rootMask=True, rootBlend="overlay", rootOpacity=0.9,
        opacity=0.85, blend="multiply", mask=True,
        capture0={"blend": "screen", "opacity": 0.85, "mask": True},
        capture1={"blend": "difference", "opacity": 0.9, "mask": True})
    plan = make_plan(gp, "benchmark-plan-validation", "benchmark-runtime", 1,
                     [101, 102, 303], 303, spec)
    root, owner = plan["groups"]
    if (root.get("blendMode") != "overlay" or root.get("opacity") != 0.9 or
            root.get("mask", {}).get("sceneItemId") != 303):
        raise AssertionError("benchmark spec failed to place mask/blend/opacity on the root")
    if (owner.get("blendMode") != "multiply" or owner.get("opacity") != 0.85 or
            owner.get("mask", {}).get("sceneItemId") != 303):
        raise AssertionError("benchmark spec failed to mask/blend its group owner")
    if any(capture.get("mask", {}).get("sceneItemId") != 303
           for capture in owner["children"]):
        raise AssertionError("benchmark spec failed to mask both child captures")
    if [capture.get("blendMode") for capture in owner["children"]] != [
            "screen", "difference"]:
        raise AssertionError("benchmark spec lost its capture blend modes")


def summarize_scenario(samples: list[dict], process_samples: list[dict],
                       baseline: dict, duration_s: float,
                       gpu: dict, logical_cpu_count: int) -> dict:
    if len(samples) < max(8, int(duration_s * 0.7)):
        raise RuntimeError(f"insufficient GetStats samples: {len(samples)}")
    end_stats = samples[-1]["obs"]
    render_total_delta = int(end_stats.get("renderTotalFrames", 0) -
                             baseline.get("renderTotalFrames", 0))
    render_skipped_delta = int(end_stats.get("renderSkippedFrames", 0) -
                               baseline.get("renderSkippedFrames", 0))
    output_total_delta = int(end_stats.get("outputTotalFrames", 0) -
                             baseline.get("outputTotalFrames", 0))
    output_skipped_delta = int(end_stats.get("outputSkippedFrames", 0) -
                               baseline.get("outputSkippedFrames", 0))
    process_cpu_percent = []
    for previous, current in zip(process_samples, process_samples[1:]):
        dt = current["t"] - previous["t"]
        dcpu = current["cpuSeconds"] - previous["cpuSeconds"]
        if dt > 0 and dcpu >= 0:
            process_cpu_percent.append(100.0 * dcpu / dt / max(logical_cpu_count, 1))
    frame_times = [float(s["obs"]["averageFrameRenderTime"]) for s in samples
                   if isinstance(s["obs"].get("averageFrameRenderTime"), (int, float))]
    active_fps = [float(s["obs"]["activeFps"]) for s in samples
                  if isinstance(s["obs"].get("activeFps"), (int, float))]
    cpu_usage = [float(s["obs"]["cpuUsage"]) for s in samples
                 if isinstance(s["obs"].get("cpuUsage"), (int, float))]
    obs_memory = [float(s["obs"]["memoryUsage"]) for s in samples
                  if isinstance(s["obs"].get("memoryUsage"), (int, float))]
    working_set = [float(s["workingSetMiB"]) for s in process_samples]
    private_bytes = [float(s["privateMiB"]) for s in process_samples]
    return {
        "sampleCount": len(samples), "windowSeconds": round(duration_s, 3),
        "renderTargetFrameBudgetMs": round(TARGET_FRAME_MS, 6),
        "activeFps": numeric_stats(active_fps),
        "averageFrameRenderTimeMs": numeric_stats(frame_times),
        "averageFrameRenderTimeIsObsRollingAverage": True,
        "obsCpuUsagePercent": numeric_stats(cpu_usage),
        "obsMemoryUsageMiB": numeric_stats(obs_memory),
        "processCpuPercentOfLogicalCapacity": numeric_stats(process_cpu_percent),
        "processWorkingSetMiB": numeric_stats(working_set),
        "processPrivateBytesMiB": numeric_stats(private_bytes),
        "renderTotalFramesDelta": render_total_delta,
        "renderThreadAdvanced": render_total_delta > 0,
        "renderSkippedFramesDelta": render_skipped_delta,
        "renderSkipRatio": (render_skipped_delta / render_total_delta
                             if render_total_delta > 0 else None),
        "renderedFpsFromCounters": (render_total_delta / duration_s
                                     if duration_s > 0 else None),
        "outputTotalFramesDelta": output_total_delta,
        "outputSkippedFramesDelta": output_skipped_delta,
        "outputCountersExpectedZeroWithoutOutput": True,
        "statsBeforeWindow": {
            key: baseline.get(key) for key in (
                "renderTotalFrames", "renderSkippedFrames", "outputTotalFrames",
                "outputSkippedFrames", "activeFps", "averageFrameRenderTime")
        },
        "gpu": gpu,
    }


async def run_profile(args, report: dict, report_path: pathlib.Path,
                      stamp: str, gp, protocol, process_protocol,
                      runtime_dir: pathlib.Path, atlas_a: pathlib.Path,
                      atlas_b: pathlib.Path, fixture_meta: list[dict]):
    runtime_id = "composition-performance-" + uuid.uuid4().hex
    port = process_protocol.pick_free_port()
    password = uuid.uuid4().hex + uuid.uuid4().hex
    process = gp.GracefulPrivatePulsarProcess(
        process_protocol, runtime_dir / "bin" / "64bit" / "pulsar.exe", port, password)
    process_sampler = None
    report["runtimeInstanceId"] = runtime_id
    previous_env = {key: value for key, value in os.environ.items()
                    if key.startswith("PULSAR_")}
    scene = f"native-composition-perf-{uuid.uuid4().hex[:10]}"
    backdrop_name = scene + "-backdrop"
    atlas_name = scene + "-atlas"
    capture_names = [f"{scene}-capture-{i}" for i in range(8)]
    atlas_item_id = None
    replacement_ids: list[int] = []
    generation = 0
    ws = None
    inbox = None
    report["syntheticScene"] = scene
    try:
        for key in list(os.environ):
            if key.startswith("PULSAR_"):
                del os.environ[key]
        os.environ.update({
            "PULSAR_RUNTIME_ROOT": str(runtime_dir.parent / "state"),
            "PULSAR_RUNTIME_DIR": str(runtime_dir.parent / "state" / "runtime"),
            "PULSAR_RUNTIME_INSTANCE_ID": runtime_id,
            "PULSAR_DESKTOP_AUDIO_DEVICE_ID": "pulsar-synthetic-test-no-device",
            "PULSAR_TEST_NO_BROWSER_PRE_SHUTDOWN": "1",
        })
        process.spawn()
        report["ownedPid"] = process.proc.proc.pid
        write_report(report_path, report)
        print(f"PROFILE_HOST pid={process.proc.proc.pid} runnerPid={os.getpid()}", flush=True)
        process.wait_ready(60)
        await asyncio.sleep(2)
        initialized = [line for line in process._lines
                       if "WASAPI:" in line and " initialized " in line]
        if initialized:
            raise RuntimeError("private host initialized an audio endpoint")
        report["bootstrap"] = {
            "initializedWasapiDevices": initialized,
            "noBrowserModules": True,
            "microphoneConfigured": False,
            "processAudioConfigured": False,
        }

        async with websockets.connect(
                f"ws://127.0.0.1:{port}", subprotocols=["obswebsocket.json"],
                max_size=2**24) as socket:
            ws = socket
            hello = json.loads(await ws.recv())["d"]
            identify = {"rpcVersion": hello["rpcVersion"], "eventSubscriptions": 0}
            if "authentication" in hello:
                auth = hello["authentication"]
                identify["authentication"] = protocol.compute_auth(
                    password, auth["salt"], auth["challenge"])
            await ws.send(json.dumps({"op": 1, "d": identify}))
            identified = json.loads(await ws.recv())
            if identified.get("op") != 2:
                raise RuntimeError("obs-websocket Identify did not complete")
            inbox = protocol.Inbox()

            async def call(request_type: str, data: dict | None = None) -> dict:
                response = await protocol.request(
                    inbox, ws, request_type, uuid.uuid4().hex, data)
                status = response.get("requestStatus", {})
                if not status.get("result"):
                    raise RuntimeError(f"{request_type} failed: {status}")
                return response.get("responseData", {})

            async def secondary_stats_client():
                secondary = await websockets.connect(
                    f"ws://127.0.0.1:{port}", subprotocols=["obswebsocket.json"],
                    max_size=2**24)
                secondary_hello = json.loads(await secondary.recv())["d"]
                secondary_identify = {
                    "rpcVersion": secondary_hello["rpcVersion"],
                    "eventSubscriptions": 0,
                }
                if "authentication" in secondary_hello:
                    auth = secondary_hello["authentication"]
                    secondary_identify["authentication"] = protocol.compute_auth(
                        password, auth["salt"], auth["challenge"])
                await secondary.send(json.dumps({"op": 1, "d": secondary_identify}))
                identified = json.loads(await secondary.recv())
                if identified.get("op") != 2:
                    await secondary.close()
                    raise RuntimeError("independent stats websocket did not identify")
                secondary_inbox = protocol.Inbox()

                async def get_stats(timeout: float = 2.0) -> dict:
                    response = await protocol.request(
                        secondary_inbox, secondary, "GetStats", uuid.uuid4().hex,
                        timeout=timeout)
                    status = response.get("requestStatus", {})
                    if not status.get("result"):
                        raise RuntimeError(f"independent GetStats failed: {status}")
                    return response.get("responseData", {})

                return secondary, get_stats

            async def diagnostic_snapshot_after_window(label: str) -> dict:
                """Read pixels after the timed window while an independent
                websocket checks whether OBS stats/render counters still answer.
                """
                screenshot_task = asyncio.create_task(call("GetSourceScreenshot", {
                    "sourceName": scene, "imageFormat": "png",
                    "imageWidth": 128, "imageHeight": 72,
                }))
                await asyncio.sleep(0.2)
                secondary = None
                stats_samples = []
                errors = []
                try:
                    secondary, get_secondary_stats = await secondary_stats_client()
                    deadline = time.monotonic() + 11.0
                    while not screenshot_task.done() and time.monotonic() < deadline:
                        started = time.monotonic()
                        try:
                            stats = await get_secondary_stats(timeout=1.5)
                            stats_samples.append({
                                "t": round(time.monotonic() - started, 3),
                                "latencyMs": round((time.monotonic() - started) * 1000, 3),
                                "obs": {key: stats.get(key) for key in (
                                    "activeFps", "averageFrameRenderTime",
                                    "renderTotalFrames", "renderSkippedFrames")},
                            })
                        except Exception as error:
                            errors.append(f"{type(error).__name__}: {error}")
                        await asyncio.sleep(0.5)
                    if not screenshot_task.done():
                        await asyncio.wait({screenshot_task}, timeout=1.0)
                    if screenshot_task.done():
                        try:
                            result = screenshot_task.result()
                            png = base64.b64decode(result["imageData"].split(",", 1)[1])
                            path = EVIDENCE / f"{stamp}-{label}-after-window.png"
                            path.write_bytes(png)
                            image = Image.open(BytesIO(png)).convert("RGBA")
                            pixel_data = {
                                "path": str(path), "sha256": sha256_bytes(png),
                                "dimensions": list(image.size),
                                "samplePixels": {
                                    "left": list(image.getpixel((24, 36))),
                                    "right": list(image.getpixel((96, 36))),
                                },
                            }
                            return {
                                "status": "success", "screenshot": pixel_data,
                                "independentGetStatsSamples": stats_samples,
                                "independentGetStatsErrors": errors,
                                "independentRenderCounterAdvanced": (
                                    len(stats_samples) >= 2 and
                                    stats_samples[-1]["obs"].get("renderTotalFrames", 0) >
                                    stats_samples[0]["obs"].get("renderTotalFrames", 0)),
                            }
                        except Exception as error:
                            error_status = (
                                "timeout" if isinstance(error, asyncio.TimeoutError)
                                else "failed")
                            return {
                                "status": error_status,
                                "errorType": type(error).__name__,
                                "error": str(error)[:1000],
                                "independentGetStatsSamples": stats_samples,
                                "independentGetStatsErrors": errors,
                            }
                    return {
                        "status": "timeout", "timeoutSeconds": 12.2,
                        "primaryWebsocketRequestTimeoutSeconds": 10,
                        "independentGetStatsSamples": stats_samples,
                        "independentGetStatsErrors": errors,
                        "independentRenderCounterAdvanced": (
                            len(stats_samples) >= 2 and
                            stats_samples[-1]["obs"].get("renderTotalFrames", 0) >
                            stats_samples[0]["obs"].get("renderTotalFrames", 0)),
                    }
                finally:
                    if secondary is not None:
                        await secondary.close()

            video = await call("GetVideoSettings")
            stats = await call("GetStats")
            record_status = await call("GetRecordStatus")
            stream_status = await call("GetStreamStatus")
            report["videoSettings"] = video
            report["initialGetStats"] = stats
            report["outputStateBeforeSampling"] = {
                "record": record_status, "stream": stream_status,
            }
            write_report(report_path, report)
            if (int(video.get("baseWidth", 0)), int(video.get("baseHeight", 0))) != VIEWPORT:
                raise RuntimeError(f"qualified runtime canvas is not 1920x1080: {video}")
            fps = float(video.get("fpsNumerator", 0)) / max(
                float(video.get("fpsDenominator", 0)), 1.0)
            if abs(fps - TARGET_FPS) > 0.01:
                raise RuntimeError(f"qualified runtime is not 60fps: {video}")
            if record_status.get("outputActive") or stream_status.get("outputActive"):
                raise RuntimeError("private host unexpectedly started an output")

            await call("CreateScene", {"sceneName": scene})
            await call("CreateInput", {
                "sceneName": scene, "inputName": backdrop_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF202030, "width": VIEWPORT[0],
                                  "height": VIEWPORT[1]},
                "sceneItemEnabled": True,
            })
            capture_ids = []
            for index, input_name in enumerate(capture_names):
                created = await call("CreateInput", {
                    "sceneName": scene, "inputName": input_name,
                    "inputKind": "color_source_v3",
                    "inputSettings": {
                        "color": [0xFFFF3030, 0xFF30FF70, 0xFF4080FF,
                                  0xFFFFFF30, 0xFFFF30D0, 0xFF30E0FF,
                                  0xFFFF9030, 0xFFB060FF][index],
                        "width": VIEWPORT[0], "height": VIEWPORT[1],
                    }, "sceneItemEnabled": False,
                })
                capture_ids.append(int(created["sceneItemId"]))
            created_atlas = await call("CreateInput", {
                "sceneName": scene, "inputName": atlas_name,
                "inputKind": "image_source",
                "inputSettings": {"file": str(atlas_a)},
                "sceneItemEnabled": False,
            })
            atlas_item_id = int(created_atlas["sceneItemId"])
            replacement_ids = capture_ids + [atlas_item_id]
            report["sceneInventory"] = {
                "backdropSceneItemId": int((await call("GetSceneItemId", {
                    "sceneName": scene, "sourceName": backdrop_name,
                }))["sceneItemId"]),
                "captureSceneItemIds": capture_ids,
                "atlasSceneItemId": atlas_item_id,
                "replacedSceneItems": replacement_ids,
                "fullResolutionSyntheticSolidCaptures": len(capture_ids),
            }
            await call("SetCurrentProgramScene", {"sceneName": scene})
            report["programSceneConfiguredOnceBeforeSampling"] = True
            await asyncio.sleep(5)
            baseline_scene = await call("GetCurrentProgramScene")
            if baseline_scene.get("sceneName") != scene:
                raise RuntimeError(f"profile scene is not Program scene: {baseline_scene}")
            report["programSceneBeforeComposition"] = baseline_scene
            report["nominalNoCompositionSetupStats"] = await call("GetStats")

            logical_cpu_count = os.cpu_count() or 1
            process_sampler = ProcessSampler(process.proc.proc.pid)
            report["processCounters"] = {
                "available": process_sampler.available,
                "error": process_sampler.error,
                "logicalCpuCount": logical_cpu_count,
            }
            scenarios: list[dict] = []

            plan_specs = [
                ("single-normal", single_owner_spec(capture_ids[:2]), None),
                ("root-and-capture-masks-blends", single_owner_spec(
                    capture_ids[:2], rootMask=True, rootBlend="overlay",
                    rootOpacity=0.9, opacity=0.85, blend="multiply", mask=True,
                    capture0={"blend": "screen", "opacity": 0.85, "mask": True},
                    capture1={"blend": "difference", "opacity": 0.9, "mask": True}), None),
                ("root-mask-multiply-opacity", single_owner_spec(
                    capture_ids[:2], rootMask=True, rootBlend="multiply",
                    rootOpacity=0.8), None),
                ("capture-mask-screen", single_owner_spec(
                    capture_ids[:2], capture0={"blend": "screen", "opacity": 0.8,
                                               "mask": True},
                    capture1={"blend": "difference", "opacity": 0.8,
                             "mask": True}), None),
            ]

            def owners_for_four() -> dict:
                blend_pairs = [
                    ("multiply", "hard-light"), ("screen", "soft-light"),
                    ("overlay", "darken"), ("difference", "lighten"),
                ]
                owners = []
                for index, (group_blend, capture_blend) in enumerate(blend_pairs):
                    owners.append({
                        "items": capture_ids[index * 2:index * 2 + 2],
                        "captures": [
                            {"blend": capture_blend, "opacity": 0.85, "mask": True},
                            {"blend": group_blend, "opacity": 0.9, "mask": True},
                        ],
                        "opacity": 0.85, "blend": group_blend, "mask": True,
                    })
                return {"rootBlend": "normal", "rootOpacity": 1.0,
                        "owners": owners}

            def owners_for_two() -> dict:
                blend_pairs = [("multiply", "hard-light"),
                               ("screen", "soft-light")]
                owners = []
                for index, (group_blend, capture_blend) in enumerate(blend_pairs):
                    owners.append({
                        "items": capture_ids[index * 2:index * 2 + 2],
                        "captures": [
                            {"blend": capture_blend, "opacity": 0.85, "mask": True},
                            {"blend": group_blend, "opacity": 0.9, "mask": True},
                        ],
                        "opacity": 0.85, "blend": group_blend, "mask": True,
                    })
                return {"rootBlend": "normal", "rootOpacity": 1.0,
                        "owners": owners}

            plan_specs.append(("two-masked-blended-owners", owners_for_two(), None))
            plan_specs.append(("four-masked-blended-owners", owners_for_four(), None))
            dynamic_spec = single_owner_spec(
                capture_ids[:2], rootMask=True, rootBlend="overlay",
                rootOpacity=0.9,
                capture0={"blend": "screen", "opacity": 0.85},
                capture1={"blend": "multiply", "opacity": 0.9})
            plan_specs.append(("dynamic-atlas-active", dynamic_spec, "dynamic"))

            async def snapshot(phase: str) -> dict:
                result = await call("GetSourceScreenshot", {
                    "sourceName": scene, "imageFormat": "png",
                    "imageWidth": 128, "imageHeight": 72,
                })
                png = base64.b64decode(result["imageData"].split(",", 1)[1])
                path = EVIDENCE / f"{stamp}-{phase}.png"
                path.write_bytes(png)
                image = Image.open(BytesIO(png)).convert("RGBA")
                return {
                    "path": str(path), "sha256": sha256_bytes(png),
                    "dimensions": list(image.size),
                    "samplePixels": {
                        "left": list(image.getpixel((24, 36))),
                        "right": list(image.getpixel((96, 36))),
                    },
                }

            nominal_scene_pixels = await snapshot("nominal-no-composition")
            report["nominalNoCompositionPixels"] = nominal_scene_pixels

            async def set_atlas(path: pathlib.Path) -> float:
                started = time.perf_counter()
                await call("SetInputSettings", {
                    "inputName": atlas_name,
                    "inputSettings": {"file": str(path)}, "overlay": True,
                })
                return (time.perf_counter() - started) * 1000.0

            selected_specs = plan_specs[
                args.scenario_start:args.scenario_start + args.scenario_limit]
            for local_index, (label, spec, mode) in enumerate(selected_specs):
                scenario_index = args.scenario_start + local_index
                generation += 1
                if mode == "dynamic":
                    setup_latency = await set_atlas(atlas_a)
                    report["dynamicAtlasBeforeWindowSetInputMs"] = setup_latency
                plan = make_plan(gp, scene, runtime_id, generation,
                                 replacement_ids, atlas_item_id, spec)
                committed = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": plan,
                })
                outcome = committed.get("responseData", {})
                if not outcome.get("ok"):
                    raise RuntimeError(f"composition commit refused for {label}: {outcome}")
                scenarios_before = time.monotonic()
                await asyncio.sleep(4 if scenario_index == 0 else 2)
                program_scene_before = await call("GetCurrentProgramScene")
                if program_scene_before.get("sceneName") != scene:
                    raise RuntimeError(f"profile scene left Program before {label}: {program_scene_before}")
                deferred_readback = label in {
                    "two-masked-blended-owners", "four-masked-blended-owners"}
                before_scene = None if deferred_readback else await snapshot(
                    f"{label}-before" if mode == "dynamic" else
                    f"{label}-active-program")
                baseline = await call("GetStats")
                gpu_sampler = GpuSampler()
                gpu_sampler.start()
                samples = []
                process_samples = []
                update_latencies = []
                next_sample = time.monotonic()
                next_update = time.monotonic() + 4.0
                update_index = 0
                start = time.monotonic()
                deadline = start + args.seconds
                report["inProgressScenario"] = {
                    "label": label, "generation": generation,
                    "planHash": plan["planHash"], "wirePlan": plan,
                    "commitResponse": outcome,
                    "programSceneBeforeWindow": program_scene_before,
                    "scenePixelReadbackBeforeWindow": before_scene,
                    "pixelReadbackDeferredUntilAfterWindow": deferred_readback,
                    "baselineStats": baseline, "rawSamples": samples,
                }
                write_report(report_path, report)
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if mode == "dynamic" and now >= next_update:
                        update_index += 1
                        update_path = atlas_b if update_index % 2 else atlas_a
                        update_latencies.append(await set_atlas(update_path))
                        next_update += 4.0
                    next_sample += 1.0
                    await asyncio.sleep(max(0.0, next_sample - time.monotonic()))
                    sample_start = time.monotonic()
                    obs_stats = await call("GetStats")
                    process_data = process_sampler.sample() if process_sampler else None
                    sample = {
                        "t": round(sample_start - start, 3),
                        "getStatsLatencyMs": round((time.monotonic() - sample_start) * 1000, 3),
                        "obs": {key: obs_stats.get(key) for key in (
                            "cpuUsage", "memoryUsage", "activeFps",
                            "averageFrameRenderTime", "renderSkippedFrames",
                            "renderTotalFrames", "outputSkippedFrames",
                            "outputTotalFrames")},
                    }
                    if process_data is not None:
                        process_data["t"] = sample_start
                        sample["process"] = process_data
                        process_samples.append(process_data)
                    samples.append(sample)
                    report["inProgressScenario"]["rawSamples"] = samples
                    report["inProgressScenario"]["sampleCount"] = len(samples)
                    write_report(report_path, report)
                    print(f"[{label}] {len(samples):02d}/{args.seconds}s "
                          f"fps={obs_stats.get('activeFps')} "
                          f"render-ms={obs_stats.get('averageFrameRenderTime')} "
                          f"render-skips={obs_stats.get('renderSkippedFrames')} "
                          f"gpu={gpu_sampler.samples[-1].get('gpuUtilPercent') if gpu_sampler.samples else 'n/a'}",
                          flush=True)
                duration = time.monotonic() - start
                gpu = gpu_sampler.stop()
                final_scene = await snapshot(f"{label}-after") \
                    if mode == "dynamic" else None
                deferred_pixel_readback = (
                    await diagnostic_snapshot_after_window(label)
                    if deferred_readback else None)
                output_end = {
                    "record": await call("GetRecordStatus"),
                    "stream": await call("GetStreamStatus"),
                }
                if (output_end["record"].get("outputActive") or
                        output_end["stream"].get("outputActive")):
                    raise RuntimeError("recording/streaming became active during the profile")
                summary = summarize_scenario(samples, process_samples,
                                             baseline, duration, gpu,
                                             logical_cpu_count)
                render_window_valid = summary["renderTotalFramesDelta"] > 0
                if not render_window_valid and not deferred_readback:
                    raise RuntimeError(
                        f"OBS render thread did not advance during {label}; "
                        "the scene cannot be treated as actively rendered")
                program_scene_after = await call("GetCurrentProgramScene")
                if program_scene_after.get("sceneName") != scene:
                    raise RuntimeError(f"profile scene left Program during {label}: {program_scene_after}")
                scenario_result = {
                    "label": label, "generation": generation,
                    "planHash": plan["planHash"],
                    "wirePlan": plan,
                    "commitResponse": outcome,
                    "programSceneBeforeWindow": program_scene_before,
                    "programSceneAfterWindow": program_scene_after,
                    "sceneRemainedProgramForEntireWindow": True,
                    "compositionWasCommittedForEntireWindow": True,
                    "renderThreadAdvancedDuringWindow": render_window_valid,
                    "scenePixelReadbackBeforeWindow": before_scene,
                    "scenePixelReadbackAfterWindow": deferred_pixel_readback,
                    "workload": {
                        "ownerGroups": len(spec["owners"]),
                        "captureRefs": sum(len(owner["items"])
                                            for owner in spec["owners"]),
                        "rootMask": bool(spec.get("rootMask")),
                        "rootBlend": spec.get("rootBlend", "normal"),
                        "ownerBlends": [owner.get("blend", "normal")
                                        for owner in spec["owners"]],
                        "captureMasks": sum(
                            bool(capture.get("mask"))
                            for owner in spec["owners"] for capture in owner["captures"]),
                        "masksResolveFromSingleDynamicImageSource": True,
                    },
                    "summary": summary,
                    "rawSamples": samples,
                    "outputStateAfter": output_end,
                    "durationSeconds": round(duration, 3),
                }
                if mode == "dynamic":
                    scene_changed = before_scene["sha256"] != final_scene["sha256"]
                    if not scene_changed:
                        raise AssertionError("active atlas update did not change composition pixels")
                    scenario_result["dynamicAtlas"] = {
                        "fixtureA": fixture_meta[0], "fixtureB": fixture_meta[1],
                        "initialSceneScreenshot": before_scene,
                        "finalSceneScreenshot": final_scene,
                        "scenePixelsChanged": scene_changed,
                        "setInputSettingsUpdateCount": update_index,
                        "setInputSettingsLatencyMs": numeric_stats(update_latencies),
                        "updateIntervalSeconds": 4,
                        "interpretation": (
                            "synthetic image_source atlas switched between two full-resolution alpha masks "
                            "while the /2 graph remained active; this includes source-settings/reload cost "
                            "and is not an end-to-end Solar atlas producer measurement"),
                    }
                    scenario_result["caseValidated"] = True
                elif deferred_readback:
                    readback_ok = (
                        render_window_valid and
                        deferred_pixel_readback.get("status") == "success" and
                        deferred_pixel_readback["screenshot"]["sha256"] !=
                        nominal_scene_pixels["sha256"])
                    scenario_result["pixelReadbackDistinguishedFromNominal"] = readback_ok
                    scenario_result["caseValidated"] = readback_ok
                    if not readback_ok:
                        scenario_result["caseValidationLimitation"] = (
                            "The timed Program/render window completed, but the separate post-window "
                            "pixel readback did not return a discriminating image.")
                else:
                    scenario_result["caseValidated"] = True
                scenarios.append(scenario_result)
                report["scenarios"] = scenarios
                report.pop("inProgressScenario", None)
                report["firstMeasuredResultAvailable"] = True
                report["latestScenario"] = label
                report["elapsedSeconds"] = round(time.monotonic() - scenarios_before, 3)
                write_report(report_path, report)
                avg = summary["averageFrameRenderTimeMs"]
                print(f"RESULT {label}: fps p50={summary['activeFps']['median']:.2f}; "
                      f"OBS render avg p50/p95/max={avg['median']:.3f}/"
                      f"{avg['p95']:.3f}/{avg['max']:.3f}ms; "
                      f"render-skips={summary['renderSkippedFramesDelta']}; "
                      f"GPU p50/max={gpu['gpuUtilPercent']['median']}/"
                      f"{gpu['gpuUtilPercent']['max']}%", flush=True)

            # Exercise a bounded retained-allocation lifecycle after the timed
            # windows. Recommit a representative two-owner graph three times,
            # explicitly reset each generation, and record settled
            # OBS/process/system GPU stats. The four-owner reset is separately
            # diagnosed below via the scenario evidence; this is not a leak stress test.
            retention_spec = plan_specs[4][1]
            cycles = []

            async def lifecycle_sample(phase: str, cycle_number: int,
                                       baseline_stats: dict) -> dict:
                await asyncio.sleep(2)
                obs_stats = await call("GetStats")
                process_data = process_sampler.sample() if process_sampler else None
                gpu_sampler = GpuSampler()
                gpu_sampler.start()
                await asyncio.sleep(1.1)
                gpu = gpu_sampler.stop()
                output_state = {
                    "record": await call("GetRecordStatus"),
                    "stream": await call("GetStreamStatus"),
                }
                if (output_state["record"].get("outputActive") or
                        output_state["stream"].get("outputActive")):
                    raise RuntimeError("recording/streaming became active during lifecycle sampling")
                return {
                    "cycle": cycle_number, "phase": phase,
                    "settleSeconds": 2,
                    "obs": {key: obs_stats.get(key) for key in (
                        "cpuUsage", "memoryUsage", "activeFps",
                        "averageFrameRenderTime", "renderSkippedFrames",
                        "renderTotalFrames", "outputSkippedFrames",
                        "outputTotalFrames")},
                    "renderTotalFramesDeltaSincePhaseStart": int(
                        obs_stats.get("renderTotalFrames", 0) -
                        baseline_stats.get("renderTotalFrames", 0)),
                    "process": process_data,
                    "gpu": gpu,
                    "outputState": output_state,
                }

            generation += 1
            retention_plan = make_plan(
                gp, scene, runtime_id, generation, replacement_ids,
                atlas_item_id, retention_spec)
            initial_stats = await call("GetStats")
            report["memoryRetentionCycles"] = {
                "purpose": (
                    "Three bounded two-owner masked/blended commit/reset repetitions to distinguish "
                    "retained allocation from monotonic per-repetition growth; not a stress test."),
                "cycleCount": 3,
                "settleSecondsPerPhase": 2,
                "planHash": retention_plan["planHash"],
                "wirePlan": retention_plan,
                "beforeCycles": {
                    "obs": {key: initial_stats.get(key) for key in (
                        "memoryUsage", "renderTotalFrames", "renderSkippedFrames")},
                    "process": process_sampler.sample() if process_sampler else None,
                },
                "cycles": cycles,
            }
            write_report(report_path, report)
            for cycle_number in range(1, 4):
                if cycle_number > 1:
                    generation += 1
                retention_plan = make_plan(
                    gp, scene, runtime_id, generation, replacement_ids,
                    atlas_item_id, retention_spec)
                pre_commit = await call("GetStats")
                committed = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": retention_plan,
                })
                commit_response = committed.get("responseData", {})
                if not commit_response.get("ok"):
                    raise RuntimeError(
                        f"retention cycle {cycle_number} commit refused: {commit_response}")
                after_commit = await lifecycle_sample(
                    "after-commit", cycle_number, pre_commit)
                cycle = {
                    "cycle": cycle_number, "generation": generation,
                    "planHash": retention_plan["planHash"],
                    "wirePlan": retention_plan,
                    "commitResponse": commit_response,
                    "afterCommit": after_commit,
                }
                cycles.append(cycle)
                report["memoryRetentionCycles"]["cycles"] = cycles
                write_report(report_path, report)
                pre_reset = await call("GetStats")
                try:
                    reset = await call("CallVendorRequest", {
                        "vendorName": "pulsar-capture-effects",
                        "requestType": "ResetGroupComposition",
                        "requestData": {"sceneName": scene,
                                        "generation": generation,
                                        "runtimeInstanceId": runtime_id},
                    })
                except Exception as error:
                    cycle["resetStatus"] = "failed"
                    cycle["resetErrorType"] = type(error).__name__
                    cycle["resetError"] = str(error)[:1000]
                    report["memoryRetentionCycles"]["cycles"] = cycles
                    write_report(report_path, report)
                    raise
                reset_response = reset.get("responseData", {})
                if not (reset_response.get("ok") and reset_response.get("reset")):
                    cycle["resetStatus"] = "refused"
                    cycle["resetResponse"] = reset_response
                    report["memoryRetentionCycles"]["cycles"] = cycles
                    write_report(report_path, report)
                    raise RuntimeError(
                        f"retention cycle {cycle_number} reset refused: {reset_response}")
                after_reset = await lifecycle_sample(
                    "after-reset", cycle_number, pre_reset)
                cycle["resetStatus"] = "success"
                cycle["resetResponse"] = reset_response
                cycle["afterReset"] = after_reset
                report["memoryRetentionCycles"]["cycles"] = cycles
                write_report(report_path, report)
                print(f"LIFECYCLE cycle={cycle_number}/3 "
                      f"privateMiB commit/reset="
                      f"{(after_commit.get('process') or {}).get('privateMiB')}/"
                      f"{(after_reset.get('process') or {}).get('privateMiB')} "
                      f"GPU-MiB-system={after_commit['gpu']['gpuMemoryMiB']['median']}/"
                      f"{after_reset['gpu']['gpuMemoryMiB']['median']}", flush=True)

            return {
                "scenarios": scenarios,
                "generation": generation,
                "memoryRetentionCycles": report.get("memoryRetentionCycles"),
                "ownedProcessExited": False,
            }
    finally:
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if process_sampler:
            process_sampler.close()
        try:
            process.shutdown(grace=30.0)
        except Exception as error:
            report["shutdownError"] = f"{type(error).__name__}: {error}"
        report["ownedProcessExited"] = (
            process.proc.proc is None or process.proc.proc.poll() is not None)
        report["gracefulShutdownRequested"] = process.graceful_shutdown_requested
        report["forcedTerminationUsed"] = process.forced_termination_used
        report["shutdownTimeoutSeconds"] = 30
        tail = process._diag()
        report["redactedShutdownTail"] = tail[-30:] if isinstance(tail, list) else str(tail)[-3000:]
        report["privateRuntimeRetained"] = True
        report["privateRuntimePath"] = str(runtime_dir)
        for key in list(os.environ):
            if key.startswith("PULSAR_"):
                del os.environ[key]
        os.environ.update(previous_env)
        write_report(report_path, report)


def write_report(path: pathlib.Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    temp.replace(path)


def verify_inputs(runtime: pathlib.Path, plugin: pathlib.Path,
                  websocket_plugin: pathlib.Path, helper: pathlib.Path) -> dict:
    paths = {
        "pulsar.exe": runtime / "bin" / "64bit" / "pulsar.exe",
        "obs.dll": runtime / "bin" / "64bit" / "obs.dll",
        "pulsar-capture-effects.dll": plugin,
        "obs-websocket.dll": websocket_plugin,
        "z-group-composition-shutdown-test-helper.dll": helper,
    }
    result = {}
    for name, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(path)
        actual = sha256_file(path)
        expected = QUALIFIED_HASHES[name]
        if actual != expected:
            raise RuntimeError(f"{name} hash differs from qualified private binary: {actual}")
        result[name] = {"path": str(path), "sha256": actual,
                        "bytes": path.stat().st_size, "matchesPriorQualification": True}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=pathlib.Path, required=True,
                        help="qualified private runtime rundir/RelWithDebInfo")
    parser.add_argument("--plugin", type=pathlib.Path, required=True,
                        help="qualified /2 pulsar-capture-effects.dll")
    parser.add_argument("--websocket-plugin", type=pathlib.Path, required=True,
                        help="qualified obs-websocket.dll")
    parser.add_argument("--graphics-remove-helper", type=pathlib.Path, required=True,
                        help="qualified no-browser pre-shutdown fixture module")
    parser.add_argument("--seconds", type=int, default=20,
                        help="sample seconds per scenario, 20-30 (default 20)")
    parser.add_argument("--scenario-limit", type=int, default=7,
                        help="run up to N scenarios (1-7); combine with --scenario-start for bounded windows")
    parser.add_argument("--scenario-start", type=int, default=0,
                        help="zero-based first scenario index (0-6)")
    parser.add_argument("--output", type=pathlib.Path, required=True,
                        help="canonical evidence/local-20261017-composition-gpu-profile/eleven")
    args = parser.parse_args()
    if not 20 <= args.seconds <= 30:
        raise ValueError("--seconds must be between 20 and 30")
    if not 1 <= args.scenario_limit <= 7:
        raise ValueError("--scenario-limit must be between 1 and 7")
    if not 0 <= args.scenario_start <= 6:
        raise ValueError("--scenario-start must be between 0 and 6")
    if args.scenario_start + args.scenario_limit > 7:
        raise ValueError("scenario start plus limit must not exceed seven scenarios")
    output = args.output.resolve()
    if output != EVIDENCE.resolve():
        raise ValueError(f"output must be canonical: {EVIDENCE}")
    runtime_source = args.runtime.resolve()
    plugin_source = args.plugin.resolve()
    websocket_source = args.websocket_plugin.resolve()
    helper_source = args.graphics_remove_helper.resolve()
    provenance = verify_inputs(runtime_source, plugin_source,
                               websocket_source, helper_source)
    browser_modules = sorted(
        path.name for path in (runtime_source / "obs-plugins" / "64bit").glob("*.dll")
        if "browser" in path.name.casefold())
    if browser_modules:
        raise RuntimeError("qualified no-browser fixture contains browser modules: " +
                           ", ".join(browser_modules))
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    scratch = CACHE / ("private-runtime-" + uuid.uuid4().hex)
    if scratch.exists() or not scratch.resolve().is_relative_to(CACHE.resolve()):
        raise RuntimeError("private runtime path is not a new cache child")
    runtime_dir = scratch / "runtime"
    runtime_dir.mkdir(parents=True)
    for name in ("bin", "data", "obs-plugins"):
        shutil.copytree(runtime_source / name, runtime_dir / name)
    plugin_targets = {
        "pulsar-capture-effects.dll": plugin_source,
        "obs-websocket.dll": websocket_source,
        "z-group-composition-shutdown-test-helper.dll": helper_source,
    }
    for name, source in plugin_targets.items():
        shutil.copyfile(source, runtime_dir / "obs-plugins" / "64bit" / name)
    copied_hashes = {
        name: sha256_file(runtime_dir / "obs-plugins" / "64bit" / name)
        for name in plugin_targets
    }
    for name, digest in copied_hashes.items():
        if digest != QUALIFIED_HASHES[name]:
            raise RuntimeError(f"staged {name} no longer matches qualified hash")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = output / f"{stamp}-group-composition-gpu-profile.json"
    if report_path.exists():
        raise FileExistsError(report_path)
    atlas_a = CACHE / f"{stamp}-atlas-a.png"
    atlas_b = CACHE / f"{stamp}-atlas-b.png"
    fixture_meta = [write_mask_fixture(atlas_a, 0), write_mask_fixture(atlas_b, 1)]
    report = {
        "startedUtc": datetime.now(timezone.utc).isoformat(), "passed": False,
        "profileType": "offline synthetic OBS render workload",
        "sourceHead": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True).stdout.strip(),
        "sourceWorktree": str(ROOT),
        "scope": {
            "viewport": list(VIEWPORT), "targetFps": TARGET_FPS,
            "frameBudgetMs": round(TARGET_FRAME_MS, 6),
            "scenariosSeconds": args.seconds,
            "scenarioStart": args.scenario_start,
            "scenarioLimit": args.scenario_limit,
            "selectedScenarioLabels": [
                "single-normal", "root-and-capture-masks-blends",
                "root-mask-multiply-opacity", "capture-mask-screen",
                "two-masked-blended-owners", "four-masked-blended-owners",
                "dynamic-atlas-active",
            ][args.scenario_start:args.scenario_start + args.scenario_limit],
            "samplesPerSecond": 1,
            "sampledMetrics": ["OBS GetStats", "Windows process CPU time and memory",
                               "nvidia-smi system-wide GPU utilization"],
            "noRecordingOrStreaming": True,
            "noPhysicalCapture": True,
            "sceneSelectedOnceBeforeSampling": True,
            "transitionAndSwitchTimingExcluded": True,
        },
        "inputs": provenance,
        "stagedRuntimeHashes": copied_hashes,
        "browserModulesInRuntime": browser_modules,
        "privateRuntimePath": str(runtime_dir),
        "privateRuntimeRetained": True,
        "generatedMaskFixtures": fixture_meta,
        "scenarioLabels": ["single-normal", "root-and-capture-masks-blends",
                           "root-mask-multiply-opacity",
                           "capture-mask-screen", "two-masked-blended-owners",
                           "four-masked-blended-owners",
                           "dynamic-atlas-active"],
        "limitations": [
            "The mask atlas and captures are synthetic color/image sources; this is not an end-to-end Solar owner or physical-capture workload.",
            "The reported renderTotalFrames delta proves the OBS render thread advanced while the committed scene was Program. OBS exposes no per-group composed-frame counter, so this does not isolate group shader frames from other scene-render work.",
            "OBS averageFrameRenderTime is a rolling average, not a per-frame distribution; its sampled p95/max summarize the polling values only.",
            "nvidia-smi load and VRAM are system-wide first-GPU readings, not attributable to Pulsar.",
            "GetStats output frame counters are expected to remain zero because the probe deliberately starts no output.",
            "Windows allocation counts are not observable here; process private bytes and working set are sampled instead.",
        ],
    }
    script_path = pathlib.Path(__file__).resolve()
    report["instrumentation"] = {
        "path": str(script_path), "sha256": sha256_file(script_path),
    }
    write_report(report_path, report)
    print(f"PROFILE_START runnerPid={os.getpid()} evidence={report_path} "
          f"runtime={runtime_dir}", flush=True)
    gp = load("composition_perf_group", "probe-group-composition.py")
    validate_plan_builder(gp)
    protocol = gp.load("composition_perf_wire", "probe-source-kinds.py")
    process_protocol = gp.load("composition_perf_process", "probe-websocket.py")
    try:
        result = asyncio.run(run_profile(args, report, report_path, stamp,
                                         gp, protocol, process_protocol,
                                         runtime_dir, atlas_a, atlas_b,
                                         fixture_meta))
        report["scenarios"] = result["scenarios"]
        report["passed"] = all(
            scenario.get("caseValidated", True) for scenario in result["scenarios"])
        report["finishedUtc"] = datetime.now(timezone.utc).isoformat()
    except Exception as error:
        report["errorType"] = type(error).__name__
        report["error"] = str(error)[:3000]
        report["finishedUtc"] = datetime.now(timezone.utc).isoformat()
        write_report(report_path, report)
        raise
    finally:
        write_report(report_path, report)
    print(f"PROFILE_DONE passed={report['passed']} report={report_path} "
          f"pidExited={report.get('ownedProcessExited')} "
          f"forcedKill={report.get('forcedTerminationUsed')}", flush=True)


if __name__ == "__main__":
    main()
