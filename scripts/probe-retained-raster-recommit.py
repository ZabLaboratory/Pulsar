#!/usr/bin/env python3
"""Private native regression for identical and translation-only recommits.

Synthetic image sources only. Copies an explicit runtime into this worktree's
cache, replaces its capture DLL, checks root identity and exact GPU pixels, then
gracefully stops the owned process. Never uses Prism or starts an output.
"""
import argparse
import asyncio
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import shutil
from datetime import datetime, timezone
from urllib.parse import urlsplit

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = load("retained_process", "probe-configurable-transition.py")
legacy = load("retained_plan", "probe-group-composition.py")


async def exercise(proc, output, fixtures, report, nested_depth=0):
    proc.spawn()
    ready = await asyncio.to_thread(proc.wait_for, helpers.process.READY_RE, 45)
    uri, password = ready.groups()
    async with helpers.websockets.connect(uri) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion": 1, "eventSubscriptions": 0}
        if "authentication" in hello:
            auth = hello["authentication"]
            secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        await ws.send(json.dumps({"op": 1, "d": identify}))
        assert json.loads(await ws.recv())["op"] == 2
        wire = helpers.Wire(ws)
        runtime = (await wire.vendor("GetState", vendor="pulsar-scene-switch"))["runtime_instance_id"]
        scene = "retained-raster-proof"
        await wire.call("CreateScene", {"sceneName": scene})
        image = Image.new("RGBA", (64, 64), (130, 70, 230, 255))
        image.save(fixtures / "synthetic.png")
        ids = []
        for name in ("raster", "capture"):
            reply = await wire.call("CreateInput", {"sceneName": scene,
                "inputName": name, "inputKind": "image_source", "sceneItemEnabled": True,
                "inputSettings": {"file": str(fixtures / "synthetic.png")}})
            ids.append(reply["sceneItemId"])
        for _ in range(80):
            transform = await wire.call("GetSceneItemTransform", {"sceneName": scene, "sceneItemId": ids[0]})
            if transform["sceneItemTransform"]["sourceWidth"] == 64:
                break
            await asyncio.sleep(.025)
        crop = dict(left=0, top=0, right=0, bottom=0)
        children = [dict(kind="solarBand", sceneItemId=ids[0], band=0,
            affine=legacy.affine(), crop=crop, rect=dict(x=0, y=0, width=64, height=64)),
            dict(kind="capture", sceneItemId=ids[1], affine=legacy.affine(), crop=crop,
                localOpacity=1, visible=True, blendMode="normal", mask=None)]
        groups = [dict(id=legacy.ROOT_GROUP_ID, parentId=None, order=0, opacity=1,
            visible=True, blendMode="normal", mask=None, bounds=dict(x=0,y=0,width=1920,height=1080),
            affine=legacy.affine(), children=children)]
        for depth in range(nested_depth):
            owner_id = "nested-owner-" + str(depth)
            groups[-1]["children"] = [dict(kind="group", id=owner_id)]
            groups.append(dict(id=owner_id, parentId=groups[-1]["id"], order=0,
                opacity=1, visible=True, blendMode="normal", mask=None,
                bounds=dict(x=0,y=0,width=1920,height=1080),
                affine=legacy.affine(), children=children))
        leaf_group = groups[-1]
        report["nestedDepth"] = nested_depth

        async def read_composition(name=scene, instance=runtime):
            return await wire.vendor("ReadGroupComposition", {
                "sceneName": name, "runtimeInstanceId": instance}, "pulsar-capture-effects")

        empty = await read_composition()
        assert empty["ok"] and not empty["active"] and empty["generation"] == 0, empty
        assert empty["runtimeInstanceId"] == runtime and empty["sceneName"] == scene
        wrong_runtime = await read_composition(instance="foreign-runtime")
        assert not wrong_runtime["ok"] and wrong_runtime["error"].startswith("runtime-instance-mismatch"), wrong_runtime
        missing = await read_composition(name="absent-private-composition-scene")
        assert not missing["ok"] and missing["error"].startswith("scene-not-found"), missing
        report["readbackBeforeCommit"] = dict(empty=empty, wrongRuntime=wrong_runtime, missingScene=missing)


        def plan(generation, digest=None):
            p = legacy.make_wire_plan(scene, runtime, generation, copy.deepcopy(groups), (1920,1080), protocol_version=2)
            p["coordinateSpace"] = "global-scene-texels"
            p["planHash"] = digest or hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()
            return p

        async def commit(p):
            return await wire.vendor("CommitGroupComposition", p, "pulsar-capture-effects")

        async def root():
            items = (await wire.call("GetSceneItemList", {"sceneName": scene}))["sceneItems"]
            item = next(x for x in items if x["isGroup"] and legacy.ROOT_GROUP_ID in x["sourceName"])
            # Private OBS groups may expose an empty sourceUuid. Their generated
            # names and parent-owned item IDs distinguish rebuilt trees.
            return (item["sourceName"], item["sceneItemId"], item["sourceUuid"])

        async def pixels():
            response = await wire.call("GetSourceScreenshot", {"sourceName": scene, "imageFormat": "png"})
            image = Image.open(io.BytesIO(base64.b64decode(response["imageData"].split(",")[1]))).convert("RGBA")
            return hashlib.sha256(image.tobytes()).hexdigest()

        report["cases"] = []
        first = plan(1)
        initial = await commit(first)
        assert initial["ok"], initial
        accepted = await read_composition()
        assert accepted["ok"] and accepted["active"] and accepted["generation"] == 1 and accepted["planHash"] == first["planHash"], accepted
        report["readbackActive"] = accepted
        await asyncio.sleep(.2)
        active_root, active_pixels = await root(), await pixels()
        for gen, digest, name in [(2, first["planHash"], "same-native-hash"), (3, None, "new-raster-revision")]:
            reply = await commit(plan(gen, digest))
            assert reply["ok"], reply
            same_root, same_pixels = await root() == active_root, await pixels() == active_pixels
            report["cases"].append(dict(case=name, retained=same_root, exactPixels=same_pixels))
            assert same_root and same_pixels, report["cases"][-1]
        children[1]["affine"]["tx"] = 80
        assert (await commit(plan(4)))["ok"]
        assert await root() == active_root
        children[1]["affine"]["tx"] = 0
        assert (await commit(plan(5)))["ok"]
        assert await root() == active_root and await pixels() == active_pixels
        report["cases"].append(dict(case="translation-and-return", retained=True, exactPixels=True))
        refused = await commit(plan(4))
        assert not refused["ok"] and await root() == active_root and await pixels() == active_pixels
        report["cases"].append(dict(case="stale-generation", refused=True, unchanged=True))
        groups[0]["opacity"] = .7
        assert (await commit(plan(6)))["ok"]
        assert await root() != active_root
        report["cases"].append(dict(case="appearance-change", rebuilt=True))
        leaf_group["children"] = [children[0]]
        raster_only = plan(7)
        assert (await commit(raster_only))["ok"]
        active_root, active_pixels = await root(), await pixels()
        reply = await commit(plan(8, raster_only["planHash"]))
        assert reply["ok"] and await root() == active_root and await pixels() == active_pixels
        report["cases"].append(dict(case="raster-only-identical", retained=True, exactPixels=True))
        image.resize((96, 64)).save(fixtures / "synthetic-resized.png")
        await wire.call("SetInputSettings", {"inputName": "raster", "inputSettings": {
            "file": str(fixtures / "synthetic-resized.png")}, "overlay": True})
        for _ in range(80):
            transform = await wire.call("GetSceneItemTransform", {"sceneName": scene, "sceneItemId": ids[0]})
            if transform["sceneItemTransform"]["sourceWidth"] == 96:
                break
            await asyncio.sleep(.025)
        assert transform["sceneItemTransform"]["sourceWidth"] == 96
        reply = await commit(plan(9))
        assert not reply["ok"] and "global-band-resampling" in reply["error"] and await root() == active_root, reply
        report["cases"].append(dict(case="bound-source-dimension-change", refused=True, retainedRoot=True))
        leaf_group["children"][0]["crop"] = {**crop, "right": 32}
        reply = await commit(plan(10))
        assert reply["ok"] and await root() != active_root, reply
        report["cases"].append(dict(case="new-atlas-crop", rebuilt=True))
        latest_generation = 10
        if nested_depth:
            active_root, active_pixels = await root(), await pixels()
            nested_items = (await wire.call("GetGroupSceneItemList", {
                "sceneName": active_root[0]}))["sceneItems"]
            owner = next(item for item in nested_items if item["isGroup"])
            await wire.call("SetSceneItemTransform", {"sceneName": active_root[0],
                "sceneItemId": owner["sceneItemId"], "sceneItemTransform": {"positionX": 12}})
            reply = await commit(plan(11))
            assert reply["ok"] and await root() != active_root and await pixels() == active_pixels, reply
            report["cases"].append(dict(case="private-owner-transform-change", rebuilt=True, restoredExactPixels=True))
            latest_generation = 11
        accepted = await read_composition()
        assert accepted["ok"] and accepted["active"] and accepted["generation"] == latest_generation, accepted
        current_root = await root()
        duplicate = await commit(plan(latest_generation))
        assert duplicate["ok"] and duplicate.get("idempotent") and await root() == current_root, duplicate
        report["cases"].append(dict(case="exact-commit-replay", idempotent=True, retained=True))
        reset = await wire.vendor("ResetGroupComposition", {"sceneName": scene,
            "runtimeInstanceId": runtime, "generation": latest_generation}, "pulsar-capture-effects")
        assert reset["ok"] and reset["reset"], reset
        after_reset = await read_composition()
        assert after_reset["ok"] and not after_reset["active"] and after_reset["generation"] == latest_generation, after_reset
        assert "planHash" not in after_reset, after_reset
        stale_after_reset = await commit(plan(latest_generation))
        assert not stale_after_reset["ok"] and stale_after_reset["error"].startswith("stale-generation"), stale_after_reset
        report["readbackAfterReset"] = after_reset
        report["cases"].append(dict(case="reset-fence-readback", inactive=True, staleReplayRefused=True))
        for command in ("GetStreamStatus", "GetRecordStatus"):
            assert not (await wire.call(command))["outputActive"]
        report["outputsInactive"] = True
        world = load("retained_world", "probe-global-texel-composition.py")
        report["globalTexels"] = await world.world_exercise(
            dict(server_port=urlsplit(uri).port, server_password=password), output, runtime, fixtures)
        report["passed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nested-depth", type=int, choices=(0, 1, 2), default=0)
    args = parser.parse_args()
    output = args.output.resolve()
    if output not in (ROOT / "evidence/local-20261114-render-stability/host", ROOT / "evidence/local-20261115-audit-fixes/pulsar"):
        raise ValueError("Output must belong to this regression's evidence directory")
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = output / (stamp + "-retained-raster-recommit.json")
    if report_path.exists():
        raise FileExistsError(report_path)
    scratch = ROOT / ".cache/render-stability" / secrets.token_hex(8)
    shutil.copytree(args.runtime, scratch)
    shutil.copyfile(args.plugin, scratch / "obs-plugins/64bit/pulsar-capture-effects.dll")
    fixtures = scratch / "fixtures"
    fixtures.mkdir()
    for key in ("PULSAR_STINGER_ASSET", "PULSAR_MIC_DEVICE_ID", "PULSAR_PROCESS_AUDIO_NAME", "PULSAR_TRACE_PATH"):
        os.environ.pop(key, None)
    os.environ.update(PULSAR_NATIVE_STINGER="0", PULSAR_DUAL_LANE_TRANSITIONS="0",
        PULSAR_LEGACY_ALIAS="disabled", PULSAR_DIRECTSHOW_LEGACY_ALIAS="0",
        PULSAR_DESKTOP_AUDIO_DEVICE_ID="pulsar-synthetic-test-no-device", PULSAR_FPS="30",
        PULSAR_RUNTIME_ROOT=str(scratch / "runtimes"), PULSAR_RUNTIME_DIR=str(scratch / "runtimes/private"))
    proc = helpers.process.PulsarProcess(scratch / "bin/64bit/pulsar.exe", "x264", fixtures, runtime_id="retained-" + secrets.token_hex(6))
    report = dict(passed=False, pluginSha256=hashlib.sha256(args.plugin.read_bytes()).hexdigest(),
        startedUtc=datetime.now(timezone.utc).isoformat(),
        hostSha256=hashlib.sha256((scratch / "bin/64bit/pulsar.exe").read_bytes()).hexdigest(),
        limits="Isolated synthetic GPU readback. No physical camera, Solar CEF paint or public output.")
    try:
        asyncio.run(exercise(proc, output, fixtures, report, args.nested_depth))
    except Exception as error:
        report["error"] = repr(error)
    finally:
        proc.shutdown()
        report["shutdown"] = dict(exitCode=proc.proc.returncode if proc.proc else None, forced=proc.forced_kill_used)
        report["passed"] = report["passed"] and report["shutdown"] == dict(exitCode=0, forced=False)
        if report["shutdown"] == dict(exitCode=0, forced=False) and scratch.resolve().is_relative_to(ROOT / ".cache/render-stability"):
            shutil.rmtree(scratch)
        report["finishedUtc"] = datetime.now(timezone.utc).isoformat()
        report_path.write_text(json.dumps(report, indent=2), encoding="utf8")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
