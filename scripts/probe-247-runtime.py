"""Reproducible Probe-5 production runtime campaign for Pulsar #247.\n\nUses only public OBS WebSocket v5 and the v1 JSON Schema.  Pass --cycles 100\nfor the 1,000-plus vendor-call campaign; cache pressure always runs separately.\n"""
import argparse, asyncio, base64, hashlib, importlib.util, json, os, pathlib, sys, time
from jsonschema import Draft202012Validator
import websockets


def load_probe(repo: pathlib.Path):
    spec = importlib.util.spec_from_file_location("probe_dual_lane", repo / "scripts" / "probe-dual-lane.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def is_scene_switch_vendor_event(event: dict) -> bool:
    """Match the OBS v5 VendorEvent envelope, not its contract payload."""
    envelope = event.get("eventData") or {}
    return (
        event.get("eventType") == "VendorEvent"
        and envelope.get("vendorName") == "pulsar-scene-switch"
    )


async def run(repo: pathlib.Path, exe: pathlib.Path, cycles: int, abort_race: bool = False, prepare_timeout: bool = False, freeze_race: bool = False, capture_window: str | None = None, cef_workload: bool = False, frame_evidence: pathlib.Path | None = None) -> None:
    p = load_probe(repo)
    schema = json.loads((repo / "scripts/contracts/scene_switch_v1/schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    cef_server = None
    if cef_workload:
        cef_server = p.DeterministicCefServer()
        cef_server.start()
    # The adapter reads this identity at process boot even when no #246 trace
    # is requested; it is scoped to this temporary probe process.
    os.environ["PULSAR_RUNTIME_INSTANCE_ID"] = "probe247-runtime"
    process = p.PulsarProcess(
        exe,
        "x264",
        pathlib.Path("D:/Temp/probe247-recordings"),
        runtime_id="probe247-runtime",
        capture_window=capture_window,
        cef_workload=cef_workload,
        cef_url=cef_server.url if cef_server is not None else None,
    )
    process.spawn()
    try:
        ready = process.wait_for(p.READY_RE, 90)
        if ready.group(2) != process.password:
            raise RuntimeError("PULSAR_READY credential mismatch")
        process.wait_for(p.DUAL_READY_RE, 90)
        async with websockets.connect(ready.group(1), subprotocols=["obswebsocket.json"], open_timeout=20) as ws:
            inbox = p.Inbox()
            await p.identify(ws, process.password)
            # The contract is vendor-only: these must remain normal OBS v5
            # invalid-request-type responses, never become top-level APIs.
            for forbidden in ("Prepare", "Take", "Abort"):
                top = await p.request(inbox, ws, forbidden, f"top-{forbidden}", {})
                status = top.get("requestStatus") or {}
                if status.get("result") or status.get("code") != 204:
                    raise RuntimeError(f"top-level {forbidden} was not OBS 204: {top}")
            if capture_window:
                await p.create_public_lane_scenes(inbox, ws, process, lanes=("A", "B"))
                await p.verify_workload_sources(inbox, ws, process, lanes=("A", "B"))
            elif cef_workload:
                for scene, source, colour in ((p.SCENE_A, p.INPUT_A, p.COLOR_RED_ABGR), (p.SCENE_B, p.INPUT_B, p.COLOR_GREEN_ABGR)):
                    await p.create_scene(inbox, ws, scene, source, colour)
                for lane, scene in (("A", p.SCENE_A), ("B", p.SCENE_B)):
                    await p.create_workload_input(inbox, ws, scene, lane, "browser_source", {"url":process.cef_url_for_lane(lane),"is_local_file":False,"width":1920,"height":1080,"fps_custom":True,"fps":60,"shutdown":False,"restart_when_active":False,"webpage_control_level":0})
            else:
                for scene, source, colour in ((p.SCENE_A, p.INPUT_A, p.COLOR_RED_ABGR), (p.SCENE_B, p.INPUT_B, p.COLOR_GREEN_ABGR)):
                    await p.create_scene(inbox, ws, scene, source, colour)
            for req, rid, data in (("SetCurrentProgramScene", "p247-program", {"sceneName": p.SCENE_A}), ("SetStudioModeEnabled", "p247-studio", {"studioModeEnabled": True}), ("SetCurrentPreviewScene", "p247-preview", {"sceneName": p.SCENE_B})):
                p.assert_success(await p.request(inbox, ws, req, rid, data), req)
            # Keep the encoder/render graph live.  The production probe uses
            # the same real local recording before it asks the dedicated
            # PreviewView for a first rendered frame.
            p.assert_success(await p.request(inbox, ws, "StartRecord", "p247-start-record"), "StartRecord")
            await p.wait_event(inbox, ws, "RecordStateChanged", lambda d: d.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED", timeout=20)

            sent = 0
            last_frame = last_pts = -1
            seen_vendor_events = []
            captured_frames = []
            def validate(event, where):
                errors = list(validator.iter_errors(event))
                if errors:
                    raise RuntimeError(f"schema invalid {where}: {errors[0].message}; {event}")
            async def screenshot_hash(source_name, label):
                deadline = time.monotonic() + 45
                last_failure = "no screenshot response"
                attempt = 0
                while time.monotonic() < deadline:
                    attempt += 1
                    response = await p.request(inbox, ws, "GetSourceScreenshot", f"frame-hash-{label}-{attempt}", {"sourceName":source_name,"imageFormat":"png","imageWidth":1920,"imageHeight":1080})
                    status = response.get("requestStatus") or {}
                    if status.get("result"):
                        try:
                            image_data = (response.get("responseData") or {}).get("imageData")
                            raw_png = base64.b64decode((image_data.split(",", 1)[-1] if isinstance(image_data, str) else ""), validate=True)
                            width, height, channels, pixels = p.decode_png(raw_png)
                            metrics = p.analyse_frame(width, height, channels, pixels)
                            if (width, height) == (1920, 1080) and p.frame_is_nonblack(metrics, require_variance=True):
                                return {"source_name":source_name,"sha256":hashlib.sha256(bytes(pixels)).hexdigest(),"width":width,"height":height,"channels":channels,"distinct_pixels":metrics["distinct"],"nonblack_fraction":metrics["nonblack_ratio"]}
                            last_failure = f"blank/invalid dimensions or metrics: {metrics}"
                        except (TypeError, ValueError, p.zlib.error) as exc:
                            last_failure = f"PNG decode: {exc}"
                    else:
                        last_failure = f"RPC {status}"
                    await asyncio.sleep(0.25)
                raise RuntimeError(f"strict frame hash capture failed for {source_name}: {last_failure}")
            async def capture_role_frames(label, role_map, committed=None):
                lane_scene = {"A":p.SCENE_A, "B":p.SCENE_B}
                record = {"label":label,"role_map":role_map,"program":await screenshot_hash(lane_scene[role_map["on_air"]], f"{label}-program"),"preview":await screenshot_hash(lane_scene[role_map["preview"]], f"{label}-preview")}
                if committed is not None:
                    record["committed_frame_id"] = committed["frame_id"]
                    record["committed_pts_ns"] = committed["pts_ns"]
                captured_frames.append(record)
            async def vendor(reqid, rtype, data, event=True):
                nonlocal sent
                sent += 1
                response = await p.request(inbox, ws, "CallVendorRequest", reqid, {"vendorName":"pulsar-scene-switch", "requestType":rtype, "requestData":data})
                p.assert_success(response, f"CallVendorRequest/{rtype}")
                inner = ((response.get("responseData") or {}).get("responseData") or {})
                if event:
                    validate(inner, f"response/{reqid}")
                return inner
            async def vendor_event(event_type, command_id):
                event = await p.wait_event(inbox, ws, "VendorEvent", lambda d: d.get("vendorName") == "pulsar-scene-switch" and (d.get("eventData") or {}).get("event_type") == event_type and (d.get("eventData") or {}).get("command_id") == command_id, timeout=20)
                # OBS v5 wraps vendor events as VendorEvent(eventData =
                # {vendorName,eventType,eventData}); validate the contract
                # payload, not the transport envelope.
                inner = (event.get("eventData") or {}).get("eventData") or {}
                validate(inner, f"VendorEvent/{event_type}/{command_id}")
                seen_vendor_events.append(inner)
                return inner
            async def state():
                data = await vendor(f"state-{sent}", "GetState", {}, event=False)
                for key in ("runtime_instance_id", "revisions", "role_map", "server_seq", "state"):
                    if key not in data: raise RuntimeError(f"GetState missing {key}: {data}")
                return data

            if frame_evidence is not None:
                initial = await state()
                await capture_role_frames("before_take_001", initial["role_map"])

            for n in range(1, cycles + 1):
                before = await state()
                rev, seq, roles = before["revisions"], before["server_seq"], before["role_map"]
                lane = roles["preview"]
                scene = p.SCENE_B if lane == "B" else p.SCENE_A
                intent, prepare_id, take_id = f"intent-{n}", f"prepare-{n}", f"take-{n}"
                prepare = {"contract":"pulsar.scene-switch.v1","schema_version":1,"message_type":"command","command_type":"Prepare","command_id":prepare_id,"intent_id":intent,"runtime_instance_id":"probe247-runtime","expected_revisions":rev,"expected_server_seq":seq,"target":{"lane_id":lane,"scene_id":scene},"timeout_ms":1 if prepare_timeout else 5000}
                if n == 1: print(json.dumps({"initial_state":before,"prepare":prepare}, sort_keys=True), flush=True)
                accepted = await vendor(f"call-{prepare_id}", "Prepare", prepare)
                if accepted["event_type"] != "PrepareAccepted": raise RuntimeError(accepted)
                await vendor_event("PrepareAccepted", prepare_id)
                if prepare_timeout:
                    expired = await vendor_event("CommandRejected", prepare_id)
                    if expired.get("error_code") != "TIMEOUT": raise RuntimeError(f"expected one-shot Prepare timeout: {expired}")
                    timeout_state = await state()
                    if timeout_state["state"] != "ready" or timeout_state["role_map"] != roles:
                        raise RuntimeError(f"timeout mutated roles or failed to settle: {before} -> {timeout_state}")
                    print(json.dumps({"prepare_timeout":"TIMEOUT","server_seq":timeout_state["server_seq"],"role_map":timeout_state["role_map"]}), flush=True)
                    return
                # Five genuinely transmitted non-mutating protocol adversaries per cycle.
                for suffix, payload in (("replay", prepare), ("conflict", {**prepare, "target":{"lane_id":lane,"scene_id":p.SCENE_A if scene==p.SCENE_B else p.SCENE_B}}), ("stale", {**prepare, "command_id":f"stale-{n}", "intent_id":f"stale-i-{n}", "expected_revisions":{"program":0,"preview":0,"role_map":0}}), ("runtime", {**prepare, "command_id":f"wrong-runtime-{n}", "intent_id":f"wrong-runtime-i-{n}", "runtime_instance_id":"other-runtime"}), ("nul", {**prepare, "command_id":f"nul-{n}", "intent_id":f"nul-i-{n}", "target":{"lane_id":lane,"scene_id":"scene\\u0000suffix"}}), ("uint64-overflow", {**prepare, "command_id":f"overflow-{n}", "intent_id":f"overflow-i-{n}", "expected_server_seq":18446744073709551616}), ("scientific", {**prepare, "command_id":f"scientific-{n}", "intent_id":f"scientific-i-{n}", "expected_server_seq":1e20}), ("invalid", {"command_type":"Prepare"})):
                    ev = await vendor(f"{suffix}-{n}", "Prepare", payload)
                    if suffix == "replay" and ev["event_type"] != "PrepareAccepted": raise RuntimeError(ev)
                    if suffix != "replay" and ev["event_type"] != "CommandRejected": raise RuntimeError(ev)
                ready_ev = await vendor_event("PreviewReady", prepare_id)
                if ready_ev["first_frame_id"] < 0 or ready_ev["first_pts_ns"] < 0: raise RuntimeError(ready_ev)
                # Each deliberate vendor rejection above is an observable
                # event and therefore advances server_seq.  Read the current
                # cached state before the legitimate Take; do not pretend the
                # PreviewReady sequence is still current.
                take_state = await state()
                take = {"contract":"pulsar.scene-switch.v1","schema_version":1,"message_type":"command","command_type":"Take","command_id":take_id,"intent_id":intent,"runtime_instance_id":"probe247-runtime","expected_revisions":take_state["revisions"],"expected_server_seq":take_state["server_seq"],"prepared_command_id":prepare_id,"timeout_ms":5000}
                if freeze_race:
                    # Place the standard OBS Preview mutation directly behind
                    # the accepted Take on the same socket, before allowing a
                    # network round-trip.  It must hit the shared mutation
                    # gate while the atomic cut is pending.
                    await ws.send(json.dumps({"op":6,"d":{"requestType":"CallVendorRequest","requestId":"freeze-race-take","requestData":{"vendorName":"pulsar-scene-switch","requestType":"Take","requestData":take}}}))
                    await ws.send(json.dumps({"op":6,"d":{"requestType":"CreateInput","requestId":"freeze-race-mutate","requestData":{"sceneName":scene,"inputName":"freeze-race-source","inputKind":"color_source_v3","inputSettings":{"color":4278190335,"width":16,"height":16},"sceneItemEnabled":True}}}))
                    sent += 1
                    take_resp = await inbox.receive_until_response(ws, "freeze-race-take")
                    mutate_resp = await inbox.receive_until_response(ws, "freeze-race-mutate")
                    p.assert_success(take_resp, "freeze-race Take")
                    take_inner = ((take_resp.get("responseData") or {}).get("responseData") or {})
                    validate(take_inner, "freeze-race Take response")
                    status = mutate_resp.get("requestStatus") or {}
                    if status.get("result") or status.get("code") != 702 or "PREVIEW_FROZEN" not in str(status.get("comment") or ""):
                        raise RuntimeError(f"Preview mutation escaped freeze gate: {mutate_resp}")
                    committed = await vendor_event("TakeCommitted", take_id)
                    print(json.dumps({"freeze_race":"PREVIEW_FROZEN","terminal":committed["event_type"]}), flush=True)
                    return
                if abort_race:
                    # Send Take and its valid same-intent Abort without
                    # waiting for either response.  This exercises the real
                    # frame-boundary commit-vs-abort race, not a synthetic
                    # state injection.  Either one terminal outcome is valid;
                    # the loser must be a rejection and no second terminal
                    # event may appear.
                    abort = {"contract":"pulsar.scene-switch.v1","schema_version":1,"message_type":"command","command_type":"Abort","command_id":"abort-race-1","intent_id":intent,"runtime_instance_id":"probe247-runtime","expected_revisions":take_state["revisions"],"take_command_id":take_id,"reason":"operator"}
                    for rid, rtype, payload in (("race-take", "Take", take), ("race-abort", "Abort", abort)):
                        await ws.send(json.dumps({"op":6,"d":{"requestType":"CallVendorRequest","requestId":rid,"requestData":{"vendorName":"pulsar-scene-switch","requestType":rtype,"requestData":payload}}}))
                        sent += 1
                    take_resp = await inbox.receive_until_response(ws, "race-take")
                    abort_resp = await inbox.receive_until_response(ws, "race-abort")
                    p.assert_success(take_resp, "race Take"); p.assert_success(abort_resp, "race Abort")
                    take_inner = ((take_resp.get("responseData") or {}).get("responseData") or {})
                    abort_inner = ((abort_resp.get("responseData") or {}).get("responseData") or {})
                    validate(take_inner, "race Take response"); validate(abort_inner, "race Abort response")
                    terminal = await p.wait_event(inbox, ws, "VendorEvent", lambda d: d.get("vendorName") == "pulsar-scene-switch" and (d.get("eventData") or {}).get("command_id") in (take_id, "abort-race-1") and (d.get("eventData") or {}).get("event_type") in ("TakeCommitted","TakeAborted"), timeout=5)
                    terminal_inner = ((terminal.get("eventData") or {}).get("eventData") or {})
                    validate(terminal_inner, "race terminal")
                    await asyncio.sleep(0.1)
                    duplicates = [e for e in inbox.events if e.get("eventType") == "VendorEvent" and ((e.get("eventData") or {}).get("eventData") or {}).get("event_type") in ("TakeCommitted","TakeAborted") and ((e.get("eventData") or {}).get("eventData") or {}).get("command_id") in (take_id, "abort-race-1")]
                    if duplicates: raise RuntimeError(f"second terminal event in abort race: {duplicates}")
                    print(json.dumps({"abort_race_take":take_inner["event_type"],"abort_race_abort":abort_inner["event_type"],"terminal":terminal_inner["event_type"]}), flush=True)
                    return
                take_ok = await vendor(f"call-{take_id}", "Take", take)
                if take_ok["event_type"] != "TakeAccepted": raise RuntimeError(take_ok)
                await vendor_event("TakeAccepted", take_id)
                take_replay_pending = await vendor(f"replay-pending-{take_id}", "Take", take)
                if json.dumps(take_replay_pending, sort_keys=True, separators=(",",":")) != json.dumps(take_ok, sort_keys=True, separators=(",",":")):
                    raise RuntimeError(f"Take retry before terminal was not byte-equivalent to initial TakeAccepted: {take_ok} != {take_replay_pending}")
                pending_state = await state()
                if pending_state["state"] not in ("take_accepted", "ready"):
                    raise RuntimeError(f"GetState was not serviced after Take: {pending_state}")
                if pending_state["state"] == "take_accepted":
                    # Mutating the future Preview during the freeze must not
                    # succeed.  A one-frame cut may also commit before a
                    # subsequent WebSocket request is handled; that is a
                    # post-boundary observation, not a blocked GetState.
                    frozen = await p.request(inbox, ws, "CreateInput", f"freeze-{n}", {"sceneName":scene,"inputName":f"frozen-{n}","inputKind":"color_source_v3","inputSettings":{"color":4278190335,"width":16,"height":16},"sceneItemEnabled":True})
                    if (frozen.get("requestStatus") or {}).get("result"):
                        raise RuntimeError("Preview mutation succeeded while Take was pending")
                double = await vendor(f"double-{n}", "Take", {**take,"command_id":f"double-{n}"})
                if double["event_type"] != "CommandRejected": raise RuntimeError(double)
                committed = await vendor_event("TakeCommitted", take_id)
                take_replay_after = await vendor(f"replay-after-{take_id}", "Take", take)
                if json.dumps(take_replay_after, sort_keys=True, separators=(",",":")) != json.dumps(take_ok, sort_keys=True, separators=(",",":")):
                    raise RuntimeError(f"Take retry after commit did not replay initial TakeAccepted: {take_ok} != {take_replay_after}")
                if sum(1 for item in seen_vendor_events if item.get("event_type") == "TakeCommitted" and item.get("command_id") == take_id) != 1:
                    raise RuntimeError(f"terminal TakeCommitted VendorEvent was not unique for {take_id}")
                if committed["frame_id"] <= last_frame or committed["pts_ns"] <= last_pts: raise RuntimeError(f"nonmonotonic boundary: {committed}")
                last_frame, last_pts = committed["frame_id"], committed["pts_ns"]
                after = await state()
                if after["role_map"]["on_air"] != lane or after["role_map"]["preview"] == lane: raise RuntimeError(f"role swap invalid: {before} -> {after}")
                if frame_evidence is not None and n in (1, cycles):
                    await capture_role_frames(f"after_take_{n:03d}", after["role_map"], committed)
                if n in (1, cycles) or n % 25 == 0: print(f"cycle={n} frame={last_frame} pts={last_pts} sent={sent}", flush=True)
            if cycles >= 100 and sent < 1000:
                raise RuntimeError(f"only {sent} CallVendorRequest attempts sent")
            # Every unconsumed VendorEvent is a rejection/replay side effect;
            # validate it too rather than treating only lifecycle happy-path
            # events as schema evidence.
            for event in inbox.events:
                if is_scene_switch_vendor_event(event):
                    validate(((event.get("eventData") or {}).get("eventData") or {}), "unconsumed VendorEvent")
            if frame_evidence is not None:
                frame_evidence.parent.mkdir(parents=True, exist_ok=True)
                with exe.open("rb") as binary:
                    exe_sha256 = hashlib.file_digest(binary, "sha256").hexdigest()
                frame_evidence.write_text(json.dumps({"format":"probe-247-frame-evidence.v1","capture_mode":"cef_only" if cef_workload and not capture_window else "wgc_cef","exe_sha256":exe_sha256,"samples":captured_frames}, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"cycles":cycles,"vendor_attempts":sent,"last_frame_id":last_frame,"last_pts_ns":last_pts}), flush=True)
    finally:
        process.shutdown()
        if cef_server is not None:
            cef_server.close()
        if process.proc and process.proc.poll() is None: raise RuntimeError("residual pulsar.exe process")


async def cache_pressure(repo: pathlib.Path, exe: pathlib.Path) -> None:
    """Use a separate process so saturation cannot contaminate the 100-cycle run."""
    p = load_probe(repo)
    schema = json.loads((repo / "scripts/contracts/scene_switch_v1/schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    os.environ["PULSAR_RUNTIME_INSTANCE_ID"] = "probe247-cache"
    process = p.PulsarProcess(exe, "x264", pathlib.Path("D:/Temp/probe247-cache-recordings"), runtime_id="probe247-cache")
    process.spawn()
    try:
        ready = process.wait_for(p.READY_RE, 90)
        async with websockets.connect(ready.group(1), subprotocols=["obswebsocket.json"], open_timeout=20) as ws:
            inbox = p.Inbox()
            await p.identify(ws, process.password)
            async def call(request_id, payload, check_event=True):
                response = await p.request(inbox, ws, "CallVendorRequest", request_id, {"vendorName":"pulsar-scene-switch", "requestType":"Take", "requestData":payload})
                p.assert_success(response, request_id)
                inner = ((response.get("responseData") or {}).get("responseData") or {})
                if check_event:
                    errors = list(validator.iter_errors(inner))
                    if errors: raise RuntimeError(f"cache response schema invalid: {errors[0].message}; {inner}")
                return inner
            async def state():
                response = await p.request(inbox, ws, "CallVendorRequest", "cache-state", {"vendorName":"pulsar-scene-switch", "requestType":"GetState", "requestData":{}})
                p.assert_success(response, "cache-state")
                return ((response.get("responseData") or {}).get("responseData") or {})
            baseline = await state()
            base = {"contract":"pulsar.scene-switch.v1","schema_version":1,"message_type":"command","command_type":"Take","intent_id":"cache-intent-0","runtime_instance_id":"probe247-cache","expected_revisions":baseline["revisions"],"prepared_command_id":"missing-preparation","timeout_ms":1000}
            known = None
            for number in range(4096):
                command = {**base, "command_id":f"cache-{number}", "intent_id":f"cache-intent-{number}"}
                event = await call(f"cache-call-{number}", command)
                if event.get("event_type") != "CommandRejected" or event.get("error_code") != "PREPARE_NOT_FOUND": raise RuntimeError(event)
                if number == 0: known = command
            full = await state()
            if full.get("idempotency_cache_entries") != 4096 or full.get("idempotency_cache_capacity") != 4096: raise RuntimeError(f"cache was not exactly full: {full}")
            replay = await call("cache-replay", known)
            if replay.get("event_type") != "CommandRejected" or replay.get("command_id") != "cache-0": raise RuntimeError(f"known replay lost at capacity: {replay}")
            overflow = await call("cache-overflow", {**base, "command_id":"cache-overflow", "intent_id":"cache-overflow-intent"})
            if overflow.get("event_type") != "CommandRejected" or overflow.get("error_code") != "SCHEMA_INVALID": raise RuntimeError(f"new entry did not fail closed: {overflow}")
            after = await state()
            for key in ("revisions", "role_map", "idempotency_cache_entries", "idempotency_cache_capacity"):
                if after.get(key) != full.get(key): raise RuntimeError(f"cache saturation mutated {key}: {full} -> {after}")
            for event in inbox.events:
                if is_scene_switch_vendor_event(event):
                    errors = list(validator.iter_errors(((event.get("eventData") or {}).get("eventData") or {})))
                    if errors: raise RuntimeError(f"cache VendorEvent schema invalid: {errors[0].message}")
            print(json.dumps({"cache_entries":after["idempotency_cache_entries"],"cache_capacity":after["idempotency_cache_capacity"],"overflow":"fail_closed"}), flush=True)
    finally:
        process.shutdown()
        if process.proc and process.proc.poll() is None: raise RuntimeError("residual cache pressure pulsar.exe process")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=pathlib.Path, required=True)
    ap.add_argument("--exe", type=pathlib.Path, required=True)
    ap.add_argument("--cycles", type=int, default=100)
    ap.add_argument("--cache-pressure", action="store_true")
    ap.add_argument("--abort-race", action="store_true")
    ap.add_argument("--prepare-timeout", action="store_true")
    ap.add_argument("--freeze-race", action="store_true")
    ap.add_argument("--capture-window", help="exact visible WGC descriptor; required with --cef-workload")
    ap.add_argument("--cef-workload", action="store_true", help="exercise strict WGC+CEF frame hashing")
    ap.add_argument("--frame-evidence", type=pathlib.Path, help="write captured Program/Preview pixel SHA-256 evidence")
    args = ap.parse_args()
    if args.frame_evidence and not args.cef_workload:
        ap.error("--frame-evidence requires --cef-workload")
    asyncio.run(run(args.repo, args.exe, args.cycles, args.abort_race, args.prepare_timeout, args.freeze_race, args.capture_window, args.cef_workload, args.frame_evidence))
    if args.cache_pressure:
        asyncio.run(cache_pressure(args.repo, args.exe))
