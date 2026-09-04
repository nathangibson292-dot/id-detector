"""AudD enterprise whole-file scanner with a single durable synchronous submission.

Anchor convention: the mix anchor is the start of AudD's 12-second chunk (``offset``), while
the reference anchor is the matched recording position (``timecode``). ``start_offset`` and
``end_offset`` localise the evidence span inside that chunk but do not move either anchor.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha1, sha256
from pathlib import Path
from typing import Any

import httpx

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    Anchor,
    AssetQueryTarget,
    ObservationRecord,
    QueryRecord,
    RawLabel,
    compose_natural_key,
    file_scan_cache_key,
    make_id,
    sort_records,
)
from id_detector.io import (
    atomic_write_json,
    canonical_json_bytes,
    native_path,
    path_is_file,
    redact_value,
)
from id_detector.jobs import HEARTBEAT_SECONDS, AsyncJobStore, Job, heartbeat_job
from id_detector.providers.base import (
    AmbiguousProviderOutcome,
    AppConfig,
    ProviderCapability,
    ProviderProtocolError,
    ProviderUnavailable,
    require_upload_permission,
)
from id_detector.shazam import canonicalize_provider_json

PROVIDER = "audd"
PROVIDER_CONFIG_VERSION = "audd-v1.json"
ENTERPRISE_ENDPOINT = "https://enterprise.audd.io/"
CHUNK_MS = 12_000
PRICE_PER_HOUR_USD_E2 = 150
CAPABILITY = ProviderCapability(PROVIDER, "file_scanner", True, "enterprise whole-file scan")

AttemptCallback = Callable[[], Awaitable[None]]
FailureHook = Callable[[str], None]


@dataclass(frozen=True)
class AudDCredentials:
    api_token: str = field(repr=False)

    @classmethod
    def from_env(cls) -> AudDCredentials:
        token = os.environ.get("AUDD_API_TOKEN", "").strip()
        if not token:
            raise ProviderUnavailable("AUDD_API_TOKEN is not set")
        return cls(api_token=token)


@dataclass(frozen=True)
class AudDScanPolicy:
    limit: int
    skip: int = 0
    every: int = 1
    accurate_offsets: bool = True

    def __post_init__(self) -> None:
        if self.limit < 1 or self.skip < 0 or self.every < 1:
            raise ValueError("AudD limit/every must be positive and skip cannot be negative")

    def form_fields(self, token: str) -> dict[str, str]:
        return {
            "api_token": token,
            "limit": str(self.limit),
            "skip": str(self.skip),
            "every": str(self.every),
            "accurate_offsets": "true" if self.accurate_offsets else "false",
        }


@dataclass(frozen=True)
class AudDExecutionResult:
    observations: tuple[ObservationRecord, ...]
    response: dict[str, Any]
    billable_units: int
    usd_e2: int


def billable_units(duration_ms: int) -> int:
    if duration_ms < 0:
        raise ValueError("duration cannot be negative")
    return (duration_ms + CHUNK_MS - 1) // CHUNK_MS


def cost_usd_e2(units: int) -> int:
    """Conservative integer-cent estimate at the plan's $1.50 per audio hour."""

    if units < 0:
        raise ValueError("units cannot be negative")
    chunks_per_hour = 3_600_000 // CHUNK_MS
    return (units * PRICE_PER_HOUR_USD_E2 + chunks_per_hour - 1) // chunks_per_hour


def build_query(
    *, media_key: str, asset_kind: str, asset_sha256: str, scan_policy: str
) -> QueryRecord:
    target = AssetQueryTarget(asset=asset_kind, asset_sha256=asset_sha256)
    natural = {
        "provider": PROVIDER,
        "capability": "file_scanner",
        "target": target.model_dump(mode="json"),
        "provider_config_version": PROVIDER_CONFIG_VERSION,
        "scan_policy": scan_policy,
    }
    return QueryRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(media_key, "query", compose_natural_key("query", natural)),
        generation=0,
        provider=PROVIDER,
        capability="file_scanner",
        target=target,
        provider_config_version=PROVIDER_CONFIG_VERSION,
        scan_policy=scan_policy,
        cache_key=file_scan_cache_key(
            asset_kind, asset_sha256, PROVIDER, PROVIDER_CONFIG_VERSION, scan_policy
        ),
    )


def _milliseconds(value: Any) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _timecode_ms(value: Any) -> int:
    if isinstance(value, (int, float, Decimal)):
        return _milliseconds(Decimal(str(value)) * 1000)
    parts = str(value).strip().split(":")
    if not parts or len(parts) > 3:
        raise ProviderProtocolError(f"invalid provider timecode: {value!r}")
    try:
        numbers = [Decimal(item) for item in parts]
    except Exception as exc:
        raise ProviderProtocolError(f"invalid provider timecode: {value!r}") from exc
    seconds = numbers[-1]
    if len(numbers) >= 2:
        seconds += numbers[-2] * 60
    if len(numbers) == 3:
        seconds += numbers[-3] * 3600
    return _milliseconds(seconds * 1000)


