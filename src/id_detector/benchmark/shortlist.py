"""Stage-3 independent per-engine shortlist benchmark."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from id_detector.benchmark.corpus import (
    _controlled_audio,
    _prediction_set,
    _run_controlled,
    _truth_files,
    _validate_source_media,
)
from id_detector.benchmark.scorer import (
    PredictionDocument,
    ScoringConfigSnapshot,
    score_corpus,
    truth_is_frozen_verified,
)
from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    BenchmarkCost,
    BenchmarkMetrics,
    GroundTruthRecord,
    RawIndexEntry,
    ShortlistEngine,
    ShortlistPairwiseAgreement,
    ShortlistReportRecord,
    compose_natural_key,
    make_id,
    sort_records,
)
from id_detector.decode import decode
from id_detector.fuse.episodes import fuse_generation_zero
from id_detector.ingest import ingest
from id_detector.io import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    read_bytes,
    read_text,
    sha256_file,
    write_completion_sidecar,
)
from id_detector.jobs import AsyncJobStore
from id_detector.local_fixture import PROVIDER_CONFIG_VERSION as LOCAL_FIXTURE_CONFIG_VERSION
from id_detector.providers.acrcloud import (
    PROVIDER_CONFIG_VERSION as ACRCLOUD_CONFIG_VERSION,
)
from id_detector.providers.acrcloud import (
    ACRCloudAdapter,
    ACRCloudCredentials,
)
from id_detector.providers.acrcloud import billable_seconds as acrcloud_billable_seconds
from id_detector.providers.acrcloud import build_query as build_acrcloud_query
from id_detector.providers.acrcloud import cost_usd_e2 as acrcloud_cost_usd_e2
from id_detector.providers.acrcloud import execute_job as execute_acrcloud_job
from id_detector.providers.acrcloud import parse_response as parse_acrcloud_response
from id_detector.providers.audd import PROVIDER_CONFIG_VERSION as AUDD_CONFIG_VERSION
from id_detector.providers.audd import AudDAdapter, AudDCredentials
from id_detector.providers.audd import billable_units as audd_billable_units
from id_detector.providers.audd import build_query as build_audd_query
from id_detector.providers.audd import cost_usd_e2 as audd_cost_usd_e2
from id_detector.providers.audd import execute_job as execute_audd_job
from id_detector.providers.audd import parse_response as parse_audd_response
from id_detector.providers.base import (
    AppConfig,
    ProviderProtocolError,
    ProviderUnavailable,
    UploadPermissionError,
)
from id_detector.recognise import (
    RecognitionResult,
    cache_valid,
    load_provider_config,
    recognise_generation_zero,
)
from id_detector.shazam import ShazamAdapter
from id_detector.windows import generate_windows


@dataclass(frozen=True)
class EngineRun:
    provider: str
    provider_config_version: str
    capability: str
    prediction_sets: tuple[dict[str, Any], ...]
    metrics: BenchmarkMetrics
    cost: BenchmarkCost
    observation_count: int
    match_count: int


@dataclass(frozen=True)
class ShortlistResult:
    report: ShortlistReportRecord
    prediction_paths: tuple[Path, ...]


EngineRunner = Callable[..., Awaitable[EngineRun]]


def _empty_cost() -> BenchmarkCost:
    return BenchmarkCost(requests=0, physical_attempts=0, billable_seconds=0, usd_e2=0, wall_ms=0)


def _add_costs(costs: list[BenchmarkCost]) -> BenchmarkCost:
    return BenchmarkCost(
        requests=sum(item.requests for item in costs),
        physical_attempts=sum(item.physical_attempts for item in costs),
        billable_seconds=sum(item.billable_seconds for item in costs),
        usd_e2=sum(item.usd_e2 for item in costs),
        wall_ms=sum(item.wall_ms for item in costs),
    )


def _config_snapshot(
    *, corpus_version: str, provider: str, provider_config_version: str, set_ids: list[str]
) -> ScoringConfigSnapshot:
    return ScoringConfigSnapshot(
        schema_version=SCHEMA_VERSION,
        config_version="stage-3-shortlist-v1",
        profile=f"shortlist-{provider}",
        bootstrap_seed=20_260_904,
        certification_targets=[],
        run_config={
            "corpus_version": corpus_version,
            "set_ids": sorted(set_ids),
            "provider": provider,
            "provider_config_version": provider_config_version,
            "execution": "whole-corpus-independent-no-cascading",
            "fuser": "baseline-fuser-v1",
        },
    )


def _score_engine(
    *,
    project_root: Path,
    corpus_dir: Path,
    corpus_version: str,
    provider: str,
    provider_config_version: str,
    prediction_sets: list[dict[str, Any]],
    cost: BenchmarkCost,
) -> tuple[BenchmarkMetrics, Path]:
    snapshot = _config_snapshot(
        corpus_version=corpus_version,
        provider=provider,
        provider_config_version=provider_config_version,
        set_ids=[item["set_id"] for item in prediction_sets],
    )
    config_hash = sha256(canonical_json_bytes(snapshot)).hexdigest()
    document = PredictionDocument(
        corpus_version=corpus_version,
        profile=snapshot.profile,
        config_hash=config_hash,
        config_snapshot=snapshot,
        sets=sorted(prediction_sets, key=lambda item: item["set_id"]),
        engines=[],
        cost=cost,
        unverified_seed_comparison=not truth_is_frozen_verified(
            corpus_dir,
            [
                GroundTruthRecord.model_validate_json(read_text(path))
                for path in _truth_files(corpus_dir, None)
            ],
        ),
    )
    path = (
        project_root
        / "data"
        / "local"
        / "benchmark"
        / corpus_version
        / "shortlist"
        / f"predictions-{provider}.json"
    )
    atomic_write_json(path, document)
    return score_corpus(corpus_dir, path).overall, path


async def _run_local_fixture(
    *,
    truths: list[GroundTruthRecord],
    project_root: Path,
    corpus_dir: Path,
    work_root: Path,
) -> EngineRun:
    predictions: list[dict[str, Any]] = []
    observations = matches = 0
    started = time.monotonic()
    for truth in truths:
        audio = _controlled_audio(project_root, truth.set_id)
        if audio is None:
            raise ValueError(f"local controlled audio is missing for {truth.set_id}")
        media_dir, fusion, _ = await _run_controlled(
            truth,
            audio,
            project_root=project_root,
            work_root=work_root / "local_fixture",
        )
        predictions.append(_prediction_set(truth.set_id, fusion, media_dir))
        observation_path = (
            media_dir / "recognise" / "invocations" / "local-fixture-v1" / "observations.gen0.jsonl"
        )
        records = [
            json.loads(line) for line in read_text(observation_path).splitlines() if line.strip()
        ]
        observations += len(records)
        matches += sum(item["status"] == "match" for item in records)
    cost = BenchmarkCost(
        requests=0,
        physical_attempts=0,
        billable_seconds=0,
        usd_e2=0,
        wall_ms=round((time.monotonic() - started) * 1000),
    )
    metrics, _ = _score_engine(
        project_root=project_root,
        corpus_dir=corpus_dir,
        corpus_version=truths[0].corpus_version,
        provider="local_fixture",
        provider_config_version=LOCAL_FIXTURE_CONFIG_VERSION,
        prediction_sets=predictions,
        cost=cost,
    )
    return EngineRun(
        "local_fixture",
        LOCAL_FIXTURE_CONFIG_VERSION,
        "clip_recognizer",
        tuple(predictions),
        metrics,
        cost,
        observations,
        matches,
    )


async def _run_shazam(
    *,
    truths: list[GroundTruthRecord],
    project_root: Path,
    corpus_dir: Path,
    work_root: Path,
    max_requests: int,
    refresh: bool = False,
) -> EngineRun:
    provider_config, provider_config_name = load_provider_config(project_root)
    adapter = ShazamAdapter(provider_config)
    predictions: list[dict[str, Any]] = []
    costs: list[BenchmarkCost] = []
    observations = matches = 0
    for truth in truths:
        audio = _controlled_audio(project_root, truth.set_id)
        if audio is None:
            raise ValueError(f"local controlled audio is missing for {truth.set_id}")
        started = time.monotonic()
        ingested = await ingest(str(audio), work_root / "shazam")
        decoded = await decode(ingested)
        _validate_source_media(
            truth,
            media_key=ingested.record.media_key,
            duration_ms=decoded.record.pcm.duration_ms,
        )
        windows = generate_windows(decoded, ingested.media_dir)
        recognised = await recognise_generation_zero(
            media_key=ingested.record.media_key,
            media_dir=ingested.media_dir,
            windows=windows,
            project_root=project_root,
            run_id=f"stage3-shortlist-shazam-{truth.set_id}",
            refresh=refresh,
            max_requests=max_requests,
            adapter=adapter,
        )
        fusion = fuse_generation_zero(
            media_key=ingested.record.media_key,
            media_dir=ingested.media_dir,
            duration_ms=decoded.record.pcm.duration_ms,
            observations=recognised.observations,
            observations_path=recognised.observations_path,
            windows=windows.records,
            windows_path=windows.record_path,
            pcm_path=decoded.record_path,
            profile="shortlist-shazam",
        )
        predictions.append(_prediction_set(truth.set_id, fusion, ingested.media_dir))
        observations += len(recognised.observations)
        matches += sum(item.status == "match" for item in recognised.observations)
        costs.append(
            BenchmarkCost(
                requests=recognised.requests,
                physical_attempts=recognised.physical_attempts,
                billable_seconds=0,
                usd_e2=0,
                wall_ms=round((time.monotonic() - started) * 1000),
            )
        )
    cost = _add_costs(costs)
    metrics, _ = _score_engine(
        project_root=project_root,
        corpus_dir=corpus_dir,
        corpus_version=truths[0].corpus_version,
        provider="shazam",
        provider_config_version=provider_config_name,
        prediction_sets=predictions,
        cost=cost,
    )
    return EngineRun(
        "shazam",
        provider_config_name,
        "clip_recognizer",
        tuple(predictions),
        metrics,
        cost,
        observations,
        matches,
    )


def _write_jsonl(path: Path, records: list[Any]) -> None:
    content = b"\n".join(canonical_json_bytes(item) for item in records)
    atomic_write_bytes(path, content + (b"\n" if content else b""))


async def _run_scanner_set(
    *,
    truth: GroundTruthRecord,
    project_root: Path,
    work_root: Path,
    provider: str,
    app_config: AppConfig,
    cli_confirmation: bool,
    refresh: bool = False,
    adapter_override: Any | None = None,
) -> tuple[dict[str, Any], RecognitionResult, BenchmarkCost]:
    audio = _controlled_audio(project_root, truth.set_id)
    if audio is None:
        raise ValueError(f"local controlled audio is missing for {truth.set_id}")
    started = time.monotonic()
    ingested = await ingest(str(audio), work_root / provider)
    decoded = await decode(ingested)
    _validate_source_media(
        truth,
        media_key=ingested.record.media_key,
        duration_ms=decoded.record.pcm.duration_ms,
    )
    windows = generate_windows(decoded, ingested.media_dir)
    asset_sha256 = ingested.record.original.sha256
    if provider == "audd":
        query = build_audd_query(
            media_key=ingested.record.media_key,
            asset_kind="original",
            asset_sha256=asset_sha256,
            scan_policy="all-12s-chunks-accurate-offsets",
        )
        units = audd_billable_units(decoded.record.pcm.duration_ms)
        expected_cost = audd_cost_usd_e2(units)
        billable = units * 12
        executor = execute_audd_job
        parser = parse_audd_response
    else:
        query = build_acrcloud_query(
            media_key=ingested.record.media_key,
            asset_kind="original",
            asset_sha256=asset_sha256,
            scan_policy="container-traverse",
        )
        units = acrcloud_billable_seconds(decoded.record.pcm.duration_ms)
        expected_cost = acrcloud_cost_usd_e2(units)
        billable = units
        executor = execute_acrcloud_job
        parser = parse_acrcloud_response
    invocation_dir = ingested.media_dir / "recognise" / "invocations" / f"shortlist-{provider}-v1"
    raw_path = invocation_dir / "raw" / f"{query.cache_key}.json"
    raw_ref = raw_path.relative_to(ingested.media_dir).as_posix()
    query_path = invocation_dir / "queries.gen0.jsonl"
    observation_path = invocation_dir / "observations.gen0.jsonl"
    raw_index_path = invocation_dir / "raw_index.json"
    _write_jsonl(query_path, [query])
    write_completion_sidecar(query_path, {"ingest/source.json": ingested.source_path})
    owner = uuid.uuid4().hex
    cache_hit = False
    request_count = 0
    run_billable = 0
    run_usd = 0
    async with AsyncJobStore(ingested.media_dir / "jobs.sqlite") as store:
        await store.ensure_budget(
            ingested.record.media_key,
            provider,
            max_requests=max(1, units),
            max_usd=max(1, expected_cost),
        )
        existing = await store.ensure_job(ingested.record.media_key, query.id, provider)
        initial_physical = existing.physical_attempts
        cached_raw_path = (
            ingested.media_dir / existing.result_path if existing.result_path else None
        )
        observations_out: tuple[Any, ...] | None = None
        if (
            not refresh
            and cached_raw_path is not None
            and cache_valid(cached_raw_path, existing.state)
        ):
            try:
                cached_response = json.loads(read_text(cached_raw_path))
                if not isinstance(cached_response, dict):
                    raise ProviderProtocolError("scanner cache root is not an object")
                observations_out = tuple(
                    sort_records(
                        parser(
                            cached_response,
                            query=query,
                            media_key=ingested.record.media_key,
                            duration_ms=decoded.record.pcm.duration_ms,
                            raw_response_ref=raw_ref,
                        )
                    )
                )
            except (json.JSONDecodeError, OSError, ProviderProtocolError, ValueError):
                observations_out = None
            else:
                if cached_raw_path.resolve() != raw_path.resolve():
                    atomic_write_bytes(raw_path, read_bytes(cached_raw_path))
                cache_hit = True
        terminal_states = {
            "succeeded",
            "no_match",
            "retryable_failure",
            "permanent_failure",
        }
        if observations_out is None and existing.state in terminal_states:
            # A new paid execution is needed because the cache expired, was malformed, is an
            # error (never cacheable), or --refresh was explicit. Add exactly one run's ceiling.
            await store.extend_budget(
                ingested.record.media_key,
                provider,
                requests=units,
                usd=expected_cost,
            )
            await store.reset_for_refresh(existing.id)
        if observations_out is None:
            adapter: Any = adapter_override
            if adapter is None and provider == "audd":
                adapter = AudDAdapter(AudDCredentials.from_env(), app_config, cli_confirmation)
            elif adapter is None:
                adapter = ACRCloudAdapter(
                    ACRCloudCredentials.from_env(), app_config, cli_confirmation
                )
            job = await store.lease_next(
                owner,
                media_key=ingested.record.media_key,
                provider=provider,
                query_ids=frozenset({query.id}),
            )
            if job is None:
                raise RuntimeError(f"{provider} scanner job is not runnable")
            execution = await executor(
                store=store,
                job=job,
                owner=owner,
                adapter=adapter,
                query=query,
                media_key=ingested.record.media_key,
                duration_ms=decoded.record.pcm.duration_ms,
                asset_path=audio,
                raw_path=raw_path,
                raw_response_ref=raw_ref,
            )
            observations_out = tuple(sort_records(execution.observations))
            request_count = 1
            run_billable = billable
            run_usd = execution.usd_e2
        final_job = await store.get_job(existing.id)
        assert final_job is not None
    _write_jsonl(observation_path, list(observations_out))
    raw_index = RawIndexEntry(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(
            ingested.record.media_key,
            "raw_index_entry",
            compose_natural_key("raw_index_entry", {"cache_key": query.cache_key}),
        ),
        cache_key=query.cache_key,
        query_id=query.id,
        path=raw_ref,
        sha256=sha256_file(raw_path),
        status="match" if any(item.status == "match" for item in observations_out) else "no_match",
        source_ids=[f"query:{query.id}"],
    )
    atomic_write_json(raw_index_path, [raw_index])
    write_completion_sidecar(raw_index_path, {raw_ref: raw_path})
    write_completion_sidecar(
        observation_path,
        {
            query_path.relative_to(ingested.media_dir).as_posix(): query_path,
            raw_index_path.relative_to(ingested.media_dir).as_posix(): raw_index_path,
        },
    )
    recognised = RecognitionResult(
        queries=(query,),
        observations=observations_out,
        raw_index=(raw_index,),
        queries_path=query_path,
        observations_path=observation_path,
        raw_index_path=raw_index_path,
        requests=request_count,
        physical_attempts=final_job.physical_attempts - initial_physical,
        failures=0,
        cache_hits=int(cache_hit),
    )
    fusion = fuse_generation_zero(
        media_key=ingested.record.media_key,
        media_dir=ingested.media_dir,
        duration_ms=decoded.record.pcm.duration_ms,
        observations=observations_out,
        observations_path=observation_path,
        windows=windows.records,
        windows_path=windows.record_path,
        pcm_path=decoded.record_path,
        profile=f"shortlist-{provider}",
    )
    cost = BenchmarkCost(
        requests=request_count,
        physical_attempts=final_job.physical_attempts - initial_physical,
        billable_seconds=run_billable,
        usd_e2=run_usd,
        wall_ms=round((time.monotonic() - started) * 1000),
    )
    return _prediction_set(truth.set_id, fusion, ingested.media_dir), recognised, cost


async def _run_scanner(
    *,
    provider: str,
    truths: list[GroundTruthRecord],
    project_root: Path,
    corpus_dir: Path,
    work_root: Path,
    app_config: AppConfig,
    cli_confirmation: bool,
    refresh: bool = False,
) -> EngineRun:
    predictions: list[dict[str, Any]] = []
    costs: list[BenchmarkCost] = []
    observations = matches = 0
    for truth in truths:
        prediction, recognised, cost = await _run_scanner_set(
            truth=truth,
            project_root=project_root,
            work_root=work_root,
            provider=provider,
            app_config=app_config,
            cli_confirmation=cli_confirmation,
            refresh=refresh,
        )
        predictions.append(prediction)
        observations += len(recognised.observations)
        matches += sum(item.status == "match" for item in recognised.observations)
        costs.append(cost)
    config_version = AUDD_CONFIG_VERSION if provider == "audd" else ACRCLOUD_CONFIG_VERSION
    cost = _add_costs(costs)
    metrics, _ = _score_engine(
        project_root=project_root,
        corpus_dir=corpus_dir,
        corpus_version=truths[0].corpus_version,
        provider=provider,
        provider_config_version=config_version,
        prediction_sets=predictions,
        cost=cost,
    )
    return EngineRun(
        provider,
        config_version,
        "file_scanner",
        tuple(predictions),
        metrics,
        cost,
        observations,
        matches,
    )


def _normalise_work(artist: str, title: str) -> str:
    return " ".join(f"{artist}|{title}".casefold().split())


def _work_sets(run: EngineRun) -> dict[str, set[str]]:
    return {
        item["set_id"]: {
            _normalise_work(episode["work"]["artist"], episode["work"]["title"])
            for episode in item["episodes"]
        }
        for item in run.prediction_sets
    }


def _pairwise(runs: list[EngineRun]) -> list[ShortlistPairwiseAgreement]:
    results: list[ShortlistPairwiseAgreement] = []
    for index, left in enumerate(runs):
        left_sets = _work_sets(left)
        for right in runs[index + 1 :]:
            right_sets = _work_sets(right)
            set_ids = sorted(set(left_sets) | set(right_sets))
            intersection = union = 0
            for set_id in set_ids:
                left_values = left_sets.get(set_id, set())
                right_values = right_sets.get(set_id, set())
                intersection += len(left_values & right_values)
                union += len(left_values | right_values)
            agreement = 10_000 if union == 0 else intersection * 10_000 // union
            results.append(
                ShortlistPairwiseAgreement(
                    provider_a=left.provider,
                    provider_b=right.provider,
                    n_sets=len(set_ids),
                    agreement_e4=agreement,
                )
            )
    return results


def _union_coverage(truths: list[GroundTruthRecord], runs: list[EngineRun]) -> int:
    # Coalesce the same fused occurrence across engines, while retaining its temporal evidence.
    predicted: dict[str, dict[tuple[str, int], list[tuple[int, int]]]] = {}
    for run in runs:
        for item in run.prediction_sets:
            occurrences = predicted.setdefault(item["set_id"], {})
            for episode in item["episodes"]:
                key = (
                    _normalise_work(episode["work"]["artist"], episode["work"]["title"]),
                    int(episode["occurrence_index"]),
                )
                occurrences.setdefault(key, []).extend(
                    (int(span[0]), int(span[1])) for span in episode["evidence_support_ms"]
                )
    total = covered = 0
    for truth in truths:
        occurrences = predicted.get(truth.set_id, {})
        unused = set(occurrences)
        for episode in sorted(
            truth.episodes,
            key=lambda item: (item.start_ms_range[0], item.occurrence_index),
        ):
            total += 1
            work = _normalise_work(episode.work.artist, episode.work.title)
            hull_start = max(0, episode.start_ms_range[0] - 30_000)
            hull_end = episode.end_ms_range[1] + 30_000
            compatible = sorted(
                (
                    key
                    for key in unused
                    if key[0] == work
                    and any(
                        start <= hull_end and end >= hull_start for start, end in occurrences[key]
                    )
                ),
                key=lambda key: (min(start for start, _ in occurrences[key]), key[1]),
            )
            if compatible:
                covered += 1
                unused.remove(compatible[0])
    return covered * 10_000 // total if total else 0


def _not_evaluated(
    *,
    provider: str,
    capability: str,
    config_version: str,
    status: str,
    expected_cost: int,
) -> ShortlistEngine:
    return ShortlistEngine(
        provider=provider,
        capability=capability,
        provider_config_version=config_version,
        status=status,
        set_count=0,
        observation_count=0,
        match_count=0,
        oracle_coverage_e4=0,
        metrics=None,
        cost=_empty_cost(),
        expected_trial_cost_usd_e2=expected_cost,
    )


async def run_shortlist(
    *,
    corpus_version: str,
    out_path: Path,
    project_root: Path,
    work_root: Path,
    app_config: AppConfig,
    cli_confirmation: bool,
    max_requests: int = 2_000,
    refresh: bool = False,
    engine_runners: Mapping[str, EngineRunner] | None = None,
) -> ShortlistResult:
    """Run each available engine independently over every controlled corpus set."""

    corpus_dir = project_root / "data" / "corpus" / corpus_version
    truths = [
        GroundTruthRecord.model_validate_json(read_text(path))
        for path in _truth_files(corpus_dir, None)
    ]
    if not truths or any("controlled" not in item.stratum.casefold() for item in truths):
        raise ValueError("Stage 3 shortlist currently requires a fully controlled corpus")
    if any(item.corpus_version != corpus_version for item in truths):
        raise ValueError("truth corpus_version differs from requested corpus")
    expected_audd = sum(audd_cost_usd_e2(audd_billable_units(t.source.duration_ms)) for t in truths)
    expected_acr = sum(
        acrcloud_cost_usd_e2(acrcloud_billable_seconds(t.source.duration_ms)) for t in truths
    )
    runners = dict(engine_runners or {})
    local_runner = runners.get("local_fixture", _run_local_fixture)
    shazam_runner = runners.get("shazam", _run_shazam)
    runs = [
        await local_runner(
            truths=truths,
            project_root=project_root,
            corpus_dir=corpus_dir,
            work_root=work_root,
        ),
        await shazam_runner(
            truths=truths,
            project_root=project_root,
            corpus_dir=corpus_dir,
            work_root=work_root,
            max_requests=max_requests,
            refresh=refresh,
        ),
    ]
    engines: list[ShortlistEngine] = []
    for run in runs:
        engines.append(
            ShortlistEngine(
                provider=run.provider,
                capability=run.capability,
                provider_config_version=run.provider_config_version,
                status=(
                    "evaluated (fixture oracle; not a production engine)"
                    if run.provider == "local_fixture"
                    else "evaluated"
                ),
                set_count=len(run.prediction_sets),
                observation_count=run.observation_count,
                match_count=run.match_count,
                oracle_coverage_e4=run.metrics.identification_work.recall_e4,
                metrics=run.metrics,
                cost=run.cost,
                expected_trial_cost_usd_e2=0,
            )
        )
    for provider, config_version, expected in (
        ("audd", AUDD_CONFIG_VERSION, expected_audd),
        ("acrcloud", ACRCLOUD_CONFIG_VERSION, expected_acr),
    ):
        try:
            scanner_runner = runners.get(provider)
            if scanner_runner is None:
                run = await _run_scanner(
                    provider=provider,
                    truths=truths,
                    project_root=project_root,
                    corpus_dir=corpus_dir,
                    work_root=work_root,
                    app_config=app_config,
                    cli_confirmation=cli_confirmation,
                    refresh=refresh,
                )
            else:
                run = await scanner_runner(
                    provider=provider,
                    truths=truths,
                    project_root=project_root,
                    corpus_dir=corpus_dir,
                    work_root=work_root,
                    app_config=app_config,
                    cli_confirmation=cli_confirmation,
                    refresh=refresh,
                )
        except ProviderUnavailable:
            engines.append(
                _not_evaluated(
                    provider=provider,
                    capability="file_scanner",
                    config_version=config_version,
                    status="not_evaluated (no credentials)",
                    expected_cost=expected,
                )
            )
        except UploadPermissionError:
            engines.append(
                _not_evaluated(
                    provider=provider,
                    capability="file_scanner",
                    config_version=config_version,
                    status="not_evaluated (upload permission required)",
                    expected_cost=expected,
                )
            )
        else:
            runs.append(run)
            engines.append(
                ShortlistEngine(
                    provider=provider,
                    capability="file_scanner",
                    provider_config_version=config_version,
                    status="evaluated",
                    set_count=len(run.prediction_sets),
                    observation_count=run.observation_count,
                    match_count=run.match_count,
                    oracle_coverage_e4=run.metrics.identification_work.recall_e4,
                    metrics=run.metrics,
                    cost=run.cost,
                    expected_trial_cost_usd_e2=expected,
                )
            )
    engines.append(
        _not_evaluated(
            provider="panako",
            capability="local_index_query",
            config_version="panako-v1.json",
            status="excluded (JDK not found; pending owner's JDK decision)",
            expected_cost=0,
        )
    )
    union = _union_coverage(truths, runs)
    total_cost = _add_costs([run.cost for run in runs])
    report = ShortlistReportRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        corpus_version=corpus_version,
        engines=engines,
        pairwise_agreement=_pairwise(runs),
        union_coverage_e4=union,
        oracle_coverage_e4=union,
        cost=total_cost,
        reference_pool_status="excluded_from_v1_pending_owner_jdk_decision",
        notes=[
            "All evaluated engines ran independently over the whole corpus; no cascade was used.",
            "local_fixture is a controlled-corpus oracle and is not a production recognizer.",
            "Union and oracle coverage are identical at this Stage-3 work-occurrence shortlist.",
            "Paid estimates use 150 cents/hour for AudD and 140 cents/hour for ACRCloud.",
        ],
    )
    atomic_write_json(out_path, report)
    prediction_paths = tuple(
        project_root
        / "data"
        / "local"
        / "benchmark"
        / corpus_version
        / "shortlist"
        / f"predictions-{run.provider}.json"
        for run in runs
    )
    return ShortlistResult(report, prediction_paths)
