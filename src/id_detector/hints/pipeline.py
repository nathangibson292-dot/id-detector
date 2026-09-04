"""Non-blocking Stage 4a connector orchestration and immutable hint artefacts."""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from id_detector.contracts import HintRecord, SourceRecord
from id_detector.hints.connectors import mixcloud, mixesdb, pointer, soundcloud, tl1001, youtube
from id_detector.hints.connectors.base import (
    CircuitBreaker,
    ConnectorContext,
    ConnectorError,
    ConnectorOutput,
    RetryableConnectorError,
    read_output,
    write_output,
)
from id_detector.hints.connectors.manual import load as load_manual
from id_detector.hints.mirrors import MirrorMetadata, mirror_is_verified
from id_detector.hints.parse import HintInput, parse_hint_inputs, parse_text_units
from id_detector.hints.relations import apply_relations
from id_detector.io import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    redact_text,
    sha256_file,
    url_has_credentials,
    write_completion_sidecar,
)
from id_detector.jobs import AsyncJobStore

ConnectorCallable = Callable[[ConnectorContext], ConnectorOutput | Awaitable[ConnectorOutput]]
CONNECTOR_CACHE_VERSION = "stage4a-review-1"


@dataclass(frozen=True)
class HintRunResult:
    hints: tuple[HintRecord, ...]
    hints_path: Path
    status_path: Path
    statuses: tuple[dict[str, object], ...]
    tracklist_blocks: int
    quarantined_mirrors: tuple[str, ...]


def _jsonl(records: list[HintRecord]) -> bytes:
    content = b"\n".join(canonical_json_bytes(record) for record in records)
    return content + (b"\n" if content else b"")


def _extract_import_pointers(inputs: list[HintInput] | tuple[HintInput, ...]) -> list[str]:
    urls: set[str] = set()
    for item in inputs:
        for match in re.finditer(r"https://[^\s<>\]\[\)\(]+", item.text, re.IGNORECASE):
            url = match.group(0).rstrip(".,;:")
            try:
                pointer.validate_pointer_url(url)
            except ConnectorError:
                continue
            urls.add(url)
    return sorted(urls)


def _mirror_inputs(urls: tuple[str, ...], source_url: str) -> tuple[HintInput, ...]:
    return tuple(
        HintInput(
            connector="mixesdb",
            source_record_id=f"mirror-{sha256(url.encode('utf-8')).hexdigest()}",
            text=url,
            author_pseudo_id="mixesdb",
            mirror_of=source_url,
            mirror_status="quarantined",
        )
        for url in sorted(set(urls))
        if not url_has_credentials(url)
    )


def _connector_config_hash(connector: str, *, page_cap: int, item_cap: int) -> str:
    specific: dict[str, object] = {}
    if connector == "yt_comments":
        specific["extractor_args"] = youtube.COMMENT_ARGS
    elif connector == "pointer_import":
        specific.update(
            {
                "allowed_hosts": sorted(pointer.ALLOWED_HOSTS),
                "max_redirects": pointer.MAX_REDIRECTS,
                "max_bytes": pointer.MAX_BYTES,
                "wall_timeout_seconds": pointer.TIMEOUT_SECONDS,
            }
        )
    payload = {
        "cache_version": CONNECTOR_CACHE_VERSION,
        "connector": connector,
        "page_cap": page_cap,
        "item_cap": item_cap,
        "specific": specific,
    }
    return sha256(canonical_json_bytes(payload)).hexdigest()


def _shared_breaker(
    breakers: dict[tuple[str, str], CircuitBreaker], connector: str, target_url: str
) -> CircuitBreaker:
    host = (urlsplit(target_url).hostname or "local").casefold()
    return breakers.setdefault((connector, host), CircuitBreaker())


def _normalise_mirror_url(url: str) -> str:
    return url.rstrip("/")


