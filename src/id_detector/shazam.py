"""Shazam clip adapter with injected single-attempt HTTP transport."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from shazamio import Shazam
from shazamio.interfaces.client import HTTPClientInterface

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    ObservationRecord,
    ProviderConfigRecord,
    QueryRecord,
    RawLabel,
    WindowRecord,
    compose_natural_key,
    make_id,
)
from id_detector.io import canonical_json_bytes, native_path
from id_detector.semantics import aggregate_shazam_anchor

AttemptCallback = Callable[[], Awaitable[None]]


class ShazamHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str, retry_after: float | None = None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(message)


class TokenBucket:
    """A monotonic, lock-protected token bucket; defaults to one non-bursting token."""

    def __init__(self, rate_per_minute: int = 18, capacity: int = 1) -> None:
        self.rate_per_second = rate_per_minute / 60
        self.capacity = capacity
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.updated) * self.rate_per_second
                )
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                delay = (1 - self.tokens) / self.rate_per_second
            await asyncio.sleep(delay)


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    open_seconds: int = 60
    failures: int = 0
    opened_at: float | None = None

    async def before_request(self) -> None:
        if self.opened_at is None:
            return
        remaining = self.open_seconds - (time.monotonic() - self.opened_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self.failures = 0
        self.opened_at = None

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = time.monotonic()


class InjectedHTTPClient(HTTPClientInterface):
    """One HTTP attempt only; retry policy remains with the job executor."""

    def __init__(
        self,
        *,
        on_attempt: AttemptCallback,
        limiter: TokenBucket,
        breaker: CircuitBreaker,
        transport: httpx.AsyncBaseTransport | None = None,
        url_override: str | None = None,
    ) -> None:
        self.on_attempt = on_attempt
        self.limiter = limiter
        self.breaker = breaker
        self.transport = transport
        self.url_override = url_override

    async def request(self, method: str, url: str, *args: object, **kwargs: Any) -> Any:
        del args
        if method.upper() not in {"GET", "POST"}:
            raise ValueError("injected Shazam transport accepts only GET/POST")
        await self.breaker.before_request()
        await self.limiter.acquire()
        await self.on_attempt()
        kwargs.pop("proxy", None)
        timeout = httpx.Timeout(connect=10, write=30, read=60, pool=10)
        try:
            async with httpx.AsyncClient(
                timeout=timeout, transport=self.transport, follow_redirects=False
            ) as client:
                response = await client.request(method, self.url_override or url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            self.breaker.failure()
            raise ShazamHTTPError(0, f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            self.breaker.failure()
            retry_after_value = response.headers.get("Retry-After")
            try:
                retry_after = float(retry_after_value) if retry_after_value else None
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after_value)
                    retry_after = max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
                except (TypeError, ValueError):
                    retry_after = None
            raise ShazamHTTPError(
                response.status_code,
                f"Shazam HTTP {response.status_code}: {response.text[:500]}",
                retry_after,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            self.breaker.failure()
            raise ShazamHTTPError(response.status_code, "Shazam response was not JSON") from exc
        self.breaker.success()
        return payload


def _fixed_ms(value: Any) -> int:
    return int((Decimal(str(value)) * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _fixed_e6(value: Any) -> int:
    return int((Decimal(str(value)) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def canonicalize_provider_json(value: Any) -> Any:
    """Preserve provider payload shape while representing otherwise-forbidden decimals as text."""

    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return format(value, ".15g")
    if isinstance(value, dict):
        return {str(key): canonicalize_provider_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [canonicalize_provider_json(item) for item in value]
    return value


def _native_matches(matches: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for match in matches:
        native: dict[str, Any] = {}
        for key, value in match.items():
            if key == "offset":
                native["offset_ms"] = _fixed_ms(value)
            elif key == "frequencyskew":
                native["frequencyskew_e6"] = _fixed_e6(value)
            elif key == "timeskew":
                native["timeskew_e6"] = _fixed_e6(value)
            else:
                native[str(key)] = canonicalize_provider_json(value)
        result.append(native)
    return result


def _raw_label(response: Mapping[str, Any]) -> RawLabel:
    track = response.get("track")
    if not isinstance(track, Mapping):
        return RawLabel(artist=None, title=None, album=None, label=None, release_date=None)
    metadata: dict[str, str] = {}
    for section in track.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        for item in section.get("metadata") or []:
            if isinstance(item, Mapping) and item.get("title") is not None:
                metadata[str(item["title"]).casefold()] = str(item.get("text") or "")
    return RawLabel(
        artist=str(track["subtitle"]) if track.get("subtitle") is not None else None,
        title=str(track["title"]) if track.get("title") is not None else None,
        album=metadata.get("album"),
        label=metadata.get("label"),
        release_date=metadata.get("released") or metadata.get("release date"),
    )


def response_to_observation(
    response: Mapping[str, Any],
    query: QueryRecord,
    window: WindowRecord,
    provider_config: ProviderConfigRecord,
    raw_response_ref: str,
    media_key: str,
) -> ObservationRecord:
    matches_value = response.get("matches") or []
    matches = [item for item in matches_value if isinstance(item, Mapping)]
    label = _raw_label(response)
    track = response.get("track") if isinstance(response.get("track"), Mapping) else {}
    provider_ids: dict[str, Any] = {}
    if track.get("key") is not None:
        provider_ids["shazam"] = str(track["key"])
    status = "match" if matches and track else "no_match"
    bias = provider_config.adapter_bias_ms or 0
    bias_uncertainty = provider_config.adapter_bias_uncertainty_ms or 0
    anchor = aggregate_shazam_anchor(
        matches,
        support_start_ms=window.support_ms[0],
        adapter_bias_ms=bias,
        adapter_bias_uncertainty_ms=bias_uncertainty,
        adapter_measured=provider_config.measured,
    )
    raw_label_hash = __import__("hashlib").sha256(canonical_json_bytes(label)).hexdigest()
    natural_values = {
        "query_id": query.id,
        "mix_span_ms": list(window.support_ms),
        "raw_label_hash": raw_label_hash,
        "native_index": 0,
    }
    observation_id = make_id(
        media_key, "observation", compose_natural_key("observation", natural_values)
    )
    return ObservationRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=observation_id,
        generation=query.generation,
        query_id=query.id,
        provider="shazam",
        capability="clip_recognizer",
        status=status,
        is_final=True,
        mix_span_ms=window.support_ms,
        support_ms=window.support_ms,
        transform=window.transform,
        logical_trial_id=window.logical_trial_id,
        raw_label=label,
        provider_ids=provider_ids,
        native={"matches": _native_matches(matches)},
        anchor=anchor,
        score_raw=None,
        quality=None,
        raw_response_ref=raw_response_ref,
        source_ids=[f"query:{query.id}", f"window:{window.id}"],
    )


@dataclass
class ShazamAdapter:
    provider_config: ProviderConfigRecord
    limiter: TokenBucket = field(default_factory=TokenBucket)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    transport: httpx.AsyncBaseTransport | None = None
    url_override: str | None = None

    async def recognize_once(self, wav_path: Path, on_attempt: AttemptCallback) -> dict[str, Any]:
        client = InjectedHTTPClient(
            on_attempt=on_attempt,
            limiter=self.limiter,
            breaker=self.breaker,
            transport=self.transport,
            url_override=self.url_override,
        )
        shazam = Shazam(http_client=client, segment_duration_seconds=12)
        result = await shazam.recognize(native_path(wav_path))
        if not isinstance(result, dict):
            raise ValueError("Shazam response root must be an object")
        return result


def retry_delay(attempt_index: int, retry_after: float | None = None) -> float:
    """Plan backoff: 2s * 2^n, capped at 60s, with Retry-After honoured."""

    calculated = min(60.0, 2.0 * (2**attempt_index))
    return max(calculated, retry_after or 0.0)


def provider_config_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
