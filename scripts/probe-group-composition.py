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

from PIL import Image
import websockets

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence" / "local-20261015-native-group-composition" / "eleven"
CACHE = ROOT / ".cache" / "native-group-composition"
ROOT_GROUP_ID = "prism.native-composition.root.v1"


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
                   replaced_scene_items: list[int] | None = None):
    replaced_scene_items = sorted(replaced_scene_items or [])
    native_plan = {
        "sourcePlanHash": hashlib.sha256(b"private-native-group-proof-v1").hexdigest(),
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "groups": groups,
        "replacedSceneItems": replaced_scene_items,
    }
    encoded = json.dumps(native_plan, sort_keys=True, separators=(",", ":")).encode()
    return {"protocol": "pulsar.group-composition/1", "sceneName": scene,
            "generation": generation, "runtimeInstanceId": runtime_id,
            "planHash": hashlib.sha256(encoded).hexdigest(), "groups": groups,
            "replacedSceneItems": [
                {"sceneItemId": item_id} for item_id in replaced_scene_items]}


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


def make_atlas_fixture(path: pathlib.Path):
    atlas = Image.new("RGBA", (1920, 1080), (0, 255, 255, 255))
    atlas.paste((255, 0, 0, 255), (700, 350, 760, 410))
    atlas.paste((0, 255, 0, 255), (900, 350, 960, 410))
    atlas.save(path)


async def exercise(config: dict, output: pathlib.Path, runtime_id: str,
                   scratch: pathlib.Path):
    protocol = load("group_comp_wire", "probe-source-kinds.py")
    token = uuid.uuid4().hex[:10]
    scene = f"native-group-proof-{token}"
    appearance_scene = f"native-appearance-{token}"
    offset_scene = f"native-offset-{token}"
    solar_scene = f"native-solar-band-{token}"
    mixed_scene = f"native-mixed-composition-{token}"
    lifecycle_scene = f"native-remove-dependency-{token}"
    same_name_scene = f"native-remove-scene-{token}"
    unload_scene = f"native-unload-active-{token}"
    red_name = f"native-group-red-{token}"
    blue_name = f"native-group-blue-{token}"
    offset_name = f"native-offset-source-{token}"
    atlas_name = f"native-solar-atlas-{token}"
    legacy_name = f"native-solar-legacy-{token}"
    mixed_atlas_name = f"native-mixed-atlas-{token}"
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
    make_atlas_fixture(atlas_path)
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
            ]:
                try:
                    await call(kind, data)
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=pathlib.Path, required=True,
                        help="Compatible prebuilt runtime rundir/RelWithDebInfo")
    parser.add_argument("--plugin", type=pathlib.Path, required=True,
                        help="Private pulsar-capture-effects.dll")
    parser.add_argument("--websocket-plugin", type=pathlib.Path, required=True,
                        help="Private obs-websocket.dll built from this checkout")
    parser.add_argument("--graphics-remove-helper", type=pathlib.Path, required=True,
                        help="Private helper for graphics-task and module-unload source removal tests")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument(
        "--shutdown-control-only", action="store_true",
        help="start the same private host but do not create any composition")
    args = parser.parse_args()
    runtime_source = args.runtime.resolve()
    plugin_source = args.plugin.resolve()
    websocket_source = args.websocket_plugin.resolve()
    graphics_remove_helper_source = args.graphics_remove_helper.resolve()
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
    report_type = ("group-composition-shutdown-control"
                   if args.shutdown_control_only else "group-composition-probe")
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
            "enabled": True,
            "mode": "private-test-helper-no-browser-source-fence",
            "browserModulesInRuntime": browser_modules,
            "helperCheck": (
                "enumerates OBS sources when the host requests the browser fence and "
                "returns success only when no browser_source/browser_source_v2 exists"),
            "cefShutdownExercised": False,
        }
        for directory in ("bin", "data", "obs-plugins"):
            shutil.copytree(runtime_source / directory, runtime / directory)
        exe = runtime / "bin" / "64bit" / "pulsar.exe"
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
            "jsonBridgeBuiltFrom": "the supplied private obs-websocket.dll, built by this unit from this checkout",
        }
        upstream_root = runtime_source.parents[2]
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
            "PULSAR_TEST_NO_BROWSER_PRE_SHUTDOWN": "1",
        })
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
            "browserPreShutdownFixture": "test-only no-browser source assertion",
            "result": "no WASAPI endpoint initialized during private bootstrap",
        }
        if args.shutdown_control_only:
            report["shutdownControl"] = {
                "compositionCreated": False,
                "reason": (
                    "compare host bootstrap/shutdown without group ownership; a "
                    "test-only fence attests no browser source is loaded, so CEF "
                    "shutdown is outside this control"),
            }
        else:
            report["probe"] = asyncio.run(exercise(
                {"server_port": port, "server_password": password}, output,
                runtime_id, scratch))
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
                if args.shutdown_control_only:
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
                "Private synthetic GPU readback only; SolarBand source crops and owner-local "
                "placement are covered, masks remain explicitly unsupported, and no physical "
                "capture, public output, transition or switch is exercised. The private "
                "no-browser shutdown fixture does not exercise CEF teardown")
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({"report": str(report_path), "passed": report["passed"],
                              "ownedProcessExited": report["ownedProcessExited"],
                              "scratchRemoved": report["scratchRemoved"],
                              "scratchCleanupError": report.get("scratchCleanupError")}))


if __name__ == "__main__":
    main()
