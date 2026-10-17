#!/usr/bin/env python3
"""Private synthetic GPU proof for native group capture composition.

This probe launches a disposable Pulsar copy, drives obs-websocket over its
loopback-only vendor request, checks readback pixels and then resets the owned
group. It never starts Live/recording or touches physical capture devices.
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
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

from PIL import Image, ImageDraw
import websockets

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence" / "local-20261017-native-group-coverage" / "eleven"
CACHE = ROOT / ".cache" / "native-group-coverage"
ROOT_GROUP_ID = "prism.native-composition.root.v1"
SOURCE_PLAN_HASH = hashlib.sha256(b"private-native-group-proof-v1").hexdigest()


class GracefulPrivatePulsarProcess:
    """Own a private Pulsar child and shut it down through its inherited event."""

    def __init__(self, protocol, exe: pathlib.Path, port: int, password: str):
        self.protocol = protocol
        self.proc = protocol.PulsarProcess(exe, port, password)
        self.shutdown_handle = None
        self.graceful_shutdown_requested = False
        self.forced_termination_used = False

    def __getattr__(self, name):
        return getattr(self.proc, name)

    @staticmethod
    def _kernel32():
        return ctypes.WinDLL("kernel32", use_last_error=True)

    def spawn(self):
        if os.name != "nt":
            self.proc.spawn()
            return
        attributes_type = type(
            "SecurityAttributes",
            (ctypes.Structure,),
            {"_fields_": [("nLength", ctypes.c_uint32),
                          ("lpSecurityDescriptor", ctypes.c_void_p),
                          ("bInheritHandle", ctypes.c_int)]},
        )
        kernel32 = self._kernel32()
        create_event = kernel32.CreateEventW
        create_event.argtypes = [ctypes.POINTER(attributes_type), ctypes.c_int,
                                 ctypes.c_int, ctypes.c_wchar_p]
        create_event.restype = ctypes.c_void_p
        attributes = attributes_type(ctypes.sizeof(attributes_type), None, 1)
        handle = create_event(ctypes.byref(attributes), 1, 0, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateEventW failed")
        self.shutdown_handle = int(handle)

        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.proc.port)
        env["PULSAR_PASSWORD"] = self.proc.password
        env["PULSAR_SHUTDOWN_EVENT_HANDLE"] = str(self.shutdown_handle)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpAttributeList = {"handle_list": [self.shutdown_handle]}
        try:
            self.proc.proc = subprocess.Popen(
                [str(self.proc.exe)], cwd=str(self.proc.exe.parent), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, bufsize=1, text=True,
                encoding="utf-8", errors="replace",
                creationflags=0x08000000, startupinfo=startupinfo,
                close_fds=True)
        except BaseException:
            self.close_event()
            raise
        set_handle_information = kernel32.SetHandleInformation
        set_handle_information.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                           ctypes.c_uint32]
        set_handle_information.restype = ctypes.c_int
        if not set_handle_information(ctypes.c_void_p(self.shutdown_handle), 1, 0):
            self.proc.terminate()
            self.close_event()
            raise OSError(ctypes.get_last_error(), "SetHandleInformation failed")
        self.proc._pump_thread = threading.Thread(
            target=self.proc._pump_stdout, daemon=True)
        self.proc._pump_thread.start()

    def close_event(self):
        if os.name == "nt" and self.shutdown_handle is not None:
            close_handle = self._kernel32().CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            if not close_handle(ctypes.c_void_p(self.shutdown_handle)):
                raise OSError(ctypes.get_last_error(), "CloseHandle failed")
            self.shutdown_handle = None

    def shutdown(self, grace: float = 8.0):
        child = self.proc.proc
        if child is None or child.poll() is not None:
            self.close_event()
            return
        if os.name == "nt":
            if self.shutdown_handle is None:
                raise RuntimeError("private child has no graceful shutdown event")
            set_event = self._kernel32().SetEvent
            set_event.argtypes = [ctypes.c_void_p]
            set_event.restype = ctypes.c_int
            if not set_event(ctypes.c_void_p(self.shutdown_handle)):
                raise OSError(ctypes.get_last_error(), "SetEvent failed")
            self.graceful_shutdown_requested = True
        else:
            child.terminate()
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.forced_termination_used = True
            child.terminate()
            child.wait(timeout=grace)
        if self.proc._pump_thread is not None:
            self.proc._pump_thread.join(timeout=1)
        self.close_event()


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def affine(x: float = 0, y: float = 0):
    return {"m00": 1, "m01": 0, "tx": x,
            "m10": 0, "m11": 1, "ty": y}


def assert_opacity_rgb(actual, source, opacity, label):
    expected = tuple(round(channel * opacity) for channel in source[:3])
    assert all(abs(value - target) <= 1 for value, target in zip(actual[:3], expected)), {
        "label": label, "actual": actual, "source": source,
        "expectedRgb": expected, "opacity": opacity}


def make_wire_plan(scene: str, runtime_id: str, generation: int, groups: list[dict],
                   viewport: tuple[int, int],
                   replaced_scene_items: list[int] | None = None,
                   protocol_version: int = 1):
    replaced_scene_items = sorted(replaced_scene_items or [])
    coordinate_space = "global-scene-texels" if protocol_version == 2 else None
    plan_hash = canonical_plan_hash(groups, viewport, replaced_scene_items,
                                    coordinate_space)
    payload = {"protocol": f"pulsar.group-composition/{protocol_version}",
            "sceneName": scene,
            "generation": generation, "runtimeInstanceId": runtime_id,
            "planHash": plan_hash, "groups": groups,
            "replacedSceneItems": [
                {"sceneItemId": item_id} for item_id in replaced_scene_items]}
    if protocol_version == 2:
        payload["coordinateSpace"] = "global-scene-texels"
    return payload


def canonical_plan_hash(groups: list[dict], viewport: tuple[int, int],
                        replaced_scene_items: list[int],
                        coordinate_space: str | None = None) -> str:
    native_plan = {
        "sourcePlanHash": SOURCE_PLAN_HASH,
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "groups": groups,
        "replacedSceneItems": sorted(replaced_scene_items),
    }
    if coordinate_space is not None:
        native_plan["coordinateSpace"] = coordinate_space
    encoded = json.dumps(native_plan, sort_keys=True,
                         separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def make_global_v2_plan(scene: str, runtime_id: str, generation: int,
                        groups: list[dict], viewport: tuple[int, int],
                        replaced_scene_items: list[int]):
    return make_wire_plan(scene, runtime_id, generation, groups, viewport,
                          replaced_scene_items, protocol_version=2)


def plan(scene: str, runtime_id: str, generation: int, red_id: int,
         blue_id: int, old_array_affine: bool = False,
         group_opacity: float = 0.5,
         viewport: tuple[int, int] = (1920, 1080),
         reverse_children: bool = False):
    capture_affine = [1, 0, 0, 0, 1, 0] if old_array_affine else affine()
    captures = [
        {"kind": "capture", "sceneItemId": red_id, "affine": capture_affine,
         "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
         "localOpacity": 1},
        {"kind": "capture", "sceneItemId": blue_id, "affine": affine(),
         "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
         "localOpacity": 1, "visible": True},
    ]
    if reverse_children:
        captures.reverse()
    width, height = viewport
    groups = [
        {"id": ROOT_GROUP_ID, "parentId": None, "order": 0, "opacity": 1,
         "visible": True,
         "blendMode": "normal", "bounds": {"x": 0, "y": 0, "width": width, "height": height},
         "affine": affine(), "children": [{"kind": "group", "id": "proof-group"}],
         "mask": None},
        {"id": "proof-group", "parentId": ROOT_GROUP_ID, "order": 0,
         "opacity": group_opacity, "visible": True, "blendMode": "normal",
         "bounds": {"x": 0, "y": 0, "width": width, "height": height},
         "affine": affine(), "children": captures, "mask": None},
    ]
    return make_wire_plan(scene, runtime_id, generation, groups, viewport)


def translated_capture_plan(scene: str, runtime_id: str, generation: int,
                             scene_item_id: int, viewport: tuple[int, int],
                             x: int, opacity: float):
    width, height = viewport
    child = {"kind": "capture", "sceneItemId": scene_item_id,
             "affine": affine(x=x, y=0),
             "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
             "localOpacity": opacity, "visible": True}
    groups = [
        {"id": ROOT_GROUP_ID, "parentId": None, "order": 0, "opacity": 1,
         "visible": True, "blendMode": "normal",
         "bounds": {"x": 0, "y": 0, "width": width, "height": height},
         "affine": affine(), "children": [{"kind": "group", "id": "offset-group"}],
         "mask": None},
        {"id": "offset-group", "parentId": ROOT_GROUP_ID, "order": 0,
         "opacity": 1, "visible": True, "blendMode": "normal",
         "bounds": {"x": 0, "y": 0, "width": width, "height": height},
         "affine": affine(), "children": [child], "mask": None},
    ]
    return make_wire_plan(scene, runtime_id, generation, groups, viewport)


def solar_band_plan(scene: str, runtime_id: str, generation: int,
                    scene_item_id: int, viewport: tuple[int, int],
                    bands: list[dict], owner_translation: tuple[int, int] = (200, 100),
                    owner_mask=None,
                    replaced_scene_items: list[int] | None = None):
    width, height = viewport
    children = [
        {"kind": "solarBand", "band": band["band"],
         "sceneItemId": scene_item_id, "crop": band["crop"],
         "rect": band["rect"], "affine": affine()}
        for band in bands
    ]
    groups = [
        {"id": ROOT_GROUP_ID, "parentId": None, "order": 0, "opacity": 1,
         "visible": True, "blendMode": "normal",
         "bounds": {"x": 0, "y": 0, "width": width, "height": height},
         "affine": affine(), "children": [{"kind": "group", "id": "band-owner"}],
         "mask": None},
        {"id": "band-owner", "parentId": ROOT_GROUP_ID, "order": 0,
         "opacity": 1, "visible": True, "blendMode": "normal",
         "bounds": {"x": 0, "y": 0, "width": width, "height": height},
         "affine": affine(*owner_translation), "children": children,
         "mask": owner_mask},
    ]
    return make_wire_plan(scene, runtime_id, generation, groups, viewport,
                          replaced_scene_items)


def mixed_composition_plan(scene: str, runtime_id: str, generation: int,
                           viewport: tuple[int, int], children: list[dict],
                           owner_affine: dict, owner_opacity: float,
                           replaced_scene_items: list[int] | None = None,
                           nested_group: dict | None = None):
    width, height = viewport
    if nested_group is None:
        owner_children = children
        groups = []
    else:
        owner_children = children + [{"kind": "group", "id": nested_group["id"]}]
        groups = [{
            "id": nested_group["id"], "parentId": "mixed-owner",
            "order": len(owner_children) - 1, "opacity": nested_group["opacity"],
            "visible": nested_group["visible"], "blendMode": nested_group["blendMode"],
            "bounds": nested_group["bounds"], "affine": nested_group["affine"],
            "children": nested_group["children"], "mask": nested_group["mask"],
        }]
    groups = [
        {"id": ROOT_GROUP_ID, "parentId": None, "order": 0, "opacity": 1,
         "visible": True, "blendMode": "normal",
         "bounds": {"x": 0, "y": 0, "width": width, "height": height},
         "affine": affine(), "children": [{"kind": "group", "id": "mixed-owner"}],
         "mask": None},
        {"id": "mixed-owner", "parentId": ROOT_GROUP_ID, "order": 0,
         "opacity": owner_opacity, "visible": True, "blendMode": "normal",
         "bounds": {"x": 0, "y": 0, "width": width, "height": height},
         "affine": owner_affine, "children": owner_children, "mask": None},
    ] + groups
    return make_wire_plan(scene, runtime_id, generation, groups, viewport,
                          replaced_scene_items)


def assert_pixel_near(actual, expected, label: str, tolerance: int = 3):
    assert all(abs(value - target) <= tolerance
               for value, target in zip(actual, expected)), {
        "label": label, "actual": actual, "expected": expected,
        "tolerance": tolerance}


CSS_BLEND_MODES = [
    ("normal", 0), ("multiply", 1), ("screen", 2),
    ("hard-light", 3), ("soft-light", 4), ("overlay", 5),
    ("darken", 6), ("lighten", 7), ("difference", 8),
    ("exclusion", 9), ("color-dodge", 11), ("color-burn", 12),
    ("hue", 13), ("saturation", 14), ("color", 15),
    ("luminosity", 16),
]


def _lum(color):
    return 0.3 * color[0] + 0.59 * color[1] + 0.11 * color[2]


def _sat(color):
    return max(color) - min(color)


def _set_lum(color, target):
    result = [value + target - _lum(color) for value in color]
    low, high = min(result), max(result)
    if low < 0:
        result = [target + (value - target) * target / max(target - low, 0.000001)
                  for value in result]
    high = max(result)
    if high > 1:
        result = [target + (value - target) * (1 - target) /
                  max(high - target, 0.000001) for value in result]
    return result


def _set_sat(color, target):
    low, high = min(color), max(color)
    spread = high - low
    if spread <= 0:
        return [0.0, 0.0, 0.0]
    return [(value - low) * target / spread for value in color]


def css_blend(mode_id: int, backdrop, source):
    def channel(b, s):
        if mode_id == 1:
            return b * s
        if mode_id == 2:
            return b + s - b * s
        if mode_id == 3:
            return 2 * b * s if s <= 0.5 else 1 - 2 * (1 - b) * (1 - s)
        if mode_id == 4:
            if s <= 0.5:
                return b - (1 - 2 * s) * b * (1 - b)
            curve = ((16 * b - 12) * b + 4) * b if b <= 0.25 else b ** 0.5
            return b + (2 * s - 1) * (curve - b)
        if mode_id == 5:
            return 2 * b * s if b <= 0.5 else 1 - 2 * (1 - b) * (1 - s)
        if mode_id == 6:
            return min(b, s)
        if mode_id == 7:
            return max(b, s)
        if mode_id == 8:
            return abs(b - s)
        if mode_id == 9:
            return b + s - 2 * b * s
        if mode_id == 11:
            return 0 if b <= 0 else 1 if s >= 1 else min(1, b / (1 - s))
        if mode_id == 12:
            return 1 if b >= 1 else 0 if s <= 0 else 1 - min(1, (1 - b) / s)
        return s

    if mode_id == 13:
        return _set_lum(_set_sat(source, _sat(backdrop)), _lum(backdrop))
    if mode_id == 14:
        return _set_lum(_set_sat(backdrop, _sat(source)), _lum(backdrop))
    if mode_id == 15:
        return _set_lum(source, _lum(backdrop))
    if mode_id == 16:
        return _set_lum(backdrop, _lum(source))
    return [channel(b, s) for b, s in zip(backdrop, source)]


def expected_over(backdrop, source, opacity: float):
    return tuple(round(s * opacity + b * (1 - opacity))
                 for b, s in zip(backdrop[:3], source[:3])) + (255,)


def expected_css_composite(backdrop, source, mode_id: int, opacity: float = 1):
    b = [value / 255 for value in backdrop[:3]]
    s = [value / 255 for value in source[:3]]
    blended = css_blend(mode_id, b, s)
    return tuple(round((base * (1 - opacity) + value * opacity) * 255)
                 for base, value in zip(b, blended)) + (255,)


def make_atlas_fixture(path: pathlib.Path):
    atlas = Image.new("RGBA", (1920, 1080), (0, 255, 255, 255))
    atlas.paste((255, 0, 0, 255), (700, 350, 760, 410))
    atlas.paste((0, 255, 0, 255), (900, 350, 960, 410))
    atlas.save(path)


def make_coverage_atlas_fixture(path: pathlib.Path):
    atlas = Image.new("RGBA", (28, 10), (255, 255, 255, 0))
    for x in range(atlas.width):
        alpha = 0 if x < 10 else 128 if x < 20 else 255
        for y in range(atlas.height):
            atlas.putpixel((x, y), (255, 255, 255, alpha))
    atlas.save(path)


def make_updated_coverage_atlas_fixture(path: pathlib.Path):
    atlas = Image.new("RGBA", (28, 10), (255, 255, 255, 0))
    for x in range(atlas.width):
        alpha = 255 if x < 10 else 64 if x < 20 else 0
        for y in range(atlas.height):
            atlas.putpixel((x, y), (255, 255, 255, alpha))
    atlas.save(path)


def make_prism_wire_coverage_atlas_fixture(path: pathlib.Path,
                                          updated: bool = False):
    """Build the Solar atlas layout used by Prism's actual /2 projection."""
    atlas = Image.new("RGBA", (1922, 3246), (255, 255, 255, 0))
    draw = ImageDraw.Draw(atlas)
    band_color = (220, 100, 40, 255) if updated else (80, 160, 220, 255)
    draw.rectangle((1, 1, 1920, 1080), fill=band_color)

    # Solar's source crop leaves one physical pixel on each side. The group
    # mask is the next exact 1920x1080 band in the same atlas.
    group_partial = 255 if updated else 128
    draw.rectangle((1, 1083, 10, 2162), fill=(255, 255, 255, 0))
    draw.rectangle((11, 1083, 20, 2162),
                   fill=(255, 255, 255, group_partial))
    draw.rectangle((21, 1083, 1920, 2162), fill=(255, 255, 255, 255))

    # Child coverage is addressed in global viewport texels. At global x=50
    # the camera contribution lands at local x=15 after Prism's 2x transform.
    child_partial = 0 if updated else 128
    draw.rectangle((1, 2165, 30, 3244), fill=(255, 255, 255, 0))
    draw.rectangle((31, 2165, 50, 3244), fill=(255, 255, 255, 255))
    draw.rectangle((51, 2165, 60, 3244),
                   fill=(255, 255, 255, child_partial))
    draw.rectangle((61, 2165, 1920, 3244), fill=(255, 255, 255, 255))
    atlas.save(path)


