"""Private native world-grid readback plus the unchanged legacy composition probe.

Uses the existing private-host runner/cleanup and wire helpers. No physical
capture, shared Prism instance, scene switch, public output or transition.
"""
import asyncio
import base64
import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import uuid
from datetime import datetime, timezone

from PIL import Image
import websockets

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "legacy_group_probe", ROOT / "scripts/probe-group-composition.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


async def world_exercise(config, output, runtime_id, scratch):
    protocol = legacy.load("world_wire", "probe-source-kinds.py")
    token = uuid.uuid4().hex[:10]
    scene = f"global-texel-proof-{token}"
    atlas_name, capture_name = f"world-atlas-{token}", f"world-capture-{token}"
    atlas_path, capture_path = scratch / "world-atlas.png", scratch / "world-capture.png"
    atlas = Image.new("RGBA", (128, 64))
    for y in range(64):
        for x in range(64):
            atlas.putpixel((x, y), (200 if (x + y) % 2 == 0 else 100, 0, 0, 255))
            atlas.putpixel((x + 64, y), (0, 0, 200, 128))
    atlas.save(atlas_path)
    capture = Image.new("RGBA", (16, 32), (200, 0, 0, 255))
    capture.paste((0, 200, 0, 255), (0, 8, 16, 24))
    capture.save(capture_path)
    images, generations, refusals = [], [], []
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

        async def call(kind, data=None):
            reply = await protocol.request(inbox, ws, kind, uuid.uuid4().hex, data)
            assert reply.get("requestStatus", {}).get("result"), reply
            return reply.get("responseData", {})

        async def commit(payload):
            reply = await call("CallVendorRequest", {
                "vendorName": "pulsar-capture-effects",
                "requestType": "CommitGroupComposition", "requestData": payload})
            return reply["responseData"]

        async def readback(label, viewport):
            reply = await call("GetSourceScreenshot", {
                "sourceName": scene, "imageFormat": "png",
                "imageWidth": viewport[0], "imageHeight": viewport[1]})
            raw = base64.b64decode(reply["imageData"].split(",", 1)[1])
            path = output / f"{stamp}-{token}-world-{label}.png"
            path.write_bytes(raw)
            images.append({"phase": label, "path": path.name,
                           "sha256": hashlib.sha256(raw).hexdigest()})
            return Image.open(io.BytesIO(raw)).convert("RGBA")

        settings = await call("GetVideoSettings")
        viewport = (int(settings["baseWidth"]), int(settings["baseHeight"]))
        width, height = viewport
        root_id = legacy.ROOT_GROUP_ID
        zero_crop = {"left": 0, "right": 0, "top": 0, "bottom": 0}
        active_generation = None
        try:
            await call("CreateScene", {"sceneName": scene})
            ids = []
            for name, path in [(atlas_name, atlas_path), (capture_name, capture_path)]:
                result = await call("CreateInput", {
                    "sceneName": scene, "inputName": name, "inputKind": "image_source",
                    "inputSettings": {"file": str(path)}, "sceneItemEnabled": True})
                ids.append(result["sceneItemId"])
            atlas_id, capture_id = ids
            # Wait for image dimensions; this is fixture readiness, not a
            # retry/reload of the actual scene or a capture mutation.
            for _ in range(80):
                try:
                    result = await call("GetSceneItemTransform", {
                        "sceneName": scene, "sceneItemId": atlas_id})
                    if result["sceneItemTransform"]["sourceWidth"] == 128:
                        break
                finally:
                    await asyncio.sleep(0.025)
            groups = [
                {"id": root_id, "parentId": None, "order": 0, "opacity": 1,
                 "visible": True, "blendMode": "normal", "mask": None,
                 "bounds": {"x": 0, "y": 0, "width": width, "height": height},
                 "affine": legacy.affine(), "children": [{"kind": "group", "id": "owner"}]},
                {"id": "owner", "parentId": root_id, "order": 0, "opacity": 1,
                 "visible": True, "blendMode": "normal", "mask": None,
                 "bounds": {"x": 0, "y": 0, "width": 16, "height": 32},
                 # Already rasterized bands MUST ignore even this shear/translation.
                 "affine": {"m00": 2, "m01": 0.5, "tx": 600,
                            "m10": 0, "m11": 2, "ty": 400},
                 "children": [
                     {"kind": "solarBand", "sceneItemId": atlas_id, "band": 0,
                      "affine": legacy.affine(), "crop": {**zero_crop, "right": 64},
                      "rect": {"x": 16, "y": 20, "width": 64, "height": 64}},
                     {"kind": "capture", "sceneItemId": capture_id,
                      "affine": {"m00": 2, "m01": 0.5, "tx": 40,
                                 "m10": 0, "m11": 2, "ty": 40},
                      "crop": {**zero_crop, "top": 8, "bottom": 8},
                      "localOpacity": 1, "visible": False},
                     {"kind": "solarBand", "sceneItemId": atlas_id, "band": 1,
                      "affine": legacy.affine(), "crop": {**zero_crop, "left": 64},
                      "rect": {"x": 32, "y": 20, "width": 64, "height": 64}},
                 ]},
            ]

            def payload(generation):
                wire = legacy.make_wire_plan(scene, runtime_id, generation, copy.deepcopy(groups), viewport)
                wire["coordinateSpace"] = "global-scene-texels"
                wire["planHash"] = hashlib.sha256(json.dumps(
                    wire, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                return wire

            first = payload(1)
            reply = await commit(first)
            assert reply.get("ok"), reply
            active_generation = 1
            image = await readback("exact-grid", viewport)
            exact = 0
            for y in range(20, 84):
                for x in range(16, 32):
                    expected = atlas.getpixel((x - 16, y - 20))
                    assert image.getpixel((x, y)) == expected, (x, y, image.getpixel((x, y)), expected)
                    exact += 1
            assert image.getpixel((620, 420)) == (0, 0, 0, 0), "owner affine applied twice"
            generations.append({"generation": 1, "exactCheckerboardPixels": exact})

            groups[1]["opacity"] = 0.5
            groups[1]["children"][1]["visible"] = True
            reply = await commit(payload(2))
            assert reply.get("ok"), reply
            active_generation = 2
            mixed = await readback("mixed-shear-opacity", viewport)
            value = mixed.getpixel((60, 55))
            legacy.assert_pixel_near(value, (0, 50, 50, 127), "capture shear, crop and once-only opacity", 1)
            generations.append({"generation": 2, "sample": list(value)})

            # Nested owners retain their hierarchy, but never transform scene texels.
            groups[0]["children"] = [{"kind": "group", "id": "outer"}]
            groups[1]["parentId"] = "outer"
            groups.append({"id": "outer", "parentId": root_id, "order": 0,
                           "opacity": 0.5, "visible": True, "blendMode": "normal", "mask": None,
                           "bounds": {"x": 0, "y": 0, "width": 16, "height": 32},
                           "affine": {"m00": 1, "m01": 0, "tx": 900,
                                      "m10": 0, "m11": 1, "ty": 600},
                           "children": [{"kind": "group", "id": "owner"}]})
            reply = await commit(payload(3))
            assert reply.get("ok"), reply
            active_generation = 3
            nested = await readback("nested-opacity", viewport)
            legacy.assert_pixel_near(nested.getpixel((60, 55)), (0, 25, 25, 64),
                                     "each nested owner opacity exactly once", 1)
            generations.append({"generation": 3, "sample": list(nested.getpixel((60, 55)))})

            groups[1]["children"][1]["affine"] = {
                "m00": 0, "m01": -2, "tx": 140, "m10": 2, "m11": 0, "ty": 40}
            reply = await commit(payload(4))
            assert reply.get("ok"), reply
            active_generation = 4
            mixed = await readback("rotated-crop", viewport)
            legacy.assert_pixel_near(mixed.getpixel((120, 55)), (0, 50, 0, 64),
                                     "rotated cropped capture with two owner opacities", 1)
            assert mixed.getpixel((104, 55)) == (0, 0, 0, 0), "source-space crop leaked"
            generations.append({"generation": 4, "sample": list(mixed.getpixel((120, 55)))})

            for label, mutate, expected_error in [
                ("unknown-space", lambda p: p.update(coordinateSpace="unknown"), "unsupported-coordinate-space"),
                ("scaled-band", lambda p: p["groups"][1]["children"][0]["rect"].update(width=63), "global-band-resampling"),
                ("fractional-band", lambda p: p["groups"][1]["children"][0]["rect"].update(x=16.25), "global-band-resampling"),
                ("float-overflow", lambda p: p["groups"][1]["children"][1]["affine"].update(m01=1e100), "invalid-affine"),
            ]:
                invalid = payload(5)
                mutate(invalid)
                reply = await commit(invalid)
                assert not reply.get("ok") and expected_error in reply.get("error", ""), reply
                preserved = await readback(label, viewport)
                assert preserved.tobytes() == mixed.tobytes(), f"failed candidate changed pixels: {label}"
                refusals.append({"case": label, "error": reply["error"], "pixelsPreserved": True})
            # Changing coordinate semantics cannot be reported as an idempotent replay.
            conflicting = payload(4)
            conflicting["coordinateSpace"] = "owner-local"
            for group in conflicting["groups"]:
                group["affine"] = legacy.affine()
                group["bounds"] = {"x": 0, "y": 0, "width": width, "height": height}
                for child in group["children"]:
                    if child["kind"] == "capture":
                        child["affine"] = legacy.affine()
            conflicting["planHash"] = payload(4)["planHash"]
            reply = await commit(conflicting)
            assert not reply.get("ok") and "stale-generation" in reply.get("error", ""), reply
            refusals.append({"case": "same-generation-different-space", "error": reply["error"]})
            return {"coordinateSpace": "global-scene-texels", "viewport": list(viewport),
                    "generations": generations, "refusals": refusals, "images": images,
                    "capture": "synthetic image with vertical source-space crop; full affine shear",
                    "physicalPresentation": "not-measured", "passed": True}
        finally:
            if active_generation is not None:
                reset = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects", "requestType": "ResetGroupComposition",
                    "requestData": {"sceneName": scene, "generation": active_generation,
                                    "runtimeInstanceId": runtime_id}})
                assert reset["responseData"].get("ok"), reset
            for kind, data in [("RemoveScene", {"sceneName": scene}),
                               ("RemoveInput", {"inputName": atlas_name}),
                               ("RemoveInput", {"inputName": capture_name})]:
                await call(kind, data)


if __name__ == "__main__":
    # Reuse the full native legacy regression/lifecycle suite unchanged, then
    # let its existing runner retain its shutdown fixture and own every cleanup.
    original = legacy.exercise

    async def combined(config, output, runtime_id, scratch):
        world = await world_exercise(config, output, runtime_id, scratch)
        previous = await original(config, output, runtime_id, scratch)
        previous["globalSceneTexels"] = world
        return previous

    legacy.exercise = combined
    legacy.EVIDENCE = ROOT / "evidence/local-20261016-global-texel-composition/eleven"
    legacy.CACHE = ROOT / ".cache/global-texel-composition"
    legacy.main()
