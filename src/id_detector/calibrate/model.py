"""Monotone, interpretable calibrator: isotonic tier scores and empirical prediction intervals.

The calibrator is deliberately a white box:

* per ``(profile, dimension)`` it maps a **documented, integer, monotone ordering index** (see
  :mod:`id_detector.calibrate.features`) through an **isotonic (pool-adjacent-violators) step
  function** to a calibrated precision, and reads tier thresholds off that step function at the
  plan's target precisions (possible 0.70, likely 0.90, verified 0.99); and
* per boundary side it learns a **prediction interval** from the empirical distribution of
  ``(true boundary - proved bound)`` on the calibration split, targeting 0.9 coverage.

Model artefacts are immutable, versioned files ``calibration/<profile>-v<K>.json`` carrying the
corpus version, a config hash, the fit population, and the certification block (all ``provisional``
until a real-mix test corpus exists).  There is no black box and no float in any artefact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from id_detector.calibrate.features import (
    DIMENSION_INDEX,
    DIMENSION_INDEX_FORMULA,
    FEATURE_NAMES,
    EpisodeFeatureInputs,
    build_features,
)
from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    CalibrationBin,
    CalibrationCertEntry,
    CalibrationDimensionModel,
    CalibrationFeatures,
    CalibrationIntervalModel,
    CalibrationModelRecord,
    CalibrationProvenance,
    CalibrationTierThreshold,
    EpisodeScores,
    EpisodeTiers,
    PredictionInterval,
    compose_natural_key,
    make_id,
)
from id_detector.io import read_text

DIMENSIONS: tuple[str, ...] = ("work", "version", "boundary")
CERT_DIMENSIONS: tuple[str, ...] = ("work", "version", "start", "end", "boundary")
CERT_TIERS: tuple[str, ...] = ("possible", "likely", "verified")
TIER_TARGET_E4: dict[str, int] = {"possible": 7_000, "likely": 9_000, "verified": 9_900}
TIER_ORDER = {"unclear": 0, "possible": 1, "likely": 2, "verified": 3}
COVERAGE_TARGET_E4 = 9_000
METHOD = "isotonic-pav-index-v1+empirical-pi-v1"
_VERSION_RE = re.compile(r"-v(\d+)$")


# --------------------------------------------------------------------------------------------------
# Fit inputs
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CalibrationExample:
    """One associated/evaluable episode with its truth labels, produced by the labeller."""

    features: CalibrationFeatures
    associated: bool
    work_correct: bool
    version_evaluable: bool
    version_correct: bool
    start_proved_ms: int
    end_proved_ms: int
    truth_start_ms: tuple[int, int] | None
    truth_end_ms: tuple[int, int] | None


# --------------------------------------------------------------------------------------------------
# Isotonic regression (pool-adjacent-violators), integer output
# --------------------------------------------------------------------------------------------------
def _pav(points: list[tuple[int, int]]) -> list[CalibrationBin]:
    """Fit a non-decreasing step function to ``(index, label 0/1)`` points.

    Returns change-point bins ``(index_ge, calibrated_e4, n)`` sorted by ``index_ge`` ascending with
    non-decreasing ``calibrated_e4``.
    """

    if not points:
        return []
    # Pool exact-index ties first so the ordering feature drives one block per distinct index.
    grouped: dict[int, list[int]] = {}
    for index, label in points:
        grouped.setdefault(index, []).append(label)
    blocks: list[list[int]] = []  # each: [sum_labels, count, min_index]
    for index in sorted(grouped):
        labels = grouped[index]
        block = [sum(labels), len(labels), index]
        blocks.append(block)
        while len(blocks) >= 2 and blocks[-2][0] * blocks[-1][1] > blocks[-1][0] * blocks[-2][1]:
            last = blocks.pop()
            prev = blocks.pop()
            blocks.append([prev[0] + last[0], prev[1] + last[1], prev[2]])
    bins: list[CalibrationBin] = []
    for total, count, min_index in blocks:
        value = min(10_000, (total * 10_000 + count // 2) // count)
        bins.append(CalibrationBin(index_ge=min_index, calibrated_e4=value, n=count))
    return bins


def isotonic_score_e4(bins: list[CalibrationBin], index: int) -> int:
    """Evaluate the fitted step function at ``index`` (0 below the first change point)."""

    score = 0
    for item in bins:
        if index >= item.index_ge:
            score = item.calibrated_e4
        else:
            break
    return score


def _tier_thresholds(bins: list[CalibrationBin]) -> list[CalibrationTierThreshold]:
    thresholds: list[CalibrationTierThreshold] = []
    for tier in CERT_TIERS:
        target = TIER_TARGET_E4[tier]
        hit = next((item for item in bins if item.calibrated_e4 >= target), None)
        thresholds.append(
            CalibrationTierThreshold(
                tier=tier,  # type: ignore[arg-type]
                target_e4=target,
                min_index=hit.index_ge if hit is not None else None,
                achieved_precision_e4=hit.calibrated_e4 if hit is not None else None,
            )
        )
    return thresholds


def _tier_for_index(thresholds: list[CalibrationTierThreshold], index: int) -> str:
    by_tier = {item.tier: item for item in thresholds}
    tier = "unclear"
    for name in CERT_TIERS:
        threshold = by_tier.get(name)
        if (
            threshold is not None
            and threshold.min_index is not None
            and index >= threshold.min_index
        ):
            tier = name
    return tier


# --------------------------------------------------------------------------------------------------
# Empirical prediction intervals
# --------------------------------------------------------------------------------------------------
def _quantile(sorted_values: list[int], numerator: int, denominator: int) -> int:
    if not sorted_values:
        return 0
    index = max(0, min(len(sorted_values) - 1, (numerator * len(sorted_values)) // denominator))
    return sorted_values[index]


def _fit_interval(
    residuals: list[int], truth_ranges: list[tuple[int, int]], proved: list[int], side: str
) -> CalibrationIntervalModel:
    ordered = sorted(residuals)
    q_lo = _quantile(ordered, 5, 100)
    q_hi = _quantile(ordered, 95, 100)
    if q_hi < q_lo:
        q_lo, q_hi = q_hi, q_lo
    covered = 0
    for base, (truth_lo, truth_hi) in zip(proved, truth_ranges, strict=True):
        lo = max(0, base + q_lo)
        hi = max(lo, base + q_hi)
        covered += int(lo <= truth_hi and hi >= truth_lo)
    achieved = (covered * 10_000 + len(proved) // 2) // len(proved) if proved else 0
    return CalibrationIntervalModel(
        side=side,  # type: ignore[arg-type]
        q_lo_ms=q_lo,
        q_hi_ms=q_hi,
        coverage_target_e4=COVERAGE_TARGET_E4,
        achieved_coverage_e4=achieved,
        method="empirical-quantile-of-true-minus-proved-v1",
        n=len(proved),
    )


def _interval_endpoints(model: CalibrationIntervalModel, proved: int) -> tuple[int, int, int]:
    lo = max(0, proved + model.q_lo_ms)
    hi = max(lo, proved + model.q_hi_ms)
    best = (lo + hi) // 2
    return lo, hi, best


# --------------------------------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------------------------------
def _provisional_certification(test_version: str) -> list[CalibrationCertEntry]:
    return [
        CalibrationCertEntry(
            dimension=dimension,  # type: ignore[arg-type]
            tier=tier,  # type: ignore[arg-type]
            status="provisional",
            n_test_predictions=0,
            lower_bound_e4=0,
            test_version=test_version,
        )
        for dimension in CERT_DIMENSIONS
        for tier in CERT_TIERS
    ]


def fit_calibration(
    examples: list[CalibrationExample],
    *,
    profile: str,
    version_number: int,
    corpus_version: str,
    config_hash: str,
    population: str,
    split_seed: int,
    calibration_set_ids: list[str],
    test_version: str = "provisional",
    id_seed: str | None = None,
) -> CalibrationModelRecord:
    """Fit isotonic tier calibrators and empirical PIs from labelled calibration examples."""

    if version_number < 1:
        raise ValueError("calibration version numbers start at 1")

    # Prediction intervals first: they define the calibrated best_start/best_end used by the
    # start/end/boundary tier labels.
    intervals: list[CalibrationIntervalModel] = []
    associated = [example for example in examples if example.associated]
    for side, proved_of, truth_of in (
        ("start", lambda e: e.start_proved_ms, lambda e: e.truth_start_ms),
        ("end", lambda e: e.end_proved_ms, lambda e: e.truth_end_ms),
    ):
        residuals: list[int] = []
        truth_ranges: list[tuple[int, int]] = []
        proved: list[int] = []
        for example in associated:
            truth_range = truth_of(example)
            if truth_range is None:
                continue
            centre = (truth_range[0] + truth_range[1]) // 2
            residuals.append(centre - proved_of(example))
            truth_ranges.append(truth_range)
            proved.append(proved_of(example))
        intervals.append(_fit_interval(residuals, truth_ranges, proved, side))
    interval_by_side = {item.side: item for item in intervals}

    dimensions: list[CalibrationDimensionModel] = []
    for dimension in DIMENSIONS:
        points: list[tuple[int, int]] = []
        positive = 0
        for example in examples:
            if dimension == "version" and not example.version_evaluable:
                continue
            index = DIMENSION_INDEX[dimension](example.features)
            label = _dimension_label(dimension, example, interval_by_side)
            if label is None:
                continue
            points.append((index, int(label)))
            positive += int(label)
        bins = _pav(points)
        dimensions.append(
            CalibrationDimensionModel(
                dimension=dimension,  # type: ignore[arg-type]
                index_formula=DIMENSION_INDEX_FORMULA[dimension],
                isotonic=bins,
                tier_thresholds=_tier_thresholds(bins),
                n=len(points),
                n_positive=positive,
            )
        )

    version = f"{profile}-v{version_number}.json"
    natural_key = compose_natural_key("calibration_model", {"profile": profile, "version": version})
    provenance = CalibrationProvenance(
        corpus_version=corpus_version,
        population=population,
        split_seed=split_seed,
        calibration_set_ids=sorted(calibration_set_ids),
        method=METHOD,
    )
    return CalibrationModelRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(id_seed or config_hash, "calibration_model", natural_key),
        profile=profile,
        version=version,
        frozen=True,
        corpus_version=corpus_version,
        config_hash=config_hash,
        population=population,
        method=METHOD,
        n_calibration_sets=len(set(calibration_set_ids)),
        n_calibration_predictions=len(examples),
        feature_names=list(FEATURE_NAMES),
        dimensions=dimensions,
        intervals=intervals,
        certification=_provisional_certification(test_version),
        frozen_from=provenance,
        notes=[
            "Fit mechanically by `id-detector benchmark certify`/calibration validation; do not "
            "hand-edit. Monotone isotonic (PAV) on a documented integer ordering index per "
            "dimension, plus empirical prediction intervals; no black box, no floats.",
            "Tier thresholds target the plan precisions possible 0.70 / likely 0.90 / verified "
            "0.99 (e4 7000/9000/9900). Prediction intervals target 0.9 coverage.",
            f"population: {population}. Certifies no tier: every certification entry stays "
            "provisional until an owner-verified real-mix calibration and test corpus exists.",
        ],
    )


BOUNDARY_TOLERANCE_MS = 10_000


def _range_error(point: int, interval: tuple[int, int]) -> int:
    if point < interval[0]:
        return interval[0] - point
    if point > interval[1]:
        return point - interval[1]
    return 0


def _dimension_label(
    dimension: str,
    example: CalibrationExample,
    interval_by_side: dict[str, CalibrationIntervalModel],
) -> bool | None:
    if dimension == "work":
        return example.work_correct
    if dimension == "version":
        return example.version_correct
    # boundary: both endpoints within tolerance of the truth ranges, using the calibrated best.
    if not example.associated or example.truth_start_ms is None or example.truth_end_ms is None:
        return False
    _, _, best_start = _interval_endpoints(interval_by_side["start"], example.start_proved_ms)
    _, _, best_end = _interval_endpoints(interval_by_side["end"], example.end_proved_ms)
    start_ok = _range_error(best_start, example.truth_start_ms) <= BOUNDARY_TOLERANCE_MS
    end_ok = _range_error(best_end, example.truth_end_ms) <= BOUNDARY_TOLERANCE_MS
    return bool(start_ok and end_ok)


# --------------------------------------------------------------------------------------------------
# Applying a fitted model
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class EpisodeCalibration:
    scores: EpisodeScores
    tiers: EpisodeTiers
    start_pi: PredictionInterval
    end_pi: PredictionInterval
    best_start_ms: int
    best_end_ms: int


class CalibrationApplier:
    """Apply a loaded, frozen calibration model to a single episode's features and proved bounds."""

    def __init__(self, model: CalibrationModelRecord) -> None:
        self.model = model
        self._dimensions = {item.dimension: item for item in model.dimensions}
        self._intervals = {item.side: item for item in model.intervals}

    @property
    def profile(self) -> str:
        return self.model.profile

    def _pi(self, side: str, proved: int) -> tuple[PredictionInterval, int]:
        interval = self._intervals[side]
        lo, hi, best = _interval_endpoints(interval, proved)
        return (
            PredictionInterval(
                lo=lo,
                hi=hi,
                coverage_target=interval.coverage_target_e4,
                method=f"empirical-pi-{side}:{self.model.version}",
                calibrated=True,
            ),
            best,
        )

    def apply_inputs(
        self, inputs: EpisodeFeatureInputs, *, start_proved_ms: int, end_proved_ms: int
    ) -> EpisodeCalibration:
        return self.apply(
            build_features(inputs), start_proved_ms=start_proved_ms, end_proved_ms=end_proved_ms
        )

    def apply_episode(
        self, *, start_proved_ms: int, end_proved_ms: int, **inputs: object
    ) -> EpisodeCalibration:
        """Duck-typed entry point so the fuser can calibrate without importing this package."""

        return self.apply_inputs(
            EpisodeFeatureInputs(**inputs),  # type: ignore[arg-type]
            start_proved_ms=start_proved_ms,
            end_proved_ms=end_proved_ms,
        )

    def apply(
        self, features: CalibrationFeatures, *, start_proved_ms: int, end_proved_ms: int
    ) -> EpisodeCalibration:
        scores: dict[str, int] = {}
        tiers: dict[str, str] = {}
        for dimension in DIMENSIONS:
            model = self._dimensions[dimension]
            index = DIMENSION_INDEX[dimension](features)
            scores[dimension] = isotonic_score_e4(model.isotonic, index)
            tiers[dimension] = _tier_for_index(model.tier_thresholds, index)
        # Structural caps the plan never lets calibration override.
        if not features.recording_supported or features.contested or features.identity_conflicts:
            tiers["version"] = "unclear"
        if not features.has_global_alignment:
            tiers["boundary"] = "unclear"
        start_pi, best_start = self._pi("start", start_proved_ms)
        end_pi, best_end = self._pi("end", end_proved_ms)
        return EpisodeCalibration(
            scores=EpisodeScores(
                work=scores["work"], version=scores["version"], boundary=scores["boundary"]
            ),
            tiers=EpisodeTiers(
                work=tiers["work"],  # type: ignore[arg-type]
                version=tiers["version"],  # type: ignore[arg-type]
                boundary=tiers["boundary"],  # type: ignore[arg-type]
            ),
            start_pi=start_pi,
            end_pi=end_pi,
            best_start_ms=best_start,
            best_end_ms=best_end,
        )


