"""Shared connector context, bounded HTTP, breaker, and local raw caches."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Any

import httpx

from id_detector.contracts import SourceRecord
from id_detector.hints.parse import HintInput
from id_detector.io import atomic_write_bytes, native_path, read_text, redact_value
from id_detector.jobs import AsyncJobStore, ConnectorJob

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 20


class ConnectorError(RuntimeError):
    """A bounded connector failure safe for status output."""


class RetryableConnectorError(ConnectorError):
    """A transient connector failure that may be retried by a later invocation."""


class CircuitOpen(RetryableConnectorError):
    """The five-failures-per-minute connector breaker is open."""


@dataclass
class CircuitBreaker:
    threshold: int = 5
    window_seconds: int = 60
    failures: list[float] | None = None

    def __post_init__(self) -> None:
        if self.failures is None:
            self.failures = []

    def before_request(self) -> None:
        assert self.failures is not None
        cutoff = monotonic() - self.window_seconds
        self.failures[:] = [value for value in self.failures if value >= cutoff]
        if len(self.failures) >= self.threshold:
            raise CircuitOpen("connector breaker open after 5 failures in 60 seconds")

    def failure(self) -> None:
        assert self.failures is not None
        self.failures.append(monotonic())


@dataclass(frozen=True)
class ConnectorContext:
    source: SourceRecord
    duration_ms: int
    media_dir: Path
    cache_root: Path
    store: AsyncJobStore
    job: ConnectorJob
    owner: str
    http: httpx.AsyncClient
    breaker: CircuitBreaker

    @property
    def connector_cache(self) -> Path:
        return self.cache_root / self.job.connector / self.source.source_key / self.job.id

    def raw_path(self, name: str) -> Path:
        return self.connector_cache / name


@dataclass(frozen=True)
class ConnectorOutput:
    inputs: tuple[HintInput, ...] = ()
    pointers: tuple[str, ...] = ()
    mirrors: tuple[str, ...] = ()
    items_fetched: int = 0
    truncated: bool = False
    tracklist_blocks: int = 0
    mirror_candidate: MirrorCandidate | None = None


@dataclass(frozen=True)
class MirrorCandidate:
    requested_url: str
    final_url: str
    platform_id: str | None = None
    uploader_id: str | None = None
    upload_date: str | None = None
    duration_ms: int | None = None
    source_record_ids: tuple[str, ...] = ()


def write_raw_json(path: Path, value: Any) -> None:
    payload = json.dumps(
        redact_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    atomic_write_bytes(path, payload)


def write_raw_text(path: Path, value: str) -> None:
    atomic_write_bytes(path, value.encode("utf-8", errors="replace"))


def write_output(path: Path, output: ConnectorOutput) -> None:
    write_raw_json(
        path,
        {
            "inputs": [asdict(item) for item in output.inputs],
            "pointers": list(output.pointers),
            "mirrors": list(output.mirrors),
            "items_fetched": output.items_fetched,
            "truncated": output.truncated,
            "tracklist_blocks": output.tracklist_blocks,
            "mirror_candidate": (
                asdict(output.mirror_candidate) if output.mirror_candidate is not None else None
            ),
        },
    )


def read_output(path: Path) -> ConnectorOutput:
    payload = json.loads(read_text(path))
    raw_candidate = payload.get("mirror_candidate")
    candidate = (
        MirrorCandidate(
            requested_url=str(raw_candidate["requested_url"]),
            final_url=str(raw_candidate["final_url"]),
            platform_id=(
                str(raw_candidate["platform_id"])
                if raw_candidate.get("platform_id") is not None
                else None
            ),
            uploader_id=(
                str(raw_candidate["uploader_id"])
                if raw_candidate.get("uploader_id") is not None
                else None
            ),
            upload_date=(
                str(raw_candidate["upload_date"])
                if raw_candidate.get("upload_date") is not None
                else None
            ),
            duration_ms=(
                int(raw_candidate["duration_ms"])
                if raw_candidate.get("duration_ms") is not None
                else None
            ),
            source_record_ids=tuple(
                str(item) for item in raw_candidate.get("source_record_ids", [])
            ),
        )
        if isinstance(raw_candidate, dict)
        else None
    )
    return ConnectorOutput(
        inputs=tuple(HintInput(**item) for item in payload.get("inputs", [])),
        pointers=tuple(str(item) for item in payload.get("pointers", [])),
        mirrors=tuple(str(item) for item in payload.get("mirrors", [])),
        items_fetched=int(payload.get("items_fetched", 0)),
        truncated=bool(payload.get("truncated", False)),
        tracklist_blocks=int(payload.get("tracklist_blocks", 0)),
        mirror_candidate=candidate,
    )


async def bounded_get(
    context: ConnectorContext,
    url: str,
    *,
    params: dict[str, str | int] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    context.breaker.before_request()
    try:
        response = await context.http.get(
            url, params=params, headers=headers, follow_redirects=False
        )
    except httpx.HTTPError as exc:
        context.breaker.failure()
        raise RetryableConnectorError(
            f"{context.job.connector} transport failure: {type(exc).__name__}"
        ) from exc
    if response.status_code >= 500 or response.status_code in {408, 429}:
        context.breaker.failure()
        raise RetryableConnectorError(
            f"{context.job.connector} returned retryable HTTP {response.status_code}"
        )
    return response


async def bounded_post(
    context: ConnectorContext,
    url: str,
    *,
    json_body: dict[str, Any],
) -> httpx.Response:
    context.breaker.before_request()
    try:
        response = await context.http.post(url, json=json_body, follow_redirects=False)
    except httpx.HTTPError as exc:
        context.breaker.failure()
        raise RetryableConnectorError(
            f"{context.job.connector} transport failure: {type(exc).__name__}"
        ) from exc
    if response.status_code >= 500 or response.status_code in {408, 429}:
        context.breaker.failure()
        raise RetryableConnectorError(
            f"{context.job.connector} returned retryable HTTP {response.status_code}"
        )
    return response


def read_json_response(response: httpx.Response, connector: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ConnectorError(f"{connector} returned an invalid HTTP/JSON response") from exc
    if not isinstance(payload, dict):
        raise ConnectorError(f"{connector} response is not an object")
    return payload


def native_exists(path: Path) -> bool:
    return Path(native_path(path)).is_file()
