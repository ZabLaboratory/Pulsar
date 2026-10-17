#!/usr/bin/env python3
"""Independently seal one full-package chain report against its prior 1080p pixels."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence/local-20261017-cef-coverage-chain/eleven"
BASELINE = EVIDENCE / "20261017T171046Z-cef-coverage-chain.json"
PIXEL_ARTIFACTS = (
    "backdropSource", "cefAtlasInitial", "sceneMask050",
    "cefAtlasUpdated", "sceneMask100",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_record(record: dict) -> dict:
    path = Path(record["path"])
    actual = {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)}
    if actual != record:
        raise AssertionError({"artifactRecordMismatch": {"reported": record, "actual": actual}})
    return actual


def verify(report_path: Path, baseline_path: Path = BASELINE) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if not report["passed"] or report.get("runtimeCohort") != "full-package":
        raise AssertionError("full-package run did not pass")
    if report["runtimeProfile"]["name"] != "1920x1080@60":
        raise AssertionError("wrong authored profile")
    if report.get("error") or report.get("cleanupErrors"):
        raise AssertionError("run or cleanup reported an error")
    if not report["runtimeStopped"] or not report["httpServerStopped"] or report["processExitCode"] != 0:
        raise AssertionError("private host or HTTP server did not stop cleanly")
    shutdown = report["shutdown"]
    if (shutdown["exitCode"] != 0 or not shutdown["gracefulEventRequested"]
            or shutdown["forcedTerminationUsed"]
            or shutdown["orderedMarkerPositions"] != sorted(set(shutdown["orderedMarkerPositions"]))
            or any(position < 0 for position in shutdown["orderedMarkerPositions"])):
        raise AssertionError("CEF shutdown marker/exit contract failed")
    markers = shutdown["markers"]
    if not any("event=barrier_released phase=Drained browser_count=0" in item for item in markers):
        raise AssertionError("CEF browser was not drained")

    preflight = report["preflight"]
    source_pins = preflight["sourcePins"]
    consumer = source_pins["prismConsumer"]
    if len({consumer[key] for key in ("headBlob", "productBaselineBlob", "priorAdmissionBlob")}) != 1:
        raise AssertionError("Prism consumer provenance differs between pins")
    if preflight["fullPackage"]["inventory"]["fileCount"] != 1256:
        raise AssertionError("full package source inventory incomplete")
    if preflight["fullPackage"]["inventory"]["bytes"] != 397714390:
        raise AssertionError("full package source byte count changed")
    staged = report["stagedRuntime"]
    if staged["overlayApplied"] or staged["inventory"]["fileCount"] != 1256:
        raise AssertionError("private stage used an overlay or omitted package files")
    if staged["inventory"]["bytes"] != 397714390:
        raise AssertionError("private stage byte count changed")
    manifest = check_record(staged["manifest"])
    for key, artifact in staged["inventory"]["critical"].items():
        if artifact["sha256"] != preflight["nativeAndCefRuntime"][key]["sha256"]:
            raise AssertionError({"stagedCriticalArtifactMismatch": key})

    update = report["sameSceneMaskUpdate"]
    if (update["sceneVersionBefore"] != update["sceneVersionAfter"]
            or update["pixelRevisionBefore"] == update["pixelRevisionAfter"]
            or not update["rejectedCandidateDidNotAdvanceFence"]
            or update["wireGenerationAfter"] != update["rejectedMaskGeneration"]):
        raise AssertionError("mask patch identity or generation-fence assertion failed")
    negative = report["negativeControl"]
    if (negative["expectedError"] != "invalid-mask-rect: mask crop and global rect must have exact one-to-one dimensions"
            or not negative["pixelsExactlyUnchanged"]):
        raise AssertionError("invalid mask was not rejected without pixel mutation")
    if not report["resetVisibilityRestored"]:
        raise AssertionError("reset visibility was not restored")
    old_presenter = report["presenter"]["legacyPresenterNegative"]
    if not old_presenter["exactReceiptSupplied"] or old_presenter["emittedFrameSubmitted"]:
        raise AssertionError("old presenter accepted the real /2 receipt")

    verified_artifacts = {key: check_record(value) for key, value in report["evidence"].items()}
    check_record(negative["evidence"])
    if not baseline["passed"]:
        raise AssertionError("historical baseline did not pass")
    matching_pixels = {}
    for key in PIXEL_ARTIFACTS:
        old = check_record(baseline["evidence"][key])
        current = verified_artifacts[key]
        if old["sha256"] != current["sha256"]:
            raise AssertionError({"pixelArtifactChanged": key})
        matching_pixels[key] = current["sha256"]
    if any(float(value) <= 0 for value in report["phaseTimingMs"].values()):
        raise AssertionError("phase timing missing or non-positive")

    return {
        "schema": "pulsar.vision-solar-cef-full-package-validation/1",
        "checkedUtc": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "report": {"path": str(report_path), "sha256": sha256(report_path)},
        "baseline": {"path": str(baseline_path), "sha256": sha256(baseline_path)},
        "packageManifest": manifest,
        "sourceCommits": {
            "pulsarSigned": source_pins["fullSignedSourceCommit"],
            "pulsarIntegrated": source_pins["fullIntegratedCommit"],
            "solar": source_pins["solarCommit"],
            "prism": source_pins["prismFullCommit"],
            "vision": source_pins["visionCommit"],
        },
        "prismConsumerBlob": consumer["headBlob"],
        "packageFilesVerifiedInSourceAndPrivateStage": 1256,
        "packageBytesVerifiedInSourceAndPrivateStage": 397714390,
        "binaryOverlayApplied": False,
        "matchingHistoricalPngHashes": matching_pixels,
        "verifiedArtifactCount": len(verified_artifacts) + 1,
        "phaseTimingMs": report["phaseTimingMs"],
        "shutdown": {
            "hostExitCode": shutdown["exitCode"],
            "forcedTerminationUsed": shutdown["forcedTerminationUsed"],
            "browserDrained": True,
        },
        "scope": "one private fixture receipt, real CEF atlas readback and native render; not an Orion/LSDP transaction or sustained 60-fps measurement",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.parent != EVIDENCE.resolve() or output.exists():
        raise ValueError(f"output must be a new direct child of {EVIDENCE}")
    result = verify(args.report.resolve())
    output.write_bytes((json.dumps(result, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"passed": result["passed"], "output": str(output)}))


if __name__ == "__main__":
    main()
