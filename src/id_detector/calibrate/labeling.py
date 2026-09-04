"""Turn scored predictions into labelled calibration examples using the benchmark scorer.

The truth labels reuse the scorer's exact association and correctness definitions so the calibrator
is fit against the same notion of "correct" the certification gate uses.
"""

from __future__ import annotations

from id_detector.benchmark.scorer import (
    PredictionSet,
    _IdentityResolver,
    _prediction_is_evaluable,
    associate_occurrences,
)
from id_detector.calibrate.model import BOUNDARY_TOLERANCE_MS, CalibrationExample, _range_error
from id_detector.contracts import CalibrationFeatures, GroundTruthRecord


def label_set(
    truth: GroundTruthRecord,
    prediction_set: PredictionSet,
    features_by_index: dict[int, CalibrationFeatures],
) -> list[CalibrationExample]:
    """Associate predictions to truth and emit one example per evaluable episode."""

    resolver = _IdentityResolver(prediction_set.identities)
    predictions = prediction_set.episodes
    evaluable = {
        index
        for index, episode in enumerate(predictions)
        if _prediction_is_evaluable(episode, truth)
    }
    order = sorted(evaluable)
    local = associate_occurrences([predictions[index] for index in order], truth.episodes, resolver)
    associations = {order[position]: truth_index for position, truth_index in local.items()}

    examples: list[CalibrationExample] = []
    for index in order:
        episode = predictions[index]
        features = features_by_index.get(index)
        if features is None:
            continue
        associated = index in associations
        version_evaluable = resolver.has_resolved_recording(episode) and not (
            associated and not truth.episodes[associations[index]].version_verified
        )
        version_correct = False
        truth_start: tuple[int, int] | None = None
        truth_end: tuple[int, int] | None = None
        if associated:
            actual = truth.episodes[associations[index]]
            truth_start = (actual.start_ms_range[0], actual.start_ms_range[1])
            truth_end = (actual.end_ms_range[0], actual.end_ms_range[1])
            version_correct = bool(
                actual.version_verified and resolver.exact_equivalent(episode, actual)
            )
        examples.append(
            CalibrationExample(
                features=features,
                associated=associated,
                work_correct=associated,
                version_evaluable=version_evaluable,
                version_correct=version_correct,
                start_proved_ms=episode.start_no_later_than_ms,
                end_proved_ms=episode.end_no_earlier_than_ms,
                truth_start_ms=truth_start,
                truth_end_ms=truth_end,
            )
        )
    return examples


__all__ = ["label_set", "BOUNDARY_TOLERANCE_MS", "_range_error"]
