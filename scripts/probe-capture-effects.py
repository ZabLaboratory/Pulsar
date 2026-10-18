#!/usr/bin/env python3
"""GPU pixel proof on a disposable Pulsar instance. Never starts Live/recording.
Requires a runtime containing pulsar_capture_effects, websockets and Pillow.
Use --config <obs-websocket/config.json> --output <explicit evidence directory>.
"""
import argparse, asyncio, importlib.util, json, pathlib, uuid, base64, io, math
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
            staged_direct=(await call('CreateSceneItem',{'sceneName':composite,'sourceName':source,'sceneItemEnabled':True}))['sceneItemId']
            staged=await call('CallVendorRequest',{'vendorName':'pulsar-capture-effects','requestType':'CommitAppearance','requestData':{
                'sceneName':composite,'layer':layer,'inputName':source,'itemId':outer,'innerId':inner,
                'boundsType':'OBS_BOUNDS_STRETCH','blendMode':'OBS_BLEND_NORMAL','visible':True,
                'preserveDirect':True,'settings':{**base,'padding':0,'x':500,'y':300,'effects':[]}}})
            assert staged['responseData'].get('ok'),staged
            staged_items=(await call('GetSceneItemList',{'sceneName':composite}))['sceneItems']
            assert any(i['sceneItemId']==staged_direct and i['sceneItemEnabled'] for i in staged_items),staged_items
            assert any(i['sceneItemId']==outer and not i['sceneItemEnabled'] for i in staged_items),staged_items
            rotations=[]
            for angle in [-90,-35,30,90,180,0]:
                settings={**base,'padding':60,'x':500,'y':300,'rotation':angle,'effects':[]}
                commit=await call('CallVendorRequest',{'vendorName':'pulsar-capture-effects','requestType':'CommitAppearance','requestData':{'sceneName':composite,'layer':layer,'inputName':source,'itemId':outer,'innerId':inner,'boundsType':'OBS_BOUNDS_STRETCH','blendMode':'OBS_BLEND_NORMAL','visible':True,'settings':settings}})
                assert commit['responseData'].get('ok'),commit
                if angle==-90:
                    items=(await call('GetSceneItemList',{'sceneName':composite}))['sceneItems']
                    assert not any(i['sceneItemId']==staged_direct for i in items),items
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
            fractional=[]
            last_fractional_settings=None
            for label,content_width,content_height,expected_width,expected_height,outside in [
                ('fractional-width',.2,18,2,180,(54,50)),
                ('fractional-height',32,.3,320,3,(50,54)),
                ('zero-width-fallback',0,18,10,180,None),
                ('negative-height-fallback',32,-2,320,10,None),
            ]:
                framing={**base,'padding':0,'corner_radius':0,'x':0,'y':0,'rotation':0,
                         'content_box_x':50 if 'width' in label else 0,
                         'content_box_y':50 if 'height' in label else 0,
                         'content_box_width':content_width,'content_box_height':content_height,
                         'source_zoom':10,'effects':[]}
                admitted=await call('CallVendorRequest',{'vendorName':'pulsar-capture-effects','requestType':'CommitAppearance','requestData':{'sceneName':composite,'layer':layer,'inputName':source,'itemId':outer,'innerId':inner,'boundsType':'OBS_BOUNDS_STRETCH','blendMode':'OBS_BLEND_NORMAL','visible':True,'settings':framing}})
                assert admitted['responseData'].get('ok'),admitted
                transform=(await call('GetSceneItemTransform',{'sceneName':layer,'sceneItemId':inner}))['sceneItemTransform']
                assert abs(transform['boundsWidth']-expected_width)<.0001,transform
                assert abs(transform['boundsHeight']-expected_height)<.0001,transform
                await asyncio.sleep(.05)
                shot=await call('GetSourceScreenshot',{'sourceName':layer,'imageFormat':'png'})
                raw=base64.b64decode(shot['imageData'].split(',',1)[1]);image=Image.open(io.BytesIO(raw)).convert('RGBA')
                file=output/f'{stamp}-{label}.png';file.write_bytes(raw)
                if outside:
                    assert image.getpixel(outside)[3]==0,(label,outside,image.getpixel(outside))
                    assert image.getpixel((50,50))[3]>0,(label,image.getpixel((50,50)))
                fractional.append({'case':label,'requestedContentSize':[content_width,content_height],
                                   'expectedZoomedBounds':[expected_width,expected_height],
                                   'actualZoomedBounds':[transform['boundsWidth'],transform['boundsHeight']],
                                   'outsidePixel':list(image.getpixel(outside)) if outside else None,
                                   'file':str(file)})
                last_fractional_settings=dict(framing)
            prior_optional_keys=('content_box_x','content_box_y','content_box_width','content_box_height')
            assert last_fractional_settings is not None
            prior_optional_geometry={key:last_fractional_settings[key] for key in prior_optional_keys}
            assert prior_optional_geometry=={
                'content_box_x':0,'content_box_y':50,
                'content_box_width':32,'content_box_height':-2,
            },prior_optional_geometry
            external=[]
            optional_geometry_reset=None
            transform_tolerance=0.0001
            inner_position_tolerance=0.51 # OBS scene coordinates round this fit position.
            for label,width,height,padding,source_zoom,external_zoom,x,y in [
                ('fractional-outer-transformed',10.5,8.75,0,10.0,2.0,500,300),
                ('fractional-padded-transformed',10.5,8.75,1.25,10.0,4.0,550,300),
                ('subpixel-outer-transformed',.2,18.25,0,10.0,3.0,700,300),
            ]:
                framing={**base,'width':width,'height':height,'padding':padding,'corner_radius':0,
                         'x':x,'y':y,'rotation':0,'source_zoom':source_zoom,'effects':[]}
                if label=='fractional-outer-transformed':
                    omitted_optional_keys=[key for key in prior_optional_keys if key not in framing]
                    assert omitted_optional_keys==list(prior_optional_keys),(framing,omitted_optional_keys)
                    filters_before=await call('GetSourceFilterList',{'sourceName':layer})
                    assert len(filters_before['filters'])==1,filters_before
                    assert filters_before['filters'][0]['filterName']=='prism/capture-effects',filters_before
                admitted=await call('CallVendorRequest',{'vendorName':'pulsar-capture-effects','requestType':'CommitAppearance','requestData':{'sceneName':composite,'layer':layer,'inputName':source,'itemId':outer,'innerId':inner,'boundsType':'OBS_BOUNDS_STRETCH','blendMode':'OBS_BLEND_NORMAL','visible':True,'settings':framing}})
                assert admitted['responseData'].get('ok'),admitted
                outer_size=(width+2*padding,height+2*padding)
                raster=(math.ceil(outer_size[0]),math.ceil(outer_size[1]))
                transform=(await call('GetSceneItemTransform',{'sceneName':composite,'sceneItemId':outer}))['sceneItemTransform']
                assert (transform['sourceWidth'],transform['sourceHeight'])==raster,(label,transform,raster)
                scale=(outer_size[0]/raster[0],outer_size[1]/raster[1])
                assert abs(transform['scaleX']-scale[0])<transform_tolerance,(label,transform,scale)
                assert abs(transform['scaleY']-scale[1])<transform_tolerance,(label,transform,scale)
                expected_position=(x-padding*external_zoom,y-padding*external_zoom)
                zoomed={'positionX':expected_position[0],'positionY':expected_position[1],
                        'rotation':0,'alignment':5,'boundsType':'OBS_BOUNDS_NONE',
                        'scaleX':scale[0]*external_zoom,'scaleY':scale[1]*external_zoom}
                await call('SetSceneItemTransform',{'sceneName':composite,'sceneItemId':outer,'sceneItemTransform':zoomed})
                transformed=(await call('GetSceneItemTransform',{'sceneName':composite,'sceneItemId':outer}))['sceneItemTransform']
                actual=(transformed['sourceWidth']*transformed['scaleX'],
                        transformed['sourceHeight']*transformed['scaleY'])
                expected_outer=(outer_size[0]*external_zoom,outer_size[1]*external_zoom)
                expected_content=(width*external_zoom,height*external_zoom)
                assert abs(transformed['positionX']-expected_position[0])<transform_tolerance and abs(transformed['positionY']-expected_position[1])<transform_tolerance,(label,'position',transformed)
                assert abs(actual[0]-expected_outer[0])<transform_tolerance,(label,transformed,expected_outer)
                assert abs(actual[1]-expected_outer[1])<transform_tolerance,(label,transformed,expected_outer)
                inner_transform=(await call('GetSceneItemTransform',{'sceneName':layer,'sceneItemId':inner}))['sceneItemTransform']
                expected_inner_bounds=(width*source_zoom,height*source_zoom)
                assert abs(inner_transform['boundsWidth']-expected_inner_bounds[0])<transform_tolerance,(label,'inner-width',inner_transform,expected_inner_bounds)
                assert abs(inner_transform['boundsHeight']-expected_inner_bounds[1])<transform_tolerance,(label,'inner-height',inner_transform,expected_inner_bounds)
                assert abs(inner_transform['positionX']-(width-expected_inner_bounds[0])/2)<=inner_position_tolerance,(label,'inner-x',inner_transform)
                assert abs(inner_transform['positionY']-(height-expected_inner_bounds[1])/2)<=inner_position_tolerance,(label,'inner-y',inner_transform)
                if label=='fractional-outer-transformed':
                    filters_after=await call('GetSourceFilterList',{'sourceName':layer})
                    assert len(filters_after['filters'])==1,filters_after
                    assert filters_after['filters'][0]['filterName']=='prism/capture-effects',filters_after
                    optional_geometry_reset={
                        'sameExistingFilterName':filters_after['filters'][0]['filterName'],
                        'filterCountBefore':len(filters_before['filters']),
                        'filterCountAfter':len(filters_after['filters']),
                        'precedingCommitOptionalGeometry':prior_optional_geometry,
                        'precedingCommitObservedInnerBounds':fractional[-1]['actualZoomedBounds'],
                        'replacementCommitOmittedOptionalKeys':omitted_optional_keys,
                        'expectedDefaultContentBoxOrigin':[0,0],
                        'expectedDefaultContentBoxSize':[width,height],
                        'observedInnerBounds':[inner_transform['boundsWidth'],inner_transform['boundsHeight']],
                        'observedInnerPosition':[inner_transform['positionX'],inner_transform['positionY']],
                        'sourceZoom':source_zoom,
                        'transformToleranceSceneUnits':transform_tolerance,
                        'innerPositionToleranceSceneUnits':inner_position_tolerance,
                    }
                await asyncio.sleep(.05)
                shot=await call('GetSourceScreenshot',{'sourceName':composite,'imageFormat':'png'})
                raw=base64.b64decode(shot['imageData'].split(',',1)[1]);image=Image.open(io.BytesIO(raw)).convert('RGBA')
                file=output/f'{stamp}-{label}.png';file.write_bytes(raw)
                if label in ('fractional-outer-transformed','fractional-padded-transformed'):
                    right=(x+math.ceil(expected_content[0])-1,y+int(expected_content[1]//2))
                    right_outside=(x+math.ceil(expected_content[0])+(3 if padding else 0),right[1])
                    bottom=(x+int(expected_content[0]//2),y+math.ceil(expected_content[1])-2)
                    bottom_outside=(bottom[0],y+math.ceil(expected_content[1])+(3 if padding else 0))
                    left_outside=(x-(4 if padding else 1),right[1])
                    top_outside=(bottom[0],y-(4 if padding else 1))
                    assert image.getpixel(right)[3]>0,(label,'right-edge',right,image.getpixel(right),transformed,image.size,image.getchannel('A').getbbox())
                    assert image.getpixel(bottom)[3]>0,(label,'bottom-edge',bottom,image.getpixel(bottom))
                    assert image.getpixel(right_outside)[3]==0,(label,'right-outside',right_outside,image.getpixel(right_outside))
                    assert image.getpixel(bottom_outside)[3]==0,(label,'bottom-outside',bottom_outside,image.getpixel(bottom_outside))
                    assert image.getpixel(left_outside)[3]==0,(label,'left-outside',left_outside,image.getpixel(left_outside))
                    assert image.getpixel(top_outside)[3]==0,(label,'top-outside',top_outside,image.getpixel(top_outside))
                    opaque_interior=(x+10,y+int(expected_content[1]//2))
                    opaque_pixel=image.getpixel(opaque_interior)
                    assert opaque_pixel[3]==255,(label,'opaque-interior',opaque_interior,opaque_pixel)
                    padding_pixels=None
                    outer_paint_bounds=None
                    if padding:
                        left_padding=(x-4,y+int(expected_content[1]//2))
                        right_padding=(x+math.ceil(expected_content[0])+3,left_padding[1])
                        top_padding=(x+int(expected_content[0]//2),y-4)
                        bottom_padding=(x+int(expected_content[0]//2),y+math.ceil(expected_content[1])+3)
                        padding_points=(left_padding,right_padding,top_padding,bottom_padding)
                        assert all(image.getpixel(p)[3]==0 for p in padding_points),(label,'padding',[(p,image.getpixel(p)) for p in padding_points])
                        outer_left,outer_top=expected_position
                        outer_right=outer_left+expected_outer[0]
                        outer_bottom=outer_top+expected_outer[1]
                        crop=(math.floor(outer_left)-2,math.floor(outer_top)-2,
                              math.ceil(outer_right)+2,math.ceil(outer_bottom)+2)
                        painted=image.getchannel('A').crop(crop).getbbox()
                        assert painted is not None,(label,'empty-outer-paint',crop)
                        outer_paint_bounds=(crop[0]+painted[0],crop[1]+painted[1],
                                            crop[0]+painted[2],crop[1]+painted[3])
                        assert (outer_paint_bounds[0]>=math.floor(outer_left) and
                                outer_paint_bounds[1]>=math.floor(outer_top) and
                                outer_paint_bounds[2]<=math.ceil(outer_right) and
                                outer_paint_bounds[3]<=math.ceil(outer_bottom)),(
                                    label,'paint-outside-allocated-outer-box',
                                    outer_paint_bounds,(outer_left,outer_top,outer_right,outer_bottom))
                        padding_pixels={'left':list(image.getpixel(left_padding)),
                                        'right':list(image.getpixel(right_padding)),
                                        'top':list(image.getpixel(top_padding)),
                                        'bottom':list(image.getpixel(bottom_padding))}
                    edge_pixels={'right':list(image.getpixel(right)),'rightOutside':list(image.getpixel(right_outside)),
                                 'bottom':list(image.getpixel(bottom)),'bottomOutside':list(image.getpixel(bottom_outside)),
                                 'leftOutside':list(image.getpixel(left_outside)),'topOutside':list(image.getpixel(top_outside)),
                                 'opaqueInterior':{'coordinate':list(opaque_interior),'rgba':list(opaque_pixel),'alphaRequired':255},
                                 'padding':padding_pixels,
                                 'outerPaintAlphaBounds':outer_paint_bounds,
                                 'outerPaintAlphaThreshold':1 if padding else None,
                                 'outerPaintBoundsTolerancePixels':0 if padding else None}
                elif label=='subpixel-outer-transformed':
                    center_x=int(x+expected_content[0]/2)
                    center_y=int(y+expected_content[1]/2)
                    subpixel_inside=(int(x),center_y)
                    left_outside=(int(x)-1,center_y)
                    right_outside=(int(x)+1,center_y)
                    bottom_inside=(center_x,int(y+math.ceil(expected_content[1])-1))
                    bottom_outside=(center_x,int(y+math.ceil(expected_content[1])))
                    assert image.getpixel(subpixel_inside)[3]>0,(label,'subpixel-pixel',image.getpixel(subpixel_inside),transformed)
                    assert image.getpixel(bottom_inside)[3]>0,(label,'bottom-edge',bottom_inside,image.getpixel(bottom_inside))
                    assert image.getpixel(left_outside)[3]==0,(label,'left-outside',image.getpixel(left_outside))
                    assert image.getpixel(right_outside)[3]==0,(label,'right-outside',image.getpixel(right_outside))
                    top_outside=(center_x,int(y)-1)
                    assert image.getpixel(top_outside)[3]==0,(label,'top-outside',image.getpixel(top_outside))
                    assert image.getpixel(bottom_outside)[3]==0,(label,'bottom-outside',image.getpixel(bottom_outside))
                    edge_pixels={'inside':list(image.getpixel(subpixel_inside)),
                                 'leftOutside':list(image.getpixel(left_outside)),
                                 'rightOutside':list(image.getpixel(right_outside)),
                                 'bottom':list(image.getpixel(bottom_inside)),
                                 'bottomOutside':list(image.getpixel(bottom_outside)),
                                 'topOutside':list(image.getpixel((center_x,y-1)))}
                else:
                    edge_pixels=None
                external.append({'case':label,'requestedBlockSize':[width,height],
                                 'requestedPadding':padding,'expectedRasterExtent':list(outer_size),
                                 'rasterAllocation':list(raster),'outerRasterScale':list(scale),
                                 'sourceZoom':source_zoom,'externalZoom':external_zoom,
                                 'transformToleranceSceneUnits':transform_tolerance,
                                 'innerPositionToleranceSceneUnits':inner_position_tolerance,
                                 'expectedContentSize':list(expected_content),
                                 'expectedTransformedSize':list(expected_outer),'actualTransformedSize':list(actual),
                                 'outerTransform':transformed,
                                 'innerTransform':inner_transform,
                                 'edgePixels':edge_pixels,
                                 'file':str(file)})
            for label,settings in [
                ('missing-outer-dimensions',{**base,'padding':0,'x':0,'y':0,'rotation':0,'effects':[]}),
                ('nonpositive-outer-dimensions',{**base,'width':0,'height':-2,'padding':0,'x':0,'y':0,'rotation':0,'effects':[]}),
            ]:
                if label=='missing-outer-dimensions':
                    settings.pop('width',None);settings.pop('height',None)
                admitted=await call('CallVendorRequest',{'vendorName':'pulsar-capture-effects','requestType':'CommitAppearance','requestData':{'sceneName':composite,'layer':layer,'inputName':source,'itemId':outer,'innerId':inner,'boundsType':'OBS_BOUNDS_STRETCH','blendMode':'OBS_BLEND_NORMAL','visible':True,'settings':settings}})
                assert admitted['responseData'].get('ok'),admitted
                transform=(await call('GetSceneItemTransform',{'sceneName':composite,'sceneItemId':outer}))['sceneItemTransform']
                assert (transform['sourceWidth'],transform['sourceHeight'])==(1,1),(label,transform)
                assert abs(transform['scaleX']-1)<.0001 and abs(transform['scaleY']-1)<.0001,(label,transform)
            assert len((await call('GetSourceFilterList',{'sourceName':layer}))['filters'])==1
            await call('CreateSourceFilter',{'sourceName':layer,'filterName':'prism/capture-rounded','filterKind':'color_filter_v2','filterSettings':{'opacity':1}})
            refused=await call('CallVendorRequest',{'vendorName':'pulsar-capture-effects','requestType':'CommitAppearance','requestData':{'sceneName':composite,'layer':layer,'inputName':source,'itemId':outer,'innerId':inner,'settings':settings}})
            assert not refused['responseData'].get('ok') and 'collision' in refused['responseData']['error'],refused
            await call('RemoveSourceFilter',{'sourceName':layer,'filterName':'prism/capture-rounded'})
            report={'zOrderPreserved':True,'stagedDirectPreserved':True,'stagedOuterHidden':True,'ordinaryDirectRemoved':True,'foreignCollisionPreserved':True,'atomicAdmissions':atomic,'rotationAdmissions':rotations,'fractionalFraming':fractional,'optionalGeometryReset':optional_geometry_reset,'externalGeometry':external,'effects':results,'reset':True,'scope':'GPU fixed-fixture pixels, fractional inner and outer geometry under zoom, integer raster allocation, and 80 atomic admissions; not physical device availability'}
            (output/f'{stamp}-gpu-proof.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report
        finally:
            for kind,data in [('RemoveScene',{'sceneName':composite}),('RemoveScene',{'sceneName':layer}),('RemoveInput',{'inputName':background}),('RemoveInput',{'inputName':source})]:
                try:await call(kind,data)
                except Exception:pass
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=pathlib.Path,required=True);parser.add_argument('--output',type=pathlib.Path,required=True);a=parser.parse_args()
    result=asyncio.run(run(json.loads(a.config.read_text()),a.output.resolve()));print(json.dumps({'effects':len(result['effects']),'reset':result['reset']}))

