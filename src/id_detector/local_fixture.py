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
    Transform,
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

# The fixture catalogue holds one *decoy* release next to the controlled truth: an unlicensed
# rate edit of the same performance, pressed 12.5 % fast.  A hypothesis whose residual temporal
# *and* frequency rate both land within tolerance of that ratio recognise the decoy instead of
# the true recording, exactly as a rate-shifted bootleg would be matched by a real catalogue.
# Without it the harness's false-match predicate is unsatisfiable (Stage 4b review, P1).
#
# 11_250 = 10_800 / 9_600 is chosen deliberately: no *untransformed* query of any corpus set can
# reach it (the largest untransformed residual is 10_800), so the decoy is reachable only by a
# wrong transform hypothesis and the ``off`` arm stays clean by construction, not by accident.
DECOY_RESIDUAL_RATE_E4 = 11_250
DECOY_ARTIST = "Decoy Bootlegs"
DECOY_TITLE = "Unlicensed Rate Edit"
DECOY_RECORDING_ID = "decoy:stage4b-rate-edit"
DECOY_SOURCE_KEY = "stem-decoy"
DECOY_SOURCE_ID = "controlled-decoy:rate-edit"


@dataclass(frozen=True)
class FixtureHit:
    episode_index: int
    artist: str
    title: str
    recording_id: str
    ref_anchor_ms: int
    # The mix time of the *first matched sample*, which is what a real provider's offset pairs
    # with. For a window that begins before the track becomes audible this is the audible start,
    # not the window start; anchoring at the window start instead would pair a moving mix time
    # with a stationary reference time and manufacture a zero-slope segment.
    mix_anchor_ms: int = 0
    frequencyskew_e6: int = 0
    timeskew_e6: int = 0
    is_decoy: bool = False


def _overlap(left: tuple[int, int], right: tuple[int, int]) -> int:
    return max(0, min(left[1], right[1]) - max(left[0], right[0]))


# Stage 4c event replicates. Each renders exactly one discontinuity, so its reference behaviour
# is a two- or three-piece function of the elapsed mix time inside the episode.
EV_ONSET_MS = 10_000
EV_LOOP_BACK_MS = 5_000
EV_JUMP_MS = 40_000
EV_DRIFT_RATE_E4 = 11_500


def _event_case(set_id: str) -> str | None:
    for name in ("ev-loop", "ev-jump", "ev-drift", "ev-replay"):
        if f"-{name}-" in set_id:
            return name
    return None


def _event_reference_progress(case: str, elapsed_ms: int) -> int:
    if case == "ev-loop":
        if elapsed_ms < EV_ONSET_MS:
            return elapsed_ms
        if elapsed_ms < EV_ONSET_MS + EV_LOOP_BACK_MS:
            return EV_ONSET_MS - EV_LOOP_BACK_MS + (elapsed_ms - EV_ONSET_MS)
        return elapsed_ms - EV_LOOP_BACK_MS
    if case == "ev-jump":
        return elapsed_ms if elapsed_ms < EV_ONSET_MS else elapsed_ms + EV_JUMP_MS
    if case == "ev-drift":
        if elapsed_ms < EV_ONSET_MS:
            return elapsed_ms
        return EV_ONSET_MS + (elapsed_ms - EV_ONSET_MS) * EV_DRIFT_RATE_E4 // 10_000
    return elapsed_ms


def _event_local_rate_e4(set_id: str, elapsed_ms: int) -> int:
    """The playback rate the provider would report as time skew for this window."""

    case = _event_case(set_id)
    if case == "ev-drift" and elapsed_ms >= EV_ONSET_MS:
        return EV_DRIFT_RATE_E4
    return 10_000


def _reference_progress(set_id: str, elapsed_ms: int) -> int:
    elapsed_ms = max(0, elapsed_ms)
    case = _event_case(set_id)
    if case is not None:
        return _event_reference_progress(case, elapsed_ms)
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


