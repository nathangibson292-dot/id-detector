"""ACRCloud File Scanning adapter with reconciliation and restart-safe polling.

Anchor convention: when sample/database offsets are present, the paired anchor is
``mix = offset*1000 + sample_begin_time_offset_ms`` and
``reference = db_begin_time_offset_ms``. If those optional fields are absent,
``offset``/``play_offset_ms`` are retained but the fallback anchor is marked unreliable.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
from id_detector.fuse.scanners import scanner_logical_trial_id
from id_detector.io import (
    atomic_write_json,
    canonical_json_bytes,
    native_path,
    path_is_file,
    path_size,
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

PROVIDER = "acrcloud"
PROVIDER_CONFIG_VERSION = "acrcloud-v1.json"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
PRICE_PER_HOUR_USD_E2 = 140
POLL_INITIAL_SECONDS = 30
POLL_MAX_SECONDS = 300
POLL_TIMEOUT_SECONDS = 48 * 60 * 60
CAPABILITY = ProviderCapability(PROVIDER, "file_scanner", True, "remote file scan")

AttemptCallback = Callable[[], Awaitable[None]]
FailureHook = Callable[[str], None]
Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], datetime]


@dataclass(frozen=True)
class ACRCloudCredentials:
    host: str
    access_key: str = field(repr=False)
    access_secret: str = field(repr=False)
    container_id: str

    @classmethod
    def from_env(cls) -> ACRCloudCredentials:
        names = (
            "ACRCLOUD_HOST",
            "ACRCLOUD_ACCESS_KEY",
            "ACRCLOUD_ACCESS_SECRET",
            "ACRCLOUD_CONTAINER_ID",
        )
        values = {name: os.environ.get(name, "").strip() for name in names}
        missing = [name for name in names if not values[name]]
        if missing:
            raise ProviderUnavailable("missing ACRCloud credentials: " + ", ".join(missing))
        return cls(
            host=values["ACRCLOUD_HOST"],
            access_key=values["ACRCLOUD_ACCESS_KEY"],
            access_secret=values["ACRCLOUD_ACCESS_SECRET"],
            container_id=values["ACRCLOUD_CONTAINER_ID"],
        )

    @property
    def base_url(self) -> str:
        value = self.host.rstrip("/")
        if not urlsplit(value).scheme:
            value = "https://" + value
        return value if value.endswith("/api") else value + "/api"


@dataclass(frozen=True)
class ACRCloudExecutionResult:
    observations: tuple[ObservationRecord, ...]
    response: dict[str, Any]
    billable_seconds: int
    usd_e2: int
    remote_ref: str


def billable_seconds(duration_ms: int) -> int:
    if duration_ms < 0:
        raise ValueError("duration cannot be negative")
    return (duration_ms + 999) // 1000


def cost_usd_e2(seconds: int) -> int:
    """Conservative integer-cent estimate at the plan's anecdotal $1.40/audio-hour."""

    if seconds < 0:
        raise ValueError("seconds cannot be negative")
    return (seconds * PRICE_PER_HOUR_USD_E2 + 3_599) // 3_600


def poll_delay_seconds(elapsed_seconds: int) -> int:
    """Resume the exponential schedule at the point implied by durable elapsed time."""

    if elapsed_seconds < 0:
        raise ValueError("poll elapsed time cannot be negative")
    delay = POLL_INITIAL_SECONDS
    scheduled = 0
    while delay < POLL_MAX_SECONDS and elapsed_seconds >= scheduled + delay:
        scheduled += delay
        delay = min(POLL_MAX_SECONDS, delay * 2)
    return delay


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


def _seconds_ms(value: Any) -> int:
    return _milliseconds(Decimal(str(value)) * 1000)


def _result_data(response: Mapping[str, Any]) -> Mapping[str, Any]:
    data = response.get("data")
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, Mapping):
        raise ProviderProtocolError("ACRCloud response data is not an object")
    return data


