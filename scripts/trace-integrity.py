"""Authenticated JSONL trace framing shared by probes and closure checks.

The runtime owns the record stream.  This module is deliberately limited to
the post-stop/fusion boundary: it verifies the runtime's chain and external
manifest, and can re-sign a fused artifact after adding receiver observations.
Runtime MAC material is supplied by the operator environment and is never
stored beside or inside the trace.  A sidecar key is retained only for
explicit non-runtime fixtures.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import pathlib
import secrets
from typing import Any, Iterable


SCHEMA = "pulsar.trace-integrity.v1"
GENESIS = "0" * 64
MANIFEST_SUFFIX = ".manifest.json"
KEY_ENV = "PULSAR_TRACE_HMAC_KEY"
# JSON numbers are parsed as IEEE-754 doubles by both the C++ runtime and the
# Python verifier.  Canonicalizing to a fixed decimal precision before hashing
# removes implementation-specific shortest-round-trip spellings (for example
# 0.0058934782600000004 versus 0.00589347826) without affecting any measurable
# video/audio timing budget (12 decimal places of seconds is sub-nanosecond).
TRACE_FLOAT_DECIMAL_PLACES = 12


def _normalize_numbers(value: Any) -> Any:
    """Return a JSON-compatible value with deterministic floating-point form."""

    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TraceIntegrityError("trace contains a non-finite JSON number")
        rounded = round(value, TRACE_FLOAT_DECIMAL_PLACES)
        # Avoid platform-dependent ``-0.0`` spellings at the canonical boundary.
        return 0.0 if rounded == 0.0 else rounded
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_numbers(item) for item in value]
    return value


class TraceIntegrityError(ValueError):
    """The trace, its footer, or its external anchor is not trustworthy."""


def external_key_hex() -> str:
    """Return the operator-held runtime key without persisting it beside a trace."""

    value = os.environ.get(KEY_ENV)
    _load_key(key_hex=value)
    assert value is not None
    return value.strip()


def key_path_for(trace_path: pathlib.Path) -> pathlib.Path:
    return trace_path.with_name(trace_path.name + ".key")


def manifest_path_for(trace_path: pathlib.Path) -> pathlib.Path:
    return trace_path.with_name(trace_path.name + MANIFEST_SUFFIX)


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temp.exists():
        raise TraceIntegrityError(f"temporary integrity path already exists: {temp}")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temp, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd != -1:
                os.close(fd)
        os.replace(temp, path)
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def ensure_key(trace_path: pathlib.Path, *, create: bool = True) -> pathlib.Path:
    """Create or validate a fixture-only key sidecar.

    Runtime evidence must use ``PULSAR_TRACE_HMAC_KEY`` instead.  Keeping this
    helper is intentional for non-runtime unit fixtures, which have no
    operator-held trust anchor.
    """

    path = trace_path.with_name(trace_path.name + ".key")
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        if not create:
            raise TraceIntegrityError(f"external trace key is missing: {path}")
        text = secrets.token_hex(32)
        _atomic_write(path, (text + "\n").encode("ascii"))
    except (OSError, UnicodeError) as exc:
        raise TraceIntegrityError(f"cannot read external trace key {path}: {exc}") from exc
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise TraceIntegrityError(f"external trace key is not a lowercase 32-byte hex key: {path}")
    return path


def _load_key(path: pathlib.Path | None = None, *, key_hex: str | None = None) -> bytes:
    if key_hex is None and path is None:
        key_hex = os.environ.get(KEY_ENV)
    if key_hex is not None:
        text = key_hex.strip()
        if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
            raise TraceIntegrityError(f"{KEY_ENV} is not a lowercase 32-byte hex key")
        return bytes.fromhex(text)
    if path is None:
        raise TraceIntegrityError(f"{KEY_ENV} is missing; runtime trace verification is unavailable")
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise TraceIntegrityError(f"cannot read external trace key {path}: {exc}") from exc
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise TraceIntegrityError(f"external trace key is not a lowercase 32-byte hex key: {path}")
    return bytes.fromhex(text)


def _canonical(record: dict[str, Any]) -> str:
    payload = dict(record)
    payload.pop("trace_integrity", None)
    payload = _normalize_numbers(payload)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mac(key: bytes, domain: str, canonical: str) -> str:
    return hmac.new(key, (domain + canonical).encode("utf-8"), hashlib.sha256).hexdigest()


def _record_hash(previous: str, canonical: str) -> str:
    return hashlib.sha256((previous + "\n" + canonical).encode("utf-8")).hexdigest()


def _json_line(record: dict[str, Any]) -> bytes:
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _annotate(
    records: Iterable[dict[str, Any]],
    key: bytes,
    *,
    previous: str = GENESIS,
    sequence: int = 0,
) -> tuple[list[dict[str, Any]], str, int]:
    annotated: list[dict[str, Any]] = []
    session_id = ""
    for original in records:
        record = dict(original)
        record.pop("trace_integrity", None)
        record = _normalize_numbers(record)
        if not annotated:
            session_id = record.get("session_id", "")
        sequence += 1
        canonical = _canonical(record)
        digest = _record_hash(previous, canonical)
        record["trace_integrity"] = {
            "schema": SCHEMA,
            "sequence": sequence,
            "previous": previous,
            "record_sha256": digest,
            "record_mac": _mac(key, "record|", f"{sequence}|{previous}|{digest}|{session_id}"),
        }
        annotated.append(record)
        previous = digest
    return annotated, previous, sequence


def write_trace(
    trace_path: pathlib.Path,
    records: Iterable[dict[str, Any]],
    key_path: pathlib.Path | None = None,
    *,
    key_hex: str | None = None,
) -> None:
    """Atomically write records, a terminal footer, and an external MAC anchor."""

    key = _load_key(key_path, key_hex=key_hex)
    annotated, chain_head, count = _annotate(records, key)
    if not annotated:
        raise TraceIntegrityError("cannot anchor an empty trace")
    session = annotated[0]
    footer = {
        "chain_head": chain_head,
        "record_count": count,
        "record_type": "trace_footer",
        "runtime_instance_id": session.get("runtime_instance_id", ""),
        "schema": SCHEMA,
        "session_id": session.get("session_id", ""),
    }
    footer["footer_mac"] = _mac(key, "footer|", _canonical(footer))
    payload = b"".join(_json_line(record) for record in annotated) + _json_line(footer)
    _atomic_write(trace_path, payload)
    trace_digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "chain_head": chain_head,
        "footer_mac": footer["footer_mac"],
        "record_count": count,
        "runtime_instance_id": session.get("runtime_instance_id", ""),
        "schema": SCHEMA,
        "session_id": session.get("session_id", ""),
        "trace_sha256": trace_digest,
    }
    manifest["manifest_mac"] = _mac(key, "manifest|", _canonical(manifest))
    _atomic_write(
        manifest_path_for(trace_path),
        (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8"),
    )


def read_trace(
    trace_path: pathlib.Path, key_path: pathlib.Path | None = None, *, key_hex: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Verify chain, footer, and external manifest; return data records/footer."""

    key = _load_key(key_path, key_hex=key_hex)
    try:
        raw = trace_path.read_bytes()
    except OSError as exc:
        raise TraceIntegrityError(f"cannot read trace {trace_path}: {exc}") from exc
    if not raw or not raw.endswith(b"\n"):
        raise TraceIntegrityError("trace is truncated or lacks a final newline")
    lines = raw.splitlines()
    if not lines:
        raise TraceIntegrityError("trace is empty")
    try:
        values = [json.loads(line.decode("utf-8")) for line in lines]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TraceIntegrityError(f"trace JSON is malformed: {exc}") from exc
    footer = values[-1]
    if not isinstance(footer, dict) or footer.get("record_type") != "trace_footer":
        raise TraceIntegrityError("trace footer is missing or is not terminal")
    if footer.get("schema") != SCHEMA:
        raise TraceIntegrityError("trace footer schema is unsupported")
    records: list[dict[str, Any]] = []
    previous = GENESIS
    session_id = ""
    for index, value in enumerate(values[:-1], start=1):
        if not isinstance(value, dict) or value.get("record_type") == "trace_footer":
            raise TraceIntegrityError(f"trace record ordering is invalid at sequence {index}")
        integrity = value.get("trace_integrity")
        if not isinstance(integrity, dict):
            raise TraceIntegrityError(f"trace record {index} has no integrity envelope")
        if index == 1:
            session_id = value.get("session_id", "")
        expected = {
            "schema": SCHEMA,
            "sequence": index,
            "previous": previous,
        }
        if any(integrity.get(key_name) != expected[key_name] for key_name in expected):
            raise TraceIntegrityError(f"trace sequence/previous link is invalid at sequence {index}")
        canonical = _canonical(value)
        digest = _record_hash(previous, canonical)
        if integrity.get("record_sha256") != digest:
            raise TraceIntegrityError(f"trace record hash mismatch at sequence {index}")
        expected_mac = _mac(key, "record|", f"{index}|{previous}|{digest}|{session_id}")
        if not hmac.compare_digest(str(integrity.get("record_mac", "")), expected_mac):
            raise TraceIntegrityError(f"trace record MAC mismatch at sequence {index}")
        clean = dict(value)
        clean.pop("trace_integrity", None)
        records.append(clean)
        previous = digest
    if not records:
        raise TraceIntegrityError("trace contains no data records")
    expected_footer = dict(footer)
    footer_mac = expected_footer.pop("footer_mac", None)
    if expected_footer.get("chain_head") != previous or expected_footer.get("record_count") != len(records):
        raise TraceIntegrityError("trace footer count or chain head is inconsistent")
    if (
        footer.get("runtime_instance_id") != records[0].get("runtime_instance_id")
        or footer.get("session_id") != session_id
    ):
        raise TraceIntegrityError("trace footer identity is inconsistent")
    if not isinstance(footer_mac, str) or not hmac.compare_digest(
        footer_mac, _mac(key, "footer|", _canonical(expected_footer))
    ):
        raise TraceIntegrityError("trace footer MAC mismatch")

    manifest_path = manifest_path_for(trace_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TraceIntegrityError(f"external trace manifest is unavailable: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise TraceIntegrityError("external trace manifest schema is unsupported")
    manifest_mac = manifest.pop("manifest_mac", None)
    if not isinstance(manifest_mac, str) or not hmac.compare_digest(
        manifest_mac, _mac(key, "manifest|", _canonical(manifest))
    ):
        raise TraceIntegrityError("external trace manifest MAC mismatch")
    if (
        manifest.get("chain_head") != previous
        or manifest.get("record_count") != len(records)
        or manifest.get("footer_mac") != footer_mac
    ):
        raise TraceIntegrityError("external trace manifest does not match the footer")
    if (
        manifest.get("runtime_instance_id") != records[0].get("runtime_instance_id")
        or manifest.get("session_id") != session_id
    ):
        raise TraceIntegrityError("external trace manifest identity is inconsistent")
    if manifest.get("trace_sha256") != hashlib.sha256(raw).hexdigest():
        raise TraceIntegrityError("external trace manifest file hash mismatch")
    return records, footer
