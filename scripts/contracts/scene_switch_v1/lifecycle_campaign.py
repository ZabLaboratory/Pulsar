"""Deterministic QA campaigns for the ``pulsar.scene-switch.v1`` contract.

The contract tests exercise individual transitions.  This module adds the
volume and correlation proof required by issue #247: repeated lifecycle/alias
sequences, controlled duplicate/concurrent/stale attempts, stable route
identities, and frame-content hashes around the Preview mutation boundary.

The campaign deliberately runs against :class:`SceneSwitchMachine`, the
transport-neutral reference machine.  It does not pretend to be a GPU/video
proof: the lane/frame oracle is a deterministic contract-level witness.  The
physical A/B and surface checks remain owned by ``scripts/probe-dual-lane.py``.
The resulting JSON is suitable for review and contains the complete command /
result matrix without timestamps derived from the wall clock.

Run from the repository root::

    python -m scripts.contracts.scene_switch_v1.lifecycle_campaign \
      --output docs/evidence/247/scene-switch-lifecycle-race.json

The module has no production dependency and is safe to run offline.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, dataclass
import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import (
    CONTRACT,
    SCHEMA_VERSION,
    SceneSwitchMachine,
    SceneSwitchStateError,
    validate_event,
)


DEFAULT_CYCLES = 128
DEFAULT_ATTEMPTS = 1024
CAMPAIGN_NAME = "pulsar-scene-switch-v1-lifecycle-race"
ADR_REVISION = "ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane-20260828"
BASE_REVISION = "8a26b8a992a9b5a783078e83f719df53b2b107ed"
RUNTIME_ID = "probe-247-runtime"


@dataclass(frozen=True)
class SurfaceIdentity:
    """Stable downstream identities exposed by the dual-lane topology."""

    program_view: str = "ProgramView#stable"
    preview_view: str = "PreviewView#stable"
    program_video: str = "ProgramVideo#stable"
    preview_video: str = "PreviewVideo#stable"
    encoder: str = "EncoderVideo#stable"
    program_return: str = "ProgramReturn#stable"
    preview_return: str = "PreviewReturn#stable"


@dataclass(frozen=True)
class FrameWitness:
    """A deterministic content witness for one lane/frame observation."""

    lane_id: str
    scene_id: str
    frame_id: int
    pts_ns: int
    content_sha256: str


def _frame_witness(lane_id: str, scene_id: str, frame_id: int, pts_ns: int) -> FrameWitness:
    # The frame hash is intentionally based on the logical content and frame
    # identity, not on process timing or pointer addresses.  This lets the
    # evidence compare the exact same frame slot before/after a Preview-only
    # mutation while keeping the witness reproducible on every host.
    payload = f"lane={lane_id}\0scene={scene_id}\0frame={frame_id}\0pts={pts_ns}".encode("utf-8")
    return FrameWitness(lane_id, scene_id, frame_id, pts_ns, hashlib.sha256(payload).hexdigest())


def _content_hash(scene_id: str, frame_slot: int) -> str:
    payload = f"scene={scene_id}\0frame_slot={frame_slot}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _event_result(event: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the correlation-bearing event fields in the evidence matrix."""

    fields = (
        "event_type",
        "command_id",
        "intent_id",
        "runtime_instance_id",
        "server_seq",
        "state",
        "previous_revisions",
        "revisions",
        "role_map",
        "previous_role_map",
        "payload_sha256",
        "error_code",
        "expected_revisions",
        "expected_server_seq",
        "target_lane_id",
        "target_scene_id",
        "take_command_id",
        "source_lane_id",
        "program_lane_id",
        "preview_lane_id",
        "frame_id",
        "pts_ns",
        "reason",
    )
    return {key: deepcopy(event[key]) for key in fields if key in event}


