"""Authoritative ``pulsar.scene-switch.v1`` contract.

This module deliberately has no OBS/libobs dependency.  It is the small
stateful reference model that the C++ vendor handler, clients, and probes can
use to agree on the wire shape and on the observable command semantics before
the dual-lane implementation is present.

The JSON schema next to this file is the wire-level shape.  The validator here
adds the semantic checks that JSON Schema alone cannot express conveniently
(revision keys, frame/PTS types, and the state-machine invariants).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
import threading
import time
from typing import Any, Final, Mapping


CONTRACT: Final = "pulsar.scene-switch.v1"
SCHEMA_VERSION: Final = 1
SCHEMA_FILE: Final = "schema.json"

REVISION_KEYS: Final = ("program", "preview", "role_map")
LANE_IDS: Final = ("A", "B")
ROLE_KEYS: Final = ("on_air", "preview")
ID_MAX_LENGTH: Final = 128
STATES: Final = ("ready", "preparing", "preview_ready", "take_accepted")
COMMAND_TYPES: Final = ("Prepare", "Take", "Abort")
EVENT_TYPES: Final = (
    "PrepareAccepted",
    "PreviewReady",
    "TakeAccepted",
    "TakeCommitted",
    "TakeAborted",
    "CommandRejected",
)
ABORT_REASONS: Final = ("operator", "timeout", "shutdown", "superseded", "queue_rejected")
ERROR_CODES: Final = (
    "SCHEMA_INVALID",
    "RUNTIME_MISMATCH",
    "REVISION_STALE",
    "SERVER_SEQ_STALE",
    "IDEMPOTENCY_CONFLICT",
    "PREVIEW_FROZEN",
    "PREVIEW_LANE_MISMATCH",
    "PREVIEW_NOT_READY",
    "PREPARE_NOT_FOUND",
    "TAKE_NOT_PENDING",
    "TAKE_INTENT_CONFLICT",
    "TIMEOUT",
    "ABORTED",
)

_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()


class SceneSwitchContractError(ValueError):
    """A stable contract error suitable for a vendor error response."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown scene-switch error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = deepcopy(dict(details or {}))


class SceneSwitchValidationError(SceneSwitchContractError):
    """The input is not a valid v1 command/event shape."""


class SceneSwitchStateError(SceneSwitchContractError):
    """An internal producer attempted an impossible state transition."""


def canonical_json(value: Mapping[str, Any]) -> bytes:
    """Return the canonical UTF-8 representation used for idempotency.

    Object key order is not significant, while numbers, strings and array
    order remain significant.  NaN and Infinity are intentionally rejected so
    two implementations cannot hash different non-JSON values.
    """

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SceneSwitchValidationError(
            "SCHEMA_INVALID",
            "scene-switch payload is not canonical JSON",
            {"reason": str(exc)},
        ) from exc
    return text.encode("utf-8")


def payload_sha256(value: Mapping[str, Any]) -> str:
    """Hash a validated command with stable sorted-key JSON semantics."""

    return hashlib.sha256(canonical_json(value)).hexdigest()


def _coerce_json_integer(value: Any) -> int | None:
    """Return the integer represented by a JSON number, or ``None``.

    Draft 2020-12 defines ``integer`` by numeric value, so a JSON number such
    as ``1.0`` is an integer instance even though Python's JSON decoder stores
    it as ``float``.  Normalize finite integral floats to ``int`` so the
    reference model and its emitted events use one representation.  ``bool``
    is deliberately excluded because it is a distinct JSON type despite
    Python's ``bool``/``int`` inheritance.
    """

    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SceneSwitchValidationError("SCHEMA_INVALID", f"{name} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, Any], required: set[str], allowed: set[str], name: str) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        raise SceneSwitchValidationError(
            "SCHEMA_INVALID", f"{name} is missing required fields", {"missing": missing}
        )
    if unknown:
        raise SceneSwitchValidationError(
            "SCHEMA_INVALID", f"{name} contains unknown fields", {"unknown": unknown}
        )


def _require_string(value: Any, name: str, *, max_length: int = 256, identifier: bool = False) -> str:
    length_limit = ID_MAX_LENGTH if identifier else max_length
    if not isinstance(value, str) or not value or len(value) > length_limit:
        raise SceneSwitchValidationError(
            "SCHEMA_INVALID", f"{name} must be a non-empty string of at most {length_limit} characters"
        )
    if identifier and _ID_RE.fullmatch(value) is None:
        raise SceneSwitchValidationError(
            "SCHEMA_INVALID", f"{name} contains characters outside the v1 identifier grammar"
        )
    return value


def _require_non_negative_int(value: Any, name: str, *, positive: bool = False) -> int:
    integer = _coerce_json_integer(value)
    if integer is None or integer < (1 if positive else 0):
        bound = "a positive integer" if positive else "a non-negative integer"
        raise SceneSwitchValidationError("SCHEMA_INVALID", f"{name} must be {bound}")
    return integer


def _require_schema_version(value: Any, name: str) -> int:
    version = _require_non_negative_int(value, name)
    if version != SCHEMA_VERSION:
        raise SceneSwitchValidationError("SCHEMA_INVALID", f"{name} must be {SCHEMA_VERSION}")
    return version


