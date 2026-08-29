#!/usr/bin/env python3
"""Runtime proof for the stable common Program audio route (issue #245).

This probe drives the real Pulsar child through obs-websocket v5, starts a local
recording, and commits ``--takes`` dual-lane video Cuts.  After every Cut it
reads the ``pulsar:GetProgramAudioRoute`` vendor contract and checks:

* the explicit route id/audio identity and every present output identity stay
  stable;
* actual encoder-fed mixer callbacks report frames and monotone audio PTS;
* changing the public Preview video scene after a committed Cut does not alter
  the Program audio route or its source/output identities; and
* r2 does not imply Preview audio or AFV support.

The route snapshots and commit evidence can be written to ``--evidence`` for
an independent reviewer.  The underlying recording is validated with ffprobe,
so the result is a real process/output proof rather than a source-only model.

Example::

    python scripts/probe-program-audio.py --exe <pulsar.exe> --takes 100 \
        --evidence .agent-tmp/program-audio-245.json

Exit codes are 0 (pass), 1 (assertion/runtime failure), 2 (usage or missing
WebSocket dependency), and 3 (typed environment skip, e.g. no binary or
ffprobe).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import pathlib
import sys
import tempfile
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
BASE_PATH = SCRIPT_DIR / "probe-dual-lane.py"


def load_base_probe() -> Any:
    spec = importlib.util.spec_from_file_location("pulsar_probe_dual_lane", BASE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load shared probe helpers from {BASE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = load_base_probe()

EXIT_FAIL = base.EXIT_FAIL
EXIT_USAGE = base.EXIT_USAGE
EXIT_SKIP = base.EXIT_SKIP
ProbeFailure = base.ProbeFailure
ProbeSkip = base.ProbeSkip
PulsarProcess = base.PulsarProcess
Inbox = base.Inbox
request = base.request
identify = base.identify
assert_success = base.assert_success
wait_event = base.wait_event
create_scene = base.create_scene
create_input = base.create_input
assert_distinct_selected_scenes = base.assert_distinct_selected_scenes
parse_commit = base.parse_commit
parse_ready = base.parse_ready
validate_commit = base.validate_commit
find_ffprobe = base.find_ffprobe
verify_recording = base.verify_recording
READY_RE = base.READY_RE
DUAL_READY_RE = base.DUAL_READY_RE
ENCODER_RE = base.ENCODER_RE
COMMIT_RE = base.COMMIT_RE
ENCODER_BIND_RE = base.ENCODER_BIND_RE
SCENE_A = base.SCENE_A
SCENE_B = base.SCENE_B
INPUT_A = base.INPUT_A
INPUT_B = base.INPUT_B
COLOR_RED_ABGR = base.COLOR_RED_ABGR
COLOR_GREEN_ABGR = base.COLOR_GREEN_ABGR
CANVAS_W = base.CANVAS_W
CANVAS_H = base.CANVAS_H


async def get_program_audio_route(inbox: Any, ws: Any, sequence: int) -> dict[str, Any]:
    response = await request(
        inbox,
        ws,
        "CallVendorRequest",
        f"program-audio-route-{sequence}",
        {
            "vendorName": "pulsar",
            "requestType": "GetProgramAudioRoute",
            "requestData": {},
        },
    )
    assert_success(response, "CallVendorRequest(GetProgramAudioRoute)")
    envelope = response.get("responseData") or {}
    route = envelope.get("responseData") or {}
    if not isinstance(route, dict):
        raise ProbeFailure(f"GetProgramAudioRoute returned a non-object response: {response}")
    if route.get("error"):
        raise ProbeFailure(f"GetProgramAudioRoute reported an error: {route}")
    return route


def route_key(route: dict[str, Any]) -> dict[str, Any]:
    """Fields that must not change when only Preview video is mutated."""

    outputs = []
    for output in route.get("outputs") or []:
        if not isinstance(output, dict):
            continue
        outputs.append(
            {
                "output": output.get("output"),
                "id": output.get("id"),
                "name": output.get("name"),
                "audio_supported": output.get("audio_supported"),
                "audio_identity": output.get("audio_identity"),
                "audio_matches_route": output.get("audio_matches_route"),
                "slots": output.get("slots") or [],
            }
        )
    sources = []
    for source in route.get("sources") or []:
        if not isinstance(source, dict):
            continue
        sources.append(
            {
                "channel": source.get("channel"),
                "identity": source.get("identity"),
                "id": source.get("id"),
                "name": source.get("name"),
            }
        )
    tracks = []
    for track in route.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        tracks.append(
            {
                "track": track.get("track"),
                "mixer_index": track.get("mixer_index"),
                "encoder": track.get("encoder"),
            }
        )
    return {
        "schema_version": route.get("schema_version"),
        "route_id": route.get("route_id"),
        "route_name": route.get("route_name"),
        "scope": route.get("scope"),
        "cut_audio_policy": route.get("cut_audio_policy"),
        "audio_identity": route.get("audio_identity"),
        "preview_audio_supported": route.get("preview_audio_supported"),
        "afv_supported": route.get("afv_supported"),
        "outputs": outputs,
        "sources": sources,
        "tracks": tracks,
    }


def check_route(
    route: dict[str, Any],
    previous: dict[str, Any] | None,
    expected_key: dict[str, Any] | None,
    label: str,
) -> dict[str, Any]:
    if route.get("schema_version") != 1:
        raise ProbeFailure(f"{label}: unexpected ProgramAudio schema: {route}")
    if route.get("route_id") != "program-common" or route.get("route_name") != "ProgramAudio":
        raise ProbeFailure(f"{label}: explicit ProgramAudio identity missing: {route}")
    if route.get("scope") != "program":
        raise ProbeFailure(f"{label}: route scope is not program: {route.get('scope')!r}")
    if route.get("cut_audio_policy") != "common-program-route-unchanged":
        raise ProbeFailure(f"{label}: implicit/unknown Cut audio policy: {route}")
    if route.get("stable") is not True or route.get("observed") is not True:
        raise ProbeFailure(f"{label}: route was not stable and observed: {route}")
    if route.get("preview_audio_supported") is not False or route.get("afv_supported") is not False:
        raise ProbeFailure(f"{label}: r2 accidentally advertises Preview audio/AFV: {route}")

    route_audio = route.get("audio_identity")
    if not isinstance(route_audio, str) or not route_audio or route_audio == "0x0":
        raise ProbeFailure(f"{label}: route audio identity is empty: {route_audio!r}")

    outputs = route.get("outputs") or []
    if not outputs:
        raise ProbeFailure(f"{label}: no output read-back entries: {route}")
    for output in outputs:
        if not isinstance(output, dict):
            raise ProbeFailure(f"{label}: malformed output entry: {output!r}")
        if output.get("audio_supported") is True:
            if output.get("audio_matches_route") is not True:
                raise ProbeFailure(f"{label}: audio output does not consume common route: {output}")
            if output.get("audio_identity") != route_audio:
                raise ProbeFailure(f"{label}: audio output identity drift: {output}")
        elif output.get("audio_matches_route") is not False:
            raise ProbeFailure(f"{label}: non-audio output was not explicit: {output}")

    tracks = route.get("tracks") or []
    if not tracks:
        raise ProbeFailure(f"{label}: no actual encoder-fed ProgramAudio track: {route}")
    if route.get("pts_monotone") is not True:
        raise ProbeFailure(f"{label}: route-level PTS is not monotone: {route}")
    for track in tracks:
        if not isinstance(track, dict):
            raise ProbeFailure(f"{label}: malformed track entry: {track!r}")
        if track.get("frames", 0) <= 0 or track.get("pts_samples", 0) <= 0:
            raise ProbeFailure(f"{label}: track has no flowing audio frames/PTS: {track}")
        if track.get("pts_monotone") is not True or track.get("pts_regressions", 1) != 0:
            raise ProbeFailure(f"{label}: track PTS regressed: {track}")
        nested = track.get("pts") or {}
        if nested.get("monotone") is not True or nested.get("regressions", 1) != 0:
            raise ProbeFailure(f"{label}: nested track PTS evidence regressed: {track}")
        series = nested.get("series_ns") or track.get("pts_series_ns") or []
        series_values = [
            point.get("pts_ns")
            for point in series
            if isinstance(point, dict) and isinstance(point.get("pts_ns"), int)
        ]
        if not series_values:
            raise ProbeFailure(f"{label}: no raw audio PTS series was returned: {track}")
        if any(right < left for left, right in zip(series_values, series_values[1:])):
            raise ProbeFailure(f"{label}: PTS history is not monotone: {series_values}")
        if track.get("last_pts_ns", 0) < track.get("first_pts_ns", 0):
            raise ProbeFailure(f"{label}: track first/last PTS order is invalid: {track}")
        if previous is not None:
            previous_tracks = {
                item.get("track"): item
                for item in previous.get("tracks") or []
                if isinstance(item, dict)
            }
            old = previous_tracks.get(track.get("track"))
            if old is not None and track.get("last_pts_ns", 0) < old.get("last_pts_ns", 0):
                raise ProbeFailure(
                    f"{label}: observed last audio PTS moved backwards: "
                    f"{old.get('last_pts_ns')} -> {track.get('last_pts_ns')}"
                )

    key = route_key(route)
    if expected_key is not None and key != expected_key:
        raise ProbeFailure(f"{label}: route/source identity changed: before={expected_key} after={key}")
    return key


async def drive(process: Any, takes: int, evidence_path: pathlib.Path | None) -> list[Any]:
    ready_match = process.wait_for(READY_RE, timeout=60)
    if ready_match.group(2) != process.password:
        raise ProbeFailure("PULSAR_READY password did not match the generated probe secret")

    identity = parse_ready(process.wait_for(DUAL_READY_RE, timeout=60))
    if identity.program_view != identity.main_view or identity.program_video != identity.main_video:
        raise ProbeFailure(f"Program surface is not the libobs main view/video: {identity}")
    if identity.lane_a == identity.lane_b or identity.program_view == identity.preview_view:
        raise ProbeFailure(f"dual-lane surfaces are aliased: {identity}")

    encoder_match = process.wait_for(ENCODER_RE, timeout=60)
    if encoder_match.group(1).lower() != process.encoder:
        if process.encoder == "nvenc":
            raise ProbeSkip(
                f"requested NVENC but Pulsar boot selected {encoder_match.group(1)}; no usable NVENC device"
            )
        raise ProbeFailure(
            f"requested encoder family {process.encoder!r}, boot selected {encoder_match.group(1)!r}"
        )
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise ProbeSkip("ffprobe is required for active-recording ProgramAudio proof")

    route_snapshots: list[dict[str, Any]] = []
    commits: list[Any] = []
    preview_mutation_key: dict[str, Any] | None = None

    async with base.websockets.connect(
        ready_match.group(1), subprotocols=["obswebsocket.json"], open_timeout=15
    ) as ws:
        await identify(ws, process.password)
        inbox = Inbox()
        await create_scene(inbox, ws, SCENE_A, INPUT_A, COLOR_RED_ABGR)
        await create_scene(inbox, ws, SCENE_B, INPUT_B, COLOR_GREEN_ABGR)

        response = await request(
            inbox,
            ws,
            "SetCurrentProgramScene",
            "program-audio-initial-program",
            {"sceneName": SCENE_A},
        )
        assert_success(response, "SetCurrentProgramScene(A)")
        response = await request(
            inbox,
            ws,
            "SetStudioModeEnabled",
            "program-audio-enable-studio",
            {"studioModeEnabled": True},
        )
        assert_success(response, "SetStudioModeEnabled(true)")
        response = await request(inbox, ws, "StartRecord", "program-audio-start-record")
        assert_success(response, "StartRecord")
        await wait_event(
            inbox,
            ws,
            "RecordStateChanged",
            lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED",
        )

        if len([line for line in process.snapshot() if ENCODER_BIND_RE.search(line)]) != 1:
            raise ProbeFailure("expected one setup-time Program video encoder binding")

        # The first read installs the persistent raw-audio observer.  Ignore
        # that wiring-only snapshot, then allow the observer and recording
        # encoder to produce a non-empty baseline before the first Cut.
        await get_program_audio_route(inbox, ws, -1)
        await asyncio.sleep(0.5)
        route = await get_program_audio_route(inbox, ws, 0)
        expected_key = check_route(route, None, None, "baseline")
        route_snapshots.append(route)

        for number in range(1, takes + 1):
            target = SCENE_B if number % 2 else SCENE_A
            response = await request(
                inbox,
                ws,
                "SetCurrentPreviewScene",
                f"program-audio-set-preview-{number}",
                {"sceneName": target},
            )
            assert_success(response, f"SetCurrentPreviewScene({target})")
            await assert_distinct_selected_scenes(
                inbox,
                ws,
                f"program-audio-take-{number}",
                expected_program=SCENE_A if number % 2 else SCENE_B,
                expected_preview=target,
            )
            response = await request(
                inbox,
                ws,
                "TriggerStudioModeTransition",
                f"program-audio-take-{number}",
            )
            assert_success(response, f"TriggerStudioModeTransition({number})")
            commit = parse_commit(process.wait_for_commit(number, timeout=15))
            validate_commit(identity, commits[-1] if commits else None, commit)
            commits.append(commit)

            # A short wait ensures this sample observes fresh audio blocks; the
            # route identity itself must be stable even when counters advance.
            await asyncio.sleep(0.04)
            route = await get_program_audio_route(inbox, ws, number)
            check_route(route, route_snapshots[-1], expected_key, f"Cut {number}")
            route_snapshots.append(route)

            if number == 1:
                # A post-commit Preview-only video mutation is deliberately
                # applied to the now-Preview public scene.  No audio request,
                # source or mixer operation accompanies this mutation.
                await create_input(
                    inbox,
                    ws,
                    SCENE_A,
                    "probe-program-audio-preview-video-mutation",
                    COLOR_GREEN_ABGR,
                )
                await asyncio.sleep(0.2)
                mutated = await get_program_audio_route(inbox, ws, number * 1000)
                preview_mutation_key = check_route(
                    mutated,
                    route_snapshots[-1],
                    expected_key,
                    "Preview video mutation",
                )
                route_snapshots.append(mutated)

        response = await request(inbox, ws, "StopRecord", "program-audio-stop-record")
        assert_success(response, "StopRecord")
        stopped = await wait_event(
            inbox,
            ws,
            "RecordStateChanged",
            lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED",
        )
        output_path = (stopped.get("eventData") or {}).get("outputPath") or ""
        if not output_path:
            raise ProbeFailure("RecordStateChanged STOPPED did not include outputPath")

        all_commits = [
            parse_commit(match)
            for line in process.snapshot()
            if (match := COMMIT_RE.search(line)) is not None
        ]
        if len(all_commits) != takes or [commit.count for commit in all_commits] != list(
            range(1, takes + 1)
        ):
            raise ProbeFailure(
                f"expected exactly {takes} contiguous TakeCommitted logs, got "
                f"{[commit.count for commit in all_commits]}"
            )
        verify_recording(output_path, ffprobe)

    if preview_mutation_key != expected_key:
        raise ProbeFailure("Preview video mutation changed the Program audio route key")
    if evidence_path is not None:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(
                {
                    "issue": 245,
                    "route_id": "program-common",
                    "takes": takes,
                    "commits": [commit.__dict__ for commit in commits],
                    "route_snapshots": route_snapshots,
                    "preview_mutation_key": preview_mutation_key,
                    "recording": output_path,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return commits


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=pathlib.Path, default=base.DEFAULT_EXE)
    parser.add_argument("--encoder", choices=("x264", "nvenc"), default="x264")
    parser.add_argument("--takes", type=int, default=100)
    parser.add_argument("--evidence", type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.takes < 1:
        parser.error("--takes must be >= 1")
    return args


def run(args: argparse.Namespace) -> int:
    if not args.exe.is_file():
        print(f"SKIP: Pulsar binary not found: {args.exe}")
        return EXIT_SKIP

    with tempfile.TemporaryDirectory(prefix="pulsar-program-audio-") as record_dir_text:
        process = PulsarProcess(args.exe.resolve(), args.encoder, pathlib.Path(record_dir_text))
        try:
            process.spawn()
            print(
                f"program-audio probe: encoder={args.encoder} takes={args.takes} exe={args.exe}"
            )
            commits = asyncio.run(drive(process, args.takes, args.evidence))
            print(
                f"PASS: {len(commits)} Cuts; ProgramAudio identity/outputs stable, "
                "audio frames observed, PTS monotone, Preview video mutation isolated"
            )
            if args.evidence:
                print(f"   evidence: {args.evidence}")
            return 0
        except ProbeSkip as exc:
            print(f"SKIP: {exc}")
            return EXIT_SKIP
        except (ProbeFailure, asyncio.TimeoutError, OSError, json.JSONDecodeError) as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return EXIT_FAIL
        finally:
            process.shutdown()


def main() -> int:
    try:
        args = parse_args(sys.argv[1:])
    except SystemExit as exc:
        return int(exc.code)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