def _mutable_state(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return product state that a rejected command must leave unchanged.

    A rejection is itself an observable event, so ``server_seq`` legitimately
    advances.  It is deliberately excluded here; route, lane content,
    revisions, state, and pending reservations are the mutation boundary.
    """

    keys = (
        "state",
        "revisions",
        "role_map",
        "lane_scenes",
        "pending_prepare_command_id",
        "pending_take_command_id",
    )
    return {key: deepcopy(snapshot[key]) for key in keys}


def _assert_event(event: Mapping[str, Any], event_type: str) -> dict[str, Any]:
    validated = validate_event(event)
    if validated["event_type"] != event_type:
        raise AssertionError(f"expected {event_type}, got {validated['event_type']}")
    return validated


def _command(
    command_type: str,
    command_id: str,
    intent_id: str,
    machine: SceneSwitchMachine,
    **fields: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "message_type": "command",
        "command_type": command_type,
        "command_id": command_id,
        "intent_id": intent_id,
        "runtime_instance_id": machine.runtime_instance_id,
        "expected_revisions": machine.revisions,
        "expected_server_seq": machine.server_seq,
    }
    value.update(fields)
    return value


def _run_concurrently(
    calls: Sequence[Callable[[], dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Run calls concurrently while preserving input order in the result list."""

    if not calls:
        return []
    with ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="probe-247") as pool:
        futures = [pool.submit(call) for call in calls]
        return [future.result() for future in futures]


class LaneOracle:
    """Contract-level witness for lane content and stable route identities."""

    def __init__(self, machine: SceneSwitchMachine) -> None:
        self.machine = machine
        self.surfaces = SurfaceIdentity()

    def snapshot(self) -> dict[str, Any]:
        state = self.machine.snapshot()
        on_air = state["role_map"]["on_air"]
        preview = state["role_map"]["preview"]
        return {
            "state": state["state"],
            "server_seq": state["server_seq"],
            "revisions": deepcopy(state["revisions"]),
            "pending_prepare_command_id": state["pending_prepare_command_id"],
            "pending_take_command_id": state["pending_take_command_id"],
            "role_map": deepcopy(state["role_map"]),
            "lane_scenes": deepcopy(state["lane_scenes"]),
            "on_air_lane": on_air,
            "preview_lane": preview,
            "on_air_root": f"LaneRoot:{on_air}",
            "preview_root": f"LaneRoot:{preview}",
            "surfaces": asdict(self.surfaces),
        }

    def program_hashes(self, frame_count: int = 30) -> list[str]:
        state = self.machine.snapshot()
        scene_id = state["lane_scenes"].get(state["role_map"]["on_air"], "<unset>")
        return [_content_hash(scene_id, slot) for slot in range(frame_count)]

    def frame(self, frame_id: int, pts_ns: int) -> FrameWitness:
        state = self.machine.snapshot()
        lane_id = state["role_map"]["on_air"]
        scene_id = state["lane_scenes"].get(lane_id, "<unset>")
        return _frame_witness(lane_id, scene_id, frame_id, pts_ns)