def _release_mirror_quarantine(
    *,
    source: SourceRecord,
    duration_ms: int,
    hints: list[HintRecord],
    outputs: list[ConnectorOutput],
    confirmed_mirrors: tuple[str, ...],
) -> tuple[list[HintRecord], list[dict[str, object]], list[dict[str, object]]]:
    candidates = [output for output in outputs if output.mirror_candidate is not None]
    candidate_hint_ids: dict[str, set[str]] = {}
    all_candidate_ids: set[str] = set()
    for output in candidates:
        candidate = output.mirror_candidate
        assert candidate is not None
        source_ids = set(candidate.source_record_ids)
        candidate_inputs = [item for item in output.inputs if item.source_record_id in source_ids]
        ids = {
            item.id for item in parse_hint_inputs(source.media_key, duration_ms, candidate_inputs)
        }
        candidate_hint_ids[candidate.final_url] = ids
        all_candidate_ids.update(ids)

    by_id = {hint.id: hint for hint in hints}
    source_hints = [
        hint
        for hint in hints
        if hint.id not in all_candidate_ids and hint.mirror_status == "verified"
    ]
    confirmed = {_normalise_mirror_url(url) for url in confirmed_mirrors}
    releases: list[dict[str, object]] = []
    matched_confirmations: set[str] = set()
    for output in candidates:
        candidate = output.mirror_candidate
        assert candidate is not None
        aliases = {
            _normalise_mirror_url(candidate.requested_url),
            _normalise_mirror_url(candidate.final_url),
        }
        manually_confirmed = bool(aliases & confirmed)
        matched_confirmations.update(aliases & confirmed)
        ids = candidate_hint_ids[candidate.final_url]
        mirror_hints = [by_id[item_id] for item_id in sorted(ids) if item_id in by_id]
        verified = mirror_is_verified(
            source,
            source_duration_ms=duration_ms,
            mirror=MirrorMetadata(
                candidate.platform_id,
                candidate.uploader_id,
                candidate.upload_date,
                candidate.duration_ms or 0,
            ),
            source_hints=source_hints,
            mirror_hints=mirror_hints,
            manual_confirmation=manually_confirmed,
        )
        if not verified:
            continue
        for item_id in ids:
            if item_id in by_id:
                by_id[item_id] = by_id[item_id].model_copy(update={"mirror_status": "verified"})
        releases.append(
            {
                "url": candidate.final_url,
                "method": "manual" if manually_confirmed else "agreement",
                "hints_released": len(ids),
            }
        )
    confirmations = [
        {
            "url": url,
            "matched_import": _normalise_mirror_url(url) in matched_confirmations,
        }
        for url in sorted(confirmed_mirrors)
    ]
    return [by_id[hint.id] for hint in hints], releases, confirmations


async def _execute(
    *,
    store: AsyncJobStore,
    source: SourceRecord,
    duration_ms: int,
    media_dir: Path,
    cache_root: Path,
    client: httpx.AsyncClient,
    owner: str,
    connector: str,
    target_url: str,
    page_cap: int,
    item_cap: int,
    refresh: bool,
    callback: ConnectorCallable,
    input_content_sha256: str,
    configuration_sha256: str,
    breaker: CircuitBreaker,
) -> tuple[ConnectorOutput, dict[str, object], Path | None]:
    job = await store.ensure_connector_job(
        source.media_key,
        connector,
        target_url,
        page_cap=page_cap,
        item_cap=item_cap,
        input_content_sha256=input_content_sha256,
        configuration_sha256=configuration_sha256,
    )
    result_path = cache_root / connector / source.source_key / job.id / "result.json"
    if refresh and job.state in {
        "succeeded",
        "no_match",
        "retryable_failure",
        "permanent_failure",
        "cancelled",
    }:
        job = await store.reset_connector(job.id)
    if job.state in {"succeeded", "no_match"}:
        if result_path.is_file():
            output = read_output(result_path)
            status = {
                "connector": connector,
                "state": job.state,
                "items_fetched": output.items_fetched,
                "input_records": len(output.inputs),
                "hints_emitted": 0,
                "parse_success_rate_e4": 0,
                "tracklist_blocks": output.tracklist_blocks,
                "truncated": output.truncated,
                "error": None,
            }
            return output, status, result_path
        job = await store.reset_connector(job.id)
    if job.state not in {"pending", "retryable_failure"}:
        if job.state in {"permanent_failure", "cancelled"}:
            job = await store.reset_connector(job.id)
        else:
            raise ConnectorError(f"connector job is not runnable: {job.state}")
    leased = await store.lease_connector(job.id, owner)
    if leased is None:
        raise ConnectorError("connector job could not be leased")
    context = ConnectorContext(
        source=source,
        duration_ms=duration_ms,
        media_dir=media_dir,
        cache_root=cache_root,
        store=store,
        job=leased,
        owner=owner,
        http=client,
        breaker=breaker,
    )
    heartbeat: asyncio.Task[None] | None = None

    async def keep_alive() -> None:
        while True:
            await asyncio.sleep(15)
            await store.heartbeat_connector(job.id, owner)

    try:
        heartbeat = asyncio.create_task(keep_alive())
        value = callback(context)
        output = await value if inspect.isawaitable(value) else value
        write_output(result_path, output)
        current = await store.get_connector_job(job.id)
        assert current is not None
        if current.page == 0:
            await store.checkpoint_connector(
                job.id,
                owner,
                cursor=None,
                page=1,
                items_fetched=min(output.items_fetched, current.item_cap),
                result_path=str(result_path),
                truncated=output.truncated,
            )
        state = "succeeded" if output.inputs or output.mirrors or output.pointers else "no_match"
        final = await store.finish_connector(
            job.id,
            owner,
            state,
            result_path=str(result_path),
            truncated=output.truncated,
        )
        error = None
    except Exception as exc:
        message = redact_text(f"{type(exc).__name__}: {exc}")
        failure_state = (
            "retryable_failure" if isinstance(exc, RetryableConnectorError) else "permanent_failure"
        )
        final = await store.finish_connector(
            job.id,
            owner,
            failure_state,
            result_path=None,
            error=message,
        )
        output = ConnectorOutput()
        error = message
        result_path = None
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
    status = {
        "connector": connector,
        "state": final.state,
        "items_fetched": output.items_fetched,
        "input_records": len(output.inputs),
        "hints_emitted": 0,
        "parse_success_rate_e4": 0,
        "tracklist_blocks": output.tracklist_blocks,
        "truncated": bool(final.truncated),
        "error": error,
    }
    return output, status, result_path


