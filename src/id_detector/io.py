"""Canonical, atomic artefact I/O and secret-safe logging."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from id_detector.contracts import SCHEMA_VERSION, SENSITIVE_FIELD_NAMES, _reject_floats


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=False)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Encode canonical JSON: UTF-8, sorted keys, compact, explicit nulls, and no floats."""

    payload = _jsonable(value)
    _reject_floats(payload)
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write beside the destination, close it, then atomically replace the destination."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value))


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def completion_sidecar_path(artifact_path: Path) -> Path:
    """Map ``X.ext`` to the plan's sibling ``X.done.json``."""

    return artifact_path.with_suffix(".done.json")


def write_completion_sidecar(
    artifact_path: Path,
    upstream_paths: Mapping[str, Path],
    schema_version: str = SCHEMA_VERSION,
) -> Path:
    sidecar = completion_sidecar_path(artifact_path)
    payload = {
        "schema_version": schema_version,
        "sha256": sha256_file(artifact_path),
        "upstream": {
            logical_path: sha256_file(upstream_path)
            for logical_path, upstream_path in sorted(upstream_paths.items())
        },
    }
    atomic_write_json(sidecar, payload)
    return sidecar


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    errors: tuple[str, ...]


def verify_completion_sidecar(
    artifact_path: Path,
    upstream_paths: Mapping[str, Path],
    schema_version: str = SCHEMA_VERSION,
) -> VerificationResult:
    sidecar = completion_sidecar_path(artifact_path)
    errors: list[str] = []
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return VerificationResult(False, (f"cannot read sidecar: {exc}",))

    if payload.get("schema_version") != schema_version:
        errors.append("schema_version differs")
    if not artifact_path.is_file() or payload.get("sha256") != sha256_file(artifact_path):
        errors.append("artifact hash differs")

    recorded = payload.get("upstream")
    if not isinstance(recorded, dict):
        errors.append("upstream is not an object")
        recorded = {}
    expected_names = set(upstream_paths)
    if set(recorded) != expected_names:
        errors.append("upstream path set differs")
    for logical_path, upstream_path in upstream_paths.items():
        if not upstream_path.is_file() or recorded.get(logical_path) != sha256_file(upstream_path):
            errors.append(f"upstream hash differs: {logical_path}")
    return VerificationResult(not errors, tuple(errors))


_QUOTED_SECRET = re.compile(
    r'(?i)(["\']?(?:client_id|oauth_token|api_key|authorization|cookie)["\']?\s*[:=]\s*)'
    r'(["\'])(.*?)\2'
)
_PLAIN_SECRET = re.compile(
    r"(?i)\b(client_id|oauth_token|api_key|authorization|cookie)(\s*[:=]\s*)"
    r"(?:Bearer\s+)?[^\s,;}]+"
)


def redact_text(value: str) -> str:
    value = _QUOTED_SECRET.sub(lambda match: f'{match.group(1)}"[REDACTED]"', value)
    return _PLAIN_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if str(key).casefold() in SENSITIVE_FIELD_NAMES else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class SecretRedactionFilter(logging.Filter):
    """Redact known credential fields from message templates and structured arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Materialise interpolation first so replacing a ``key=%s`` token cannot leave an
        # argument-count mismatch in the logging machinery.
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True