def _encoded_behavior(set_id: str) -> tuple[str, int, int]:
    """Return controlled behavior as (kind, temporal rate e4, pitch rate e4)."""

    for kind in ("tempo", "resample"):
        marker = f"-{kind}-"
        if marker in set_id:
            rate = int(set_id.rsplit(marker, 1)[1])
            return kind, rate, rate if kind == "resample" else 10_000
    marker = "-pitch-"
    if marker in set_id:
        semitones = int(set_id.rsplit(marker, 1)[1])
        pitch_rate = round(10_000 * (2 ** (semitones / 12)))
        return "pitch", 10_000, pitch_rate
    return "none", 10_000, 10_000


def _residual_rates_e4(set_id: str, transform: Transform) -> tuple[int, int]:
    _, native_time, native_pitch = _encoded_behavior(set_id)
    if transform.type == "none":
        correction_time = correction_pitch = 10_000
    elif transform.type == "resample":
        correction_time = correction_pitch = transform.rate_e4
    elif transform.type == "tempo":
        correction_time, correction_pitch = transform.rate_e4, 10_000
    else:
        correction_time, correction_pitch = 10_000, transform.rate_e4
    return (
        round(native_time * 10_000 / correction_time),
        round(native_pitch * 10_000 / correction_pitch),
    )


def transform_matches_fixture_rate(
    set_id: str, transform: Transform, *, tolerance_e4: int = 300
) -> bool:
    """Emulate recognition only when temporal and frequency residuals are within ±3%."""

    residual_time, residual_pitch = _residual_rates_e4(set_id, transform)
    return (
        abs(residual_time - 10_000) <= tolerance_e4 and abs(residual_pitch - 10_000) <= tolerance_e4
    )


def transform_matches_fixture_decoy(
    set_id: str, transform: Transform, *, tolerance_e4: int = 300
) -> bool:
    """Return whether an undone window resembles the catalogue's rate-edit decoy release."""

    residual_time, residual_pitch = _residual_rates_e4(set_id, transform)
    return (
        abs(residual_time - DECOY_RESIDUAL_RATE_E4) <= tolerance_e4
        and abs(residual_pitch - DECOY_RESIDUAL_RATE_E4) <= tolerance_e4
    )


def _hits_for_window(
    truth: GroundTruthRecord,
    support: tuple[int, int],
    source_offset_ms: int,
    *,
    transform: Transform | None = None,
    rate_tolerance_e4: int | None = None,
) -> list[FixtureHit]:
    gated = rate_tolerance_e4 is not None and transform is not None
    matches_truth = not gated or transform_matches_fixture_rate(
        truth.set_id, transform, tolerance_e4=rate_tolerance_e4
    )
    matches_decoy = gated and transform_matches_fixture_decoy(
        truth.set_id, transform, tolerance_e4=rate_tolerance_e4
    )
    if not matches_truth and not matches_decoy:
        return []
    residual_time, residual_pitch = (
        _residual_rates_e4(truth.set_id, transform) if transform is not None else (10_000, 10_000)
    )
    _, native_time_e4, _ = _encoded_behavior(truth.set_id)
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
        progress = _reference_progress(truth.set_id, elapsed)
        if "-tempo-" in truth.set_id or "-resample-" in truth.set_id:
            progress = progress * native_time_e4 // 10_000
        local_rate_e4 = _event_local_rate_e4(truth.set_id, max(0, elapsed))
        mix_anchor_ms = max(support[0], audible[0])
        hits.append(
            FixtureHit(
                episode_index=index,
                artist=episode.work.artist if matches_truth else DECOY_ARTIST,
                title=episode.work.title if matches_truth else DECOY_TITLE,
                recording_id=recording_id if matches_truth else DECOY_RECORDING_ID,
                ref_anchor_ms=source_offset_ms + progress,
                mix_anchor_ms=mix_anchor_ms,
                frequencyskew_e6=(residual_pitch - 10_000) * 100,
                timeskew_e6=(residual_time * local_rate_e4 // 10_000 - 10_000) * 100,
                is_decoy=not matches_truth,
            )
        )
    if not matches_truth:
        # One catalogue entry, one identity: a decoy answer collapses to a single hit even where
        # several stems are audible, mirroring one wrong track name for the whole window.
        hits = hits[:1]
    return hits


def build_recorded_response_map(
    *,
    truth: GroundTruthRecord,
    windows: WindowsResult,
    source_offset_ms: int,
    rate_tolerance_e4: int | None = None,
) -> dict[str, tuple[FixtureHit, ...]]:
    """Build the immutable response map for an already validated controlled source."""

    recorded: dict[str, tuple[FixtureHit, ...]] = {}
    for window in windows.records:
        hits = tuple(
            _hits_for_window(
                truth,
                window.support_ms,
                source_offset_ms,
                transform=window.transform,
                rate_tolerance_e4=rate_tolerance_e4,
            )
        )
        previous = recorded.setdefault(window.wav_sha256, hits)
        if previous != hits:
            raise ValueError("equal controlled window hashes map to different recorded responses")
    return recorded


def _query(media_key: str, window: Any, generation: int = 0) -> QueryRecord:
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
        generation=generation,
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
        "transform": window.transform.model_dump(mode="json"),
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
                {
                    "offset_ms": hit.ref_anchor_ms,
                    "frequencyskew_e6": hit.frequencyskew_e6,
                    "timeskew_e6": hit.timeskew_e6,
                }
            ],
            "simultaneous_source": (
                DECOY_SOURCE_KEY if hit.is_decoy else f"stem-{hit.episode_index:02d}"
            ),
            "content_sha256": window.wav_sha256,
        }
        anchor = Anchor(
            mix_anchor_ms=max(window.support_ms[0], hit.mix_anchor_ms),
            ref_anchor_ms=hit.ref_anchor_ms,
            uncertainty_ms=25,
            reliable=True,
            method="local_fixture_content_hash",
            bias_applied_ms=0,
        )
        source_ids.append(
            DECOY_SOURCE_ID if hit.is_decoy else f"controlled-truth:{hit.episode_index}"
        )
    return ObservationRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(media_key, "observation", compose_natural_key("observation", natural)),
        generation=query.generation,
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


