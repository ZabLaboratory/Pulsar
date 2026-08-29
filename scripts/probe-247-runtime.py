#!/usr/bin/env python3
"""Physical runtime leg for Pulsar issue #247.

The scene-switch contract campaign is intentionally a deterministic reference
machine test.  It cannot prove that a built Pulsar process keeps its physical
lane roots, route surfaces, or encoded frames isolated.  This probe therefore
drives the exact built candidate through the existing dual-lane WebSocket
probe and persists a redacted, structured runtime witness:

* 100 or more real frame-boundary Cuts with stable lane/root/view/video IDs;
* the pre-commit ``PREVIEW_FROZEN`` response and the post-commit Preview
  mutation/30-frame settle assertions from the physical probe;
* the resulting recording's decoded YUV420P SHA-256 hash for every frame;
* the candidate binary hash, recording metadata, and parsed TakeCommitted
  frame/PTS route evidence.

This is a separate runtime leg, not a substitute for the contract leg.  Run
both against the same candidate::

    python -m scripts.contracts.scene_switch_v1.lifecycle_campaign \
      --cycles 128 --attempts 1024 \
      --output docs/evidence/247/scene-switch-lifecycle-race.json
    python scripts/probe-247-runtime.py --exe <pulsar.exe> \
      --encoder x264 --takes 100 \
      --output docs/evidence/247/dual-lane-runtime.json

The process boundary remains the public obs-websocket v5 API.  No native
objects or production code are modified by this script.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_PROBE_PATH = REPO_ROOT / "scripts" / "probe-dual-lane.py"
BASE_REVISION = "8a26b8a992a9b5a783078e83f719df53b2b107ed"
ADR_REVISION = "ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane-20260828"
ISSUE = 247
MIN_TAKES = 100
CANVAS_W = 1920
CANVAS_H = 1080


def _load_base_probe() -> Any:
    spec = importlib.util.spec_from_file_location("probe_dual_lane_for_247", BASE_PROBE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load physical dual-lane probe: {BASE_PROBE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BASE = _load_base_probe()


class RuntimeEvidenceError(RuntimeError):
    """The candidate ran, but the physical evidence was incomplete."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_runs(hashes: list[str]) -> list[dict[str, Any]]:
    """Compress adjacent equal frame hashes into auditable output runs."""

    if not hashes:
        return []
    runs: list[dict[str, Any]] = []
    start = 0
    current = hashes[0]
    for index, value in enumerate(hashes[1:], start=1):
        if value == current:
            continue
        runs.append({"start_index": start, "end_index": index - 1, "sha256": current})
        start = index
        current = value
    runs.append({"start_index": start, "end_index": len(hashes) - 1, "sha256": current})
    return runs


def _status_summary(response: dict[str, Any]) -> dict[str, Any]:
    status = response.get("requestStatus") or {}
    return {
        "result": status.get("result"),
        "code": status.get("code"),
        "comment": status.get("comment"),
    }


def _decode_frame_hashes(recording: Path, pixel_format: str, ffmpeg: str) -> dict[str, Any]:
    """Decode the recording and hash each fixed-size raw frame.

    Hashing decoded frames, rather than the compressed packets, makes the
    witness independent of muxer packet boundaries while still tying it to
    the exact output produced by the candidate.  ``nv12`` is included because
    it is the headless canvas format; it is a conversion of the recorded
    output, not a claim that the public WebSocket API exposes the raw encoder
    input.  The frame list is kept in decoder order; TakeCommitted frame/PTS
    values are retained separately because they use the runtime's monotonic
    clock domain.
    """

    frame_size = CANVAS_W * CANVAS_H * 3 // 2
    process = subprocess.Popen(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(recording),
            "-map",
            "0:v:0",
            "-an",
            "-f",
            "rawvideo",
            "-pix_fmt",
            pixel_format,
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    hashes: list[str] = []
    try:
        while True:
            frame = bytearray()
            while len(frame) < frame_size:
                block = process.stdout.read(frame_size - len(frame))
                if not block:
                    break
                frame.extend(block)
            if not frame:
                break
            if len(frame) != frame_size:
                raise RuntimeEvidenceError(
                    f"decoded recording ended with a partial frame: {len(frame)} of {frame_size} bytes"
                )
            hashes.append(hashlib.sha256(frame).hexdigest())
    finally:
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        return_code = process.wait(timeout=60)
    if return_code != 0:
        raise RuntimeEvidenceError(f"ffmpeg frame decode failed ({return_code}): {stderr[-1000:]}")
    if len(hashes) < 60:
        raise RuntimeEvidenceError(f"recording yielded too few decoded frames: {len(hashes)}")
    return {
        "algorithm": "sha256",
        "pixel_format": pixel_format,
        "width": CANVAS_W,
        "height": CANVAS_H,
        "frame_count": len(hashes),
        "frames": [{"frame_index": index, "sha256": value} for index, value in enumerate(hashes)],
        "runs": _hash_runs(hashes),
    }


def _record_frame_hashes(recording: Path) -> dict[str, Any]:
    """Persist both decoded NV12 and YUV420P frame hashes."""

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeEvidenceError("ffmpeg is required to hash decoded runtime frames")
    nv12 = _decode_frame_hashes(recording, "nv12", ffmpeg)
    yuv420p = _decode_frame_hashes(recording, "yuv420p", ffmpeg)
    if nv12["frame_count"] != yuv420p["frame_count"]:
        raise RuntimeEvidenceError(
            "NV12 and YUV420P decode passes yielded different frame counts: "
            f"{nv12['frame_count']} != {yuv420p['frame_count']}"
        )
    return {"decoded_nv12": nv12, "decoded_yuv420p": yuv420p}


def _recording_file(record_dir: Path) -> Path:
    files = sorted(
        path
        for path in record_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".mov"}
    )
    if len(files) != 1:
        raise RuntimeEvidenceError(
            f"expected exactly one recording in {record_dir}, found {[str(path) for path in files]}"
        )
    return files[0]