def _validate_revisions(value: Any, name: str) -> dict[str, int]:
    obj = _require_object(value, name)
    _require_exact_keys(obj, set(REVISION_KEYS), set(REVISION_KEYS), name)
    return {
        key: _require_non_negative_int(obj[key], f"{name}.{key}")
        for key in REVISION_KEYS
    }


def _validate_role_map(value: Any, name: str = "role_map") -> dict[str, str]:
    obj = _require_object(value, name)
    _require_exact_keys(obj, set(ROLE_KEYS), set(ROLE_KEYS), name)
    result: dict[str, str] = {}
    for key in ROLE_KEYS:
        lane = obj[key]
        if lane not in LANE_IDS:
            raise SceneSwitchValidationError("SCHEMA_INVALID", f"{name}.{key} must be 'A' or 'B'")
        result[key] = lane
    if result["on_air"] == result["preview"]:
        raise SceneSwitchValidationError("SCHEMA_INVALID", f"{name} must point to two distinct lanes")
    return result


def _validate_target(value: Any) -> dict[str, str]:
    obj = _require_object(value, "target")
    _require_exact_keys(obj, {"lane_id", "scene_id"}, {"lane_id", "scene_id"}, "target")
    lane = obj["lane_id"]
    if lane not in LANE_IDS:
        raise SceneSwitchValidationError("SCHEMA_INVALID", "target.lane_id must be 'A' or 'B'")
    scene_id = _require_string(obj["scene_id"], "target.scene_id")
    if len(scene_id) > 256 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in scene_id):
        raise SceneSwitchValidationError("SCHEMA_INVALID", "target.scene_id contains a control character or exceeds 256 characters")
    return {"lane_id": lane, "scene_id": scene_id}


