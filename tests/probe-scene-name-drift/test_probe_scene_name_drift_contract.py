"""Offline contracts for the scene-name-drift probe's Main-canvas setup."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "probe-scene-name-drift.py"
SPEC = importlib.util.spec_from_file_location("probe_scene_name_drift", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class FakeRequest:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    async def __call__(self, _inbox, _ws, request_type: str, _request_id: str,
                       data: dict | None = None, timeout: float = 10.0) -> dict:
        del timeout
        self.calls.append((request_type, data))
        response = self.responses[request_type]
        if isinstance(response, list):
            return response.pop(0)
        return response  # type: ignore[return-value]


def _success(data: dict) -> dict:
    return {"requestStatus": {"result": True}, "responseData": data}


def test_private_lane_selection_is_replaced_by_registered_main_scene(monkeypatch):
    fake = FakeRequest({
        "GetCurrentProgramScene": [
            _success({"sceneName": "PulsarLaneA", "sceneUuid": "private-lane"}),
            _success({"sceneName": "Default", "sceneUuid": "main-default"}),
        ],
        "GetSceneList": _success({
            "scenes": [{"sceneName": "Default", "sceneUuid": "main-default"}],
        }),
        "SetCurrentProgramScene": _success({}),
    })
    monkeypatch.setattr(probe, "request", fake)

    selected = asyncio.run(probe.resolve_main_canvas_scene(probe.Inbox(), object()))

    assert selected == {"sceneName": "Default", "sceneUuid": "main-default"}
    assert fake.calls == [
        ("GetCurrentProgramScene", {}),
        ("GetSceneList", {}),
        ("SetCurrentProgramScene", {"sceneUuid": "main-default"}),
        ("GetCurrentProgramScene", {}),
    ]
    assert probe.scene_request_data(selected) == {"sceneUuid": "main-default"}


def test_empty_main_canvas_inventory_is_fail_closed(monkeypatch):
    fake = FakeRequest({
        "GetCurrentProgramScene": _success({"sceneName": "PulsarLaneA"}),
        "GetSceneList": _success({"scenes": []}),
    })
    monkeypatch.setattr(probe, "request", fake)

    with pytest.raises(RuntimeError, match="no Main-canvas scenes"):
        asyncio.run(probe.resolve_main_canvas_scene(probe.Inbox(), object()))
