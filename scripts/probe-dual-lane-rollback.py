#!/usr/bin/env python3
"""Exercise the dual-lane operational rollback guard (issue #249).

The runtime is started with ``PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES=1``.  The
first real studio Cut is therefore the rollback boundary: the current Program
frame must be the committed frame, the stable encoder/video surfaces must
remain bound, and subsequent mutating requests must fail closed.  This probe
does not pretend that a process restart is an in-process lane conversion; the
compatibility-path restart is documented in the #249 runbook.

Run against an exact built artifact::

    python scripts/probe-dual-lane-rollback.py --exe <pulsar.exe> --encoder x264
    python scripts/probe-dual-lane-rollback.py --exe <pulsar.exe> --encoder nvenc

The probe uses the same public obs-websocket v5 path and the same color-source
scene setup as ``probe-dual-lane.py``.  It deliberately keeps the workload
small: the long x264/NVENC canaries remain the responsibility of the main
dual-lane probe, while this command isolates the rollback boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import pathlib
import os
import re
import sys
import tempfile
from typing import Any


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE_PATH = REPO_ROOT / "scripts" / "probe-dual-lane.py"
SPEC = importlib.util.spec_from_file_location("pulsar_dual_lane_rollback_driver", PROBE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load shared dual-lane probe: {PROBE_PATH}")
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


ROLLBACK_RE = re.compile(
    probe.DUAL_LANE_LOG_PREFIX
    + r"\s*rollback committed at frame_id=(\d+) pts_ns=(\d+) "
    r"onair_lane=(-?\d+) preview_lane=(-?\d+) current_program_preserved=(\d) "
    r"active_video_t_rebound=(\d) new_takes_enabled=(\d)"
)


async def drive_rollback(process: Any) -> None:
    """Drive one accepted Cut, then prove the post-boundary freeze."""

    # Keep this function typed at runtime by using the shared module's class;
    # importing it as a dynamic script keeps this probe independent from a
    # package install and preserves the exact public-driver helpers.
    runtime = process
    ready_match = runtime.wait_for(probe.READY_RE, timeout=60)
    if ready_match.group(2) != runtime.password:
        raise probe.ProbeFailure("PULSAR_READY password did not match the generated probe secret")
    activation, activation_source, rollback_after = probe.assert_dual_lane_activation(runtime, expected=True)
    if rollback_after != 1:
        raise probe.ProbeFailure(
            "rollback probe was not armed for exactly one Take: "
            f"activation={activation!r} source={activation_source!r} after={rollback_after}"
        )
    identity = probe.parse_ready(runtime.wait_for(probe.DUAL_READY_RE, timeout=60))
    if (identity.lane_root_binding_valid, identity.program_main_view_valid,
            identity.program_main_video_valid, identity.preview_distinct_valid) != (1, 1, 1, 1):
        raise probe.ProbeFailure(f"dual-lane ready reported an invalid surface relation: {identity}")
    encoder_match = runtime.wait_for(probe.ENCODER_RE, timeout=60)
    actual_family = encoder_match.group(1).lower()
    if actual_family != runtime.encoder:
        if runtime.encoder == "nvenc":
            raise probe.ProbeSkip(
                f"requested NVENC but Pulsar boot selected {actual_family}; no usable NVENC device"
            )
        raise probe.ProbeFailure(
            f"requested encoder family {runtime.encoder!r}, boot selected {actual_family!r}"
        )

    import websockets

    async with websockets.connect(
        ready_match.group(1), subprotocols=["obswebsocket.json"], open_timeout=15
    ) as ws:
        await probe.identify(ws, runtime.password)
        inbox = probe.Inbox()
        await probe.create_public_lane_scenes(inbox, ws, runtime, lanes=("A", "B"))
        response = await probe.request(
            inbox, ws, "SetCurrentProgramScene", "rollback-program-A", {"sceneName": probe.SCENE_A}
        )
        probe.assert_success(response, "SetCurrentProgramScene(A)")
        response = await probe.request(
            inbox, ws, "SetStudioModeEnabled", "rollback-enable-studio", {"studioModeEnabled": True}
        )
        probe.assert_success(response, "SetStudioModeEnabled(true)")
        response = await probe.request(
            inbox, ws, "SetCurrentPreviewScene", "rollback-preview-B", {"sceneName": probe.SCENE_B}
        )
        probe.assert_success(response, "SetCurrentPreviewScene(B)")

        response = await probe.request(inbox, ws, "StartRecord", "rollback-start-record")
        probe.assert_success(response, "StartRecord")
        await probe.wait_event(
            inbox,
            ws,
            "RecordStateChanged",
            lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED",
        )
        bind_lines = [line for line in runtime.snapshot() if probe.ENCODER_BIND_RE.search(line)]
        if len(bind_lines) != 1:
            raise probe.ProbeFailure(
                f"expected exactly one setup-time encoder bind before rollback, got {len(bind_lines)}"
            )

        response = await probe.request(
            inbox,
            ws,
            "TriggerStudioModeTransition",
            "rollback-take-1",
            probe.take_telemetry_data(runtime, 1, probe.SCENE_B),
        )
        probe.assert_success(response, "TriggerStudioModeTransition(rollback boundary)")
        commit = probe.parse_commit(runtime.wait_for_commit(1, timeout=15))
        rollback_match = runtime.wait_for(ROLLBACK_RE, timeout=15)
        rollback_frame_id = int(rollback_match.group(1))
        rollback_pts_ns = int(rollback_match.group(2))
        if (rollback_frame_id, rollback_pts_ns) != (commit.frame_id, commit.pts_ns):
            raise probe.ProbeFailure(
                "rollback boundary does not match the committed frame/PTS: "
                f"commit={(commit.frame_id, commit.pts_ns)} "
                f"rollback={(rollback_frame_id, rollback_pts_ns)}"
            )
        if rollback_match.group(5) != "1" or rollback_match.group(6) != "0" or rollback_match.group(7) != "0":
            raise probe.ProbeFailure(f"rollback log reported unsafe state: {rollback_match.group(0)}")

        marker_path = runtime.record_dir / "pulsar-dual-lane-rollback.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise probe.ProbeFailure(f"rollback status marker is missing or invalid: {marker_path}: {exc}") from exc
        expected_marker = {
            "schema": "pulsar.dual-lane-rollback.v1",
            "state": "frozen",
            "frame_id": commit.frame_id,
            "pts_ns": commit.pts_ns,
            "onair_lane": commit.onair_lane,
            "preview_lane": commit.preview_lane,
            "current_program_preserved": True,
            "active_video_t_rebound": False,
            "new_takes_enabled": False,
            "program_view_stable": True,
            "preview_view_stable": True,
        }
        for key, value in expected_marker.items():
            if marker.get(key) != value:
                raise probe.ProbeFailure(
                    f"rollback status marker mismatch for {key!r}: got {marker.get(key)!r}, expected {value!r}"
                )

        await probe.assert_distinct_selected_scenes(
            inbox, ws, "rollback-committed", expected_program=probe.SCENE_B, expected_preview=probe.SCENE_A
        )

        # The bridge rejects every mutation after the committed boundary.  A
        # read remains available, proving that rollback freezes the current
        # Program rather than tearing down the live output.
        response = await probe.request(
            inbox, ws, "SetCurrentPreviewScene", "rollback-rejected-preview", {"sceneName": probe.SCENE_B}
        )
        probe.assert_preview_frozen(response, "SetCurrentPreviewScene after rollback")
        response = await probe.request(
            inbox,
            ws,
            "CreateInput",
            "rollback-rejected-mutation",
            {
                "sceneName": probe.SCENE_A,
                "inputName": "probe-dual-lane-rollback-rejected",
                "inputKind": "color_source_v3",
                "inputSettings": {"color": probe.COLOR_GREEN_ABGR, "width": probe.CANVAS_W, "height": probe.CANVAS_H},
                "sceneItemEnabled": True,
            },
        )
        probe.assert_preview_frozen(response, "CreateInput after rollback")
        await probe.assert_scene_item_presence(
            inbox, ws, probe.SCENE_A, "probe-dual-lane-rollback-rejected", False, "rollback mutation rejection"
        )
        await probe.assert_distinct_selected_scenes(
            inbox, ws, "rollback-frozen", expected_program=probe.SCENE_B, expected_preview=probe.SCENE_A
        )

        response = await probe.request(inbox, ws, "StopRecord", "rollback-stop-record")
        probe.assert_success(response, "StopRecord")
        stopped = await probe.wait_event(
            inbox,
            ws,
            "RecordStateChanged",
            lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED",
        )
        output_path = (stopped.get("eventData") or {}).get("outputPath") or ""
        if not output_path:
            raise probe.ProbeFailure("rollback recording STOPPED did not include outputPath")
        ffprobe = probe.find_ffprobe()
        if not ffprobe:
            raise probe.ProbeSkip("ffprobe is required for the rollback recording proof")
        probe.verify_recording(output_path, ffprobe)

    bind_lines_after = [line for line in runtime.snapshot() if probe.ENCODER_BIND_RE.search(line)]
    if len(bind_lines_after) != 1:
        raise probe.ProbeFailure(
            f"rollback caused an encoder/video surface rebind: observed {len(bind_lines_after)} binds"
        )
    print(
        "PASS: rollback committed at frame_id="
        f"{commit.frame_id} pts_ns={commit.pts_ns}; Program preserved, mutations frozen, "
        "stable encoder/video binding count=1"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=pathlib.Path, required=True)
    parser.add_argument("--encoder", choices=("x264", "nvenc"), required=True)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if not args.exe.is_file():
        print(f"SKIP: Pulsar binary not found: {args.exe}")
        return probe.EXIT_SKIP
    with tempfile.TemporaryDirectory(prefix="pulsar-dual-lane-rollback-") as record_dir_text:
        process = probe.PulsarProcess(args.exe.resolve(), args.encoder, pathlib.Path(record_dir_text))
        previous = os.environ.get("PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES")
        os.environ["PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES"] = "1"
        result = probe.EXIT_FAIL
        try:
            process.spawn()
            print(f"dual-lane rollback probe: encoder={args.encoder} exe={args.exe}")
            asyncio.run(drive_rollback(process))
            result = 0
        except probe.ProbeSkip as exc:
            print(f"SKIP: {exc}")
            result = probe.EXIT_SKIP
        except (probe.ProbeFailure, asyncio.TimeoutError, OSError, ValueError) as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            result = probe.EXIT_FAIL
        finally:
            process.shutdown()
            if process.thread is not None:
                process.thread.join(timeout=2)
            if previous is None:
                os.environ.pop("PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES", None)
            else:
                os.environ["PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES"] = previous
        if result == 0:
            lines = process.snapshot()
            if not any("PULSAR_RUNTIME_INSTANCE lease=released" in line for line in lines):
                print("FAIL: runtime instance lease was not released at shutdown", file=sys.stderr)
                return probe.EXIT_FAIL
            if not any("runtime_dir_lease=released" in line for line in lines):
                print("FAIL: runtime directory lease was not released at shutdown", file=sys.stderr)
                return probe.EXIT_FAIL
            alias_acquired = any("PULSAR_LEGACY_ALIAS lease=acquired" in line for line in lines)
            alias_refused_or_disabled = any(
                "PULSAR_LEGACY_ALIAS lease=refused" in line or "PULSAR_LEGACY_ALIAS lease=disabled" in line
                for line in lines
            )
            if alias_acquired and not any("PULSAR_LEGACY_ALIAS lease=released" in line for line in lines):
                print("FAIL: acquired DirectShow alias lease was not released", file=sys.stderr)
                return probe.EXIT_FAIL
            if not alias_acquired and not alias_refused_or_disabled:
                print("FAIL: DirectShow alias lease state was not observable", file=sys.stderr)
                return probe.EXIT_FAIL
            print("PASS: runtime and DirectShow lease state was observable through release")
        return result


def main() -> int:
    return run(parse_args(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