def validate_command(command: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy a v1 command, rejecting unknown fields strictly."""

    obj = _require_object(command, "command")
    common = {
        "contract",
        "schema_version",
        "message_type",
        "command_type",
        "command_id",
        "intent_id",
        "runtime_instance_id",
        "expected_revisions",
        "expected_server_seq",
    }
    required = {
        "contract",
        "schema_version",
        "message_type",
        "command_type",
        "command_id",
        "intent_id",
        "runtime_instance_id",
        "expected_revisions",
    }
    _require_exact_keys(obj, required, common | {"target", "timeout_ms", "prepared_command_id", "take_command_id", "reason"}, "command")
    if obj.get("contract") != CONTRACT:
        raise SceneSwitchValidationError("SCHEMA_INVALID", "command contract or schema_version is not v1")
    _require_schema_version(obj.get("schema_version"), "schema_version")
    if obj.get("message_type") != "command":
        raise SceneSwitchValidationError("SCHEMA_INVALID", "message_type must be 'command'")
    command_type = obj["command_type"]
    if command_type not in COMMAND_TYPES:
        raise SceneSwitchValidationError("SCHEMA_INVALID", "command_type is not supported by v1")

    result: dict[str, Any] = {
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "message_type": "command",
        "command_type": command_type,
        "command_id": _require_string(obj["command_id"], "command_id", identifier=True),
        "intent_id": _require_string(obj["intent_id"], "intent_id", identifier=True),
        "runtime_instance_id": _require_string(obj["runtime_instance_id"], "runtime_instance_id", identifier=True),
        "expected_revisions": _validate_revisions(obj["expected_revisions"], "expected_revisions"),
    }
    if "expected_server_seq" in obj:
        result["expected_server_seq"] = _require_non_negative_int(obj["expected_server_seq"], "expected_server_seq")

    if command_type == "Prepare":
        _require_exact_keys(
            obj,
            required | {"target", "timeout_ms"},
            required | {"target", "timeout_ms", "expected_server_seq"},
            "Prepare command",
        )
        result["target"] = _validate_target(obj["target"])
        timeout = _require_non_negative_int(obj["timeout_ms"], "timeout_ms")
        if timeout < 1 or timeout > 60000:
            raise SceneSwitchValidationError("SCHEMA_INVALID", "timeout_ms must be between 1 and 60000")
        result["timeout_ms"] = timeout
    elif command_type == "Take":
        _require_exact_keys(
            obj,
            required | {"prepared_command_id", "timeout_ms"},
            required | {"prepared_command_id", "timeout_ms", "expected_server_seq"},
            "Take command",
        )
        result["prepared_command_id"] = _require_string(
            obj["prepared_command_id"], "prepared_command_id", identifier=True
        )
        timeout = _require_non_negative_int(obj["timeout_ms"], "timeout_ms")
        if timeout < 1 or timeout > 60000:
            raise SceneSwitchValidationError("SCHEMA_INVALID", "timeout_ms must be between 1 and 60000")
        result["timeout_ms"] = timeout
    else:
        _require_exact_keys(
            obj,
            required | {"take_command_id", "reason"},
            required | {"take_command_id", "reason", "expected_server_seq"},
            "Abort command",
        )
        result["take_command_id"] = _require_string(obj["take_command_id"], "take_command_id", identifier=True)
        if obj["reason"] not in ABORT_REASONS:
            raise SceneSwitchValidationError("SCHEMA_INVALID", "reason is not supported by v1")
        result["reason"] = obj["reason"]
    return result


def _validate_event_common(obj: Mapping[str, Any], event_type: str) -> dict[str, Any]:
    common_allowed = {
        "contract",
        "schema_version",
        "message_type",
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
        "observed_at_monotonic_ns",
        "payload_sha256",
    }
    common_required = common_allowed - {"previous_role_map"}
    _require_exact_keys(
        obj,
        common_required,
        common_allowed | _EVENT_FIELDS[event_type] | _EVENT_OPTIONAL_FIELDS.get(event_type, set()),
        "event",
    )
    if obj.get("contract") != CONTRACT:
        raise SceneSwitchValidationError("SCHEMA_INVALID", "event contract or schema_version is not v1")
    _require_schema_version(obj.get("schema_version"), "event.schema_version")
    if obj.get("message_type") != "event" or obj.get("event_type") != event_type:
        raise SceneSwitchValidationError("SCHEMA_INVALID", "event envelope does not match its event_type")
    state = obj.get("state")
    if state not in STATES:
        raise SceneSwitchValidationError("SCHEMA_INVALID", "event.state is not supported by v1")
    result: dict[str, Any] = {
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "message_type": "event",
        "event_type": event_type,
        "command_id": _require_string(obj["command_id"], "event.command_id", identifier=True),
        "intent_id": _require_string(obj["intent_id"], "event.intent_id", identifier=True),
        "runtime_instance_id": _require_string(obj["runtime_instance_id"], "event.runtime_instance_id", identifier=True),
        "server_seq": _require_non_negative_int(obj["server_seq"], "event.server_seq", positive=True),
        "state": state,
        "previous_revisions": _validate_revisions(obj["previous_revisions"], "event.previous_revisions"),
        "revisions": _validate_revisions(obj["revisions"], "event.revisions"),
        "role_map": _validate_role_map(obj["role_map"], "event.role_map"),
        "observed_at_monotonic_ns": _require_non_negative_int(
            obj["observed_at_monotonic_ns"], "event.observed_at_monotonic_ns"
        ),
        "payload_sha256": _require_string(obj["payload_sha256"], "event.payload_sha256", max_length=64),
    }
    if any(result["revisions"][key] < result["previous_revisions"][key] for key in REVISION_KEYS):
        raise SceneSwitchValidationError(
            "SCHEMA_INVALID", "event revisions cannot move backwards"
        )
    if _SHA256_RE.fullmatch(result["payload_sha256"]) is None:
        raise SceneSwitchValidationError("SCHEMA_INVALID", "event.payload_sha256 must be lowercase SHA-256")
    if "previous_role_map" in obj:
        result["previous_role_map"] = _validate_role_map(obj["previous_role_map"], "event.previous_role_map")
    return result


_EVENT_FIELDS: Final[dict[str, set[str]]] = {
    "PrepareAccepted": {"target_lane_id", "target_scene_id", "deadline_monotonic_ns"},
    "PreviewReady": {"target_lane_id", "target_scene_id", "first_frame_id", "first_pts_ns"},
    "TakeAccepted": {
        "take_command_id",
        "target_lane_id",
        "target_scene_id",
        "freeze_until_monotonic_ns",
    },
    "TakeCommitted": {
        "take_command_id",
        "target_lane_id",
        "target_scene_id",
        "source_lane_id",
        "frame_id",
        "pts_ns",
        "program_lane_id",
        "preview_lane_id",
    },
    "TakeAborted": {"take_command_id", "reason"},
    "CommandRejected": {"error_code", "error_message", "error_details", "expected_revisions", "expected_server_seq"},
}

_EVENT_OPTIONAL_FIELDS: Final[dict[str, set[str]]] = {
    "TakeAborted": {"last_committed_frame_id", "last_committed_pts_ns"},
}


def validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy an event, including event-specific required fields."""

    obj = _require_object(event, "event")
    event_type = obj.get("event_type")
    if event_type not in EVENT_TYPES:
        raise SceneSwitchValidationError("SCHEMA_INVALID", "event_type is not supported by v1")
    required = {
        "contract",
        "schema_version",
        "message_type",
        "event_type",
        "command_id",
        "intent_id",
        "runtime_instance_id",
        "server_seq",
        "state",
        "previous_revisions",
        "revisions",
        "role_map",
        "observed_at_monotonic_ns",
        "payload_sha256",
    }
    required_fields = required | _EVENT_FIELDS[event_type]
    allowed_fields = required_fields | _EVENT_OPTIONAL_FIELDS.get(event_type, set()) | {"previous_role_map"}
    _require_exact_keys(obj, required_fields, allowed_fields, "event")
    result = _validate_event_common(obj, event_type)

    if event_type == "PrepareAccepted":
        if result["state"] != "preparing":
            raise SceneSwitchValidationError("SCHEMA_INVALID", "PrepareAccepted.state must be preparing")
        _validate_event_lane_scene(result, obj, "target_lane_id", "target_scene_id")
        result["target_lane_id"] = obj["target_lane_id"]
        result["target_scene_id"] = _require_string(obj["target_scene_id"], "target_scene_id")
        result["deadline_monotonic_ns"] = _require_non_negative_int(
            obj["deadline_monotonic_ns"], "deadline_monotonic_ns", positive=True
        )
    elif event_type == "PreviewReady":
        if result["state"] != "preview_ready":
            raise SceneSwitchValidationError("SCHEMA_INVALID", "PreviewReady.state must be preview_ready")
        _validate_event_lane_scene(result, obj, "target_lane_id", "target_scene_id")
        result["target_lane_id"] = obj["target_lane_id"]
        result["target_scene_id"] = _require_string(obj["target_scene_id"], "target_scene_id")
        result["first_frame_id"] = _require_non_negative_int(obj["first_frame_id"], "first_frame_id")
        result["first_pts_ns"] = _require_non_negative_int(obj["first_pts_ns"], "first_pts_ns")
    elif event_type == "TakeAccepted":
        if result["state"] != "take_accepted":
            raise SceneSwitchValidationError("SCHEMA_INVALID", "TakeAccepted.state must be take_accepted")
        result["take_command_id"] = _require_string(obj["take_command_id"], "take_command_id", identifier=True)
        _validate_event_lane_scene(result, obj, "target_lane_id", "target_scene_id")
        result["target_lane_id"] = obj["target_lane_id"]
        result["target_scene_id"] = _require_string(obj["target_scene_id"], "target_scene_id")
        result["freeze_until_monotonic_ns"] = _require_non_negative_int(
            obj["freeze_until_monotonic_ns"], "freeze_until_monotonic_ns", positive=True
        )
    elif event_type == "TakeCommitted":
        if result["state"] != "ready":
            raise SceneSwitchValidationError("SCHEMA_INVALID", "TakeCommitted.state must be ready")
        result["take_command_id"] = _require_string(obj["take_command_id"], "take_command_id", identifier=True)
        _validate_event_lane_scene(result, obj, "target_lane_id", "target_scene_id")
        result["target_lane_id"] = obj["target_lane_id"]
        result["target_scene_id"] = _require_string(obj["target_scene_id"], "target_scene_id")
        for field in ("source_lane_id", "program_lane_id", "preview_lane_id"):
            lane = obj[field]
            if lane not in LANE_IDS:
                raise SceneSwitchValidationError("SCHEMA_INVALID", f"{field} must be 'A' or 'B'")
            result[field] = lane
        result["frame_id"] = _require_non_negative_int(obj["frame_id"], "frame_id")
        result["pts_ns"] = _require_non_negative_int(obj["pts_ns"], "pts_ns")
        if result["program_lane_id"] != result["role_map"]["on_air"]:
            raise SceneSwitchValidationError("SCHEMA_INVALID", "program_lane_id must equal role_map.on_air")
        if result["preview_lane_id"] != result["role_map"]["preview"]:
            raise SceneSwitchValidationError("SCHEMA_INVALID", "preview_lane_id must equal role_map.preview")
    elif event_type == "TakeAborted":
        if result["state"] != "ready":
            raise SceneSwitchValidationError("SCHEMA_INVALID", "TakeAborted.state must be ready")
        result["take_command_id"] = _require_string(obj["take_command_id"], "take_command_id", identifier=True)
        if obj["reason"] not in ABORT_REASONS:
            raise SceneSwitchValidationError("SCHEMA_INVALID", "event.reason is not supported by v1")
        result["reason"] = obj["reason"]
        for field in ("last_committed_frame_id", "last_committed_pts_ns"):
            if field in obj:
                result[field] = _require_non_negative_int(obj[field], f"event.{field}")
    else:
        if obj["error_code"] not in ERROR_CODES:
            raise SceneSwitchValidationError("SCHEMA_INVALID", "event.error_code is not supported by v1")
        result["error_code"] = obj["error_code"]
        result["error_message"] = _require_string(obj["error_message"], "error_message", max_length=1024)
        result["error_details"] = _require_object(obj["error_details"], "error_details")
        if "expected_revisions" in obj:
            result["expected_revisions"] = _validate_revisions(obj["expected_revisions"], "event.expected_revisions")
        if "expected_server_seq" in obj:
            result["expected_server_seq"] = _require_non_negative_int(
                obj["expected_server_seq"], "event.expected_server_seq"
            )
    return result


def _validate_event_lane_scene(
    event: Mapping[str, Any], obj: Mapping[str, Any], lane_field: str, scene_field: str
) -> None:
    lane = obj[lane_field]
    if lane not in LANE_IDS:
        raise SceneSwitchValidationError("SCHEMA_INVALID", f"{lane_field} must be 'A' or 'B'")
    _require_string(obj[scene_field], scene_field)


@dataclass(frozen=True)
class _PendingPrepare:
    command: dict[str, Any]
    digest: str
    target_lane_id: str
    target_scene_id: str
    deadline_ns: int


@dataclass(frozen=True)
class _PendingTake:
    command: dict[str, Any]
    digest: str
    prepare: _PendingPrepare
    deadline_ns: int


class SceneSwitchMachine:
    """Reference state machine for the observable v1 lifecycle.

    ``dispatch`` handles wire commands.  ``mark_preview_ready`` and
    ``commit_take`` are producer callbacks: they model a rendered first frame
    and the eventual frame-boundary swap without prescribing any OBS/libobs
    topology.  All returned dictionaries are validated event envelopes.
    """

    def __init__(
        self,
        runtime_instance_id: str,
        *,
        initial_revisions: Mapping[str, int] | None = None,
        initial_role_map: Mapping[str, str] | None = None,
        initial_lane_scenes: Mapping[str, str] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.runtime_instance_id = _require_string(
            runtime_instance_id, "runtime_instance_id", identifier=True
        )
        self._revisions = _validate_revisions(
            dict(initial_revisions or {key: 0 for key in REVISION_KEYS}), "initial_revisions"
        )
        self._role_map = _validate_role_map(
            dict(initial_role_map or {"on_air": "A", "preview": "B"}), "initial_role_map"
        )
        scenes = dict(initial_lane_scenes or {})
        for lane in scenes:
            if lane not in LANE_IDS:
                raise SceneSwitchValidationError("SCHEMA_INVALID", "initial_lane_scenes has an unknown lane")
            scenes[lane] = _require_string(scenes[lane], f"initial_lane_scenes.{lane}")
        self._lane_scenes: dict[str, str] = scenes
        self._state = "ready"
        self._server_seq = 0
        self._pending_prepare: _PendingPrepare | None = None
        self._preview_ready: dict[str, int] | None = None
        self._preview_ready_event: dict[str, Any] | None = None
        self._pending_take: _PendingTake | None = None
        self._commands: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._prepare_outcomes: dict[tuple[str, str], dict[str, Any]] = {}
        self._take_outcomes: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def server_seq(self) -> int:
        with self._lock:
            return self._server_seq

    @property
    def revisions(self) -> dict[str, int]:
        with self._lock:
            return deepcopy(self._revisions)

    @property
    def role_map(self) -> dict[str, str]:
        with self._lock:
            return deepcopy(self._role_map)

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._events)

    def snapshot(self) -> dict[str, Any]:
        """Return an inspection snapshot without exposing mutable internals."""

        with self._lock:
            return {
                "contract": CONTRACT,
                "schema_version": SCHEMA_VERSION,
                "runtime_instance_id": self.runtime_instance_id,
                "state": self._state,
                "server_seq": self._server_seq,
                "revisions": deepcopy(self._revisions),
                "role_map": deepcopy(self._role_map),
                "lane_scenes": deepcopy(self._lane_scenes),
                "pending_prepare_command_id": (
                    self._pending_prepare.command["command_id"] if self._pending_prepare else None
                ),
                "pending_take_command_id": self._pending_take.command["command_id"] if self._pending_take else None,
            }

    def dispatch(self, command: Mapping[str, Any], *, now_monotonic_ns: int | None = None) -> dict[str, Any]:
        """Accept a command or return a stable, non-mutating rejection event.

        Validation, idempotency, guards, and the state transition share one
        lock so two callers cannot both pass the same revision snapshot.
        """

        with self._lock:
            return self._dispatch(command, now_monotonic_ns=now_monotonic_ns)

    def _dispatch(self, command: Mapping[str, Any], *, now_monotonic_ns: int | None = None) -> dict[str, Any]:
        """Locked implementation of :meth:`dispatch`."""

        validated = validate_command(command)
        command_id = validated["command_id"]
        command_key = (validated["runtime_instance_id"], command_id)
        digest = payload_sha256(validated)

        previous = self._commands.get(command_key)
        if previous is not None:
            previous_digest, previous_event = previous
            if previous_digest == digest:
                return deepcopy(previous_event)
            return self._reject(
                validated,
                "IDEMPOTENCY_CONFLICT",
                "command_id was already used with a different payload",
                {"original_payload_sha256": previous_digest, "received_payload_sha256": digest},
                now_monotonic_ns=now_monotonic_ns,
            )

        if validated["runtime_instance_id"] != self.runtime_instance_id:
            event = self._reject(
                validated,
                "RUNTIME_MISMATCH",
                "command runtime_instance_id does not belong to this runtime",
                {"runtime_instance_id": self.runtime_instance_id},
                now_monotonic_ns=now_monotonic_ns,
            )
            self._commands[command_key] = (digest, event)
            return deepcopy(event)

        # A command arrival is also a valid timer tick.  Expire an elapsed
        # preparation before evaluating a new local command so it cannot
        # silently replace the pending Preview reservation.  Exact command
        # retries were handled above and still replay their original result.
        now = self._now(now_monotonic_ns)
        pending_prepare = self._pending_prepare
        if (
            pending_prepare is not None
            and self._state == "preparing"
            and now >= pending_prepare.deadline_ns
        ):
            self._expire_prepare(now)

        expected = validated["expected_revisions"]
        if expected != self._revisions:
            event = self._reject(
                validated,
                "REVISION_STALE",
                "expected revisions do not match the current runtime revisions",
                {"expected_revisions": expected, "actual_revisions": self._revisions},
                now_monotonic_ns=now_monotonic_ns,
            )
            self._commands[command_key] = (digest, event)
            return deepcopy(event)

        expected_seq = validated.get("expected_server_seq", _MISSING)
        if expected_seq is not _MISSING and expected_seq != self._server_seq:
            event = self._reject(
                validated,
                "SERVER_SEQ_STALE",
                "expected server sequence does not match the current runtime sequence",
                {"expected_server_seq": expected_seq, "actual_server_seq": self._server_seq},
                now_monotonic_ns=now_monotonic_ns,
            )
            self._commands[command_key] = (digest, event)
            return deepcopy(event)

        if validated["command_type"] == "Prepare":
            event = self._prepare(validated, digest, now)
        elif validated["command_type"] == "Take":
            event = self._take(validated, digest, now)
        else:
            event = self._abort(validated, digest, now)
        self._commands[command_key] = (digest, event)
        return deepcopy(event)

    def mark_preview_ready(
        self,
        prepared_command_id: str,
        first_frame_id: int,
        first_pts_ns: int,
        *,
        now_monotonic_ns: int | None = None,
    ) -> dict[str, Any]:
        """Publish readiness for the first actually rendered Preview frame."""

        with self._lock:
            return self._mark_preview_ready(
                prepared_command_id,
                first_frame_id,
                first_pts_ns,
                now_monotonic_ns=now_monotonic_ns,
            )

    def _mark_preview_ready(
        self,
        prepared_command_id: str,
        first_frame_id: int,
        first_pts_ns: int,
        *,
        now_monotonic_ns: int | None = None,
    ) -> dict[str, Any]:
        """Locked implementation of :meth:`mark_preview_ready`."""

        _require_string(prepared_command_id, "prepared_command_id", identifier=True)
        first_frame_id = _require_non_negative_int(first_frame_id, "first_frame_id")
        first_pts_ns = _require_non_negative_int(first_pts_ns, "first_pts_ns")
        terminal = self._prepare_outcomes.get((self.runtime_instance_id, prepared_command_id))
        if terminal is not None:
            return deepcopy(terminal)
        if self._state == "take_accepted":
            if (
                self._preview_ready_event is not None
                and self._pending_prepare is not None
                and self._pending_prepare.command["command_id"] == prepared_command_id
                and self._preview_ready == {"first_frame_id": first_frame_id, "first_pts_ns": first_pts_ns}
            ):
                return deepcopy(self._preview_ready_event)
            raise SceneSwitchStateError(
                "PREVIEW_FROZEN",
                "Preview readiness cannot change after TakeAccepted",
                {"pending_take_command_id": self._pending_take.command["command_id"] if self._pending_take else None},
            )
        pending = self._pending_prepare
        if pending is None or pending.command["command_id"] != prepared_command_id:
            raise SceneSwitchStateError(
                "PREPARE_NOT_FOUND", "PreviewReady references no current preparation", {"prepared_command_id": prepared_command_id}
            )
        if self._preview_ready_event is not None:
            if self._preview_ready == {"first_frame_id": first_frame_id, "first_pts_ns": first_pts_ns}:
                return deepcopy(self._preview_ready_event)
            raise SceneSwitchStateError(
                "IDEMPOTENCY_CONFLICT",
                "PreviewReady callback attempted to replace immutable readiness",
                {
                    "prepared_command_id": prepared_command_id,
                    "original_first_frame_id": self._preview_ready["first_frame_id"] if self._preview_ready else None,
                    "original_first_pts_ns": self._preview_ready["first_pts_ns"] if self._preview_ready else None,
                    "received_first_frame_id": first_frame_id,
                    "received_first_pts_ns": first_pts_ns,
                },
            )
        now = self._now(now_monotonic_ns)
        if now >= pending.deadline_ns:
            return deepcopy(self._expire_prepare(now))
        self._state = "preview_ready"
        self._preview_ready = {"first_frame_id": first_frame_id, "first_pts_ns": first_pts_ns}
        event = self._emit(
            "PreviewReady",
            pending.command,
            pending.digest,
            {
                "target_lane_id": pending.target_lane_id,
                "target_scene_id": pending.target_scene_id,
                "first_frame_id": first_frame_id,
                "first_pts_ns": first_pts_ns,
            },
            state="preview_ready",
            now_monotonic_ns=now,
        )
        self._preview_ready_event = event
        return deepcopy(event)

    def commit_take(
        self,
        take_command_id: str,
        frame_id: int,
        pts_ns: int,
        *,
        now_monotonic_ns: int | None = None,
    ) -> dict[str, Any]:
        """Commit the pending Take at the frame boundary chosen by the engine."""

        with self._lock:
            return self._commit_take(
                take_command_id,
                frame_id,
                pts_ns,
                now_monotonic_ns=now_monotonic_ns,
            )

    def _commit_take(
        self,
        take_command_id: str,
        frame_id: int,
        pts_ns: int,
        *,
        now_monotonic_ns: int | None = None,
    ) -> dict[str, Any]:
        """Locked implementation of :meth:`commit_take`."""

        _require_string(take_command_id, "take_command_id", identifier=True)
        frame_id = _require_non_negative_int(frame_id, "frame_id")
        pts_ns = _require_non_negative_int(pts_ns, "pts_ns")
        previous_outcome = self._take_outcomes.get(take_command_id)
        if previous_outcome is not None:
            return deepcopy(previous_outcome)
        pending = self._pending_take
        if pending is None or pending.command["command_id"] != take_command_id:
            raise SceneSwitchStateError(
                "TAKE_NOT_PENDING", "Take commit references no pending Take", {"take_command_id": take_command_id}
            )
        now = self._now(now_monotonic_ns)
        if now >= pending.deadline_ns:
            event = self._finish_abort_or_timeout(pending, "timeout", now)
            self._take_outcomes[take_command_id] = event
            return deepcopy(event)

        previous_revisions = deepcopy(self._revisions)
        previous_role_map = deepcopy(self._role_map)
        source_lane = self._role_map["preview"]
        old_on_air = self._role_map["on_air"]
        self._role_map = {"on_air": source_lane, "preview": old_on_air}
        self._revisions["program"] += 1
        self._revisions["role_map"] += 1
        self._state = "ready"
        self._pending_take = None
        self._pending_prepare = None
        self._preview_ready = None
        self._preview_ready_event = None
        event = self._emit(
            "TakeCommitted",
            pending.command,
            pending.digest,
            {
                "take_command_id": take_command_id,
                "target_lane_id": pending.prepare.target_lane_id,
                "target_scene_id": pending.prepare.target_scene_id,
                "source_lane_id": source_lane,
                "frame_id": frame_id,
                "pts_ns": pts_ns,
                "program_lane_id": source_lane,
                "preview_lane_id": old_on_air,
                "previous_role_map": previous_role_map,
            },
            previous_revisions=previous_revisions,
            revisions=self._revisions,
            state="ready",
            now_monotonic_ns=now,
        )
        self._take_outcomes[take_command_id] = event
        return deepcopy(event)

    def poll(self, *, now_monotonic_ns: int | None = None) -> dict[str, Any] | None:
        """Expire pending preparation or Take without changing the route."""

        with self._lock:
            return self._poll(now_monotonic_ns=now_monotonic_ns)

    def _poll(self, *, now_monotonic_ns: int | None = None) -> dict[str, Any] | None:
        """Locked implementation of :meth:`poll`."""

        pending = self._pending_take
        if pending is not None:
            now = self._now(now_monotonic_ns)
            if now < pending.deadline_ns:
                return None
            event = self._finish_abort_or_timeout(pending, "timeout", now)
            self._take_outcomes[pending.command["command_id"]] = event
            return deepcopy(event)

        pending_prepare = self._pending_prepare
        if pending_prepare is None or self._state != "preparing":
            return None
        now = self._now(now_monotonic_ns)
        if now < pending_prepare.deadline_ns:
            return None
        return deepcopy(self._expire_prepare(now))

    def _expire_prepare(self, now: int) -> dict[str, Any]:
        """Expire the pending preparation once and retain its timeout result."""

        pending = self._pending_prepare
        if pending is None:
            raise SceneSwitchStateError(
                "PREPARE_NOT_FOUND",
                "prepare expiration references no current preparation",
            )
        self._state = "ready"
        self._pending_prepare = None
        self._preview_ready = None
        self._preview_ready_event = None
        event = self._reject(
            pending.command,
            "TIMEOUT",
            "Preview did not produce its first frame before the preparation deadline",
            {"deadline_monotonic_ns": pending.deadline_ns},
            now_monotonic_ns=now,
        )
        command_key = (pending.command["runtime_instance_id"], pending.command["command_id"])
        self._prepare_outcomes[command_key] = event
        return event

    def _prepare(self, command: dict[str, Any], digest: str, now_ns: int | None) -> dict[str, Any]:
        now = self._now(now_ns)
        if self._state == "take_accepted":
            event = self._reject(
                command,
                "PREVIEW_FROZEN",
                "Preview is frozen from TakeAccepted until TakeCommitted or abort",
                {"pending_take_command_id": self._pending_take.command["command_id"] if self._pending_take else None},
                now_monotonic_ns=now,
            )
            return event
        target = command["target"]
        if target["lane_id"] != self._role_map["preview"]:
            return self._reject(
                command,
                "PREVIEW_LANE_MISMATCH",
                "Prepare must target the current Preview lane",
                {"expected_lane_id": self._role_map["preview"], "received_lane_id": target["lane_id"]},
                now_monotonic_ns=now,
            )
        previous_revisions = deepcopy(self._revisions)
        self._revisions["preview"] += 1
        self._lane_scenes[target["lane_id"]] = target["scene_id"]
        deadline = now + command["timeout_ms"] * 1_000_000
        pending = _PendingPrepare(command, digest, target["lane_id"], target["scene_id"], deadline)
        self._pending_prepare = pending
        self._preview_ready = None
        self._preview_ready_event = None
        self._state = "preparing"
        return self._emit(
            "PrepareAccepted",
            command,
            digest,
            {
                "target_lane_id": target["lane_id"],
                "target_scene_id": target["scene_id"],
                "deadline_monotonic_ns": deadline,
            },
            previous_revisions=previous_revisions,
            revisions=self._revisions,
            state="preparing",
            now_monotonic_ns=now,
        )

    def _take(self, command: dict[str, Any], digest: str, now_ns: int | None) -> dict[str, Any]:
        now = self._now(now_ns)
        pending_prepare = self._pending_prepare
        if self._state != "preview_ready" or pending_prepare is None:
            return self._reject(
                command,
                "PREVIEW_NOT_READY",
                "Take requires a PreviewReady event for the referenced preparation",
                {"prepared_command_id": command["prepared_command_id"]},
                now_monotonic_ns=now,
            )
        if pending_prepare.command["command_id"] != command["prepared_command_id"]:
            return self._reject(
                command,
                "PREPARE_NOT_FOUND",
                "prepared_command_id is not the current ready Preview",
                {"prepared_command_id": command["prepared_command_id"], "current_prepared_command_id": pending_prepare.command["command_id"]},
                now_monotonic_ns=now,
            )
        if pending_prepare.command["intent_id"] != command["intent_id"]:
            return self._reject(
                command,
                "TAKE_INTENT_CONFLICT",
                "Take intent_id must match the preparation intent_id",
                {"prepared_intent_id": pending_prepare.command["intent_id"], "received_intent_id": command["intent_id"]},
                now_monotonic_ns=now,
            )
        deadline = now + command["timeout_ms"] * 1_000_000
        pending_take = _PendingTake(command, digest, pending_prepare, deadline)
        self._pending_take = pending_take
        self._state = "take_accepted"
        return self._emit(
            "TakeAccepted",
            command,
            digest,
            {
                "take_command_id": command["command_id"],
                "target_lane_id": pending_prepare.target_lane_id,
                "target_scene_id": pending_prepare.target_scene_id,
                "freeze_until_monotonic_ns": deadline,
            },
            state="take_accepted",
            now_monotonic_ns=now,
        )

    def _abort(self, command: dict[str, Any], digest: str, now_ns: int | None) -> dict[str, Any]:
        now = self._now(now_ns)
        pending = self._pending_take
        if pending is None or pending.command["command_id"] != command["take_command_id"]:
            return self._reject(
                command,
                "TAKE_NOT_PENDING",
                "Abort references no pending Take",
                {"take_command_id": command["take_command_id"]},
                now_monotonic_ns=now,
            )
        if pending.command["intent_id"] != command["intent_id"]:
            return self._reject(
                command,
                "TAKE_INTENT_CONFLICT",
                "Abort intent_id must match the pending Take intent_id",
                {"take_intent_id": pending.command["intent_id"], "received_intent_id": command["intent_id"]},
                now_monotonic_ns=now,
            )
        event = self._finish_abort_or_timeout(pending, command["reason"], now, cause=command)
        self._take_outcomes[pending.command["command_id"]] = event
        return event

    def _finish_abort_or_timeout(
        self,
        pending: _PendingTake | _PendingPrepare,
        reason: str,
        now: int,
        *,
        cause: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if reason not in ABORT_REASONS:
            raise SceneSwitchStateError("ABORTED", "invalid abort reason", {"reason": reason})
        command = cause or pending.command
        take_command = pending.command if isinstance(pending, _PendingTake) else pending.command
        intent_id = take_command["intent_id"]
        digest = payload_sha256(command) if cause is not None else pending.digest
        command_id = command["command_id"]
        self._state = "ready"
        self._pending_take = None
        self._pending_prepare = None
        self._preview_ready = None
        self._preview_ready_event = None
        return self._emit(
            "TakeAborted",
            {"command_id": command_id, "intent_id": intent_id},
            digest,
            {
                "take_command_id": take_command["command_id"],
                "reason": reason,
            },
            state="ready",
            now_monotonic_ns=now,
        )

    def _reject(
        self,
        command: Mapping[str, Any],
        code: str,
        message: str,
        details: Mapping[str, Any],
        *,
        now_monotonic_ns: int | None,
    ) -> dict[str, Any]:
        digest = payload_sha256(command)
        extra: dict[str, Any] = {
            "error_code": code,
            "error_message": message,
            "error_details": dict(details),
            "expected_revisions": deepcopy(command.get("expected_revisions", self._revisions)),
        }
        if "expected_server_seq" in command:
            extra["expected_server_seq"] = command["expected_server_seq"]
        return self._emit(
            "CommandRejected",
            {
                "command_id": command["command_id"],
                "intent_id": command["intent_id"],
            },
            digest,
            extra,
            state=self._state,
            now_monotonic_ns=self._now(now_monotonic_ns),
        )

    def _emit(
        self,
        event_type: str,
        command: Mapping[str, Any],
        digest: str,
        fields: Mapping[str, Any],
        *,
        previous_revisions: Mapping[str, int] | None = None,
        revisions: Mapping[str, int] | None = None,
        state: str,
        now_monotonic_ns: int,
    ) -> dict[str, Any]:
        self._server_seq += 1
        event: dict[str, Any] = {
            "contract": CONTRACT,
            "schema_version": SCHEMA_VERSION,
            "message_type": "event",
            "event_type": event_type,
            "command_id": command["command_id"],
            "intent_id": command["intent_id"],
            "runtime_instance_id": self.runtime_instance_id,
            "server_seq": self._server_seq,
            "state": state,
            "previous_revisions": deepcopy(dict(previous_revisions or self._revisions)),
            "revisions": deepcopy(dict(revisions or self._revisions)),
            "role_map": deepcopy(self._role_map),
            "observed_at_monotonic_ns": now_monotonic_ns,
            "payload_sha256": digest,
        }
        event.update(deepcopy(dict(fields)))
        validated = validate_event(event)
        self._events.append(validated)
        return validated

    @staticmethod
    def _now(value: int | None) -> int:
        if value is None:
            return time.monotonic_ns()
        return _require_non_negative_int(value, "now_monotonic_ns")


__all__ = [
    "ABORT_REASONS",
    "COMMAND_TYPES",
    "CONTRACT",
    "ERROR_CODES",
    "EVENT_TYPES",
    "LANE_IDS",
    "REVISION_KEYS",
    "SCHEMA_FILE",
    "SCHEMA_VERSION",
    "STATES",
    "SceneSwitchContractError",
    "SceneSwitchMachine",
    "SceneSwitchStateError",
    "SceneSwitchValidationError",
    "canonical_json",
    "payload_sha256",
    "validate_command",
    "validate_event",
]
