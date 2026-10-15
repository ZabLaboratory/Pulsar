#!/usr/bin/env python3
"""GPU pixel proof on a disposable Pulsar instance. Never starts Live/recording.
Requires a runtime containing pulsar_capture_effects, websockets and Pillow.
Use --config <obs-websocket/config.json> --output <explicit evidence directory>.
"""
import argparse, asyncio, importlib.util, json, pathlib, uuid, base64, io
from datetime import datetime, timezone
from PIL import Image, ImageChops
import websockets
spec=importlib.util.spec_from_file_location('source_probe',pathlib.Path(__file__).with_name('probe-source-kinds.py'))
protocol=importlib.util.module_from_spec(spec);spec.loader.exec_module(protocol)
ROOT=pathlib.Path(__file__).resolve().parent.parent
async def run(config,output):
    output.mkdir(parents=True,exist_ok=True)
    token=uuid.uuid4().hex[:10];stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+token;layer=f'ZabCapture:effects-{token}:appearance:proof';composite=f'effects-background-{token}'
    source=f'ZabCapture:effects-{token}';background=f'effects-checker-{token}'
    async with websockets.connect(f"ws://127.0.0.1:{config['server_port']}",subprotocols=['obswebsocket.json']) as ws:
        hello=json.loads(await ws.recv())['d'];identify={'rpcVersion':hello['rpcVersion'],'eventSubscriptions':0}
        if 'authentication' in hello:
            a=hello['authentication'];identify['authentication']=protocol.compute_auth(config['server_password'],a['salt'],a['challenge'])
        await ws.send(json.dumps({'op':1,'d':identify}));await ws.recv();inbox=protocol.Inbox()
        async def call(kind,data=None):
            r=await protocol.request(inbox,ws,kind,uuid.uuid4().hex,data)
            if not r['requestStatus']['result']:raise RuntimeError(f"{kind}: {r['requestStatus']}")
            return r.get('responseData',{})
        try:
            assert 'pulsar_capture_effects' in (await call('GetSourceFilterKindList'))['sourceFilterKinds']
            await call('CreateScene',{'sceneName':layer});await call('CreateScene',{'sceneName':composite})
            for scene,name,file in [(layer,source,'foreground.png'),(composite,background,'background.png')]:
                await call('CreateInput',{'sceneName':scene,'inputName':name,'inputKind':'image_source','inputSettings':{'file':str(ROOT/'tests/capture-effects/fixtures'/file)},'sceneItemEnabled':True})
            await call('CreateSceneItem',{'sceneName':composite,'sourceName':layer,'sceneItemEnabled':True})
            base={'width':320,'height':180,'corner_radius':24,'padding':60,'opacity':1,'x':0,'y':0,'background_scene':composite}
            cases=[('baseline',{'kind':3,'radius':0}),('drop-shadow',{'kind':1,'radius':12,'spread':4,'x':8,'y':12,'color':{'r':0,'g':0,'b':0,'a':1}}),('inner-shadow',{'kind':2,'radius':12,'x':12,'y':8,'color':{'r':0,'g':0,'b':0,'a':1}}),('layer-blur',{'kind':3,'radius':20}),('backdrop-blur',{'kind':4,'radius':20}),('noise',{'kind':5,'grain_x':4,'grain_y':4,'density':.7,'noise_type':1,'color':{'r':0,'g':0,'b':0,'a':1}}),('texture',{'kind':6,'radius':2,'grain_x':4,'grain_y':4}),('glass',{'kind':7,'radius':8,'refraction':.8,'depth':.8,'angle':1.2,'intensity':.8,'dispersion':.4,'splay':.4})]
            await call('CreateSourceFilter',{'sourceName':layer,'filterName':'effects-proof','filterKind':'pulsar_capture_effects','filterSettings':{**base,'effects':[cases[0][1]]}})
            results=[];baseline=None
            for label,stage in cases:
                await call('SetSourceFilterSettings',{'sourceName':layer,'filterName':'effects-proof','filterSettings':{**base,'effects':[stage]},'overlay':True});await asyncio.sleep(.15)
                file=output/f'{stamp}-{label}.png';await call('SaveSourceScreenshot',{'sourceName':layer,'imageFormat':'png','imageFilePath':str(file)})
                image=Image.open(file).convert('RGBA');assert image.size==(440,300)
                if label=='baseline':baseline=image.copy();assert image.getpixel((0,0))[3]==0
                else:
                    extrema=ImageChops.difference(baseline,image).getextrema();assert any(hi>0 for lo,hi in extrema),label
                    results.append({'kind':label,'difference':extrema,'file':str(file)})
            await call('RemoveSourceFilter',{'sourceName':layer,'filterName':'effects-proof'})
            assert not (await call('GetSourceFilterList',{'sourceName':layer}))['filters']
            outer=(await call('GetSceneItemId',{'sceneName':composite,'sourceName':layer}))['sceneItemId']
            inner=(await call('GetSceneItemId',{'sceneName':layer,'sourceName':source}))['sceneItemId']
            direct=(await call('CreateSceneItem',{'sceneName':composite,'sourceName':source,'sceneItemEnabled':True}))['sceneItemId']
            await call('SetSceneItemIndex',{'sceneName':composite,'sceneItemId':direct,'sceneItemIndex':1})
            atomic=[]
            for index in range(80):
                label,stage=cases[index % len(cases)]
                padding=[0,20,60,120][index % 4]
                settings={**base,'padding':padding,'x':500,'y':300,'effects':[] if index % 9==0 else [stage]}
                commit=await call('CallVendorRequest',{'vendorName':'pulsar-capture-effects','requestType':'CommitAppearance','requestData':{
                    'sceneName':composite,'layer':layer,'inputName':source,'itemId':outer,'innerId':inner,
                    'boundsType':'OBS_BOUNDS_STRETCH','blendMode':'OBS_BLEND_NORMAL','visible':True,'settings':settings}})
                assert commit['responseData'].get('ok'),commit
                if index==0:
                    items=(await call('GetSceneItemList',{'sceneName':composite}))['sceneItems']
                    assert len(items)==2 and not any(i['sourceName']==source for i in items),items
                    assert next(i['sceneItemIndex'] for i in items if i['sourceName']==layer)==1,items
                t=(await call('GetSceneItemTransform',{'sceneName':composite,'sceneItemId':outer}))['sceneItemTransform']
                assert (t['positionX'],t['positionY'])==(500-padding,300-padding),t
                assert t['cropRight']==0 and t['cropBottom']==0,t
                assert (t['sourceWidth'],t['sourceHeight'])==(320+2*padding,180+2*padding),t
                snap=await call('GetSourceScreenshot',{'sourceName':composite,'imageFormat':'png'})
                img=Image.open(io.BytesIO(base64.b64decode(snap['imageData'].split(',',1)[1]))).convert('RGBA')
                # Outside any possible external paint, the preceding scene is
                # stable. The authored source remains visible at its center.
                assert img.getpixel((500+80,300+90))[3]>0
                outside=img.getpixel((20,20))
                assert outside==Image.open(ROOT/'tests/capture-effects/fixtures/background.png').convert('RGBA').getpixel((20,20)),outside
                atomic.append({'index':index,'effect':label,'padding':padding,'size':[t['sourceWidth'],t['sourceHeight']]})
            rotations=[]
            for angle in [-90,-35,30,90,180,0]:
                settings={**base,'padding':60,'x':500,'y':300,'rotation':angle,'effects':[]}
                commit=await call('CallVendorRequest',{'vendorName':'pulsar-capture-effects','requestType':'CommitAppearance','requestData':{'sceneName':composite,'layer':layer,'inputName':source,'itemId':outer,'innerId':inner,'boundsType':'OBS_BOUNDS_STRETCH','blendMode':'OBS_BLEND_NORMAL','visible':True,'settings':settings}})
                assert commit['responseData'].get('ok'),commit
                t=(await call('GetSceneItemTransform',{'sceneName':composite,'sceneItemId':outer}))['sceneItemTransform']
                assert t['rotation']==angle,t
                assert (t['positionX'],t['positionY'])==((660,390) if angle else (440,240)),t
                assert t['alignment']==(0 if angle else 5),t
                assert (t['sourceWidth'],t['sourceHeight'])==(440,300),t
                inside=(await call('GetSceneItemTransform',{'sceneName':layer,'sceneItemId':inner}))['sceneItemTransform']
                assert inside['rotation']==0,inside
                await asyncio.sleep(.05)
                file=output/f'{stamp}-rotation-{str(angle).replace(chr(45),"minus-")}.png'
                await call('SaveSourceScreenshot',{'sourceName':composite,'imageFormat':'png','imageFilePath':str(file)})
                image=Image.open(file).convert('RGBA')
                assert image.getpixel((660,390))[2]>100,image.getpixel((660,390))
                rotations.append({'angle':angle,'transform':t,'file':str(file)})
            assert len((await call('GetSourceFilterList',{'sourceName':layer}))['filters'])==1
            await call('CreateSourceFilter',{'sourceName':layer,'filterName':'prism/capture-rounded','filterKind':'color_filter_v2','filterSettings':{'opacity':1}})
            refused=await call('CallVendorRequest',{'vendorName':'pulsar-capture-effects','requestType':'CommitAppearance','requestData':{'sceneName':composite,'layer':layer,'inputName':source,'itemId':outer,'innerId':inner,'settings':settings}})
            assert not refused['responseData'].get('ok') and 'collision' in refused['responseData']['error'],refused
            await call('RemoveSourceFilter',{'sourceName':layer,'filterName':'prism/capture-rounded'})
            report={'zOrderPreserved':True,'foreignCollisionPreserved':True,'atomicAdmissions':atomic,'rotationAdmissions':rotations,'effects':results,'reset':True,'scope':'GPU fixed-fixture pixels and 80 atomic admissions; not physical device availability'}
            (output/f'{stamp}-gpu-proof.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report
        finally:
            for kind,data in [('RemoveScene',{'sceneName':composite}),('RemoveScene',{'sceneName':layer}),('RemoveInput',{'inputName':background}),('RemoveInput',{'inputName':source})]:
                try:await call(kind,data)
                except Exception:pass
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=pathlib.Path,required=True);parser.add_argument('--output',type=pathlib.Path,required=True);a=parser.parse_args()
    result=asyncio.run(run(json.loads(a.config.read_text()),a.output.resolve()));print(json.dumps({'effects':len(result['effects']),'reset':result['reset']}))

