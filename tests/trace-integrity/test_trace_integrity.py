from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("trace_integrity_contract", ROOT / "scripts" / "trace-integrity.py")
assert SPEC is not None and SPEC.loader is not None
trace_integrity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trace_integrity)


def _records() -> list[dict[str, object]]:
    return [
        {
            "record_type": "session",
            "schema": "pulsar.take-latency.v1",
            "runtime_instance_id": "runtime-integrity-001",
            "session_id": "session-integrity-001",
            "evidence_kind": "runtime",
        },
        {
            "record_type": "resource_sample",
            "runtime_instance_id": "runtime-integrity-001",
            "sample_mode": "reference",
            "sample_count": 1,
        },
    ]


def _write(path: Path) -> Path:
    key = trace_integrity.ensure_key(path)
    trace_integrity.write_trace(path, _records(), key)
    return key


def test_authenticated_trace_round_trip_and_external_anchor(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    key = _write(path)
    records, footer = trace_integrity.read_trace(path, key)
    assert len(records) == 2
    assert footer["record_type"] == "trace_footer"
    assert trace_integrity.manifest_path_for(path).is_file()
    assert trace_integrity.key_path_for(path).is_file()


@pytest.mark.parametrize("mutation", ["corrupt", "truncate", "reorder", "append"])
def test_trace_mutations_fail_closed(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "trace.jsonl"
    key = _write(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    if mutation == "corrupt":
        lines[1] = lines[1].replace('"sample_count":1', '"sample_count":2')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif mutation == "truncate":
        path.write_bytes(path.read_bytes().rsplit(b"{", 1)[0])
    elif mutation == "reorder":
        path.write_text("\n".join([lines[0], lines[-1], lines[1]]) + "\n", encoding="utf-8")
    else:
        extra = {"record_type": "resource_sample", "sample_mode": "reference"}
        path.write_text(path.read_text(encoding="utf-8") + json.dumps(extra) + "\n", encoding="utf-8")
    with pytest.raises(trace_integrity.TraceIntegrityError):
        trace_integrity.read_trace(path, key)


def test_manifest_edit_and_wrong_key_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    key = _write(path)
    manifest_path = trace_integrity.manifest_path_for(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["record_count"] += 1
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    with pytest.raises(trace_integrity.TraceIntegrityError, match="manifest"):
        trace_integrity.read_trace(path, key)


def test_runtime_key_is_environment_held_and_not_serialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "runtime.jsonl"
    key_hex = "ab" * 32
    monkeypatch.setenv(trace_integrity.KEY_ENV, key_hex)
    trace_integrity.write_trace(path, _records(), key_hex=trace_integrity.external_key_hex())
    records, _footer = trace_integrity.read_trace(path)
    assert records
    assert key_hex not in path.read_text(encoding="utf-8")
    assert not trace_integrity.key_path_for(path).exists()


def test_wrong_out_of_band_key_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    _write(path)
    with pytest.raises(trace_integrity.TraceIntegrityError):
        trace_integrity.read_trace(path, key_hex="cd" * 32)