def _parse_counts(
    source: SourceRecord,
    duration_ms: int,
    outputs: list[ConnectorOutput],
    status_outputs: list[ConnectorOutput],
    statuses: list[dict[str, object]],
) -> tuple[list[HintRecord], list[dict[str, object]]]:
    inputs = [item for output in outputs for item in output.inputs]
    hints = parse_hint_inputs(source.media_key, duration_ms, inputs)
    hints = apply_relations(source.media_key, duration_ms, hints, inputs)
    for status, output in zip(statuses, status_outputs, strict=True):
        successes = 0
        emitted = 0
        accepted_blocks = 0
        structured_lines = 0
        for item in output.inputs:
            units = parse_text_units(
                item.text,
                media_duration_ms=duration_ms,
                comment_timestamp_ms=item.position_ms,
                comment_position_kind=item.position_kind,
                structured_tracklist=item.structured_tracklist,
                enforce_block_acceptance=True,
            )
            successes += bool(units)
            emitted += len(units)
            tracklist_lines = sum(unit.kind == "tracklist_line" for unit in units)
            if item.structured_tracklist:
                structured_lines += tracklist_lines
            elif tracklist_lines >= 2:
                accepted_blocks += 1
        if structured_lines:
            accepted_blocks += 1
        status["hints_emitted"] = emitted
        status["parse_success_rate_e4"] = (
            successes * 10_000 // len(output.inputs) if output.inputs else 0
        )
        status["tracklist_blocks"] = accepted_blocks
    return hints, statuses


