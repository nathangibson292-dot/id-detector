"""The local reference-index (Panako) query stage — the crowd/DJ-owned-tracks lever.

``build-index`` fingerprints an uploader's OWN tracks (including unreleased ones Shazam has never
heard) into a local Panako index.  This stage is the query half: after the free fuse, it takes the
still-uncertain spans, runs the matching window clips against that index, and turns Panako's hits
into ``local_index_query`` observations that re-fuse alongside Shazam's.  It is the only lever that
can recover a track no public catalogue holds.

Entirely local — no network, no cost — so the only guards are index existence and the JDK/jar being
present; a missing index or runtime is a clean skip.  Each window's raw Panako output is cached by
``(clip, index_id, index_version)`` so a repeat analysis never re-runs the JVM for it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    ObservationRecord,
    QueryRecord,
    RawLabel,
    WindowQueryTarget,
    WindowRecord,
    compose_natural_key,
    local_index_cache_key,
    make_id,
    sort_records,
)
from id_detector.io import atomic_write_bytes, path_is_file, read_text
from id_detector.providers.base import ProviderUnavailable
from id_detector.providers.panako import (
    CAPABILITY_NAME,
    PROVIDER,
    PROVIDER_CONFIG_VERSION,
    PanakoError,
    PanakoIndexPaths,
    PanakoProvider,
    PanakoRuntime,
    QueryWindow,
    normalise_matches,
    parse_query_output,
)
from id_detector.providers.panako_setup import jar_path
from id_detector.recognise import _write_jsonl
from id_detector.scan import PaidScanResult
from id_detector.scan_targeting import Span
from id_detector.windows import WindowsResult

DEFAULT_INDEX_ROOT = Path("data/local/panako-db")
DEFAULT_TOOL_DIR = Path("data/local/panako")
#: Ceiling on windows queried per analysis (each is a JVM launch — bounds wall-clock, not cost).
DEFAULT_MAX_WINDOWS = 400

LogFn = Callable[[str], None]


def _resource_labels(manifest: Mapping[str, object]) -> dict[str, RawLabel]:
    """resource_id -> label, so a Panako hit carries the uploader/title, not a bare file stem."""

    labels: dict[str, RawLabel] = {}
    for entry in manifest.get("resources", []) or []:
        if not isinstance(entry, Mapping):
            continue
        resource_id = entry.get("resource_id")
        if not isinstance(resource_id, str):
            continue
        labels[resource_id] = RawLabel(
            artist=entry.get("uploader") if isinstance(entry.get("uploader"), str) else None,
            title=entry.get("title") if isinstance(entry.get("title"), str) else None,
            album=None,
            label=None,
            release_date=None,
        )
    return labels


def _index_query(
    media_key: str, window: WindowRecord, index_id: str, index_version: str
) -> QueryRecord:
    target = WindowQueryTarget(window_id=window.id)
    natural = {
        "provider": PROVIDER,
        "capability": CAPABILITY_NAME,
        "target": target.model_dump(mode="json"),
        "provider_config_version": PROVIDER_CONFIG_VERSION,
        "scan_policy": "single-window-local-index",
        "index_id": index_id,
        "index_version": index_version,
    }
    return QueryRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(media_key, "query", compose_natural_key("query", natural)),
        generation=0,
        provider=PROVIDER,
        capability=CAPABILITY_NAME,
        target=target,
        provider_config_version=PROVIDER_CONFIG_VERSION,
        scan_policy="single-window-local-index",
        cache_key=local_index_cache_key(window.wav_sha256, index_id, index_version),
    )


def _windows_in_targets(windows: WindowsResult, targets: tuple[Span, ...]) -> list[WindowRecord]:
    selected: list[WindowRecord] = []
    seen: set[str] = set()
    for window in windows.records:
        if window.transform.type != "none":
            continue
        start = window.support_ms[0]
        if not any(lo <= start < hi for lo, hi in targets):
            continue
        if window.wav_sha256 in seen:
            continue
        seen.add(window.wav_sha256)
        selected.append(window)
    selected.sort(key=lambda w: w.support_ms[0])
    return selected


async def run_local_index_recognition(
    *,
    media_key: str,
    media_dir: Path,
    windows: WindowsResult,
    targets: tuple[Span, ...],
    duration_ms: int,
    run_id: str,
    index_label: str,
    index_root: Path = DEFAULT_INDEX_ROOT,
    tool_dir: Path = DEFAULT_TOOL_DIR,
    max_windows: int = DEFAULT_MAX_WINDOWS,
    provider: PanakoProvider | None = None,
    log: LogFn | None = None,
) -> PaidScanResult:
    """Query the uncertain-region window clips against a local Panako index and return observations.

    ``provider`` injects a fake for tests.  A missing index, jar, or JDK is a recorded skip, never a
    raise.  Cross-run cached raw output means the JVM runs at most once per (clip, index version).
    """

    emit: LogFn = log or (lambda _message: None)
    if not targets:
        return PaidScanResult()
    paths = PanakoIndexPaths(root=index_root / index_label)
    if not path_is_file(paths.manifest_path):
        emit(f"local index '{index_label}' not found at {paths.manifest_path.parent}; skipped")
        return PaidScanResult(skipped=(("panako", "no index built — run build-index first"),))
    manifest = json.loads(read_text(paths.manifest_path))
    index_id = str(manifest.get("index_id", ""))
    index_version = str(manifest.get("index_version", ""))
    labels = _resource_labels(manifest)

    if provider is None:
        try:
            runtime = PanakoRuntime.resolve(jar=jar_path(tool_dir))
        except ProviderUnavailable as exc:
            emit(f"panako skipped: {exc}")
            return PaidScanResult(skipped=(("panako", str(exc)),))
        provider = PanakoProvider(runtime=runtime, paths=paths)

    selected = _windows_in_targets(windows, tuple(targets))[:max_windows]
    if not selected:
        return PaidScanResult()

    cache_dir = media_dir / "recognise" / "invocations" / "local-panako-v1" / "raw"
    invocation_dir = media_dir / "recognise" / "invocations" / f"local-panako-{run_id[:12]}"
    observations: list[ObservationRecord] = []
    queries: list[QueryRecord] = []
    queried = 0

    for chunk_index, window in enumerate(selected):
        query = _index_query(media_key, window, index_id, index_version)
        queries.append(query)
        raw_path = cache_dir / f"{query.cache_key}.txt"
        raw_ref = raw_path.relative_to(media_dir).as_posix()
        if path_is_file(raw_path):
            raw_text: str | None = read_text(raw_path)
        else:
            wav = media_dir / window.wav_path
            try:
                result, _matches = await provider.query_wav(wav)
                queried += 1
            except (PanakoError, FileNotFoundError, OSError) as exc:
                emit(f"panako query error @{window.support_ms[0] // 1000}s: {type(exc).__name__}")
                continue
            raw_text = result.stdout + result.stderr
            atomic_write_bytes(raw_path, raw_text.encode("utf-8"))
        # Re-parse from the cached/queried text so cache hits and fresh runs go the same path.
        matches = parse_query_output(raw_text)
        query_window = QueryWindow(
            window_id=window.id,
            start_ms=window.support_ms[0],
            wav_sha256=window.wav_sha256,
            chunk_index=chunk_index,
        )
        observations.extend(
            normalise_matches(
                matches,
                query=query,
                media_key=media_key,
                window=query_window,
                duration_ms=duration_ms,
                raw_response_ref=raw_ref,
                resource_labels=labels,
            )
        )

    observations_out = tuple(sort_records(observations))
    observation_path = invocation_dir / "observations.gen0.jsonl"
    _write_jsonl(observation_path, list(observations_out))
    _write_jsonl(invocation_dir / "queries.gen0.jsonl", queries)
    matched = sum(item.status == "match" for item in observations_out)
    emit(
        f"panako: {matched} match(es) across {len(selected)} uncertain windows "
        f"({queried} newly queried, {len(selected) - queried} cached)"
    )
    return PaidScanResult(
        observations=observations_out,
        observation_paths=(observation_path,),
        engines_run=(PROVIDER,),
    )