def run_lifecycle_campaign(cycles: int = DEFAULT_CYCLES) -> dict[str, Any]:
    """Run repeated pre/post-commit alias and lifecycle checks."""

    if cycles < 100:
        raise ValueError("issue #247 requires at least 100 lifecycle cycles")

    machine = SceneSwitchMachine(
        RUNTIME_ID,
        initial_lane_scenes={"A": "scene-on-air-000", "B": "scene-preview-000"},
    )
    oracle = LaneOracle(machine)
    records: list[dict[str, Any]] = []
    counts = {
        "prepare_accepted": 0,
        "preview_ready": 0,
        "take_accepted": 0,
        "take_committed": 0,
        "precommit_preview_frozen": 0,
        "promoted_lane_rejected": 0,
        "postcommit_prepare_accepted": 0,
        "postcommit_preview_ready": 0,
    }

    for sequence in range(1, cycles + 1):
        before = oracle.snapshot()
        target_lane = before["preview_lane"]
        old_on_air_lane = before["on_air_lane"]
        intent_id = f"intent-{sequence:04d}"
        prepare = _command(
            "Prepare",
            f"prepare-{sequence:04d}",
            intent_id,
            machine,
            target={"lane_id": target_lane, "scene_id": f"scene-preview-{sequence:04d}"},
            timeout_ms=2000,
        )
        prepare_event = _assert_event(
            machine.dispatch(prepare, now_monotonic_ns=sequence * 10_000_000_000 + 1_000_000),
            "PrepareAccepted",
        )
        counts["prepare_accepted"] += 1
        preview_frame = _frame_witness(
            target_lane,
            prepare["target"]["scene_id"],
            10_000 + sequence,
            sequence * 16_666_667,
        )
        ready_event = _assert_event(
            machine.mark_preview_ready(
                prepare["command_id"],
                preview_frame.frame_id,
                preview_frame.pts_ns,
                now_monotonic_ns=sequence * 10_000_000_000 + 2_000_000,
            ),
            "PreviewReady",
        )
        counts["preview_ready"] += 1

        take = _command(
            "Take",
            f"take-{sequence:04d}",
            intent_id,
            machine,
            prepared_command_id=prepare["command_id"],
            timeout_ms=1000,
        )
        take_accepted = _assert_event(
            machine.dispatch(take, now_monotonic_ns=sequence * 10_000_000_000 + 3_000_000),
            "TakeAccepted",
        )
        counts["take_accepted"] += 1

        # This is the decisive pre-commit alias probe.  The future Preview is
        # the old OnAir lane; mutating it while the Take is accepted must not
        # touch lane content, role mapping, or revisions.
        precommit_before = oracle.snapshot()
        premature = _command(
            "Prepare",
            f"precommit-{sequence:04d}",
            f"premature-{sequence:04d}",
            machine,
            target={"lane_id": old_on_air_lane, "scene_id": f"scene-must-not-leak-{sequence:04d}"},
            timeout_ms=2000,
        )
        premature_event = _assert_event(
            machine.dispatch(premature, now_monotonic_ns=sequence * 10_000_000_000 + 4_000_000),
            "CommandRejected",
        )
        if premature_event["error_code"] != "PREVIEW_FROZEN":
            raise AssertionError(f"pre-commit mutation was not frozen: {premature_event}")
        precommit_after = oracle.snapshot()
        if _mutable_state(precommit_after) != _mutable_state(precommit_before):
            raise AssertionError("pre-commit Preview mutation changed the route or lane state")
        counts["precommit_preview_frozen"] += 1

        commit_frame = _frame_witness(
            target_lane,
            prepare["target"]["scene_id"],
            20_000 + sequence,
            sequence * 16_666_667 + 8_333_333,
        )
        committed = _assert_event(
            machine.commit_take(
                take["command_id"],
                commit_frame.frame_id,
                commit_frame.pts_ns,
                now_monotonic_ns=sequence * 10_000_000_000 + 5_000_000,
            ),
            "TakeCommitted",
        )
        counts["take_committed"] += 1
        after_commit = oracle.snapshot()
        expected_map = {"on_air": target_lane, "preview": old_on_air_lane}
        if after_commit["role_map"] != expected_map:
            raise AssertionError(f"role map did not swap atomically: {after_commit}")
        if committed["program_lane_id"] != target_lane or committed["preview_lane_id"] != old_on_air_lane:
            raise AssertionError(f"commit lane evidence is inconsistent: {committed}")
        if committed["frame_id"] != commit_frame.frame_id or committed["pts_ns"] != commit_frame.pts_ns:
            raise AssertionError("commit did not carry the selected frame-boundary witness")
        if tuple(after_commit["surfaces"].values()) != tuple(oracle.surfaces.__dict__.values()):
            raise AssertionError("stable surface identity changed across commit")
        # The promotion itself legitimately changes Program content.  Capture
        # the post-commit baseline, then compare the same frame slots after the
        # new Preview is mutated; comparing against the pre-Take baseline would
        # conflate a valid Take with an alias leak.
        commit_program_hashes = oracle.program_hashes()

        # A promoted lane is now Program and must reject direct Preview
        # mutation.  The former OnAir lane is the only legal new Preview.
        promoted_before = oracle.snapshot()
        promoted_prepare = _command(
            "Prepare",
            f"promoted-{sequence:04d}",
            f"promoted-intent-{sequence:04d}",
            machine,
            target={"lane_id": target_lane, "scene_id": f"scene-promoted-must-stay-on-air-{sequence:04d}"},
            timeout_ms=2000,
        )
        promoted_event = _assert_event(
            machine.dispatch(promoted_prepare, now_monotonic_ns=sequence * 10_000_000_000 + 6_000_000),
            "CommandRejected",
        )
        if promoted_event["error_code"] != "PREVIEW_LANE_MISMATCH":
            raise AssertionError(f"promoted lane accepted Preview mutation: {promoted_event}")
        if _mutable_state(oracle.snapshot()) != _mutable_state(promoted_before):
            raise AssertionError("promoted lane rejection mutated route state")
        counts["promoted_lane_rejected"] += 1

        post_prepare = _command(
            "Prepare",
            f"postprepare-{sequence:04d}",
            f"postintent-{sequence:04d}",
            machine,
            target={"lane_id": old_on_air_lane, "scene_id": f"scene-post-preview-{sequence:04d}"},
            timeout_ms=2000,
        )
        post_prepare_event = _assert_event(
            machine.dispatch(post_prepare, now_monotonic_ns=sequence * 10_000_000_000 + 7_000_000),
            "PrepareAccepted",
        )
        counts["postcommit_prepare_accepted"] += 1
        post_ready_frame = _frame_witness(
            old_on_air_lane,
            post_prepare["target"]["scene_id"],
            30_000 + sequence,
            sequence * 16_666_667 + 10_000_000,
        )
        post_ready_event = _assert_event(
            machine.mark_preview_ready(
                post_prepare["command_id"],
                post_ready_frame.frame_id,
                post_ready_frame.pts_ns,
                now_monotonic_ns=sequence * 10_000_000_000 + 8_000_000,
            ),
            "PreviewReady",
        )
        counts["postcommit_preview_ready"] += 1
        after_preview_mutation = oracle.snapshot()
        after_program_hashes = oracle.program_hashes()
        if after_program_hashes != commit_program_hashes:
            raise AssertionError(
                f"post-commit Preview mutation changed Program frame hashes at sequence {sequence}"
            )

        records.append(
            {
                "sequence": sequence,
                "before": before,
                "commands": {
                    "prepare": prepare,
                    "take": take,
                    "premature_prepare": premature,
                    "promoted_lane_prepare": promoted_prepare,
                    "postcommit_prepare": post_prepare,
                },
                "results": {
                    "prepare_accepted": _event_result(prepare_event),
                    "preview_ready": _event_result(ready_event),
                    "take_accepted": _event_result(take_accepted),
                    "premature_prepare": _event_result(premature_event),
                    "take_committed": _event_result(committed),
                    "promoted_lane_prepare": _event_result(promoted_event),
                    "postcommit_prepare": _event_result(post_prepare_event),
                    "postcommit_preview_ready": _event_result(post_ready_event),
                },
                "frames": {
                    "preview_ready": asdict(preview_frame),
                    "commit": asdict(commit_frame),
                    "postcommit_preview_ready": asdict(post_ready_frame),
                    "program_hashes_before": commit_program_hashes,
                    "program_hashes_after_preview_mutation": after_program_hashes,
                },
                "route_after_commit": after_commit,
                "route_after_preview_mutation": after_preview_mutation,
            }
        )

    if len([event for event in machine.events if event["event_type"] == "TakeCommitted"]) != cycles:
        raise AssertionError("lifecycle campaign emitted an unexpected commit count")
    return {
        "campaign": CAMPAIGN_NAME,
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "adr_revision": ADR_REVISION,
        "base_revision": BASE_REVISION,
        "runtime_instance_id": RUNTIME_ID,
        "cycles": cycles,
        "counts": counts,
        "surface_identity": asdict(oracle.surfaces),
        "final_snapshot": oracle.snapshot(),
        "records": records,
    }