# --------------------------------------------------------------------------------------------------
# Immutable, versioned artefacts
# --------------------------------------------------------------------------------------------------
def next_version_number(out_dir: Path, profile: str) -> int:
    highest = 0
    if out_dir.is_dir():
        for path in out_dir.glob(f"{profile}-v*.json"):
            match = _VERSION_RE.search(path.stem)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def _model_texts(project_root: Path, profile: str) -> dict[int, str]:
    found: dict[int, str] = {}
    directory = project_root / "calibration"
    if directory.is_dir():
        for path in directory.glob(f"{profile}-v*.json"):
            match = _VERSION_RE.search(path.stem)
            if match:
                found[int(match.group(1))] = read_text(path)
    try:
        packaged = files("id_detector.resources.calibration")
    except (ModuleNotFoundError, FileNotFoundError):
        packaged = None
    if packaged is not None:
        for item in packaged.iterdir():
            if not item.name.endswith(".json") or item.name.endswith(".done.json"):
                continue
            stem = item.name.removesuffix(".json")
            match = _VERSION_RE.search(stem)
            if match and stem.startswith(f"{profile}-v"):
                found.setdefault(int(match.group(1)), item.read_text(encoding="utf-8"))
    return found


def load_calibration(project_root: Path, profile: str) -> CalibrationApplier | None:
    """Load the highest-versioned frozen calibration model for ``profile``; ``None`` if absent.

    No calibration model is committed for the real-mix profiles, so ``analyse`` stays heuristic by
    default; a model appears here only once an owner-verified corpus fits one.
    """

    texts = _model_texts(project_root, profile)
    if not texts:
        return None
    model = CalibrationModelRecord.model_validate_json(texts[max(texts)])
    if not model.frozen:
        return None
    return CalibrationApplier(model)