def _patch_observation_hooks(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Patch only probe assertions so their real responses enter the artifact."""

    originals: dict[str, Any] = {}

    originals["assert_preview_frozen"] = BASE.assert_preview_frozen

    def capture_preview_frozen(response: dict[str, Any], operation: str) -> None:
        originals["assert_preview_frozen"](response, operation)
        observations.append(
            {
                "kind": "precommit_preview_mutation",
                "operation": operation,
                "status": _status_summary(response),
            }
        )

    BASE.assert_preview_frozen = capture_preview_frozen

    originals["assert_scene_item_presence"] = BASE.assert_scene_item_presence

    async def capture_scene_item_presence(
        inbox: Any,
        ws: Any,
        scene: str,
        input_name: str,
        expected: bool,
        operation: str,
    ) -> None:
        await originals["assert_scene_item_presence"](
            inbox, ws, scene, input_name, expected, operation
        )
        if any(
            marker in operation
            for marker in ("during pending", "after commit", "after 30 frames", "frozen input")
        ):
            observations.append(
                {
                    "kind": "scene_item_visibility",
                    "scene": scene,
                    "input": input_name,
                    "expected": expected,
                    "operation": operation,
                    "assertion": "passed",
                }
            )

    BASE.assert_scene_item_presence = capture_scene_item_presence

    originals["assert_distinct_selected_scenes"] = BASE.assert_distinct_selected_scenes

    async def capture_distinct_selected_scenes(
        inbox: Any,
        ws: Any,
        operation: str,
        expected_program: str,
        expected_preview: str,
    ) -> None:
        await originals["assert_distinct_selected_scenes"](
            inbox, ws, operation, expected_program, expected_preview
        )
        observations.append(
            {
                "kind": "logical_route_selection",
                "operation": operation,
                "program": expected_program,
                "preview": expected_preview,
                "distinct": expected_program != expected_preview,
            }
        )

    BASE.assert_distinct_selected_scenes = capture_distinct_selected_scenes

    originals["request_batch"] = BASE.request_batch

    async def capture_batch(
        inbox: Any,
        ws: Any,
        request_id: str,
        requests: list[dict[str, Any]],
        execution_type: int = 1,
    ) -> dict[str, Any]:
        response = await originals["request_batch"](
            inbox, ws, request_id, requests, execution_type
        )
        if request_id in {"take-1-freeze-batch", "take-1-post-commit-settle"}:
            results = response.get("results")
            result_summaries = [
                _status_summary(result) for result in results
            ] if isinstance(results, list) else []
            observations.append(
                {
                    "kind": "lifecycle_batch",
                    "request_id": request_id,
                    "result_count": len(result_summaries),
                    "statuses": result_summaries,
                }
            )
        return response

    BASE.request_batch = capture_batch
    return originals


def _restore_observation_hooks(originals: dict[str, Any]) -> None:
    for name, value in originals.items():
        setattr(BASE, name, value)


async def _probe_scene_switch_surface(process: Any) -> dict[str, Any]:
    """Ask the exact candidate whether the v1 command surface is exposed.

    The baseline build is controlled through OBS WebSocket v5 requests and
    does not expose ``Prepare``/``Take``/``Abort`` as v1 wire commands.  A
    physical run must record that fact instead of presenting the reference
    machine's 1,000 attempts as if they had crossed this process boundary.
    """

    ready_match = process.wait_for(BASE.READY_RE, timeout=60)
    ws_url = ready_match.group(1)
    requests = {
        "Prepare": {
            "contract": "pulsar.scene-switch.v1",
            "schema_version": 1,
            "message_type": "command",
            "command_type": "Prepare",
            "command_id": "surface-probe-prepare",
            "intent_id": "surface-probe-intent",
            "runtime_instance_id": "probe-247-surface",
            "expected_revisions": {"program": 0, "preview": 0, "role_map": 0},
            "expected_server_seq": 0,
            "target": {"lane_id": "B", "scene_id": "probe-247-surface-sentinel"},
            "timeout_ms": 2000,
        },
        "Take": {
            "contract": "pulsar.scene-switch.v1",
            "schema_version": 1,
            "message_type": "command",
            "command_type": "Take",
            "command_id": "surface-probe-take",
            "intent_id": "surface-probe-intent",
            "runtime_instance_id": "probe-247-surface",
            "expected_revisions": {"program": 0, "preview": 0, "role_map": 0},
            "expected_server_seq": 0,
            "prepared_command_id": "surface-probe-prepare",
            "timeout_ms": 1000,
        },
        "Abort": {
            "contract": "pulsar.scene-switch.v1",
            "schema_version": 1,
            "message_type": "command",
            "command_type": "Abort",
            "command_id": "surface-probe-abort",
            "intent_id": "surface-probe-intent",
            "runtime_instance_id": "probe-247-surface",
            "expected_revisions": {"program": 0, "preview": 0, "role_map": 0},
            "expected_server_seq": 0,
            "take_command_id": "surface-probe-take",
            "reason": "operator",
        },
    }
    observed: list[dict[str, Any]] = []
    async with BASE.websockets.connect(
        ws_url, subprotocols=["obswebsocket.json"], open_timeout=15
    ) as ws:
        await BASE.identify(ws, process.password)
        inbox = BASE.Inbox()
        for command_type, payload in requests.items():
            response = await BASE.request(
                inbox,
                ws,
                command_type,
                f"scene-switch-surface-{command_type.lower()}",
                payload,
            )
            status = _status_summary(response)
            observed.append(
                {
                    "request_type": command_type,
                    "status": status,
                }
            )
            if status.get("result"):
                raise RuntimeEvidenceError(
                    f"candidate unexpectedly accepted scene-switch.v1 {command_type}; stop before fabricating a contract result"
                )
    return {
        "available": False,
        "requests": observed,
        "interpretation": "The physical candidate accepts OBS WebSocket v5 requests but does not expose the scene-switch.v1 Prepare/Take/Abort command surface; the 1,000-attempt contract campaign is therefore reported separately.",
    }


def _route_assertions(identity: Any, commits: list[Any]) -> dict[str, Any]:
    if not commits:
        raise RuntimeEvidenceError("no TakeCommitted route records were observed")
    lane_roots = {identity.lane_a, identity.lane_b}
    stable_surfaces = {
        "program_view": identity.program_view,
        "preview_view": identity.preview_view,
        "program_video": identity.program_video,
        "preview_video": identity.preview_video,
        "main_view": identity.main_view,
        "main_video": identity.main_video,
    }
    route_pairs = [(commit.onair_lane, commit.preview_lane) for commit in commits]
    roots_match_lanes = all(
        commit.onair_root == (identity.lane_a, identity.lane_b)[commit.onair_lane]
        and commit.preview_root == (identity.lane_a, identity.lane_b)[commit.preview_lane]
        for commit in commits
    )
    stable_identity = all(
        {
            "program_view": commit.program_view,
            "preview_view": commit.preview_view,
            "program_video": commit.program_video,
            "preview_video": commit.preview_video,
            "main_view": commit.main_view,
            "main_video": commit.main_video,
        }
        == stable_surfaces
        for commit in commits
    )
    frame_ids = [commit.frame_id for commit in commits]
    pts_values = [commit.pts_ns for commit in commits]
    return {
        "lane_ids_distinct": identity.lane_a != identity.lane_b,
        "surface_ids_distinct": identity.program_view != identity.preview_view
        and identity.program_video != identity.preview_video,
        "program_is_main_surface": identity.program_view == identity.main_view
        and identity.program_video == identity.main_video,
        "roots_match_lane_roles": roots_match_lanes,
        "surfaces_stable": stable_identity,
        "role_pairs_are_disjoint": all(on_air != preview for on_air, preview in route_pairs),
        "frame_ids_strictly_increasing": all(
            current > previous for previous, current in zip(frame_ids, frame_ids[1:])
        ),
        "pts_strictly_increasing": all(
            current > previous for previous, current in zip(pts_values, pts_values[1:])
        ),
        "take_committed_count": len(commits),
        "take_counts": [commit.count for commit in commits],
    }


def run_runtime_probe(exe: Path, encoder: str, takes: int) -> dict[str, Any]:
    if not exe.is_file():
        raise BASE.ProbeSkip(f"Pulsar binary not found: {exe}")
    if takes < MIN_TAKES:
        raise ValueError(f"issue #247 runtime leg requires at least {MIN_TAKES} Takes")
    candidate = exe.resolve()
    candidate_before = _sha256_file(candidate)
    observations: list[dict[str, Any]] = []
    originals = _patch_observation_hooks(observations)
    try:
        with tempfile.TemporaryDirectory(prefix="pulsar-247-runtime-") as record_dir_text:
            record_dir = Path(record_dir_text)
            process = BASE.PulsarProcess(candidate, encoder, record_dir)
            try:
                process.spawn()
                contract_surface = asyncio.run(_probe_scene_switch_surface(process))
                commits = asyncio.run(BASE.drive(process, takes))
                lines = process.snapshot()
                ready_match = next(
                    (match for line in lines if (match := BASE.READY_RE.search(line)) is not None),
                    None,
                )
                dual_match = next(
                    (match for line in lines if (match := BASE.DUAL_READY_RE.search(line)) is not None),
                    None,
                )
                if ready_match is None or dual_match is None:
                    raise RuntimeEvidenceError("runtime readiness identity was not retained")
                identity = BASE.parse_ready(dual_match)
                recording = _recording_file(record_dir)
                frame_hashes = _record_frame_hashes(recording)
                recording_sha256 = _sha256_file(recording)
                all_commits = [
                    BASE.parse_commit(match)
                    for line in lines
                    if (match := BASE.COMMIT_RE.search(line)) is not None
                ]
                if len(all_commits) != takes:
                    raise RuntimeEvidenceError(
                        f"expected {takes} parsed TakeCommitted records, found {len(all_commits)}"
                    )
                route_assertions = _route_assertions(identity, all_commits)
                evidence = {
                    "issue": ISSUE,
                    "adr_revision": ADR_REVISION,
                    "base_revision": BASE_REVISION,
                    "evidence_scope": "physical_runtime_leg",
                    "contract_leg_is_separate": True,
                    "contract_leg": {
                        "script": "scripts/contracts/scene_switch_v1/lifecycle_campaign.py",
                        "minimum_lifecycle_cycles": 100,
                        "minimum_controlled_attempts": 1000,
                        "frame_hashes_are_reference_only": True,
                    },
                    "source_probe": "scripts/probe-dual-lane.py",
                    "contract_surface_probe": contract_surface,
                    "candidate": {
                        "path": str(candidate),
                        "sha256": candidate_before,
                        "encoder_requested": encoder,
                    },
                    "runtime_identity": asdict(identity),
                    "route_assertions": route_assertions,
                    "take_committed": [asdict(commit) for commit in all_commits],
                    "lifecycle_observations": observations,
                    "recording": {
                        "path_name": recording.name,
                        "size_bytes": recording.stat().st_size,
                        "sha256": recording_sha256,
                        "frames": frame_hashes,
                    },
                    "frame_hash_correlation": {
                        "commit_frame_ids_and_pts_ns": "retained in take_committed",
                        "recording_frame_indices": "decoder order, relative to recording start",
                        "note": "The runtime commit clock and decoder PTS domains are not equated; no synthetic alias claim is made.",
                        "raw_nv12_status": "The public candidate exposes no raw encoder-input callback; decoded NV12 hashes are retained as a physical-output witness and are not mislabeled as raw input.",
                    },
                }
            finally:
                process.shutdown()
    finally:
        _restore_observation_hooks(originals)
    candidate_after = _sha256_file(candidate)
    if candidate_after != candidate_before:
        raise RuntimeEvidenceError("candidate binary changed during the runtime probe")
    evidence["candidate"]["sha256_after"] = candidate_after
    return evidence


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, default=BASE.DEFAULT_EXE)
    parser.add_argument("--encoder", choices=("x264", "nvenc"), required=True)
    parser.add_argument("--takes", type=int, default=MIN_TAKES)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.takes < MIN_TAKES:
        parser.error(f"--takes must be >= {MIN_TAKES}")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        report = run_runtime_probe(args.exe, args.encoder, args.takes)
        encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8", newline="\n")
            print(f"PASS: issue #247 physical runtime evidence written to {args.output}")
        else:
            print(encoded, end="")
        print(
            f"PASS: runtime_takes={report['route_assertions']['take_committed_count']} "
            f"recording_frames={report['recording']['frames']['decoded_nv12']['frame_count']} "
            f"candidate_sha256={report['candidate']['sha256']}"
        )
        return 0
    except BASE.ProbeSkip as exc:
        print(f"SKIP: {exc}")
        return BASE.EXIT_SKIP
    except (BASE.ProbeFailure, RuntimeEvidenceError, asyncio.TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return BASE.EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover - exercised by the runtime command
    raise SystemExit(main())
