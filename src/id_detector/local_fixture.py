"""Content-hash recorded-response recognizer for controlled synthetic renders."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    Anchor,
    GroundTruthRecord,
    ObservationRecord,
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
    atomic_write_json,
    canonical_json_bytes,
    sha256_file,
    write_completion_sidecar,
)
from id_detector.recognise import RecognitionResult
from id_detector.windows import WindowsResult

PROVIDER_CONFIG_VERSION = "local_fixture-v1.json"


@dataclass(frozen=True)
class FixtureHit:
    episode_index: int
    artist: str
    title: str
    recording_id: str
    ref_anchor_ms: int


def _overlap(left: tuple[int, int], right: tuple[int, int]) -> int:
    return max(0, min(left[1], right[1]) - max(left[0], right[0]))


def _reference_progress(set_id: str, elapsed_ms: int) -> int:
    elapsed_ms = max(0, elapsed_ms)
    if "loop" in set_id:
        return elapsed_ms % 5_000
    if "cue-jump" in set_id:
        return elapsed_ms if elapsed_ms < 10_000 else 35_000 + elapsed_ms
    if "repeated-section" in set_id:
        if elapsed_ms < 5_000:
            return elapsed_ms
        if elapsed_ms < 10_000:
            return 15_000 + elapsed_ms - 5_000
        return (elapsed_ms - 10_000) % 5_000
    if "drifting-tempo" in set_id:
        rates = (9_400, 9_800, 10_200, 10_600)
        progress = 0
        remaining = elapsed_ms
        for rate in rates:
            section = min(5_000, remaining)
            progress += section * rate // 10_000
            remaining -= section
            if remaining <= 0:
                break
        return progress
    return elapsed_ms


def _hits_for_window(
    truth: GroundTruthRecord,
    support: tuple[int, int],
    source_offset_ms: int,
) -> list[FixtureHit]:
    hits: list[FixtureHit] = []
    for index, episode in enumerate(truth.episodes):
        audible = (
            sum(episode.start_ms_range) // 2,
            sum(episode.end_ms_range) // 2,
        )
        if _overlap(support, audible) < min(2_000, audible[1] - audible[0]):
            continue
        recording_id = episode.version.ids.get("mb_recording")
        if recording_id is None:
            recording_id = "synthetic:" + sha256(f"{truth.set_id}:{index}".encode()).hexdigest()
        elapsed = support[0] - audible[0]
        hits.append(
            FixtureHit(
                episode_index=index,
                artist=episode.work.artist,
                title=episode.work.title,
                recording_id=recording_id,
                ref_anchor_ms=source_offset_ms + _reference_progress(truth.set_id, elapsed),
            )
        )
    return hits


def build_recorded_response_map(
    *,
    truth: GroundTruthRecord,
    windows: WindowsResult,
    source_offset_ms: int,
) -> dict[str, tuple[FixtureHit, ...]]:
    """Build the immutable response map for an already validated controlled source."""

    recorded: dict[str, tuple[FixtureHit, ...]] = {}
    for window in windows.records:
        hits = tuple(_hits_for_window(truth, window.support_ms, source_offset_ms))
        previous = recorded.setdefault(window.wav_sha256, hits)
        if previous != hits:
            raise ValueError("equal controlled window hashes map to different recorded responses")
    return recorded


def _query(media_key: str, window: Any) -> QueryRecord:
    target = WindowQueryTarget(window_id=window.id)
    cache_key = clip_cache_key(window.wav_sha256, "local_fixture", PROVIDER_CONFIG_VERSION)
    natural = {
        "provider": "local_fixture",
        "capability": "clip_recognizer",
        "target": target.model_dump(mode="json"),
        "provider_config_version": PROVIDER_CONFIG_VERSION,
        "scan_policy": "content-hash-recorded-response",
    }
    return QueryRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(media_key, "query", compose_natural_key("query", natural)),
        generation=0,
        provider="local_fixture",
        capability="clip_recognizer",
        target=target,
        provider_config_version=PROVIDER_CONFIG_VERSION,
        scan_policy="content-hash-recorded-response",
        cache_key=cache_key,
    )


def _observation(
    *,
    media_key: str,
    query: QueryRecord,
    window: Any,
    hit: FixtureHit | None,
    native_index: int,
    raw_response_ref: str,
) -> ObservationRecord:
    label = RawLabel(
        artist=hit.artist if hit else None,
        title=hit.title if hit else None,
        album="Controlled Fixture" if hit else None,
        label="Local" if hit else None,
        release_date=None,
    )
    label_hash = sha256(canonical_json_bytes(label)).hexdigest()
    natural = {
        "query_id": query.id,
        "mix_span_ms": list(window.support_ms),
        "raw_label_hash": label_hash,
        "native_index": native_index,
    }
    provider_ids = {}
    native: dict[str, Any] = {"matches": []}
    anchor = None
    status = "no_match"
    source_ids = [f"query:{query.id}", f"window:{window.id}"]
    if hit is not None:
        status = "match"
        provider_ids = {
            "mb_recording": hit.recording_id,
            "isrc": "FIX" + sha256(hit.recording_id.encode("utf-8")).hexdigest()[:9].upper(),
        }
        native = {
            "matches": [
                {"offset_ms": hit.ref_anchor_ms, "frequencyskew_e6": 40, "timeskew_e6": 30}
            ],
            "simultaneous_source": f"stem-{hit.episode_index:02d}",
            "content_sha256": window.wav_sha256,
        }
        anchor = Anchor(
            mix_anchor_ms=window.support_ms[0],
            ref_anchor_ms=hit.ref_anchor_ms,
            uncertainty_ms=25,
            reliable=True,
            method="local_fixture_content_hash",
            bias_applied_ms=0,
        )
        source_ids.append(f"controlled-truth:{hit.episode_index}")
    return ObservationRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(media_key, "observation", compose_natural_key("observation", natural)),
        generation=0,
        query_id=query.id,
        provider="local_fixture",
        capability="clip_recognizer",
        status=status,
        is_final=True,
        mix_span_ms=window.support_ms,
        support_ms=window.support_ms,
        transform=window.transform,
        logical_trial_id=window.logical_trial_id,
        raw_label=label,
        provider_ids=provider_ids,
        native=native,
        anchor=anchor,
        score_raw=10_000 if hit else None,
        quality=10_000 if hit else None,
        raw_response_ref=raw_response_ref,
        source_ids=source_ids,
    )


def recognise_controlled_fixture(
    *,
    media_key: str,
    media_dir: Path,
    windows: WindowsResult,
    recorded_responses: Mapping[str, tuple[FixtureHit, ...]],
) -> RecognitionResult:
    """Materialise deterministic recorded responses keyed by each exact window WAV hash."""

    # Recognition consults only a response map built from the validated expected source. Unknown
    # or mutated window content therefore cannot acquire truth-derived labels.
    windows_by_hash: dict[str, list[Any]] = {}
    for window in windows.records:
        windows_by_hash.setdefault(window.wav_sha256, []).append(window)

    invocation_dir = media_dir / "recognise" / "invocations" / "local-fixture-v1"
    raw_dir = invocation_dir / "raw"
    queries_path = invocation_dir / "queries.gen0.jsonl"
    observations_path = invocation_dir / "observations.gen0.jsonl"
    raw_index_path = invocation_dir / "raw_index.json"
    queries: list[QueryRecord] = []
    observations: list[ObservationRecord] = []
    raw_index: list[RawIndexEntry] = []
    for content_hash in sorted(windows_by_hash):
        grouped_windows = windows_by_hash[content_hash]
        query = _query(media_key, grouped_windows[0])
        queries.append(query)
        hits = recorded_responses.get(content_hash, ())
        raw_path = raw_dir / f"{query.cache_key}.json"
        raw_ref = raw_path.relative_to(media_dir).as_posix()
        atomic_write_json(
            raw_path,
            {
                "provider": "local_fixture",
                "provider_config_version": PROVIDER_CONFIG_VERSION,
                "content_sha256": content_hash,
                "hits": [hit.__dict__ for hit in hits],
            },
        )
        if hits:
            for window in grouped_windows:
                observations.extend(
                    _observation(
                        media_key=media_key,
                        query=query,
                        window=window,
                        hit=hit,
                        native_index=index,
                        raw_response_ref=raw_ref,
                    )
                    for index, hit in enumerate(hits)
                )
        else:
            observations.extend(
                _observation(
                    media_key=media_key,
                    query=query,
                    window=window,
                    hit=None,
                    native_index=0,
                    raw_response_ref=raw_ref,
                )
                for window in grouped_windows
            )
        raw_index.append(
            RawIndexEntry(
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
                status="match" if hits else "no_match",
                source_ids=[f"query:{query.id}"],
            )
        )
    ordered_queries = tuple(sort_records(queries))
    ordered_observations = tuple(sort_records(observations))
    ordered_index = tuple(sort_records(raw_index))
    query_bytes = b"\n".join(canonical_json_bytes(item) for item in ordered_queries) + b"\n"
    observation_bytes = (
        b"\n".join(canonical_json_bytes(item) for item in ordered_observations) + b"\n"
    )
    atomic_write_bytes(queries_path, query_bytes)
    write_completion_sidecar(queries_path, {"windows/windows.gen0.jsonl": windows.record_path})
    atomic_write_json(raw_index_path, list(ordered_index))
    write_completion_sidecar(
        raw_index_path, {item.path: media_dir / item.path for item in ordered_index}
    )
    atomic_write_bytes(observations_path, observation_bytes)
    write_completion_sidecar(
        observations_path,
        {
            queries_path.relative_to(media_dir).as_posix(): queries_path,
            raw_index_path.relative_to(media_dir).as_posix(): raw_index_path,
            "windows/windows.gen0.jsonl": windows.record_path,
        },
    )
    return RecognitionResult(
        queries=ordered_queries,
        observations=ordered_observations,
        raw_index=ordered_index,
        queries_path=queries_path,
        observations_path=observations_path,
        raw_index_path=raw_index_path,
        requests=0,
        physical_attempts=0,
        failures=0,
        cache_hits=len(ordered_queries),
    )
