const root='D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20261008-webm-transition/evidence/local-20261008-webm-transition/eleven';
const socket=new WebSocket((await(await fetch('http://127.0.0.1:9235/json/list')).json()).find(x=>x.type==='node').webSocketDebuggerUrl);
await new Promise((done,fail)=>{socket.onopen=done;socket.onerror=fail});
let id=0;const pending=new Map();socket.onmessage=e=>{const x=JSON.parse(e.data);if(x.id){pending.get(x.id)?.(x);pending.delete(x.id)}};
const call=(method,params)=>new Promise(done=>{pending.set(++id,done);socket.send(JSON.stringify({id,method,params}))});
const fn=(await call('Runtime.evaluate',{expression:`process.getBuiltinModule('module').createRequire(process.cwd()+'/package.json')('electron').ipcMain._invokeHandlers.get('broadcast:status')`})).result.result;
const scopes=(await call('Runtime.getProperties',{objectId:fn.objectId})).result.internalProperties.find(x=>x.name==='[[Scopes]]');
const list=(await call('Runtime.getProperties',{objectId:scopes.value.objectId,ownProperties:true})).result.result;
const found=[];
for(const scope of list.filter(x=>/^\d+$/.test(x.name))){
  const vars=(await call('Runtime.getProperties',{objectId:scope.value.objectId,ownProperties:true})).result.result;
  for(const [name,dest] of [['engine','__webmTestEngine'],['PulsarSceneSwitch','__webmTestSwitcherClass']]){
    const item=vars.find(x=>x.name===name);
    if(item?.value?.objectId){await call('Runtime.callFunctionOn',{objectId:item.value.objectId,functionDeclaration:`function(){globalThis.${dest}=this}`,returnByValue:true});found.push(name)}
  }
}
if(found.length!==2)throw new Error('Required Prism owners unavailable');
async function exercise(root) {
  const e=globalThis.__webmTestEngine, fs=process.getBuiltinModule('fs');
  const report={startedAt:new Date().toISOString(),provenance:'Running Prism BroadcastEngine client and unmodified PulsarSceneSwitch adapter; development inspector invocation; no UI automation',limitations:['Preview UI bridge SetPreviewComposite is absent from tested Pulsar branch','No subjective visual or audio verdict','Development reloads interrupted prior visual sampling'],events:[]};
  const stamp=report.startedAt.replace(/[-:]/g,'').replace(/\.\d{3}Z$/,'Z'), file=root+'/'+stamp+'-prism-runtime-test.json';
  const save=()=>{fs.mkdirSync(root,{recursive:true});fs.writeFileSync(file,JSON.stringify(report,null,2))};save();
  let obs,listener;
  try {
    report.before=e.status();report.recordingBefore=await e.recordStatus();
    if(report.before.state!=='idle'||report.recordingBefore.active)throw new Error('Real output active');
    const p=await e.ensure({});obs=p.client.obs;report.pid=p.child.pid;report.runtime=p.runtimeInstanceId;
    const v=async(t,d={})=>(await obs.call('CallVendorRequest',{vendorName:'pulsar-transitions',requestType:t,requestData:d})).responseData;
    const state=await v('GetState');if(!state.operational)throw new Error('Runtime unavailable');
    const config={path:'D:/Documents/Zab/Artifacts/2026-10-08/zab-transition-trace-sound/zab-smooth-alpha.webm',cut_point_ms:875,volume:1,muted:false};
    report.configured=await v('Configure',{runtime_instance_id:p.runtimeInstanceId,config});
    for(let i=0;i<100;i++){report.ready=await v('GetState');if(report.ready.ready)break;await new Promise(r=>setTimeout(r,50))}
    if(!report.ready.ready)throw new Error('WebM not ready');
    const names=['WebMProbe.Program','WebMProbe.Outgoing','WebMProbe.Incoming'];
    const existing=(await obs.call('GetSceneList')).scenes.map(s=>s.sceneName);
    for(const [i,name] of names.entries())if(!existing.includes(name)){await obs.call('CreateScene',{sceneName:name});await obs.call('CreateInput',{sceneName:name,inputName:name+'.Color',inputKind:'color_source_v3',inputSettings:{width:1920,height:1080,color:[0xff456232,0xff423524,0xffdddce8][i]},sceneItemEnabled:true})}
    await obs.call('SetCurrentProgramScene',{sceneName:names[0]});await obs.call('SetStudioModeEnabled',{studioModeEnabled:true});await obs.call('SetCurrentPreviewScene',{sceneName:names[1]});
    listener=x=>{if(['pulsar-transitions','pulsar-scene-switch'].includes(x.vendorName))report.events.push(x)};obs.on('VendorEvent',listener);
    const commandId='prism-native-'+Date.now();const before=await v('GetState');
    report.previewAccepted=await v('SwitchLane',{runtime_instance_id:p.runtimeInstanceId,command_id:commandId,lane_id:before.role_map.preview,expected_scene_name:names[1],scene_name:names[2]});save();
    if(report.previewAccepted.status!=='accepted')throw new Error(JSON.stringify(report.previewAccepted));
    for(let i=0;i<100;i++){report.previewResult=await v('GetResult',{runtime_instance_id:p.runtimeInstanceId,command_id:commandId});if(report.previewResult.status!=='accepted')break;await new Promise(r=>setTimeout(r,40))}
    if(report.previewResult.status!=='completed')throw new Error('Preview did not complete');
    report.afterPreview=await obs.call('GetSceneList');save();
    const switcher=new globalThis.__webmTestSwitcherClass(obs,p.runtimeInstanceId);
    report.take=await switcher.prepareAndTake(await switcher.getState(),names[1]);
    report.afterTake=await obs.call('GetSceneList');
    report.cleared=await v('Clear',{runtime_instance_id:p.runtimeInstanceId});
    report.passed=report.previewResult.frame_id>0&&report.take.frameId>0&&report.afterPreview.currentProgramSceneName===names[0]&&report.afterTake.currentProgramSceneName===names[1]&&!report.cleared.configured;
  }catch(error){report.error=error.message;report.passed=false}finally{if(obs&&listener)obs.off('VendorEvent',listener);report.after=e.status();report.recordingAfter=await e.recordStatus();report.finishedAt=new Date().toISOString();save()}
  return {file,passed:report.passed,error:report.error,preview:report.previewResult,take:report.take,after:report.after};
}
const result=await call('Runtime.evaluate',{expression:`(${exercise.toString()})(${JSON.stringify(root)})`,awaitPromise:true,returnByValue:true});console.log(JSON.stringify(result));socket.close();