def _attempt_record(group: str, ordinal: int, command: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "group": group,
        "ordinal": ordinal,
        "command_id": command["command_id"],
        "intent_id": command["intent_id"],
        "command_type": command["command_type"],
        "event_type": event["event_type"],
        "error_code": event.get("error_code"),
        "server_seq": event["server_seq"],
        "payload_sha256": event["payload_sha256"],
        "role_map": deepcopy(event["role_map"]),
        "revisions": deepcopy(event["revisions"]),
    }


def run_attempt_campaign(attempts: int = DEFAULT_ATTEMPTS) -> dict[str, Any]:
    """Run >=1,000 deterministic duplicate/concurrent/stale attempts."""

    if attempts < 1000:
        raise ValueError("issue #247 requires at least 1,000 controlled attempts")
    # Keep the matrix divisible into four equal, independently auditable
    # classes.  The default is exactly 1,024; larger values remain deterministic
    # by putting the remainder in the stale class.
    width = attempts // 4
    remainder = attempts - width * 4
    machine = SceneSwitchMachine(
        f"{RUNTIME_ID}-attempts",
        initial_lane_scenes={"A": "scene-on-air-attempts", "B": "scene-preview-attempts"},
    )
    oracle = LaneOracle(machine)
    prepare = _command(
        "Prepare",
        "attempt-prepare",
        "attempt-intent-prepare",
        machine,
        target={"lane_id": "B", "scene_id": "scene-preview-attempts"},
        timeout_ms=2000,
    )
    _assert_event(machine.dispatch(prepare, now_monotonic_ns=1_000_000), "PrepareAccepted")
    _assert_event(
        machine.mark_preview_ready("attempt-prepare", 4000, 40_000_000, now_monotonic_ns=2_000_000),
        "PreviewReady",
    )

    take = _command(
        "Take",
        "attempt-take",
        # A Take must carry the preparation's stable intent_id; retries keep
        # this value even when command_id is reused or raced.
        "attempt-intent-prepare",
        machine,
        prepared_command_id="attempt-prepare",
        timeout_ms=1000,
    )
    duplicate_take_results = _run_concurrently(
        [lambda take=deepcopy(take): machine.dispatch(take, now_monotonic_ns=3_000_000)] * width
    )
    take_events = [_assert_event(event, "TakeAccepted") for event in duplicate_take_results]
    if any(event != take_events[0] for event in take_events):
        raise AssertionError("identical concurrent Take retries did not replay byte-identically")
    take_matrix = [_attempt_record("duplicate_take_concurrent", i, take, event) for i, event in enumerate(take_events)]

    premature = _command(
        "Prepare",
        "attempt-premature",
        "attempt-intent-premature",
        machine,
        target={"lane_id": "A", "scene_id": "scene-premature-must-not-leak"},
        timeout_ms=2000,
    )
    duplicate_premature_results = _run_concurrently(
        [lambda premature=deepcopy(premature): machine.dispatch(premature, now_monotonic_ns=4_000_000)] * width
    )
    premature_events = [_assert_event(event, "CommandRejected") for event in duplicate_premature_results]
    if any(event.get("error_code") != "PREVIEW_FROZEN" for event in premature_events):
        raise AssertionError("pre-commit duplicate mutations were not all PREVIEW_FROZEN")
    if any(event != premature_events[0] for event in premature_events):
        raise AssertionError("identical concurrent pre-commit retries did not replay byte-identically")
    premature_matrix = [
        _attempt_record("duplicate_premature_concurrent", i, premature, event)
        for i, event in enumerate(premature_events)
    ]

    stale_matrix: list[dict[str, Any]] = []
    stale_count = width + remainder
    stale_revisions = {"program": 0, "preview": 0, "role_map": 0}
    for index in range(stale_count):
        stale = {
            **_command(
                "Take",
                f"attempt-stale-{index:04d}",
                f"attempt-stale-intent-{index:04d}",
                machine,
                prepared_command_id="attempt-prepare",
                timeout_ms=1000,
            ),
            "expected_revisions": stale_revisions,
            "expected_server_seq": machine.server_seq,
        }
        stale_event = _assert_event(
            machine.dispatch(stale, now_monotonic_ns=5_000_000 + index),
            "CommandRejected",
        )
        if stale_event.get("error_code") != "REVISION_STALE":
            raise AssertionError(f"stale command was not rejected: {stale_event}")
        stale_matrix.append(_attempt_record("stale_revision", index, stale, stale_event))

    conflict = deepcopy(take)
    conflict["timeout_ms"] = 999
    conflict_before = oracle.snapshot()
    # A conflicting payload is intentionally not treated as an idempotent
    # replay: the contract rejects each independently received divergent
    # payload and emits a fresh diagnostic event.  Keep this class sequential
    # so its server-sequence evidence is deterministic; the duplicate classes
    # above already exercise concurrent dispatch.
    conflict_results = [
        machine.dispatch(deepcopy(conflict), now_monotonic_ns=6_000_000 + index)
        for index in range(width)
    ]
    conflict_events = [_assert_event(event, "CommandRejected") for event in conflict_results]
    if any(event.get("error_code") != "IDEMPOTENCY_CONFLICT" for event in conflict_events):
        raise AssertionError("conflicting command-id reuse was not rejected")
    if _mutable_state(oracle.snapshot()) != _mutable_state(conflict_before):
        raise AssertionError("conflicting command-id reuse mutated route state")
    conflict_matrix = [_attempt_record("conflicting_command_reuse", i, conflict, event) for i, event in enumerate(conflict_events)]

    matrix = take_matrix + premature_matrix + stale_matrix + conflict_matrix
    if len(matrix) != attempts:
        raise AssertionError(f"attempt matrix has {len(matrix)} rows, expected {attempts}")

    # Commit callbacks are deliberately separate from the command-attempt
    # count.  They model multiple frame-boundary notifications racing after a
    # single TakeAccepted.  The reference machine must publish one commit and
    # replay it for every duplicate callback, regardless of supplied frame.
    callback_count = max(32, min(64, attempts // 8))
    callback_results = _run_concurrently(
        [
            lambda index=index: machine.commit_take(
                "attempt-take",
                # Every callback carries the same frame-boundary witness.  A
                # different callback may win the scheduler race, but the
                # observable result remains byte-identical and the evidence
                # stays reproducible across hosts.
                5000,
                50_000_000,
                now_monotonic_ns=7_000_000,
            )
            for index in range(callback_count)
        ]
    )
    callback_events = [_assert_event(event, "TakeCommitted") for event in callback_results]
    if any(event != callback_events[0] for event in callback_events):
        raise AssertionError("duplicate commit callbacks did not replay the sole commit")
    commits = [event for event in machine.events if event["event_type"] == "TakeCommitted"]
    if len(commits) != 1:
        raise AssertionError(f"expected one TakeCommitted, got {len(commits)}")
    if machine.revisions != {"program": 1, "preview": 1, "role_map": 1}:
        raise AssertionError(f"duplicate callbacks changed revisions more than once: {machine.revisions}")
    if oracle.snapshot()["role_map"] != {"on_air": "B", "preview": "A"}:
        raise AssertionError("duplicate callbacks changed the role map more than once")

    group_counts = {
        "duplicate_take_concurrent": len(take_matrix),
        "duplicate_premature_concurrent": len(premature_matrix),
        "stale_revision": len(stale_matrix),
        "conflicting_command_reuse": len(conflict_matrix),
    }
    result_counts: dict[str, int] = {}
    for row in matrix:
        key = row["error_code"] or row["event_type"]
        result_counts[key] = result_counts.get(key, 0) + 1
    return {
        "campaign": CAMPAIGN_NAME,
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "adr_revision": ADR_REVISION,
        "base_revision": BASE_REVISION,
        "runtime_instance_id": f"{RUNTIME_ID}-attempts",
        "attempts": attempts,
        "group_counts": group_counts,
        "result_counts": result_counts,
        "command_result_matrix": matrix,
        "commit_callback_attempts": callback_count,
        "commit_callback_result": _event_result(callback_events[0]),
        "committed_event_count": len(commits),
        "final_snapshot": oracle.snapshot(),
    }


def _run_concurrent_take_race() -> dict[str, Any]:
    """Race two distinct Take intents and prove only one can be accepted.

    The 1,000-row attempt matrix deliberately spends its budget on duplicate,
    stale, and conflicting command classes.  This smaller independent race
    covers the separate ``double Take`` acceptance criterion: two different
    command IDs present the same compare-and-swap snapshot concurrently.  The
    second arrival must be rejected by the server-sequence guard, and only the
    accepted command may commit.

    The summary intentionally omits the scheduler-selected winner ID and
    event sequence so the evidence remains deterministic while still proving
    the cardinality, error class, route, and revision invariants.
    """

    machine = SceneSwitchMachine(
        f"{RUNTIME_ID}-double-take",
        initial_lane_scenes={"A": "scene-double-take-on-air", "B": "scene-double-take-preview"},
    )
    prepare = _command(
        "Prepare",
        "double-take-prepare",
        "double-take-preparation",
        machine,
        target={"lane_id": "B", "scene_id": "scene-double-take-preview"},
        timeout_ms=2000,
    )
    _assert_event(machine.dispatch(prepare, now_monotonic_ns=1_000_000), "PrepareAccepted")
    _assert_event(
        machine.mark_preview_ready(
            "double-take-prepare", 6000, 60_000_000, now_monotonic_ns=2_000_000
        ),
        "PreviewReady",
    )
    take_a = _command(
        "Take",
        "double-take-a",
        "double-take-preparation",
        machine,
        prepared_command_id="double-take-prepare",
        timeout_ms=1000,
    )
    take_b = _command(
        "Take",
        "double-take-b",
        # Both commands refer to the same prepared intent; only their command
        # IDs differ.  That is the valid duplicate/race shape for v1 because
        # a Take intent is bound to its one Preview preparation.
        "double-take-preparation",
        machine,
        prepared_command_id="double-take-prepare",
        timeout_ms=1000,
    )
    results = _run_concurrently(
        [
            lambda take=deepcopy(take_a): machine.dispatch(take, now_monotonic_ns=3_000_000),
            lambda take=deepcopy(take_b): machine.dispatch(take, now_monotonic_ns=3_000_000),
        ]
    )
    accepted = [_assert_event(event, "TakeAccepted") for event in results if event["event_type"] == "TakeAccepted"]
    rejected = [
        _assert_event(event, "CommandRejected")
        for event in results
        if event["event_type"] == "CommandRejected"
    ]
    if len(accepted) != 1 or len(rejected) != 1:
        raise AssertionError(f"double Take race cardinality was not 1 accepted/1 rejected: {results}")
    if rejected[0].get("error_code") != "SERVER_SEQ_STALE":
        raise AssertionError(f"double Take race did not reject the loser as stale: {rejected[0]}")

    winner_id = accepted[0]["command_id"]
    committed = _assert_event(
        machine.commit_take(winner_id, 6001, 60_016_667, now_monotonic_ns=4_000_000),
        "TakeCommitted",
    )
    if len([event for event in machine.events if event["event_type"] == "TakeCommitted"]) != 1:
        raise AssertionError("double Take race produced more than one commit")
    if committed["revisions"] != {"program": 1, "preview": 1, "role_map": 1}:
        raise AssertionError(f"double Take race changed revisions unexpectedly: {committed}")
    if machine.role_map != {"on_air": "B", "preview": "A"}:
        raise AssertionError(f"double Take race changed the wrong role mapping: {machine.role_map}")
    return {
        "command_attempts": 2,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "rejection_codes": [rejected[0]["error_code"]],
        "commit_count": len([event for event in machine.events if event["event_type"] == "TakeCommitted"]),
        "final_role_map": machine.role_map,
        "final_revisions": machine.revisions,
    }


def run_campaigns(cycles: int = DEFAULT_CYCLES, attempts: int = DEFAULT_ATTEMPTS) -> dict[str, Any]:
    lifecycle = run_lifecycle_campaign(cycles)
    attempts_report = run_attempt_campaign(attempts)
    return {
        "campaign": CAMPAIGN_NAME,
        "issue": 247,
        "adr_revision": ADR_REVISION,
        "base_revision": BASE_REVISION,
        "requirements": {
            "minimum_lifecycle_cycles": 100,
            "minimum_controlled_attempts": 1000,
            "required_categories": [
                "preview_mutation_pre_commit",
                "preview_mutation_post_commit",
                "double_take",
                "concurrent_takes",
                "identical_retry",
                "conflicting_command_reuse",
                "stale_revisions",
                "abort_mapping_preservation",
                "concurrent_takes",
                "exactly_one_commit_per_intent",
            ],
        },
        "lifecycle": lifecycle,
        "attempts": attempts_report,
        "concurrent_take_race": _run_concurrent_take_race(),
    }


def _run_abort_checks() -> dict[str, Any]:
    """Exercise all abort causes and prove mapping/revisions stay unchanged."""

    reports: list[dict[str, Any]] = []
    for index, (reason, mode) in enumerate(
        (("operator", "explicit"), ("shutdown", "explicit"), ("superseded", "explicit"), ("timeout", "poll")),
        start=1,
    ):
        machine = SceneSwitchMachine(
            f"{RUNTIME_ID}-abort-{index}",
            initial_lane_scenes={"A": "scene-abort-on-air", "B": "scene-abort-preview"},
        )
        prepare = _command(
            "Prepare",
            "abort-prepare",
            "abort-intent",
            machine,
            target={"lane_id": "B", "scene_id": "scene-abort-preview"},
            timeout_ms=1,
        )
        _assert_event(machine.dispatch(prepare, now_monotonic_ns=1_000_000), "PrepareAccepted")
        _assert_event(
            machine.mark_preview_ready("abort-prepare", 7000, 70_000_000, now_monotonic_ns=1_100_000),
            "PreviewReady",
        )
        take = _command(
            "Take",
            "abort-take",
            "abort-intent",
            machine,
            prepared_command_id="abort-prepare",
            timeout_ms=1,
        )
        _assert_event(machine.dispatch(take, now_monotonic_ns=1_200_000), "TakeAccepted")
        before = machine.snapshot()
        if mode == "poll":
            event = machine.poll(now_monotonic_ns=3_000_000)
            assert event is not None
        else:
            abort = _command(
                "Abort",
                f"abort-command-{index}",
                "abort-intent",
                machine,
                take_command_id="abort-take",
                reason=reason,
            )
            event = machine.dispatch(abort, now_monotonic_ns=1_300_000)
        aborted = _assert_event(event, "TakeAborted")
        if aborted["reason"] != reason:
            raise AssertionError(f"abort reason changed: expected {reason}, got {aborted}")
        after = machine.snapshot()
        if after["role_map"] != before["role_map"]:
            raise AssertionError("abort changed the current role mapping")
        if after["revisions"] != before["revisions"]:
            raise AssertionError("abort changed route revisions")
        reports.append({"reason": reason, "mode": mode, "result": _event_result(aborted), "before": before, "after": after})
    return {"cases": reports, "count": len(reports)}


def build_report(cycles: int = DEFAULT_CYCLES, attempts: int = DEFAULT_ATTEMPTS) -> dict[str, Any]:
    report = run_campaigns(cycles, attempts)
    report["abort_mapping_preservation"] = _run_abort_checks()
    return report


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.cycles < 100:
        parser.error("--cycles must be >= 100")
    if args.attempts < 1000:
        parser.error("--attempts must be >= 1000")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args.cycles, args.attempts)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
        print(f"PASS: issue #247 evidence written to {args.output}")
    else:
        print(encoded, end="")
    print(
        f"PASS: lifecycle_cycles={report['lifecycle']['cycles']} "
        f"controlled_attempts={report['attempts']['attempts']} "
        f"commits={report['attempts']['committed_event_count']} abort_cases={report['abort_mapping_preservation']['count']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the evidence command
    raise SystemExit(main())
