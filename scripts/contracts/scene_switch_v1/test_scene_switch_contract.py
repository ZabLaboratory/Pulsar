"""Executable contract tests for ``pulsar.scene-switch.v1``.

These tests intentionally exercise the reference state machine rather than a
fake OBS implementation.  The contract's job is to make the future producer
and consumers agree on ordering, correlation, and failure semantics before
the physical dual-lane implementation lands.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import json
from pathlib import Path
import threading
from typing import Any, cast

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


def _run_concurrently(calls: list[Callable[[], dict[str, Any]]]) -> list[dict[str, Any]]:
    """Start all calls together and fail deterministically on thread errors."""

    start = threading.Barrier(len(calls) + 1)
    results: list[dict[str, Any] | None] = [None] * len(calls)
    errors: list[BaseException | None] = [None] * len(calls)

    def worker(index: int, call: Callable[[], dict[str, Any]]) -> None:
        try:
            start.wait(timeout=5)
            results[index] = call()
        except BaseException as exc:  # pragma: no cover - asserted by the caller
            errors[index] = exc

    threads = [threading.Thread(target=worker, args=(index, call)) for index, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive(), "concurrent contract call did not complete"
    assert errors == [None] * len(calls), errors
    assert all(result is not None for result in results)
    return [result for result in results if result is not None]


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
        lambda c: c.update(schema_version=True),
        lambda c: c.update(schema_version=2),
        lambda c: c.update(command_id="bad id"),
    ],
    ids=[
        "unknown-field",
        "bool-timeout",
        "float-revision",
        "bool-revision",
        "unknown-lane",
        "bool-schema-version",
        "unknown-version",
        "bad-id",
    ],
)
def test_command_validation_is_strict(mutator: Any) -> None:
    value = prepare_command()
    mutator(value)
    with pytest.raises(SceneSwitchValidationError) as exc:
        validate_command(value)
    assert exc.value.code == "SCHEMA_INVALID"


def test_identifiers_obey_schema_length_and_strict_end_anchor() -> None:
    for field in ("command_id", "intent_id", "runtime_instance_id"):
        too_long = prepare_command()
        too_long[field] = "a" * 129
        with pytest.raises(SceneSwitchValidationError) as length_error:
            validate_command(too_long)
        assert length_error.value.code == "SCHEMA_INVALID"

        trailing_newline = prepare_command()
        trailing_newline[field] = "valid-id\n"
        with pytest.raises(SceneSwitchValidationError) as newline_error:
            validate_command(trailing_newline)
        assert newline_error.value.code == "SCHEMA_INVALID"


def test_integral_json_numbers_are_normalized_and_draft_compatible() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((_HERE / "schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    value = prepare_command(timeout_ms=1)
    value["schema_version"] = 1.0
    value["expected_server_seq"] = 0.0
    value["expected_revisions"] = {key: 0.0 for key in ("program", "preview", "role_map")}
    value["timeout_ms"] = 1.0
    validator.validate(value)

    normalized = validate_command(value)
    assert normalized["schema_version"] == 1
    assert type(normalized["schema_version"]) is int
    assert normalized["expected_server_seq"] == 0
    assert type(normalized["timeout_ms"]) is int
    validator.validate(normalized)

    machine = SceneSwitchMachine("runtime-001")
    # Keep the JSON-decoded integral float at runtime; the cast only documents
    # this deliberate schema-boundary case to mypy without changing production.
    accepted = machine.dispatch(value, now_monotonic_ns=cast(int, 1.0))
    validator.validate(accepted)
    assert type(accepted["server_seq"]) is int
    assert type(accepted["deadline_monotonic_ns"]) is int

    invalid = prepare_command(command_id="invalid-id\n")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)

    invalid_bool = prepare_command()
    invalid_bool["schema_version"] = True
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid_bool)

    invalid_length = prepare_command(command_id="a" * 129)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid_length)


def test_reference_outputs_validate_as_draft_2020_12_events() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((_HERE / "schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    machine = ready_machine()
    machine.dispatch(take_command(), now_monotonic_ns=1_200_000)
    machine.commit_take("take-001", 501, 8_333_333_333, now_monotonic_ns=1_300_000)
    for event in machine.events:
        validator.validate(event)

    timeout_machine = SceneSwitchMachine("runtime-001")
    timeout_machine.dispatch(prepare_command(timeout_ms=1), now_monotonic_ns=1_000_000)
    timeout_event = timeout_machine.poll(now_monotonic_ns=3_000_000)
    assert timeout_event is not None
    validator.validate(timeout_event)

    take_timeout_machine = ready_machine()
    take_timeout_machine.dispatch(take_command(timeout_ms=1), now_monotonic_ns=1_200_000)
    take_timeout_event = take_timeout_machine.poll(now_monotonic_ns=3_000_000)
    assert take_timeout_event is not None
    validator.validate(take_timeout_event)


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


def test_concurrent_prepare_commands_are_serialized_exactly_once() -> None:
    machine = SceneSwitchMachine("runtime-001")
    prepare_20 = prepare_command("prepare-20", scene_id="scene-20")
    prepare_10 = prepare_command("prepare-10", scene_id="scene-10")

    results = _run_concurrently(
        [
            lambda: machine.dispatch(prepare_20, now_monotonic_ns=1_000_000),
            lambda: machine.dispatch(prepare_10, now_monotonic_ns=1_000_000),
        ]
    )

    accepted = [result for result in results if result["event_type"] == "PrepareAccepted"]
    rejected = [result for result in results if result["event_type"] == "CommandRejected"]
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0]["error_code"] == "REVISION_STALE"
    assert [event["event_type"] for event in machine.events] == ["PrepareAccepted", "CommandRejected"]
    assert machine.server_seq == 2
    assert machine.revisions == {"program": 0, "preview": 1, "role_map": 0}
    assert machine.snapshot()["lane_scenes"]["B"] == accepted[0]["target_scene_id"]


def test_concurrent_commit_callbacks_are_serialized_exactly_once() -> None:
    machine = ready_machine()
    machine.dispatch(take_command(), now_monotonic_ns=1_200_000)

    results = _run_concurrently(
        [
            lambda: machine.commit_take("take-001", 20, 20_000, now_monotonic_ns=1_300_000),
            lambda: machine.commit_take("take-001", 10, 10_000, now_monotonic_ns=1_300_000),
        ]
    )

    commits = [event for event in machine.events if event["event_type"] == "TakeCommitted"]
    assert len(commits) == 1
    assert all(result == commits[0] for result in results)
    assert commits[0]["frame_id"] in {10, 20}
    assert machine.server_seq == 4
    assert machine.revisions == {"program": 1, "preview": 1, "role_map": 1}
    assert machine.role_map == {"on_air": "B", "preview": "A"}


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


def test_command_idempotency_is_scoped_by_runtime_instance_id() -> None:
    machine = SceneSwitchMachine("runtime-001")
    foreign = prepare_command("shared-command")
    foreign["runtime_instance_id"] = "runtime-002"
    foreign_rejected = machine.dispatch(foreign, now_monotonic_ns=1_000_000)
    assert foreign_rejected["error_code"] == "RUNTIME_MISMATCH"

    local = prepare_command("shared-command", server_seq=machine.server_seq)
    local_accepted = machine.dispatch(local, now_monotonic_ns=1_100_000)
    assert local_accepted["event_type"] == "PrepareAccepted"
    assert machine.revisions == {"program": 0, "preview": 1, "role_map": 0}
    assert machine.dispatch(deepcopy(foreign), now_monotonic_ns=1_200_000) == foreign_rejected


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


def test_prepare_timeout_is_polled_once_and_cannot_be_replaced_silently() -> None:
    machine = SceneSwitchMachine("runtime-001")
    prepare = prepare_command(timeout_ms=1)
    accepted = machine.dispatch(prepare, now_monotonic_ns=1_000_000)
    before_expiration = machine.snapshot()

    expired = machine.poll(now_monotonic_ns=3_000_000)
    assert expired is not None
    assert expired["event_type"] == "CommandRejected"
    assert expired["error_code"] == "TIMEOUT"
    assert expired["server_seq"] == accepted["server_seq"] + 1
    assert machine.poll(now_monotonic_ns=4_000_000) is None
    after_expiration = machine.snapshot()
    assert after_expiration["state"] == "ready"
    assert after_expiration["revisions"] == before_expiration["revisions"]
    assert after_expiration["role_map"] == before_expiration["role_map"]
    assert after_expiration["lane_scenes"] == before_expiration["lane_scenes"]
    # The command response remains the original PrepareAccepted result; the
    # timeout is a separate terminal lifecycle event retained for callbacks.
    assert machine.dispatch(deepcopy(prepare), now_monotonic_ns=5_000_000) == accepted

    assert machine.mark_preview_ready("prepare-001", 20, 20_000, now_monotonic_ns=5_000_000) == expired

    replacement = prepare_command(
        "prepare-after-timeout",
        revisions=machine.revisions,
        server_seq=machine.server_seq,
        scene_id="scene-explicit-replacement",
    )
    replacement_accepted = machine.dispatch(replacement, now_monotonic_ns=5_000_000)
    assert replacement_accepted["event_type"] == "PrepareAccepted"
    assert machine.snapshot()["lane_scenes"]["B"] == "scene-explicit-replacement"


def test_elapsed_prepare_expires_before_a_new_command_can_replace_it() -> None:
    machine = SceneSwitchMachine("runtime-001")
    accepted = machine.dispatch(prepare_command(timeout_ms=1), now_monotonic_ns=1_000_000)
    replacement = prepare_command(
        "prepare-after-expiry",
        revisions=accepted["revisions"],
        server_seq=accepted["server_seq"] + 1,
        scene_id="scene-explicit-replacement",
    )

    replacement_accepted = machine.dispatch(replacement, now_monotonic_ns=3_000_000)
    assert [event["event_type"] for event in machine.events] == [
        "PrepareAccepted",
        "CommandRejected",
        "PrepareAccepted",
    ]
    timeout = machine.events[1]
    assert timeout["error_code"] == "TIMEOUT"
    assert replacement_accepted["event_type"] == "PrepareAccepted"
    assert machine.snapshot()["lane_scenes"]["B"] == "scene-explicit-replacement"


def test_preview_ready_is_immutable_and_exact_retries_are_idempotent() -> None:
    machine = SceneSwitchMachine("runtime-001")
    machine.dispatch(prepare_command(), now_monotonic_ns=1_000_000)
    first = machine.mark_preview_ready("prepare-001", 20, 20_000, now_monotonic_ns=1_100_000)
    assert machine.poll(now_monotonic_ns=3_100_000_000) is None
    assert machine.state == "preview_ready"
    same = machine.mark_preview_ready("prepare-001", 20, 20_000, now_monotonic_ns=1_200_000)
    assert same == first
    assert machine.server_seq == 2
    assert len(machine.events) == 2

    with pytest.raises(SceneSwitchStateError) as divergent:
        machine.mark_preview_ready("prepare-001", 21, 21_000, now_monotonic_ns=1_300_000)
    assert divergent.value.code == "IDEMPOTENCY_CONFLICT"
    assert divergent.value.details["original_first_frame_id"] == 20
    assert divergent.value.details["received_first_frame_id"] == 21
    assert machine.server_seq == 2
    assert machine.events[-1] == first

    machine.dispatch(take_command(), now_monotonic_ns=1_400_000)
    assert machine.mark_preview_ready("prepare-001", 20, 20_000, now_monotonic_ns=1_500_000) == first


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
