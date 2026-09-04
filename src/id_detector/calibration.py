"""Live Shazam insertion-test harness for measured offset configuration."""

from __future__ import annotations

import shutil
import tempfile
import wave
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Any

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    ProviderConfigRecord,
    compose_natural_key,
    make_id,
)
from id_detector.decode import BYTES_PER_SAMPLE, SAMPLE_RATE, decode
from id_detector.ingest import ingest
from id_detector.io import atomic_write_json, canonical_json_bytes, write_completion_sidecar
from id_detector.shazam import ShazamAdapter

QUERY_LENGTHS_SECONDS = (6, 8, 12)
PARTIAL_OVERLAPS_E4 = (10_000, 7_500, 5_000)


@dataclass(frozen=True)
class CalibrationResult:
    config_path: Path
    adapter_bias_ms: int
    adapter_bias_uncertainty_ms: int
    l_min_ms: dict[str, int]
    cases: int
    successes: int
    physical_attempts: int


def _nearest_rank(values: list[int], percentile: int) -> int:
    ordered = sorted(values)
    index = max(0, (percentile * len(ordered) + 99) // 100 - 1)
    return ordered[index]


def estimate_latency_distribution(
    raw_cases: list[dict[str, Any]], expected_track_key: str
) -> tuple[dict[str, int], list[dict[str, int]]]:
    """Estimate recognition latency over independent insertion positions.

    A position's event time is its shortest successful audible duration. Failed shorter cases are
    censored lower bounds on that event; a position with no success is right-censored at the
    maximum tested duration. Thus layouts never masquerade as independent positions and failures
    are retained instead of being discarded from the latency population.
    """

    positions = sorted({int(case["position_ms"]) for case in raw_cases})
    durations = sorted({int(case["material_ms"]) for case in raw_cases})
    if not positions or not durations:
        raise ValueError("latency estimation requires cases and insertion positions")

    first_success_by_position: dict[int, int | None] = {}
    for position in positions:
        successful_durations = [
            int(case["material_ms"])
            for case in raw_cases
            if int(case["position_ms"]) == position
            and case.get("track_key") == expected_track_key
            and case.get("offset_ms") is not None
        ]
        first_success_by_position[position] = min(successful_durations, default=None)

    curve: list[dict[str, int]] = []
    for duration in durations:
        trials = 0
        successes = 0
        for position in positions:
            cell = [
                case
                for case in raw_cases
                if int(case["position_ms"]) == position and int(case["material_ms"]) == duration
            ]
            if not cell:
                raise ValueError(f"latency grid is missing position {position} ms at {duration} ms")
            cell_successes = sum(
                case.get("track_key") == expected_track_key and case.get("offset_ms") is not None
                for case in cell
            )
            trials += len(cell)
            successes += cell_successes
        recognized_positions = sum(
            event is not None and event <= duration for event in first_success_by_position.values()
        )
        curve.append(
            {
                "audible_ms": duration,
                "n_positions": len(positions),
                "n_trials": trials,
                "n_successes": successes,
                "n_censored_positions": len(positions) - recognized_positions,
                "success_fraction_e4": (recognized_positions * 10_000 + len(positions) // 2)
                // len(positions),
            }
        )
    l_min: dict[str, int] = {}
    for percentile in (50, 90, 95):
        threshold_e4 = percentile * 100
        duration = next(
            (item["audible_ms"] for item in curve if item["success_fraction_e4"] >= threshold_e4),
            None,
        )
        if duration is None:
            raise RuntimeError(f"latency p{percentile} is right-censored above {durations[-1]} ms")
        l_min[f"p{percentile}"] = duration
    return l_min, curve


def _write_case(
    pcm_path: Path,
    destination: Path,
    *,
    position_ms: int,
    query_seconds: int,
    overlap_e4: int,
) -> tuple[int, int]:
    total_samples = query_seconds * SAMPLE_RATE
    content_samples = total_samples * overlap_e4 // 10_000
    leading_silence = (total_samples - content_samples) // 2
    with pcm_path.open("rb") as source:
        source.seek(position_ms * SAMPLE_RATE // 1000 * BYTES_PER_SAMPLE)
        content = source.read(content_samples * BYTES_PER_SAMPLE)
    if len(content) != content_samples * BYTES_PER_SAMPLE:
        raise ValueError(
            f"position {position_ms} ms plus query material exceeds the calibration track"
        )
    frames = (
        bytes(leading_silence * BYTES_PER_SAMPLE)
        + content
        + bytes((total_samples - leading_silence - content_samples) * BYTES_PER_SAMPLE)
    )
    with wave.open(str(destination), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(BYTES_PER_SAMPLE)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(frames)
    return (
        content_samples * 1000 // SAMPLE_RATE,
        leading_silence * 1000 // SAMPLE_RATE,
    )


def offset_error_from_first_fingerprinted_sample(
    *, offset_ms: int, leading_silence_ms: int, position_ms: int
) -> int:
    """Translate Shazam's query-start offset to the first non-silent fingerprinted sample."""

    return offset_ms + leading_silence_ms - position_ms


def _median_offset_ms(matches: Any) -> int | None:
    offsets = sorted(
        round(float(item["offset"]) * 1000)
        for item in matches or []
        if isinstance(item, dict) and item.get("offset") is not None
    )
    if not offsets:
        return None
    candidates = [
        [value for value in offsets[index:] if value - start <= 1500]
        for index, start in enumerate(offsets)
    ]
    cluster = max(candidates, key=lambda values: (len(values), -values[0]))
    if len(cluster) * 2 < len(offsets):
        return None
    return round(median(cluster))


async def calibrate_shazam(
    *,
    track: str,
    positions_ms: list[int],
    project_root: Path,
) -> CalibrationResult:
    if len(positions_ms) < 5:
        raise ValueError("calibration requires at least five known positions")
    if any(position < 0 for position in positions_ms):
        raise ValueError("calibration positions cannot be negative")

    config_dir = project_root / "provider_configs"
    existing = (
        [
            int(path.stem.removeprefix("shazam-v"))
            for path in config_dir.glob("shazam-v*.json")
            if path.stem.removeprefix("shazam-v").isdigit()
        ]
        if config_dir.is_dir()
        else []
    )
    version_number = max(existing, default=0) + 1
    version = f"shazam-v{version_number}.json"
    destination = config_dir / version
    if destination.exists():
        raise FileExistsError(f"provider configuration already exists: {destination}")

    temporary_root = Path(tempfile.mkdtemp(prefix="id-detector-calibration-"))
    try:
        ingested = await ingest(track, temporary_root / "work")
        decoded = await decode(ingested)
        unmeasured = ProviderConfigRecord(
            schema_version=SCHEMA_VERSION,
            generated_by=GENERATED_BY,
            id=make_id(
                ingested.record.media_key,
                "provider_config",
                compose_natural_key(
                    "provider_config",
                    {"provider": "shazam", "version": "calibration-unmeasured"},
                ),
            ),
            provider="shazam",
            version="calibration-unmeasured",
            capability="clip_recognizer",
            measured=False,
            config={"library_retries": 0, "segment_duration_seconds": 12},
            adapter_bias_ms=None,
            adapter_bias_uncertainty_ms=None,
            L_min_ms=None,
            source_ids=[],
        )
        adapter = ShazamAdapter(unmeasured)
        physical_attempts = 0

        async def attempted() -> None:
            nonlocal physical_attempts
            physical_attempts += 1

        raw_cases: list[dict[str, Any]] = []
        case_dir = temporary_root / "cases"
        case_dir.mkdir()
        for position_ms in positions_ms:
            for query_seconds in QUERY_LENGTHS_SECONDS:
                for overlap_e4 in PARTIAL_OVERLAPS_E4:
                    path = case_dir / f"p{position_ms}-q{query_seconds}-o{overlap_e4}.wav"
                    material_ms, leading_silence_ms = _write_case(
                        decoded.pcm_path,
                        path,
                        position_ms=position_ms,
                        query_seconds=query_seconds,
                        overlap_e4=overlap_e4,
                    )
                    try:
                        response = await adapter.recognize_once(path, attempted)
                    except Exception as exc:
                        raw_cases.append(
                            {
                                "position_ms": position_ms,
                                "query_ms": query_seconds * 1000,
                                "overlap_e4": overlap_e4,
                                "material_ms": material_ms,
                                "leading_silence_ms": leading_silence_ms,
                                "track_key": None,
                                "offset_ms": None,
                                "error": type(exc).__name__,
                            }
                        )
                        continue
                    track_value = response.get("track") or {}
                    raw_cases.append(
                        {
                            "position_ms": position_ms,
                            "query_ms": query_seconds * 1000,
                            "overlap_e4": overlap_e4,
                            "material_ms": material_ms,
                            "leading_silence_ms": leading_silence_ms,
                            "track_key": str(track_value.get("key"))
                            if track_value.get("key") is not None
                            else None,
                            "offset_ms": _median_offset_ms(response.get("matches")),
                            "error": None,
                        }
                    )

        keys = Counter(
            str(case["track_key"])
            for case in raw_cases
            if case["track_key"] is not None and case["offset_ms"] is not None
        )
        if not keys:
            raise RuntimeError("no calibration case produced a Shazam match")
        expected_key = keys.most_common(1)[0][0]
        successes = [
            case
            for case in raw_cases
            if case["track_key"] == expected_key and case["offset_ms"] is not None
        ]
        if len({case["position_ms"] for case in successes}) < 5:
            raise RuntimeError("Shazam did not match the released track at five distinct positions")
        errors = [
            offset_error_from_first_fingerprinted_sample(
                offset_ms=int(case["offset_ms"]),
                leading_silence_ms=int(case["leading_silence_ms"]),
                position_ms=int(case["position_ms"]),
            )
            for case in successes
        ]
        bias_ms = round(median(errors))
        uncertainty_ms = _nearest_rank([abs(value - bias_ms) for value in errors], 95)
        l_min, latency_curve = estimate_latency_distribution(raw_cases, expected_key)
        evidence_hash = sha256(canonical_json_bytes(raw_cases)).hexdigest()
        config = ProviderConfigRecord(
            schema_version=SCHEMA_VERSION,
            generated_by=GENERATED_BY,
            id=make_id(
                ingested.record.media_key,
                "provider_config",
                compose_natural_key("provider_config", {"provider": "shazam", "version": version}),
            ),
            provider="shazam",
            version=version,
            capability="clip_recognizer",
            measured=True,
            config={
                "library_retries": 0,
                "segment_duration_seconds": 12,
                "positions_ms": positions_ms,
                "query_lengths_ms": [value * 1000 for value in QUERY_LENGTHS_SECONDS],
                "partial_overlaps_e4": list(PARTIAL_OVERLAPS_E4),
                "n_cases": len(raw_cases),
                "n_successes": len(successes),
                "latency_estimator": "position-first-success-ecdf-v1",
                "latency_failures": "right-censored",
                "latency_success_curve": latency_curve,
                "expected_track_key_hash": sha256(expected_key.encode()).hexdigest(),
            },
            adapter_bias_ms=bias_ms,
            adapter_bias_uncertainty_ms=uncertainty_ms,
            L_min_ms=l_min,
            source_ids=[f"insertion-suite:{evidence_hash}"],
        )
        atomic_write_json(destination, config)
        write_completion_sidecar(destination, {})
        return CalibrationResult(
            destination,
            bias_ms,
            uncertainty_ms,
            l_min,
            len(raw_cases),
            len(successes),
            physical_attempts,
        )
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