def _external_ids(result: Mapping[str, Any]) -> dict[str, Any]:
    ids: dict[str, Any] = {}
    if result.get("acrid") is not None:
        ids["acr"] = str(result["acrid"])
    external_ids = result.get("external_ids")
    if isinstance(external_ids, Mapping) and external_ids.get("isrc") is not None:
        ids["isrc"] = str(external_ids["isrc"])
    metadata = result.get("external_metadata")
    aliases = {"spotify": "spotify", "deezer": "deezer", "musicbrainz": "mb_recording"}
    if isinstance(metadata, Mapping):
        for source, namespace in aliases.items():
            block = metadata.get(source)
            if not isinstance(block, Mapping):
                continue
            track = block.get("track")
            if isinstance(track, Mapping) and track.get("id") is not None:
                ids[namespace] = str(track["id"])
    if result.get("audio_id") is not None:
        ids["audio_id"] = str(result["audio_id"])
    return ids


def _artist(result: Mapping[str, Any]) -> str | None:
    artists = result.get("artists")
    if isinstance(artists, list):
        names = [
            str(item["name"])
            for item in artists
            if isinstance(item, Mapping) and item.get("name") is not None
        ]
        if names:
            return ", ".join(names)
    return str(result["artist"]) if result.get("artist") is not None else None


def _album(result: Mapping[str, Any]) -> str | None:
    album = result.get("album")
    if isinstance(album, Mapping) and album.get("name") is not None:
        return str(album["name"])
    return str(album) if album is not None and not isinstance(album, Mapping) else None


def _chunk_indexes(results: Mapping[str, Any]) -> dict[int, int]:
    """Map each distinct scan-chunk offset (seconds) to its ordinal chunk index.

    The plan's scanner natural key is ``sha1(provider || chunk_index)``. ACRCloud reports one
    entry per matched bucket inside a chunk, so music and own-bucket hits at the same offset are
    *simultaneous sources of one logical trial*, distinguished by ``native.simultaneous_source``.
    """

    offsets: set[int] = set()
    for result_type in ("music", "custom_files"):
        entries = results.get(result_type) or []
        if isinstance(entries, list):
            for outer in entries:
                if isinstance(outer, Mapping):
                    offsets.add(max(0, _seconds_ms(outer.get("offset", 0))))
    return {offset: index for index, offset in enumerate(sorted(offsets))}