def _empty_label() -> RawLabel:
    return RawLabel(artist=None, title=None, album=None, label=None, release_date=None)


def _logical_trial_id(chunk_index: int) -> str:
    return sha1(f"{PROVIDER}|{chunk_index}".encode(), usedforsecurity=False).hexdigest()


def parse_response(
    response: Mapping[str, Any],
    *,
    query: QueryRecord,
    media_key: str,
    duration_ms: int,
    raw_response_ref: str,
) -> tuple[ObservationRecord, ...]:
    """Convert exactly one final observation per returned enterprise chunk."""

    if response.get("status") != "success" or not isinstance(response.get("result"), list):
        raise ProviderProtocolError("AudD enterprise response is not a successful chunk list")
    observations: list[ObservationRecord] = []
    for chunk_index, chunk_value in enumerate(response["result"]):
        if not isinstance(chunk_value, Mapping):
            raise ProviderProtocolError("AudD chunk is not an object")
        chunk_offset_ms = _timecode_ms(chunk_value.get("offset", 0))
        support_start = min(duration_ms, max(0, chunk_offset_ms))
        support_end = min(duration_ms, support_start + CHUNK_MS)
        songs_value = chunk_value.get("songs") or []
        songs = [item for item in songs_value if isinstance(item, Mapping)]
        song = min(
            songs,
            key=lambda item: (-_milliseconds(item.get("score", 0)), canonical_json_bytes(item)),
            default=None,
        )
        label = _empty_label()
        status = "no_match"
        provider_ids: dict[str, Any] = {}
        anchor = None
        score = None
        start_offset_ms = 0
        end_offset_ms = max(0, support_end - support_start)
        native: dict[str, Any] = {
            "offset": chunk_value.get("offset", "00:00"),
            "timecode": None,
            "start_offset": None,
            "end_offset": None,
            "score": None,
            "songs": canonicalize_provider_json(songs),
        }
        if song is not None:
            status = "match"
            label = RawLabel(
                artist=str(song["artist"]) if song.get("artist") is not None else None,
                title=str(song["title"]) if song.get("title") is not None else None,
                album=str(song["album"]) if song.get("album") is not None else None,
                label=str(song["label"]) if song.get("label") is not None else None,
                release_date=(
                    str(song["release_date"]) if song.get("release_date") is not None else None
                ),
            )
            for key in ("isrc", "upc", "audd"):
                if song.get(key) is not None:
                    provider_ids[key] = str(song[key])
            score = _milliseconds(song.get("score", 0))
            start_offset_ms = max(0, _milliseconds(song.get("start_offset", 0)))
            end_offset_ms = max(
                start_offset_ms,
                _milliseconds(song.get("end_offset", support_end - support_start)),
            )
            ref_anchor_ms = _timecode_ms(song.get("timecode", 0))
            anchor = Anchor(
                mix_anchor_ms=support_start,
                ref_anchor_ms=ref_anchor_ms,
                uncertainty_ms=1_000,
                reliable=True,
                method="audd_chunk_offset_to_song_timecode",
                bias_applied_ms=0,
            )
            native.update(
                {
                    "timecode": song.get("timecode"),
                    "start_offset": start_offset_ms,
                    "end_offset": end_offset_ms,
                    "score": score,
                }
            )
        mix_start = min(support_end, support_start + start_offset_ms)
        mix_end = min(support_end, support_start + end_offset_ms)
        if mix_end < mix_start:
            mix_end = mix_start
        label_hash = sha256(canonical_json_bytes(label)).hexdigest()
        natural = {
            "query_id": query.id,
            "mix_span_ms": [mix_start, mix_end],
            "raw_label_hash": label_hash,
            "native_index": chunk_index,
            "transform": None,
        }
        observations.append(
            ObservationRecord(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                id=make_id(media_key, "observation", compose_natural_key("observation", natural)),
                generation=query.generation,
                query_id=query.id,
                provider=PROVIDER,
                capability="file_scanner",
                status=status,
                is_final=True,
                mix_span_ms=(mix_start, mix_end),
                support_ms=(support_start, support_end),
                transform=None,
                logical_trial_id=_logical_trial_id(chunk_index),
                raw_label=label,
                provider_ids=provider_ids,
                native=native,
                anchor=anchor,
                score_raw=score,
                quality=None,
                raw_response_ref=raw_response_ref,
                source_ids=[f"query:{query.id}"],
            )
        )
    return tuple(sort_records(observations))


