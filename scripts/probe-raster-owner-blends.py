#!/usr/bin/env python3
"""Private native blend, wrapper retirement and removed-root replay regression.

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


async def exercise_blends(wire, runtime, fixtures, report):
    """Actual solarBand children, with their blend/opacity envelope on an owner.

    Reuses the independent CSS pixel reference from the established group probe.
    No Vision renderer or receipt transformation is mocked as native acceptance.
    """
    scene = "postfix-raster-owner-blends"
    await wire.call("CreateScene", {"sceneName": scene})
    colors = {"backdrop": (230, 40, 50, 255), "band": (130, 70, 230, 255)}
    ids = {}
    for label, color in colors.items():
        path = fixtures / ("blend-" + label + ".png")
        Image.new("RGBA", (64, 64), color).save(path)
        ids[label] = (await wire.call("CreateInput", {
            "sceneName": scene, "inputName": "blend-" + label,
            "inputKind": "image_source", "sceneItemEnabled": True,
            "inputSettings": {"file": str(path)}}))["sceneItemId"]
    await asyncio.sleep(.15)
    crop = dict(left=0, top=0, right=0, bottom=0)
    bounds = dict(x=0, y=0, width=1920, height=1080)
    capture = dict(kind="capture", sceneItemId=ids["backdrop"],
        affine=legacy.affine(), crop=crop, localOpacity=1, visible=True,
        blendMode="normal", mask=None)
    band = dict(kind="solarBand", sceneItemId=ids["band"], band=0,
        affine=legacy.affine(), crop=crop, rect=dict(x=0,y=0,width=64,height=64))
    root_id = legacy.ROOT_GROUP_ID
    groups = [dict(id=root_id, parentId=None, order=0, opacity=1,
        visible=True, blendMode="normal", mask=None, bounds=bounds,
        affine=legacy.affine(), children=[capture, dict(kind="group",id="paint")]),
        dict(id="paint", parentId=root_id, order=1, opacity=1, visible=True,
            blendMode="normal", mask=None, bounds=bounds,
            affine=legacy.affine(), children=[band])]
    generation = 0
    async def submit():
        nonlocal generation
        generation += 1
        plan = legacy.make_wire_plan(scene,runtime,generation,copy.deepcopy(groups),
            (1920,1080),protocol_version=2)
        plan["coordinateSpace"] = "global-scene-texels"
        plan["planHash"] = hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
        response = await wire.vendor("CommitGroupComposition", plan, "pulsar-capture-effects")
        assert response["ok"], response
        image = await wire.call("GetSourceScreenshot", {"sourceName":scene,"imageFormat":"png"})
        rgba = Image.open(io.BytesIO(base64.b64decode(image["imageData"].split(",")[1]))).convert("RGBA")
        return rgba.getpixel((20,20))
    report["rasterOwnerBlends"] = []
    for name, mode in legacy.CSS_BLEND_MODES:
        groups[1]["blendMode"] = name
        actual = await submit()
        expected = legacy.expected_css_composite(colors["backdrop"], colors["band"], mode)
        legacy.assert_pixel_near(actual, expected, "raster owner " + name, 5)
        report["rasterOwnerBlends"].append(dict(mode=name,actual=list(actual),expected=list(expected)))
    groups[1].update(blendMode="difference", opacity=.5)
    actual = await submit()
    expected = legacy.expected_css_composite(colors["backdrop"], colors["band"], 8, .5)
    legacy.assert_pixel_near(actual,expected,"raster owner opacity once",3)
    report["rasterOwnerOpacityOnce"] = dict(actual=list(actual),expected=list(expected))
    # A native group is isolated: its child cannot read a grandparent prefix.
    # This documents the boundary for Vision; never treat it as pass-through.
    groups[0]["children"][1]["id"] = "intermediate"
    groups[1].update(parentId="intermediate",order=0,opacity=1)
    groups.append(dict(id="intermediate",parentId=root_id,order=1,opacity=1,
        visible=True,blendMode="normal",mask=None,bounds=bounds,
        affine=legacy.affine(),children=[dict(kind="group",id="paint")]))
    actual = await submit()
    legacy.assert_pixel_near(actual,colors["band"],"isolated nested owner backdrop",3)
    report["nestedOwnerIsolation"] = dict(actual=list(actual),
        passThroughExpected=list(legacy.expected_css_composite(colors["backdrop"],colors["band"],8)),
        requiresVisionRejectionOrEquivalentHoist=True)
    reset = await wire.vendor("ResetGroupComposition", {
        "sceneName":scene,"generation":generation,"runtimeInstanceId":runtime},"pulsar-capture-effects")
    assert reset["ok"],reset


async def exercise_retirement(wire, runtime, fixtures, report):
    """Reproduce post-ACK cleanup, then prove transactional cleanup and guards."""
    cases = report["wrapperRetirement"] = []
    for protocol in (1, 2):
        scene = "retirement-parent-" + str(protocol)
        await wire.call("CreateScene", {"sceneName": scene})
        wrappers = []
        for number, color in enumerate(((200, 40, 60, 255), (60, 180, 210, 255), (190, 150, 40, 255))):
            name = scene + "-wrapper-" + str(number)
            await wire.call("CreateScene", {"sceneName": name})
            path = fixtures / (name + ".png")
            Image.new("RGBA", (64, 64), color).save(path)
            await wire.call("CreateInput", {"sceneName": name, "inputName": name + "-pixels",
                "inputKind": "image_source", "inputSettings": {"file": str(path)}, "sceneItemEnabled": True})
            item = await wire.call("CreateSceneItem", {"sceneName": scene, "sourceName": name, "sceneItemEnabled": True})
            wrappers.append(dict(name=name, id=item["sceneItemId"], color=list(color)))
        # A previous capture can be an image, but only scene wrappers are removable.
        image_id = (await wire.call("CreateInput", {"sceneName": scene, "inputName": scene + "-direct",
            "inputKind": "image_source", "inputSettings": {"file": str(path)}, "sceneItemEnabled": True}))["sceneItemId"]
        await asyncio.sleep(.15)
        def plan(gen, capture_id, retired=()):
            children = [dict(kind="capture", sceneItemId=capture_id, affine=legacy.affine(),
                crop=dict(left=0, top=0, right=0, bottom=0), localOpacity=1, visible=True,
                blendMode="normal", mask=None)]
            groups = [dict(id=legacy.ROOT_GROUP_ID, parentId=None, order=0, opacity=1, visible=True,
                blendMode="normal", mask=None, bounds=dict(x=0,y=0,width=1920,height=1080),
                affine=legacy.affine(), children=children)]
            p = legacy.make_wire_plan(scene, runtime, gen, groups, (1920,1080), protocol_version=protocol)
            p.update(coordinateSpace="global-scene-texels", retiredCaptureSceneItems=[dict(sceneItemId=x) for x in retired])
            p["planHash"] = hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest()
            return p
        async def commit(p):
            return await wire.vendor("CommitGroupComposition", p, "pulsar-capture-effects")
        async def read():
            return await wire.vendor("ReadGroupComposition", {"sceneName":scene,"runtimeInstanceId":runtime}, "pulsar-capture-effects")
        async def tree():
            return (await wire.call("GetSceneItemList", {"sceneName":scene}))["sceneItems"]
        async def root():
            return next(x["sceneItemId"] for x in await tree() if x["isGroup"] and legacy.ROOT_GROUP_ID in x["sourceName"])
        async def pixels():
            p = await wire.call("GetSourceScreenshot", {"sourceName":scene,"imageFormat":"png"})
            return Image.open(io.BytesIO(base64.b64decode(p["imageData"].split(",")[1]))).convert("RGBA")
        for item in await tree():
            await wire.call("SetSceneItemEnabled", {"sceneName":scene,"sceneItemId":item["sceneItemId"],"sceneItemEnabled":False})
        assert (await commit(plan(1,wrappers[0]["id"])))["ok"]
        initial_root = await root()
        initial_pixels = hashlib.sha256((await pixels()).tobytes()).hexdigest()
        async def refuse(p, code):
            before = await root()
            response = await commit(p)
            assert not response["ok"] and code in response["error"], response
            assert await root() == before and (await read())["generation"] == 1
            assert hashlib.sha256((await pixels()).tobytes()).hexdigest() == initial_pixels
            cases.append(dict(protocol=protocol, rejected=code, frontUnchanged=True))
        await refuse(plan(2,wrappers[1]["id"],[999999]), "not-owned")
        await refuse(plan(2,wrappers[0]["id"],[wrappers[0]["id"]]), "referenced")
        await refuse(plan(2,wrappers[1]["id"],[wrappers[2]["id"]]), "not-owned")
        await refuse(plan(2,wrappers[1]["id"],[wrappers[0]["id"]]*2), "duplicate-retired")
        await refuse(plan(2,wrappers[1]["id"],[0]), "invalid-retired")
        await refuse(plan(2,wrappers[1]["id"],[1.5]), "invalid-retired")
        await refuse(plan(2,wrappers[1]["id"],[9007199254740992]), "invalid-retired")
        bad = plan(2,wrappers[1]["id"],[wrappers[0]["id"]]);bad["runtimeInstanceId"]="foreign"
        await refuse(bad, "runtime-instance-mismatch")
        # Witness: a legitimate but external post-ACK deletion invalidates the snapshot.
        assert (await commit(plan(2,wrappers[1]["id"])))["ok"]
        witness_root = await root()
        await wire.call("RemoveSceneItem", {"sceneName":scene,"sceneItemId":wrappers[0]["id"]})
        moved = plan(3,wrappers[1]["id"]);moved["groups"][0]["children"][0]["affine"]["tx"] = 1
        response = await commit(moved);assert response["ok"] and await root() != witness_root
        cases.append(dict(protocol=protocol, case="external-cleanup-witness", rebuilt=True, debug=response.get("debugCommitTiming")))
        # Transactional replacement: removal precedes snapshot and exact replay is harmless.
        accepted = plan(4,wrappers[2]["id"],[wrappers[1]["id"]])
        ack = await commit(accepted);assert ack["ok"] and ack["retiredCaptureSceneItems"] == accepted["retiredCaptureSceneItems"],ack
        assert all(x["sceneItemId"]!=wrappers[1]["id"] for x in await tree())
        assert (await read())["retiredCaptureSceneItems"] == accepted["retiredCaptureSceneItems"]
        installed_root = await root()
        replay = await commit(accepted);assert replay["ok"] and replay.get("idempotent") and replay["retiredCaptureSceneItems"] == accepted["retiredCaptureSceneItems"]
        mismatched = copy.deepcopy(accepted);mismatched["retiredCaptureSceneItems"] = []
        rejected = await commit(mismatched);assert not rejected["ok"] and "stale-generation" in rejected["error"]
        assert await root() == installed_root
        # Main now releases only the source, no further host-scene mutation.
        await wire.call("RemoveScene", {"sceneName":wrappers[1]["name"]})
        for gen, tx in ((5,1),(6,2),(7,0)):
            moved=plan(gen,wrappers[2]["id"]);moved["groups"][0]["children"][0]["affine"]["tx"]=tx
            response=await commit(moved);assert response["ok"] and await root()==installed_root,response
            assert (await pixels()).getpixel((20,20)) == tuple(wrappers[2]["color"])
            assert (await read())["retiredCaptureSceneItems"] == []
            cases.append(dict(protocol=protocol,case="transactional-cleanup-translation",generation=gen,rootPreserved=True,debug=response.get("debugCommitTiming")))
        # A direct image from the previous front is not a wrapper and must survive refusal.
        assert (await commit(plan(8,image_id)))["ok"]
        refused=await commit(plan(9,wrappers[2]["id"],[image_id]));assert not refused["ok"] and "identity-mismatch" in refused["error"],refused
        assert (await read())["generation"]==8
        reset=await wire.vendor("ResetGroupComposition", {"sceneName":scene,"runtimeInstanceId":runtime,"generation":8}, "pulsar-capture-effects")
        assert reset["ok"]
        assert (await read())["retiredCaptureSceneItems"] == []
        cases.append(dict(protocol=protocol,exactReplay=True,differentReplayRefused=True,nonWrapperRefused=True,reset=True,initialRoot=initial_root))


async def exercise(proc, output, fixtures, report):
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
        await ws.send(json.dumps({"op":1,"d":identify})); assert json.loads(await ws.recv())["op"]==2
        wire=helpers.Wire(ws)
        runtime=(await wire.vendor("GetState",vendor="pulsar-scene-switch"))["runtime_instance_id"]
        await exercise_blends(wire, runtime, fixtures, report)
        await exercise_retirement(wire, runtime, fixtures, report)
        scene="postfix-native-root-replay"
        await wire.call("CreateScene",{"sceneName":scene})
        ids=[]
        for name,color in [("raster",(130,70,230,255)),("capture",(230,40,50,255))]:
            file=fixtures/(name+".png");Image.new("RGBA",(64,64),color).save(file)
            result=await wire.call("CreateInput",{"sceneName":scene,"inputName":name,"inputKind":"image_source","sceneItemEnabled":True,"inputSettings":{"file":str(file)}})
            ids.append(result["sceneItemId"])
        await asyncio.sleep(.2)
        crop=dict(left=0,top=0,right=0,bottom=0)
        children=[dict(kind="solarBand",sceneItemId=ids[0],band=0,affine=legacy.affine(),crop=crop,rect=dict(x=0,y=0,width=64,height=64)),dict(kind="capture",sceneItemId=ids[1],affine=legacy.affine(),crop=crop,localOpacity=1,visible=True,blendMode="normal",mask=None)]
        groups=[dict(id=legacy.ROOT_GROUP_ID,parentId=None,order=0,opacity=1,visible=True,blendMode="normal",mask=None,bounds=dict(x=0,y=0,width=1920,height=1080),affine=legacy.affine(),children=children)]
        def plan(gen,digest=None):
            p=legacy.make_wire_plan(scene,runtime,gen,copy.deepcopy(groups),(1920,1080),protocol_version=2);p["coordinateSpace"]="global-scene-texels";p["planHash"]=digest or hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest();return p
        async def commit(p):return await wire.vendor("CommitGroupComposition",p,"pulsar-capture-effects")
        async def read():return await wire.vendor("ReadGroupComposition",{"sceneName":scene,"runtimeInstanceId":runtime},"pulsar-capture-effects")
        async def tree():
            items=(await wire.call("GetSceneItemList",{"sceneName":scene}))["sceneItems"]
            root=next(x for x in items if x["isGroup"] and legacy.ROOT_GROUP_ID in x["sourceName"])
            inner=(await wire.call("GetGroupSceneItemList",{"sceneName":root["sourceName"]}))["sceneItems"]
            return {"root":[root["sceneItemId"],root["sourceName"]],"children":[[x["sceneItemId"],x["sourceName"]] for x in inner]}
        async def pixels():
            response=await wire.call("GetSourceScreenshot",{"sourceName":scene,"imageFormat":"png"});im=Image.open(io.BytesIO(base64.b64decode(response["imageData"].split(",")[1]))).convert("RGBA");return dict(hash=hashlib.sha256(im.tobytes()).hexdigest(),sample=list(im.getpixel((20,20))))
        first=plan(1);assert (await commit(first))["ok"]
        await asyncio.sleep(.2)
        initial_tree,initial_pixels=await tree(),await pixels()
        assert initial_pixels["sample"]==[230,40,50,255],initial_pixels
        await asyncio.sleep(.3)
        idle_tree=await tree();assert idle_tree==initial_tree
        report["idle"]={"treeUnchanged":True,"generation":(await read())["generation"],"waitMs":300}
        report["cases"]=[]
        for gen in (2,3,4):
            assert (await commit(plan(gen,first["planHash"])))["ok"]
            assert await tree()==initial_tree and await pixels()==initial_pixels
            report["cases"].append(dict(case="no-op",generation=gen,identitiesPreserved=True,exactPixels=True))
        children[1]["affine"]["tx"]=80;assert (await commit(plan(5)))["ok"]
        assert await tree()==initial_tree and (await pixels())["sample"]==[130,70,230,255]
        children[1]["affine"]["tx"]=0;assert (await commit(plan(6)))["ok"]
        assert await tree()==initial_tree and await pixels()==initial_pixels
        report["cases"].append(dict(case="translation-return",identitiesPreserved=True,exactPixels=True))
        children.reverse();assert (await commit(plan(7)))["ok"]
        reordered_tree,reordered_pixels=await tree(),await pixels()
        assert reordered_tree!=initial_tree and reordered_pixels["sample"]==[130,70,230,255]
        stale=await commit(plan(6));assert not stale["ok"] and await tree()==reordered_tree
        report["cases"].append(dict(case="order-change",rebuilt=True,frontSample=reordered_pixels["sample"],staleRefused=True))
        children.reverse();accepted=plan(8);assert (await commit(accepted))["ok"]
        final_tree=await tree();assert await pixels()==initial_pixels
        healthy_replay=await commit(accepted);assert healthy_replay["ok"] and healthy_replay.get("idempotent")
        await wire.call("RemoveSceneItem",{"sceneName":scene,"sceneItemId":final_tree["root"][0]})
        after_remove=await read();replay_after_remove=await commit(accepted);newer_after_remove=await commit(plan(9));after_pixels=await pixels()
        report["removedRoot"]={"readback":after_remove,"sameGenerationReplay":replay_after_remove,"newerGeneration":newer_after_remove,"pixels":after_pixels,"initialSample":initial_pixels["sample"]}
        assert not after_remove["ok"] and "active-root-replaced" in after_remove["error"],after_remove
        assert not replay_after_remove["ok"] and "active-root-replaced" in replay_after_remove["error"],replay_after_remove
        assert not newer_after_remove["ok"] and "active-root-replaced" in newer_after_remove["error"],newer_after_remove
        report["removedRootReplayRefused"]=True
        for command in ("GetStreamStatus","GetRecordStatus"):assert not (await wire.call(command))["outputActive"]
        report["outputsInactive"]=True;report["passed"]=True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output not in (ROOT / "evidence/local-20261115-postfix-fixes/pulsar",
                      ROOT / "evidence/local-20261115-first-move-rebuild/pulsar"):
        raise ValueError("Output must belong to this regression's evidence directory")
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = output / (stamp + "-raster-owner-blends.json")
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
        asyncio.run(exercise(proc, output, fixtures, report))
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
