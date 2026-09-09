"""The live paid file-scanner stage: run AudD / ACRCloud once over the whole mix.

.. warning::
   CORRECTED POST-KEY PLAN — do NOT extend this whole-file mode.  Uploading the entire mix to
   AudD's *enterprise file scanner* is why the ownership-consent gate exists, and that makes the
   paid engine useless for its actual use case: analysing OTHER people's published mixes, which you
   don't own.  The free engine already sends short ~12 s CLIPS to Shazam with NO consent gate, so
   the correct paid design is the same shape:

     * Recognise CLIPS via AudD's standard recognition API (``api.audd.io``), NOT the whole-file
       ``enterprise.audd.io`` endpoint — send only the ~12 s windows of the UNCERTAIN regions that
       :func:`id_detector.scan_targeting.select_scan_targets` already picks out.
     * DROP ``require_upload_permission`` for that clip mode: a clip to a paid recogniser is no
       different from the Shazam clip we already send gate-free (keep the gate only for a genuine
       whole-file upload, which we will not use).
     * The paid clip observations feed the SAME fuser as Shazam's, so they recover Shazam-missed
       tracks and let a paid disagreement demote a Shazam phantom.
   This whole-file path stays only as the tested scaffolding until a key exists to build + verify
   the clip path live.  See the ``paid-engine-activation`` project memory.

Shazam (the free ``clip_recognizer``) sweeps overlapping windows; this (interim) whole-file mode has
the paid engines scan the entire file in one durable submission and return positioned
``file_scanner`` observations that enter the *same* fuser as clip observations (see
:mod:`id_detector.fuse.scanners`).  Because a whole-file scan is not re-run per rescan generation,
these observations are injected once as the generation loop's static ``extra_observations``.

Three independent gates must ALL hold before a paid engine runs; any missing gate skips just that
engine (with a logged reason) and never fails the free analysis:

1. the active profile lists the engine in ``enabled_engines`` (only ``max_accuracy`` does),
2. the provider's credentials are present in the environment (``AUDD_API_TOKEN`` etc.), and
3. both third-party-upload consent gates are satisfied
   (``allow_third_party_upload = true`` in config AND the per-run confirmation) — see
   :func:`id_detector.providers.base.require_upload_permission`.

Secrets are read only from the environment and every persisted response is redacted by the
provider adapter before it is written.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    ObservationRecord,
    QueryRecord,
    RawIndexEntry,
    compose_natural_key,
    make_id,
    sort_records,
)
from id_detector.io import (
    atomic_write_bytes,
    atomic_write_json,
    read_bytes,
    read_text,
    sha256_file,
    write_completion_sidecar,
)
from id_detector.jobs import AsyncJobStore
from id_detector.paid_clip import PaidScanResult
from id_detector.providers import acrcloud as acrcloud_mod
from id_detector.providers import audd as audd_mod
from id_detector.providers.base import (
    AppConfig,
    ProviderProtocolError,
    ProviderUnavailable,
    UploadPermissionError,
    require_upload_permission,
)
from id_detector.recognise import _write_jsonl, cache_valid

#: Provider names that are paid whole-file scanners (the profile's ``enabled_engines`` may list
#: these alongside the free ``shazam`` engine).
PAID_FILE_SCANNERS: tuple[str, ...] = ("audd", "acrcloud")

LogFn = Callable[[str], None]


@dataclass(frozen=True)
class _Bundle:
    """The provider-specific hooks the otherwise-uniform scan path needs."""

    provider: str
    config_version: str
    scan_policy: str
    build_query: Callable[..., QueryRecord]
    billable: Callable[[int], int]
    cost: Callable[[int], int]
    execute: Callable[..., Awaitable[Any]]
    parse: Callable[..., tuple[ObservationRecord, ...]]
    make_adapter: Callable[[AppConfig, bool], Any]


def _audd_adapter(config: AppConfig, confirmation: bool) -> Any:
    return audd_mod.AudDAdapter(audd_mod.AudDCredentials.from_env(), config, confirmation)


def _acrcloud_adapter(config: AppConfig, confirmation: bool) -> Any:
    return acrcloud_mod.ACRCloudAdapter(
        acrcloud_mod.ACRCloudCredentials.from_env(), config, confirmation
    )


_BUNDLES: dict[str, _Bundle] = {
    "audd": _Bundle(
        provider="audd",
        config_version=audd_mod.PROVIDER_CONFIG_VERSION,
        scan_policy="all-12s-chunks-accurate-offsets",
        build_query=audd_mod.build_query,
        billable=audd_mod.billable_units,
        cost=audd_mod.cost_usd_e2,
        execute=audd_mod.execute_job,
        parse=audd_mod.parse_response,
        make_adapter=_audd_adapter,
    ),
    "acrcloud": _Bundle(
        provider="acrcloud",
        config_version=acrcloud_mod.PROVIDER_CONFIG_VERSION,
        scan_policy="container-traverse",
        build_query=acrcloud_mod.build_query,
        billable=acrcloud_mod.billable_seconds,
        cost=acrcloud_mod.cost_usd_e2,
        execute=acrcloud_mod.execute_job,
        parse=acrcloud_mod.parse_response,
        make_adapter=_acrcloud_adapter,
    ),
}

_TERMINAL_STATES = frozenset({"succeeded", "no_match", "retryable_failure", "permanent_failure"})


async def run_paid_scanners(
    *,
    media_key: str,
    media_dir: Path,
    asset_path: Path,
    asset_sha256: str,
    asset_kind: str,
    duration_ms: int,
    source_path: Path,
    app_config: AppConfig,
    enabled_engines: tuple[str, ...] | list[str],
    cli_confirmation: bool,
    refresh: bool = False,
    adapters: Mapping[str, Any] | None = None,
    log: LogFn | None = None,
) -> PaidScanResult:
    """Run every enabled+credentialed+consented paid scanner and collect their observations.

    ``adapters`` lets a test inject a fake provider adapter (bypassing the network) keyed by
    provider name; production leaves it ``None`` and each adapter is built from environment
    credentials.  Never raises for an unavailable/unconsented engine — it is skipped and recorded.
    """

    emit: LogFn = log or (lambda _message: None)
    requested = [name for name in enabled_engines if name in PAID_FILE_SCANNERS]
    if not requested:
        return PaidScanResult()

    # Consent is one decision for the whole run; check it once so an unconsented max_accuracy run
    # still completes on the free path with a clear reason rather than failing.
    try:
        require_upload_permission(app_config, cli_confirmation)
    except UploadPermissionError as exc:
        reason = str(exc)
        for name in requested:
            emit(f"paid engine {name} skipped: {reason}")
        return PaidScanResult(skipped=tuple((name, reason) for name in requested))

    observations: list[ObservationRecord] = []
    paths: list[Path] = []
    engines_run: list[str] = []
    skipped: list[tuple[str, str]] = []
    total_usd = 0
    for name in requested:
        bundle = _BUNDLES[name]
        override = adapters.get(name) if adapters else None
        try:
            engine_observations, path, usd = await _scan_one(
                bundle=bundle,
                override=override,
                app_config=app_config,
                cli_confirmation=cli_confirmation,
                media_key=media_key,
                media_dir=media_dir,
                asset_path=asset_path,
                asset_sha256=asset_sha256,
                asset_kind=asset_kind,
                duration_ms=duration_ms,
                source_path=source_path,
                refresh=refresh,
            )
        except ProviderUnavailable as exc:
            # Credentials are only needed on a cache miss, so this is reached lazily: the engine is
            # enabled and consented but has no key set — skip it, keep the free result.
            reason = str(exc)
            emit(f"paid engine {name} skipped: {reason}")
            skipped.append((name, reason))
            continue
        except (ProviderProtocolError, RuntimeError, OSError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            emit(f"paid engine {name} failed: {reason}")
            skipped.append((name, reason))
            continue
        observations.extend(engine_observations)
        paths.append(path)
        engines_run.append(name)
        total_usd += usd
        matched = sum(item.status == "match" for item in engine_observations)
        emit(f"paid engine {name}: {matched} match(es) across {len(engine_observations)} chunk(s)")

    return PaidScanResult(
        observations=tuple(sort_records(observations)),
        observation_paths=tuple(paths),
        engines_run=tuple(engines_run),
        skipped=tuple(skipped),
        usd_e2=total_usd,
    )


async def _scan_one(
    *,
    bundle: _Bundle,
    override: Any | None,
    app_config: AppConfig,
    cli_confirmation: bool,
    media_key: str,
    media_dir: Path,
    asset_path: Path,
    asset_sha256: str,
    asset_kind: str,
    duration_ms: int,
    source_path: Path,
    refresh: bool,
) -> tuple[tuple[ObservationRecord, ...], Path, int]:
    """Run one scanner via the durable job store, persist its artefacts, return observations.

    Faithful to ``benchmark.shortlist._run_scanner_set`` — the proven cache/lease/execute path —
    but reusing the mix already ingested by ``_analyse`` and writing under a distinct ``live-``
    invocation directory so it never collides with a shortlist evaluation.
    """

    query = bundle.build_query(
        media_key=media_key,
        asset_kind=asset_kind,
        asset_sha256=asset_sha256,
        scan_policy=bundle.scan_policy,
    )
    units = bundle.billable(duration_ms)
    expected_cost = bundle.cost(units)
    invocation_dir = media_dir / "recognise" / "invocations" / f"live-{bundle.provider}-v1"
    raw_path = invocation_dir / "raw" / f"{query.cache_key}.json"
    raw_ref = raw_path.relative_to(media_dir).as_posix()
    query_path = invocation_dir / "queries.gen0.jsonl"
    observation_path = invocation_dir / "observations.gen0.jsonl"
    raw_index_path = invocation_dir / "raw_index.json"
    _write_jsonl(query_path, [query])
    write_completion_sidecar(query_path, {"ingest/source.json": source_path})

    owner = uuid.uuid4().hex
    run_usd = 0
    async with AsyncJobStore(media_dir / "jobs.sqlite") as store:
        await store.ensure_budget(
            media_key,
            bundle.provider,
            max_requests=max(1, units),
            max_usd=max(1, expected_cost),
        )
        existing = await store.ensure_job(media_key, query.id, bundle.provider)
        cached_raw_path = media_dir / existing.result_path if existing.result_path else None
        observations_out: tuple[ObservationRecord, ...] | None = None
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
                        bundle.parse(
                            cached_response,
                            query=query,
                            media_key=media_key,
                            duration_ms=duration_ms,
                            raw_response_ref=raw_ref,
                        )
                    )
                )
            except (ValueError, OSError, ProviderProtocolError):
                observations_out = None
            else:
                if cached_raw_path.resolve() != raw_path.resolve():
                    atomic_write_bytes(raw_path, read_bytes(cached_raw_path))
        if observations_out is None and existing.state in _TERMINAL_STATES:
            # The cache expired, was malformed, was an (uncacheable) error, or --refresh forced a
            # re-run: add exactly one run's ceiling back to the budget and reset the job.
            await store.extend_budget(media_key, bundle.provider, requests=units, usd=expected_cost)
            await store.reset_for_refresh(existing.id)
        if observations_out is None:
            # Build the adapter only now (a cache hit needs no credentials): production reads the
            # key from the environment; a test injects a MockTransport-backed adapter.
            adapter = (
                override
                if override is not None
                else bundle.make_adapter(app_config, cli_confirmation)
            )
            job = await store.lease_next(
                owner,
                media_key=media_key,
                provider=bundle.provider,
                query_ids=frozenset({query.id}),
            )
            if job is None:
                raise RuntimeError(f"{bundle.provider} scanner job is not runnable")
            execution = await bundle.execute(
                store=store,
                job=job,
                owner=owner,
                adapter=adapter,
                query=query,
                media_key=media_key,
                duration_ms=duration_ms,
                asset_path=asset_path,
                raw_path=raw_path,
                raw_response_ref=raw_ref,
            )
            observations_out = tuple(sort_records(execution.observations))
            run_usd = execution.usd_e2

    _write_jsonl(observation_path, list(observations_out))
    raw_index = RawIndexEntry(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(
            media_key,
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
            query_path.relative_to(media_dir).as_posix(): query_path,
            raw_index_path.relative_to(media_dir).as_posix(): raw_index_path,
        },
    )
    return observations_out, observation_path, run_usd
