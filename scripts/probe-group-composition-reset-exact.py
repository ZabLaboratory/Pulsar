#!/usr/bin/env python3
"""Replay the qualified four-owner composition and trace its gen-2 reset."""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import importlib.util
import io
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

from PIL import Image
import websockets

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence" / "local-20261018-group-geometry" / "eleven"
CACHE = ROOT / ".cache" / "native-group-geometry"
EXPECTED_HELPER_SHA256 = "f83277e677f6ecb28b12f5efc5234ed21176cfb4eb519cc1ce99e1d51d991eff"
EXPECTED_UPSTREAM_HEAD = "6328ff8b3c29f10f29b7e1fad75f170efc41e198"
EXPECTED_BUILD_SOURCE_HEAD = "70a58302435308e609e0b1114efe3ef402f810b7"
EXPECTED_OBS_LIB_SHA256 = "b11eee9ea91c6bcff2d9fe62cfe6edba2fbcff57036c8cfb9d8f3f1b5a0729e4"
MUTEX_OWNER_PROBE = (ROOT.parent /
                     "eleven-local-20261017-native-reset-stability" /
                     "evidence" / "local-20261017-native-reset-stability" /
                     "eleven" / "20261017T163849Z-mutex-owner-stack-probe.py")
WINDOW_SECONDS = 20
VIEWPORT = (1920, 1080)


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load probe helper {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_native_thread_dump(process_id: int, output_path: pathlib.Path) -> dict:
    """Write a stack-focused dump without broad process-memory capture."""
    if os.name != "nt":
        raise OSError("native thread dumps are available only on Windows")
    import ctypes

    if output_path.exists():
        raise FileExistsError(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int,
                                     ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    dbghelp.MiniDumpWriteDump.argtypes = (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    dbghelp.MiniDumpWriteDump.restype = ctypes.c_int

    process_query_information = 0x0400
    process_vm_read = 0x0010
    process_dup_handle = 0x0040
    process_handle = kernel32.OpenProcess(
        process_query_information | process_vm_read | process_dup_handle,
        False, process_id)
    if not process_handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed for host dump")

    generic_write = 0x40000000
    create_new = 1
    file_attribute_normal = 0x80
    invalid_handle = ctypes.c_void_p(-1).value
    dump_handle = kernel32.CreateFileW(
        str(output_path), generic_write, 0, None, create_new,
        file_attribute_normal, None)
    if dump_handle in (None, invalid_handle):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(process_handle)
        raise OSError(error, "CreateFileW failed for host dump")

    # Normal minidump output contains thread contexts and stack memory. Add
    # process/thread environment data and thread info, while excluding handles,
    # unloaded modules, full-memory info, private RW memory, and indirect memory.
    dump_type = 0x00000100 | 0x00001000
    try:
        if not dbghelp.MiniDumpWriteDump(
                process_handle, process_id, dump_handle, dump_type,
                None, None, None):
            raise OSError(ctypes.get_last_error(), "MiniDumpWriteDump failed")
    finally:
        kernel32.CloseHandle(dump_handle)
        kernel32.CloseHandle(process_handle)

    return {
        "path": str(output_path.resolve()),
        "sha256": sha256(output_path),
        "sizeBytes": output_path.stat().st_size,
        "dumpTypeFlags": dump_type,
    }


class FreezeDumpCapture:
    """Capture one private host dump after a bounded render-counter stall."""

    def __init__(self, enabled: bool, process_id: int,
                 output_path: pathlib.Path, owner_output_path: pathlib.Path,
                 initial_frame_count=None,
                 stall_seconds: float = 2.0):
        self.enabled = enabled
        self.process_id = process_id
        self.output_path = output_path
        self.owner_output_path = owner_output_path
        self.last_frame_count = initial_frame_count
        self.last_progress_at = time.monotonic()
        self.stall_seconds = stall_seconds
        self.attempted = False
        self.task = None
        self.result = {
            "requested": enabled,
            "thresholdSeconds": stall_seconds,
            "attempted": False,
            "status": "not-requested" if not enabled else "waiting-for-frame-stall",
        }

    async def observe(self, stats: dict) -> None:
        if not self.enabled:
            return
        count = stats.get("renderTotalFrames")
        if not isinstance(count, int):
            return
        now = time.monotonic()
        if self.last_frame_count is None or count != self.last_frame_count:
            self.last_frame_count = count
            self.last_progress_at = now
            return
        if self.attempted or now - self.last_progress_at < self.stall_seconds:
            return
        self.attempted = True
        self.result.update({
            "attempted": True,
            "status": "capture-started",
            "trigger": "renderTotalFrames unchanged",
            "lastRenderTotalFrames": count,
            "stalledSeconds": round(now - self.last_progress_at, 3),
            "pid": self.process_id,
            "outputPath": str(self.output_path.resolve()),
            "captureStartedUtc": datetime.now(timezone.utc).isoformat(),
            "captureTimeoutSeconds": 8.0,
            "sizeBudgetBytes": 64 * 1024 * 1024,
            "dumpScope": (
                "native thread contexts and stack memory with thread metadata; "
                "no full-process/private/indirect memory, handles, or unloaded modules"),
        })
        self.task = asyncio.create_task(self._capture())

    async def _capture(self) -> None:
        started = time.monotonic()
        child = None
        try:
            child = await asyncio.create_subprocess_exec(
                sys.executable, str(pathlib.Path(__file__).resolve()),
                "--native-thread-dump-helper", str(self.process_id),
                str(self.output_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(
                    child.communicate(), timeout=8.0)
            except asyncio.TimeoutError:
                child.kill()
                try:
                    _stdout, _stderr = await asyncio.wait_for(
                        child.communicate(), timeout=2.0)
                except asyncio.TimeoutError:
                    self.result.update({
                        "captured": False,
                        "status": "helper-termination-timeout",
                        "helperPid": child.pid,
                    })
                    return
                self.result.update({
                    "captured": False,
                    "status": "capture-timeout",
                    "helperPid": child.pid,
                })
                return
            if child.returncode != 0:
                self.result.update({
                    "captured": False,
                    "status": "capture-error",
                    "helperPid": child.pid,
                    "errorType": "NativeDumpHelperError",
                    "error": stderr.decode("utf-8", errors="replace")[:1000],
                })
                return
            dump = json.loads(stdout.decode("utf-8"))
            within_budget = dump.get("sizeBytes", 0) <= 64 * 1024 * 1024
            self.result.update({
                "captured": True,
                "status": "captured-within-size-budget" if within_budget
                         else "captured-over-size-budget",
                "helperPid": child.pid,
                "withinSizeBudget": within_budget,
                **dump,
            })
            if not within_budget:
                self.result["liveMutexOwnerProbe"] = {
                    "status": "skipped-dump-over-64-mib-budget",
                    "outputPath": str(self.owner_output_path.resolve()),
                }
                return
            probe_started = time.monotonic()
            probe = await asyncio.create_subprocess_exec(
                sys.executable, str(MUTEX_OWNER_PROBE),
                "--minidump", str(self.output_path),
                "--discover-waiters", "--pid", str(self.process_id),
                "--output", str(self.owner_output_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                probe_stdout, probe_stderr = await asyncio.wait_for(
                    probe.communicate(), timeout=6.0)
            except asyncio.TimeoutError:
                probe.kill()
                try:
                    await asyncio.wait_for(probe.communicate(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                self.result["liveMutexOwnerProbe"] = {
                    "status": "probe-timeout",
                    "pid": self.process_id,
                    "helperPid": probe.pid,
                    "timeoutSeconds": 6.0,
                }
                return
            probe_result = {
                "status": "captured" if probe.returncode == 0 else "probe-error",
                "pid": self.process_id,
                "helperPid": probe.pid,
                "returnCode": probe.returncode,
                "outputPath": str(self.owner_output_path.resolve()),
                "probeDurationSeconds": round(time.monotonic() - probe_started, 3),
                "stdout": probe_stdout.decode("utf-8", errors="replace")[:4000],
                "stderr": probe_stderr.decode("utf-8", errors="replace")[:4000],
            }
            if self.owner_output_path.is_file():
                probe_result["outputSha256"] = sha256(self.owner_output_path)
            self.result["liveMutexOwnerProbe"] = probe_result
        except Exception as error:
            self.result.update({
                "captured": False,
                "status": "capture-error",
                "errorType": type(error).__name__,
                "error": str(error)[:1000],
            })
        finally:
            self.result.setdefault("captureCompletedUtc",
                                   datetime.now(timezone.utc).isoformat())
            self.result["captureDurationSeconds"] = round(
                time.monotonic() - started, 3)
            if child is not None and child.returncode is None:
                self.result["helperPidStillRunning"] = child.pid

    async def wait_for_result(self) -> None:
        if self.task is not None:
            await self.task


async def connect(protocol, url: str, password: str):
    ws = await websockets.connect(url, subprotocols=["obswebsocket.json"],
                                  open_timeout=10, max_size=2**24)
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))["d"]
    identify = {"rpcVersion": hello["rpcVersion"], "eventSubscriptions": 0}
    auth = hello.get("authentication")
    if auth:
        identify["authentication"] = protocol.compute_auth(
            password, auth["salt"], auth["challenge"])
    await ws.send(json.dumps({"op": 1, "d": identify}))
    identified = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    if identified.get("op") != 2:
        raise RuntimeError(f"obs-websocket Identify failed: {identified}")
    return ws, protocol.Inbox()


async def request(protocol, inbox, ws, request_type: str, data=None,
                  timeout: float = 10.0):
    response = await protocol.request(inbox, ws, request_type, uuid.uuid4().hex,
                                      data, timeout=timeout)
    status = response.get("requestStatus", {})
    if not status.get("result"):
        raise RuntimeError(f"{request_type} failed: {status}")
    return response.get("responseData", {})


async def screenshot(protocol, inbox, ws, source_name: str,
                     label: str, stamp: str,
                     image_width: int = 128, image_height: int = 72):
    result = await request(protocol, inbox, ws, "GetSourceScreenshot", {
        "sourceName": source_name, "imageFormat": "png",
        "imageWidth": image_width, "imageHeight": image_height,
    }, timeout=12)
    encoded = result["imageData"].split(",", 1)[1]
    data = base64.b64decode(encoded)
    path = EVIDENCE / f"{stamp}-{label}.png"
    path.write_bytes(data)
    image = Image.open(io.BytesIO(data)).convert("RGBA")
    return {
        "path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
        "dimensions": list(image.size),
        "samplePixels": {
            "left": list(image.getpixel((24, 36))),
            "right": list(image.getpixel((96, 36))),
        },
    }


def assert_translation_only_plan_delta(
        before: dict, after: dict,
        expected_delta: tuple[float, float] | None = (37, 23)) -> dict:
    if (before.get("protocol") != after.get("protocol") or
            before.get("sceneName") != after.get("sceneName") or
            before.get("runtimeInstanceId") != after.get("runtimeInstanceId") or
            before.get("replacedSceneItems") != after.get("replacedSceneItems") or
            before.get("coordinateSpace") != after.get("coordinateSpace")):
        raise RuntimeError("translation-only plans changed a fenced outer identity")
    previous_groups = copy.deepcopy(before["groups"])
    next_groups = copy.deepcopy(after["groups"])
    changed = []
    for group_before, group_after in zip(previous_groups, next_groups, strict=True):
        for child_before, child_after in zip(
                group_before.get("children", []),
                group_after.get("children", []), strict=True):
            if child_before.get("kind") == "capture":
                affine_before = child_before["affine"]
                affine_after = child_after["affine"]
                dx = affine_after["tx"] - affine_before["tx"]
                dy = affine_after["ty"] - affine_before["ty"]
                if dx or dy:
                    changed.append({
                        "sceneItemId": child_before["sceneItemId"],
                        "dx": dx, "dy": dy,
                    })
                affine_before["tx"] = affine_after["tx"] = 0
                affine_before["ty"] = affine_after["ty"] = 0
    if previous_groups != next_groups:
        raise RuntimeError("translation-only plans changed topology or nontranslation semantics")
    if len(changed) != 1 or (expected_delta is not None and
                             (changed[0]["dx"], changed[0]["dy"]) != expected_delta):
        raise RuntimeError(f"translation fixture did not move exactly one capture: {changed}")
    return changed[0]


async def capture_scene_model(protocol, inbox, ws, scene_name: str,
                              is_group: bool = False,
                              visited: set[str] | None = None) -> list[dict]:
    visited = visited if visited is not None else set()
    if scene_name in visited:
        raise RuntimeError(f"scene model contains a cycle at {scene_name}")
    visited.add(scene_name)
    request_type = "GetGroupSceneItemList" if is_group else "GetSceneItemList"
    response = await request(protocol, inbox, ws, request_type,
                             {"sceneName": scene_name})
    model = []
    for item in response.get("sceneItems", []):
        entry = copy.deepcopy(item)
        if item.get("sourceType") == "OBS_SOURCE_TYPE_SCENE":
            entry["children"] = await capture_scene_model(
                protocol, inbox, ws, item["sourceName"],
                bool(item.get("isGroup")), visited)
        model.append(entry)
    visited.remove(scene_name)
    return model


def image_pixel_identity(path: pathlib.Path) -> dict:
    image = Image.open(path).convert("RGBA")
    pixels = image.tobytes()
    return {
        "dimensions": list(image.size),
        "pixelSha256": hashlib.sha256(pixels).hexdigest(),
    }


def scene_model_hash(model: list[dict]) -> str:
    encoded = json.dumps(model, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def scene_model_items(items: list[dict]):
    for item in items:
        yield item
        yield from scene_model_items(item.get("children", []))


def assert_fast_geometry_commit(timing: dict, skipped_frames: int,
                                generation: int,
                                idle_control: dict | None = None,
                                observed_window_ms: float | None = None) -> None:
    if not isinstance(timing, dict):
        raise RuntimeError(f"generation-{generation} debug timing missing")
    if timing.get("geometryFastPath") is not True:
        raise RuntimeError(f"generation-{generation} did not use retained geometry path: {timing}")
    if timing.get("buildGroupMs") != 0 or timing.get("filterSourcesCreated") != 0 or \
            timing.get("shaderCreateCalls") != 0:
        raise RuntimeError(f"generation-{generation} rebuilt filters on geometry path: {timing}")
    if timing.get("geometryUpdateMs", float("inf")) >= 1000 / 60:
        raise RuntimeError(
            f"generation-{generation} lock-held geometry validation/update exceeded one frame: {timing}")
    if idle_control is None or observed_window_ms is None:
        if skipped_frames != 0:
            raise RuntimeError(
                f"generation-{generation} translation fast path skipped "
                f"{skipped_frames} frames without an idle control sample; timing={timing}")
        return
    idle_duration_ms = max(float(idle_control.get("durationMs", 0)), 1.0)
    idle_skipped = int(idle_control.get("skippedFrames", 0))
    expected_idle_skips = math.ceil(
        idle_skipped * max(observed_window_ms, 0) / idle_duration_ms)
    allowed_skips = expected_idle_skips + 1
    if skipped_frames > allowed_skips:
        raise RuntimeError(
            f"generation-{generation} fast path skipped {skipped_frames} frames "
            f"in {observed_window_ms:.2f} ms, above idle control allowance "
            f"{allowed_skips} from {idle_control}; timing={timing}")


async def measure_render_skip_control(protocol, inbox, ws, duration_seconds=0.5):
    before = await request(protocol, inbox, ws, "GetStats", timeout=3)
    started = time.perf_counter()
    await asyncio.sleep(duration_seconds)
    after = await request(protocol, inbox, ws, "GetStats", timeout=3)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    skipped = int(after["renderSkippedFrames"] - before["renderSkippedFrames"])
    rendered = int(after["renderTotalFrames"] - before["renderTotalFrames"])
    if skipped < 0 or rendered < 0:
        raise RuntimeError("GetStats counters regressed during idle control")
    return {
        "durationMs": round(elapsed_ms, 3),
        "skippedFrames": skipped,
        "renderedFrames": rendered,
        "beforeStats": before,
        "afterStats": after,
    }


def reference_workload(reference_report: dict) -> tuple[dict, list[dict], pathlib.Path, str]:
    entry = next((scenario for scenario in reference_report.get("scenarios", [])
                  if scenario.get("label") == "four-masked-blended-owners"), None)
    if entry is None:
        raise RuntimeError("reference report lacks four-masked-blended-owners")
    plan = entry.get("wirePlan")
    if not isinstance(plan, dict) or plan.get("protocol") != "pulsar.group-composition/2":
        raise RuntimeError("reference four-owner plan is not composition protocol /2")
    groups = plan.get("groups")
    if not isinstance(groups, list) or len(groups) != 5:
        raise RuntimeError("reference plan must contain one root and four owner groups")
    root, owners = groups[0], groups[1:]
    bounds = root.get("bounds", {})
    viewport = (int(bounds.get("width", 0)), int(bounds.get("height", 0)))
    if viewport != VIEWPORT:
        raise RuntimeError(f"reference root bounds differ from {VIEWPORT}: {viewport}")
    expected_owner_blends = ["multiply", "screen", "overlay", "difference"]
    expected_capture_blends = ["hard-light", "multiply", "soft-light", "screen",
                               "darken", "overlay", "lighten", "difference"]
    if (root.get("blendMode") != "normal" or root.get("opacity") != 1.0 or
            root.get("visible") is not True or root.get("mask") is not None):
        raise RuntimeError("reference root must be visible, normal, opacity 1, and unmasked")
    if [group.get("blendMode") for group in owners] != expected_owner_blends:
        raise RuntimeError("reference owner blend inventory differs from the qualified run")
    captures = [child for group in owners for child in group.get("children", [])
                if child.get("kind") == "capture"]
    if (len(captures) != 8 or
            [capture.get("blendMode") for capture in captures] != expected_capture_blends):
        raise RuntimeError("reference capture blend inventory differs from the qualified run")
    if any(group.get("mask") is None for group in owners):
        raise RuntimeError("reference workload omits an owner mask")
    if any(capture.get("mask") is None for capture in captures):
        raise RuntimeError("reference workload omits a capture mask")
    fixtures = reference_report.get("generatedMaskFixtures", [])
    if not fixtures:
        raise RuntimeError("reference report has no generated atlas fixture metadata")
    atlas = pathlib.Path(fixtures[0]["path"]).resolve()
    expected_atlas_hash = fixtures[0].get("sha256")
    if not atlas.is_file() or sha256(atlas) != expected_atlas_hash:
        raise RuntimeError("reference alpha-atlas fixture is missing or changed")
    return plan, copy.deepcopy(groups), atlas, expected_atlas_hash


async def run_scenario(args, process, runtime_id: str, port: int,
                       password: str, report: dict, stamp: str,
                       atlas_path: pathlib.Path):
    comp = load("reset_exact_composition", "probe-group-composition.py")
    protocol = load("reset_exact_protocol", "probe-source-kinds.py")
    reference_report = json.loads(args.reference_report.read_text(encoding="utf-8"))
    reference_plan, groups_template, _source_atlas, source_atlas_hash = reference_workload(
        reference_report)
    url = f"ws://127.0.0.1:{port}"
    primary, primary_inbox = await connect(protocol, url, password)
    stats_ws, stats_inbox = await connect(protocol, url, password)
    scenario_report: dict = {}
    try:
        video = await request(protocol, primary_inbox, primary, "GetVideoSettings")
        viewport = (int(video.get("baseWidth", 0)), int(video.get("baseHeight", 0)))
        fps = float(video.get("fpsNumerator", 0)) / max(
            float(video.get("fpsDenominator", 0)), 1.0)
        if viewport != VIEWPORT or abs(fps - 60.0) > 0.01:
            raise RuntimeError(f"private host must be 1920x1080@60: {video}")
        record = await request(protocol, primary_inbox, primary, "GetRecordStatus")
        stream = await request(protocol, primary_inbox, primary, "GetStreamStatus")
        if record.get("outputActive") or stream.get("outputActive"):
            raise RuntimeError("private host unexpectedly has recording or streaming active")

        token = uuid.uuid4().hex[:10]
        scene = f"native-reset-four-owner-{token}"
        backdrop_name = scene + "-backdrop"
        atlas_name = scene + "-atlas"
        capture_names = [f"{scene}-capture-{index}" for index in range(8)]
        capture_colors = [0xFFFF3030, 0xFF30FF70, 0xFF4080FF, 0xFFFFFF30,
                          0xFFFF30D0, 0xFF30E0FF, 0xFFFF9030, 0xFFB060FF]

        await request(protocol, primary_inbox, primary, "CreateScene",
                      {"sceneName": scene})
        await request(protocol, primary_inbox, primary, "CreateInput", {
            "sceneName": scene, "inputName": backdrop_name,
            "inputKind": "color_source_v3",
            "inputSettings": {"color": 0xFF202030,
                              "width": VIEWPORT[0], "height": VIEWPORT[1]},
            "sceneItemEnabled": True,
        })
        capture_ids = []
        for index, input_name in enumerate(capture_names):
            result = await request(protocol, primary_inbox, primary, "CreateInput", {
                "sceneName": scene, "inputName": input_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": capture_colors[index],
                                  "width": VIEWPORT[0], "height": VIEWPORT[1]},
                "sceneItemEnabled": False,
            })
            capture_ids.append(int(result["sceneItemId"]))
        atlas = await request(protocol, primary_inbox, primary, "CreateInput", {
            "sceneName": scene, "inputName": atlas_name,
            "inputKind": "image_source",
            "inputSettings": {"file": str(atlas_path)},
            "sceneItemEnabled": False,
        })
        atlas_id = int(atlas["sceneItemId"])
        backdrop_id = int((await request(protocol, primary_inbox, primary,
                                         "GetSceneItemId", {
            "sceneName": scene, "sourceName": backdrop_name,
        }))["sceneItemId"])
        replaced_ids = [*capture_ids, atlas_id]

        await request(protocol, primary_inbox, primary, "SetCurrentProgramScene",
                      {"sceneName": scene})
        await asyncio.sleep(5)
        program_before = await request(protocol, primary_inbox, primary,
                                       "GetCurrentProgramScene")
        if program_before.get("sceneName") != scene:
            raise RuntimeError(f"composition scene is not Program: {program_before}")
        nominal = await screenshot(protocol, primary_inbox, primary, scene,
                                   "nominal-before-composition", stamp)

        def make_plan(generation: int) -> dict:
            groups = copy.deepcopy(groups_template)
            capture_index = 0
            for group in groups:
                group_mask = group.get("mask")
                if isinstance(group_mask, dict):
                    group_mask["sceneItemId"] = atlas_id
                for child in group.get("children", []):
                    if child.get("kind") != "capture":
                        continue
                    child["sceneItemId"] = capture_ids[capture_index]
                    capture_index += 1
                    mask = child.get("mask")
                    if isinstance(mask, dict):
                        mask["sceneItemId"] = atlas_id
            if capture_index != 8:
                raise RuntimeError(f"only remapped {capture_index} captures")
            return comp.make_global_v2_plan(
                scene, runtime_id, generation, groups, viewport, replaced_ids)

        generation_one = make_plan(1)
        commit_one = await request(protocol, primary_inbox, primary, "CallVendorRequest", {
            "vendorName": "pulsar-capture-effects",
            "requestType": "CommitGroupComposition",
            "requestData": generation_one,
        })
        commit_one_result = commit_one.get("responseData", {})
        if not commit_one_result.get("ok"):
            raise RuntimeError(f"generation-1 composition commit failed: {commit_one}")
        if (commit_one_result.get("generation") != generation_one["generation"] or
                commit_one_result.get("planHash") != generation_one["planHash"]):
            raise RuntimeError("generation-1 response changed the commit contract")
        commit_one_timing = commit_one_result.get("debugCommitTiming")
        if not isinstance(commit_one_timing, dict):
            raise RuntimeError("generation-1 full-build timing missing")
        await asyncio.sleep(2)
        window_baseline = await request(protocol, primary_inbox, primary, "GetStats")
        start = time.monotonic()
        deadline = start + args.window_seconds
        next_sample = start
        samples = []
        while time.monotonic() < deadline:
            next_sample += 1.0
            await asyncio.sleep(max(0.0, next_sample - time.monotonic()))
            sample_started = time.monotonic()
            try:
                stats = await request(protocol, primary_inbox, primary, "GetStats", timeout=3)
                samples.append({
                    "secondsFromWindowStart": round(sample_started - start, 3),
                    "latencyMs": round((time.monotonic() - sample_started) * 1000, 3),
                    "ok": True,
                    "stats": {key: stats.get(key) for key in (
                        "activeFps", "averageFrameRenderTime", "renderTotalFrames",
                        "renderSkippedFrames", "outputTotalFrames", "outputSkippedFrames")},
                })
            except Exception as error:
                samples.append({
                    "secondsFromWindowStart": round(sample_started - start, 3),
                    "ok": False, "error": f"{type(error).__name__}: {error}",
                })
        duration = time.monotonic() - start
        program_after = await request(protocol, primary_inbox, primary,
                                      "GetCurrentProgramScene")
        if program_after.get("sceneName") != scene:
            raise RuntimeError(f"Program scene left during the 20-second workload: {program_after}")

        screenshot_task = asyncio.create_task(screenshot(
            protocol, primary_inbox, primary, scene, "after-20s-window", stamp))
        screenshot_started = time.monotonic()
        during_screenshot = []
        screenshot_errors = []
        screenshot_deadline = screenshot_started + 12.0
        while not screenshot_task.done() and time.monotonic() < screenshot_deadline:
            stat_started = time.monotonic()
            try:
                stats = await request(protocol, stats_inbox, stats_ws, "GetStats", timeout=2)
                during_screenshot.append({
                    "secondsFromScreenshotStart": round(stat_started - screenshot_started, 3),
                    "latencyMs": round((time.monotonic() - stat_started) * 1000, 3),
                    "stats": {key: stats.get(key) for key in (
                        "activeFps", "averageFrameRenderTime", "renderTotalFrames",
                        "renderSkippedFrames")},
                })
            except Exception as error:
                screenshot_errors.append(f"{type(error).__name__}: {error}")
            if not screenshot_task.done():
                await asyncio.sleep(0.3)
        try:
            after_window_screenshot = await asyncio.wait_for(
                asyncio.shield(screenshot_task), timeout=0.2)
            screenshot_status = "success"
            screenshot_error = None
        except Exception as error:
            screenshot_status = "timeout" if isinstance(error, asyncio.TimeoutError) else "error"
            screenshot_error = f"{type(error).__name__}: {error}"
            screenshot_task.cancel()
            try:
                await screenshot_task
            except BaseException:
                pass
        if screenshot_status != "success":
            after_window_screenshot = None
        model_before_translation = await capture_scene_model(
            protocol, primary_inbox, primary, scene)
        recommit_idle_control = await measure_render_skip_control(
            protocol, stats_inbox, stats_ws)
        pre_recommit_stats = recommit_idle_control["afterStats"]

        generation_two = make_plan(2)
        translation_delta = None
        if args.translation_only:
            translated_groups = copy.deepcopy(generation_two["groups"])
            moved = False
            for group in translated_groups:
                for child in group.get("children", []):
                    if child.get("kind") == "capture":
                        child["affine"]["tx"] += 37
                        child["affine"]["ty"] += 23
                        moved = True
                        break
                if moved:
                    break
            if not moved:
                raise RuntimeError("translation fixture found no capture child")
            generation_two = comp.make_global_v2_plan(
                scene, runtime_id, 2, translated_groups, viewport, replaced_ids)
            translation_delta = assert_translation_only_plan_delta(
                generation_one, generation_two)
        elif generation_one.get("planHash") != generation_two.get("planHash"):
            raise RuntimeError("generation-2 recommit changed the canonical graph hash")
        commit_two_started = time.perf_counter()
        commit_two = await request(protocol, primary_inbox, primary, "CallVendorRequest", {
            "vendorName": "pulsar-capture-effects",
            "requestType": "CommitGroupComposition",
            "requestData": generation_two,
        })
        commit_two_round_trip_ms = round(
            (time.perf_counter() - commit_two_started) * 1000.0, 3)
        commit_two_result = commit_two.get("responseData", {})
        if not commit_two_result.get("ok"):
            raise RuntimeError(f"generation-2 replacement commit failed: {commit_two}")
        if (commit_two_result.get("generation") != generation_two["generation"] or
                commit_two_result.get("planHash") != generation_two["planHash"]):
            raise RuntimeError(
                "generation-2 response did not preserve the generation/planHash contract")
        commit_timing = commit_two_result.get("debugCommitTiming")
        timing_fields = (
            "queueWaitMs", "graphicsCallbackMs", "parseMs",
            "resolveCapturesMs", "buildGroupMs", "atomicSwapMs",
            "retirePreviousMs", "handlerTotalMs", "shaderCreateMs",
            "inputBindingSnapshotMs", "geometrySnapshotMs",
            "geometryUpdateMs", "geometryFastPath")
        if not isinstance(commit_timing, dict) or any(
                not isinstance(commit_timing.get(key), (int, float)) or
                not math.isfinite(commit_timing[key]) or
                commit_timing[key] < 0 for key in timing_fields):
            raise RuntimeError("generation-2 debug commit timing missing or invalid")
        if args.translation_only:
            if (commit_timing.get("geometryFastPath") is not True or
                    commit_timing.get("buildGroupMs") != 0 or
                    commit_timing.get("filterSourcesCreated") != 0 or
                    commit_timing.get("shaderCreateCalls") != 0):
                raise RuntimeError(
                    f"generation-2 did not use the retained geometry path: {commit_timing}")
        else:
            if commit_timing.get("filterSourcesCreated") != 12:
                raise RuntimeError(
                    "generation-2 filter creation count differs from the 12-filter fixture")
            if commit_timing.get("shaderCreateCalls") != 12:
                raise RuntimeError(
                    "generation-2 shader creation count differs from the 12-filter fixture")
            if commit_timing["shaderCreateMs"] > commit_timing["buildGroupMs"]:
                raise RuntimeError("shader creation time exceeds group build time")
        # Sample immediately after the synchronous commit, before either the
        # settling delay or PNG readback can contribute to the skip delta.
        post_recommit_stats = await request(protocol, stats_inbox, stats_ws,
                                            "GetStats", timeout=3)
        recommit_stats_window_ms = round(
            (time.perf_counter() - commit_two_started) * 1000.0, 3)
        recommit_skipped_frames_delta = int(
            post_recommit_stats["renderSkippedFrames"] -
            pre_recommit_stats["renderSkippedFrames"])
        recommit_rendered_frames_delta = int(
            post_recommit_stats["renderTotalFrames"] -
            pre_recommit_stats["renderTotalFrames"])
        if recommit_skipped_frames_delta < 0 or recommit_rendered_frames_delta < 0:
            raise RuntimeError("GetStats counters regressed across recommit")
        if args.translation_only:
            assert_fast_geometry_commit(
                commit_timing, recommit_skipped_frames_delta, 2,
                recommit_idle_control, recommit_stats_window_ms)
        geometry_commits = [{
            "generation": 2,
            "planHash": generation_two["planHash"],
            "timing": commit_timing,
            "skippedFrames": recommit_skipped_frames_delta,
            "renderedFrames": recommit_rendered_frames_delta,
            "idleControl": recommit_idle_control,
            "statsWindowMs": recommit_stats_window_ms,
        }]
        final_translation_plan = generation_two
        repeated_deltas = ([(11, 7), (-11, -7)] *
                           (args.translation_repeats // 2)
                           if args.translation_only else [])
        for delta_index, (dx, dy) in enumerate(repeated_deltas, start=1):
            generation = final_translation_plan["generation"] + 1
            moved_groups = copy.deepcopy(final_translation_plan["groups"])
            moved = False
            for group in moved_groups:
                for child in group.get("children", []):
                    if child.get("kind") == "capture":
                        child["affine"]["tx"] += dx
                        child["affine"]["ty"] += dy
                        moved = True
                        break
                if moved:
                    break
            if not moved:
                raise RuntimeError("repeated translation fixture found no capture child")
            translated_plan = comp.make_global_v2_plan(
                scene, runtime_id, generation, moved_groups, viewport, replaced_ids)
            delta = assert_translation_only_plan_delta(
                final_translation_plan, translated_plan, (dx, dy))
            idle_control = await measure_render_skip_control(
                protocol, stats_inbox, stats_ws)
            before_stats = idle_control["afterStats"]
            started = time.perf_counter()
            repeated_commit = await request(protocol, primary_inbox, primary,
                                            "CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition",
                "requestData": translated_plan,
            })
            round_trip_ms = round((time.perf_counter() - started) * 1000.0, 3)
            repeated_result = repeated_commit.get("responseData", {})
            if (not repeated_result.get("ok") or
                    repeated_result.get("generation") != generation or
                    repeated_result.get("planHash") != translated_plan["planHash"]):
                raise RuntimeError(
                    f"generation-{generation} response contract changed: {repeated_commit}")
            after_stats = await request(protocol, stats_inbox, stats_ws,
                                        "GetStats", timeout=3)
            stats_window_ms = round((time.perf_counter() - started) * 1000.0, 3)
            skipped = int(after_stats["renderSkippedFrames"] -
                          before_stats["renderSkippedFrames"])
            rendered = int(after_stats["renderTotalFrames"] -
                           before_stats["renderTotalFrames"])
            if skipped < 0 or rendered < 0:
                raise RuntimeError(f"GetStats counters regressed at generation-{generation}")
            timing = repeated_result.get("debugCommitTiming")
            assert_fast_geometry_commit(
                timing, skipped, generation, idle_control, stats_window_ms)
            geometry_commits.append({
                "generation": generation, "translationDelta": delta,
                "roundTripMs": round_trip_ms, "timing": timing,
                "skippedFrames": skipped, "renderedFrames": rendered,
                "idleControl": idle_control, "statsWindowMs": stats_window_ms,
            })
            final_translation_plan = translated_plan
        freeze_dump = FreezeDumpCapture(
            args.capture_freeze_dump, process.proc.proc.pid,
            CACHE / f"{stamp}-gen2-freeze.dmp",
            EVIDENCE / f"{stamp}-mutex-owner-live.json",
            initial_frame_count=pre_recommit_stats.get("renderTotalFrames"))
        await asyncio.sleep(0.5)
        after_recommit_settle_before_png_stats = await request(
            protocol, stats_inbox, stats_ws, "GetStats", timeout=3)
        translation_final_screenshot = None
        translation_pixel_baseline = None
        retained_model_after_translation = None
        source_setting_fast_commit = None
        internal_filter_fallback_commit = None
        filter_semantics_change = None
        final_active_plan = final_translation_plan
        if args.translation_only:
            translation_final_screenshot = await screenshot(
                protocol, primary_inbox, primary, scene,
                "translation-final-1920x1080", stamp, 1920, 1080)
            baseline_identity = image_pixel_identity(args.translation_baseline_image)
            final_identity = image_pixel_identity(
                pathlib.Path(translation_final_screenshot["path"]))
            translation_pixel_baseline = {
                "path": str(args.translation_baseline_image),
                "baseline": baseline_identity,
                "final": final_identity,
                "exactPixelMatch": baseline_identity == final_identity,
            }
            if not translation_pixel_baseline["exactPixelMatch"]:
                raise RuntimeError(
                    f"repeated fast-path drag pixels differ from full-rebuild baseline: {translation_pixel_baseline}")
            retained_model_after_translation = await capture_scene_model(
                protocol, primary_inbox, primary, scene)
            model_before_hash = scene_model_hash(model_before_translation)
            model_after_hash = scene_model_hash(retained_model_after_translation)
            if model_before_hash != model_after_hash:
                raise RuntimeError(
                    "retained OBS scene model changed across translation-only updates")

            # The capture source object is deliberately retained by the nested
            # scenes. A same-object color setting update should propagate through
            # that tree and remain eligible for the geometry path.
            changed_color = 0xFF20E0F0
            input_settings_response = await request(
                protocol, primary_inbox, primary, "SetInputSettings", {
                    "inputName": capture_names[0],
                    "inputSettings": {"color": changed_color},
                    "overlay": True,
                })
            settings_changed_screenshot = None
            for attempt in range(20):
                await asyncio.sleep(0.1)
                settings_changed_screenshot = await screenshot(
                    protocol, primary_inbox, primary, scene,
                    f"capture-setting-updated-{attempt}", stamp, 1920, 1080)
                if image_pixel_identity(
                        pathlib.Path(settings_changed_screenshot["path"])) != baseline_identity:
                    break
            else:
                raise RuntimeError(
                    "same-object capture color setting did not propagate into the retained composition")

            settings_generation = final_translation_plan["generation"] + 1
            settings_groups = copy.deepcopy(final_translation_plan["groups"])
            moved = False
            for group in settings_groups:
                for child in group.get("children", []):
                    if child.get("kind") == "capture":
                        child["affine"]["tx"] += 7
                        child["affine"]["ty"] -= 3
                        moved = True
                        break
                if moved:
                    break
            if not moved:
                raise RuntimeError("source-setting test found no capture to translate")
            settings_plan = comp.make_global_v2_plan(
                scene, runtime_id, settings_generation, settings_groups,
                viewport, replaced_ids)
            settings_delta = assert_translation_only_plan_delta(
                final_translation_plan, settings_plan, (7, -3))
            settings_idle_control = await measure_render_skip_control(
                protocol, stats_inbox, stats_ws)
            settings_before_stats = settings_idle_control["afterStats"]
            settings_commit_started = time.perf_counter()
            settings_commit = await request(protocol, primary_inbox, primary,
                                            "CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition",
                "requestData": settings_plan,
            })
            settings_result = settings_commit.get("responseData", {})
            if (not settings_result.get("ok") or
                    settings_result.get("generation") != settings_generation or
                    settings_result.get("planHash") != settings_plan["planHash"]):
                raise RuntimeError(
                    f"same-object settings translation response contract changed: {settings_commit}")
            settings_after_stats = await request(
                protocol, stats_inbox, stats_ws, "GetStats", timeout=3)
            settings_stats_window_ms = round(
                (time.perf_counter() - settings_commit_started) * 1000.0, 3)
            settings_skipped = int(settings_after_stats["renderSkippedFrames"] -
                                   settings_before_stats["renderSkippedFrames"])
            settings_timing = settings_result.get("debugCommitTiming")
            assert_fast_geometry_commit(
                settings_timing, settings_skipped, settings_generation,
                settings_idle_control, settings_stats_window_ms)
            await asyncio.sleep(0.2)
            settings_fast_screenshot = await screenshot(
                protocol, primary_inbox, primary, scene,
                "capture-setting-after-fast-translation", stamp, 1920, 1080)
            if image_pixel_identity(pathlib.Path(settings_fast_screenshot["path"])) == baseline_identity:
                raise RuntimeError(
                    "capture source settings stopped propagating after retained translation")
            settings_model = await capture_scene_model(
                protocol, primary_inbox, primary, scene)
            if scene_model_hash(settings_model) != model_before_hash:
                raise RuntimeError(
                    "host/group scene model changed after same-object source settings update")
            source_setting_fast_commit = {
                "inputName": capture_names[0],
                "newColor": changed_color,
                "setInputSettingsResponse": input_settings_response,
                "pixelsChangedBeforeNextTranslation": True,
                "translationDelta": settings_delta,
                "generation": settings_generation,
                "commitTiming": settings_timing,
                "skippedFrames": settings_skipped,
                "statsWindowMs": settings_stats_window_ms,
                "idleControl": settings_idle_control,
                "sourceScreenshot": settings_fast_screenshot,
            }
            final_active_plan = settings_plan

            # Mutate a structural filter owned by a retained composition group.
            # Exact snapshot mismatch must decline reuse and atomically rebuild.
            internal_filter = None
            for item in scene_model_items(model_before_translation):
                if item.get("sourceType") != "OBS_SOURCE_TYPE_SCENE":
                    continue
                filters_response = await request(
                    protocol, primary_inbox, primary, "GetSourceFilterList",
                    {"sourceName": item["sourceName"]})
                for filter_item in filters_response.get("filters", []):
                    settings = filter_item.get("filterSettings") or {}
                    opacity = settings.get("opacity")
                    if (filter_item.get("filterKind") == "pulsar_capture_effects" and
                            isinstance(opacity, (int, float))):
                        internal_filter = {
                            "sourceName": item["sourceName"],
                            "filterName": filter_item["filterName"],
                            "opacityBefore": opacity,
                            "opacityAfter": max(0.1, float(opacity) * 0.8),
                        }
                        break
                if internal_filter:
                    break
            if internal_filter is None:
                raise RuntimeError(
                    "no retained composition filter with opacity settings was exposed for the fallback test")
            await request(protocol, primary_inbox, primary,
                          "SetSourceFilterSettings", {
                "sourceName": internal_filter["sourceName"],
                "filterName": internal_filter["filterName"],
                "filterSettings": {"opacity": internal_filter["opacityAfter"]},
                "overlay": True,
            })
            filter_generation = final_active_plan["generation"] + 1
            filter_groups = copy.deepcopy(final_active_plan["groups"])
            moved = False
            for group in filter_groups:
                for child in group.get("children", []):
                    if child.get("kind") == "capture":
                        child["affine"]["tx"] += 4
                        child["affine"]["ty"] -= 6
                        moved = True
                        break
                if moved:
                    break
            if not moved:
                raise RuntimeError("internal-filter test found no capture to translate")
            filter_plan = comp.make_global_v2_plan(
                scene, runtime_id, filter_generation, filter_groups,
                viewport, replaced_ids)
            assert_translation_only_plan_delta(
                final_active_plan, filter_plan, (4, -6))
            filter_commit = await request(protocol, primary_inbox, primary,
                                          "CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition",
                "requestData": filter_plan,
            })
            filter_result = filter_commit.get("responseData", {})
            if (not filter_result.get("ok") or
                    filter_result.get("generation") != filter_generation or
                    filter_result.get("planHash") != filter_plan["planHash"]):
                raise RuntimeError(
                    f"internal filter fallback response contract changed: {filter_commit}")
            filter_timing = filter_result.get("debugCommitTiming")
            if (not isinstance(filter_timing, dict) or
                    filter_timing.get("geometryFastPath") is not False or
                    filter_timing.get("buildGroupMs", 0) <= 0 or
                    filter_timing.get("filterSourcesCreated") != 12):
                raise RuntimeError(
                    f"internal filter change did not fall back to safe full rebuild: {filter_timing}")
            filter_semantics_change = internal_filter
            internal_filter_fallback_commit = {
                "generation": filter_generation,
                "commitTiming": filter_timing,
                "response": filter_result,
            }
            final_active_plan = filter_plan
        pre_reset_screenshot = None
        pre_reset_screenshot_error = None
        pre_reset_screenshot_stats = []
        pre_reset_screenshot_stats_errors = []
        pre_reset_screenshot_task = asyncio.create_task(screenshot(
            protocol, primary_inbox, primary, scene,
            f"pre-reset-generation-{final_active_plan['generation']}", stamp))
        pre_reset_screenshot_started = time.monotonic()
        try:
            while not pre_reset_screenshot_task.done():
                elapsed = time.monotonic() - pre_reset_screenshot_started
                remaining = 12.0 - elapsed
                if remaining <= 0:
                    raise asyncio.TimeoutError(
                        "pre-reset screenshot exceeded 12 seconds")
                try:
                    pre_reset_screenshot = await asyncio.wait_for(
                        asyncio.shield(pre_reset_screenshot_task),
                        timeout=min(1.0, remaining))
                    break
                except asyncio.TimeoutError:
                    stats_started = time.monotonic()
                    try:
                        stats = await request(protocol, stats_inbox, stats_ws,
                                              "GetStats", timeout=2)
                        pre_reset_screenshot_stats.append({
                            "secondsFromScreenshotStart": round(
                                stats_started - pre_reset_screenshot_started, 3),
                            "latencyMs": round(
                                (time.monotonic() - stats_started) * 1000, 3),
                            "stats": {key: stats.get(key) for key in (
                                "activeFps", "averageFrameRenderTime",
                                "renderTotalFrames", "renderSkippedFrames")},
                        })
                        await freeze_dump.observe(stats)
                    except Exception as error:
                        pre_reset_screenshot_stats_errors.append(
                            f"{type(error).__name__}: {error}")
            if pre_reset_screenshot is None:
                pre_reset_screenshot = await pre_reset_screenshot_task
        except Exception as error:
            pre_reset_screenshot_error = f"{type(error).__name__}: {error}"
            if not pre_reset_screenshot_task.done():
                pre_reset_screenshot_task.cancel()
                try:
                    await pre_reset_screenshot_task
                except BaseException:
                    pass
        scenario_report["preResetScreenshotError"] = pre_reset_screenshot_error
        scenario_report["preResetScreenshotStats"] = pre_reset_screenshot_stats
        scenario_report["preResetScreenshotStatsErrors"] = (
            pre_reset_screenshot_stats_errors)
        pre_reset_stats = await request(protocol, stats_inbox, stats_ws,
                                        "GetStats", timeout=3)
        reset_id = uuid.uuid4().hex
        reset_request = {
            "vendorName": "pulsar-capture-effects",
            "requestType": "ResetGroupComposition",
            "requestData": {"sceneName": scene,
                            "generation": final_active_plan["generation"],
                            "runtimeInstanceId": runtime_id},
        }
        scenario_report.update({
            "sceneName": scene, "viewport": list(viewport), "fps": fps,
            "windowSeconds": round(duration, 3),
            "windowTargetSeconds": args.window_seconds,
            "programSceneBeforeWindow": program_before,
            "programSceneAfterWindow": program_after,
            "compositionCommittedDuringEntireWindow": True,
            "windowBaselineGetStats": window_baseline,
            "windowGetStatsSamples": samples,
            "windowRenderTotalFramesDelta": (
                int(samples[-1]["stats"].get("renderTotalFrames", 0) -
                    window_baseline.get("renderTotalFrames", 0))
                if samples and samples[-1].get("ok") else None),
            "afterWindowScreenshotStatus": screenshot_status,
            "afterWindowScreenshotError": screenshot_error,
            "afterWindowScreenshot": after_window_screenshot,
            "independentGetStatsDuringScreenshot": during_screenshot,
            "independentGetStatsErrorsDuringScreenshot": screenshot_errors,
            "preRecommitStats": pre_recommit_stats,
            "recommitIdleRenderControl": recommit_idle_control,
            "generation2CommitRoundTripMs": commit_two_round_trip_ms,
            "generation2StatsWindowMs": recommit_stats_window_ms,
            "generation2DebugCommitTiming": commit_timing,
            "postRecommitStatsBeforeSleepOrPng": post_recommit_stats,
            "recommitSkippedFramesDeltaBeforeSleepOrPng": (
                recommit_skipped_frames_delta),
            "recommitRenderedFramesDeltaBeforeSleepOrPng": (
                recommit_rendered_frames_delta),
            "afterRecommitSettleBeforePngStats": (
                after_recommit_settle_before_png_stats),
            "preResetScreenshot": pre_reset_screenshot,
            "preResetStats": pre_reset_stats,
            "generation1Plan": generation_one,
            "generation1CommitResponse": commit_one,
            "generation1FullBuildTimingIncludingSnapshots": commit_one_timing,
            "generation2ReplacementPlan": generation_two,
            "generation2CommitResponse": commit_two,
            "geometryFastPathCommits": geometry_commits,
            "repeatedTranslationCount": args.translation_repeats,
            "translationOnlyDelta": translation_delta,
            "translationFinalScreenshot": translation_final_screenshot,
            "translationPixelBaseline": translation_pixel_baseline,
            "retainedSceneModelBeforeTranslationSha256": (
                scene_model_hash(model_before_translation)),
            "retainedSceneModelAfterTranslationSha256": (
                scene_model_hash(retained_model_after_translation)
                if retained_model_after_translation is not None else None),
            "sourceSettingFastPathProof": source_setting_fast_commit,
            "internalFilterChangeProof": filter_semantics_change,
            "internalFilterFullRebuildFallbackProof": internal_filter_fallback_commit,
            "lastActivePlan": final_active_plan,
            "samePlanHashAcrossGenerations": (
                generation_one.get("planHash") == generation_two.get("planHash")),
            "rootAppearance": {key: groups_template[0].get(key)
                               for key in ("opacity", "visible", "blendMode", "mask")},
            "ownerCount": len(groups_template) - 1,
            "captureCount": len(capture_names),
            "ownerBlends": [group.get("blendMode") for group in groups_template[1:]],
            "captureBlends": [capture.get("blendMode")
                              for group in groups_template[1:]
                              for capture in group.get("children", [])
                              if capture.get("kind") == "capture"],
            "ownerMasks": [group.get("mask") for group in groups_template[1:]],
            "captureMasks": [capture.get("mask")
                             for group in groups_template[1:]
                             for capture in group.get("children", [])
                             if capture.get("kind") == "capture"],
            "sceneInventory": {"backdropSceneItemId": backdrop_id,
                               "captureSceneItemIds": capture_ids,
                               "atlasSceneItemId": atlas_id,
                               "replacedSceneItems": replaced_ids},
            "referenceProfileReport": str(args.reference_report.resolve()),
            "referenceProfileReportSha256": sha256(args.reference_report.resolve()),
            "referencePlanHash": reference_plan.get("planHash"),
            "referenceAtlasPath": str(_source_atlas),
            "referenceAtlasSha256": source_atlas_hash,
            "stagedAtlasPath": str(atlas_path),
            "stagedAtlasSha256": sha256(atlas_path),
            "nominalScreenshot": nominal,
            "resetRequestId": reset_id,
            "resetRequest": reset_request,
            "resetTimeoutSeconds": args.reset_timeout,
            "transitionReadinessSwitchDurationTouched": False,
        })

        reset_task = asyncio.create_task(protocol.request(
            primary_inbox, primary, "CallVendorRequest", reset_id,
            reset_request, timeout=args.reset_timeout))
        reset_started = time.monotonic()
        during_reset = []
        reset_stats_errors = []
        while not reset_task.done() and time.monotonic() - reset_started < args.reset_timeout:
            stat_started = time.monotonic()
            try:
                stats = await request(protocol, stats_inbox, stats_ws, "GetStats", timeout=2)
                await freeze_dump.observe(stats)
                during_reset.append({
                    "secondsFromResetStart": round(stat_started - reset_started, 3),
                    "latencyMs": round((time.monotonic() - stat_started) * 1000, 3),
                    "stats": {key: stats.get(key) for key in (
                        "activeFps", "averageFrameRenderTime", "renderTotalFrames",
                        "renderSkippedFrames")},
                })
            except Exception as error:
                reset_stats_errors.append(f"{type(error).__name__}: {error}")
            if not reset_task.done():
                await asyncio.sleep(0.3)
        try:
            response = await asyncio.wait_for(
                asyncio.shield(reset_task), timeout=0.2)
            status = response.get("requestStatus", {})
            plugin_result = response.get("responseData", {}).get("responseData", {})
            accepted = bool(status.get("result") and plugin_result.get("ok") and
                            plugin_result.get("reset"))
            scenario_report["resetOutcome"] = "response"
            scenario_report["resetResponse"] = response
            scenario_report["resetAccepted"] = accepted
        except Exception as error:
            scenario_report["resetOutcome"] = (
                "timeout" if isinstance(error, asyncio.TimeoutError) else "error")
            scenario_report["resetError"] = f"{type(error).__name__}: {error}"
            scenario_report["resetAccepted"] = False
            if not reset_task.done():
                reset_task.cancel()
                try:
                    await reset_task
                except BaseException:
                    pass
        scenario_report["resetDurationSeconds"] = round(time.monotonic() - reset_started, 3)
        scenario_report["independentGetStatsDuringReset"] = during_reset
        scenario_report["independentGetStatsErrorsDuringReset"] = reset_stats_errors
        await freeze_dump.wait_for_result()
        scenario_report["nativeFreezeDump"] = freeze_dump.result
        if scenario_report["resetAccepted"]:
            await asyncio.sleep(0.3)
            try:
                after_reset = await screenshot(
                    protocol, primary_inbox, primary, scene, "after-reset", stamp)
                scenario_report["afterResetScreenshot"] = after_reset
                scenario_report["resetRestoredNominalPixels"] = (
                    after_reset["samplePixels"] == nominal["samplePixels"])
            except Exception as error:
                scenario_report["afterResetScreenshotError"] = (
                    f"{type(error).__name__}: {error}")
        report["scenario"] = scenario_report
    finally:
        await stats_ws.close()
        await primary.close()


def main() -> int:
    if (len(sys.argv) == 4 and
            sys.argv[1] == "--native-thread-dump-helper"):
        try:
            result = write_native_thread_dump(
                int(sys.argv[2]), pathlib.Path(sys.argv[3]))
            print(json.dumps(result, separators=(",", ":")), flush=True)
            return 0
        except Exception as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr, flush=True)
            return 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-source", type=pathlib.Path, required=True)
    parser.add_argument("--upstream-source", type=pathlib.Path, required=True)
    parser.add_argument("--upstream-build", type=pathlib.Path, required=True)
    parser.add_argument("--plugin", type=pathlib.Path, required=True)
    parser.add_argument("--plugin-build-cache", type=pathlib.Path,
                        help="CMake cache of a freshly built candidate; omit for a retained binary")
    parser.add_argument("--shutdown-helper", type=pathlib.Path, required=True)
    parser.add_argument("--reference-report", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, default=EVIDENCE)
    parser.add_argument("--reset-timeout", type=float, default=20.0)
    parser.add_argument("--window-seconds", type=float, default=WINDOW_SECONDS,
                        help="render-window duration; shorten for isolated fast-path diagnosis")
    parser.add_argument("--translation-only", action="store_true",
                        help="move one synthetic /2 capture by (+37,+23) during generation 2")
    parser.add_argument("--translation-repeats", type=int, default=4,
                        help="even number of additional (+/-11,+/-7) retained-tree translations")
    parser.add_argument("--translation-baseline-image", type=pathlib.Path,
                        default=EVIDENCE / "20261018T143423Z-translation-final-1920x1080.png",
                        help="full-rebuild 1920x1080 PNG used for exact pixel comparison")
    parser.add_argument("--capture-freeze-dump", action="store_true",
                        help="write one private Windows minidump after a 2s render-frame stall and read the exact live mutex fields")
    args = parser.parse_args()
    if not 5 <= args.reset_timeout <= 60:
        raise ValueError("reset timeout must be bounded between 5 and 60 seconds")
    if not 2 <= args.window_seconds <= 60:
        raise ValueError("render window must be between 2 and 60 seconds")
    if args.translation_only:
        if not 2 <= args.translation_repeats <= 12 or args.translation_repeats % 2:
            raise ValueError("translation repeats must be an even number from 2 through 12")
        args.translation_baseline_image = args.translation_baseline_image.resolve()
        if not args.translation_baseline_image.is_file():
            raise FileNotFoundError(args.translation_baseline_image)

    runtime_source = args.runtime_source.resolve()
    upstream_source = args.upstream_source.resolve()
    upstream_build = args.upstream_build.resolve()
    plugin_source = args.plugin.resolve()
    helper_source = args.shutdown_helper.resolve()
    reference_report_path = args.reference_report.resolve()
    output = args.output.resolve()
    if output != EVIDENCE.resolve():
        raise ValueError(f"output must be canonical: {EVIDENCE}")
    if runtime_source != (upstream_build / "rundir" / "RelWithDebInfo").resolve():
        raise ValueError("runtime must belong to the supplied qualified ABI cache")
    if (not runtime_source.is_dir() or not plugin_source.is_file() or
            not helper_source.is_file() or not MUTEX_OWNER_PROBE.is_file()):
        raise FileNotFoundError("qualified runtime or private plugin/helper is missing")
    if not reference_report_path.is_file():
        raise FileNotFoundError(reference_report_path)
    if sha256(helper_source) != EXPECTED_HELPER_SHA256:
        raise RuntimeError("shutdown helper does not match the qualified native-build helper")
    reference_json = json.loads(reference_report_path.read_text(encoding="utf-8"))
    _plan, _groups, source_atlas, source_atlas_hash = reference_workload(reference_json)

    upstream_git = subprocess.run(
        ["git", "-C", str(upstream_source), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()
    upstream_status = subprocess.run(
        ["git", "-C", str(upstream_source), "status", "--short"],
        check=True, capture_output=True, text=True).stdout.strip()
    if upstream_git != EXPECTED_UPSTREAM_HEAD or upstream_status:
        raise RuntimeError(
            f"qualified upstream changed: HEAD={upstream_git} status={upstream_status!r}")
    cmake_cache = upstream_build / "CMakeCache.txt"
    if not cmake_cache.is_file():
        raise FileNotFoundError(cmake_cache)
    cmake_home_line = next((line for line in cmake_cache.read_text(
        encoding="utf-8", errors="replace").splitlines()
        if line.startswith("CMAKE_HOME_DIRECTORY:INTERNAL=")), None)
    if not cmake_home_line:
        raise RuntimeError("qualified upstream build CMakeCache has no CMAKE_HOME_DIRECTORY")
    cmake_home_value = cmake_home_line.split("=", 1)[1]
    cmake_home_leaf = pathlib.PureWindowsPath(cmake_home_value.replace("/", "\\")).name
    expected_build_home_leaf = upstream_source.name + "-windows-checkout"
    if cmake_home_leaf != expected_build_home_leaf:
        raise RuntimeError(
            f"qualified upstream build source leaf changed: {cmake_home_leaf!r}")
    build_source = upstream_source.with_name(cmake_home_leaf).resolve()
    if not build_source.is_dir():
        raise FileNotFoundError(build_source)
    build_source_git = subprocess.run(
        ["git", "-C", str(build_source), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()
    build_source_status = subprocess.run(
        ["git", "-C", str(build_source), "status", "--short"],
        check=True, capture_output=True, text=True).stdout.strip()
    build_libobs_status = subprocess.run(
        ["git", "-C", str(build_source), "status", "--short", "--", "libobs"],
        check=True, capture_output=True, text=True).stdout.strip()
    if build_source_git != EXPECTED_BUILD_SOURCE_HEAD or build_libobs_status:
        raise RuntimeError(
            "qualified build source mismatch: "
            f"HEAD={build_source_git} libobsStatus={build_libobs_status!r}")
    obs_lib = upstream_build / "libobs" / "RelWithDebInfo" / "obs.lib"
    if not obs_lib.is_file():
        raise FileNotFoundError(obs_lib)
    obs_lib_hash = sha256(obs_lib)
    if obs_lib_hash != EXPECTED_OBS_LIB_SHA256:
        raise RuntimeError(
            f"qualified obs.lib hash changed: expected {EXPECTED_OBS_LIB_SHA256}, got {obs_lib_hash}")

    browser_modules = sorted(
        path.name for path in (runtime_source / "obs-plugins" / "64bit").glob("*.dll")
        if "browser" in path.name.casefold())
    if browser_modules:
        raise RuntimeError("qualified runtime unexpectedly contains browser modules")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    report_path = output / f"{stamp}-reset-exact.json"
    log_path = output / f"{stamp}-reset-exact-host-log.txt"
    if report_path.exists() or log_path.exists():
        raise FileExistsError(report_path)
    private_root = CACHE / f"private-runtime-exact-{stamp}"
    runtime = private_root / "runtime"
    if private_root.exists() or not private_root.resolve().is_relative_to(CACHE.resolve()):
        raise FileExistsError(private_root)
    runtime.mkdir(parents=True)
    for directory in ("bin", "data", "obs-plugins"):
        shutil.copytree(runtime_source / directory, runtime / directory,
                        dirs_exist_ok=True)
    plugin_target = runtime / "obs-plugins" / "64bit" / "pulsar-capture-effects.dll"
    helper_target = runtime / "obs-plugins" / "64bit" / "z-group-composition-shutdown-test-helper.dll"
    shutil.copyfile(plugin_source, plugin_target)
    shutil.copyfile(helper_source, helper_target)
    atlas_target = CACHE / f"{stamp}-four-owner-reference-atlas.png"
    shutil.copyfile(source_atlas, atlas_target)
    if sha256(atlas_target) != source_atlas_hash:
        raise RuntimeError("private staged atlas differs from the preserved reference fixture")

    process_protocol = load("reset_exact_process", "probe-websocket.py")
    composition_probe = load("reset_exact_host", "probe-group-composition.py")
    runtime_id = "reset-exact-" + uuid.uuid4().hex
    port = process_protocol.pick_free_port()
    password = uuid.uuid4().hex + uuid.uuid4().hex
    exe = runtime / "bin" / "64bit" / "pulsar.exe"
    process = None
    previous_env = dict(os.environ)
    report = {
        "startedUtc": datetime.now(timezone.utc).isoformat(),
        "passed": False,
        "scope": "exact synthetic four-owner/eight-capture reset reproduction",
        "transitionReadinessSwitchDurationTouched": False,
        "liveOrRecording": False,
        "runtimeInstanceId": runtime_id,
        "sourceWorktree": str(ROOT),
        "sourceHead": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip(),
        "sourceStatusBeforeRun": subprocess.run(
            ["git", "-C", str(ROOT), "status", "--short"],
            check=True, capture_output=True, text=True).stdout.strip(),
        "referenceReport": {
            "path": str(reference_report_path), "sha256": sha256(reference_report_path),
            "originalScenario": "four-masked-blended-owners",
            "originalPlanHash": _plan.get("planHash"),
            "originalWindowSeconds": 20.016,
        },
        "qualifiedObsProvenance": {
            "upstreamSource": str(build_source), "upstreamRevision": build_source_git,
            "upstreamWorkingTreeStatus": build_source_status,
            "buildCMakeHomeDirectory": cmake_home_value,
            "buildCMakeCacheSha256": sha256(cmake_cache),
            "buildSourceRelevantLibobsStatus": build_libobs_status,
            "preflightSourceCheckout": {
                "path": str(upstream_source), "revision": upstream_git,
                "workingTreeStatus": upstream_status,
                "role": "clean ABI/source review checkout; not the CMake build source",
            },
            "upstreamBuildPath": str(upstream_build),
            "obsLibPath": str(obs_lib), "obsLibSha256": obs_lib_hash,
            "cachePathSuffixIsNotGitRevision": True,
            "actualSourceHeadDiffersFromCacheSuffix": build_source_git != "25ed780b7f392a1690dfca929cd57679959cf3ad",
            "libobsSourceHashes": {
                name: sha256(build_source / "libobs" / name)
                for name in ("obs.c", "obs-video.c", "obs-scene.c", "obs-source.c")
            },
        },
        "runtimeProvenance": {
            "runtimeSource": str(runtime_source),
            "pulsarExeSha256": sha256(runtime_source / "bin" / "64bit" / "pulsar.exe"),
            "obsDllSha256": sha256(runtime_source / "bin" / "64bit" / "obs.dll"),
            "obsWebsocketSha256": sha256(runtime_source / "obs-plugins" / "64bit" / "obs-websocket.dll"),
            "browserModules": browser_modules,
        },
        "privateRuntime": {
            "path": str(runtime), "retained": True,
            "capturePluginPath": str(plugin_target),
            "capturePluginSha256": sha256(plugin_target),
            "shutdownHelperPath": str(helper_target),
            "shutdownHelperSha256": sha256(helper_target),
            "helperMatchesQualifiedHash": sha256(helper_target) == EXPECTED_HELPER_SHA256,
            "atlasPath": str(atlas_target), "atlasSha256": sha256(atlas_target),
        },
        "instrumentation": {
            "sourcePath": str(ROOT / "plugins/pulsar-capture-effects/src/group-composition.cpp"),
            "sourceSha256": sha256(ROOT / "plugins/pulsar-capture-effects/src/group-composition.cpp"),
            "relatedSources": {
                name: sha256(ROOT / path) for name, path in {
                    "captureEffects": "plugins/pulsar-capture-effects/src/capture-effects.cpp",
                    "worldSource": "plugins/pulsar-capture-effects/src/group-composition-world-source.cpp",
                    "traceHeader": "plugins/pulsar-capture-effects/src/reset-trace.hpp",
                    "probe": "scripts/probe-group-composition-reset-exact.py",
                }.items()
            },
            "mutexOwnerProbe": str(MUTEX_OWNER_PROBE),
            "mutexOwnerProbeSha256": sha256(MUTEX_OWNER_PROBE),
            "compileDefinition": "PULSAR_GROUP_COMPOSITION_RESET_TRACE",
            "buildCache": ({"path": str(args.plugin_build_cache.resolve()),
                            "sha256": sha256(args.plugin_build_cache.resolve())}
                           if args.plugin_build_cache else
                           "not-rebuilt; reused retained qualified private-runtime artifact"),
            "pluginInputPath": str(plugin_source),
            "pluginSha256": sha256(plugin_source),
            "buildFlags": "/DPULSAR_GROUP_COMPOSITION_RESET_TRACE /EHsc",
        },
    }
    try:
        for key in list(os.environ):
            if key.startswith("PULSAR_"):
                del os.environ[key]
        os.environ.update({
            "PULSAR_RUNTIME_ROOT": str(private_root / "state"),
            "PULSAR_RUNTIME_DIR": str(private_root / "state" / "runtime"),
            "PULSAR_RUNTIME_INSTANCE_ID": runtime_id,
            "PULSAR_DESKTOP_AUDIO_DEVICE_ID": "pulsar-synthetic-test-no-device",
            "PULSAR_TEST_NO_BROWSER_PRE_SHUTDOWN": "1",
        })
        process = composition_probe.GracefulPrivatePulsarProcess(
            process_protocol, exe, port, password)
        process.spawn()
        report["ownedPid"] = process.proc.proc.pid
        process.wait_ready(60)
        time.sleep(2)
        helper_registration = [line for line in process._lines
                               if "no_browser_pre_shutdown_fixture_registered" in line]
        if not helper_registration:
            raise RuntimeError("staged shutdown helper did not register its no-browser fixture")
        report["shutdownHelperRegistration"] = helper_registration
        awaitable = run_scenario(args, process, runtime_id, port, password,
                                 report, stamp, atlas_target)
        asyncio.run(awaitable)
        scenario = report.get("scenario", {})
        report["diagnosticCompleted"] = True
        report["passed"] = bool(
            scenario.get("resetAccepted") is True and
            scenario.get("afterWindowScreenshotStatus") == "success" and
            scenario.get("windowSeconds", 0) >= 19.5 and
            scenario.get("windowRenderTotalFramesDelta", 0) > 0 and
            scenario.get("resetRestoredNominalPixels") is True)
    except Exception as error:
        report["errorType"] = type(error).__name__
        report["error"] = str(error)[:3000]
    finally:
        if process is not None:
            try:
                process.shutdown(30)
            except Exception as error:
                report["shutdownError"] = f"{type(error).__name__}: {error}"
            child = process.proc.proc
            report["ownedProcessExited"] = bool(child and child.poll() is not None)
            report["hostExitCode"] = child.returncode if child else None
            report["gracefulShutdownRequested"] = process.graceful_shutdown_requested
            report["forcedTerminationUsed"] = process.forced_termination_used
            report["shutdownHandleClosed"] = process.shutdown_handle is None
            report["hostLogLines"] = [process_protocol.redact_boot_line(line)
                                      for line in process._lines]
            report["resetTraceLines"] = [
                line for line in report["hostLogLines"]
                if ("PULSAR_GROUP_COMPOSITION_RESET" in line or
                    "PULSAR_NATIVE_RESET_TRACE" in line)]
            report["shutdownHelperEvidence"] = {
                "fixtureRegistered": any(
                    "no_browser_pre_shutdown_fixture_registered" in line
                    for line in report["hostLogLines"]),
                "preShutdownSucceeded": any(
                    "PULSAR_BROWSER_QUIESCE event=pre_obs_shutdown_complete" in line
                    for line in report["hostLogLines"]),
                "websocketQuiesceSucceeded": any(
                    "PULSAR_WEBSOCKET_QUIESCE event=ack active_handlers=0" in line
                    for line in report["hostLogLines"]),
            }
        os.environ.clear()
        os.environ.update(previous_env)
    phases = []
    for line in report.get("resetTraceLines", []):
        if " phase=" in line:
            phases.append(line.split(" phase=", 1)[1].strip())
    report["resetTracePhases"] = phases
    report["traceInterpretation"] = (
        "queue_wait_begin without graphics_enter means the callback did not enter; "
        "the last graphics phase identifies the in-callback boundary; absence of "
        "queue_wait_end means the synchronous graphics queue wait did not return.")
    report["endedUtc"] = datetime.now(timezone.utc).isoformat()
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    log_path.write_text("\n".join(report.get("hostLogLines", [])) + "\n",
                        encoding="utf-8")
    print(json.dumps({
        "report": str(report_path), "passed": report["passed"],
        "ownedPid": report.get("ownedPid"),
        "exitCode": report.get("hostExitCode"),
        "resetOutcome": report.get("scenario", {}).get("resetOutcome"),
        "resetTracePhases": phases,
    }, ensure_ascii=False), flush=True)
    return 0 if report.get("diagnosticCompleted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