async def run_hints(
    *,
    source: SourceRecord,
    duration_ms: int,
    media_dir: Path,
    source_path: Path,
    project_root: Path,
    manual_tracklist: Path | None = None,
    confirmed_mirrors: tuple[str, ...] = (),
    refresh: bool = False,
    http: httpx.AsyncClient | None = None,
) -> HintRunResult:
    """Run platform primary flow; failures are recorded and never block audio recognition."""

    cache_root = project_root.resolve() / "data" / "local" / "hints"
    outputs: list[ConnectorOutput] = []
    status_outputs: list[ConnectorOutput] = []
    statuses: list[dict[str, object]] = []
    result_paths: list[Path] = []
    breakers: dict[tuple[str, str], CircuitBreaker] = {}
    owner = f"hints-{uuid.uuid4().hex}"
    source_content_sha256 = sha256(canonical_json_bytes(source)).hexdigest()
    validated_confirmations = tuple(
        sorted({pointer.validate_pointer_url(url) for url in confirmed_mirrors})
    )
    owned_client = http is None
    client = http or httpx.AsyncClient(
        timeout=httpx.Timeout(20, connect=10),
        headers={"User-Agent": "id-detector/0.1 (+local research tool)"},
    )

    async def execute(
        connector: str,
        target: str,
        callback: ConnectorCallable,
        *,
        page_cap: int = 1,
        item_cap: int = 5_000,
        input_content_sha256: str = source_content_sha256,
    ) -> ConnectorOutput:
        output, status, result_path = await _execute(
            store=store,
            source=source,
            duration_ms=duration_ms,
            media_dir=media_dir,
            cache_root=cache_root,
            client=client,
            owner=owner,
            connector=connector,
            target_url=target,
            page_cap=page_cap,
            item_cap=item_cap,
            refresh=refresh,
            callback=callback,
            input_content_sha256=input_content_sha256,
            configuration_sha256=_connector_config_hash(
                connector, page_cap=page_cap, item_cap=item_cap
            ),
            breaker=_shared_breaker(breakers, connector, target),
        )
        outputs.append(output)
        status_outputs.append(output)
        statuses.append(status)
        if result_path is not None:
            result_paths.append(result_path)
        return output

    store = AsyncJobStore(media_dir / "jobs.sqlite")
    store_started = False
    try:
        await store.start()
        store_started = True
        pointer_urls = set(validated_confirmations)
        if source.platform == "soundcloud":
            resolved_holder: dict[str, dict[str, object]] = {}

            async def resolve_soundcloud(context: ConnectorContext) -> ConnectorOutput:
                resolved_holder["value"] = await soundcloud.resolve_track(context)
                return ConnectorOutput(items_fetched=1)

            # The resolve prerequisite is itself durable and cached under the sc_comments family.
            await execute("sc_comments", source.canonical_url + "#resolve", resolve_soundcloud)
            mix = await execute("mixesdb", source.canonical_url, mixesdb.fetch)
            await execute(
                "sc_comments",
                source.canonical_url,
                lambda context: soundcloud.fetch_comments(context, resolved_holder.get("value")),
                page_cap=25,
            )
            await execute(
                "sc_description", source.canonical_url + "#description", soundcloud.description
            )
            if mix.mirrors:
                outputs.append(
                    ConnectorOutput(inputs=_mirror_inputs(mix.mirrors, source.canonical_url))
                )
        elif source.platform == "youtube":
            description_output = await execute(
                "yt_description", source.canonical_url + "#description", youtube.description
            )
            await execute("yt_chapters", source.canonical_url + "#chapters", youtube.chapters)
            comments_output = await execute(
                "yt_comments", source.canonical_url + "#comments", youtube.fetch_comments
            )
            pointer_urls.update(
                _extract_import_pointers((*description_output.inputs, *comments_output.inputs))
            )
            mix = await execute("mixesdb", source.canonical_url, mixesdb.fetch)
            if mix.mirrors:
                outputs.append(
                    ConnectorOutput(inputs=_mirror_inputs(mix.mirrors, source.canonical_url))
                )

        elif source.platform == "mixcloud":
            await execute("mixcloud_graphql", source.canonical_url, mixcloud.fetch)
            await execute(
                "mixcloud_description",
                source.canonical_url + "#description",
                soundcloud.description,
            )
            mix = await execute("mixesdb", source.canonical_url, mixesdb.fetch)
            if mix.mirrors:
                outputs.append(
                    ConnectorOutput(inputs=_mirror_inputs(mix.mirrors, source.canonical_url))
                )

        for url in sorted(pointer_urls):
            await execute(
                "pointer_import",
                url,
                lambda context, target=url: pointer.fetch(context, target),
                item_cap=1,
            )

        # Search is discovery only. Its results remain quarantined and are never fetched here.
        if source.platform in {"soundcloud", "youtube", "mixcloud"}:
            await execute("tl1001_search", source.canonical_url + "#title-search", tl1001.search)
        if manual_tracklist is not None:
            manual = manual_tracklist.resolve()
            await execute(
                "manual_tracklist",
                manual.as_uri(),
                lambda _context: load_manual(manual),
                input_content_sha256=sha256_file(manual),
            )
    finally:
        if store_started:
            await store.release_owner(owner)
            await store.close()
        if owned_client:
            await client.aclose()

    hints, statuses = _parse_counts(source, duration_ms, outputs, status_outputs, statuses)
    hints, mirror_releases, mirror_confirmations = _release_mirror_quarantine(
        source=source,
        duration_ms=duration_ms,
        hints=hints,
        outputs=outputs,
        confirmed_mirrors=validated_confirmations,
    )
    hints = sorted(hints, key=lambda item: item.id)
    hints_path = media_dir / "hints" / "hints.jsonl"
    status_path = media_dir / "hints" / "connector_status.json"
    quarantined = tuple(
        sorted(
            {
                hint.title or hint.raw_text
                for hint in hints
                if hint.mirror_status == "quarantined" and hint.kind == "pointer"
            }
        )
    )
    atomic_write_bytes(hints_path, _jsonl(hints))
    atomic_write_json(
        status_path,
        {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "media_key": source.media_key,
            "connectors": statuses,
            "counts_by_connector": dict(sorted(Counter(item.connector for item in hints).items())),
            "counts_by_kind": dict(sorted(Counter(item.kind for item in hints).items())),
            "tracklist_blocks": sum(int(status["tracklist_blocks"]) for status in statuses),
            "quarantined_mirrors": list(quarantined),
            "mirror_releases": mirror_releases,
            "mirror_confirmations": mirror_confirmations,
        },
    )
    upstream: dict[str, Path] = {"ingest/source.json": source_path}
    upstream.update(
        {
            f"local-cache/{path.parent.parent.parent.name}/{path.parent.name}/result.json": path
            for path in result_paths
        }
    )
    write_completion_sidecar(hints_path, upstream)
    write_completion_sidecar(status_path, upstream)
    return HintRunResult(
        hints=tuple(hints),
        hints_path=hints_path,
        status_path=status_path,
        statuses=tuple(statuses),
        tracklist_blocks=sum(int(status["tracklist_blocks"]) for status in statuses),
        quarantined_mirrors=quarantined,
    )
