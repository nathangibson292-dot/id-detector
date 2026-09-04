"""Canonical, atomic artefact I/O and secret-safe logging."""

from __future__ import annotations

import contextlib
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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel

from id_detector.contracts import SCHEMA_VERSION, SENSITIVE_FIELD_NAMES, _reject_floats


def native_path(path: Path) -> str:
    """Use Win32's extended path form so hashed work paths are not limited to MAX_PATH."""

    resolved = str(path.resolve())
    if os.name != "nt" or resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved[2:]
    return "\\\\?\\" + resolved


def path_is_file(path: Path) -> bool:
    return os.path.isfile(native_path(path))


def read_text(path: Path) -> str:
    with open(native_path(path), encoding="utf-8") as handle:
        return handle.read()


def read_bytes(path: Path) -> bytes:
    with open(native_path(path), "rb") as handle:
        return handle.read()


def path_mtime(path: Path) -> float:
    return os.stat(native_path(path)).st_mtime


def path_size(path: Path) -> int:
    return os.stat(native_path(path)).st_size


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json", by_alias=True, exclude_none=False))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
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
    os.makedirs(native_path(path.parent), exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=native_path(path.parent),
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(native_path(temporary), native_path(path))
        temporary = None
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(native_path(temporary))


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value))


def sha256_file(path: Path) -> str:
    digest = sha256()
    with open(native_path(path), "rb") as handle:
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
        payload = json.loads(read_text(sidecar))
    except (OSError, json.JSONDecodeError) as exc:
        return VerificationResult(False, (f"cannot read sidecar: {exc}",))

    if payload.get("schema_version") != schema_version:
        errors.append("schema_version differs")
    if not path_is_file(artifact_path) or payload.get("sha256") != sha256_file(artifact_path):
        errors.append("artifact hash differs")

    recorded = payload.get("upstream")
    if not isinstance(recorded, dict):
        errors.append("upstream is not an object")
        recorded = {}
    expected_names = set(upstream_paths)
    if set(recorded) != expected_names:
        errors.append("upstream path set differs")
    for logical_path, upstream_path in upstream_paths.items():
        if not path_is_file(upstream_path) or recorded.get(logical_path) != sha256_file(
            upstream_path
        ):
            errors.append(f"upstream hash differs: {logical_path}")
    return VerificationResult(not errors, tuple(errors))


_QUOTED_SECRET = re.compile(
    r'(?i)(["\']?(?:client_id|oauth_token|api_key|api_token|access_key|access_secret|'
    r'client_secret|authorization|cookie)["\']?\s*[:=]\s*)'
    r'(["\'])(.*?)\2'
)
_PLAIN_SECRET = re.compile(
    r"(?i)\b(client_id|oauth_token|api_key|api_token|access_key|access_secret|client_secret|"
    r"authorization|cookie)(\s*[:=]\s*)"
    r"(?:Bearer\s+)?[^\s,;}]+"
)
_HTTP_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

_SENSITIVE_URL_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "client_id",
        "client_secret",
        "cookie",
        "credential",
        "key",
        "oauth_token",
        "policy",
        "sig",
        "signature",
        "token",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
        "x_goog_credential",
        "x_goog_signature",
    }
)


def sensitive_url_query_key(key: str) -> bool:
    normalised = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalised in _SENSITIVE_URL_QUERY_KEYS or normalised.endswith(
        ("_access_token", "_api_key", "_credential", "_secret", "_signature")
    )


def url_has_credentials(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return False
    if parts.username is not None or parts.password is not None:
        return True
    return any(
        sensitive_url_query_key(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True)
    )


def _redact_url_credentials(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return value
    netloc = parts.netloc.rsplit("@", 1)[-1]
    query = urlencode(
        [
            (key, "[REDACTED]" if sensitive_url_query_key(key) else item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_command_argument(value: str) -> str:
    """Remove URL userinfo and query credentials before journaling an argument."""

    return redact_text(_redact_url_credentials(value))


def redact_text(value: str) -> str:
    value = _HTTP_URL.sub(lambda match: _redact_url_credentials(match.group(0)), value)
    value = _QUOTED_SECRET.sub(lambda match: f'{match.group(1)}"[REDACTED]"', value)
    return _PLAIN_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)


def redact_value(value: Any) -> Any:
    """Recursively redact supported secret fields and credential-shaped text."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: (
                "[REDACTED]" if str(key).casefold() in SENSITIVE_FIELD_NAMES else redact_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


class SecretRedactionFilter(logging.Filter):
    """Redact known credential fields from message templates and structured arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Materialise interpolation first so replacing a ``key=%s`` token cannot leave an
        # argument-count mismatch in the logging machinery.
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True
