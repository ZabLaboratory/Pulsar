#!/usr/bin/env python3
"""Private CEF proof for one live Vision /2 → Solar → Prism → Pulsar chain.

The runner is intentionally gated twice because it starts a private native
host and uses its GPU. It reuses only already-qualified binaries; no build is
performed. Every external request is loopback-only and the host owns a unique
scratch runtime and shutdown event.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence/local-20261017-cef-coverage-chain/eleven"
CACHE = ROOT / ".cache/cef-coverage-chain"
SOLAR = Path(r"D:\Documents\Zab\Solar\.worktrees\eleven-local-20261015-project-integration")
PRISM = Path(r"D:\Documents\Zab\Prism\.worktrees\eleven-local-20261015-project-integration")
CANVAS_SIZE = (322, 546)
SCENE_SIZE = (320, 180)
SAMPLE_POINTS = {
    "partial": (16, 40),
    "overlap": (36, 40),
    "clippedOverflow": (62, 40),
    "unclippedChild": (110, 40),
    "stencilFrameOverflow": (124, 40),
}


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load helper module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_record(path: Path) -> dict:
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def pixel(width: int, channels: int, pixels: bytearray, point: tuple[int, int]) -> list[int]:
    x, y = point
    offset = (y * width + x) * channels
    values = list(pixels[offset:offset + channels])
    if channels == 3:
        values.append(255)
    return values


def assert_close(actual: list[int], expected: list[int], tolerance: int, label: str) -> None:
    if any(abs(a - e) > tolerance for a, e in zip(actual[:3], expected[:3])):
        raise AssertionError({"label": label, "actual": actual, "expected": expected, "tolerance": tolerance})


def rgba_near_alpha(actual: list[int], expected: int, tolerance: int, label: str) -> None:
    if actual[3] < expected - tolerance or actual[3] > expected + tolerance:
        raise AssertionError({"label": label, "rgba": actual, "expectedAlpha": expected, "tolerance": tolerance})
    if max(actual[:3]) - min(actual[:3]) > 2:
        raise AssertionError({"label": label, "coverageRGBNotGray": actual})
    if abs(actual[0] - actual[3]) > tolerance:
        raise AssertionError({"label": label, "coverageColorAlphaMismatch": actual})


def validate_atlas_samples(browser, frame: dict, png: bytes, expected_partial: int) -> dict:
    receipt = frame["message"]["composition"]
    coverage = receipt.get("coverageSurfaces")
    if receipt.get("schema") != "vision.native-composition-frame/2" or not coverage:
        raise AssertionError("CEF page did not submit a real Vision /2 coverage receipt")
    width, height, channels, pixels = browser.decode_png(png)
    if (width, height) != tuple(receipt["canvasSize"]):
        raise AssertionError({"cefAtlasSize": [width, height], "receiptCanvasSize": receipt["canvasSize"]})
    if (width, height) != CANVAS_SIZE:
        raise AssertionError({"cefAtlasSize": [width, height], "qualifiedAtlasSize": CANVAS_SIZE})
    if channels != 4:
        raise AssertionError({"cefAtlasChannels": channels, "required": "RGBA to retain mask coverage alpha"})
    surface = coverage[0]
    rect = surface["atlasRect"]
    samples = {}
    expected_by_name = {
        "partial": expected_partial,
        "overlap": expected_partial,
        "clippedOverflow": 0,
        "unclippedChild": expected_partial,
        "stencilFrameOverflow": 0,
    }
    for label, (scene_x, scene_y) in SAMPLE_POINTS.items():
        atlas_point = (rect[0] + scene_x, rect[1] + scene_y)
        rgba = pixel(width, channels, pixels, atlas_point)
        expected = expected_by_name[label]
        tolerance = 2 if expected == 0 else 3
        rgba_near_alpha(rgba, expected, tolerance, label)
        samples[label] = {"scenePoint": [scene_x, scene_y], "atlasPoint": list(atlas_point),
                          "rgba": rgba, "expectedAlpha": expected}
    return {"path": None, "sha256": sha256_bytes(png), "dimensions": [width, height],
            "channels": channels, "coverageSurface": surface, "samples": samples}


async def run_probe(args) -> dict:
    if os.name != "nt":
        raise RuntimeError("This probe requires the pinned Windows Pulsar/CEF runtime")
    if not args.execute or not args.gpu_slot_released:
        raise RuntimeError("both --execute and --gpu-slot-released are required")

    preflight = load("coverage_chain_preflight", ROOT / "scripts/cef-coverage-chain/preflight.py")
    preflight_report = preflight.run()
    browser = load("coverage_chain_browser", ROOT / "scripts/probe-browser-m3.py")
    process_helpers = load("coverage_chain_group", ROOT / "scripts/probe-group-composition.py")
    process_protocol = load("coverage_chain_process", ROOT / "scripts/probe-websocket.py")
    protocol = load("coverage_chain_protocol", ROOT / "scripts/probe-source-kinds.py")
    page_module = load("coverage_chain_page", ROOT / "scripts/cef-coverage-chain/http-page.py")
    websockets = __import__("websockets")

    native_host_source = preflight.PULSAR_NATIVE / preflight.PULSAR_ARTIFACTS["nativeHost"][0]
    native_runtime = native_host_source.parent.parent.parent
    native_plugin_dir = native_runtime / "obs-plugins/64bit"
    capture_source = preflight.PULSAR_COVERAGE / preflight.PULSAR_ARTIFACTS["capturePlugin"][0]
    cef_browser_source = preflight.PULSAR_CEF / preflight.PULSAR_ARTIFACTS["cefBrowser"][0]
    cef_overlay = cef_browser_source.parent
    stamp = utc_stamp()
    token = uuid.uuid4().hex
    scratch = (CACHE / f"run-{token}").resolve()
    if scratch.parent != CACHE.resolve() or scratch.exists():
        raise RuntimeError(f"private scratch path is not new and contained: {scratch}")
    scratch.mkdir(parents=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    report_path = args.output.resolve() if args.output else EVIDENCE / f"{stamp}-cef-coverage-chain.json"
    if report_path.parent != EVIDENCE.resolve() or report_path.exists():
        raise ValueError(f"output must be a new direct child of {EVIDENCE}")
    artifacts = {}
    report = {
        "schema": "pulsar.vision-solar-prism-cef-native-coverage-chain/1",
        "passed": False,
        "startedUtc": datetime.now(timezone.utc).isoformat(),
        "runtimeStarted": False,
        "buildStarted": False,
        "gpuUsed": False,
        "gpuSlotReleasedByParent": True,
        "preflight": preflight_report,
        "scratch": str(scratch),
        "reportPath": str(report_path),
        "sourcePins": {
            "solar": preflight.PINS["solarCommit"],
            "prismRuntimeHead": preflight.PINS["prismCommit"],
            "prismProductBaseline": preflight.PINS["prismProductBaselineCommit"],
            "vision": preflight.PINS["visionCommit"],
            "visionBaselinePresenter": preflight.PINS["baselinePresenterCommit"],
            "pulsarNativeSource": preflight.PINS["nativeSourceCommit"],
            "pulsarCefSource": preflight.PINS["cefSourceCommit"],
        },
        "selectedWebsocket": "retained native d3ecaf55; CEF-reported 3130c2cf is not present in overlay and is not copied",
        "evidence": {},
    }
    process = None
    page = page_module.ChainPageServer(
        ROOT / "scripts/cef-coverage-chain", SOLAR, preflight.VISION
    )
    old_env = {key: value for key, value in os.environ.items() if key.startswith("PULSAR_")}
    runtime_id = "cef-coverage-" + token
    password = uuid.uuid4().hex + uuid.uuid4().hex
    scene_name = "cef-coverage-" + token[:12]
    names = {
        "backdrop": f"{scene_name}-backdrop",
        "binding": f"{scene_name}-capture-binding",
        "contribution": f"{scene_name}-capture-contribution",
        "browser": f"{scene_name}-vision-atlas",
    }
    try:
        runtime = scratch / "runtime"
        for directory in ("bin", "data", "obs-plugins"):
            shutil.copytree(native_runtime / directory, runtime / directory)
        plugin_dir = runtime / "obs-plugins/64bit"
        if not plugin_dir.is_dir():
            raise FileNotFoundError(plugin_dir)
        if any("browser" in path.name.casefold() for path in plugin_dir.glob("*.dll")):
            raise RuntimeError("native base already contains a browser module; refusing mixed CEF owners")
        staged_websocket = plugin_dir / "obs-websocket.dll"
        if sha256_file(staged_websocket) != preflight.PULSAR_ARTIFACTS["nativeWebsocket"][1]:
            raise RuntimeError("native d3ecaf55 obs-websocket binary was not present in the private base")
        if (cef_overlay / "obs-websocket.dll").exists():
            raise RuntimeError("CEF overlay contains obs-websocket.dll; refusing ABI substitution")
        shutil.copyfile(capture_source, plugin_dir / "pulsar-capture-effects.dll")
        shutil.copytree(cef_overlay, plugin_dir, dirs_exist_ok=True)
        staged_websocket = plugin_dir / "obs-websocket.dll"
        if sha256_file(staged_websocket) != preflight.PULSAR_ARTIFACTS["nativeWebsocket"][1]:
            raise RuntimeError("CEF overlay changed the selected native websocket binary")
        executable = runtime / "bin/64bit/pulsar.exe"
        stage_assets = {
            "pulsar.exe": file_record(executable),
            "obs.dll": file_record(executable.parent / "obs.dll"),
            "obs-websocket.dll": file_record(staged_websocket),
            "pulsar-capture-effects.dll": file_record(plugin_dir / "pulsar-capture-effects.dll"),
            "pulsar-browser.dll": file_record(plugin_dir / "pulsar-browser.dll"),
            "pulsar-browser-page.exe": file_record(plugin_dir / "pulsar-browser-page.exe"),
            "libcef.dll": file_record(plugin_dir / "libcef.dll"),
        }
        if stage_assets["pulsar.exe"]["sha256"] != preflight.PULSAR_ARTIFACTS["nativeHost"][1]:
            raise RuntimeError("private stage host hash mismatch")
        if stage_assets["obs.dll"]["sha256"] != preflight.PULSAR_ARTIFACTS["nativeObs"][1]:
            raise RuntimeError("private stage obs.dll hash mismatch")
        if stage_assets["pulsar-capture-effects.dll"]["sha256"] != preflight.PULSAR_ARTIFACTS["capturePlugin"][1]:
            raise RuntimeError("private stage capture plugin hash mismatch")
        for key, artifact_key in (("pulsar-browser.dll", "cefBrowser"),
                                  ("pulsar-browser-page.exe", "cefHelper"),
                                  ("libcef.dll", "cefLib")):
            if stage_assets[key]["sha256"] != preflight.PULSAR_ARTIFACTS[artifact_key][1]:
                raise RuntimeError(f"private stage CEF hash mismatch for {key}")
        report["stagedRuntime"] = {
            "sourceRuntime": str(native_runtime),
            "cefOverlay": str(cef_overlay),
            "capturePluginSource": str(capture_source),
            "artifactPins": stage_assets,
            "cefOverlayFileCount": sum(1 for path in cef_overlay.rglob("*") if path.is_file()),
            "cefReportedWebsocketDllCopied": False,
        }

        page.start()
        page_started = True
        for key in list(os.environ):
            if key.startswith("PULSAR_"):
                del os.environ[key]
        os.environ.update({
            "PULSAR_RUNTIME_ROOT": str(scratch / "state"),
            "PULSAR_RUNTIME_DIR": str(scratch / "state/runtime"),
            "PULSAR_RUNTIME_INSTANCE_ID": runtime_id,
            "PULSAR_RESOLUTION": f"{SCENE_SIZE[0]}x{SCENE_SIZE[1]}",
            "PULSAR_FPS": "30",
            "PULSAR_DESKTOP_AUDIO_DEVICE_ID": "pulsar-synthetic-test-no-device",
        })
        process = process_helpers.GracefulPrivatePulsarProcess(
            process_protocol, executable, process_protocol.pick_free_port(), password
        )
        process.spawn()
        report["runtimeStarted"] = True
        report["gpuUsed"] = True
        report["ownedPid"] = process.proc.proc.pid
        report["websocketPort"] = process.proc.port
        process.wait_ready(60)
        report["hostReady"] = True

        async def exercise() -> None:
            async with websockets.connect(
                f"ws://127.0.0.1:{process.proc.port}",
                subprotocols=["obswebsocket.json"],
                max_size=32 * 1024 * 1024,
            ) as ws:
                hello = json.loads(await ws.recv())["d"]
                identify = {"rpcVersion": hello["rpcVersion"], "eventSubscriptions": 0}
                if "authentication" in hello:
                    auth = hello["authentication"]
                    identify["authentication"] = protocol.compute_auth(
                        password, auth["salt"], auth["challenge"]
                    )
                await ws.send(json.dumps({"op": 1, "d": identify}))
                identified = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if identified.get("op") != 2:
                    raise RuntimeError(f"obs-websocket identify failed: {identified}")
                inbox = protocol.Inbox()

                async def raw_call(kind: str, data: dict | None = None, timeout: float = 15) -> dict:
                    return await protocol.request(inbox, ws, kind, uuid.uuid4().hex, data, timeout=timeout)

                async def call(kind: str, data: dict | None = None, timeout: float = 15) -> dict:
                    response = await raw_call(kind, data, timeout)
                    status = response.get("requestStatus", {})
                    if not status.get("result"):
                        raise RuntimeError(f"{kind} failed: {status}")
                    return response.get("responseData", {})

                async def wait_atlas(frame: dict, expected_alpha: int, label: str):
                    path = EVIDENCE / f"{stamp}-{token[:8]}-cef-atlas-{label}.png"
                    deadline = time.monotonic() + 45
                    last_error = None
                    while True:
                        png, shot = await screenshot(call, browser, names["browser"], CANVAS_SIZE, path)
                        if shot["channels"] != 4:
                            raise AssertionError({"cefAtlasChannels": shot["channels"],
                                                  "required": "RGBA for coverage alpha"})
                        try:
                            check = validate_atlas_samples(browser, frame, png, expected_alpha)
                        except AssertionError as error:
                            last_error = str(error)
                            if time.monotonic() >= deadline:
                                raise TimeoutError(f"CEF atlas did not reach expected alpha {expected_alpha}: {last_error}")
                            await asyncio.sleep(0.5)
                            continue
                        check["path"] = shot["artifact"]["path"]
                        return png, shot, check

                async def wait_native_scene(label: str, expected_alpha: int, compare_to: list[int] | None = None):
                    path = EVIDENCE / f"{stamp}-{token[:8]}-scene-mask-{label}.png"
                    deadline = time.monotonic() + 45
                    last_error = None
                    while True:
                        png, shot = await screenshot(call, browser, scene_name, SCENE_SIZE, path)
                        samples = {
                            key: pixel(SCENE_SIZE[0], shot["channels"], shot["pixels"], point)
                            for key, point in SAMPLE_POINTS.items()
                        }
                        partial = samples["partial"]
                        try:
                            for key in ("overlap", "unclippedChild"):
                                assert_close(samples[key], partial, 4, f"coverage {key} matches partial")
                            for key in ("clippedOverflow", "stencilFrameOverflow"):
                                assert_close(samples[key], baseline_point, 4, f"coverage {key} remains backdrop")
                            distance = sum(abs(a - b) for a, b in zip(partial[:3], baseline_point[:3]))
                            if distance < 80:
                                raise AssertionError({"partialCoverageWasNotVisible": partial, "backdrop": baseline_point})
                            if compare_to is not None and sum(abs(a - b) for a, b in zip(partial[:3], compare_to[:3])) < 90:
                                raise AssertionError({"sameSourceMaskUpdateNotVisible": {"previous": compare_to, "current": partial}})
                        except AssertionError as error:
                            last_error = str(error)
                            if time.monotonic() >= deadline:
                                raise TimeoutError(f"native scene did not reach expected mask {expected_alpha}: {last_error}")
                            await asyncio.sleep(0.5)
                            continue
                        return png, shot, samples

                video_after = await call("GetVideoSettings")
                if ((int(video_after["baseWidth"]), int(video_after["baseHeight"])) != SCENE_SIZE
                    or (int(video_after["outputWidth"]), int(video_after["outputHeight"])) != SCENE_SIZE):
                    raise RuntimeError({"videoViewportNotApplied": video_after})
                report["videoSettings"] = {
                    "bootEnvironment": {"PULSAR_RESOLUTION": f"{SCENE_SIZE[0]}x{SCENE_SIZE[1]}",
                                        "PULSAR_FPS": "30"},
                    "after": video_after,
                }

                await call("CreateScene", {"sceneName": scene_name})
                backdrop = await call("CreateInput", {
                    "sceneName": scene_name, "inputName": names["backdrop"],
                    "inputKind": "color_source_v3",
                    "inputSettings": {"color": 0xFF203040,
                                      "width": SCENE_SIZE[0], "height": SCENE_SIZE[1]},
                    "sceneItemEnabled": True,
                })
                binding = await call("CreateInput", {
                    "sceneName": scene_name, "inputName": names["binding"],
                    "inputKind": "color_source_v3",
                    "inputSettings": {"color": 0xFF40C0E0, "width": 32, "height": 32},
                    "sceneItemEnabled": False,
                })
                contribution = await call("CreateInput", {
                    "sceneName": scene_name, "inputName": names["contribution"],
                    "inputKind": "color_source_v3",
                    "inputSettings": {"color": 0xFFE02090, "width": 32, "height": 32},
                    "sceneItemEnabled": False,
                })
                browser_item = await call("CreateInput", {
                    "sceneName": scene_name, "inputName": names["browser"],
                    "inputKind": "browser_source",
                    "inputSettings": {
                        "url": page.url, "width": CANVAS_SIZE[0], "height": CANVAS_SIZE[1],
                        "fps": 30, "shutdown": False,
                        "restart_when_active": False, "reroute_audio": False,
                    },
                    "sceneItemEnabled": True,
                })
                scene_item_ids = {
                    "backdrop": int(backdrop["sceneItemId"]),
                    "captureBinding": int(binding["sceneItemId"]),
                    "captureContribution": int(contribution["sceneItemId"]),
                    "cefAtlas": int(browser_item["sceneItemId"]),
                }
                report["scene"] = {
                    "sceneName": scene_name,
                    "sceneSize": list(SCENE_SIZE),
                    "browserSourceUrl": page.url,
                    "browserSourceSettings": {"width": CANVAS_SIZE[0], "height": CANVAS_SIZE[1], "fps": 30},
                    "sceneItemIds": scene_item_ids,
                    "captureInputKind": "synthetic color_source_v3; no camera/device opened",
                }
                activation = await call("SetCurrentProgramScene", {"sceneName": scene_name})
                active_program = await call("GetCurrentProgramScene")
                active_scene = active_program.get("currentProgramSceneName") or active_program.get("sceneName")
                if active_scene != scene_name:
                    raise RuntimeError({"privateBrowserSceneNotActive": {
                        "setResponse": activation, "currentProgramScene": active_program,
                    }})
                report["scene"]["activation"] = {
                    "request": "SetCurrentProgramScene",
                    "response": activation,
                    "readback": active_program,
                    "browserLifecycle": "active private fixture scene to drive actual CEF browser_source",
                }

                initial = await asyncio.to_thread(page.wait_for_frame, "initial", 60)
                report["presenter"] = {
                    "initialReceiptSchema": initial["message"]["composition"]["schema"],
                    "initialReceiptIdentity": {
                        "sceneId": initial["document"]["scene_id"],
                        "sceneVersion": initial["document"]["scene_version"],
                        "pixelRevision": initial["message"]["composition"].get("pixelRevision"),
                    },
                    "canvasSize": initial["canvasSize"],
                    "legacyPresenterNegative": "pending until initial and updated native pixels are captured",
                }
                backdrop_png, backdrop_shot = await screenshot(
                    call, browser, names["backdrop"], SCENE_SIZE,
                    EVIDENCE / f"{stamp}-{token[:8]}-backdrop-source.png",
                )
                report["evidence"]["backdropSource"] = backdrop_shot["artifact"]
                baseline_point = pixel(SCENE_SIZE[0], backdrop_shot["channels"],
                                       backdrop_shot["pixels"], SAMPLE_POINTS["partial"])

                initial_atlas_png, initial_atlas, initial_atlas_check = await wait_atlas(initial, 128, "initial")
                report["evidence"]["cefAtlasInitial"] = initial_atlas["artifact"]
                report["cefAtlasInitialSamples"] = initial_atlas_check

                async def emit(frame: dict, generation: int, label: str) -> dict:
                    frame_input = scratch / f"{label}-emitter-input.json"
                    frame_output = scratch / f"{label}-emitter-output.json"
                    emitter_input = {
                        **frame,
                        "solarRoot": str(SOLAR), "prismRoot": str(PRISM),
                        "solarCacheDir": str(scratch / "vite/solar"),
                        "prismCacheDir": str(scratch / "vite/prism"),
                        "producerId": "pulsar-cef-coverage-chain",
                        "captureBindingSceneItemId": scene_item_ids["captureBinding"],
                        "captureContributionSceneItemId": scene_item_ids["captureContribution"],
                        "cefSceneItemId": scene_item_ids["cefAtlas"],
                        "sceneName": scene_name, "runtimeInstanceId": runtime_id,
                        "generation": generation,
                    }
                    frame_input.write_text(json.dumps(emitter_input), encoding="utf-8")
                    result = subprocess.run(
                        ["node", str(ROOT / "scripts/cef-coverage-chain/emit-wire.mjs"),
                         str(frame_input), str(frame_output)],
                        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=90,
                    )
                    if result.returncode != 0:
                        raise RuntimeError({"emitterExit": result.returncode,
                                            "stdout": result.stdout[-4000:],
                                            "stderr": result.stderr[-4000:]})
                    emitted = json.loads(frame_output.read_text(encoding="utf-8"))
                    if emitted["wire"]["protocol"] != "pulsar.group-composition/2":
                        raise RuntimeError("Prism did not emit native /2")
                    evidence_json = EVIDENCE / f"{stamp}-{token[:8]}-{label}-chain.json"
                    shutil.copyfile(frame_output, evidence_json)
                    return {"record": emitted,
                            "artifact": file_record(evidence_json),
                            "stdout": result.stdout[-1000:]}

                initial_chain = await emit(initial, 1, "initial")
                initial_wire = initial_chain["record"]["wire"]
                if initial_chain["record"]["solarProjection"]["atlasSize"] != {
                    "width": CANVAS_SIZE[0], "height": CANVAS_SIZE[1]
                }:
                    raise RuntimeError({"solarAtlasSizeMismatch": initial_chain["record"]["solarProjection"]["atlasSize"]})
                report["evidence"]["initialChain"] = initial_chain["artifact"]
                report["initialProjection"] = {
                    "solarPlanSchema": initial_chain["record"]["solarPlan"]["schema"],
                    "nativeProtocol": initial_wire["protocol"],
                    "nativePlanHash": initial_wire["planHash"],
                    "atlasSize": initial_chain["record"]["provenance"]["atlasSize"],
                    "coverageSurfaces": initial_chain["record"]["solarProjection"]["coverageSurfaces"],
                    "wireGroups": len(initial_wire["groups"]),
                }
                initial_native = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": initial_wire,
                })
                initial_outcome = initial_native.get("responseData", {})
                if not initial_outcome.get("ok"):
                    raise RuntimeError({"nativeInitialCommitRejected": initial_native})
                report["nativeInitialCommit"] = initial_native
                initial_scene_png, initial_scene, initial_pixels = await wait_native_scene("050", 128)
                report["evidence"]["sceneMask050"] = initial_scene["artifact"]
                half = initial_pixels["partial"]
                report["nativeInitialPixels"] = {
                    "backdrop": baseline_point,
                    "samples": {key: {"scenePoint": list(SAMPLE_POINTS[key]), "rgba": value}
                                for key, value in initial_pixels.items()},
                    "assertions": ["partial mask visible", "overlapping children do not double-apply frame opacity",
                                   "nested clips suppress overflow", "unclipped child remains inside stencil-frame clip"],
                }

                negative_wire = initial_chain["record"]["maskDimensionNegativeWire"]
                negative_native = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": negative_wire,
                })
                negative_outcome = negative_native.get("responseData", {})
                if negative_outcome.get("ok") or not str(negative_outcome.get("error", "")).startswith("invalid-mask"):
                    raise AssertionError({"maskDimensionNegativeWasNotDiscriminating": negative_native})
                after_negative_png, after_negative = await screenshot(
                    call, browser, scene_name, SCENE_SIZE,
                    EVIDENCE / f"{stamp}-{token[:8]}-scene-after-negative.png",
                )
                if initial_scene_png != after_negative_png:
                    raise AssertionError("invalid /2 mask changed native scene pixels")
                report["negativeControl"] = {
                    "wirePlanHashRecomputed": True,
                    "changedMaskCropLeftBy": 1,
                    "nativeResponse": negative_native,
                    "pixelsExactlyUnchanged": True,
                    "evidence": after_negative["artifact"],
                }

                page.set_opacity(1.0)
                updated = await asyncio.to_thread(page.wait_for_frame, "updated", 60)
                updated_atlas_png, updated_atlas, updated_atlas_check = await wait_atlas(updated, 255, "updated")
                report["evidence"]["cefAtlasUpdated"] = updated_atlas["artifact"]
                report["cefAtlasUpdatedSamples"] = updated_atlas_check
                initial_receipt = initial["message"]["composition"]
                updated_receipt = updated["message"]["composition"]
                if (updated["document"]["scene_id"] != initial["document"]["scene_id"]
                    or updated["document"]["scene_version"] == initial["document"]["scene_version"]
                    or updated_receipt.get("pixelRevision") == initial_receipt.get("pixelRevision")):
                    raise AssertionError("mask update did not revise the same scene through the same presenter")
                updated_chain = await emit(updated, 2, "updated")
                report["evidence"]["updatedChain"] = updated_chain["artifact"]
                updated_wire = updated_chain["record"]["wire"]
                updated_native = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "CommitGroupComposition",
                    "requestData": updated_wire,
                })
                updated_outcome = updated_native.get("responseData", {})
                if not updated_outcome.get("ok"):
                    raise RuntimeError({"nativeUpdatedCommitRejected": updated_native})
                updated_scene_png, updated_scene, updated_pixels = await wait_native_scene("100", 255, half)
                report["evidence"]["sceneMask100"] = updated_scene["artifact"]
                full = updated_pixels["partial"]
                report["nativeUpdatedCommit"] = updated_native
                report["sameSceneMaskUpdate"] = {
                    "sceneId": updated["document"]["scene_id"],
                    "sceneVersionBefore": initial["document"]["scene_version"],
                    "sceneVersionAfter": updated["document"]["scene_version"],
                    "pixelRevisionBefore": initial_receipt.get("pixelRevision"),
                    "pixelRevisionAfter": updated_receipt.get("pixelRevision"),
                    "wireGenerationBefore": initial_wire["generation"],
                    "wireGenerationAfter": updated_wire["generation"],
                    "browserSceneItemIdStable": scene_item_ids["cefAtlas"],
                    "initialNativeSamples": {key: value for key, value in initial_pixels.items()},
                    "updatedNativeSamples": {key: value for key, value in updated_pixels.items()},
                    "assertions": ["same browser source and scene identity", "coverage 0.5→1.0 changes real native pixels",
                                   "overlap remains single-alpha", "clips remain enforced after update"],
                }

                reset = await call("CallVendorRequest", {
                    "vendorName": "pulsar-capture-effects",
                    "requestType": "ResetGroupComposition",
                    "requestData": {"sceneName": scene_name, "generation": 2,
                                    "runtimeInstanceId": runtime_id},
                })
                reset_result = reset.get("responseData", {})
                if not reset_result.get("reset"):
                    raise RuntimeError({"nativeResetFailed": reset})
                report["reset"] = reset
                for name, original in ((names["browser"], True), (names["binding"], False),
                                       (names["contribution"], False)):
                    visible = await call("GetSceneItemEnabled", {
                        "sceneName": scene_name, "sceneItemId": scene_item_ids[
                            {names["browser"]: "cefAtlas", names["binding"]: "captureBinding",
                             names["contribution"]: "captureContribution"}[name]
                        ],
                    })
                    if visible.get("sceneItemEnabled") != original:
                        raise AssertionError({"resetVisibilityMismatch": {"input": name, "response": visible}})
                report["resetVisibilityRestored"] = True

                page.request_legacy_negative()
                legacy_negative = await asyncio.to_thread(
                    page.wait_for_event,
                    lambda event: event.get("baselineCommit") == preflight.PINS["baselinePresenterCommit"],
                    60,
                )
                report["presenter"]["legacyPresenterNegative"] = legacy_negative

        await asyncio.wait_for(exercise(), timeout=480)
        process.shutdown(grace=35)
        report["shutdown"] = shutdown_report(process)
        assert_shutdown(report["shutdown"])
        report["passed"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        if hasattr(error, "__dict__") and error.__dict__:
            report["error"]["details"] = error.__dict__
    finally:
        if process:
            try:
                process.shutdown(grace=35)
            except Exception as error:
                report.setdefault("cleanupErrors", []).append({"hostShutdown": str(error)})
            child = process.proc.proc
            report["processExitCode"] = child.poll() if child is not None else None
            log_text = "\n".join(process._lines)
            import re
            log_text = re.sub(r"password=\S+", "password=[redacted]", log_text)
            log_path = EVIDENCE / f"{stamp}-{token[:8]}-host.log"
            log_path.write_text(log_text + "\n", encoding="utf-8")
            report["evidence"]["hostLog"] = file_record(log_path)
            report["shutdown"] = shutdown_report(process)
        try:
            frame_summaries = {}
            for variant, frame in page.frames.items():
                receipt = frame.get("message", {}).get("composition", {})
                frame_summaries[variant] = {
                    "sceneId": frame.get("document", {}).get("scene_id"),
                    "sceneVersion": frame.get("document", {}).get("scene_version"),
                    "messageType": frame.get("message", {}).get("type"),
                    "schema": receipt.get("schema"),
                    "pixelRevision": receipt.get("pixelRevision"),
                    "canvasSize": frame.get("canvasSize"),
                    "coverageSurfaceCount": len(receipt.get("coverageSurfaces", [])),
                    "owners": [
                        {key: owner.get(key) for key in ("targetNodeId", "maskPresent", "maskSurfaceIndex")}
                        for owner in receipt.get("owners", [])
                    ],
                }
            report["pageServer"] = {
                "url": page.url,
                "threadAliveBeforeClose": bool(page.thread and page.thread.is_alive()),
                "requests": page.requests,
                "events": page.events,
                "submittedFrames": frame_summaries,
            }
        except Exception as error:
            report.setdefault("cleanupErrors", []).append({"pageDiagnostics": str(error)})
        try:
            page.close()
        except Exception as error:
            report.setdefault("cleanupErrors", []).append({"httpServerClose": str(error)})
        for key in list(os.environ):
            if key.startswith("PULSAR_"):
                del os.environ[key]
        os.environ.update(old_env)
        child = process.proc.proc if process is not None else None
        report["runtimeStopped"] = bool(process is None or (child is not None and child.poll() is not None))
        report["httpServerStopped"] = page.thread is None
        report["finishedUtc"] = datetime.now(timezone.utc).isoformat()
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


async def screenshot(call, browser, source_name: str, size: tuple[int, int], path: Path):
    response = await call("GetSourceScreenshot", {
        "sourceName": source_name, "imageFormat": "png",
        "imageWidth": size[0], "imageHeight": size[1],
    }, timeout=15)
    encoded = response.get("imageData", "")
    if "," not in encoded:
        raise RuntimeError(f"GetSourceScreenshot returned no PNG data for {source_name}")
    png = base64.b64decode(encoded.split(",", 1)[1])
    width, height, channels, pixels = browser.decode_png(png)
    if (width, height) != size:
        raise AssertionError({"sourceScreenshotSize": [width, height], "requested": list(size), "source": source_name})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return png, {"artifact": file_record(path), "dimensions": [width, height],
                 "channels": channels, "pixels": pixels}


def shutdown_report(process) -> dict:
    lines = process._lines
    markers = [line for line in lines if "PULSAR_CEF_SHUTDOWN" in line]
    expected = ["pre_obs_shutdown_begin", "cef_shutdown_begin", "cef_shutdown_complete", "pre_obs_shutdown_complete"]
    positions = [next((i for i, line in enumerate(lines) if "event=" + event in line), -1)
                 for event in expected]
    return {
        "exitCode": process.proc.proc.returncode if process.proc.proc else None,
        "gracefulEventRequested": process.graceful_shutdown_requested,
        "forcedTerminationUsed": process.forced_termination_used,
        "orderedMarkerNames": expected,
        "orderedMarkerPositions": positions,
        "markers": markers,
    }


def assert_shutdown(result: dict) -> None:
    positions = result["orderedMarkerPositions"]
    markers = result["markers"]
    if result["exitCode"] != 0 or not result["gracefulEventRequested"] or result["forcedTerminationUsed"]:
        raise AssertionError({"gracefulShutdownFailed": result})
    if not all(position >= 0 for position in positions) or positions != sorted(set(positions)):
        raise AssertionError({"cefShutdownMarkerOrderInvalid": result})
    if not any("event=close_requested browser_count=1" in line for line in markers):
        raise AssertionError({"activeBrowserNotDrained": markers})
    if not any("event=barrier_released phase=Drained browser_count=0" in line for line in markers):
        raise AssertionError({"cefBarrierDidNotDrain": markers})
    if any("event=timeout" in line or "event=cef_shutdown_skipped" in line for line in markers):
        raise AssertionError({"cefShutdownTimedOutOrSkipped": markers})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="start the private host/CEF proof")
    parser.add_argument("--gpu-slot-released", action="store_true", help="explicitly attest parent release of GPU slot")
    parser.add_argument("--output", type=Path, help="new canonical evidence JSON path")
    args = parser.parse_args()
    try:
        report = asyncio.run(run_probe(args))
    except Exception as error:
        print(f"CHAIN_PROBE_FAILED_BEFORE_REPORT: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"passed": report["passed"], "report": report.get("reportPath"),
                      "pid": report.get("ownedPid"), "shutdown": report.get("shutdown")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
