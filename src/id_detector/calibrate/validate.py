"""Machinery validation of the calibration code path on a controlled corpus.

Splits the controlled sets whole-set into a calibration and a test half, fits the calibrator on the
calibration half, applies it to the test half, and measures prediction-interval coverage/width/
Winkler and per-tier precision with Clopper-Pearson and cluster (by-set) lower bounds.  It writes
``data/corpus/<corpus>/calibration-validation.json`` labelled as controlled machinery validation.

This proves the code path end-to-end.  It is **not** a real-mix certification and certifies no tier:
the plan forbids certifying real-mix tiers from controlled renders, so the report's certification
block is all ``provisional`` with ``n_test_predictions: 0``.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from id_detector.benchmark.corpus import (
    _controlled_audio,
    _local_media,
    _prediction_set,
    _run_controlled,
    _truth_files,
)
from id_detector.benchmark.scorer import (
    BOUNDARY_TOLERANCE_MS,
    PredictionSet,
    _IdentityResolver,
    _interval_values,
    _percentile,
    _prediction_is_evaluable,
    _range_error,
    associate_occurrences,
    clopper_pearson_lower_e4,
    score_corpus,
)
from id_detector.calibrate.labeling import label_set
from id_detector.calibrate.model import (
    CERT_DIMENSIONS,
    CERT_TIERS,
    CalibrationApplier,
    _provisional_certification,
    fit_calibration,
)
from id_detector.calibrate.reconstruct import features_from_record
from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    CalibrationCertEntry,
    CalibrationValidationInterval,
    CalibrationValidationRecord,
    CalibrationValidationSet,
    CalibrationValidationTier,
    GroundTruthRecord,
)
from id_detector.io import (
    atomic_write_json,
    canonical_json_bytes,
    read_text,
    write_completion_sidecar,
)

DEFAULT_SPLIT_SEED = 20_260_904
MACHINERY_PROFILE = "controlled-machinery"
POPULATION_LABEL = "controlled -- machinery validation, not real-mix certification"
TIER_ORDER = {"unclear": 0, "possible": 1, "likely": 2, "verified": 3}


@dataclass(frozen=True)
class ValidationResult:
    validation_path: Path
    model_path: Path
    record: CalibrationValidationRecord
    n_calibration_sets: int
    n_test_sets: int


@dataclass
class _EpisodeOutcome:
    set_id: str
    work_tier: str
    version_tier: str
    boundary_tier: str
    version_evaluable: bool
    associated: bool
    work_correct: bool
    version_correct: bool
    start_ok: bool
    end_ok: bool
    start_pi: Any
    end_pi: Any
    truth_start: tuple[int, int] | None
    truth_end: tuple[int, int] | None


def _split_sets(set_ids: list[str]) -> tuple[list[str], list[str]]:
    """Deterministic whole-set split: alternate the sorted sets so both halves span the strata.

    The split is a fixed stratified alternation and does not depend on any seed (``split_seed``
    seeds only the by-set cluster bootstrap, never this partition), so no seed is taken here.
    """

    ordered = sorted(set_ids)
    calibration = [set_id for index, set_id in enumerate(ordered) if index % 2 == 0]
    test = [set_id for index, set_id in enumerate(ordered) if index % 2 == 1]
    return calibration, test


def _audio_for(project_root: Path, corpus_dir: Path, truth: GroundTruthRecord) -> Path:
    audio = _local_media(project_root, corpus_dir, truth.set_id) or _controlled_audio(
        project_root, truth.set_id
    )
    if audio is None:
        raise ValueError(f"local controlled audio is missing for {truth.set_id}")
    return audio


def _outcomes_for_set(
    truth: GroundTruthRecord, prediction_set: PredictionSet
) -> list[_EpisodeOutcome]:
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
    outcomes: list[_EpisodeOutcome] = []
    for index in order:
        episode = predictions[index]
        associated = index in associations
        version_evaluable = resolver.has_resolved_recording(episode) and not (
            associated and not truth.episodes[associations[index]].version_verified
        )
        truth_start: tuple[int, int] | None = None
        truth_end: tuple[int, int] | None = None
        start_ok = end_ok = version_correct = False
        if associated:
            actual = truth.episodes[associations[index]]
            truth_start = (actual.start_ms_range[0], actual.start_ms_range[1])
            truth_end = (actual.end_ms_range[0], actual.end_ms_range[1])
            start_ok = _range_error(episode.best_start_ms, truth_start) <= BOUNDARY_TOLERANCE_MS
            end_ok = _range_error(episode.best_end_ms, truth_end) <= BOUNDARY_TOLERANCE_MS
            version_correct = bool(
                actual.version_verified and resolver.exact_equivalent(episode, actual)
            )
        outcomes.append(
            _EpisodeOutcome(
                set_id=prediction_set.set_id,
                work_tier=episode.tiers.work,
                version_tier=episode.tiers.version,
                boundary_tier=episode.tiers.boundary,
                version_evaluable=version_evaluable,
                associated=associated,
                work_correct=associated,
                version_correct=version_correct,
                start_ok=start_ok,
                end_ok=end_ok,
                start_pi=episode.start_pi,
                end_pi=episode.end_pi,
                truth_start=truth_start,
                truth_end=truth_end,
            )
        )
    return outcomes


def _tier_field(outcome: _EpisodeOutcome, dimension: str) -> str:
    if dimension == "work":
        return outcome.work_tier
    if dimension == "version":
        return outcome.version_tier
    return outcome.boundary_tier


def _in_population(outcome: _EpisodeOutcome, dimension: str, tier: str) -> bool:
    if TIER_ORDER[_tier_field(outcome, dimension)] < TIER_ORDER[tier]:
        return False
    return not (dimension == "version" and not outcome.version_evaluable)


def _is_correct(outcome: _EpisodeOutcome, dimension: str) -> bool:
    if dimension == "work":
        return outcome.work_correct
    if dimension == "version":
        return outcome.version_correct
    if dimension == "start":
        return outcome.associated and outcome.start_ok
    if dimension == "end":
        return outcome.associated and outcome.end_ok
    return outcome.associated and outcome.start_ok and outcome.end_ok


def _cluster_lower_e4(per_set: dict[str, tuple[int, int]], seed: int) -> int:
    keys = sorted(per_set)
    if not keys:
        return 0
    rng = random.Random(seed)
    estimates: list[int] = []
    for _ in range(2_000):
        correct = total = 0
        for _ in keys:
            key = keys[rng.randrange(len(keys))]
            correct += per_set[key][0]
            total += per_set[key][1]
        estimates.append((correct * 10_000 + total // 2) // total if total else 0)
    return _percentile(estimates, 5, 100)


def _tier_rows(
    outcomes_by_set: dict[str, list[_EpisodeOutcome]], seed: int
) -> list[CalibrationValidationTier]:
    rows: list[CalibrationValidationTier] = []
    for dimension in CERT_DIMENSIONS:
        tier_field = "boundary" if dimension in {"start", "end", "boundary"} else dimension
        for tier in CERT_TIERS:
            per_set: dict[str, tuple[int, int]] = {}
            total = correct = 0
            for set_id, outcomes in outcomes_by_set.items():
                set_total = set_correct = 0
                for outcome in outcomes:
                    if _in_population(outcome, tier_field, tier):
                        set_total += 1
                        set_correct += int(_is_correct(outcome, dimension))
                if set_total:
                    per_set[set_id] = (set_correct, set_total)
                total += set_total
                correct += set_correct
            precision = (correct * 10_000 + total // 2) // total if total else 0
            rows.append(
                CalibrationValidationTier(
                    dimension=dimension,  # type: ignore[arg-type]
                    tier=tier,  # type: ignore[arg-type]
                    n=total,
                    correct=correct,
                    precision_e4=min(10_000, precision),
                    cp_lower_e4=clopper_pearson_lower_e4(correct, total),
                    cluster_lower_e4=_cluster_lower_e4(per_set, seed + len(rows) * 7),
                )
            )
    return rows


def _interval_rows(outcomes: list[_EpisodeOutcome]) -> list[CalibrationValidationInterval]:
    start = [
        (o.start_pi, o.truth_start) for o in outcomes if o.start_pi is not None and o.truth_start
    ]
    end = [(o.end_pi, o.truth_end) for o in outcomes if o.end_pi is not None and o.truth_end]

    def summarise(
        side: str, pairs: list[tuple[Any, tuple[int, int]]]
    ) -> CalibrationValidationInterval:
        covered: list[int] = []
        widths: list[int] = []
        winkler: list[int] = []
        for pi, truth_range in pairs:
            cover, width, score = _interval_values(pi, truth_range)
            covered.append(cover)
            widths.append(width)
            winkler.append(score)
        n = len(pairs)
        return CalibrationValidationInterval(
            side=side,  # type: ignore[arg-type]
            coverage_e4=(sum(covered) * 10_000 + n // 2) // n if n else 0,
            median_width_ms=_percentile(widths, 1, 2),
            p90_width_ms=_percentile(widths, 9, 10),
            winkler_score=(sum(winkler) + n // 2) // n if n else 0,
        )

    return [
        summarise("start", start),
        summarise("end", end),
        summarise("boundary", start + end),
    ]


def _frozen_subset_truth(corpus_dir: Path, set_ids: list[str], destination: Path) -> Path:
    manifest = json.loads(read_text(corpus_dir / "corpus-version.json"))
    kept = [item for item in manifest.get("sets", []) if item.get("set_id") in set(set_ids)]
    for item in kept:
        source = corpus_dir / str(item["path"])
        target = destination / str(item["set_id"]) / "ground_truth.json"
        atomic_write_json(target, json.loads(read_text(source)))
        item["path"] = f"{item['set_id']}/ground_truth.json"
    atomic_write_json(
        destination / "corpus-version.json",
        {**manifest, "sets": sorted(kept, key=lambda item: item["set_id"])},
    )
    return destination


async def run_calibration_validation(
    *,
    corpus_version: str,
    project_root: Path,
    work_root: Path,
    split_seed: int = DEFAULT_SPLIT_SEED,
    out_path: Path | None = None,
    model_out: Path | None = None,
) -> ValidationResult:
    corpus_dir = project_root / "data" / "corpus" / corpus_version
    manifest_path = corpus_dir / "corpus-version.json"
    if (
        not manifest_path.is_file()
        or json.loads(read_text(manifest_path)).get("frozen") is not True
    ):
        raise ValueError(f"calibration validation requires a frozen corpus: {corpus_version}")
    truths = {
        truth.set_id: truth
        for truth in (
            GroundTruthRecord.model_validate_json(read_text(path))
            for path in _truth_files(corpus_dir, None)
        )
    }
    if any(truth.corpus_version != corpus_version for truth in truths.values()):
        raise ValueError("truth corpus_version differs from requested corpus")
    calibration_ids, test_ids = _split_sets(list(truths))
    if not calibration_ids or not test_ids:
        raise ValueError("calibration validation needs at least one set per split")

    # -- calibration split (heuristic) → features + labels → fit --------------------------------
    examples = []
    calibration_sets: list[CalibrationValidationSet] = []
    for set_id in calibration_ids:
        truth = truths[set_id]
        audio = _audio_for(project_root, corpus_dir, truth)
        media_dir, fusion, _observations, _cost = await _run_controlled(
            truth, audio, project_root=project_root, work_root=work_root, calibrator=None
        )
        observations_by_id = {item.id: item for item in _observations}
        features_by_index = {
            index: features_from_record(
                episode,
                identities=fusion.identities.record,
                observations_by_id=observations_by_id,
                hints_by_id={},
                all_episodes=list(fusion.episodes.episodes),
                duration_ms=truth.source.duration_ms,
            )
            for index, episode in enumerate(fusion.episodes.episodes)
        }
        prediction_set = PredictionSet.model_validate(_prediction_set(set_id, fusion, media_dir))
        examples.extend(label_set(truth, prediction_set, features_by_index))
        calibration_sets.append(
            CalibrationValidationSet(
                set_id=set_id, split="calibration", n_episodes=len(prediction_set.episodes)
            )
        )

    config_hash = sha256(
        canonical_json_bytes(
            {"corpus": corpus_version, "split_seed": split_seed, "population": POPULATION_LABEL}
        )
    ).hexdigest()
    model = fit_calibration(
        examples,
        profile=MACHINERY_PROFILE,
        version_number=1,
        corpus_version=corpus_version,
        config_hash=config_hash,
        population=POPULATION_LABEL,
        split_seed=split_seed,
        calibration_set_ids=calibration_ids,
        test_version="controlled-machinery",
    )
    applier = CalibrationApplier(model)
    model_path = model_out or (
        project_root / "data" / "local" / "calibration" / corpus_version / model.version
    )
    atomic_write_json(model_path, model)
    write_completion_sidecar(model_path, {})

    # -- test split (calibrated) → PI + per-tier metrics ----------------------------------------
    outcomes_by_set: dict[str, list[_EpisodeOutcome]] = {}
    test_sets: list[CalibrationValidationSet] = []
    calibrated_prediction_sets: list[dict[str, Any]] = []
    for set_id in test_ids:
        truth = truths[set_id]
        audio = _audio_for(project_root, corpus_dir, truth)
        media_dir, fusion, _observations, _cost = await _run_controlled(
            truth, audio, project_root=project_root, work_root=work_root, calibrator=applier
        )
        prediction_set = PredictionSet.model_validate(_prediction_set(set_id, fusion, media_dir))
        outcomes_by_set[set_id] = _outcomes_for_set(truth, prediction_set)
        calibrated_prediction_sets.append(_prediction_set(set_id, fusion, media_dir))
        test_sets.append(
            CalibrationValidationSet(
                set_id=set_id, split="test", n_episodes=len(prediction_set.episodes)
            )
        )

    tier_rows = _tier_rows(outcomes_by_set, split_seed)
    interval_rows = _interval_rows(
        [outcome for outcomes in outcomes_by_set.values() for outcome in outcomes]
    )

    # -- prove the scorer certification path yields all-provisional on this test split ----------
    certification = _score_certification(
        corpus_version=corpus_version,
        project_root=project_root,
        work_root=work_root,
        test_ids=test_ids,
        prediction_sets=calibrated_prediction_sets,
        corpus_dir=corpus_dir,
    )

    n_test_predictions = sum(len(outcomes) for outcomes in outcomes_by_set.values())
    record = CalibrationValidationRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        corpus_version=corpus_version,
        profile=MACHINERY_PROFILE,
        config_hash=config_hash,
        population="controlled -- not real-mix certification",
        calibration_model_ref=sha256(canonical_json_bytes(model)).hexdigest(),
        split_seed=split_seed,
        calibration_sets=sorted(calibration_sets, key=lambda item: item.set_id),
        test_sets=sorted(test_sets, key=lambda item: item.set_id),
        n_calibration_predictions=len(examples),
        n_test_predictions=n_test_predictions,
        tiers=tier_rows,
        intervals=interval_rows,
        certification=certification,
        notes=[
            "Controlled machinery validation of the Stage 5 calibration code path: fit on the "
            "calibration half, applied to the test half. It proves feature extraction, isotonic "
            "tier calibration and empirical prediction intervals run end-to-end.",
            "It is NOT a real-mix certification and certifies no tier: the plan forbids certifying "
            "real-mix tiers from controlled renders, so every certification entry is provisional "
            "with n_test_predictions 0. The per-tier precision and PI coverage above are the "
            "machinery's controlled-corpus behaviour only.",
            f"Deterministic stratified whole-set split: {len(calibration_ids)} calibration sets, "
            f"{len(test_ids)} test sets (the split is seed-independent; split_seed {split_seed} "
            "seeds only the by-set cluster bootstrap). The controlled oracle measures the fuser, "
            "not open-world recognition accuracy.",
        ],
    )
    validation_path = out_path or (corpus_dir / "calibration-validation.json")
    atomic_write_json(validation_path, record)
    return ValidationResult(
        validation_path=validation_path,
        model_path=model_path,
        record=record,
        n_calibration_sets=len(calibration_ids),
        n_test_sets=len(test_ids),
    )


def _score_certification(
    *,
    corpus_version: str,
    project_root: Path,
    work_root: Path,
    test_ids: list[str],
    prediction_sets: list[dict[str, Any]],
    corpus_dir: Path,
) -> list[CalibrationCertEntry]:
    """Score the calibrated test split with pre-registered targets; the block is all-provisional.

    Controlled sets are excluded from the real-mix test population by construction, so the scorer's
    certification is provisional with a zero denominator no matter how accurate the machinery is.
    """

    from id_detector.calibrate.certify import build_prediction_document, registered_targets

    truth_dir = work_root.resolve() / ".calibration-validation" / corpus_version / "truth"
    _frozen_subset_truth(corpus_dir, test_ids, truth_dir)
    document = build_prediction_document(
        corpus_version=corpus_version,
        profile="free",
        prediction_sets=prediction_sets,
        certification_targets=registered_targets("free"),
        project_root=project_root,
        unverified=False,
    )
    predictions_path = (
        work_root.resolve() / ".calibration-validation" / corpus_version / "predictions.json"
    )
    atomic_write_json(predictions_path, document)
    report = score_corpus(truth_dir, predictions_path)
    entries: list[CalibrationCertEntry] = []
    by_key = {(item.dimension, item.tier): item for item in report.certification}
    for dimension in CERT_DIMENSIONS:
        for tier in CERT_TIERS:
            row = by_key.get((dimension, tier))
            entries.append(
                CalibrationCertEntry(
                    dimension=dimension,  # type: ignore[arg-type]
                    tier=tier,  # type: ignore[arg-type]
                    status=row.status if row is not None else "provisional",
                    n_test_predictions=row.n if row is not None else 0,
                    lower_bound_e4=row.lower_bound_e4 if row is not None else 0,
                    test_version="controlled-machinery",
                )
            )
    # Belt and braces: controlled machinery can never be certified.
    if any(entry.status != "provisional" or entry.n_test_predictions for entry in entries):
        return _provisional_certification("controlled-machinery")
    return entries
