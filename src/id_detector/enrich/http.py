"""Bounded, cached, rate-limited HTTP for zero-auth catalogue lookups.

Every enrichment lookup goes through :class:`EnrichHttp`, which adds three things the raw
``httpx`` client does not: a small on-disk JSON cache under ``data/local/enrich/`` (git-ignored), a
polite per-source minimum request interval, and uniform timeout / transport / HTTP-error handling.
Responses are cached by a *credential-stripped* URL so a SoundCloud ``client_id`` never lands in the
cache.  Tests inject an ``httpx.AsyncClient`` backed by a mock transport and a no-op sleeper, so the
default test run performs no network and no real waiting.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from id_detector.io import atomic_write_bytes, path_is_file, read_text, sensitive_url_query_key

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 20
USER_AGENT = "id-detector/0.1 (+local research tool; enrichment lookups)"


class EnrichHttpError(RuntimeError):
    """A bounded lookup transport/HTTP failure that must never abort enrichment."""


@dataclass(frozen=True)
class RateLimit:
    """Minimum seconds between requests to one source (a conservative reading of its docs)."""

    min_interval_s: float


# Deezer ~50/5 s, iTunes ~20/min, MusicBrainz 1/s, Discogs 60/min, SoundCloud api-v2 (polite).
RATE_LIMITS: dict[str, RateLimit] = {
    "deezer": RateLimit(0.12),
    "apple": RateLimit(3.1),
    "musicbrainz": RateLimit(1.05),
    "discogs": RateLimit(1.05),
    "soundcloud": RateLimit(0.6),
}


def _credential_free_key(url: str, params: Mapping[str, Any] | None) -> str:
    parts = urlsplit(url)
    merged: list[tuple[str, str]] = []
    for key, value in list(_query_pairs(parts.query)) + list((params or {}).items()):
        if sensitive_url_query_key(str(key)):
            continue
        merged.append((str(key), str(value)))
    merged.sort()
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged), ""))


def _query_pairs(query: str) -> list[tuple[str, str]]:
    return parse_qsl(query, keep_blank_values=True)


@dataclass
class EnrichHttp:
    client: httpx.AsyncClient
    cache_root: Path
    refresh: bool = False
    sleeper: Callable[[float], Awaitable[None]] | None = None
    clock: Callable[[], float] = monotonic
    _last_request: dict[str, float] | None = None
    request_count: int = 0
    cache_hits: int = 0

    def __post_init__(self) -> None:
        if self._last_request is None:
            self._last_request = {}

    def _cache_path(self, source: str, url: str, params: Mapping[str, Any] | None) -> Path:
        digest = sha256(_credential_free_key(url, params).encode("utf-8")).hexdigest()
        return self.cache_root / source / f"{digest}.json"

    def _read_cache(self, path: Path) -> Any | None:
        if self.refresh or not path_is_file(path):
            return None
        try:
            payload = json.loads(read_text(path))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or "payload" not in payload:
            return None
        return payload["payload"]

    def _write_cache(self, path: Path, url: str, payload: Any) -> None:
        # Store only the parsed catalogue payload plus the credential-free URL for provenance.
        body = json.dumps(
            {"url": _credential_free_key(url, None), "payload": payload},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        atomic_write_bytes(path, body)

    async def _respect_rate(self, source: str) -> None:
        limit = RATE_LIMITS.get(source)
        assert self._last_request is not None
        if limit is None:
            return
        last = self._last_request.get(source)
        now = self.clock()
        if last is not None:
            wait = limit.min_interval_s - (now - last)
            if wait > 0:
                sleeper = self.sleeper
                if sleeper is not None:
                    await sleeper(wait)
        self._last_request[source] = self.clock()

    async def get_json(
        self,
        source: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any | None:
        """Return the parsed JSON body (dict/list) or ``None`` on a benign miss/error.

        Cache-first; else honour the source's rate limit, issue one bounded request, and cache
        successful body.  Transport failures, non-200s, and non-JSON bodies raise
        :class:`EnrichHttpError` so the caller records the source as unavailable and moves on.
        """

        cache_path = self._cache_path(source, url, params)
        cached = self._read_cache(cache_path)
        if cached is not None:
            self.cache_hits += 1
            return cached

        await self._respect_rate(source)
        merged_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        merged_headers.update(headers or {})
        self.request_count += 1
        try:
            response = await self.client.get(
                url, params=dict(params or {}), headers=merged_headers, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            raise EnrichHttpError(f"{source} transport failure: {type(exc).__name__}") from exc
        if response.status_code == 404:
            self._write_cache(cache_path, url, None)
            return None
        if response.status_code != 200:
            raise EnrichHttpError(f"{source} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise EnrichHttpError(f"{source} returned a non-JSON body") from exc
        self._write_cache(cache_path, url, payload)
        return payload


def build_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        headers={"User-Agent": USER_AGENT},
    )
