#!/usr/bin/env python3
"""Exercise capture parent and coverage lifetimes in the owned private native host.

This extends probe-group-composition.py's existing private-host bootstrap and
shutdown. It uses synthetic image/color sources only; it never starts Live, REC,
CEF, or a physical capture device. Pass the canonical evidence directory as
--output and the same runtime/plugin/helper arguments as the group probe.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import pathlib
import time
import uuid
from datetime import datetime, timezone

from PIL import Image
import websockets


ROOT = pathlib.Path(__file__).resolve().parent.parent
SIZE = (64, 64)
CENTER = (32, 32)


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def distance(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    return max(abs(left - right) for left, right in zip(a, b))


async def run(config: dict, output: pathlib.Path, scratch: pathlib.Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    fixtures = scratch / "capture-parent-lifecycle-fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:10]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"{stamp}-{token}-capture-parent"

    transparent_path = fixtures / f"{token}-transparent.png"
    left_path = fixtures / f"{token}-left-alpha.png"
    right_path = fixtures / f"{token}-right-alpha.png"
    Image.new("RGBA", SIZE, (255, 255, 255, 0)).save(transparent_path)
    for path, opaque_left in ((left_path, True), (right_path, False)):
        mask = Image.new("RGBA", SIZE, (255, 255, 255, 0))
        start = 0 if opaque_left else SIZE[0] // 2
        for x in range(start, start + SIZE[0] // 2):
            for y in range(SIZE[1]):
                mask.putpixel((x, y), (255, 255, 255, 255))
        mask.save(path)

    weak_layer = f"parent-weak-layer-{token}"
    transparent_name = f"parent-transparent-{token}"
    requested_parent = f"parent-requested-{token}"
    renamed_parent = f"parent-renamed-{token}"
    red_name = f"parent-red-{token}"
    blue_name = f"parent-blue-{token}"
    green_name = f"parent-green-{token}"
    weak_filter = f"parent-weak-filter-{token}"
    composite = f"coverage-composite-{token}"
    capture = f"ZabCapture:parent-{token}"
    layer = f"{capture}:appearance:proof"
    mask_owner = f"coverage-masks-{token}"
    left_name = f"coverage-left-{token}"
    right_name = f"coverage-right-{token}"
    captures: list[dict] = []

    protocol = load("capture_parent_protocol", "probe-source-kinds.py")
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

        async def screenshot(source: str) -> tuple[Image.Image, bytes]:
            response = await call("GetSourceScreenshot", {
                "sourceName": source, "imageFormat": "png",
                "imageWidth": SIZE[0], "imageHeight": SIZE[1],
            })
            data = base64.b64decode(response["imageData"].split(",", 1)[1])
            from io import BytesIO
            image = Image.open(BytesIO(data)).convert("RGBA")
            assert image.size == SIZE, (source, image.size)
            return image, data

        def preserve(phase: str, image: Image.Image, data: bytes) -> dict:
            path = output / f"{prefix}-{phase}.png"
            path.write_bytes(data)
            item = {
                "phase": phase, "file": path.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "left": list(image.getpixel((16, 32))),
                "center": list(image.getpixel(CENTER)),
                "right": list(image.getpixel((48, 32))),
            }
            captures.append(item)
            return item

        async def expect_pixel(phase: str, source: str, predicate,
                               timeout: float = 3.0) -> dict:
            deadline = time.monotonic() + timeout
            while True:
                image, data = await screenshot(source)
                if predicate(image):
                    return preserve(phase, image, data)
                if time.monotonic() >= deadline:
                    observed = preserve(f"{phase}-failed", image, data)
                    raise AssertionError(f"{phase}: unexpected pixels {observed}")
                await asyncio.sleep(0.08)

        async def vendor_commit(settings: dict) -> dict:
            result = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitAppearance",
                "requestData": {
                    "sceneName": composite, "layer": layer, "inputName": capture,
                    "itemId": outer_id, "innerId": inner_id,
                    "boundsType": "OBS_BOUNDS_STRETCH",
                    "blendMode": "OBS_BLEND_NORMAL", "visible": True,
                    "settings": settings,
                },
            })
            return result["responseData"]

        try:
            kinds = (await call("GetSourceFilterKindList"))["sourceFilterKinds"]
            assert "pulsar_capture_effects" in kinds, kinds

            # The filter is installed while its requested parent name does not
            # exist. A transparent foreground makes the sampled backdrop the
            # only possible source of a nontransparent centre pixel.
            await call("CreateScene", {"sceneName": weak_layer})
            await call("CreateInput", {
                "sceneName": weak_layer, "inputName": transparent_name,
                "inputKind": "image_source",
                "inputSettings": {"file": str(transparent_path)},
                "sceneItemEnabled": True,
            })
            await call("CreateSourceFilter", {
                "sourceName": weak_layer, "filterName": weak_filter,
                "filterKind": "pulsar_capture_effects",
                "filterSettings": {
                    "width": 64, "height": 64, "padding": 0,
                    "background_scene": requested_parent,
                    "effects": [{"kind": 4, "radius": 0}],
                },
            })
            empty = lambda image: image.getpixel(CENTER)[3] <= 8
            await expect_pixel("weak-parent-initially-absent", weak_layer, empty)

            async def create_parent(scene: str, source: str, color: int) -> tuple[int, ...]:
                await call("CreateScene", {"sceneName": scene})
                await call("CreateInput", {
                    "sceneName": scene, "inputName": source,
                    "inputKind": "color_source_v3",
                    "inputSettings": {"color": color, "width": 64, "height": 64},
                    "sceneItemEnabled": True,
                })
                reference, data = await screenshot(source)
                preserve(f"reference-{source}", reference, data)
                value = reference.getpixel(CENTER)
                assert value[3] >= 240, (source, value)
                return value

            red = await create_parent(requested_parent, red_name, 0xFFFF0000)
            await expect_pixel("weak-parent-created-red", weak_layer,
                               lambda image: distance(image.getpixel(CENTER), red) <= 24)
            await call("SetSceneName", {
                "sceneName": requested_parent, "newSceneName": renamed_parent,
            })
            await asyncio.sleep(0.25)  # cross several video ticks after rename
            await expect_pixel("weak-parent-renamed-still-red", weak_layer,
                               lambda image: distance(image.getpixel(CENTER), red) <= 24)

            blue = await create_parent(requested_parent, blue_name, 0xFF0000FF)
            assert distance(red, blue) >= 100, (red, blue)
            await asyncio.sleep(0.25)
            await expect_pixel("weak-parent-new-name-competitor-still-red", weak_layer,
                               lambda image: distance(image.getpixel(CENTER), red) <= 24)
            await call("RemoveScene", {"sceneName": renamed_parent})
            await expect_pixel("weak-parent-removed-switches-blue", weak_layer,
                               lambda image: distance(image.getpixel(CENTER), blue) <= 24)
            await call("RemoveScene", {"sceneName": requested_parent})
            await expect_pixel("weak-parent-removed-again-empty", weak_layer, empty)
            green = await create_parent(requested_parent, green_name, 0xFF00FF00)
            assert distance(green, red) >= 100 and distance(green, blue) >= 100
            await expect_pixel("weak-parent-recreated-green", weak_layer,
                               lambda image: distance(image.getpixel(CENTER), green) <= 24)

            # A complete CommitAppearance snapshot replaces a strong coverage
            # source. Removing the first mask before replacement makes the old
            # Effect reference its final owner during the scene transaction.
            await call("CreateScene", {"sceneName": composite})
            await call("CreateScene", {"sceneName": layer})
            await call("CreateScene", {"sceneName": mask_owner})
            inner = await call("CreateInput", {
                "sceneName": layer, "inputName": capture,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF33CC55, "width": 64, "height": 64},
                "sceneItemEnabled": True,
            })
            inner_id = int(inner["sceneItemId"])
            outer = await call("CreateSceneItem", {
                "sceneName": composite, "sourceName": layer,
                "sceneItemEnabled": True,
            })
            outer_id = int(outer["sceneItemId"])
            for source, path in ((left_name, left_path), (right_name, right_path)):
                await call("CreateInput", {
                    "sceneName": mask_owner, "inputName": source,
                    "inputKind": "image_source",
                    "inputSettings": {"file": str(path)},
                    "sceneItemEnabled": False,
                })
            for source, opaque_left in ((left_name, True), (right_name, False)):
                mask_image, mask_data = await screenshot(source)
                preserve(f"reference-{source}", mask_image, mask_data)
                assert (mask_image.getpixel((16, 32))[3] >= 240) == opaque_left
                assert (mask_image.getpixel((48, 32))[3] >= 240) != opaque_left
            foreground, foreground_data = await screenshot(capture)
            preserve("reference-coverage-foreground", foreground, foreground_data)
            foreground_pixel = foreground.getpixel(CENTER)
            assert foreground_pixel[3] >= 240, foreground_pixel

            def coverage_settings(mask_name: str, opacity: float = 1.0) -> dict:
                return {
                    "width": 64, "height": 64, "padding": 0,
                    "opacity": opacity, "background_scene": composite,
                    "effects": [], "coverage_source_name": mask_name,
                    "coverage_crop_left": 0, "coverage_crop_top": 0,
                    "coverage_crop_right": 0, "coverage_crop_bottom": 0,
                    "coverage_rect_x": 0, "coverage_rect_y": 0,
                    "coverage_rect_width": 64, "coverage_rect_height": 64,
                }

            def half_mask(image: Image.Image, opaque_left: bool) -> bool:
                left = image.getpixel((16, 32))
                right = image.getpixel((48, 32))
                opaque, clear = (left, right) if opaque_left else (right, left)
                return (opaque[3] >= 240 and clear[3] <= 8 and
                        distance(opaque, foreground_pixel) <= 24)

            first = await vendor_commit(coverage_settings(left_name))
            assert first.get("ok") is True, first
            await expect_pixel("coverage-first-left", layer,
                               lambda image: half_mask(image, True))
            await call("RemoveInput", {"inputName": left_name})
            second = await vendor_commit(coverage_settings(right_name))
            assert second.get("ok") is True, second
            await expect_pixel("coverage-replaced-right", layer,
                               lambda image: half_mask(image, False))

            await call("CreateSourceFilter", {
                "sourceName": layer, "filterName": "prism/capture-rounded",
                "filterKind": "color_filter_v2",
                "filterSettings": {"opacity": 1},
            })
            def right_half_alpha(image: Image.Image) -> bool:
                return (image.getpixel((16, 32))[3] <= 8 and
                        image.getpixel((48, 32))[3] >= 240)

            before_collision = await expect_pixel("coverage-before-retired-collision", layer,
                                                  right_half_alpha)
            refused_retired = await vendor_commit(coverage_settings(right_name, 0))
            assert not refused_retired.get("ok") and "collision" in refused_retired.get("error", ""), refused_retired
            after_collision = await expect_pixel("coverage-after-retired-collision", layer,
                                                 right_half_alpha)
            assert before_collision["left"] == after_collision["left"]
            assert before_collision["right"] == after_collision["right"]
            names = [item["filterName"] for item in (await call(
                "GetSourceFilterList", {"sourceName": layer}))["filters"]]
            assert names.count("prism/capture-effects") == 1 and names.count("prism/capture-rounded") == 1, names
            await call("RemoveSourceFilter", {
                "sourceName": layer, "filterName": "prism/capture-rounded"})

            await call("RemoveSourceFilter", {
                "sourceName": layer, "filterName": "prism/capture-effects"})
            await call("CreateSourceFilter", {
                "sourceName": layer, "filterName": "prism/capture-effects",
                "filterKind": "color_filter_v2",
                "filterSettings": {"opacity": 1},
            })
            canonical_before_image, canonical_before_data = await screenshot(layer)
            preserve("coverage-before-canonical-collision", canonical_before_image,
                     canonical_before_data)
            assert canonical_before_image.getchannel("A").getextrema()[1] >= 240
            assert distance(canonical_before_image.getpixel((0, 0)),
                            foreground_pixel) <= 24
            refused_canonical = await vendor_commit(coverage_settings(right_name, 0))
            assert not refused_canonical.get("ok") and "collision" in refused_canonical.get("error", ""), refused_canonical
            canonical_after_image, canonical_after_data = await screenshot(layer)
            preserve("coverage-after-canonical-collision", canonical_after_image,
                     canonical_after_data)
            assert canonical_before_image.tobytes() == canonical_after_image.tobytes(), refused_canonical
            names = [item["filterName"] for item in (await call(
                "GetSourceFilterList", {"sourceName": layer}))["filters"]]
            assert names == ["prism/capture-effects"], names

            report = {
                "passed": True,
                "scope": "synthetic private-host GPU pixels; no Live, REC, CEF or physical capture",
                "weakParent": {
                    "initiallyAbsent": True, "createdWithoutFilterMutation": True,
                    "renameKeepsSameWeakParent": True,
                    "oldNameCompetitorDoesNotStealBinding": True,
                    "removedParentSwitchesToNewSource": True,
                    "absenceAndRecreationRecover": True,
                    "redReference": list(red), "blueReference": list(blue),
                    "greenReference": list(green),
                },
                "coverage": {
                    "firstCommit": first, "replacementCommit": second,
                    "firstMaskRemovedBeforeReplacement": True,
                    "retiredFilterCollision": refused_retired,
                    "canonicalFilterCollision": refused_canonical,
                },
                "captures": captures,
            }
            report_path = output / f"{prefix}-proof.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            report["file"] = report_path.name
            return report
        finally:
            for kind, data in (
                ("RemoveSourceFilter", {"sourceName": weak_layer, "filterName": weak_filter}),
                ("RemoveSourceFilter", {"sourceName": layer, "filterName": "prism/capture-effects"}),
                ("RemoveSourceFilter", {"sourceName": layer, "filterName": "prism/capture-rounded"}),
                ("RemoveScene", {"sceneName": renamed_parent}),
                ("RemoveScene", {"sceneName": requested_parent}),
                ("RemoveScene", {"sceneName": weak_layer}),
                ("RemoveScene", {"sceneName": composite}),
                ("RemoveScene", {"sceneName": layer}),
                ("RemoveScene", {"sceneName": mask_owner}),
                ("RemoveInput", {"inputName": transparent_name}),
                ("RemoveInput", {"inputName": red_name}),
                ("RemoveInput", {"inputName": blue_name}),
                ("RemoveInput", {"inputName": green_name}),
                ("RemoveInput", {"inputName": capture}),
                ("RemoveInput", {"inputName": left_name}),
                ("RemoveInput", {"inputName": right_name}),
            ):
                try:
                    await call(kind, data)
                except Exception:
                    pass


if __name__ == "__main__":
    native = load("parent_lifecycle_native_host", "probe-group-composition.py")
    original = native.exercise

    async def combined(config, output, runtime_id, scratch,
                       expect_v2_rejection=False, prism_wire_emitter=None):
        parent = await run(config, output, scratch)
        result = await original(config, output, runtime_id, scratch,
                                expect_v2_rejection, prism_wire_emitter)
        result["captureParentLifecycle"] = parent
        return result

    native.exercise = combined
    output_parser = argparse.ArgumentParser(add_help=False)
    output_parser.add_argument("--output", type=pathlib.Path, required=True)
    output_args, _ = output_parser.parse_known_args()
    native.EVIDENCE = output_args.output.resolve()
    # libobs probes modules through bin/64bit/../../obs-plugins. Keep the staged
    # path short enough for the Windows module loader including that suffix.
    native.CACHE = ROOT / ".cache" / "parent-lifecycle"
    native.main()
