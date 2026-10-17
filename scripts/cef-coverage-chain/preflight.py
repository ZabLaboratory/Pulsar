#!/usr/bin/env python3
"""Offline-only provenance check for the private CEF coverage-chain harness."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence/local-20261017-cef-coverage-chain/eleven"
SOLAR = Path(
    r"D:\Documents\Zab\Solar\.worktrees\eleven-local-20261015-project-integration"
)
PRISM = Path(
    r"D:\Documents\Zab\Prism\.worktrees\eleven-local-20261015-project-integration"
)
VISION = Path(
    r"D:\Documents\Lumencast\lumencast-vision\.worktrees\eleven-local-20261017-native-mask-coverage"
)
PULSAR_NATIVE = Path(
    r"D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition"
)
PULSAR_CEF = Path(
    r"D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-cef-runtime-qualification"
)
PULSAR_COVERAGE = Path(
    r"D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-native-reset-stability"
)

PINS = {
    "solarCommit": "e8ca9a3a6944fac68430fa9ddb79b93366a2f99a",
    "prismCommit": "8933556f85272b077553444a7ffd891438432175",
    "prismProductBaselineCommit": "49080662c422eded328bcf0e7fdf8dd16febb0d2",
    "visionCommit": "044ddb62df050eee9869150a69a1b88fd26f2962",
    "baselinePresenterCommit": "85c49664a029744369b063ddffaa92415a59c8a8",
    "nativeSourceCommit": "6328ff8b3c29f10f29b7e1fad75f170efc41e198",
    "cefSourceCommit": "47cb08c56e010c5da45923a5eee147f294216756",
}

ASSETS = {
    "ui/mainpresenter.mjs": (43272, "1046dafdf9d1544dd15e17d24b57dcf8e32fdf74a140285b5f200ca544ccf508"),
    "ui/fonts.mjs": (1977, "d49ed45181a8aa714f675aaa24f5439e9547b274d6eba6cd2bb995c981765c5f"),
    "pkg/lumencast_vision_web.js": (157228, "2661fc24146f703adcfa1b94769d9c789b707abf0027792b6f4287fb40bc04e6"),
    "pkg/lumencast_vision_web_bg.wasm": (10618773, "39c64876a92c0d8d5a104fa8366dc437520140be92e8376db59ed127420ab4a9"),
}

PULSAR_ARTIFACTS = {
    "nativeHost": (".cache/native-group-composition/upstream-build-25ed780b7f392a1690dfca929cd57679959cf3ad/rundir/RelWithDebInfo/bin/64bit/pulsar.exe", "69ab22a5829bee254d53fa9edc463cfbdb33886a23781ae67fdc40fb1b36e973"),
    "nativeObs": (".cache/native-group-composition/upstream-build-25ed780b7f392a1690dfca929cd57679959cf3ad/rundir/RelWithDebInfo/bin/64bit/obs.dll", "891f6c58b1a9e865836fb94f03f8d90b238198b55c70d2656214cde4790478b1"),
    "nativeWebsocket": (".cache/native-group-composition/upstream-build-25ed780b7f392a1690dfca929cd57679959cf3ad/rundir/RelWithDebInfo/obs-plugins/64bit/obs-websocket.dll", "d3ecaf55d8adc4a9e593589d0ec232ac4974db00980a41361a3fb8df989ef26b"),
    "capturePlugin": (".cache/native-reset-stability/plugin-build/RelWithDebInfo/pulsar-capture-effects.dll", "1deaa45c64f30d877f93aa9133c6b562a306cacb72931a376f293e8c52403375"),
    "cefBrowser": (".cache/cef-runtime/upstream-build/rundir/RelWithDebInfo/obs-plugins/64bit/pulsar-browser.dll", "0602fdc46559109ad24a57fc769580480fe8b2794930cc591bf5a450129df8e6"),
    "cefHelper": (".cache/cef-runtime/upstream-build/rundir/RelWithDebInfo/obs-plugins/64bit/pulsar-browser-page.exe", "9148fb2f5e1cc8867b19ab1cfd033eaea4e647e05ff0ac313f883cf281ab65e6"),
    "cefLib": (".cache/cef-runtime/upstream-build/rundir/RelWithDebInfo/obs-plugins/64bit/libcef.dll", "551bea07c46c9a330eff7861105321f3c7d47e3c0412ced4b0ff35aa130f2536"),
}

CEF_REPORTED_WEBSOCKET = "3130c2cf8485bfbf80b67b4ee09126f21a38a90229457aabdb03126526374f3a"

BASE_SCENE_SIZE = (320, 180)
BASE_SAMPLE_POINTS = {
    "partial": (16, 40),
    "overlap": (36, 40),
    "clippedOverflow": (62, 40),
    "unclippedChild": (110, 40),
    "stencilFrameOverflow": (124, 40),
}
MAX_GET_SOURCE_SCREENSHOT_DIMENSION = 4096
VISION_PACKING_TEST_MAX_DIMENSION = 8192
PNG_READBACK_CHANNELS = 4
MIB = 1024 * 1024


def profile_plan(name: str) -> dict:
    """Describe the authored scene and exact packed atlas for a private run."""
    if name == "320x180@30":
        scale, fps, websocket_max = 1, 30, 32 * MIB
    elif name == "1920x1080@60":
        scale, fps, websocket_max = 6, 60, 36 * MIB
    else:
        raise ValueError(f"unsupported CEF coverage-chain profile: {name}")

    width, height = (BASE_SCENE_SIZE[0] * scale, BASE_SCENE_SIZE[1] * scale)
    atlas_width, atlas_height = width + 2, 3 * (height + 2)
    atlas_bands = [
        [1, band_index * (height + 2) + 1, width, height]
        for band_index in range(3)
    ]
    atlas_rect = atlas_bands[2]
    if max(atlas_width, atlas_height) > MAX_GET_SOURCE_SCREENSHOT_DIMENSION:
        raise ValueError("planned atlas exceeds GetSourceScreenshot's documented 4096-pixel bound")
    if max(atlas_width, atlas_height) > VISION_PACKING_TEST_MAX_DIMENSION:
        raise ValueError("planned atlas exceeds Vision's documented packer-test bound")

    atlas_raw_bytes = atlas_width * atlas_height * PNG_READBACK_CHANNELS
    scene_raw_bytes = width * height * PNG_READBACK_CHANNELS
    return {
        "name": name,
        "geometryScale": scale,
        "fps": fps,
        "baseSceneGeometry": list(BASE_SCENE_SIZE),
        "sceneSize": [width, height],
        "authoredLayoutGeometry": {
            "rootAndOwnerSceneRect": [0, 0, width, height],
            "stencilFrameRect": [0, 0, 120 * scale, 124 * scale],
            "clippedOpacityFrameRect": [8 * scale, 16 * scale, 52 * scale, 92 * scale],
            "clippedChildrenRelativeRects": [
                [0, 0, 36 * scale, 92 * scale],
                [24 * scale, 0, 44 * scale, 92 * scale],
            ],
            "unclippedOpacityFrameRect": [80 * scale, 16 * scale, 20 * scale, 92 * scale],
            "unclippedChildRelativeRect": [0, 0, 64 * scale, 92 * scale],
            "captureRect": [250 * scale, 20 * scale, 32 * scale, 32 * scale],
        },
        "browserSourceSize": [atlas_width, atlas_height],
        "atlasSize": [atlas_width, atlas_height],
        "atlasPacking": {
            "surfaceCount": 3,
            "gutterPixels": 1,
            "bandRects": atlas_bands,
            "expectedCoverageSceneRect": [0, 0, width, height],
            "expectedCoverageAtlasRect": atlas_rect,
            "sourceTestMaxDimension": VISION_PACKING_TEST_MAX_DIMENSION,
            "sourceTest": "crates/vision-gpu/src/renderer/contribution/packing.rs::canonical_global_texel_shelves_fit_the_1080p_three_band_fixture",
            "getSourceScreenshotDocumentedMaxDimension": MAX_GET_SOURCE_SCREENSHOT_DIMENSION,
            "getSourceScreenshotContract": "plugins/pulsar-websocket/src/requesthandler/RequestHandler_Sources.cpp:218-220",
        },
        "samplePoints": {
            label: [point[0] * scale, point[1] * scale]
            for label, point in BASE_SAMPLE_POINTS.items()
        },
        "samplePointRationale": {
            "partial": "inside only the first clipped child",
            "overlap": "inside both clipped children; group opacity must apply once",
            "clippedOverflow": "x=62 is beyond the clipped frame's base x=60 edge",
            "unclippedChild": "x=110 is past the unclipped frame's base x=100 edge but inside the stencil's x=120 edge",
            "stencilFrameOverflow": "x=124 is outside the stencil frame's base x=120 edge",
        },
        "expectedCoverageAlpha": {
            "initialHalfOpacity": 128,
            "patchedFullOpacity": 255,
            "clippedOverflow": 0,
            "stencilFrameOverflow": 0,
        },
        "readbackMemoryEstimate": {
            "atlasRgbaBytes": atlas_raw_bytes,
            "nativeSceneRgbaBytes": scene_raw_bytes,
            "oneAtlasPlusOneNativeSceneRgbaBytes": atlas_raw_bytes + scene_raw_bytes,
            "websocketMaxMessageBytes": websocket_max,
            "estimateScope": "decoded pixel buffers only; excludes PNG/base64 copies, GPU/CEF surfaces and allocator overhead",
        },
        "qualificationLimits": [
            "The 8192 value is the max-dimension argument in Vision's packer unit test, not a measurement of the active GPU.",
            "The actual WebGL MAX_TEXTURE_SIZE and CEF screenshot dimensions must be recorded by the private run.",
            "A single 60-fps-configured capture is not evidence of sustained 60-fps presentation or frame pacing.",
        ],
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, encoding="utf-8"
    ).rstrip()


def file_record(path: Path, expected_size: int | None = None, expected_hash: str | None = None):
    if not path.is_file():
        raise FileNotFoundError(path)
    record = {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)}
    if expected_size is not None and record["bytes"] != expected_size:
        raise ValueError(f"size mismatch for {path}: {record['bytes']} != {expected_size}")
    if expected_hash is not None and record["sha256"] != expected_hash:
        raise ValueError(f"sha256 mismatch for {path}: {record['sha256']} != {expected_hash}")
    return record


def run(profile_name: str = "320x180@30") -> dict:
    selected_profile = profile_plan(profile_name)
    pulsar_root = Path(git(ROOT, "rev-parse", "--show-toplevel"))
    if pulsar_root != ROOT:
        raise ValueError(f"expected assigned Pulsar worktree {ROOT}, got {pulsar_root}")
    git_state = {
        "worktree": str(ROOT),
        "branch": git(ROOT, "branch", "--show-current"),
        "head": git(ROOT, "rev-parse", "HEAD"),
        "status": git(ROOT, "status", "--short"),
        "commonDir": git(ROOT, "rev-parse", "--git-common-dir"),
    }
    if git_state["branch"] != "eleven/local-20261017-cef-coverage-chain":
        raise ValueError(f"wrong branch: {git_state['branch']}")
    unexpected = []
    for line in git(ROOT, "status", "--short", "--untracked-files=all").splitlines():
        relative = line[3:].replace("\\", "/")
        if not (
            relative.startswith("scripts/cef-coverage-chain/")
            or relative.startswith("evidence/local-20261017-cef-coverage-chain/eleven/")
        ):
            unexpected.append(line)
    if unexpected:
        raise ValueError(f"worktree has changes outside the owned harness/evidence: {unexpected}")

    solar_head = git(SOLAR, "rev-parse", "HEAD")
    prism_head = git(PRISM, "rev-parse", "HEAD")
    if solar_head != PINS["solarCommit"]:
        raise ValueError(f"Solar pin moved: {solar_head}")
    if prism_head != PINS["prismCommit"]:
        raise ValueError(f"Prism pin moved: {prism_head}")

    prism_consumer_path = "src/main/broadcast/engine/capture/native-composition-consumer.ts"
    prism_consumer_at_head = git(PRISM, "rev-parse", f"HEAD:{prism_consumer_path}")
    prism_consumer_at_product_baseline = git(
        PRISM,
        "rev-parse",
        f"{PINS['prismProductBaselineCommit']}:{prism_consumer_path}",
    )
    if prism_consumer_at_head != prism_consumer_at_product_baseline:
        raise ValueError("Prism native-composition consumer changed after the adopted product pin")
    for root, label in ((SOLAR, "Solar"), (PRISM, "Prism")):
        vite_entry = root / "node_modules/vite/dist/node/index.js"
        if not vite_entry.is_file():
            raise FileNotFoundError(f"{label} Vite dependency is unavailable: {vite_entry}")

    asset_files: dict[str, dict] = {}
    for relative, (size, expected) in ASSETS.items():
        source = file_record(SOLAR / "public/vision" / relative, size, expected)
        dist = file_record(SOLAR / "dist/host/vision" / relative, size, expected)
        if source["sha256"] != dist["sha256"]:
            raise ValueError(f"Solar public/dist drift: {relative}")
        asset_files[relative] = {"public": source, "dist": dist, "byteIdentical": True}

    vision_fixture = file_record(
        VISION / "crates/vision-web/tests/contribution-webgl2-proof/contribution-scene.lsml"
    )
    if git(VISION, "rev-parse", "HEAD") != PINS["visionCommit"]:
        raise ValueError("Vision fixture source worktree moved from its pinned presenter proof commit")

    runtime_files = {}
    for key, (relative, expected_hash) in PULSAR_ARTIFACTS.items():
        owner = PULSAR_NATIVE if key.startswith("native") else PULSAR_COVERAGE if key == "capturePlugin" else PULSAR_CEF
        runtime_files[key] = file_record(owner / relative, expected_hash=expected_hash)

    native_websocket = Path(runtime_files["nativeWebsocket"]["path"])
    cef_runtime_websocket = (
        PULSAR_CEF
        / ".cache/cef-runtime/upstream-build/rundir/RelWithDebInfo/obs-plugins/64bit/obs-websocket.dll"
    )
    if cef_runtime_websocket.exists():
        raise ValueError("CEF overlay unexpectedly contains obs-websocket.dll; do not overwrite native pin")

    websocket_source = "plugins/pulsar-websocket/src/obs-websocket.cpp"
    websocket_api = "plugins/pulsar-websocket/lib/obs-websocket-api.h"
    source_blobs = {}
    for label, commit in (
        ("assigned", git(ROOT, "rev-parse", "HEAD")),
        ("native", PINS["nativeSourceCommit"]),
        ("cef", PINS["cefSourceCommit"]),
    ):
        source_blobs[label] = {
            "commit": commit,
            "websocketSourceBlob": git(ROOT, "rev-parse", f"{commit}:{websocket_source}"),
            "websocketApiBlob": git(ROOT, "rev-parse", f"{commit}:{websocket_api}"),
        }
    if len({entry["websocketSourceBlob"] for entry in source_blobs.values()}) != 1:
        raise ValueError("obs-websocket.cpp differs between native/CEF/assigned sources")
    if len({entry["websocketApiBlob"] for entry in source_blobs.values()}) != 1:
        raise ValueError("obs-websocket API header differs between native/CEF/assigned sources")

    native_checkpoint = json.loads(
        (PULSAR_CEF / "evidence/local-20261017-cef-runtime-qualification/eleven/20261017T121331Z-agent-checkpoint.json").read_text(encoding="utf-8")
    )
    cef_checkpoint_websocket = native_checkpoint["build"]["artifacts"]["obs-plugins\\64bit\\obs-websocket.dll"]
    if cef_checkpoint_websocket != CEF_REPORTED_WEBSOCKET:
        raise ValueError("CEF websocket report pin differs from the user-provided pin")

    websocket_header = (ROOT / "plugins/pulsar-websocket/lib/obs-websocket-api.h").read_text(encoding="utf-8")
    websocket_source_text = (ROOT / "plugins/pulsar-websocket/src/obs-websocket.cpp").read_text(encoding="utf-8")
    capture_source_text = (ROOT / "plugins/pulsar-capture-effects/src/capture-effects.cpp").read_text(encoding="utf-8")
    if '#define OBS_WEBSOCKET_API_VERSION 3' not in websocket_header:
        raise ValueError("obs-websocket vendor API version changed")
    if 'proc_handler_call(_ph, "vendor_register"' not in websocket_header:
        raise ValueError("vendor API no longer uses the expected proc-handler bridge")
    if 'void obs_module_post_load(void)' not in capture_source_text or 'obs_websocket_register_vendor("pulsar-capture-effects")' not in capture_source_text:
        raise ValueError("capture-effects vendor registration moved; re-audit ABI path")
    if "obs-websocket.dll" in capture_source_text or "LoadLibrary" in capture_source_text:
        raise ValueError("capture-effects now links/loads the websocket binary directly")

    return {
        "schema": "pulsar.vision-solar-cef-coverage-chain-preflight/1",
        "checkedUtc": datetime.now(timezone.utc).isoformat(),
        "runtimeStarted": False,
        "buildStarted": False,
        "gpuUsed": False,
        "runtimeProfile": selected_profile,
        "worktree": git_state,
        "sourcePins": {
            **PINS,
            "nativeSourceCommitScope": "clean repository reference used for the websocket ABI comparison; not the source checkout that built obs.dll",
            "solarHead": solar_head,
            "prismHead": prism_head,
            "prismProductBaselineCommit": PINS["prismProductBaselineCommit"],
            "prismConsumer": {
                "path": prism_consumer_path,
                "headBlob": prism_consumer_at_head,
                "productBaselineBlob": prism_consumer_at_product_baseline,
                "byteIdentical": True,
            },
            "visionFixtureHead": git(VISION, "rev-parse", "HEAD"),
            "sourceBlobs": source_blobs,
        },
        "solarVisionAssets": asset_files,
        "visionFixture": vision_fixture,
        "nativeAndCefRuntime": runtime_files,
        "websocketProvenance": {
            "nativeDll": runtime_files["nativeWebsocket"],
            "cefReportedDllSha256": CEF_REPORTED_WEBSOCKET,
            "cefReportedDllRetainedAtOverlayPath": False,
            "sameObsDllSha256": runtime_files["nativeObs"]["sha256"] == "891f6c58b1a9e865836fb94f03f8d90b238198b55c70d2656214cde4790478b1",
            "apiVersion": 3,
            "vendorRegistration": "obs_module_post_load → inline proc_handler API calls",
            "directImportFromCaptureEffects": False,
            "staticEvidence": {
                "headerPath": str(ROOT / "plugins/pulsar-websocket/lib/obs-websocket-api.h"),
                "headerSha256": sha256(ROOT / "plugins/pulsar-websocket/lib/obs-websocket-api.h"),
                "sourcePath": str(ROOT / "plugins/pulsar-websocket/src/obs-websocket.cpp"),
                "sourceSha256": sha256(ROOT / "plugins/pulsar-websocket/src/obs-websocket.cpp"),
                "captureEffectsPath": str(ROOT / "plugins/pulsar-capture-effects/src/capture-effects.cpp"),
                "captureEffectsSha256": sha256(ROOT / "plugins/pulsar-capture-effects/src/capture-effects.cpp"),
            },
            "selectionForPrivateStage": "preserve native d3ecaf55 DLL and overlay CEF browser files only; pixel proofs exist for both profiles, while this offline preflight makes no runtime or frame-pacing claim",
        },
        "harness": {
            "page": str(ROOT / "scripts/cef-coverage-chain/index.html"),
            "pageModule": str(ROOT / "scripts/cef-coverage-chain/page.mjs"),
            "solarPrismEmitter": str(ROOT / "scripts/cef-coverage-chain/emit-wire.mjs"),
            "privateRunner": str(ROOT / "scripts/cef-coverage-chain/probe.py"),
            "offlinePreflight": str(Path(__file__).resolve()),
            "executionGate": "the private runner requires both --execute and --gpu-slot-released",
            "realAtlasRequirement": "use GetSourceScreenshot from this exact CEF browser_source; no generated PNG/image atlas may stand in for it",
            "selectedProfile": selected_profile["name"],
        },
        "limitations": [
            "No CEF/browser/GPU/native process was started by this preflight.",
            "No build or test was run.",
            "The LSML capture node uses the proof fixture's synthetic source binding; no physical camera is involved.",
            "The exact d3ecaf55 native WebSocket binary is selected for this private stage because it is retained and already exercised with the matching host/obs.dll; the CEF report's 3130c2cf binary is absent from the retained overlay and is not substituted.",
        ],
    }


def main() -> int:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=EVIDENCE / f"{timestamp}-chain-preflight.json",
        help="canonical evidence JSON file (must be new and under this unit's evidence directory)",
    )
    parser.add_argument(
        "--profile",
        choices=("320x180@30", "1920x1080@60"),
        default="320x180@30",
        help="offline sizing/FPS plan to record; this command never starts runtime or GPU",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output.parent != EVIDENCE.resolve() or output.exists():
        raise ValueError(f"output must be a new direct child of {EVIDENCE}")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    result = run(args.profile)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "report": str(output), "runtimeStarted": False, "gpuUsed": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # offline preflight should name the first broken pin
        print(f"PREFLIGHT_FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
