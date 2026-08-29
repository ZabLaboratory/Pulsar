"""Execute the normative Draft 2020-12 schema against contract examples."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator


HERE = Path(__file__).parent
SCHEMA = json.loads((HERE / "schema.json").read_text(encoding="utf-8"))
EXAMPLES = json.loads((HERE / "fixtures" / "examples.json").read_text(encoding="utf-8"))


def test_examples_are_valid_under_the_normative_draft202012_schema() -> None:
    validator = Draft202012Validator(SCHEMA)
    for payload in EXAMPLES["commands"] + EXAMPLES["events"]:
        assert not list(validator.iter_errors(payload)), payload


def test_abort_queue_rejected_requires_both_last_committed_observations() -> None:
    # The runtime applies this extra lifecycle guard in addition to the schema
    # shape so a queue rejection can never fabricate a physical boundary.
    command = {
        "contract": "pulsar.scene-switch.v1", "schema_version": 1,
        "message_type": "command", "command_type": "Abort",
        "command_id": "abort-queue", "intent_id": "intent-queue",
        "runtime_instance_id": "runtime-queue",
        "expected_revisions": {"program": 0, "preview": 0, "role_map": 0},
        "take_command_id": "take-queue", "reason": "queue_rejected",
        "last_committed_frame_id": 12, "last_committed_pts_ns": 34,
    }
    validator = Draft202012Validator(SCHEMA)
    assert not list(validator.iter_errors(command))
