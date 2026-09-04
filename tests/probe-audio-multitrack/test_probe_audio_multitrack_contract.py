"""Offline contracts for the multi-track audio probe's canvas setup."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "probe-audio-multitrack.py"
SPEC = importlib.util.spec_from_file_location("probe_audio_multitrack", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class FakeSession:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    async def ok(self, request_type: str, data: dict | None = None) -> dict:
        self.calls.append((request_type, data))
        response = self.responses[request_type]
        if isinstance(response, list):
            return response.pop(0)
        return response  # type: ignore[return-value]


def test_private_lane_selection_is_replaced_by_registered_main_scene():
    session = FakeSession({
        "GetCurrentProgramScene": [
            {"sceneName": "PulsarLaneA", "sceneUuid": "private-lane"},
            {"sceneName": "Default", "sceneUuid": "main-default"},
        ],
        "GetSceneList": {
            "scenes": [{"sceneName": "Default", "sceneUuid": "main-default"}],
        },
        "SetCurrentProgramScene": {},
    })

    selected = asyncio.run(probe.resolve_main_canvas_scene(session))

    assert selected == {"sceneName": "Default", "sceneUuid": "main-default"}
    assert session.calls == [
        ("GetCurrentProgramScene", None),
        ("GetSceneList", None),
        ("SetCurrentProgramScene", {"sceneUuid": "main-default"}),
        ("GetCurrentProgramScene", None),
    ]


def test_empty_main_canvas_inventory_remains_a_hard_failure():
    session = FakeSession({
        "GetCurrentProgramScene": {"sceneName": "PulsarLaneA"},
        "GetSceneList": {"scenes": []},
    })

    with pytest.raises(probe.Failure, match="no Main-canvas scenes"):
        asyncio.run(probe.resolve_main_canvas_scene(session))
