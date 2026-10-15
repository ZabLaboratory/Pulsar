"""Pixel proof for inherited frame clipping on a disposable Pulsar runtime.
Never starts Live or recording; private fixture sources are removed in finally.
"""
import argparse, asyncio, base64, importlib.util, io, json, math, pathlib, uuid
from datetime import datetime, timezone
from PIL import Image, ImageChops
import websockets

spec = importlib.util.spec_from_file_location("source_probe", pathlib.Path(__file__).with_name("probe-source-kinds.py"))
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)

def distance(clip, x, y):
    qx = clip.get("xx", 1)*x + clip.get("xy", 0)*y + clip.get("tx", 0)
    qy = clip.get("yx", 0)*x + clip.get("yy", 1)*y + clip.get("ty", 0)
    width, height = clip["width"], clip["height"]
    radius = min(clip.get("radius", 0), width/2, height/2)
    dx, dy = abs(qx-width/2)-(width/2-radius), abs(qy-height/2)-(height/2-radius)
    return math.hypot(max(dx, 0), max(dy, 0)) + min(max(dx, dy), 0)-radius

async def run(config, output):
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = uuid.uuid4().hex[:10]
    source = f"ZabCapture:ancestor-{token}"
    layer = source+":appearance:proof"
    parent = f"ancestor-composite-{token}"
    cases = [
        ("left-half", [{"width":80,"height":120}]),
        ("translated", [{"tx":-40,"width":80,"height":120}]),
        ("rotated", [{"xx":math.cos(.5),"xy":-math.sin(.5),"tx":60-80*math.cos(.5)+60*math.sin(.5),"yx":math.sin(.5),"yy":math.cos(.5),"ty":50-80*math.sin(.5)-60*math.cos(.5),"width":120,"height":100}]),
        ("nested-intersection", [{"width":120,"height":120},{"tx":-40,"ty":-20,"width":120,"height":80}]),
        ("rounded", [{"width":80,"height":80,"radius":20}]),
        ("empty-frame", [{"width":0,"height":0}]),
    ]
    async with websockets.connect(f"ws://127.0.0.1:{config['server_port']}", subprotocols=["obswebsocket.json"]) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify = {"rpcVersion":hello["rpcVersion"],"eventSubscriptions":0}
        if "authentication" in hello:
            auth = hello["authentication"]
            identify["authentication"] = protocol.compute_auth(config["server_password"],auth["salt"],auth["challenge"])
        await ws.send(json.dumps({"op":1,"d":identify})); await ws.recv()
        inbox = protocol.Inbox()
        async def call(kind, data=None):
            result = await protocol.request(inbox,ws,kind,uuid.uuid4().hex,data)
            if not result["requestStatus"]["result"]: raise RuntimeError(f"{kind}: {result['requestStatus']}")
            return result.get("responseData",{})
        try:
            await call("CreateScene",{"sceneName":layer})
            await call("CreateScene",{"sceneName":parent})
            await call("CreateInput",{"sceneName":layer,"inputName":source,"inputKind":"color_source_v3","inputSettings":{"color":4294967295,"width":160,"height":120},"sceneItemEnabled":True})
            inner = (await call("GetSceneItemId",{"sceneName":layer,"sourceName":source}))["sceneItemId"]
            outer = (await call("CreateSceneItem",{"sceneName":parent,"sourceName":layer,"sceneItemEnabled":True}))["sceneItemId"]
            base = {"width":160,"height":120,"padding":20,"opacity":1,"background_scene":parent,"effects":[]}
            async def admit(clips, suffix, effects=None):
                result = await call("CallVendorRequest",{"vendorName":"pulsar-capture-effects","requestType":"CommitAppearance","requestData":{"sceneName":parent,"layer":layer,"inputName":source,"itemId":outer,"innerId":inner,"boundsType":"OBS_BOUNDS_STRETCH","blendMode":"OBS_BLEND_NORMAL","visible":True,"settings":{**base,"ancestor_clips":clips,"effects":effects or []}}})
                assert result["responseData"].get("ok"), result
                await asyncio.sleep(.12)
                snapshot = await call("GetSourceScreenshot",{"sourceName":layer,"imageFormat":"png"})
                image = Image.open(io.BytesIO(base64.b64decode(snapshot["imageData"].split(",",1)[1]))).convert("RGBA")
                image.save(output/f"{stamp}-{suffix}.png")
                assert image.size == (200,160), image.size
                return image
            baseline = await admit([],"baseline")
            assert baseline.getpixel((100,80))[3] == 255
            results = []
            for label, clips in cases:
                image = await admit(clips,label)
                inside = outside = 0
                for y in range(160):
                    for x in range(200):
                        d = max(distance(clip,x-20+.5,y-20+.5) for clip in clips)
                        if d > 1.5:
                            assert image.getpixel((x,y))[3] == 0, (label,"leak",x,y,image.getpixel((x,y)))
                            outside += 1
                        elif d < -1.5:
                            assert image.getpixel((x,y)) == baseline.getpixel((x,y)), (label,"lost pixels",x,y)
                            inside += 1
                assert outside > 0
                results.append({"case":label,"insidePixels":inside,"outsidePixels":outside,"leakedPixels":0})
            shadow = [{"kind":1,"radius":8,"spread":3,"x":12,"y":8,"color":{"r":0,"g":0,"b":0,"a":1}}]
            clips = [{"width":80,"height":120}]
            image = await admit(clips,"shadow-boundary",shadow)
            assert all(image.getpixel((x,y))[3] == 0 for x in range(103,200) for y in range(160))
            results.append({"case":"shadow-boundary","leakedPixels":0})
            restored = await admit([],"reset")
            assert not ImageChops.difference(baseline,restored).getbbox()
            results.append({"case":"reset","identical":True})
            (output/f"{stamp}-pixels.json").write_text(json.dumps({"passed":True,"cases":results},indent=2))
            print(json.dumps({"passed":True,"cases":len(results),"output":str(output)}))
        finally:
            for kind,data in [("RemoveInput",{"inputName":source}),("RemoveScene",{"sceneName":parent}),("RemoveScene",{"sceneName":layer})]:
                try: await call(kind,data)
                except Exception: pass

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",type=pathlib.Path,required=True)
    parser.add_argument("--output",type=pathlib.Path,required=True)
    args=parser.parse_args()
    asyncio.run(run(json.loads(args.config.read_text()),args.output))
