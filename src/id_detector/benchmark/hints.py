"""Held-out Stage 4a fused-vs-audio-only gate."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from id_detector.benchmark.corpus import run_corpus
from id_detector.benchmark.scorer import (
    PredictionDocument,
    SetScore,
    load_truth_directory,
    paired_non_inferiority,
    score_set,
    truth_is_frozen_verified,
)
from id_detector.contracts import GroundTruthRecord
from id_detector.io import atomic_write_json, read_text


@dataclass(frozen=True)
class HintGateResult:
    report_path: Path
    passed: bool
    coverage_delta_e4: int
    coverage_cluster_lower_e4: int
    precision_cluster_lower_e4: int


def _evidence_coverage(
    predictions_path: Path, duration_by_set: dict[str, int]
) -> dict[str, tuple[int, int]]:
    """Return badge-eligible evidence-support unions, weighted by each set duration."""

    payload = json.loads(read_text(predictions_path))
    result: dict[str, tuple[int, int]] = {}
    for prediction_set in payload.get("sets", []):
        if not isinstance(prediction_set, dict):
            continue
        set_id = str(prediction_set.get("set_id"))
        duration = duration_by_set[set_id]
        spans: list[tuple[int, int]] = []
        for episode in prediction_set.get("episodes", []):
            if not isinstance(episode, dict):
                continue
            tiers = episode.get("tiers")
            if not isinstance(tiers, dict) or tiers.get("work") == "unclear":
                continue
            for raw_span in episode.get("evidence_support_ms", []):
                if (
                    isinstance(raw_span, list)
                    and len(raw_span) == 2
                    and all(isinstance(value, int) for value in raw_span)
                ):
                    spans.append((max(0, raw_span[0]), min(duration, raw_span[1])))
        supported = 0
        end = 0
        for start, stop in sorted(spans):
            if stop <= start:
                continue
            if start >= end:
                supported += stop - start
            elif stop > end:
                supported += stop - end
            end = max(end, stop)
        result[set_id] = (supported, duration)
    if set(result) != set(duration_by_set):
        raise ValueError("hint gate predictions do not cover the frozen set population")
    return result


def _raw_segment_precision_counts(scores: list[SetScore]) -> dict[str, tuple[int, int]]:
    return {
        score.truth.set_id: (
            score.state.segment.tp,
            score.state.segment.tp + score.state.segment.fp,
        )
        for score in scores
    }


def _segment_precision_counts(
    predictions_path: Path, truths: list[GroundTruthRecord]
) -> dict[str, tuple[int, int]]:
    document = PredictionDocument.model_validate_json(read_text(predictions_path))
    predictions = {item.set_id: item for item in document.sets}
    if set(predictions) != {truth.set_id for truth in truths}:
        raise ValueError("hint gate precision inputs do not cover the frozen set population")
    return _raw_segment_precision_counts(
        [score_set(truth, predictions[truth.set_id]) for truth in truths]
    )


async def run_hint_gate(
    *,
    corpus_version: str,
    out_path: Path,
    project_root: Path,
    work_root: Path,
    max_requests: int = 2_000,
) -> HintGateResult:
    corpus_dir = project_root / "data" / "corpus" / corpus_version
    if not corpus_dir.is_dir():
        raise ValueError(
            f"formal Stage 4a gate pending owner-verified frozen {corpus_version} truth"
        )
    truths = load_truth_directory(corpus_dir)
    if not truth_is_frozen_verified(corpus_dir, truths):
        raise ValueError(
            f"formal Stage 4a gate pending owner-verified frozen {corpus_version} truth"
        )
    local_report_root = project_root / "data" / "local" / "benchmark" / corpus_version / "hints"
    audio = await run_corpus(
        corpus_version=corpus_version,
        profile="free",
        out_path=local_report_root / "audio-only.json",
        project_root=project_root,
        work_root=work_root,
        max_requests=max_requests,
        include_hints=False,
    )
    duration_by_set = {truth.set_id: truth.source.duration_ms for truth in truths}
    audio_coverage = _evidence_coverage(audio.predictions_path, duration_by_set)
    fused = await run_corpus(
        corpus_version=corpus_version,
        profile="free",
        out_path=local_report_root / "fused.json",
        project_root=project_root,
        work_root=work_root,
        max_requests=max_requests,
        include_hints=True,
    )
    fused_coverage = _evidence_coverage(fused.predictions_path, duration_by_set)
    audio_sets = {item.set_id: item for item in audio.report.sets}
    fused_sets = {item.set_id: item for item in fused.report.sets}
    if set(audio_sets) != set(fused_sets):
        raise ValueError("hint gate runs produced different set populations")
    coverage = paired_non_inferiority(audio_coverage, fused_coverage, seed=20_260_904, margin_e4=0)
    audio_precision = _segment_precision_counts(audio.predictions_path, truths)
    fused_precision = _segment_precision_counts(fused.predictions_path, truths)
    precision = paired_non_inferiority(
        audio_precision, fused_precision, seed=20_260_905, margin_e4=100
    )
    coverage_delta = int(coverage["delta_e4"])
    coverage_lower = int(coverage["lower_bound_e4"])
    precision_lower = int(precision["lower_bound_e4"])
    passed = coverage_delta >= 500 and coverage_lower > 0 and bool(precision["pass"])
    atomic_write_json(
        out_path,
        {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "corpus_version": corpus_version,
            "profile": "free",
            "unverified_seed_comparison": False,
            "sets": sorted(audio_sets),
            "duration_weighted_evidence_coverage_delta_e4": coverage_delta,
            "coverage_one_sided_cluster_lower_e4": coverage_lower,
            "segment_precision_delta_e4": int(precision["delta_e4"]),
            "segment_precision_one_sided_cluster_lower_e4": precision_lower,
            "gates": {
                "coverage_plus_5pp": coverage_delta >= 500,
                "coverage_cluster_lower_above_zero": coverage_lower > 0,
                "precision_noninferior_1pp": bool(precision["pass"]),
                "all": passed,
            },
        },
    )
    return HintGateResult(out_path, passed, coverage_delta, coverage_lower, precision_lower)
