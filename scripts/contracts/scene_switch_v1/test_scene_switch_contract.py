"""Executable contract tests for ``pulsar.scene-switch.v1``.

These tests intentionally exercise the reference state machine rather than a
fake OBS implementation.  The contract's job is to make the future producer
and consumers agree on ordering, correlation, and failure semantics before
the physical dual-lane implementation lands.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from . import (
    CONTRACT,
    SCHEMA_VERSION,
    SceneSwitchMachine,
    SceneSwitchStateError,
    SceneSwitchValidationError,
    payload_sha256,
    validate_command,
    validate_event,
)


_HERE = Path(__file__).resolve().parent


def command(
    command_type: str,
    command_id: str,
    intent_id: str,
    revisions: dict[str, int],
    server_seq: int,
    **fields: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "message_type": "command",
        "command_type": command_type,
        "command_id": command_id,
        "intent_id": intent_id,
        "runtime_instance_id": "runtime-001",
        "expected_revisions": revisions,
        "expected_server_seq": server_seq,
    }
    value.update(fields)
    return value


def prepare_command(
    command_id: str = "prepare-001",
    *,
    revisions: dict[str, int] | None = None,
    server_seq: int = 0,
    lane_id: str = "B",
    scene_id: str = "scene-lower-third",
    intent_id: str = "intent-001",
    timeout_ms: int = 2000,
) -> dict[str, Any]:
    return command(
        "Prepare",
        command_id,
        intent_id,
        revisions or {"program": 0, "preview": 0, "role_map": 0},
        server_seq,
        target={"lane_id": lane_id, "scene_id": scene_id},
        timeout_ms=timeout_ms,
    )


def take_command(
    command_id: str = "take-001",
    *,
    revisions: dict[str, int] | None = None,
    server_seq: int = 2,
    prepared_command_id: str = "prepare-001",
    intent_id: str = "intent-001",
    timeout_ms: int = 1000,
) -> dict[str, Any]:
    return command(
        "Take",
        command_id,
        intent_id,
        revisions or {"program": 0, "preview": 1, "role_map": 0},
        server_seq,
        prepared_command_id=prepared_command_id,
        timeout_ms=timeout_ms,
    )


def abort_command(
    command_id: str = "abort-001",
    *,
    revisions: dict[str, int] | None = None,
    server_seq: int = 3,
    take_command_id: str = "take-001",
    intent_id: str = "intent-001",
    reason: str = "operator",
) -> dict[str, Any]:
    return command(
        "Abort",
        command_id,
        intent_id,
        revisions or {"program": 0, "preview": 1, "role_map": 0},
        server_seq,
        take_command_id=take_command_id,
        reason=reason,
    )


def ready_machine() -> SceneSwitchMachine:
    machine = SceneSwitchMachine("runtime-001", initial_lane_scenes={"A": "scene-live"})
    assert machine.dispatch(prepare_command(), now_monotonic_ns=1_000_000)["event_type"] == "PrepareAccepted"
    assert machine.mark_preview_ready("prepare-001", 500, 8_316_666_666, now_monotonic_ns=1_100_000)["event_type"] == "PreviewReady"
    return machine


def test_schema_and_examples_are_versioned_and_validate() -> None:
    schema = json.loads((_HERE / "schema.json").read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$defs"]["commandCommon"]["properties"]["contract"]["const"] == CONTRACT
    assert schema["$defs"]["commandCommon"]["properties"]["schema_version"]["const"] == SCHEMA_VERSION

    examples = json.loads((_HERE / "fixtures" / "examples.json").read_text(encoding="utf-8"))
    assert examples["contract"] == CONTRACT
    for item in examples["commands"]:
        validated = validate_command(item)
        assert payload_sha256(validated) == examples["payload_sha256"][item["command_id"]]
    for item in examples["events"]:
        assert validate_event(item) == item


@pytest.mark.parametrize(
    "mutator",
    [
        lambda c: c.update(unexpected=True),
        lambda c: c.update(timeout_ms=True),
        lambda c: c["expected_revisions"].update(program=1.5),
        lambda c: c["expected_revisions"].update(preview=True),
        lambda c: c["target"].update(lane_id="C"),
        lambda c: c.update(schema_version=2),
        lambda c: c.update(command_id="bad id"),
    ],
    ids=["unknown-field", "bool-timeout", "float-revision", "bool-revision", "unknown-lane", "unknown-version", "bad-id"],
)
def test_command_validation_is_strict(mutator: Any) -> None:
    value = prepare_command()
    mutator(value)
    with pytest.raises(SceneSwitchValidationError) as exc:
        validate_command(value)
    assert exc.value.code == "SCHEMA_INVALID"


def test_lifecycle_carries_correlation_revisions_and_commit_frame() -> None:
    machine = SceneSwitchMachine("runtime-001", initial_lane_scenes={"A": "scene-live"})
    accepted = machine.dispatch(prepare_command(), now_monotonic_ns=1_000_000)
    assert accepted["event_type"] == "PrepareAccepted"
    assert accepted["previous_revisions"] == {"program": 0, "preview": 0, "role_map": 0}
    assert accepted["revisions"] == {"program": 0, "preview": 1, "role_map": 0}
    assert accepted["server_seq"] == 1
    assert accepted["runtime_instance_id"] == "runtime-001"

    ready = machine.mark_preview_ready("prepare-001", 500, 8_316_666_666, now_monotonic_ns=1_100_000)
    assert ready["event_type"] == "PreviewReady"
    assert ready["first_frame_id"] == 500
    assert ready["first_pts_ns"] == 8_316_666_666

    take_accepted = machine.dispatch(take_command(), now_monotonic_ns=1_200_000)
    assert take_accepted["event_type"] == "TakeAccepted"
    assert take_accepted["state"] == "take_accepted"
    assert take_accepted["role_map"] == {"on_air": "A", "preview": "B"}

    committed = machine.commit_take("take-001", 501, 8_333_333_333, now_monotonic_ns=1_300_000)
    assert committed["event_type"] == "TakeCommitted"
    assert committed["frame_id"] == 501
    assert committed["pts_ns"] == 8_333_333_333
    assert committed["previous_revisions"] == {"program": 0, "preview": 1, "role_map": 0}
    assert committed["revisions"] == {"program": 1, "preview": 1, "role_map": 1}
    assert committed["previous_role_map"] == {"on_air": "A", "preview": "B"}
    assert committed["role_map"] == {"on_air": "B", "preview": "A"}
    assert committed["program_lane_id"] == "B"
    assert committed["preview_lane_id"] == "A"
    assert [e["server_seq"] for e in machine.events] == [1, 2, 3, 4]


def test_preview_is_frozen_until_commit_and_old_on_air_becomes_new_preview_afterward() -> None:
    machine = ready_machine()
    machine.dispatch(take_command(), now_monotonic_ns=1_200_000)
    before = machine.snapshot()

    with pytest.raises(SceneSwitchStateError) as ready_again:
        machine.mark_preview_ready("prepare-001", 999, 9_000_000_000, now_monotonic_ns=1_250_000)
    assert ready_again.value.code == "PREVIEW_FROZEN"

    premature_prepare = prepare_command(
        "prepare-too-soon",
        revisions=before["revisions"],
        server_seq=machine.server_seq,
        lane_id="A",
        scene_id="scene-that-must-not-leak",
    )
    rejected = machine.dispatch(premature_prepare, now_monotonic_ns=1_250_000)
    assert rejected["event_type"] == "CommandRejected"
    assert rejected["error_code"] == "PREVIEW_FROZEN"
    after = machine.snapshot()
    assert after["role_map"] == before["role_map"]
    assert after["revisions"] == before["revisions"]
    assert after["lane_scenes"] == before["lane_scenes"]

    machine.commit_take("take-001", 501, 8_333_333_333, now_monotonic_ns=1_300_000)
    next_preview = prepare_command(
        "prepare-after-commit",
        revisions=machine.revisions,
        server_seq=machine.server_seq,
        lane_id="A",
        scene_id="scene-next-preview",
    )
    accepted = machine.dispatch(next_preview, now_monotonic_ns=1_400_000)
    assert accepted["event_type"] == "PrepareAccepted"
    assert machine.snapshot()["role_map"] == {"on_air": "B", "preview": "A"}
    assert machine.snapshot()["lane_scenes"]["A"] == "scene-next-preview"
    assert machine.snapshot()["lane_scenes"]["B"] == "scene-lower-third"


def test_duplicate_command_replays_original_without_second_commit_or_sequence() -> None:
    machine = ready_machine()
    take = take_command()
    accepted = machine.dispatch(take, now_monotonic_ns=1_200_000)
    duplicate = machine.dispatch(deepcopy(take), now_monotonic_ns=1_250_000)
    assert duplicate == accepted
    assert machine.server_seq == 3

    committed = machine.commit_take("take-001", 501, 8_333_333_333, now_monotonic_ns=1_300_000)
    event_count = len(machine.events)
    replayed_commit = machine.commit_take("take-001", 999, 9_999_999_999, now_monotonic_ns=1_400_000)
    assert replayed_commit == committed
    assert machine.server_seq == 4
    assert len(machine.events) == event_count
    assert machine.revisions == {"program": 1, "preview": 1, "role_map": 1}


def test_command_id_payload_mismatch_is_conflict_and_original_stays_authoritative() -> None:
    machine = SceneSwitchMachine("runtime-001")
    original = prepare_command()
    accepted = machine.dispatch(original, now_monotonic_ns=1_000_000)
    conflict = prepare_command(scene_id="scene-other")
    rejected = machine.dispatch(conflict, now_monotonic_ns=1_100_000)
    assert rejected["event_type"] == "CommandRejected"
    assert rejected["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert rejected["error_details"]["original_payload_sha256"] == payload_sha256(original)
    assert rejected["error_details"]["received_payload_sha256"] == payload_sha256(conflict)
    assert machine.snapshot()["revisions"] == accepted["revisions"]
    assert machine.snapshot()["lane_scenes"] == {"B": "scene-lower-third"}
    assert machine.dispatch(original, now_monotonic_ns=1_200_000) == accepted


def test_stale_revisions_are_rejected_without_route_or_surface_mutation() -> None:
    machine = ready_machine()
    before = machine.snapshot()
    stale = take_command(
        "take-stale",
        revisions={"program": 0, "preview": 0, "role_map": 0},
        server_seq=1,
    )
    rejected = machine.dispatch(stale, now_monotonic_ns=1_200_000)
    assert rejected["error_code"] == "REVISION_STALE"
    after = machine.snapshot()
    for key in ("state", "revisions", "role_map", "lane_scenes", "pending_prepare_command_id", "pending_take_command_id"):
        assert after[key] == before[key]
    assert rejected["previous_revisions"] == rejected["revisions"] == before["revisions"]
    assert machine.dispatch(deepcopy(stale), now_monotonic_ns=1_300_000) == rejected


def test_server_sequence_guard_is_stable_and_non_mutating() -> None:
    machine = SceneSwitchMachine("runtime-001")
    machine.dispatch(prepare_command(), now_monotonic_ns=1_000_000)
    before = machine.snapshot()
    stale_seq = prepare_command(
        "prepare-seq-stale",
        revisions=before["revisions"],
        server_seq=0,
    )
    rejected = machine.dispatch(stale_seq, now_monotonic_ns=1_100_000)
    assert rejected["error_code"] == "SERVER_SEQ_STALE"
    after = machine.snapshot()
    assert after["revisions"] == before["revisions"]
    assert after["role_map"] == before["role_map"]
    assert after["lane_scenes"] == before["lane_scenes"]


def test_timeout_and_abort_preserve_mapping_and_prevent_late_commit() -> None:
    machine = ready_machine()
    take = take_command(timeout_ms=1)
    machine.dispatch(take, now_monotonic_ns=2_000_000)
    before = machine.snapshot()
    expired = machine.poll(now_monotonic_ns=3_000_000)
    assert expired is not None
    assert expired["event_type"] == "TakeAborted"
    assert expired["reason"] == "timeout"
    assert machine.snapshot()["role_map"] == before["role_map"]
    assert machine.snapshot()["revisions"] == before["revisions"]
    assert machine.commit_take("take-001", 501, 8_333_333_333, now_monotonic_ns=4_000_000) == expired
    assert machine.snapshot()["role_map"] == {"on_air": "A", "preview": "B"}

    # A second machine covers the explicit operator abort path.
    machine = ready_machine()
    machine.dispatch(take_command(), now_monotonic_ns=1_200_000)
    before = machine.snapshot()
    aborted = machine.dispatch(abort_command(), now_monotonic_ns=1_250_000)
    assert aborted["event_type"] == "TakeAborted"
    assert aborted["reason"] == "operator"
    assert machine.snapshot()["role_map"] == before["role_map"]
    assert machine.snapshot()["revisions"] == before["revisions"]
    assert machine.commit_take("take-001", 501, 8_333_333_333, now_monotonic_ns=1_300_000) == aborted


def test_concurrent_takes_cannot_both_commit() -> None:
    machine = ready_machine()
    first = machine.dispatch(take_command(), now_monotonic_ns=1_200_000)
    second = take_command(
        "take-002",
        revisions={"program": 0, "preview": 1, "role_map": 0},
        server_seq=first["server_seq"],
    )
    rejected = machine.dispatch(second, now_monotonic_ns=1_210_000)
    assert rejected["event_type"] == "CommandRejected"
    assert rejected["error_code"] == "PREVIEW_NOT_READY"
    committed = machine.commit_take("take-001", 501, 8_333_333_333, now_monotonic_ns=1_300_000)
    assert committed["event_type"] == "TakeCommitted"
    assert machine.revisions["program"] == 1
    assert not any(e.get("take_command_id") == "take-002" and e["event_type"] == "TakeCommitted" for e in machine.events)


def test_invalid_internal_commit_reference_is_typed_and_non_mutating() -> None:
    machine = ready_machine()
    with pytest.raises(SceneSwitchStateError) as exc:
        machine.commit_take("unknown-take", 1, 1, now_monotonic_ns=1_200_000)
    assert exc.value.code == "TAKE_NOT_PENDING"
    assert machine.snapshot()["state"] == "preview_ready"


def test_event_validation_rejects_backwards_revisions_and_incomplete_commit() -> None:
    machine = ready_machine()
    machine.dispatch(take_command(), now_monotonic_ns=1_200_000)
    committed = machine.commit_take("take-001", 501, 8_333_333_333, now_monotonic_ns=1_300_000)
    backwards = deepcopy(committed)
    backwards["previous_revisions"]["program"] = 2
    with pytest.raises(SceneSwitchValidationError):
        validate_event(backwards)

    incomplete = deepcopy(committed)
    del incomplete["frame_id"]
    with pytest.raises(SceneSwitchValidationError):
        validate_event(incomplete)
