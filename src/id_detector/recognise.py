"""Generation-0 query materialisation, Shazam execution, cache, and observations."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    ObservationRecord,
    ProviderConfigRecord,
    QueryRecord,
    RawIndexEntry,
    RawLabel,
    WindowQueryTarget,
    clip_cache_key,
    compose_natural_key,
    make_id,
    sort_records,
)
from id_detector.io import (
    atomic_write_bytes,
    canonical_json_bytes,
    completion_sidecar_path,
    path_is_file,
    path_mtime,
    read_bytes,
    read_text,
    sha256_file,
    verify_completion_sidecar,
    write_completion_sidecar,
)
from id_detector.jobs import (
    DEFAULT_SHAZAM_MAX_REQUESTS,
    HEARTBEAT_SECONDS,
    AsyncJobStore,
    BudgetExhausted,
    Job,
)
from id_detector.shazam import (
    ShazamAdapter,
    ShazamHTTPError,
    canonicalize_provider_json,
    response_to_observation,
    retry_delay,
)
from id_detector.windows import WindowsResult

POSITIVE_MAX_AGE_SECONDS = 180 * 24 * 60 * 60
NO_MATCH_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
MAX_RETRIES = 5


@dataclass(frozen=True)
class RecognitionResult:
    queries: tuple[QueryRecord, ...]
    observations: tuple[ObservationRecord, ...]
    raw_index: tuple[RawIndexEntry, ...]
    queries_path: Path
    observations_path: Path
    raw_index_path: Path
    requests: int
    physical_attempts: int
    failures: int
    cache_hits: int


def _unmeasured_config() -> ProviderConfigRecord:
    version = "shazam-unmeasured.json"
    natural = {"provider": "shazam", "version": version}
    return ProviderConfigRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id("0" * 64, "provider_config", compose_natural_key("provider_config", natural)),
        provider="shazam",
        version=version,
        capability="clip_recognizer",
        measured=False,
        config={"library_retries": 0, "segment_duration_seconds": 12},
        adapter_bias_ms=None,
        adapter_bias_uncertainty_ms=None,
        L_min_ms=None,
        source_ids=[],
    )


def load_provider_configs(project_root: Path) -> tuple[ProviderConfigRecord, ...]:
    """Return every measured Shazam config found on disk or packaged, oldest version first.

    The last entry is the *active* config; earlier entries are superseded and may be reported,
    but rev 5.2 forbids using a superseded measurement as a gate.
    """

    directory = project_root / "provider_configs"
    measured: dict[int, str] = {}
    for path in directory.glob("shazam-v*.json") if directory.is_dir() else []:
        suffix = path.stem.removeprefix("shazam-v")
        if suffix.isdigit():
            measured[int(suffix)] = read_text(path)
    packaged = files("id_detector.resources.provider_configs")
    for item in packaged.iterdir():
        suffix = item.name.removesuffix(".json").removeprefix("shazam-v")
        if item.name.endswith(".json") and suffix.isdigit():
            measured.setdefault(int(suffix), item.read_text(encoding="utf-8"))
    if not measured:
        return (_unmeasured_config(),)
    return tuple(
        ProviderConfigRecord.model_validate_json(measured[version]) for version in sorted(measured)
    )


def load_provider_config(project_root: Path) -> tuple[ProviderConfigRecord, str]:
    config = load_provider_configs(project_root)[-1]
    return config, config.version


def build_queries(
    media_key: str,
    windows: WindowsResult,
    provider_config: ProviderConfigRecord,
    generation: int = 0,
) -> tuple[QueryRecord, ...]:
    queries: list[QueryRecord] = []
    seen_cache_keys: set[str] = set()
    for window in windows.records:
        target = WindowQueryTarget(window_id=window.id)
        cache_key = clip_cache_key(window.wav_sha256, "shazam", provider_config.version)
        if cache_key in seen_cache_keys:
            continue
        seen_cache_keys.add(cache_key)
        natural = {
            "provider": "shazam",
            "capability": "clip_recognizer",
            "target": target.model_dump(mode="json"),
            "provider_config_version": provider_config.version,
            "scan_policy": "single-window",
        }
        query_id = make_id(media_key, "query", compose_natural_key("query", natural))
        queries.append(
            QueryRecord(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                id=query_id,
                generation=generation,
                provider="shazam",
                capability="clip_recognizer",
                target=target,
                provider_config_version=provider_config.version,
                scan_policy="single-window",
                cache_key=cache_key,
            )
        )
    return tuple(sort_records(queries))


def _write_jsonl(path: Path, records: tuple[Any, ...] | list[Any]) -> None:
    content = b"\n".join(canonical_json_bytes(item) for item in records)
    _write_immutable_bytes(path, content + (b"\n" if content else b""))


def _write_immutable_bytes(path: Path, content: bytes) -> None:
    if path_is_file(path):
        if read_bytes(path) != content:
            raise FileExistsError(f"refusing to replace immutable artifact: {path}")
        return
    atomic_write_bytes(path, content)


def _write_immutable_json(path: Path, value: Any) -> None:
    _write_immutable_bytes(path, canonical_json_bytes(value))


def _write_immutable_sidecar(path: Path, upstream: dict[str, Path]) -> None:
    sidecar = completion_sidecar_path(path)
    if path_is_file(sidecar):
        if not verify_completion_sidecar(path, upstream).valid:
            raise FileExistsError(f"refusing to replace immutable sidecar: {sidecar}")
        return
    write_completion_sidecar(path, upstream)


def cache_valid(raw_path: Path, state: str) -> bool:
    """Apply the shared positive/no-match TTL contract; errors are never cacheable."""

    if not path_is_file(raw_path):
        return False
    age = max(0.0, time.time() - path_mtime(raw_path))
    if state == "succeeded":
        return age <= POSITIVE_MAX_AGE_SECONDS
    if state == "no_match":
        return age <= NO_MATCH_MAX_AGE_SECONDS
    return False


# Kept for Stage-1 callers while scanners share the public cache predicate above.
_cache_valid = cache_valid


def _raw_index_entry(
    media_key: str, query: QueryRecord, relative_path: str, raw_path: Path, status: str
) -> RawIndexEntry:
    natural = {"cache_key": query.cache_key}
    return RawIndexEntry(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(
            media_key,
            "raw_index_entry",
            compose_natural_key("raw_index_entry", natural),
        ),
        cache_key=query.cache_key,
        query_id=query.id,
        path=relative_path,
        sha256=sha256_file(raw_path),
        status=status,
        source_ids=[f"query:{query.id}"],
    )


def _error_observation(
    media_key: str,
    query: QueryRecord,
    window: Any,
    raw_response_ref: str,
) -> ObservationRecord:
    label = RawLabel(artist=None, title=None, album=None, label=None, release_date=None)
    label_hash = sha256(canonical_json_bytes(label)).hexdigest()
    natural = {
        "query_id": query.id,
        "mix_span_ms": list(window.support_ms),
        "raw_label_hash": label_hash,
        "native_index": 0,
        "transform": window.transform.model_dump(mode="json"),
    }
    return ObservationRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(media_key, "observation", compose_natural_key("observation", natural)),
        generation=query.generation,
        query_id=query.id,
        provider="shazam",
        capability="clip_recognizer",
        status="error",
        is_final=True,
        mix_span_ms=window.support_ms,
        support_ms=window.support_ms,
        transform=window.transform,
        logical_trial_id=window.logical_trial_id,
        raw_label=label,
        provider_ids={},
        native={"matches": []},
        anchor=None,
        score_raw=None,
        quality=None,
        raw_response_ref=raw_response_ref,
        source_ids=[f"query:{query.id}", f"window:{window.id}"],
    )


async def _heartbeat(store: AsyncJobStore, job_id: str, owner: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        await store.heartbeat(job_id, owner)


async def _run_job(
    *,
    store: AsyncJobStore,
    job: Job,
    query: QueryRecord,
    window: Any,
    adapter: ShazamAdapter,
    media_dir: Path,
    raw_dir: Path,
    owner: str,
) -> None:
    raw_path = raw_dir / f"{query.cache_key}.json"
    relative_path = raw_path.relative_to(media_dir).as_posix()
    heartbeat = asyncio.create_task(_heartbeat(store, job.id, owner))
    try:
        await store.submission_started(job.id, owner)
        response: dict[str, Any] | None = None
        last_error: Exception | None = None
        for retry_index in range(MAX_RETRIES + 1):
            try:
                response = await adapter.recognize_once(
                    media_dir / window.wav_path,
                    lambda: store.begin_physical_attempt(job.id),
                )
                break
            except ShazamHTTPError as exc:
                last_error = exc
                retryable = exc.status_code == 0 or exc.status_code == 429 or exc.status_code >= 500
                if not retryable or retry_index == MAX_RETRIES:
                    break
                await asyncio.sleep(retry_delay(retry_index, exc.retry_after))
            except BudgetExhausted as exc:
                last_error = exc
                break
            except Exception as exc:
                # Signature-generation failures happen before ``on_attempt`` and therefore do
                # not consume a request. They are deterministic for this WAV and are not retried.
                last_error = exc
                break
        if response is None:
            error_payload = {
                "error": type(last_error).__name__ if last_error else "unknown",
                "message": str(last_error)[:1000] if last_error else "recognition failed",
            }
            _write_immutable_json(raw_path, error_payload)
            await store.finish(
                job.id,
                "permanent_failure",
                result_path=relative_path,
                error=error_payload["message"],
            )
            return

        # The synchronous Shazam response is its acknowledgement. Commit that fact before any
        # downstream artifact work so a crash cannot silently submit the same job again.
        await store.submitted(job.id)
        _write_immutable_json(raw_path, canonicalize_provider_json(response))
        status = "succeeded" if response.get("matches") and response.get("track") else "no_match"
        await store.finish(job.id, status, result_path=relative_path)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def recognise_generation(
    *,
    media_key: str,
    media_dir: Path,
    windows: WindowsResult,
    project_root: Path,
    run_id: str,
    generation: int = 0,
    refresh: bool = False,
    max_requests: int = DEFAULT_SHAZAM_MAX_REQUESTS,
    adapter: ShazamAdapter | None = None,
) -> RecognitionResult:
    config, config_name = load_provider_config(project_root)
    queries = build_queries(media_key, windows, config, generation)
    invocation_key = sha256(run_id.encode("utf-8")).hexdigest()[:20]
    invocation_dir = media_dir / "recognise" / "invocations" / invocation_key
    raw_dir = invocation_dir / "raw"
    queries_path = invocation_dir / f"queries.gen{generation}.jsonl"
    observations_path = invocation_dir / f"observations.gen{generation}.jsonl"
    raw_index_path = invocation_dir / f"raw_index.gen{generation}.json"
    config_snapshot_path = invocation_dir / config_name
    _write_immutable_json(config_snapshot_path, config)
    _write_jsonl(queries_path, list(queries))
    _write_immutable_sidecar(
        queries_path,
        {
            windows.record_path.relative_to(media_dir).as_posix(): windows.record_path,
            f"provider_configs/{config_name}": config_snapshot_path,
        },
    )

    query_by_id = {query.id: query for query in queries}
    window_by_id = {window.id: window for window in windows.records}
    windows_by_cache: dict[str, list[Any]] = {}
    for window in windows.records:
        cache_key = clip_cache_key(window.wav_sha256, "shazam", config.version)
        windows_by_cache.setdefault(cache_key, []).append(window)
    adapter = adapter or ShazamAdapter(config)
    cache_hits = 0
    initial_physical = 0
    initial_by_query: dict[str, int] = {}
    async with AsyncJobStore(media_dir / "jobs.sqlite") as store:
        await store.ensure_budget(media_key, "shazam", max_requests=max_requests)
        # Cross-generation content cache. Window ids carry the generation, so a later generation
        # that happens to fingerprint byte-identical audio gets a *new* query id and would submit
        # it again. Reuse the earlier generation's stored raw response instead: the plan's cache
        # key is content-addressed, and one content must be submitted exactly once.
        cached_by_content: dict[str, tuple[str, str]] = {}
        for existing in await store.list_jobs():
            if existing.provider != "shazam" or existing.state not in {"succeeded", "no_match"}:
                continue
            if not existing.result_path:
                continue
            stored = media_dir / existing.result_path
            if cache_valid(stored, existing.state):
                cached_by_content.setdefault(
                    Path(existing.result_path).stem, (existing.result_path, existing.state)
                )
        for query in queries:
            job = await store.ensure_job(media_key, query.id, "shazam")
            initial_physical += job.physical_attempts
            initial_by_query[query.id] = job.physical_attempts
            raw_path = raw_dir / f"{query.cache_key}.json"
            cached_raw_path = media_dir / job.result_path if job.result_path else None
            if refresh and job.state in {
                "succeeded",
                "no_match",
                "retryable_failure",
                "permanent_failure",
            }:
                await store.reset_for_refresh(job.id)
            elif cached_raw_path is not None and cache_valid(cached_raw_path, job.state):
                _write_immutable_bytes(raw_path, read_bytes(cached_raw_path))
                cache_hits += 1
            elif job.state == "pending" and query.cache_key in cached_by_content:
                stored_path, stored_state = cached_by_content[query.cache_key]
                _write_immutable_bytes(raw_path, read_bytes(media_dir / stored_path))
                await store.finish(
                    job.id,
                    stored_state,
                    result_path=raw_path.relative_to(media_dir).as_posix(),
                )
                cache_hits += 1
            elif job.state in {"succeeded", "no_match", "permanent_failure"}:
                await store.reset_for_refresh(job.id)

        try:
            while True:
                job = await store.lease_next(
                    run_id,
                    media_key=media_key,
                    provider="shazam",
                    query_ids=frozenset(query_by_id),
                )
                if job is None:
                    break
                query = query_by_id[job.query_id]
                target = query.target
                window = window_by_id[target.window_id]
                await _run_job(
                    store=store,
                    job=job,
                    query=query,
                    window=window,
                    adapter=adapter,
                    media_dir=media_dir,
                    raw_dir=raw_dir,
                    owner=run_id,
                )
        except asyncio.CancelledError:
            await store.release_owner(run_id)
            raise

        jobs = await store.list_jobs()
        final_physical = sum(job.physical_attempts for job in jobs if job.query_id in query_by_id)
        requests = sum(
            job.query_id in query_by_id and job.physical_attempts > initial_by_query[job.query_id]
            for job in jobs
        )

    observations: list[ObservationRecord] = []
    raw_index: list[RawIndexEntry] = []
    failures = 0
    jobs_by_query = {job.query_id: job for job in jobs}
    for query in queries:
        job = jobs_by_query[query.id]
        raw_path = raw_dir / f"{query.cache_key}.json"
        relative_path = raw_path.relative_to(media_dir).as_posix()
        if job.state in {"succeeded", "no_match"} and path_is_file(raw_path):
            response = json.loads(read_text(raw_path))
            for window in windows_by_cache[query.cache_key]:
                observations.append(
                    response_to_observation(
                        response, query, window, config, relative_path, media_key
                    )
                )
            raw_index.append(
                _raw_index_entry(
                    media_key,
                    query,
                    relative_path,
                    raw_path,
                    "match" if response.get("matches") and response.get("track") else "no_match",
                )
            )
        else:
            failures += len(windows_by_cache[query.cache_key])
            if path_is_file(raw_path):
                observations.extend(
                    _error_observation(media_key, query, window, relative_path)
                    for window in windows_by_cache[query.cache_key]
                )
                raw_index.append(
                    _raw_index_entry(media_key, query, relative_path, raw_path, "error")
                )

    ordered_observations = tuple(sort_records(observations))
    ordered_index = tuple(sort_records(raw_index))
    _write_immutable_json(raw_index_path, list(ordered_index))
    _write_immutable_sidecar(
        raw_index_path,
        {entry.path: media_dir / entry.path for entry in ordered_index},
    )
    _write_jsonl(observations_path, list(ordered_observations))
    _write_immutable_sidecar(
        observations_path,
        {
            f"{queries_path.relative_to(media_dir).as_posix()}": queries_path,
            f"{raw_index_path.relative_to(media_dir).as_posix()}": raw_index_path,
            windows.record_path.relative_to(media_dir).as_posix(): windows.record_path,
        },
    )
    return RecognitionResult(
        queries=queries,
        observations=ordered_observations,
        raw_index=ordered_index,
        queries_path=queries_path,
        observations_path=observations_path,
        raw_index_path=raw_index_path,
        requests=requests,
        physical_attempts=final_physical - initial_physical,
        failures=failures,
        cache_hits=cache_hits,
    )


async def recognise_generation_zero(**kwargs: Any) -> RecognitionResult:
    """Backwards-compatible alias for the generation-0 call used before Stage 4c."""

    return await recognise_generation(generation=0, **kwargs)