def emit_prism_wire(emitter: pathlib.Path, output: pathlib.Path,
                    scratch: pathlib.Path, input_data: dict) -> dict:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required for the signed Prism wire emitter")
    input_path = scratch / "prism-wire-emitter-input.json"
    cache_dir = CACHE / "prism-wire-emitter-vite-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    input_data = {**input_data, "cacheDir": str(cache_dir)}
    input_path.write_text(json.dumps(input_data, indent=2) + "\n",
                          encoding="utf-8")
    result = subprocess.run(
        [node, str(emitter), str(input_path), str(output)],
        cwd=str(emitter.parent), capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        raise RuntimeError(
            f"Prism wire emitter failed ({result.returncode}): "
            f"{result.stderr[-2000:]} {result.stdout[-2000:]}")
    emitted = json.loads(output.read_text(encoding="utf-8"))
    wire = emitted["wire"]
    assert wire["protocol"] == "pulsar.group-composition/2", wire
    assert wire["coordinateSpace"] == "global-scene-texels", wire
    assert wire["sceneName"] == input_data["sceneName"], wire
    assert wire["runtimeInstanceId"] == input_data["runtimeInstanceId"], wire
    assert wire["generation"] == input_data["generation"], wire
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", wire["planHash"]), wire["planHash"]
    expected_replaced = sorted({input_data["cefItemId"],
                                input_data["captureItemId"]})
    actual_replaced = sorted(item["sceneItemId"]
                             for item in wire["replacedSceneItems"])
    assert actual_replaced == expected_replaced, {
        "actual": actual_replaced, "expected": expected_replaced,
    }
    groups = wire["groups"]
    assert len(groups) == 1 and groups[0]["id"] == ROOT_GROUP_ID, groups
    root = groups[0]
    assert root["mask"]["sceneItemId"] == input_data["cefItemId"], root["mask"]
    children = root["children"]
    band = next(child for child in children if child["kind"] == "solarBand")
    capture = next(child for child in children if child["kind"] == "capture")
    assert band["sceneItemId"] == input_data["cefItemId"], band
    assert capture["sceneItemId"] == input_data["contributionItemId"], capture
    assert capture["mask"]["sceneItemId"] == input_data["cefItemId"], capture
    return {
        "emitterInput": input_data,
        "emitterInputSha256": sha256(input_path),
        "emitterOutput": str(output),
        "emitterOutputSha256": sha256(output),
        "wire": wire,
        "provenance": emitted["provenance"],
    }


async def exercise(config: dict, output: pathlib.Path, runtime_id: str,
                   scratch: pathlib.Path, expect_v2_rejection: bool = False,
                   prism_wire_emitter: pathlib.Path | None = None):
    protocol = load("group_comp_wire", "probe-source-kinds.py")
    token = uuid.uuid4().hex[:10]
    scene = f"native-group-proof-{token}"
    appearance_scene = f"native-appearance-{token}"
    offset_scene = f"native-offset-{token}"
    solar_scene = f"native-solar-band-{token}"
    mixed_scene = f"native-mixed-composition-{token}"
    coverage_scene = f"native-group-coverage-{token}"
    lifecycle_scene = f"native-remove-dependency-{token}"
    same_name_scene = f"native-remove-scene-{token}"
    unload_scene = f"native-unload-active-{token}"
    red_name = f"native-group-red-{token}"
    blue_name = f"native-group-blue-{token}"
    offset_name = f"native-offset-source-{token}"
    atlas_name = f"native-solar-atlas-{token}"
    legacy_name = f"native-solar-legacy-{token}"
    mixed_atlas_name = f"native-mixed-atlas-{token}"
    coverage_background_name = f"native-coverage-background-{token}"
    coverage_backdrop_name = f"native-coverage-backdrop-{token}"
    coverage_owner_background_name = f"native-coverage-owner-background-{token}"
    coverage_red_name = f"native-coverage-red-{token}"
    coverage_green_name = f"native-coverage-green-{token}"
    coverage_capture_name = f"native-coverage-capture-{token}"
    coverage_capture_binding_name = f"native-coverage-capture-binding-{token}"
    coverage_blend_source_name = f"native-coverage-blend-source-{token}"
    coverage_atlas_name = f"native-coverage-atlas-{token}"
    mixed_legacy_name = f"native-mixed-legacy-{token}"
    mixed_capture_name = f"native-mixed-capture-{token}"
    nested_capture_name = f"native-nested-capture-{token}"
    lifecycle_legacy_name = f"native-remove-legacy-{token}"
    lifecycle_input_name = f"native-remove-input-{token}"
    graphics_input_name = f"native-graphics-remove-input-{token}"
    same_name_legacy_name = f"native-same-name-legacy-{token}"
    same_name_input_name = f"native-same-name-input-{token}"
    unload_legacy_name = f"native-unload-legacy-{token}"
    unload_input_name = f"native-unload-input-{token}"
    atlas_path = scratch / "solar-band-atlas.png"
    coverage_atlas_path = scratch / "coverage-alpha-atlas.png"
    coverage_atlas_update_path = scratch / "coverage-alpha-atlas-updated.png"
    prism_atlas_path = scratch / "prism-solar-coverage-atlas.png"
    prism_atlas_update_path = scratch / "prism-solar-coverage-atlas-updated.png"
    make_atlas_fixture(atlas_path)
    make_coverage_atlas_fixture(coverage_atlas_path)
    make_updated_coverage_atlas_fixture(coverage_atlas_update_path)
    generation = 1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    async with websockets.connect(
        f"ws://127.0.0.1:{config['server_port']}",
        subprotocols=["obswebsocket.json"],
    ) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion": hello["rpcVersion"], "eventSubscriptions": 0}
        if "authentication" in hello:
            auth = hello["authentication"]
            identify["authentication"] = protocol.compute_auth(
                config["server_password"], auth["salt"], auth["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify}))
        await ws.recv()
        inbox = protocol.Inbox()

        async def call(kind: str, data: dict | None = None) -> dict:
            response = await protocol.request(inbox, ws, kind, uuid.uuid4().hex, data)
            status = response.get("requestStatus", {})
            if not status.get("result"):
                raise RuntimeError(f"{kind} failed: {status}")
            return response.get("responseData", {})

        video_settings = await call("GetVideoSettings")
        viewport = (int(video_settings["baseWidth"]), int(video_settings["baseHeight"]))
        assert all(dimension > 0 for dimension in viewport), video_settings

        readback_images: list[dict[str, str]] = []

        async def pixels(phase: str, source_name: str = scene,
                         size: tuple[int, int] = (64, 64)) -> Image.Image:
            result = await call("GetSourceScreenshot", {
                "sourceName": source_name, "imageFormat": "png",
                "imageWidth": size[0], "imageHeight": size[1],
            })
            data = base64.b64decode(result["imageData"].split(",", 1)[1])
            image_path = output / f"{stamp}-{token}-group-{phase}.png"
            image_path.write_bytes(data)
            readback_images.append({"phase": phase, "path": image_path.name,
                                    "sha256": hashlib.sha256(data).hexdigest()})
            return Image.open(image_path).convert("RGBA")

        async def prepare_prism_wire(generation_value: int) -> dict:
            if prism_wire_emitter is None:
                raise RuntimeError("Prism wire emitter was not configured")
            make_prism_wire_coverage_atlas_fixture(prism_atlas_path)
            make_prism_wire_coverage_atlas_fixture(
                prism_atlas_update_path, updated=True)
            await call("SetInputSettings", {
                "inputName": coverage_atlas_name,
                "inputSettings": {"file": str(prism_atlas_path)},
                "overlay": True,
            })
            await asyncio.sleep(0.5)
            atlas_readback = await pixels(
                "prism-wire-solar-coverage-atlas-source", coverage_atlas_name,
                (1922, 3246))
            assert atlas_readback.size == (1922, 3246), atlas_readback.size
            atlas_samples = {
                "solarBand": list(atlas_readback.getpixel((51, 51))),
                "groupMaskTransparent": atlas_readback.getpixel((6, 1183))[3],
                "groupMaskPartial": atlas_readback.getpixel((16, 1183))[3],
                "groupMaskOpaque": atlas_readback.getpixel((26, 1183))[3],
                "captureMaskTransparent": atlas_readback.getpixel((26, 2215))[3],
                "captureMaskPartial": atlas_readback.getpixel((51, 2215))[3],
                "captureMaskOpaque": atlas_readback.getpixel((61, 2215))[3],
            }
            assert atlas_samples["solarBand"] == [80, 160, 220, 255], atlas_samples
            assert atlas_samples["groupMaskTransparent"] == 0, atlas_samples
            assert atlas_samples["groupMaskPartial"] in (127, 128), atlas_samples
            assert atlas_samples["groupMaskOpaque"] == 255, atlas_samples
            assert atlas_samples["captureMaskTransparent"] == 0, atlas_samples
            assert atlas_samples["captureMaskPartial"] in (127, 128), atlas_samples
            assert atlas_samples["captureMaskOpaque"] == 255, atlas_samples
            baseline = await pixels(
                "prism-wire-scene-prefix-before-commit", coverage_scene, viewport)
            assert baseline.tobytes() == coverage_before_bytes, {
                "reason": "switching the hidden atlas changed the scene prefix",
            }
            emitter_output = output / f"{stamp}-{token}-prism-wire.json"
            emitter_record = emit_prism_wire(
                prism_wire_emitter, emitter_output, scratch, {
                    "cefItemId": coverage_ids["atlas"],
                    "captureItemId": coverage_ids["captureBinding"],
                    "contributionItemId": coverage_ids["capture"],
                    "sceneName": coverage_scene,
                    "runtimeInstanceId": runtime_id,
                    "generation": generation_value,
                })
            wire = emitter_record["wire"]
            assert wire["groups"][0]["bounds"] == {
                "x": 0, "y": 0, "width": viewport[0], "height": viewport[1]}, wire
            assert wire["groups"][0]["opacity"] == 0.5 and \
                wire["groups"][0]["blendMode"] == "multiply", wire["groups"][0]
            assert wire["groups"][0]["mask"]["crop"] == {
                "left": 1, "top": 1083, "right": 1, "bottom": 1083}, wire
            band = next(child for child in wire["groups"][0]["children"]
                        if child["kind"] == "solarBand")
            capture = next(child for child in wire["groups"][0]["children"]
                           if child["kind"] == "capture")
            assert band["crop"] == {
                "left": 1, "top": 1, "right": 1, "bottom": 2165}, band
            assert capture["affine"] == {
                "m00": 2, "m01": 0, "tx": 20,
                "m10": 0, "m11": 2, "ty": 40}, capture
            return {
                **emitter_record,
                "atlasFixture": {
                    "path": str(prism_atlas_path),
                    "sha256": sha256(prism_atlas_path),
                    "updatedPath": str(prism_atlas_update_path),
                    "updatedSha256": sha256(prism_atlas_update_path),
                    "dimensions": [1922, 3246],
                    "readbackDimensions": list(atlas_readback.size),
                    "readbackSamples": atlas_samples,
                },
                "scenePrefixBeforeCommitSha256": hashlib.sha256(
                    baseline.tobytes()).hexdigest(),
                "captureBindingAndContributionAreDistinctSceneItems": True,
                "rootBackdropPrefixSource": coverage_background_name,
            }

        async def wait_for_restoration(scene_name: str, legacy_id: int,
                                       expected_pixel: tuple[int, int, int, int],
                                       phase: str) -> dict:
            deadline = time.monotonic() + 8
            attempts = 0
            last_pixel = None
            last_items = None
            while time.monotonic() < deadline:
                attempts += 1
                image = await pixels(f"{phase}-{attempts}", scene_name, (64, 64))
                last_pixel = image.getpixel((32, 32))
                last_items = (await call("GetSceneItemList", {
                    "sceneName": scene_name,
                }))["sceneItems"]
                legacy_state = await call("GetSceneItemEnabled", {
                    "sceneName": scene_name, "sceneItemId": legacy_id,
                })
                item_ids = sorted(int(item["sceneItemId"]) for item in last_items)
                if (last_pixel == expected_pixel and item_ids == [legacy_id] and
                        legacy_state["sceneItemEnabled"] is True):
                    return {
                        "attempts": attempts, "pixel": list(last_pixel),
                        "remainingSceneItemIds": item_ids,
                        "legacyVisible": legacy_state["sceneItemEnabled"],
                    }
                await asyncio.sleep(0.1)
            raise AssertionError({
                "phase": phase, "restoredPixel": last_pixel,
                "expectedPixel": expected_pixel,
                "remainingSceneItemIds": [item.get("sceneItemId") for item in last_items or []],
            })

        committed = False
        offset_committed = False
        solar_committed = False
        mixed_committed = False
        coverage_committed = False
        coverage_generation = 1
        lifecycle_committed = False
        lifecycle_generation = 1
        same_name_committed = False
        same_name_generation = 3
        unload_active = False
        try:
            await call("CreateScene", {"sceneName": scene})
            await call("CreateScene", {"sceneName": appearance_scene})
            await call("CreateInput", {
                "sceneName": scene, "inputName": red_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFFFF0000, "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": True,
            })
            await call("CreateInput", {
                "sceneName": appearance_scene, "inputName": blue_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF0000FF, "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": True,
            })
            wrapper_item = await call("CreateSceneItem", {
                "sceneName": scene, "sourceName": appearance_scene,
                "sceneItemEnabled": False,
            })
            source_red = await pixels("source-red", red_name)
            source_blue = await pixels("source-blue", blue_name)
            source_red_center = source_red.getpixel((32, 32))
            source_blue_center = source_blue.getpixel((32, 32))
            assert source_red_center[3] == 255, source_red_center
            assert source_blue_center[3] == 255, source_blue_center
            red_id = (await call("GetSceneItemId", {
                "sceneName": scene, "sourceName": red_name,
            }))["sceneItemId"]
            blue_id = wrapper_item["sceneItemId"]
            payload = plan(scene, runtime_id, generation, red_id, blue_id,
                           viewport=viewport)
            result = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": payload,
            })
            assert result["responseData"].get("ok"), result
            committed = True
            await asyncio.sleep(0.25)
            before = await pixels("before-reset")
            center = before.getpixel((32, 32))
            assert center[3] in (127, 128), {"center": center, "expectedAlpha": [127, 128]}
            assert_opacity_rgb(center, source_blue_center, 0.5, "initial")

            malformed = plan(scene, runtime_id, generation + 1, red_id, blue_id,
                             old_array_affine=True, viewport=viewport)
            refused = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": malformed,
            })
            refusal = refused["responseData"]
            assert not refusal.get("ok") and str(refusal.get("error", "")).startswith("invalid-affine"), refused
            after_refusal = await pixels("after-refusal")
            assert after_refusal.getpixel((32, 32)) == center, {
                "before": center, "afterMalformedRefusal": after_refusal.getpixel((32, 32))}

            unsafe_generation_plan = plan(
                scene, runtime_id, 9007199254740992, red_id, blue_id,
                viewport=viewport)
            unsafe_generation = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition",
                "requestData": unsafe_generation_plan,
            })
            assert not unsafe_generation["responseData"].get("ok"), unsafe_generation
            after_unsafe_generation = await pixels("after-unsafe-generation-refusal")
            assert after_unsafe_generation.getpixel((32, 32)) == center, {
                "before": center,
                "afterUnsafeGeneration": after_unsafe_generation.getpixel((32, 32))}

            unsafe_id_plan = plan(scene, runtime_id, generation + 1,
                                  9007199254740992, blue_id,
                                  viewport=viewport)
            unsafe_id = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": unsafe_id_plan,
            })
            assert not unsafe_id["responseData"].get("ok"), unsafe_id
            after_unsafe_id = await pixels("after-unsafe-scene-item-refusal")
            assert after_unsafe_id.getpixel((32, 32)) == center, {
                "before": center,
                "afterUnsafeSceneItemId": after_unsafe_id.getpixel((32, 32))}

            updated_generation = generation + 1
            updated_plan = plan(scene, runtime_id, updated_generation, red_id,
                                blue_id, group_opacity=0.75, viewport=viewport,
                                reverse_children=True)
            updated = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": updated_plan,
            })
            assert updated["responseData"].get("ok"), updated
            generation = updated_generation
            await asyncio.sleep(0.25)
            after_update = await pixels("after-update")
            updated_center = after_update.getpixel((32, 32))
            assert updated_center[3] in (191, 192), {
                "updatedCenter": updated_center, "expectedGroupOpacityAlpha": [191, 192]}
            assert_opacity_rgb(updated_center, source_red_center, 0.75, "successive")

            reset = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "ResetGroupComposition",
                "requestData": {"sceneName": scene, "generation": generation,
                                "runtimeInstanceId": runtime_id},
            })
            assert reset["responseData"].get("ok") and reset["responseData"].get("reset"), reset
            committed = False
            await asyncio.sleep(0.15)
            restored = await pixels("after-reset")
            restored_center = restored.getpixel((32, 32))
            assert restored_center[3] == 255, {
                "restoredCenter": restored_center, "expectedAlpha": 255}

            await call("CreateScene", {"sceneName": offset_scene})
            await call("CreateInput", {
                "sceneName": offset_scene, "inputName": offset_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFFFF0000, "width": 64, "height": 64},
                "sceneItemEnabled": False,
            })
            offset_id = (await call("GetSceneItemId", {
                "sceneName": offset_scene, "sourceName": offset_name,
            }))["sceneItemId"]
            offset_source = await pixels("offset-source", offset_name)
            offset_source_center = offset_source.getpixel((32, 32))
            offset_plan = translated_capture_plan(
                offset_scene, runtime_id, 1, offset_id, viewport,
                x=256, opacity=0.5)
            offset_result = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": offset_plan,
            })
            assert offset_result["responseData"].get("ok"), offset_result
            offset_committed = True
            await asyncio.sleep(0.25)
            offset_image = await pixels("translated-local-opacity", offset_scene, viewport)
            translated_pixel = offset_image.getpixel((256 + 32, 32))
            assert translated_pixel[3] in (127, 128), {
                "translatedPixel": translated_pixel, "expectedAlpha": [127, 128],
                "sourcePixel": offset_source_center}
            assert_opacity_rgb(translated_pixel, offset_source_center, 0.5,
                               "translated localOpacity")
            offset_reset = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "ResetGroupComposition",
                "requestData": {"sceneName": offset_scene, "generation": 1,
                                "runtimeInstanceId": runtime_id},
            })
            assert offset_reset["responseData"].get("reset"), offset_reset
            offset_committed = False
            offset_item_state = await call("GetSceneItemEnabled", {
                "sceneName": offset_scene, "sceneItemId": offset_id,
            })
            assert offset_item_state["sceneItemEnabled"] is False, offset_item_state

            await call("CreateScene", {"sceneName": solar_scene})
            legacy_item = await call("CreateInput", {
                "sceneName": solar_scene, "inputName": legacy_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF5A20D0,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": True,
            })
            legacy_id = legacy_item["sceneItemId"]
            legacy_state_before = await call("GetSceneItemEnabled", {
                "sceneName": solar_scene, "sceneItemId": legacy_id,
            })
            assert legacy_state_before["sceneItemEnabled"] is True, legacy_state_before
            legacy_before_image = await pixels(
                "solar-legacy-before-commit", solar_scene, viewport)
            legacy_before_red = legacy_before_image.getpixel((260, 150))
            legacy_before_green = legacy_before_image.getpixel((360, 150))

            atlas_item = await call("CreateInput", {
                "sceneName": solar_scene, "inputName": atlas_name,
                "inputKind": "image_source", "inputSettings": {"file": str(atlas_path)},
                "sceneItemEnabled": False,
            })
            atlas_id = atlas_item["sceneItemId"]
            atlas_enabled_before = await call("GetSceneItemEnabled", {
                "sceneName": solar_scene, "sceneItemId": atlas_id,
            })
            assert atlas_enabled_before["sceneItemEnabled"] is False, atlas_enabled_before
            atlas_image = await pixels("solar-atlas-source", atlas_name, viewport)
            atlas_red = atlas_image.getpixel((730, 380))
            atlas_green = atlas_image.getpixel((930, 380))
            assert_pixel_near(atlas_red, (255, 0, 0, 255), "atlas red source")
            assert_pixel_near(atlas_green, (0, 255, 0, 255), "atlas green source")

            atlas_bands = [
                {"band": 17,
                 "crop": {"left": 700, "top": 350, "right": 1160, "bottom": 670},
                 "rect": {"x": 30, "y": 20, "width": 60, "height": 60}},
                {"band": 18,
                 "crop": {"left": 900, "top": 350, "right": 960, "bottom": 670},
                 "rect": {"x": 130, "y": 20, "width": 60, "height": 60}},
            ]
            band_payload = solar_band_plan(
                solar_scene, runtime_id, 1, atlas_id, viewport, atlas_bands,
                replaced_scene_items=[legacy_id])
            band_result = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": band_payload,
            })
            assert band_result["responseData"].get("ok"), band_result
            solar_committed = True
            replaced_after_commit = await call("GetSceneItemEnabled", {
                "sceneName": solar_scene, "sceneItemId": legacy_id,
            })
            assert replaced_after_commit["sceneItemEnabled"] is False, replaced_after_commit
            await asyncio.sleep(0.25)
            band_image = await pixels("solar-band-committed", solar_scene, viewport)
            band_red_point = (260, 150)
            band_green_point = (360, 150)
            band_red = band_image.getpixel(band_red_point)
            band_green = band_image.getpixel(band_green_point)
            assert_pixel_near(band_red, atlas_red, "cropped first band at owner-local rect")
            assert_pixel_near(band_green, atlas_green, "same source second crop at owner-local rect")
            assert band_image.getpixel((229, 119))[3] == 0, {
                "outsideBand": band_image.getpixel((229, 119)),
                "legacyBackgroundBeforeCommit": legacy_before_image.getpixel((229, 119)),
                "expected": "replaced legacy item absent outside the solar bands"}

            idempotent_repeat = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": band_payload,
            })
            assert idempotent_repeat["responseData"].get("ok") and idempotent_repeat[
                "responseData"].get("idempotent"), idempotent_repeat

            changed_set_same_hash_payload = solar_band_plan(
                solar_scene, runtime_id, 1, atlas_id, viewport, atlas_bands,
                replaced_scene_items=[legacy_id, 9007199254740989])
            changed_set_same_hash_payload["planHash"] = band_payload["planHash"]
            changed_set_same_hash = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition",
                "requestData": changed_set_same_hash_payload,
            })
            assert not changed_set_same_hash["responseData"].get("ok") and str(
                changed_set_same_hash["responseData"].get("error", "")).startswith(
                    "stale-generation"), changed_set_same_hash
            after_changed_set_same_hash = await pixels(
                "solar-band-after-replacement-set-mismatch", solar_scene, viewport)
            assert after_changed_set_same_hash.getpixel(band_red_point) == band_red
            assert after_changed_set_same_hash.getpixel(band_green_point) == band_green

            changed_hash_payload = solar_band_plan(
                solar_scene, runtime_id, 1, atlas_id, viewport, atlas_bands,
                replaced_scene_items=[legacy_id, 9007199254740989])
            assert changed_hash_payload["planHash"] != band_payload["planHash"]
            changed_hash = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": changed_hash_payload,
            })
            assert not changed_hash["responseData"].get("ok") and str(
                changed_hash["responseData"].get("error", "")).startswith(
                    "stale-generation"), changed_hash
            after_changed_hash = await pixels(
                "solar-band-after-plan-hash-mismatch", solar_scene, viewport)
            assert after_changed_hash.getpixel(band_red_point) == band_red
            assert after_changed_hash.getpixel(band_green_point) == band_green

            invalid_crop_bands = [dict(band) for band in atlas_bands]
            invalid_crop_bands[0] = {
                **invalid_crop_bands[0],
                "crop": {"left": 1920, "top": 0, "right": 0, "bottom": 0},
            }
            invalid_crop_plan = solar_band_plan(
                solar_scene, runtime_id, 2, atlas_id, viewport, invalid_crop_bands)
            invalid_crop = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": invalid_crop_plan,
            })
            assert not invalid_crop["responseData"].get("ok") and str(
                invalid_crop["responseData"].get("error", "")).startswith("invalid-crop"), invalid_crop
            after_invalid_crop = await pixels("solar-band-after-invalid-crop", solar_scene, viewport)
            assert after_invalid_crop.getpixel(band_red_point) == band_red
            assert after_invalid_crop.getpixel(band_green_point) == band_green

            invalid_rect_bands = [dict(band) for band in atlas_bands]
            invalid_rect_bands[0] = {
                **invalid_rect_bands[0],
                "rect": {"x": viewport[0] - 30, "y": 20,
                         "width": 60, "height": 60},
            }
            invalid_rect_plan = solar_band_plan(
                solar_scene, runtime_id, 2, atlas_id, viewport, invalid_rect_bands)
            invalid_rect = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": invalid_rect_plan,
            })
            assert not invalid_rect["responseData"].get("ok") and str(
                invalid_rect["responseData"].get("error", "")).startswith("invalid-band-rect"), invalid_rect
            after_invalid_rect = await pixels("solar-band-after-invalid-rect", solar_scene, viewport)
            assert after_invalid_rect.getpixel(band_red_point) == band_red
            assert after_invalid_rect.getpixel(band_green_point) == band_green

            duplicate_band_specs = [dict(band) for band in atlas_bands]
            duplicate_band_specs[1] = {**duplicate_band_specs[1], "band": 17}
            duplicate_band_plan = solar_band_plan(
                solar_scene, runtime_id, 2, atlas_id, viewport, duplicate_band_specs)
            duplicate_band = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": duplicate_band_plan,
            })
            assert not duplicate_band["responseData"].get("ok") and str(
                duplicate_band["responseData"].get("error", "")).startswith("duplicate-solar-band"), duplicate_band
            after_duplicate_band = await pixels("solar-band-after-duplicate-band", solar_scene, viewport)
            assert after_duplicate_band.getpixel(band_red_point) == band_red
            assert after_duplicate_band.getpixel(band_green_point) == band_green

            unsupported_mask_plan = solar_band_plan(
                solar_scene, runtime_id, 2, atlas_id, viewport, atlas_bands,
                owner_mask={"kind": "coverage", "mode": "alpha",
                            "source": {"kind": "solarBand", "band": 17}})
            unsupported_mask = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": unsupported_mask_plan,
            })
            assert not unsupported_mask["responseData"].get("ok") and str(
                unsupported_mask["responseData"].get("error", "")).startswith("unsupported-mask"), unsupported_mask
            after_unsupported_mask = await pixels("solar-band-after-unsupported-mask", solar_scene, viewport)
            assert after_unsupported_mask.getpixel(band_red_point) == band_red
            assert after_unsupported_mask.getpixel(band_green_point) == band_green

            band_reset = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "ResetGroupComposition",
                "requestData": {"sceneName": solar_scene, "generation": 1,
                                "runtimeInstanceId": runtime_id},
            })
            assert band_reset["responseData"].get("reset"), band_reset
            solar_committed = False
            await asyncio.sleep(0.15)
            band_after_reset = await pixels("solar-band-after-reset", solar_scene, viewport)
            assert band_after_reset.getpixel(band_red_point) == legacy_before_red
            assert band_after_reset.getpixel(band_green_point) == legacy_before_green
            atlas_enabled_after = await call("GetSceneItemEnabled", {
                "sceneName": solar_scene, "sceneItemId": atlas_id,
            })
            assert atlas_enabled_after["sceneItemEnabled"] is False, atlas_enabled_after
            legacy_enabled_after = await call("GetSceneItemEnabled", {
                "sceneName": solar_scene, "sceneItemId": legacy_id,
            })
            assert legacy_enabled_after["sceneItemEnabled"] is True, legacy_enabled_after

            stale_band = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": band_payload,
            })
            assert not stale_band["responseData"].get("ok") and str(
                stale_band["responseData"].get("error", "")).startswith("stale-generation"), stale_band
            stale_band_pixels = await pixels("solar-band-after-stale-generation", solar_scene, viewport)
            assert stale_band_pixels.getpixel(band_red_point) == legacy_before_red
            assert stale_band_pixels.getpixel(band_green_point) == legacy_before_green

            await call("CreateScene", {"sceneName": mixed_scene})
            mixed_legacy = await call("CreateInput", {
                "sceneName": mixed_scene, "inputName": mixed_legacy_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF2A5AD0,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": True,
            })
            mixed_legacy_id = mixed_legacy["sceneItemId"]
            mixed_atlas_item = await call("CreateInput", {
                "sceneName": mixed_scene, "inputName": mixed_atlas_name,
                "inputKind": "image_source",
                "inputSettings": {"file": str(atlas_path)},
                "sceneItemEnabled": False,
            })
            mixed_atlas_id = mixed_atlas_item["sceneItemId"]
            mixed_capture_item = await call("CreateInput", {
                "sceneName": mixed_scene, "inputName": mixed_capture_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF00FF00, "width": 64, "height": 64},
                "sceneItemEnabled": False,
            })
            mixed_capture_id = mixed_capture_item["sceneItemId"]
            nested_capture_item = await call("CreateInput", {
                "sceneName": mixed_scene, "inputName": nested_capture_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFFFF8000, "width": 16, "height": 16},
                "sceneItemEnabled": False,
            })
            nested_capture_id = nested_capture_item["sceneItemId"]
            mixed_legacy_before = await pixels(
                "mixed-legacy-before", mixed_scene, viewport)
            mixed_legacy_pixel = mixed_legacy_before.getpixel((260, 150))
            mixed_atlas_before = await pixels(
                "mixed-atlas-source", mixed_atlas_name, (1920, 1080))
            mixed_atlas_red = mixed_atlas_before.getpixel((730, 380))
            mixed_atlas_green = mixed_atlas_before.getpixel((930, 380))
            mixed_capture_before = await pixels(
                "mixed-capture-source", mixed_capture_name, (64, 64))
            mixed_capture_pixel = mixed_capture_before.getpixel((32, 32))
            nested_capture_before = await pixels(
                "nested-capture-source", nested_capture_name, (16, 16))
            nested_capture_pixel = nested_capture_before.getpixel((8, 8))
            assert mixed_capture_pixel[3] == 255, mixed_capture_pixel
            assert nested_capture_pixel[3] == 255, nested_capture_pixel

            mixed_bands = [
                {"kind": "solarBand", "band": 31, "sceneItemId": mixed_atlas_id,
                 "crop": {"left": 700, "top": 350, "right": 1160, "bottom": 670},
                 "rect": {"x": 30, "y": 20, "width": 60, "height": 60},
                 "affine": affine()},
                {"kind": "capture", "sceneItemId": mixed_capture_id,
                 "affine": affine(30, 20),
                 "crop": {"left": 0, "top": 0, "right": 4, "bottom": 4},
                 "localOpacity": 1, "visible": True},
                {"kind": "solarBand", "band": 32, "sceneItemId": mixed_atlas_id,
                 "crop": {"left": 900, "top": 350, "right": 960, "bottom": 670},
                 "rect": {"x": 130, "y": 20, "width": 60, "height": 60},
                 "affine": affine()},
                {"kind": "capture", "sceneItemId": mixed_capture_id,
                 "affine": affine(300, 150),
                 "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                 "localOpacity": 0.25, "visible": True},
                {"kind": "capture", "sceneItemId": mixed_capture_id,
                 "affine": affine(400, 150),
                 "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                 "localOpacity": 0.75, "visible": True},
            ]
            rotated_group = {
                "id": "mixed-rotated-owner", "opacity": 1, "visible": True,
                "blendMode": "normal",
                "bounds": {"x": 0, "y": 0, "width": 64, "height": 64},
                "affine": {"m00": 0, "m01": -1, "tx": 600,
                           "m10": 1, "m11": 0, "ty": 250},
                "children": [{
                    "kind": "capture", "sceneItemId": nested_capture_id,
                    "affine": affine(20, 20),
                    "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                    "localOpacity": 1, "visible": True,
                }],
                "mask": None,
            }
            mixed_payload = mixed_composition_plan(
                mixed_scene, runtime_id, 1, viewport, mixed_bands,
                affine(200, 100), 0.5,
                replaced_scene_items=[mixed_legacy_id, mixed_capture_id,
                                      nested_capture_id],
                nested_group=rotated_group)
            mixed_result = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": mixed_payload,
            })
            assert mixed_result["responseData"].get("ok"), mixed_result
            mixed_committed = True
            await asyncio.sleep(0.3)
            mixed_image = await pixels("mixed-composition-committed",
                                       mixed_scene, viewport)
            mixed_overlap_point = (260, 150)
            mixed_overlap = mixed_image.getpixel(mixed_overlap_point)
            assert mixed_overlap[3] in (127, 128), {
                "mixedOverlap": mixed_overlap, "expectedAlpha": [127, 128],
                "reason": "opaque band and opaque capture are combined before group opacity"}
            assert_opacity_rgb(mixed_overlap, mixed_capture_pixel, 0.5,
                               "capture painted after overlapped band")
            low_opacity_point = (200 + 300 + 32, 100 + 150 + 32)
            high_opacity_point = (200 + 400 + 32, 100 + 150 + 32)
            low_opacity_pixel = mixed_image.getpixel(low_opacity_point)
            high_opacity_pixel = mixed_image.getpixel(high_opacity_point)
            assert low_opacity_pixel[3] in (31, 32), {
                "lowOpacityRepeatedInput": low_opacity_pixel,
                "expectedAlpha": [31, 32]}
            assert high_opacity_pixel[3] in (95, 96), {
                "highOpacityRepeatedInput": high_opacity_pixel,
                "expectedAlpha": [95, 96]}
            assert_opacity_rgb(low_opacity_pixel, mixed_capture_pixel, 0.125,
                               "first repeated source occurrence")
            assert_opacity_rgb(high_opacity_pixel, mixed_capture_pixel, 0.375,
                               "second repeated source occurrence")
            nested_group_rgb = tuple(round(channel * 0.5)
                                     for channel in nested_capture_pixel[:3])
            rotated_bounds = []
            for y in range(300, 520):
                for x in range(600, 900):
                    pixel = mixed_image.getpixel((x, y))
                    if pixel[3] in (127, 128) and all(
                            abs(value - expected) <= 1
                            for value, expected in zip(pixel[:3], nested_group_rgb)):
                        rotated_bounds.append((x, y))
            assert rotated_bounds, {
                "nestedRotatedCapture": "no pixels found",
                "sourcePixel": nested_capture_pixel,
            }
            rotated_bbox = (
                min(point[0] for point in rotated_bounds),
                min(point[1] for point in rotated_bounds),
                max(point[0] for point in rotated_bounds) + 1,
                max(point[1] for point in rotated_bounds) + 1,
            )
            assert rotated_bbox == (764, 370, 780, 386), {
                "nestedRotatedBounds": rotated_bbox,
                "expected": [764, 370, 780, 386],
            }
            assert all(mixed_image.getpixel(point)[3] in (127, 128)
                       for point in rotated_bounds)
            mixed_reset = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "ResetGroupComposition",
                "requestData": {"sceneName": mixed_scene, "generation": 1,
                                "runtimeInstanceId": runtime_id},
            })
            assert mixed_reset["responseData"].get("reset"), mixed_reset
            mixed_committed = False
            await asyncio.sleep(0.15)
            mixed_after_reset = await pixels("mixed-composition-after-reset",
                                             mixed_scene, viewport)
            assert mixed_after_reset.getpixel((260, 150)) == mixed_legacy_pixel
            assert (await call("GetSceneItemEnabled", {
                "sceneName": mixed_scene, "sceneItemId": mixed_legacy_id,
            }))["sceneItemEnabled"] is True
            assert (await call("GetSceneItemEnabled", {
                "sceneName": mixed_scene, "sceneItemId": mixed_capture_id,
            }))["sceneItemEnabled"] is False
            assert (await call("GetSceneItemEnabled", {
                "sceneName": mixed_scene, "sceneItemId": nested_capture_id,
            }))["sceneItemEnabled"] is False

            await call("CreateScene", {"sceneName": coverage_scene})
            coverage_backdrop_item = await call("CreateInput", {
                "sceneName": coverage_scene,
                "inputName": coverage_backdrop_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF102030,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": True,
            })
            coverage_background_item = await call("CreateInput", {
                "sceneName": coverage_scene,
                "inputName": coverage_background_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF203040,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": True,
            })
            coverage_owner_background_item = await call("CreateInput", {
                "sceneName": coverage_scene,
                "inputName": coverage_owner_background_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF304050,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": False,
            })
            coverage_red_item = await call("CreateInput", {
                "sceneName": coverage_scene, "inputName": coverage_red_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFFFF2020,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": False,
            })
            coverage_green_item = await call("CreateInput", {
                "sceneName": coverage_scene, "inputName": coverage_green_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF20E060,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": False,
            })
            coverage_capture_item = await call("CreateInput", {
                "sceneName": coverage_scene, "inputName": coverage_capture_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFFE02090, "width": 24, "height": 8},
                "sceneItemEnabled": False,
            })
            coverage_capture_binding_item = await call("CreateInput", {
                "sceneName": coverage_scene,
                "inputName": coverage_capture_binding_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF50B090,
                                  "width": 24, "height": 8},
                "sceneItemEnabled": False,
            })
            coverage_blend_source_item = await call("CreateInput", {
                "sceneName": coverage_scene,
                "inputName": coverage_blend_source_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF40C0E0, "width": 8, "height": 8},
                "sceneItemEnabled": False,
            })
            coverage_atlas_item = await call("CreateInput", {
                "sceneName": coverage_scene, "inputName": coverage_atlas_name,
                "inputKind": "image_source",
                "inputSettings": {"file": str(coverage_atlas_path)},
                "sceneItemEnabled": False,
            })
            coverage_ids = {
                "backdrop": int(coverage_backdrop_item["sceneItemId"]),
                "background": int(coverage_background_item["sceneItemId"]),
                "ownerBackground": int(coverage_owner_background_item["sceneItemId"]),
                "red": int(coverage_red_item["sceneItemId"]),
                "green": int(coverage_green_item["sceneItemId"]),
                "capture": int(coverage_capture_item["sceneItemId"]),
                "captureBinding": int(
                    coverage_capture_binding_item["sceneItemId"]),
                "blendSource": int(coverage_blend_source_item["sceneItemId"]),
                "atlas": int(coverage_atlas_item["sceneItemId"]),
            }
            coverage_replaced_ids = [
                scene_item_id for key, scene_item_id in coverage_ids.items()
                if key != "backdrop"]
            coverage_original_scene_item_ids = sorted(
                int(item["sceneItemId"]) for item in
                (await call("GetSceneItemList", {
                    "sceneName": coverage_scene,
                }))["sceneItems"])
            coverage_backdrop_image = await pixels(
                "coverage-external-backdrop-source", coverage_backdrop_name,
                (8, 8))
            coverage_backdrop_pixel = coverage_backdrop_image.getpixel((4, 4))
            coverage_background_before = await pixels(
                "coverage-background-before", coverage_scene, viewport)
            coverage_before_bytes = coverage_background_before.tobytes()
            owner_background_image = await pixels(
                "coverage-owner-background-source", coverage_owner_background_name,
                (8, 8))
            coverage_content_image = await pixels(
                "coverage-content-source", coverage_background_name, (8, 8))
            green_source_image = await pixels(
                "coverage-green-source", coverage_green_name, (8, 8))
            capture_source_image = await pixels(
                "coverage-capture-source", coverage_capture_name, (24, 8))
            blend_source_image = await pixels(
                "coverage-blend-source", coverage_blend_source_name, (8, 8))
            mask_source_image = await pixels(
                "coverage-alpha-atlas-source", coverage_atlas_name, (28, 10))
            owner_background_pixel = owner_background_image.getpixel((4, 4))
            coverage_content_pixel = coverage_content_image.getpixel((4, 4))
            green_pixel = green_source_image.getpixel((4, 4))
            masked_capture_pixel = capture_source_image.getpixel((12, 4))
            blend_source_pixel = blend_source_image.getpixel((4, 4))
            assert mask_source_image.getpixel((6, 5))[3] == 0
            assert mask_source_image.getpixel((14, 5))[3] in (127, 128)
            assert mask_source_image.getpixel((22, 5))[3] == 255

            zero_crop = {"left": 0, "top": 0, "right": 0, "bottom": 0}
            mask_crop = {"left": 2, "top": 1, "right": 2, "bottom": 1}
            group_mask = {
                "sceneItemId": coverage_ids["atlas"], "crop": mask_crop,
                "rect": {"x": 100, "y": 100, "width": 24, "height": 8},
            }
            child_mask = {
                "sceneItemId": coverage_ids["atlas"], "crop": mask_crop,
                "rect": {"x": 300, "y": 100, "width": 24, "height": 8},
            }

            def v2_capture(scene_item_id, x=0, y=0, local_opacity=1.0,
                           blend_mode="normal", mask=None):
                return {
                    "kind": "capture", "sceneItemId": scene_item_id,
                    "affine": affine(x, y), "crop": zero_crop,
                    "localOpacity": local_opacity, "visible": True,
                    "blendMode": blend_mode,
                    "mask": json.loads(json.dumps(mask)) if mask else None,
                }

            root_children = [v2_capture(coverage_ids["background"]),
                             {"kind": "group", "id": "coverage-owner"}]
            group_definitions = []
            group_definitions.append({
                "id": ROOT_GROUP_ID, "parentId": None, "order": 0,
                "opacity": 1, "visible": True, "blendMode": "normal",
                "bounds": {"x": 0, "y": 0, "width": viewport[0],
                           "height": viewport[1]},
                "affine": affine(), "children": root_children, "mask": None,
            })
            owner_children = [
                v2_capture(coverage_ids["ownerBackground"]),
                {"kind": "group", "id": "coverage-mask-group"},
                v2_capture(coverage_ids["capture"], x=300, y=100,
                           local_opacity=0.5, blend_mode="multiply",
                           mask=child_mask),
            ]
            group_definitions.append({
                "id": "coverage-owner", "parentId": ROOT_GROUP_ID,
                "order": 1, "opacity": 1, "visible": True,
                "blendMode": "normal",
                "bounds": {"x": 0, "y": 0, "width": viewport[0],
                           "height": viewport[1]},
                "affine": affine(), "children": owner_children, "mask": None,
            })
            group_definitions.append({
                "id": "coverage-mask-group", "parentId": "coverage-owner",
                "order": 1, "opacity": 0.5, "visible": True,
                "blendMode": "normal",
                "bounds": {"x": 0, "y": 0, "width": viewport[0],
                           "height": viewport[1]},
                "affine": affine(),
                "children": [v2_capture(coverage_ids["red"]),
                             v2_capture(coverage_ids["green"])],
                "mask": group_mask,
            })
            blend_tiles = []
            blend_tile_y = 200
            blend_tile_start_x = 600
            for mode_index, (mode_name, mode_id) in enumerate(CSS_BLEND_MODES):
                group_id = f"coverage-blend-{mode_name}"
                root_children.append({"kind": "group", "id": group_id})
                group_definitions.append({
                    "id": group_id, "parentId": ROOT_GROUP_ID,
                    "order": 2 + mode_index, "opacity": 1, "visible": True,
                    "blendMode": mode_name,
                    "bounds": {"x": 0, "y": 0, "width": viewport[0],
                               "height": viewport[1]},
                    "affine": affine(),
                    "children": [v2_capture(
                        coverage_ids["blendSource"],
                        x=blend_tile_start_x + mode_index * 12,
                        y=blend_tile_y)],
                    "mask": None,
                })
                blend_tiles.append({
                    "name": mode_name, "id": mode_id,
                    "point": [blend_tile_start_x + mode_index * 12 + 4,
                              blend_tile_y + 4],
                })
            v2_payload = make_global_v2_plan(
                coverage_scene, runtime_id, 1, group_definitions, viewport,
                coverage_replaced_ids)
            v2_result = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition",
                "requestData": v2_payload,
            })
            if expect_v2_rejection:
                v2_outcome = v2_result.get("responseData", {})
                assert not v2_outcome.get("ok") and str(
                    v2_outcome.get("error", "")).startswith("unsupported-protocol"), v2_result
                v2_rejected_pixels = await pixels(
                    "coverage-v2-legacy-plugin-after-rejection",
                    coverage_scene, viewport)
                assert v2_rejected_pixels.tobytes() == coverage_before_bytes, {
                    "reason": "legacy plugin refusal changed scene pixels",
                    "error": v2_result,
                }
                assert (await call("GetSceneItemEnabled", {
                    "sceneName": coverage_scene,
                    "sceneItemId": coverage_ids["background"],
                }))["sceneItemEnabled"] is True
                report_v2_coverage = {
                    "negativeControl": True,
                    "legacyPluginRefusal": v2_result,
                    "pixelsUnchanged": True,
                    "refusalOccursBeforeSourceVisibilityMutation": True,
                    "allowedProtocol": "pulsar.group-composition/1",
                    "attemptedProtocol": "pulsar.group-composition/2",
                    "imageFixture": str(coverage_atlas_path),
                }
                if prism_wire_emitter is not None:
                    prism_wire_record = await prepare_prism_wire(1)
                    prism_wire_result = await call("CallVendorRequest", {
                        "vendorName": "pulsar-capture-effects",
                        "requestType": "CommitGroupComposition",
                        "requestData": prism_wire_record["wire"],
                    })
                    prism_wire_outcome = prism_wire_result.get("responseData", {})
                    assert not prism_wire_outcome.get("ok") and str(
                        prism_wire_outcome.get("error", "")).startswith(
                            "unsupported-protocol"), prism_wire_result
                    after_prism_wire_rejection = await pixels(
                        "coverage-v2-prism-wire-legacy-plugin-rejection",
                        coverage_scene, viewport)
                    assert after_prism_wire_rejection.tobytes() == \
                        coverage_before_bytes, {
                            "reason": "Prism-emitted /2 refusal changed scene pixels",
                            "error": prism_wire_result,
                        }
                    for key in ("atlas", "captureBinding", "capture"):
                        assert (await call("GetSceneItemEnabled", {
                            "sceneName": coverage_scene,
                            "sceneItemId": coverage_ids[key],
                        }))["sceneItemEnabled"] is False
                    report_v2_coverage["prismEmittedWireNegativeControl"] = {
                        "wire": prism_wire_record,
                        "response": prism_wire_result,
                        "pixelsUnchanged": True,
                        "sourceVisibilityUnchanged": True,
                        "exactPrismWireObjectForwarded": True,
                    }
            else:
                assert v2_result.get("responseData", {}).get("ok"), v2_result
                coverage_committed = True
                await asyncio.sleep(0.3)
                coverage_composited = await pixels(
                    "coverage-v2-composition-committed", coverage_scene, viewport)
                sample_y = 104
                group_mask_points = {
                    "zero": (103, sample_y), "half": (112, sample_y),
                    "full": (120, sample_y), "outside": (130, sample_y),
                }
                coverage_values = {"zero": 0.0, "half": 128 / 255, "full": 1.0}
                group_mask_pixels = {}
                for label, point in group_mask_points.items():
                    actual = coverage_composited.getpixel(point)
                    if label in coverage_values:
                        expected = expected_over(
                            owner_background_pixel, green_pixel,
                            0.5 * coverage_values[label])
                    else:
                        expected = owner_background_pixel
                    assert_pixel_near(actual, expected,
                                      f"isolated group mask {label}", 4)
                    group_mask_pixels[label] = {
                        "point": list(point), "actual": list(actual),
                        "expected": list(expected),
                        "coverage": coverage_values.get(label, 0),
                    }

                child_mask_point = (312, sample_y)
                child_mask_actual = coverage_composited.getpixel(child_mask_point)
                child_mask_expected = expected_css_composite(
                    owner_background_pixel, masked_capture_pixel, 1,
                    0.5 * (128 / 255))
                assert_pixel_near(child_mask_actual, child_mask_expected,
                                  "masked capture multiply with local opacity", 5)

                blend_results = []
                for tile in blend_tiles:
                    point = tuple(tile["point"])
                    actual = coverage_composited.getpixel(point)
                    expected = expected_css_composite(
                        owner_background_pixel, blend_source_pixel,
                        tile["id"])
                    assert_pixel_near(actual, expected,
                                      f"group CSS blend {tile['name']}", 5)
                    blend_results.append({
                        "name": tile["name"], "id": tile["id"],
                        "point": list(point), "actual": list(actual),
                        "expected": list(expected),
                    })

                idempotent_v2 = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": v2_payload,
                })
                assert idempotent_v2.get("responseData", {}).get("ok") and \
                    idempotent_v2.get("responseData", {}).get("idempotent"), idempotent_v2
                stale_groups = json.loads(json.dumps(group_definitions))
                stale_groups[0]["opacity"] = 0.9
                stale_payload = make_global_v2_plan(
                    coverage_scene, runtime_id, 1, stale_groups, viewport,
                    coverage_replaced_ids)
                stale_result = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": stale_payload,
                })
                assert str(stale_result.get("responseData", {}).get("error", "")).startswith(
                    "stale-generation"), stale_result
                after_stale = await pixels(
                    "coverage-v2-after-stale-replacement", coverage_scene, viewport)
                assert after_stale.tobytes() == coverage_composited.tobytes(), {
                    "reason": "stale replacement changed active pixels",
                    "error": stale_result,
                }

                await call("SetInputSettings", {
                    "inputName": coverage_atlas_name,
                    "inputSettings": {"file": str(coverage_atlas_update_path)},
                    "overlay": True,
                })
                await asyncio.sleep(0.5)
                updated_mask_source = await pixels(
                    "coverage-alpha-atlas-updated-source", coverage_atlas_name,
                    (28, 10))
                updated_alpha_samples = {
                    "opaque": updated_mask_source.getpixel((6, 5))[3],
                    "partial": updated_mask_source.getpixel((14, 5))[3],
                    "transparent": updated_mask_source.getpixel((22, 5))[3],
                }
                assert updated_alpha_samples == {
                    "opaque": 255, "partial": 64, "transparent": 0}, updated_alpha_samples
                assert (await call("GetSceneItemEnabled", {
                    "sceneName": coverage_scene,
                    "sceneItemId": coverage_ids["atlas"],
                }))["sceneItemEnabled"] is False
                updated_coverage_composited = await pixels(
                    "coverage-v2-after-dynamic-atlas-update", coverage_scene, viewport)
                updated_group_mask_pixels = {}
                updated_coverages = {
                    "opaque": 1.0, "partial": 64 / 255, "transparent": 0.0,
                }
                updated_group_points = {
                    "opaque": (103, sample_y), "partial": (112, sample_y),
                    "transparent": (120, sample_y), "outside": (130, sample_y),
                }
                for label, point in updated_group_points.items():
                    coverage = updated_coverages.get(label, 0.0)
                    expected = expected_over(
                        owner_background_pixel, green_pixel, 0.5 * coverage)
                    actual = updated_coverage_composited.getpixel(point)
                    assert_pixel_near(actual, expected,
                                      f"dynamic atlas group mask {label}", 4)
                    updated_group_mask_pixels[label] = {
                        "point": list(point), "actual": list(actual),
                        "expected": list(expected), "coverage": coverage,
                    }
                updated_child_mask_point = (312, sample_y)
                updated_child_mask_actual = updated_coverage_composited.getpixel(
                    updated_child_mask_point)
                updated_child_mask_expected = expected_css_composite(
                    owner_background_pixel, masked_capture_pixel, 1,
                    0.5 * (64 / 255))
                assert_pixel_near(updated_child_mask_actual,
                                  updated_child_mask_expected,
                                  "dynamic atlas capture mask multiply", 5)

                root_mask = {
                    "sceneItemId": coverage_ids["atlas"], "crop": mask_crop,
                    "rect": {"x": 100, "y": 100, "width": 24, "height": 8},
                }
                root_only_groups = [{
                    "id": ROOT_GROUP_ID, "parentId": None, "order": 0,
                    "opacity": 0.5, "visible": True, "blendMode": "multiply",
                    "bounds": {"x": 0, "y": 0, "width": viewport[0],
                               "height": viewport[1]},
                    "affine": affine(),
                    "children": [v2_capture(coverage_ids["background"])],
                    "mask": root_mask,
                }]
                root_appearance_payload = make_global_v2_plan(
                    coverage_scene, runtime_id, 2, root_only_groups, viewport,
                    coverage_replaced_ids)
                root_appearance_result = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": root_appearance_payload,
                })
                assert root_appearance_result.get("responseData", {}).get("ok"), \
                    root_appearance_result
                coverage_generation = 2
                await asyncio.sleep(0.3)
                root_appearance_image = await pixels(
                    "coverage-v2-root-appearance", coverage_scene, viewport)
                root_appearance_points = {
                    "opaqueCoverage": (103, sample_y),
                    "partialCoverage": (112, sample_y),
                    "transparentCoverage": (120, sample_y),
                    "outsideMaskRect": (130, sample_y),
                }
                root_appearance_coverages = {
                    "opaqueCoverage": 1.0,
                    "partialCoverage": 64 / 255,
                    "transparentCoverage": 0.0,
                    "outsideMaskRect": 0.0,
                }
                root_appearance_pixels = {}
                for label, point in root_appearance_points.items():
                    effective_opacity = 0.5 * root_appearance_coverages[label]
                    expected = expected_css_composite(
                        coverage_backdrop_pixel, coverage_content_pixel,
                        1, effective_opacity)
                    actual = root_appearance_image.getpixel(point)
                    assert_pixel_near(actual, expected,
                                      f"root mask opacity multiply {label}", 6)
                    root_appearance_pixels[label] = {
                        "point": list(point), "actual": list(actual),
                        "expected": list(expected),
                        "effectiveOpacity": effective_opacity,
                    }

                invalid_reference_groups = json.loads(
                    json.dumps(root_only_groups))
                invalid_reference_groups[0]["mask"]["sceneItemId"] = 9007199254740991
                invalid_reference_payload = make_global_v2_plan(
                    coverage_scene, runtime_id, 3, invalid_reference_groups,
                    viewport, coverage_replaced_ids)
                invalid_reference = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": invalid_reference_payload,
                })
                assert not invalid_reference.get("responseData", {}).get("ok"), \
                    invalid_reference
                after_invalid_reference = await pixels(
                    "coverage-v2-after-invalid-root-reference", coverage_scene,
                    viewport)
                assert after_invalid_reference.tobytes() == root_appearance_image.tobytes(), {
                    "reason": "invalid root mask reference changed active frame",
                    "error": invalid_reference,
                }
                invalid_rect_groups = json.loads(json.dumps(root_only_groups))
                invalid_rect_groups[0]["mask"]["rect"]["width"] = 23
                invalid_rect_payload = make_global_v2_plan(
                    coverage_scene, runtime_id, 3, invalid_rect_groups,
                    viewport, coverage_replaced_ids)
                invalid_rect = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": invalid_rect_payload,
                })
                invalid_rect_outcome = invalid_rect.get("responseData", {})
                assert not invalid_rect_outcome.get("ok") and str(
                    invalid_rect_outcome.get("error", "")).startswith(
                        "invalid-mask-rect"), invalid_rect
                after_invalid_rect = await pixels(
                    "coverage-v2-after-invalid-root-rect", coverage_scene, viewport)
                assert after_invalid_rect.tobytes() == root_appearance_image.tobytes(), {
                    "reason": "invalid root mask geometry changed active frame",
                    "error": invalid_rect,
                }

                excluded_blend_groups = json.loads(
                    json.dumps(root_only_groups))
                excluded_blend_groups[0]["blendMode"] = "plus-lighter"
                excluded_blend_payload = make_global_v2_plan(
                    coverage_scene, runtime_id, 3, excluded_blend_groups,
                    viewport, coverage_replaced_ids)
                excluded_blend = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": excluded_blend_payload,
                })
                assert str(excluded_blend.get("responseData", {}).get("error", "")).startswith(
                    "unsupported-blend-mode"), excluded_blend
                after_excluded_blend = await pixels(
                    "coverage-v2-after-excluded-plus-lighter", coverage_scene,
                    viewport)
                assert after_excluded_blend.tobytes() == root_appearance_image.tobytes(), {
                    "reason": "excluded plus-lighter blend changed active pixels",
                    "error": excluded_blend,
                }

                hidden_root_groups = [{
                    "id": ROOT_GROUP_ID, "parentId": None, "order": 0,
                    "opacity": 1, "visible": False, "blendMode": "normal",
                    "bounds": {"x": 0, "y": 0, "width": viewport[0],
                               "height": viewport[1]},
                    "affine": affine(),
                    "children": [v2_capture(coverage_ids["background"])],
                    "mask": None,
                }]
                hidden_root_payload = make_global_v2_plan(
                    coverage_scene, runtime_id, 3, hidden_root_groups, viewport,
                    coverage_replaced_ids)
                hidden_root_result = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": hidden_root_payload,
                })
                assert hidden_root_result.get("responseData", {}).get("ok"), \
                    hidden_root_result
                coverage_generation = 3
                await asyncio.sleep(0.2)
                hidden_root_image = await pixels(
                    "coverage-v2-hidden-root", coverage_scene, viewport)
                assert_pixel_near(hidden_root_image.getpixel((112, sample_y)),
                                  coverage_backdrop_pixel,
                                  "root visibility false", 0)

                invalid_hidden_payload = make_global_v2_plan(
                    coverage_scene, runtime_id, 4, invalid_reference_groups,
                    viewport, coverage_replaced_ids)
                invalid_hidden = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": invalid_hidden_payload,
                })
                assert not invalid_hidden.get("responseData", {}).get("ok"), \
                    invalid_hidden
                after_invalid_hidden = await pixels(
                    "coverage-v2-after-invalid-hidden-root", coverage_scene,
                    viewport)
                assert after_invalid_hidden.tobytes() == hidden_root_image.tobytes(), {
                    "reason": "invalid replacement changed hidden root pixels",
                    "error": invalid_hidden,
                }

                v2_reset = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "ResetGroupComposition",
                    "requestData": {"sceneName": coverage_scene,
                                    "generation": coverage_generation,
                                    "runtimeInstanceId": runtime_id},
                })
                assert v2_reset.get("responseData", {}).get("reset"), v2_reset
                coverage_committed = False
                await asyncio.sleep(0.2)
                coverage_after_reset = await pixels(
                    "coverage-v2-after-reset", coverage_scene, viewport)
                assert coverage_after_reset.tobytes() == coverage_before_bytes
                coverage_after_reset_items = sorted(
                    int(item["sceneItemId"]) for item in
                    (await call("GetSceneItemList", {
                        "sceneName": coverage_scene,
                    }))["sceneItems"])
                assert coverage_after_reset_items == coverage_original_scene_item_ids, {
                    "reason": "reset leaked generated composition/dependency scene items",
                    "before": coverage_original_scene_item_ids,
                    "after": coverage_after_reset_items,
                }
                assert (await call("GetSceneItemEnabled", {
                    "sceneName": coverage_scene,
                    "sceneItemId": coverage_ids["atlas"],
                }))["sceneItemEnabled"] is False
                report_v2_coverage = {
                    "negativeControl": False,
                    "protocol": "pulsar.group-composition/2",
                    "coordinateSpace": "global-scene-texels",
                    "viewport": list(viewport),
                    "canonicalPlanHashExcludesProtocolVersion": True,
                    "coverageSource": {
                        "sceneItemId": coverage_ids["atlas"],
                        "originalSceneItemDisabled": True,
                        "dimensions": list(mask_source_image.size),
                        "fixtureSha256": sha256(coverage_atlas_path),
                        "updatedFixtureSha256": sha256(coverage_atlas_update_path),
                        "crop": mask_crop,
                        "groupRect": group_mask["rect"],
                        "captureRect": child_mask["rect"],
                        "initialAlphaSamples": {
                            "transparent": mask_source_image.getpixel((6, 5))[3],
                            "partial": mask_source_image.getpixel((14, 5))[3],
                            "opaque": mask_source_image.getpixel((22, 5))[3],
                        },
                        "updatedAlphaSamples": updated_alpha_samples,
                    },
                    "groupMaskAndOpacity": {
                        "opacity": 0.5,
                        "twoOverlappingOpaqueChildren": [
                            coverage_ids["red"], coverage_ids["green"]],
                        "initialMaskPixels": group_mask_pixels,
                        "dynamicUpdatedMaskPixels": updated_group_mask_pixels,
                        "dynamicPixelsChangedAfterCompositionCommit": True,
                        "pixelTolerance": 4,
                    },
                    "maskedCapture": {
                        "blendMode": "multiply", "localOpacity": 0.5,
                        "initialPoint": list(child_mask_point),
                        "initialActual": list(child_mask_actual),
                        "initialExpected": list(child_mask_expected),
                        "updatedPoint": list(updated_child_mask_point),
                        "updatedActual": list(updated_child_mask_actual),
                        "updatedExpected": list(updated_child_mask_expected),
                        "pixelTolerance": 5,
                    },
                    "rootAppearance": {
                        "opacity": 0.5, "visible": True,
                        "blendMode": "multiply", "mask": root_mask,
                        "backdropSource": coverage_backdrop_name,
                        "pixels": root_appearance_pixels,
                        "pixelTolerance": 6,
                    },
                    "rootVisibilityFalse": {
                        "response": hidden_root_result,
                        "pixel": list(hidden_root_image.getpixel((112, sample_y))),
                        "backdrop": list(coverage_backdrop_pixel),
                    },
                    "groupCssBlendModes": {
                        "tested": blend_results,
                        "plusLighterId10Excluded": True,
                        "pixelTolerance": 5,
                    },
                    "idempotentReplay": idempotent_v2,
                    "staleGeneration": stale_result,
                    "staleGenerationPixelsUnchanged": True,
                    "invalidRootMaskReference": invalid_reference,
                    "invalidRootMaskReferencePixelsUnchanged": True,
                    "invalidRootMaskRect": invalid_rect,
                    "invalidRootMaskRectPixelsUnchanged": True,
                    "excludedPlusLighter": excluded_blend,
                    "excludedPlusLighterPixelsUnchanged": True,
                    "invalidReplacementWhileRootHidden": invalid_hidden,
                    "invalidReplacementWhileRootHiddenPixelsUnchanged": True,
                    "dependencyProxy": {
                        "purpose": "retain the same atlas in OBS active-source traversal",
                        "duplicatesPixels": False,
                        "noReferenceCycleByConstruction": True,
                        "resetRestoresOriginalSceneItemIds": True,
                        "sceneItemIdsBeforeReset": coverage_original_scene_item_ids,
                        "sceneItemIdsAfterReset": coverage_after_reset_items,
                        "atlasOriginalVisibilityRestored": True,
                    },
                    "reset": v2_reset,
                    "resetRestoresPixels": True,
                    "syntheticImageAtlas": True,
                    "doesNotExerciseCefOrPhysicalCapture": True,
                }

                if prism_wire_emitter is not None:
                    prism_generation = coverage_generation + 1
                    prism_wire_record = await prepare_prism_wire(
                        prism_generation)
                    prism_wire = prism_wire_record["wire"]
                    prism_baseline = await pixels(
                        "prism-wire-parent-prefix-before-commit",
                        coverage_scene, viewport)
                    assert prism_baseline.tobytes() == coverage_before_bytes
                    prism_commit = await call("CallVendorRequest", {
                        "vendorName": "pulsar-capture-effects",
                        "requestType": "CommitGroupComposition",
                        "requestData": prism_wire,
                    })
                    assert prism_commit.get("responseData", {}).get("ok"), {
                        "response": prism_commit,
                        "wire": prism_wire,
                    }
                    coverage_committed = True
                    coverage_generation = prism_generation
                    await asyncio.sleep(0.4)
                    prism_image = await pixels(
                        "prism-wire-v2-composition-committed",
                        coverage_scene, viewport)

                    solar_band_pixel = tuple(
                        prism_wire_record["atlasFixture"]["readbackSamples"][
                            "solarBand"])
                    capture_pixel = capture_source_image.getpixel((15, 5))
                    wire_points = {
                        "rootMaskTransparent": (5, 100),
                        "rootMaskPartial": (15, 100),
                        "captureMaskTransparent": (25, 50),
                        "captureMaskPartial": (50, 50),
                    }
                    wire_initial = {}
                    prefix = prism_baseline.getpixel(
                        wire_points["rootMaskTransparent"])
                    actual = prism_image.getpixel(
                        wire_points["rootMaskTransparent"])
                    assert_pixel_near(actual, prefix,
                                      "Prism wire transparent root mask", 0)
                    wire_initial["rootMaskTransparent"] = {
                        "point": list(wire_points["rootMaskTransparent"]),
                        "actual": list(actual), "expected": list(prefix),
                        "coverage": 0.0,
                    }

                    partial_root_point = wire_points["rootMaskPartial"]
                    partial_root_prefix = prism_baseline.getpixel(
                        partial_root_point)
                    partial_root_expected = expected_css_composite(
                        partial_root_prefix, solar_band_pixel, 1,
                        0.5 * (128 / 255))
                    partial_root_actual = prism_image.getpixel(partial_root_point)
                    assert_pixel_near(partial_root_actual, partial_root_expected,
                                      "Prism wire partial root mask", 6)
                    wire_initial["rootMaskPartial"] = {
                        "point": list(partial_root_point),
                        "actual": list(partial_root_actual),
                        "expected": list(partial_root_expected),
                        "coverage": 128 / 255,
                        "rootOpacity": 0.5,
                        "blendMode": "multiply",
                    }

                    capture_zero_point = wire_points["captureMaskTransparent"]
                    capture_zero_prefix = prism_baseline.getpixel(
                        capture_zero_point)
                    capture_zero_group = expected_css_composite(
                        solar_band_pixel, capture_pixel, 2, 0)
                    capture_zero_expected = expected_css_composite(
                        capture_zero_prefix, capture_zero_group, 1, 0.5)
                    capture_zero_actual = prism_image.getpixel(capture_zero_point)
                    assert_pixel_near(capture_zero_actual,
                                      capture_zero_expected,
                                      "Prism wire transparent capture mask", 6)
                    wire_initial["captureMaskTransparent"] = {
                        "point": list(capture_zero_point),
                        "actual": list(capture_zero_actual),
                        "expected": list(capture_zero_expected),
                        "coverage": 0.0,
                        "captureBlendMode": "screen",
                    }

                    capture_partial_point = wire_points["captureMaskPartial"]
                    capture_partial_prefix = prism_baseline.getpixel(
                        capture_partial_point)
                    capture_partial_group = expected_css_composite(
                        solar_band_pixel, capture_pixel, 2,
                        0.75 * (128 / 255))
                    capture_partial_expected = expected_css_composite(
                        capture_partial_prefix, capture_partial_group, 1, 0.5)
                    capture_partial_actual = prism_image.getpixel(
                        capture_partial_point)
                    assert_pixel_near(capture_partial_actual,
                                      capture_partial_expected,
                                      "Prism wire partial transformed capture mask", 6)
                    wire_initial["captureMaskPartial"] = {
                        "point": list(capture_partial_point),
                        "actual": list(capture_partial_actual),
                        "expected": list(capture_partial_expected),
                        "coverage": 128 / 255,
                        "captureLocalOpacity": 0.75,
                        "captureBlendMode": "screen",
                        "captureAffine": {
                            "m00": 2, "m01": 0, "tx": 20,
                            "m10": 0, "m11": 2, "ty": 40,
                        },
                        "sourcePointApproximation": [15, 5],
                    }

                    await call("SetInputSettings", {
                        "inputName": coverage_atlas_name,
                        "inputSettings": {"file": str(prism_atlas_update_path)},
                        "overlay": True,
                    })
                    await asyncio.sleep(0.5)
                    updated_prism_atlas = await pixels(
                        "prism-wire-solar-coverage-atlas-updated-source",
                        coverage_atlas_name, (1922, 3246))
                    assert updated_prism_atlas.size == (1922, 3246), \
                        updated_prism_atlas.size
                    updated_prism_samples = {
                        "solarBand": list(updated_prism_atlas.getpixel((51, 51))),
                        "groupMaskAtFormerPartial":
                            updated_prism_atlas.getpixel((16, 1183))[3],
                        "captureMaskAtFormerPartial":
                            updated_prism_atlas.getpixel((51, 2215))[3],
                    }
                    assert updated_prism_samples["solarBand"] == \
                        [220, 100, 40, 255], updated_prism_samples
                    assert updated_prism_samples["groupMaskAtFormerPartial"] == \
                        255, updated_prism_samples
                    assert updated_prism_samples["captureMaskAtFormerPartial"] == \
                        0, updated_prism_samples
                    updated_band_pixel = tuple(
                        updated_prism_samples["solarBand"])
                    updated_prism_image = await pixels(
                        "prism-wire-v2-after-live-atlas-update",
                        coverage_scene, viewport)
                    updated_wire_pixels = {}
                    updated_root_expected = expected_css_composite(
                        prism_baseline.getpixel(partial_root_point),
                        updated_band_pixel, 1, 0.5)
                    updated_root_actual = updated_prism_image.getpixel(
                        partial_root_point)
                    assert_pixel_near(updated_root_actual, updated_root_expected,
                                      "Prism wire updated opaque root mask", 6)
                    updated_wire_pixels["rootMaskFormerPartialNowOpaque"] = {
                        "point": list(partial_root_point),
                        "actual": list(updated_root_actual),
                        "expected": list(updated_root_expected),
                        "coverage": 1.0,
                    }
                    updated_capture_expected = expected_css_composite(
                        prism_baseline.getpixel(capture_partial_point),
                        updated_band_pixel, 1, 0.5)
                    updated_capture_actual = updated_prism_image.getpixel(
                        capture_partial_point)
                    assert_pixel_near(updated_capture_actual,
                                      updated_capture_expected,
                                      "Prism wire updated transparent capture mask", 6)
                    updated_wire_pixels[
                        "captureMaskFormerPartialNowTransparent"] = {
                            "point": list(capture_partial_point),
                            "actual": list(updated_capture_actual),
                            "expected": list(updated_capture_expected),
                            "coverage": 0.0,
                        }
                    assert updated_root_actual != partial_root_actual, {
                        "reason": "live atlas update did not change the root mask sample",
                        "before": partial_root_actual,
                        "after": updated_root_actual,
                    }
                    assert updated_capture_actual != capture_partial_actual, {
                        "reason": "live atlas update did not change capture output",
                        "before": capture_partial_actual,
                        "after": updated_capture_actual,
                    }

                    prism_reset = await call("CallVendorRequest", {
                        "vendorName": "pulsar-capture-effects",
                        "requestType": "ResetGroupComposition",
                        "requestData": {
                            "sceneName": coverage_scene,
                            "generation": prism_generation,
                            "runtimeInstanceId": runtime_id,
                        },
                    })
                    assert prism_reset.get("responseData", {}).get("reset"), \
                        prism_reset
                    coverage_committed = False
                    await asyncio.sleep(0.25)
                    prism_after_reset = await pixels(
                        "prism-wire-v2-after-reset", coverage_scene, viewport)
                    assert prism_after_reset.tobytes() == coverage_before_bytes
                    prism_after_reset_items = sorted(
                        int(item["sceneItemId"]) for item in
                        (await call("GetSceneItemList", {
                            "sceneName": coverage_scene,
                        }))["sceneItems"])
                    assert prism_after_reset_items == \
                        coverage_original_scene_item_ids, {
                            "before": coverage_original_scene_item_ids,
                            "after": prism_after_reset_items,
                        }
                    for key in ("atlas", "captureBinding", "capture"):
                        assert (await call("GetSceneItemEnabled", {
                            "sceneName": coverage_scene,
                            "sceneItemId": coverage_ids[key],
                        }))["sceneItemEnabled"] is False
                    report_v2_coverage["prismEmittedWireGpu"] = {
                        "wire": prism_wire_record,
                        "wireWasForwardedWithoutMutation": True,
                        "planHash": prism_wire["planHash"],
                        "exactPrismWireObjectForwarded": True,
                        "nativeCommit": prism_commit,
                        "initialPixels": wire_initial,
                        "updatedPixels": updated_wire_pixels,
                        "updatedAtlasSamples": updated_prism_samples,
                        "dynamicAtlasChangedPixelsAfterActivation": True,
                        "liveSourceUpdateSha256": sha256(prism_atlas_update_path),
                        "parentPrefixPixel": list(capture_partial_prefix),
                        "parentPrefixSource": coverage_background_name,
                        "reset": prism_reset,
                        "resetRestoresPixels": True,
                        "resetRestoresOriginalItemsAndHiddenInputs": True,
                        "captureBindingAndContributionAreDistinctSceneItems": True,
                        "pixelTolerance": 6,
                        "limitations": [
                            "Prism/Solar projection and hashing are real; the Solar fixture is synthetic.",
                            "Native GPU coverage uses a synthetic image_source atlas and color_source_v3 capture, not live CEF or a physical camera.",
                        ],
                    }

            await call("CreateScene", {"sceneName": lifecycle_scene})
            lifecycle_legacy = await call("CreateInput", {
                "sceneName": lifecycle_scene, "inputName": lifecycle_legacy_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF2A5AD0,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": True,
            })
            lifecycle_legacy_id = int(lifecycle_legacy["sceneItemId"])
            lifecycle_input = await call("CreateInput", {
                "sceneName": lifecycle_scene, "inputName": lifecycle_input_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF00FF00,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": False,
            })
            lifecycle_input_id = int(lifecycle_input["sceneItemId"])
            lifecycle_legacy_pixel = (await pixels(
                "remove-dependency-legacy-before", lifecycle_scene,
                (64, 64))).getpixel((32, 32))
            lifecycle_payload = mixed_composition_plan(
                lifecycle_scene, runtime_id, 1, viewport,
                [{"kind": "capture", "sceneItemId": lifecycle_input_id,
                  "affine": affine(),
                  "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                  "localOpacity": 1, "visible": True}],
                affine(), 1,
                replaced_scene_items=[lifecycle_legacy_id, lifecycle_input_id])
            lifecycle_result = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition",
                "requestData": lifecycle_payload,
            })
            assert lifecycle_result["responseData"].get("ok"), lifecycle_result
            lifecycle_committed = True
            dependency_composited = await pixels(
                "remove-dependency-before-removal", lifecycle_scene, (64, 64))
            dependency_pixel = dependency_composited.getpixel((32, 32))
            assert dependency_pixel[1] > dependency_pixel[0], dependency_pixel
            removed_dependency = await call("RemoveInput", {
                "inputName": lifecycle_input_name,
            })
            dependency_cleanup = await wait_for_restoration(
                lifecycle_scene, lifecycle_legacy_id, lifecycle_legacy_pixel,
                "remove-dependency-restored")
            lifecycle_committed = False

            graphics_input = await call("CreateInput", {
                "sceneName": lifecycle_scene, "inputName": graphics_input_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFFFF8000,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": False,
            })
            graphics_input_id = int(graphics_input["sceneItemId"])
            graphics_payload = mixed_composition_plan(
                lifecycle_scene, runtime_id, 2, viewport,
                [{"kind": "capture", "sceneItemId": graphics_input_id,
                  "affine": affine(),
                  "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                  "localOpacity": 1, "visible": True}],
                affine(), 1,
                replaced_scene_items=[lifecycle_legacy_id, graphics_input_id])
            graphics_result = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition",
                "requestData": graphics_payload,
            })
            assert graphics_result["responseData"].get("ok"), graphics_result
            lifecycle_committed = True
            lifecycle_generation = 2
            graphics_remove_call = await call("CallVendorRequest", {
                "vendorName": "group-composition-test-helper",
                "requestType": "RemoveSourceOnGraphics",
                "requestData": {"sourceName": graphics_input_name},
            })
            graphics_removed = graphics_remove_call["responseData"]
            assert graphics_removed.get("ok"), graphics_removed
            assert graphics_removed.get("executedOnGraphicsTask") is True, graphics_removed
            graphics_cleanup = await wait_for_restoration(
                lifecycle_scene, lifecycle_legacy_id, lifecycle_legacy_pixel,
                "graphics-source-remove-restored")
            lifecycle_committed = False

            old_scene = await call("CreateScene", {"sceneName": same_name_scene})
            old_scene_uuid = old_scene["sceneUuid"]
            same_name_legacy = await call("CreateInput", {
                "sceneName": same_name_scene, "inputName": same_name_legacy_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF2A5AD0,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": True,
            })
            same_name_legacy_id = int(same_name_legacy["sceneItemId"])
            same_name_input = await call("CreateInput", {
                "sceneName": same_name_scene, "inputName": same_name_input_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF00FF00,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": False,
            })
            same_name_input_id = int(same_name_input["sceneItemId"])
            same_name_payload = mixed_composition_plan(
                same_name_scene, runtime_id, 3, viewport,
                [{"kind": "capture", "sceneItemId": same_name_input_id,
                  "affine": affine(),
                  "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                  "localOpacity": 1, "visible": True}],
                affine(), 1,
                replaced_scene_items=[same_name_legacy_id, same_name_input_id])
            same_name_result = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition",
                "requestData": same_name_payload,
            })
            assert same_name_result["responseData"].get("ok"), same_name_result
            same_name_committed = True
            await call("RemoveScene", {"sceneName": same_name_scene})
            same_name_committed = False
            await asyncio.sleep(0.2)
            new_scene = await call("CreateScene", {"sceneName": same_name_scene})
            new_scene_uuid = new_scene["sceneUuid"]
            assert new_scene_uuid != old_scene_uuid, {
                "oldSceneUuid": old_scene_uuid, "newSceneUuid": new_scene_uuid}
            new_legacy = await call("CreateInput", {
                "sceneName": same_name_scene,
                "inputName": "new-" + same_name_legacy_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF2A5AD0,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": True,
            })
            new_input = await call("CreateInput", {
                "sceneName": same_name_scene,
                "inputName": "new-" + same_name_input_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF00FF00,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": False,
            })
            new_scene_payload = mixed_composition_plan(
                same_name_scene, runtime_id, 1, viewport,
                [{"kind": "capture", "sceneItemId": int(new_input["sceneItemId"]),
                  "affine": affine(),
                  "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                  "localOpacity": 1, "visible": True}],
                affine(), 1,
                replaced_scene_items=[int(new_legacy["sceneItemId"]),
                                      int(new_input["sceneItemId"])])
            new_scene_commit = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition",
                "requestData": new_scene_payload,
            })
            assert new_scene_commit["responseData"].get("ok"), new_scene_commit
            same_name_committed = True
            same_name_generation = 1
            new_scene_pixel = await pixels("same-name-new-uuid-generation-1",
                                           same_name_scene, (64, 64))
            assert new_scene_pixel.getpixel((32, 32))[1] > 0
            new_scene_reset = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "ResetGroupComposition",
                "requestData": {"sceneName": same_name_scene, "generation": 1,
                                "runtimeInstanceId": runtime_id},
            })
            assert new_scene_reset["responseData"].get("reset"), new_scene_reset
            same_name_committed = False

            await call("CreateScene", {"sceneName": unload_scene})
            unload_legacy = await call("CreateInput", {
                "sceneName": unload_scene, "inputName": unload_legacy_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF2A5AD0,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": True,
            })
            unload_input = await call("CreateInput", {
                "sceneName": unload_scene, "inputName": unload_input_name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF00FF00,
                                  "width": viewport[0], "height": viewport[1]},
                "sceneItemEnabled": False,
            })
            unload_payload = mixed_composition_plan(
                unload_scene, runtime_id, 1, viewport,
                [{"kind": "capture", "sceneItemId": int(unload_input["sceneItemId"]),
                  "affine": affine(),
                  "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                  "localOpacity": 1, "visible": True}],
                affine(), 1,
                replaced_scene_items=[int(unload_legacy["sceneItemId"]),
                                      int(unload_input["sceneItemId"])])
            unload_result = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition",
                "requestData": unload_payload,
            })
            assert unload_result["responseData"].get("ok"), unload_result
            unload_active = True
            pending_unload = await call("CallVendorRequest", {
                "vendorName": "group-composition-test-helper",
                "requestType": "ArmPendingUnloadRemoval",
                "requestData": {"sourceName": f"native-pending-unload-{token}"},
            })
            assert pending_unload["responseData"].get("ok"), pending_unload
            return {
                "scene": scene, "runtimeInstanceId": runtime_id,
                "generation": generation, "planHash": payload["planHash"],
                "sceneItemIds": {"red": red_id, "blue": blue_id},
                "appearanceWrapper": {
                    "sceneName": appearance_scene,
                    "sourceIsNestedScene": True,
                    "stagingSceneItemEnabled": False,
                    "authoredCaptureVisible": True,
                },
                "viewport": {"width": viewport[0], "height": viewport[1]},
                "overlapPixel": list(center), "expectedGroupOpacityAlpha": 128,
                "colorSourceV3Convention": {
                    "redInputReportedRgb": list(source_red_center[:3]),
                    "blueInputReportedRgb": list(source_blue_center[:3]),
                },
                "sourcePixels": {"red": list(source_red_center),
                                 "blue": list(source_blue_center)},
                "malformedNumericArray": {
                    "response": refusal, "pixelsUnchanged": True,
                    "reason": "The actual obs-websocket bridge preserves only object entries inside arrays."},
                "unsafeJsonIntegers": {
                    "generationResponse": unsafe_generation["responseData"],
                    "generationPixelsUnchanged": True,
                    "sceneItemIdResponse": unsafe_id["responseData"],
                    "sceneItemIdPixelsUnchanged": True,
                    "maximumSafeInteger": 9007199254740991,
                },
                "successiveCommit": {
                    "generation": updated_generation, "planHash": updated_plan["planHash"],
                    "overlapPixel": list(updated_center),
                    "expectedGroupOpacityAlpha": 191,
                },
                "reset": reset["responseData"],
                "restoredPixel": list(restored_center),
                "translatedLocalOpacity": {
                    "sourceDimensions": [64, 64], "translationX": 256,
                    "opacity": 0.5, "sampleAt": [288, 32],
                    "pixel": list(translated_pixel),
                    "sourcePixel": list(offset_source_center),
                    "reset": offset_reset["responseData"],
                    "stagingVisibilityRestored": offset_item_state["sceneItemEnabled"],
                },
                "solarBand": {
                    "scene": solar_scene,
                    "sceneItemId": atlas_id,
                    "sourceInput": "image_source",
                    "sourceDimensions": list(atlas_image.size),
                    "sourceSceneItemEnabledBefore": atlas_enabled_before["sceneItemEnabled"],
                    "sourceSceneItemEnabledAfterReset": atlas_enabled_after["sceneItemEnabled"],
                    "legacyVisibleItemId": legacy_id,
                    "legacyVisibleBefore": legacy_state_before["sceneItemEnabled"],
                    "legacyVisibleAfterCommit": replaced_after_commit["sceneItemEnabled"],
                    "legacyVisibleAfterReset": legacy_enabled_after["sceneItemEnabled"],
                    "legacyPixelBefore": list(legacy_before_red),
                    "legacyPixelAfterReset": list(band_after_reset.getpixel(band_red_point)),
                    "bands": [17, 18],
                    "sameSceneItemReused": True,
                    "cropPixels": [atlas_bands[0]["crop"], atlas_bands[1]["crop"]],
                    "ownerLocalRects": [atlas_bands[0]["rect"], atlas_bands[1]["rect"]],
                    "ownerAffine": affine(200, 100),
                    "sampleSceneCoordinates": [list(band_red_point), list(band_green_point)],
                    "sourcePixels": {"red": list(atlas_red), "green": list(atlas_green)},
                    "compositedPixels": {"red": list(band_red), "green": list(band_green)},
                    "invalidCrop": invalid_crop["responseData"],
                    "invalidCropPixelsUnchanged": True,
                    "invalidRect": invalid_rect["responseData"],
                    "invalidRectPixelsUnchanged": True,
                    "duplicateBand": duplicate_band["responseData"],
                    "duplicateBandPixelsUnchanged": True,
                    "replacementSetMismatchSameHash": changed_set_same_hash["responseData"],
                    "replacementSetMismatchPixelsUnchanged": True,
                    "replacementSetMismatchChangedHash": changed_hash["responseData"],
                    "replacementSetMismatchChangedHashPixelsUnchanged": True,
                    "unsupportedMask": unsupported_mask["responseData"],
                    "unsupportedMaskPixelsUnchanged": True,
                    "reset": band_reset["responseData"],
                    "resetRestoresLegacyPixels": True,
                    "staleAfterReset": stale_band["responseData"],
                    "staleReplayPreservesLegacyPixels": True,
                },
                "mixedComposition": {
                    "scene": mixed_scene,
                    "childrenInPaintOrder": ["solarBand:31", "capture:green",
                                             "solarBand:32", "capture:green@0.25",
                                             "capture:green@0.75", "group:rotated"],
                    "sameCaptureSceneItemReused": True,
                    "captureAndBandOverlap": {
                        "scenePoint": list(mixed_overlap_point),
                        "pixel": list(mixed_overlap),
                        "expectedAlphaAfterGroupOpacity": [127, 128],
                        "groupOpacity": 0.5,
                        "captureAuthoredVisibleWhileSourceSceneItemDisabled": True,
                    },
                    "repeatedCaptureOccurrences": [
                        {"localOpacity": 0.25, "pixel": list(low_opacity_pixel),
                         "expectedAlphaAfterGroupOpacity": [31, 32],
                         "scenePoint": list(low_opacity_point)},
                        {"localOpacity": 0.75, "pixel": list(high_opacity_pixel),
                         "expectedAlphaAfterGroupOpacity": [95, 96],
                         "scenePoint": list(high_opacity_point)},
                    ],
                    "nestedGroup": {
                        "affine": rotated_group["affine"],
                        "parentTranslation": affine(200, 100),
                        "sourcePixel": list(nested_capture_pixel),
                        "sourcePixelAfterGroupOpacity": list(nested_group_rgb) + [127],
                        "expectedScenePixelBounds": [764, 370, 780, 386],
                        "observedScenePixelBounds": list(rotated_bbox),
                    },
                    "replacementItems": [mixed_legacy_id, mixed_capture_id,
                                         nested_capture_id],
                    "resetRestoresLegacyAndStagingVisibility": True,
                    "reset": mixed_reset["responseData"],
                },
                "groupCompositionV2": report_v2_coverage,
                "sourceRemovalLifecycle": {
                    "removeInput": {
                        "removedInput": lifecycle_input_name,
                        "response": removed_dependency,
                        "compositedPixel": list(dependency_pixel),
                        "restored": dependency_cleanup,
                    },
                    "graphicsTaskSourceRemove": {
                        "sourceName": graphics_input_name,
                        "response": graphics_removed,
                        "restored": graphics_cleanup,
                        "executedOnGraphicsTask": graphics_removed["executedOnGraphicsTask"],
                    },
                    "sameNameSceneRecreation": {
                        "oldSceneUuid": old_scene_uuid,
                        "newSceneUuid": new_scene_uuid,
                        "differentUuid": new_scene_uuid != old_scene_uuid,
                        "newSceneGenerationOneCommit": new_scene_commit["responseData"],
                        "readbackPixel": list(new_scene_pixel.getpixel((32, 32))),
                        "reset": new_scene_reset["responseData"],
                    },
                    "activeCompositionAtModuleUnload": {
                        "scene": unload_scene,
                        "generation": 1,
                        "leftActiveForShutdown": unload_active,
                    },
                    "sourceRemovalArmedForModuleUnload": pending_unload["responseData"],
                },
                "readbackImages": readback_images,
            }
        finally:
            if committed:
                try:
                    await call("CallVendorRequest", {
                        "vendorName": "pulsar-capture-effects",
                        "requestType": "ResetGroupComposition",
                        "requestData": {"sceneName": scene, "generation": generation,
                                        "runtimeInstanceId": runtime_id},
                    })
                except Exception:
                    pass
            if offset_committed:
                try:
                    await call("CallVendorRequest", {
                        "vendorName": "pulsar-capture-effects",
                        "requestType": "ResetGroupComposition",
                        "requestData": {"sceneName": offset_scene, "generation": 1,
                                        "runtimeInstanceId": runtime_id},
                    })
                except Exception:
                    pass
            if solar_committed:
                try:
                    await call("CallVendorRequest", {
                        "vendorName": "pulsar-capture-effects",
                        "requestType": "ResetGroupComposition",
                        "requestData": {"sceneName": solar_scene, "generation": 1,
                                        "runtimeInstanceId": runtime_id},
                    })
                except Exception:
                    pass
            if mixed_committed:
                try:
                    await call("CallVendorRequest", {
                        "vendorName": "pulsar-capture-effects",
                        "requestType": "ResetGroupComposition",
                        "requestData": {"sceneName": mixed_scene, "generation": 1,
                                        "runtimeInstanceId": runtime_id},
                    })
                except Exception:
                    pass
            if coverage_committed:
                try:
                    await call("CallVendorRequest", {
                        "vendorName": "pulsar-capture-effects",
                        "requestType": "ResetGroupComposition",
                        "requestData": {"sceneName": coverage_scene,
                                        "generation": coverage_generation,
                                        "runtimeInstanceId": runtime_id},
                    })
                except Exception:
                    pass
            if lifecycle_committed:
                try:
                    await call("CallVendorRequest", {
                        "vendorName": "pulsar-capture-effects",
                        "requestType": "ResetGroupComposition",
                        "requestData": {"sceneName": lifecycle_scene,
                                        "generation": lifecycle_generation,
                                        "runtimeInstanceId": runtime_id},
                    })
                except Exception:
                    pass
            if same_name_committed:
                try:
                    await call("CallVendorRequest", {
                        "vendorName": "pulsar-capture-effects",
                        "requestType": "ResetGroupComposition",
                        "requestData": {"sceneName": same_name_scene,
                                        "generation": same_name_generation,
                                        "runtimeInstanceId": runtime_id},
                    })
                except Exception:
                    pass
            for kind, data in [
                ("RemoveScene", {"sceneName": scene}),
                ("RemoveScene", {"sceneName": appearance_scene}),
                ("RemoveScene", {"sceneName": offset_scene}),
                ("RemoveScene", {"sceneName": solar_scene}),
                ("RemoveScene", {"sceneName": lifecycle_scene}),
                ("RemoveScene", {"sceneName": same_name_scene}),
                ("RemoveScene", {"sceneName": coverage_scene}),
                ("RemoveInput", {"inputName": red_name}),
                ("RemoveInput", {"inputName": blue_name}),
                ("RemoveInput", {"inputName": offset_name}),
                ("RemoveInput", {"inputName": atlas_name}),
                ("RemoveInput", {"inputName": legacy_name}),
                ("RemoveInput", {"inputName": lifecycle_legacy_name}),
                ("RemoveInput", {"inputName": lifecycle_input_name}),
                ("RemoveInput", {"inputName": graphics_input_name}),
                ("RemoveInput", {"inputName": same_name_legacy_name}),
                ("RemoveInput", {"inputName": same_name_input_name}),
                ("RemoveInput", {"inputName": "new-" + same_name_legacy_name}),
                ("RemoveInput", {"inputName": "new-" + same_name_input_name}),
                ("RemoveInput", {"inputName": coverage_background_name}),
                ("RemoveInput", {"inputName": coverage_backdrop_name}),
                ("RemoveInput", {"inputName": coverage_owner_background_name}),
                ("RemoveInput", {"inputName": coverage_red_name}),
                ("RemoveInput", {"inputName": coverage_green_name}),
                ("RemoveInput", {"inputName": coverage_capture_name}),
                ("RemoveInput", {"inputName": coverage_capture_binding_name}),
                ("RemoveInput", {"inputName": coverage_blend_source_name}),
                ("RemoveInput", {"inputName": coverage_atlas_name}),
            ]:
                try:
                    await call(kind, data)
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=pathlib.Path, required=True,
                        help="Compatible prebuilt runtime rundir/RelWithDebInfo")
    parser.add_argument(
        "--upstream-source", type=pathlib.Path,
        help=("Optional upstream checkout used to record source provenance; "
              "defaults to the runtime's ancestor layout"))
    parser.add_argument(
        "--host-executable", type=pathlib.Path,
        help="Optional private pulsar.exe copied into the disposable runtime")
    parser.add_argument("--plugin", type=pathlib.Path, required=True,
                        help="Private pulsar-capture-effects.dll")
    parser.add_argument("--websocket-plugin", type=pathlib.Path, required=True,
                        help="Private obs-websocket.dll built from this checkout")
    parser.add_argument("--graphics-remove-helper", type=pathlib.Path, required=True,
                        help="Private helper for graphics-task and module-unload source removal tests")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument(
        "--prism-wire-emitter", type=pathlib.Path,
        help=("optional read-only Prism SSR emitter; sends its exact hashed /2 "
              "wire object through the private Pulsar native request"))
    parser.add_argument(
        "--expect-v2-rejection", action="store_true",
        help="negative control: a preserved /1 plugin must reject a valid /2 plan before mutation")
    shutdown_mode = parser.add_mutually_exclusive_group()
    shutdown_mode.add_argument(
        "--shutdown-control-only", action="store_true",
        help="start the same private host but do not create any composition")
    shutdown_mode.add_argument(
        "--expect-browser-fence-failure", action="store_true",
        help=("omit the test-only browser proc and prove the private host exits "
              "fail-closed with code 1 after a shutdown request"))
    args = parser.parse_args()
    runtime_source = args.runtime.resolve()
    host_executable_source = (
        args.host_executable.resolve() if args.host_executable else None)
    plugin_source = args.plugin.resolve()
    websocket_source = args.websocket_plugin.resolve()
    graphics_remove_helper_source = args.graphics_remove_helper.resolve()
    prism_wire_emitter_source = (
        args.prism_wire_emitter.resolve() if args.prism_wire_emitter else None)
    output = args.output.resolve()
    if output != EVIDENCE.resolve():
        raise ValueError(f"Output must be canonical: {EVIDENCE}")
    cache = CACHE.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    scratch = cache / ("private-runtime-" + uuid.uuid4().hex)
    if scratch.exists() or not scratch.resolve().is_relative_to(cache):
        raise ValueError("Scratch must be new and contained in this worktree cache")
    runtime = scratch / "runtime"
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_type = (
        "browser-fence-failure"
        if args.expect_browser_fence_failure else
        "group-composition-shutdown-control"
        if args.shutdown_control_only else
        "group-composition-v2-negative-control"
        if args.expect_v2_rejection else
        "group-composition-probe")
    report_path = output / f"{stamp}-{report_type}.json"
    if report_path.exists():
        raise FileExistsError(report_path)
    report = {"startedUtc": datetime.now(timezone.utc).isoformat(), "passed": False}
    process = None
    process_protocol = load("group_comp_process", "probe-websocket.py")
    previous_env = {key: value for key, value in os.environ.items()
                    if key.startswith("PULSAR_")}
    runtime_id = "group-proof-" + uuid.uuid4().hex
    try:
        runtime_plugin_dir = runtime_source / "obs-plugins" / "64bit"
        browser_modules = sorted(
            path.name for path in runtime_plugin_dir.glob("*.dll")
            if "browser" in path.name.casefold())
        if browser_modules:
            raise RuntimeError(
                "the no-browser shutdown fixture cannot run with browser modules: "
                + ", ".join(browser_modules))
        report["browserPreShutdownFixture"] = {
            "enabled": not args.expect_browser_fence_failure,
            "mode": (
                "missing-private-browser-proc"
                if args.expect_browser_fence_failure else
                "private-test-helper-no-browser-source-fence"),
            "browserModulesInRuntime": browser_modules,
            "helperCheck": (
                "no test procedure is registered; the host must reject the missing "
                "browser pre-shutdown proc"
                if args.expect_browser_fence_failure else
                "enumerates OBS sources when the host requests the browser fence and "
                "returns success only when no browser_source/browser_source_v2 exists"),
            "cefShutdownExercised": False,
        }
        for directory in ("bin", "data", "obs-plugins"):
            shutil.copytree(runtime_source / directory, runtime / directory)
        exe = runtime / "bin" / "64bit" / "pulsar.exe"
        if host_executable_source is not None:
            if not host_executable_source.is_file():
                raise FileNotFoundError(host_executable_source)
            shutil.copyfile(host_executable_source, exe)
        report["hostExecutable"] = {
            "path": str(host_executable_source or exe),
            "sha256": sha256(host_executable_source or exe),
            "copiedFromPrivateBuild": host_executable_source is not None,
        }
        capture_target = runtime / "obs-plugins" / "64bit" / "pulsar-capture-effects.dll"
        websocket_target = runtime / "obs-plugins" / "64bit" / "obs-websocket.dll"
        graphics_remove_helper_target = (
            runtime / "obs-plugins" / "64bit" /
            "z-group-composition-shutdown-test-helper.dll")
        shutil.copyfile(plugin_source, capture_target)
        shutil.copyfile(websocket_source, websocket_target)
        shutil.copyfile(graphics_remove_helper_source, graphics_remove_helper_target)
        assert sha256(capture_target) == sha256(plugin_source)
        assert sha256(websocket_target) == sha256(websocket_source)
        assert sha256(graphics_remove_helper_target) == sha256(graphics_remove_helper_source)
        report["provenance"] = {
            "runtimeSource": str(runtime_source),
            "runtimeExeSha256": sha256(runtime_source / "bin" / "64bit" / "pulsar.exe"),
            "runtimeObsSha256": sha256(runtime_source / "bin" / "64bit" / "obs.dll"),
            "privateCapturePlugin": str(plugin_source),
            "privateCapturePluginSha256": sha256(plugin_source),
            "privateWebsocketPlugin": str(websocket_source),
            "privateWebsocketPluginSha256": sha256(websocket_source),
            "privateGraphicsRemoveHelper": str(graphics_remove_helper_source),
            "privateGraphicsRemoveHelperSha256": sha256(graphics_remove_helper_source),
            "graphicsRemoveHelperPurpose": (
                "test-only module calls obs_source_remove within an OBS graphics task "
                "and queues a second removal during module unload after stop_video"),
            "jsonBridgeSource": str(ROOT / "plugins/pulsar-websocket/src/utils/Json.cpp"),
            "jsonBridgeSourceSha256": sha256(ROOT / "plugins/pulsar-websocket/src/utils/Json.cpp"),
            "jsonBridgeBinarySourceClaim": (
                "not asserted by this runner; supplied obs-websocket.dll is "
                "identified by its private path and SHA-256 above"),
        }
        upstream_root = (
            args.upstream_source.resolve()
            if args.upstream_source else runtime_source.parents[2])
        upstream_git = subprocess.run(
            ["git", "-C", str(upstream_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        report["upstreamShutdownContract"] = {
            "revision": upstream_git,
            "obsSource": {
                "path": str(upstream_root / "libobs" / "obs.c"),
                "sha256": sha256(upstream_root / "libobs" / "obs.c"),
                "locations": "obs_shutdown:1477; stop_video:1512; module unload loop:1517",
                "contract": "the graphics video thread is stopped and joined before module unload callbacks",
            },
            "graphicsTaskSource": {
                "path": str(upstream_root / "libobs" / "obs-video.c"),
                "sha256": sha256(upstream_root / "libobs" / "obs-video.c"),
                "locations": "execute_graphics_tasks:1024; task execution:1216; loop stop check:1248",
                "contract": "graphics tasks are executed before the loop checks whether to exit; final-boundary races are handled by the plugin-owned pending-event set",
            },
            "queueSource": {
                "path": str(upstream_root / "libobs" / "obs.c"),
                "sha256": sha256(upstream_root / "libobs" / "obs.c"),
                "locations": "obs_queue_task:3518-3544",
                "contract": "same-task-thread callbacks execute inline; otherwise graphics tasks enter the queue, so module unload cannot wait on a stopped graphics worker",
            },
            "videoTaskReleaseSource": {
                "path": str(upstream_root / "libobs" / "obs.c"),
                "sha256": sha256(upstream_root / "libobs" / "obs.c"),
                "locations": "obs_shutdown module unload loop:1516-1528; obs_free_video/deque_free(video.tasks):909-950, called at 1534",
                "contract": "graphics tasks enqueued after stop_video cannot execute; after module unload the video task deque is freed without invoking the callbacks, so payload ownership must be discharged by the plugin before the queue storage is released",
            },
            "moduleListSource": {
                "path": str(upstream_root / "libobs" / "obs-module.c"),
                "sha256": sha256(upstream_root / "libobs" / "obs-module.c"),
                "locations": "obs_open_module prepends at 181-198; free_module:777-817; obs_shutdown:1516-1528",
                "contract": "the module list is prepended on successful load and unloaded from its head; the runtime probe records load and unload marker order instead of assuming file enumeration order",
            },
            "globalSourceRemoveSignal": {
                "path": str(upstream_root / "libobs" / "obs-internal.h"),
                "sha256": sha256(upstream_root / "libobs" / "obs-internal.h"),
                "locations": "obs_source_dosignal:1100-1111",
                "contract": "private sources intentionally omit global source_remove; the private helper removes its retained synthetic source then forwards the same source pointer through the global source_remove handler so the plugin's actual listener queues an event after stop_video without registering an input for frontend cleanup",
            },
        }
        for key in list(os.environ):
            if key.startswith("PULSAR_"):
                del os.environ[key]
        os.environ.update({
            "PULSAR_RUNTIME_ROOT": str(scratch / "state"),
            "PULSAR_RUNTIME_DIR": str(scratch / "state" / "runtime"),
            "PULSAR_RUNTIME_INSTANCE_ID": runtime_id,
            "PULSAR_DESKTOP_AUDIO_DEVICE_ID": "pulsar-synthetic-test-no-device",
        })
        if not args.expect_browser_fence_failure:
            os.environ["PULSAR_TEST_NO_BROWSER_PRE_SHUTDOWN"] = "1"
        port = process_protocol.pick_free_port()
        password = uuid.uuid4().hex + uuid.uuid4().hex
        process = GracefulPrivatePulsarProcess(
            process_protocol, exe, port, password)
        process.spawn()
        report["ownedPid"] = process.proc.proc.pid
        process.wait_ready(60)
        time.sleep(2)
        initialized_audio_devices = [line for line in process._lines
                                     if "WASAPI:" in line and " initialized " in line]
        if initialized_audio_devices:
            raise RuntimeError(
                "private probe initialized an audio endpoint: "
                + " | ".join(initialized_audio_devices))
        report["syntheticOnlyBootstrap"] = {
            "desktopAudioDeviceId": "pulsar-synthetic-test-no-device",
            "microphoneConfigured": False,
            "processAudioConfigured": False,
            "initializedWasapiDevices": initialized_audio_devices,
            "browserModulesInRuntime": browser_modules,
            "browserPreShutdownFixture": (
                "missing-proc fail-closed test"
                if args.expect_browser_fence_failure else
                "test-only no-browser source assertion"),
            "result": "no WASAPI endpoint initialized during private bootstrap",
        }
        if args.shutdown_control_only or args.expect_browser_fence_failure:
            report["shutdownControl"] = {
                "compositionCreated": False,
                "reason": (
                    "exercise the fail-closed path for an absent browser proc; the "
                    "runtime contains no browser modules or sources, so CEF shutdown "
                    "is outside this test"
                    if args.expect_browser_fence_failure else
                    "compare host bootstrap/shutdown without group ownership; a "
                    "test-only fence attests no browser source is loaded, so CEF "
                    "shutdown is outside this control"),
            }
        else:
            report["probe"] = asyncio.run(exercise(
                {"server_port": port, "server_password": password}, output,
                runtime_id, scratch, args.expect_v2_rejection,
                prism_wire_emitter_source))
        report["passed"] = True
    except Exception as error:
        report["errorType"] = type(error).__name__
        report["error"] = str(error)[:2000]
        if process is not None:
            report["redactedBootTail"] = process._diag()
        raise
    finally:
        try:
            if process is not None:
                shutdown_timeout = 30.0
                shutdown_started = time.monotonic()
                try:
                    process.shutdown(grace=shutdown_timeout)
                except Exception as error:
                    report["shutdownError"] = {
                        "type": type(error).__name__, "message": str(error)[:1000]}
                report["shutdownWaitSeconds"] = round(
                    time.monotonic() - shutdown_started, 3)
                report["shutdownTimeoutSeconds"] = shutdown_timeout
                report["ownedProcessExited"] = process.proc.proc.poll() is not None
                report["gracefulShutdownRequested"] = process.graceful_shutdown_requested
                report["forcedTerminationUsed"] = process.forced_termination_used
                shutdown_lines = list(process._lines)
                report["redactedShutdownTail"] = [
                    process_protocol.redact_boot_line(line)
                    for line in shutdown_lines[-30:]]
                shutdown_evidence = {
                    "eventAdopted": any(
                        "PULSAR_SHUTDOWN_CONTROL event=ready" in line
                        for line in shutdown_lines),
                    "eventSignaled": any(
                        "PULSAR_SHUTDOWN_CONTROL event=signaled" in line
                        for line in shutdown_lines),
                    "hostShutdownReached": any(
                        "[pulsar-headless] shutting down" in line
                        for line in shutdown_lines),
                    "exitCode": process.proc.proc.returncode,
                }
                report["shutdownMilestones"] = [
                    process_protocol.redact_boot_line(line)
                    for line in shutdown_lines
                    if any(marker in line for marker in (
                        "PULSAR_SHUTDOWN_CONTROL", "[pulsar-headless] shutting down",
                        "PULSAR_FRONTEND_CLEANUP", "obs_module_unload",
                        "[obs_module_unload]", "module_unload", "PULSAR_WEBSOCKET",
                        "PULSAR_GROUP_COMPOSITION"))]
                shutdown_signal_index = next((i for i, line in enumerate(shutdown_lines)
                    if "PULSAR_SHUTDOWN_CONTROL event=signaled" in line), 0)
                shutdown_error_lines = [
                    line for line in shutdown_lines[shutdown_signal_index:]
                    if re.search(r"\bERROR\b", line)]
                report["shutdownDiagnostics"] = {
                    "errorFree": not shutdown_error_lines,
                    "errorCount": len(shutdown_error_lines),
                    "errors": [process_protocol.redact_boot_line(line)
                               for line in shutdown_error_lines],
                    "attribution": (
                        "No shutdown ERROR log was observed after the shutdown signal."
                        if not shutdown_error_lines else
                        "The observed error follows frontend callback uninstall and "
                        "occurs during obs-websocket module unload; this report does "
                        "not attribute it to group composition."),
                }
                report["gracefulShutdownEvidence"] = shutdown_evidence
                frontend_callback_markers = {
                    "scriptingShutdownReceived": next((i for i, line in enumerate(shutdown_lines)
                        if "PULSAR_WEBSOCKET event=frontend_exit_received phase=scripting_shutdown" in line), None),
                    "exitReceived": next((i for i, line in enumerate(shutdown_lines)
                        if "PULSAR_WEBSOCKET event=frontend_exit_received phase=exit" in line), None),
                    "exitCleanupStarted": next((i for i, line in enumerate(shutdown_lines)
                        if "PULSAR_WEBSOCKET event=frontend_exit_cleanup phase=started" in line), None),
                    "exitCleanupComplete": next((i for i, line in enumerate(shutdown_lines)
                        if "PULSAR_WEBSOCKET event=frontend_exit_cleanup phase=complete" in line), None),
                    "duplicateCleanupSkipped": next((i for i, line in enumerate(shutdown_lines)
                        if "PULSAR_WEBSOCKET event=frontend_exit_cleanup phase=duplicate_skipped" in line), None),
                    "exitDetach": next((i for i, line in enumerate(shutdown_lines)
                        if "PULSAR_WEBSOCKET event=frontend_callback_detached phase=exit" in line), None),
                    "callbacksUninstalled": next((i for i, line in enumerate(shutdown_lines)
                        if "pulsar-frontend-stub | callbacks uninstalled" in line), None),
                    "websocketUnloadStarted": next((i for i, line in enumerate(shutdown_lines)
                        if "obs-websocket | [obs_module_unload] Shutting down..." in line), None),
                    "destructorSkip": next((i for i, line in enumerate(shutdown_lines)
                        if "PULSAR_WEBSOCKET event=frontend_callback_destructor_skip phase=already_detached" in line), None),
                    "websocketUnloadFinished": next((i for i, line in enumerate(shutdown_lines)
                        if "obs-websocket | [obs_module_unload] Finished shutting down." in line), None),
                }
                frontend_callback_lifecycle = {
                    "scriptShutdownThenExitRunsCleanupOnce": (
                        frontend_callback_markers["scriptingShutdownReceived"] is not None and
                        frontend_callback_markers["exitReceived"] is not None and
                        frontend_callback_markers["exitCleanupStarted"] is not None and
                        frontend_callback_markers["exitCleanupComplete"] is not None and
                        frontend_callback_markers["duplicateCleanupSkipped"] is not None and
                        frontend_callback_markers["scriptingShutdownReceived"] <
                        frontend_callback_markers["exitCleanupStarted"] <
                        frontend_callback_markers["exitCleanupComplete"] <
                        frontend_callback_markers["exitReceived"] <
                        frontend_callback_markers["duplicateCleanupSkipped"]),
                    "exitCleanupMarkerCountsAreExact": (
                        sum("PULSAR_WEBSOCKET event=frontend_exit_received phase=scripting_shutdown" in line
                            for line in shutdown_lines) == 1 and
                        sum("PULSAR_WEBSOCKET event=frontend_exit_received phase=exit" in line
                            for line in shutdown_lines) == 1 and
                        sum("PULSAR_WEBSOCKET event=frontend_exit_cleanup phase=started" in line
                            for line in shutdown_lines) == 1 and
                        sum("PULSAR_WEBSOCKET event=frontend_exit_cleanup phase=complete" in line
                            for line in shutdown_lines) == 1 and
                        sum("PULSAR_WEBSOCKET event=frontend_exit_cleanup phase=duplicate_skipped" in line
                            for line in shutdown_lines) == 1),
                    "exitCallbackDetachedBeforeTableUninstall": (
                        frontend_callback_markers["exitDetach"] is not None and
                        frontend_callback_markers["callbacksUninstalled"] is not None and
                        frontend_callback_markers["exitDetach"] <
                        frontend_callback_markers["callbacksUninstalled"]),
                    "destructorSkippedDuplicateRemovalAfterUninstall": (
                        frontend_callback_markers["callbacksUninstalled"] is not None and
                        frontend_callback_markers["websocketUnloadStarted"] is not None and
                        frontend_callback_markers["destructorSkip"] is not None and
                        frontend_callback_markers["websocketUnloadFinished"] is not None and
                        frontend_callback_markers["callbacksUninstalled"] <
                        frontend_callback_markers["websocketUnloadStarted"] <
                        frontend_callback_markers["destructorSkip"] <
                        frontend_callback_markers["websocketUnloadFinished"]),
                    "noMissingFrontendCallbackError": not shutdown_error_lines,
                    "markerIndexes": frontend_callback_markers,
                }
                frontend_callback_lifecycle["passed"] = all(
                    value for key, value in frontend_callback_lifecycle.items()
                    if key != "markerIndexes")
                report["frontendCallbackShutdownLifecycle"] = frontend_callback_lifecycle
                unload_cleanup_line = next(
                    (line for line in shutdown_lines
                     if "PULSAR_GROUP_COMPOSITION event=module_unload_cleanup_complete" in line),
                    None)
                unload_cleanup_match = (
                    re.search(
                        r"active_before=(\d+) fences_before=(\d+) "
                        r"pending_before=(\d+) pending_released=(\d+)",
                        unload_cleanup_line)
                    if unload_cleanup_line else None)
                report["compositionModuleUnloadCleanup"] = {
                    "markerObserved": unload_cleanup_line is not None,
                    "activeCompositionsBeforeCleanup": (
                        int(unload_cleanup_match.group(1))
                        if unload_cleanup_match else None),
                    "generationFencesBeforeCleanup": (
                        int(unload_cleanup_match.group(2))
                        if unload_cleanup_match else None),
                    "pendingRemovalEventsBeforeCleanup": (
                        int(unload_cleanup_match.group(3))
                        if unload_cleanup_match else None),
                    "pendingRemovalEventsReleased": (
                        int(unload_cleanup_match.group(4))
                        if unload_cleanup_match else None),
                }
                marker_indexes = {
                    "captureModuleLoad": next((i for i, line in enumerate(shutdown_lines)
                        if "PULSAR_GROUP_COMPOSITION event=module_load" in line), None),
                    "testHelperModuleLoad": next((i for i, line in enumerate(shutdown_lines)
                        if "PULSAR_GROUP_COMPOSITION_TEST_HELPER event=module_load" in line), None),
                    "testHelperUnloadRemoval": next((i for i, line in enumerate(shutdown_lines)
                        if "PULSAR_GROUP_COMPOSITION_TEST_HELPER event=module_unload_removed_source" in line), None),
                    "captureModuleUnloadCleanup": next((i for i, line in enumerate(shutdown_lines)
                        if "PULSAR_GROUP_COMPOSITION event=module_unload_cleanup_complete" in line), None),
                }
                load_order_valid = (
                    marker_indexes["captureModuleLoad"] is not None and
                    marker_indexes["testHelperModuleLoad"] is not None and
                    marker_indexes["testHelperModuleLoad"] > marker_indexes["captureModuleLoad"])
                unload_order_valid = (
                    marker_indexes["testHelperUnloadRemoval"] is not None and
                    marker_indexes["captureModuleUnloadCleanup"] is not None and
                    marker_indexes["testHelperUnloadRemoval"] < marker_indexes["captureModuleUnloadCleanup"])
                pending_event_proof = {
                    "testHelperLoadedAfterCapturePlugin": load_order_valid,
                    "testRemovalRanBeforeCapturePluginUnload": unload_order_valid,
                    "helperRemovalOnStoppedGraphicsThread": any(
                        "PULSAR_GROUP_COMPOSITION_TEST_HELPER event=module_unload_removed_source" in line
                        and "on_graphics_task=0 removed=1 global_signal_forwarded=1" in line
                        for line in shutdown_lines),
                    "pendingEventObservedAtCaptureUnload": (
                        report["compositionModuleUnloadCleanup"][
                            "pendingRemovalEventsBeforeCleanup"] == 1),
                    "pendingEventReleasedAtCaptureUnload": (
                        report["compositionModuleUnloadCleanup"][
                            "pendingRemovalEventsReleased"] == 1),
                    "markerIndexes": marker_indexes,
                }
                pending_event_proof["passed"] = all(
                    value for key, value in pending_event_proof.items()
                    if key != "markerIndexes")
                report["pendingRemovalUnloadProof"] = pending_event_proof
                report["boundedExitWithoutForcedTermination"] = (
                    report["ownedProcessExited"] and
                    report["gracefulShutdownRequested"] and
                    not report["forcedTerminationUsed"] and
                    shutdown_evidence["exitCode"] == 0)
                report["cleanShutdown"] = (
                    report["boundedExitWithoutForcedTermination"] and
                    report["shutdownDiagnostics"]["errorFree"])
                graceful_host_shutdown = (
                    report["cleanShutdown"] and
                    shutdown_evidence["eventAdopted"] and
                    shutdown_evidence["eventSignaled"] and
                    shutdown_evidence["hostShutdownReached"] and
                    shutdown_evidence["exitCode"] == 0 and
                    frontend_callback_lifecycle["passed"])
                graceful_plugin_shutdown = (
                    graceful_host_shutdown and
                    report["compositionModuleUnloadCleanup"]["markerObserved"])
                active_at_unload = bool(
                    report.get("probe", {}).get("sourceRemovalLifecycle", {})
                    .get("activeCompositionAtModuleUnload", {})
                    .get("leftActiveForShutdown"))
                if args.expect_browser_fence_failure:
                    fail_marker = "PULSAR_BROWSER_QUIESCE event=fail_closed_exit"
                    fail_index = next((i for i, line in enumerate(shutdown_lines)
                                       if fail_marker in line), None)
                    exit_code = process.proc.proc.returncode
                    forbidden_markers = (
                        "PULSAR_CEF_SHUTDOWN event=pre_obs_shutdown_begin",
                        "PULSAR_CEF_SHUTDOWN event=pre_obs_shutdown_complete",
                        "PULSAR_FRONTEND_CLEANUP event=",
                        "PULSAR_RUNTIME_INSTANCE runtime_dir_lease=released",
                        "PULSAR_RUNTIME_INSTANCE lease=released",
                        "PULSAR_LEGACY_ALIAS lease=released",
                        "pulsar-frontend-stub | callbacks uninstalled",
                        "obs-websocket | [obs_module_unload] Shutting down...",
                    )
                    no_teardown_after_failure = (
                        fail_index is not None and
                        not any(
                            marker in line
                            for line in shutdown_lines[fail_index + 1:]
                            for marker in forbidden_markers))
                    no_teardown_during_failed_shutdown = not any(
                        marker in line
                        for line in shutdown_lines[shutdown_signal_index:]
                        for marker in forbidden_markers)
                    no_leases_released = not any(
                        marker in line
                        for line in shutdown_lines
                        for marker in (
                            "PULSAR_RUNTIME_INSTANCE runtime_dir_lease=released",
                            "PULSAR_RUNTIME_INSTANCE lease=released",
                            "PULSAR_LEGACY_ALIAS lease=released"))
                    no_av = exit_code not in (0xC0000005, -1073741819)
                    browser_failure_evidence = {
                        "missingProcReasonObserved": any(
                            "browser_pre_shutdown_proc_unavailable" in line
                            for line in shutdown_lines),
                        "failClosedMarkerObserved": fail_index is not None,
                        "exitCode": exit_code,
                        "intentionalExitCodeOne": exit_code == 1,
                        "noAccessViolation": no_av,
                        "noForcedTermination": not process.forced_termination_used,
                        "shutdownEventWasSignaled": process.graceful_shutdown_requested,
                        "ownedProcessExited": report["ownedProcessExited"],
                        "runtimeLeasesHeldUntilProcessExit": no_leases_released,
                        "noFrontendLibobsTeardownDuringFailedShutdown": (
                            no_teardown_during_failed_shutdown),
                        "noTeardownAfterFailureMarker": no_teardown_after_failure,
                        "cefShutdownExercised": False,
                    }
                    browser_failure_evidence["passed"] = all(
                        value for key, value in browser_failure_evidence.items()
                        if key not in ("exitCode", "cefShutdownExercised")) and no_av
                    report["browserFenceFailureEvidence"] = browser_failure_evidence
                    report["gracefulHostShutdownWithoutComposition"] = False
                    report["activeCompositionCleanlyUnloaded"] = None
                    report["passed"] = bool(
                        report.get("passed") and browser_failure_evidence["passed"])
                    if not browser_failure_evidence["passed"]:
                        report["shutdownError"] = {
                            "type": "BrowserFenceFailClosedProofFailed",
                            "message": "the missing-proc path did not meet the expected bounded process-exit contract"}
                elif args.shutdown_control_only:
                    report["gracefulHostShutdownWithoutComposition"] = graceful_plugin_shutdown
                    report["activeCompositionCleanlyUnloaded"] = None
                    if report.get("passed") and not graceful_plugin_shutdown:
                        report["passed"] = False
                        report["shutdownError"] = {
                            "type": "GracefulShutdownControlFailed",
                            "message": "private host and capture-effects module did not finish graceful shutdown without a composition"}
                else:
                    active_count_at_module_unload = report[
                        "compositionModuleUnloadCleanup"][
                            "activeCompositionsBeforeCleanup"]
                    clean_unload = (
                        graceful_plugin_shutdown and
                        active_count_at_module_unload is not None and
                        active_count_at_module_unload > 0)
                    report["activeCompositionCleanlyUnloaded"] = clean_unload
                    report["activeCompositionDisposedBeforeModuleUnload"] = (
                        active_at_unload and graceful_plugin_shutdown and
                        active_count_at_module_unload == 0)
                    report["gracefulShutdownAfterActiveComposition"] = (
                        active_at_unload and graceful_plugin_shutdown)
                    if report.get("passed") and not report["gracefulShutdownAfterActiveComposition"]:
                        report["passed"] = False
                        report["shutdownError"] = {
                            "type": "GracefulUnloadEvidenceMissing",
                            "message": "a run that created an active composition did not finish through an error-free obs_shutdown"}
                    if report.get("passed") and not pending_event_proof["passed"]:
                        report["passed"] = False
                        report["shutdownError"] = {
                            "type": "PendingRemovalUnloadProofMissing",
                            "message": "the test-only source removal was not observed and released at the capture plugin unload boundary"}
            else:
                report["ownedProcessExited"] = True
        finally:
            for key in list(os.environ):
                if key.startswith("PULSAR_"):
                    del os.environ[key]
            os.environ.update(previous_env)
            report["scratchRemoved"] = not scratch.exists()
            if scratch.exists() and report["ownedProcessExited"]:
                resolved_scratch = scratch.resolve()
                resolved_cache = cache.resolve()
                if resolved_scratch == resolved_cache or not resolved_scratch.is_relative_to(resolved_cache):
                    report["scratchCleanupError"] = {
                        "type": "PathContainmentError",
                        "message": "refused cleanup outside the owned worktree cache"}
                else:
                    try:
                        shutil.rmtree(resolved_scratch)
                    except OSError as error:
                        report["scratchCleanupError"] = {
                            "type": type(error).__name__, "message": str(error)[:1000]}
                    report["scratchRemoved"] = not scratch.exists()
            elif scratch.exists():
                report["scratchCleanupError"] = {
                    "type": "OwnedProcessStillRunning",
                    "message": "scratch retained because owned process has not exited"}
            report["finishedUtc"] = datetime.now(timezone.utc).isoformat()
            report["limits"] = (
                "Private synthetic GPU readback only. Protocol /2 group and capture masks, "
                "all 16 admitted CSS blend modes, root appearance, dynamic atlas sampling, "
                "atomic refusal and reset are exercised; the /1 plugin is the negative "
                "control. The atlas is a synthetic image input, not a Solar CEF source or "
                "physical capture device. No public output or scene transition is exercised. "
                "The private no-browser shutdown fixture does not exercise CEF teardown."
                if not args.expect_browser_fence_failure else
                "Private synthetic no-browser missing-proc lifecycle probe only; no real "
                "browser module/source is loaded, so this does not exercise CEF teardown.")
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({"report": str(report_path), "passed": report["passed"],
                              "ownedProcessExited": report["ownedProcessExited"],
                              "scratchRemoved": report["scratchRemoved"],
                              "scratchCleanupError": report.get("scratchCleanupError")}))


if __name__ == "__main__":
    main()