def recognise_fixture_windows_in_memory(
    *,
    media_key: str,
    truth: GroundTruthRecord,
    windows: tuple[Any, ...] | list[Any],
    source_offset_ms: int,
    rate_tolerance_e4: int = 300,
) -> tuple[ObservationRecord, ...]:
    """Run the tolerance-based local fixture without publishing provider artifacts."""

    observations: list[ObservationRecord] = []
    for window in windows:
        query = _query(media_key, window)
        hits = _hits_for_window(
            truth,
            window.support_ms,
            source_offset_ms,
            transform=window.transform,
            rate_tolerance_e4=rate_tolerance_e4,
        )
        raw_ref = f"local_fixture/in-memory/{query.cache_key}.json"
        if hits:
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
            observations.append(
                _observation(
                    media_key=media_key,
                    query=query,
                    window=window,
                    hit=None,
                    native_index=0,
                    raw_response_ref=raw_ref,
                )
            )
    return tuple(sort_records(observations))


def recognise_controlled_fixture(
    *,
    media_key: str,
    media_dir: Path,
    windows: WindowsResult,
    recorded_responses: Mapping[str, tuple[FixtureHit, ...]],
    generation: int = 0,
) -> RecognitionResult:
    """Materialise deterministic recorded responses keyed by each exact window WAV hash."""

    # Recognition consults only a response map built from the validated expected source. Unknown
    # or mutated window content therefore cannot acquire truth-derived labels.
    windows_by_hash: dict[str, list[Any]] = {}
    for window in windows.records:
        windows_by_hash.setdefault(window.wav_sha256, []).append(window)

    invocation_dir = media_dir / "recognise" / "invocations" / "local-fixture-v1"
    raw_dir = invocation_dir / "raw"
    queries_path = invocation_dir / f"queries.gen{generation}.jsonl"
    observations_path = invocation_dir / f"observations.gen{generation}.jsonl"
    raw_index_path = invocation_dir / f"raw_index.gen{generation}.json"
    queries: list[QueryRecord] = []
    observations: list[ObservationRecord] = []
    raw_index: list[RawIndexEntry] = []
    for content_hash in sorted(windows_by_hash):
        grouped_windows = windows_by_hash[content_hash]
        query = _query(media_key, grouped_windows[0], generation)
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
    windows_key = windows.record_path.name
    write_completion_sidecar(queries_path, {f"windows/{windows_key}": windows.record_path})
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
            f"windows/{windows_key}": windows.record_path,
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