def parse_response(
    response: Mapping[str, Any],
    *,
    query: QueryRecord,
    media_key: str,
    duration_ms: int,
    raw_response_ref: str,
) -> tuple[ObservationRecord, ...]:
    """Parse ACRCloud music and own-bucket matches into final observations."""

    data = _result_data(response)
    results = data.get("results") or {}
    if not isinstance(results, Mapping):
        raise ProviderProtocolError("ACRCloud results is not an object")
    observations: list[ObservationRecord] = []
    native_index = 0
    chunk_indexes = _chunk_indexes(results)
    for result_type in ("music", "custom_files"):
        entries = results.get(result_type) or []
        if not isinstance(entries, list):
            raise ProviderProtocolError(f"ACRCloud {result_type} is not a list")
        for result_index, outer in enumerate(entries):
            if not isinstance(outer, Mapping) or not isinstance(outer.get("result"), Mapping):
                raise ProviderProtocolError(f"ACRCloud {result_type} entry is malformed")
            result = outer["result"]
            offset_ms = max(0, _seconds_ms(outer.get("offset", 0)))
            sample_begin = max(0, _milliseconds(result.get("sample_begin_time_offset_ms", 0)))
            fallback_length = max(0, _seconds_ms(outer.get("played_duration", 0)))
            sample_end = max(
                sample_begin,
                _milliseconds(result.get("sample_end_time_offset_ms", fallback_length)),
            )
            mix_start = min(duration_ms, offset_ms + sample_begin)
            mix_end = min(duration_ms, offset_ms + sample_end)
            if mix_end < mix_start:
                mix_end = mix_start
            has_paired_offsets = (
                result.get("sample_begin_time_offset_ms") is not None
                and result.get("db_begin_time_offset_ms") is not None
            )
            if has_paired_offsets:
                ref_anchor_ms = _milliseconds(result["db_begin_time_offset_ms"])
                anchor = Anchor(
                    mix_anchor_ms=mix_start,
                    ref_anchor_ms=ref_anchor_ms,
                    uncertainty_ms=0,
                    reliable=True,
                    method="acrcloud_sample_begin_to_db_begin",
                    bias_applied_ms=0,
                )
            elif result.get("play_offset_ms") is not None:
                anchor = Anchor(
                    mix_anchor_ms=offset_ms,
                    ref_anchor_ms=_milliseconds(result["play_offset_ms"]),
                    uncertainty_ms=max(1_000, sample_end - sample_begin),
                    reliable=False,
                    method="acrcloud_offset_to_play_offset_fallback",
                    bias_applied_ms=0,
                )
            else:
                anchor = None
            label = RawLabel(
                artist=_artist(result),
                title=str(result["title"]) if result.get("title") is not None else None,
                album=_album(result),
                label=str(result["label"]) if result.get("label") is not None else None,
                release_date=(
                    str(result["release_date"]) if result.get("release_date") is not None else None
                ),
            )
            score = _milliseconds(result["score"]) if result.get("score") is not None else None
            native = {
                "result_type": result_type,
                "result_index": result_index,
                "offset": canonicalize_provider_json(outer.get("offset", 0)),
                "played_duration": canonicalize_provider_json(outer.get("played_duration", 0)),
                "play_offset_ms": (
                    _milliseconds(result["play_offset_ms"])
                    if result.get("play_offset_ms") is not None
                    else None
                ),
                "sample_begin_time_offset_ms": (
                    _milliseconds(result["sample_begin_time_offset_ms"])
                    if result.get("sample_begin_time_offset_ms") is not None
                    else None
                ),
                "sample_end_time_offset_ms": (
                    _milliseconds(result["sample_end_time_offset_ms"])
                    if result.get("sample_end_time_offset_ms") is not None
                    else None
                ),
                "db_begin_time_offset_ms": (
                    _milliseconds(result["db_begin_time_offset_ms"])
                    if result.get("db_begin_time_offset_ms") is not None
                    else None
                ),
                "db_end_time_offset_ms": (
                    _milliseconds(result["db_end_time_offset_ms"])
                    if result.get("db_end_time_offset_ms") is not None
                    else None
                ),
                "score": score,
                "simultaneous_source": result_type,
                "result": canonicalize_provider_json(result),
            }
            label_hash = sha256(canonical_json_bytes(label)).hexdigest()
            natural = {
                "query_id": query.id,
                "mix_span_ms": [mix_start, mix_end],
                "raw_label_hash": label_hash,
                "native_index": native_index,
                "transform": None,
            }
            observations.append(
                ObservationRecord(
                    schema_version=SCHEMA_VERSION,
                    generated_by=GENERATED_BY,
                    id=make_id(
                        media_key, "observation", compose_natural_key("observation", natural)
                    ),
                    generation=query.generation,
                    query_id=query.id,
                    provider=PROVIDER,
                    capability="file_scanner",
                    status="match",
                    is_final=True,
                    mix_span_ms=(mix_start, mix_end),
                    support_ms=(mix_start, mix_end),
                    transform=None,
                    logical_trial_id=scanner_logical_trial_id(PROVIDER, chunk_indexes[offset_ms]),
                    raw_label=label,
                    provider_ids=_external_ids(result),
                    native=native,
                    anchor=anchor,
                    score_raw=score,
                    quality=None,
                    raw_response_ref=raw_response_ref,
                    source_ids=[f"query:{query.id}"],
                )
            )
            native_index += 1
    return tuple(sort_records(observations))