@dataclass
class AudDAdapter:
    credentials: AudDCredentials
    app_config: AppConfig
    cli_confirmation: bool
    transport: httpx.AsyncBaseTransport | None = None
    endpoint: str = ENTERPRISE_ENDPOINT

    async def scan_file(
        self,
        path: Path,
        *,
        policy: AudDScanPolicy,
        on_attempt: AttemptCallback,
    ) -> dict[str, Any]:
        require_upload_permission(self.app_config, self.cli_confirmation)
        if not path_is_file(path):
            raise FileNotFoundError(path)
        timeout = httpx.Timeout(connect=30, write=1_800, read=1_800, pool=30)
        try:
            with open(native_path(path), "rb") as handle:
                await on_attempt()
                async with httpx.AsyncClient(
                    transport=self.transport, timeout=timeout, follow_redirects=False
                ) as client:
                    result = await client.post(
                        self.endpoint,
                        data=policy.form_fields(self.credentials.api_token),
                        files={"file": (path.name, handle, "application/octet-stream")},
                    )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            message = "AudD response was lost; no reconciliation exists"
            raise AmbiguousProviderOutcome(message) from exc
        if result.status_code >= 400:
            raise ProviderProtocolError(f"AudD HTTP {result.status_code}")
        try:
            payload = result.json()
        except ValueError as exc:
            raise ProviderProtocolError("AudD returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise ProviderProtocolError("AudD response root is not an object")
        return redact_value(payload)

    async def scan_url(
        self,
        url: str,
        *,
        policy: AudDScanPolicy,
        on_attempt: AttemptCallback,
    ) -> dict[str, Any]:
        require_upload_permission(self.app_config, self.cli_confirmation)
        await on_attempt()
        data = {**policy.form_fields(self.credentials.api_token), "url": url}
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(connect=30, write=1_800, read=1_800, pool=30),
                follow_redirects=False,
            ) as client:
                result = await client.post(self.endpoint, data=data)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            message = "AudD response was lost; no reconciliation exists"
            raise AmbiguousProviderOutcome(message) from exc
        if result.status_code >= 400:
            raise ProviderProtocolError(f"AudD HTTP {result.status_code}")
        try:
            payload = result.json()
        except ValueError as exc:
            raise ProviderProtocolError("AudD returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise ProviderProtocolError("AudD response root is not an object")
        return redact_value(payload)


async def execute_job(
    *,
    store: AsyncJobStore,
    job: Job,
    owner: str,
    adapter: AudDAdapter,
    query: QueryRecord,
    media_key: str,
    duration_ms: int,
    asset_path: Path,
    raw_path: Path,
    raw_response_ref: str,
    failure_hook: FailureHook | None = None,
) -> AudDExecutionResult:
    """Run AudD once; any lost response becomes unrecoverable ``outcome_unknown``."""

    require_upload_permission(adapter.app_config, adapter.cli_confirmation)
    units = billable_units(duration_ms)
    reserved_cost = cost_usd_e2(units)
    heartbeat = asyncio.create_task(
        heartbeat_job(store, job.id, owner, interval_seconds=HEARTBEAT_SECONDS)
    )
    try:
        await store.reserve_billing(job.id, units=units, usd=reserved_cost)
        await store.submission_started(job.id, owner)
        hook = failure_hook or (lambda _point: None)
        hook("before_network")
        try:
            response = await adapter.scan_file(
                asset_path,
                policy=AudDScanPolicy(limit=max(1, units)),
                on_attempt=lambda: store.begin_physical_attempt(job.id, reserve_request=False),
            )
        except AmbiguousProviderOutcome as exc:
            await store.mark_outcome_unknown(job.id, error=str(exc))
            raise
        except ProviderProtocolError as exc:
            await store.finish(
                job.id,
                "permanent_failure",
                result_path=None,
                error=str(exc),
                actual_units=units,
                actual_usd=reserved_cost,
            )
            raise
        hook("after_acceptance")
        await store.submitted(job.id)
        atomic_write_json(raw_path, canonicalize_provider_json(response))
        try:
            observations = parse_response(
                response,
                query=query,
                media_key=media_key,
                duration_ms=duration_ms,
                raw_response_ref=raw_response_ref,
            )
        except ProviderProtocolError as exc:
            await store.finish(
                job.id,
                "permanent_failure",
                result_path=None,
                error=str(exc),
                actual_units=units,
                actual_usd=reserved_cost,
            )
            raise
        actual_units = len(response.get("result", []))
        actual_cost = cost_usd_e2(actual_units)
        state = "succeeded" if any(item.status == "match" for item in observations) else "no_match"
        await store.finish(
            job.id,
            state,
            result_path=raw_response_ref,
            actual_units=actual_units,
            actual_usd=actual_cost,
        )
        return AudDExecutionResult(observations, dict(response), actual_units, actual_cost)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