@dataclass
class ACRCloudAdapter:
    credentials: ACRCloudCredentials
    app_config: AppConfig
    cli_confirmation: bool
    transport: httpx.AsyncBaseTransport | None = None
    sleep: Sleep = asyncio.sleep
    now: Now = lambda: datetime.now(UTC)
    poll_timeout_seconds: int = POLL_TIMEOUT_SECONDS

    def _path(self, suffix: str = "") -> str:
        base = f"/fs-containers/{self.credentials.container_id}/files"
        return base + suffix

    def _headers(self) -> dict[str, str]:
        # The current File Scanning Console API is bearer authenticated. The task's mandated
        # ACRCLOUD_ACCESS_KEY variable therefore carries that developer access token; the secret
        # is still loaded from the environment for entitlement/configuration completeness, but is
        # never transmitted to this API.
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.credentials.access_key}",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        on_attempt: AttemptCallback,
        **kwargs: Any,
    ) -> dict[str, Any]:
        await on_attempt()
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(connect=30, write=1_800, read=1_800, pool=30),
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    self.credentials.base_url + path,
                    headers=self._headers(),
                    **kwargs,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AmbiguousProviderOutcome(f"ACRCloud {method} response was lost") from exc
        if response.status_code >= 400:
            raise ProviderProtocolError(f"ACRCloud HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderProtocolError("ACRCloud response was not JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderProtocolError("ACRCloud response root is not an object")
        return redact_value(payload)

    async def container(self, on_attempt: AttemptCallback) -> dict[str, Any]:
        path = f"/fs-containers/{self.credentials.container_id}"
        return await self._request("GET", path, on_attempt=on_attempt)

    async def reconcile(self, cache_key: str, on_attempt: AttemptCallback) -> str | None:
        """Adopt an exact same-name remote file before considering another upload."""

        path = self._path()
        page = 1
        while True:
            payload = await self._request(
                "GET",
                path,
                on_attempt=on_attempt,
                params={"page": page, "per_page": 100, "search": cache_key},
            )
            data = payload.get("data")
            if not isinstance(data, list):
                raise ProviderProtocolError("ACRCloud file listing is not a list")
            exact = [
                item for item in data if isinstance(item, Mapping) and item.get("name") == cache_key
            ]
            if exact:
                remote = min(exact, key=lambda item: str(item.get("id", ""))).get("id")
                if not remote:
                    raise ProviderProtocolError("ACRCloud listed file has no id")
                return str(remote)
            if len(data) < 100:
                return None
            page += 1

    async def submit(self, cache_key: str, asset_path: Path, on_attempt: AttemptCallback) -> str:
        require_upload_permission(self.app_config, self.cli_confirmation)
        if not path_is_file(asset_path):
            raise FileNotFoundError(asset_path)
        if path_size(asset_path) >= MAX_UPLOAD_BYTES:
            raise ValueError("ACRCloud upload must be smaller than 500 MB")
        with open(native_path(asset_path), "rb") as handle:
            payload = await self._request(
                "POST",
                self._path(),
                on_attempt=on_attempt,
                data={"data_type": "audio", "name": cache_key},
                files={"file": (cache_key, handle, "application/octet-stream")},
            )
        data = payload.get("data")
        if not isinstance(data, Mapping) or not data.get("id"):
            raise ProviderProtocolError("ACRCloud upload acknowledgement has no remote file id")
        return str(data["id"])

    async def poll(
        self,
        remote_ref: str,
        on_attempt: AttemptCallback,
        *,
        submitted_at: datetime | None = None,
    ) -> dict[str, Any]:
        submitted = submitted_at or self.now()
        if submitted.tzinfo is None:
            raise ValueError("submitted_at must be timezone-aware")
        deadline = submitted + timedelta(seconds=self.poll_timeout_seconds)
        elapsed = max(0, int((self.now() - submitted).total_seconds()))
        delay = poll_delay_seconds(elapsed)
        while True:
            if self.now() >= deadline:
                raise TimeoutError("ACRCloud poll exceeded 48 hours")
            payload = await self._request(
                "GET", self._path("/" + remote_ref), on_attempt=on_attempt
            )
            data = _result_data(payload)
            try:
                state = int(data.get("state", 0))
            except (TypeError, ValueError) as exc:
                raise ProviderProtocolError("ACRCloud file state is invalid") from exc
            if state in {1, -1}:
                return payload
            if state in {-2, -3}:
                raise ProviderProtocolError(f"ACRCloud scan failed with state {state}")
            if self.now() + timedelta(seconds=delay) > deadline:
                raise TimeoutError("ACRCloud poll exceeded 48 hours")
            await self.sleep(delay)
            delay = min(POLL_MAX_SECONDS, delay * 2)


def has_required_probe_fields(response: Mapping[str, Any]) -> bool:
    """Check fields promised by the selected file-scanning entitlement."""

    results = _result_data(response).get("results") or {}
    if not isinstance(results, Mapping):
        return False
    matches = results.get("music") or results.get("custom_files") or []
    if not isinstance(matches, list) or not matches:
        return False
    outer = matches[0]
    if not isinstance(outer, Mapping) or not isinstance(outer.get("result"), Mapping):
        return False
    inner = outer["result"]
    return all(
        value is not None
        for value in (
            outer.get("offset"),
            outer.get("played_duration"),
            inner.get("play_offset_ms"),
            inner.get("sample_begin_time_offset_ms"),
            inner.get("sample_end_time_offset_ms"),
            inner.get("db_begin_time_offset_ms"),
            inner.get("db_end_time_offset_ms"),
            inner.get("score"),
        )
    )


async def execute_job(
    *,
    store: AsyncJobStore,
    job: Job,
    owner: str,
    adapter: ACRCloudAdapter,
    query: QueryRecord,
    media_key: str,
    duration_ms: int,
    asset_path: Path,
    raw_path: Path,
    raw_response_ref: str,
    failure_hook: FailureHook | None = None,
) -> ACRCloudExecutionResult:
    """Reconcile/upload/persist/poll one file scan and safely resume by ``remote_ref``."""

    require_upload_permission(adapter.app_config, adapter.cli_confirmation)
    units = billable_seconds(duration_ms)
    reserved_cost = cost_usd_e2(units)
    heartbeat = asyncio.create_task(
        heartbeat_job(store, job.id, owner, interval_seconds=HEARTBEAT_SECONDS)
    )
    try:
        await store.reserve_billing(job.id, units=units, usd=reserved_cost)
        await store.submission_started(job.id, owner)
        hook = failure_hook or (lambda _point: None)
        remote_ref = job.remote_ref
        try:
            if remote_ref is None:
                hook("before_network")
                remote_ref = await adapter.reconcile(
                    query.cache_key,
                    lambda: store.begin_physical_attempt(job.id, reserve_request=False),
                )
            if remote_ref is None:
                try:
                    remote_ref = await adapter.submit(
                        query.cache_key,
                        asset_path,
                        lambda: store.begin_physical_attempt(job.id, reserve_request=False),
                    )
                except BaseException:
                    hook("during_upload")
                    raise
                hook("after_acceptance")
            await store.submitted(job.id, remote_ref)
            persisted = await store.get_job(job.id)
            assert persisted is not None and persisted.submitted_at is not None
            submitted_at = datetime.fromisoformat(persisted.submitted_at.replace("Z", "+00:00"))
            if persisted.submission_started_at is not None:
                # The start is a conservative fallback for the crash gap after remote acceptance
                # but before its acknowledgement timestamp can be committed.
                submission_started_at = datetime.fromisoformat(
                    persisted.submission_started_at.replace("Z", "+00:00")
                )
                submitted_at = min(submitted_at, submission_started_at)
            hook("after_remote_id_persistence")
            try:
                response = await adapter.poll(
                    remote_ref,
                    lambda: store.begin_physical_attempt(job.id, reserve_request=False),
                    submitted_at=submitted_at,
                )
            except BaseException:
                hook("during_polling")
                raise
        except AmbiguousProviderOutcome:
            # A persisted remote id is reconcilable: release_owner turns it back into a pollable
            # pending job. Without an id, startup/manual acknowledgement will list by cache_key.
            await store.release_owner(owner)
            raise
        except (ProviderProtocolError, TimeoutError) as exc:
            await store.finish(
                job.id,
                "permanent_failure",
                result_path=None,
                error=str(exc),
                actual_units=units,
                actual_usd=reserved_cost,
            )
            raise
        atomic_write_json(raw_path, canonicalize_provider_json(response))
        try:
            observations = parse_response(
                response,
                query=query,
                media_key=media_key,
                duration_ms=duration_ms,
                raw_response_ref=raw_response_ref,
            )
            state_value = int(_result_data(response).get("state", 0))
        except (ProviderProtocolError, TypeError, ValueError) as exc:
            await store.finish(
                job.id,
                "permanent_failure",
                result_path=None,
                error=str(exc),
                actual_units=units,
                actual_usd=reserved_cost,
            )
            raise ProviderProtocolError("ACRCloud final response was malformed") from exc
        state = "succeeded" if state_value == 1 and observations else "no_match"
        await store.finish(
            job.id,
            state,
            result_path=raw_response_ref,
            actual_units=units,
            actual_usd=reserved_cost,
        )
        return ACRCloudExecutionResult(
            observations, dict(response), units, reserved_cost, remote_ref
        )
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
