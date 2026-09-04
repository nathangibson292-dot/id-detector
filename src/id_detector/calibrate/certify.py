"""Single frozen-test certification evaluation (Stage 5).

``id-detector benchmark certify --corpus <version> --profile <name> --test-version <v>`` runs the
frozen profile over a **frozen** corpus, scores it with the pre-registered per
``(dimension, tier)`` targets, and writes the certification report.  It refuses to run on a corpus
that is not frozen and refuses to re-run the same ``(profile, test_version)`` without a new test
version, so a test set is evaluated exactly once.

Certification populations are real-mix test sets only; controlled and self-index sets are excluded
by the scorer.  With no owner-verified real-mix test corpus, every certification entry is
``provisional`` with a zero denominator — the machinery is exercised, nothing is fabricated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from id_detector.benchmark import corpus as corpus_mod
from id_detector.benchmark.scorer import (
    CertificationTarget,
    PredictionDocument,
    ScoringConfigSnapshot,
    is_certification_stratum,
    score_corpus,
    truth_is_frozen_verified,
)
from id_detector.calibrate.model import (
    CERT_DIMENSIONS,
    CERT_TIERS,
    TIER_TARGET_E4,
    load_calibration,
)
from id_detector.contracts import BenchmarkCost, BenchmarkReportRecord, GroundTruthRecord
from id_detector.io import atomic_write_json, canonical_json_bytes, read_text
from id_detector.profiles import UnknownProfile, load_profile

BOOTSTRAP_SEED = 20_260_904


class CorpusNotFrozen(ValueError):
    """Raised when certification is attempted on a corpus that is not frozen."""


class DuplicateTestVersion(ValueError):
    """Raised when a ``(profile, test_version)`` pair has already been certified."""


@dataclass(frozen=True)
class CertifyResult:
    report: BenchmarkReportRecord
    report_path: Path
    n_test_predictions: int
    n_certified: int


def registered_targets(profile: str) -> list[CertificationTarget]:
    """Pre-registered per ``(dimension, tier)`` precision targets (possible/likely/verified)."""

    return [
        CertificationTarget(
            profile=profile,
            dimension=dimension,  # type: ignore[arg-type]
            tier=tier,  # type: ignore[arg-type]
            target_e4=TIER_TARGET_E4[tier],
        )
        for dimension in CERT_DIMENSIONS
        for tier in CERT_TIERS
    ]


def build_prediction_document(
    *,
    corpus_version: str,
    profile: str,
    prediction_sets: list[dict[str, Any]],
    certification_targets: list[CertificationTarget],
    project_root: Path,
    unverified: bool,
    cost: BenchmarkCost | None = None,
) -> PredictionDocument:
    snapshot = ScoringConfigSnapshot(
        schema_version="1.0.0",
        config_version="stage5-certify-v1",
        profile=profile,
        bootstrap_seed=BOOTSTRAP_SEED,
        certification_targets=certification_targets,
        run_config={
            "set_ids": sorted(item["set_id"] for item in prediction_sets),
            "certification": "pre-registered targets possible 0.70 / likely 0.90 / verified 0.99",
        },
    )
    config_hash = sha256(canonical_json_bytes(snapshot)).hexdigest()
    return PredictionDocument(
        corpus_version=corpus_version,
        profile=profile,
        config_hash=config_hash,
        config_snapshot=snapshot,
        sets=sorted(prediction_sets, key=lambda item: item["set_id"]),
        engines=[],
        cost=cost
        or BenchmarkCost(requests=0, physical_attempts=0, billable_seconds=0, usd_e2=0, wall_ms=0),
        unverified_seed_comparison=unverified,
    )


def _population_prediction_count(
    truths: list[GroundTruthRecord], prediction_sets: list[dict[str, Any]]
) -> int:
    """Actual predicted-episode count over the certification population (real-mix test sets).

    Consistent with ``validate.py``'s per-episode count -- and unlike ``sum(entry.n for entry in
    report.certification)``, which sums the 15 overlapping cumulative tier populations and so
    double-counts each prediction across dimensions and nested tiers.
    """

    population_ids = {
        truth.set_id
        for truth in truths
        if truth.split == "test" and is_certification_stratum(truth.stratum)
    }
    return sum(
        len(item["episodes"]) for item in prediction_sets if item["set_id"] in population_ids
    )


def _require_frozen(corpus_dir: Path, corpus_version: str) -> None:
    manifest_path = corpus_dir / "corpus-version.json"
    if not manifest_path.is_file():
        raise CorpusNotFrozen(
            f"corpus {corpus_version} has no freeze manifest; certification refused"
        )
    if json.loads(read_text(manifest_path)).get("frozen") is not True:
        raise CorpusNotFrozen(
            f"corpus {corpus_version} is not frozen; freeze it before certification"
        )


def _registry_path(project_root: Path, corpus_version: str, profile: str) -> Path:
    return (
        project_root
        / "data"
        / "local"
        / "certification"
        / corpus_version
        / profile
        / "registry.json"
    )


def _guard_test_version(registry_path: Path, profile: str, test_version: str) -> list[str]:
    used: list[str] = []
    if registry_path.is_file():
        used = list(json.loads(read_text(registry_path)).get("test_versions", []))
    if test_version in used:
        raise DuplicateTestVersion(
            f"({profile}, {test_version}) already certified; use a new --test-version"
        )
    return used


async def run_certify(
    *,
    corpus_version: str,
    profile: str,
    test_version: str,
    project_root: Path,
    work_root: Path,
    out_path: Path | None = None,
    max_requests: int = 2_000,
) -> CertifyResult:
    corpus_dir = project_root / "data" / "corpus" / corpus_version
    _require_frozen(corpus_dir, corpus_version)
    try:
        profile_record = load_profile(project_root, profile)
    except UnknownProfile as exc:
        raise ValueError(str(exc)) from None
    registry_path = _registry_path(project_root, corpus_version, profile)
    used = _guard_test_version(registry_path, profile, test_version)

    report_path = out_path or (
        project_root
        / "data"
        / "local"
        / "certification"
        / corpus_version
        / profile
        / f"certification-{test_version}.json"
    )
    if report_path.exists():
        raise DuplicateTestVersion(
            f"certification report already exists for {test_version}: {report_path}"
        )

    # The frozen profile is loaded to enforce that certification runs a real frozen profile; a
    # committed calibration model for it (none exists for the real-mix profiles) would supply
    # calibrated scores/tiers, otherwise the pipeline stays heuristic.
    calibrator = load_calibration(project_root, profile_record.name)
    links = corpus_mod._source_links(project_root)

    truths = [
        GroundTruthRecord.model_validate_json(read_text(path))
        for path in corpus_mod._truth_files(corpus_dir, None)
    ]
    if any(truth.corpus_version != corpus_version for truth in truths):
        raise ValueError("truth corpus_version differs from requested corpus")

    prediction_sets: list[dict[str, Any]] = []
    costs: list[BenchmarkCost] = []
    for truth in truths:
        controlled = "controlled" in truth.stratum.casefold()
        audio = corpus_mod._local_media(project_root, corpus_dir, truth.set_id)
        if controlled:
            audio = audio or corpus_mod._controlled_audio(project_root, truth.set_id)
            if audio is None:
                raise ValueError(f"local controlled audio is missing for {truth.set_id}")
            media_dir, fusion, _observations, cost = await corpus_mod._run_controlled(
                truth,
                audio,
                project_root=project_root,
                work_root=work_root,
                calibrator=calibrator,
            )
        else:
            source_link = links.get(truth.source.url_ref)
            source = corpus_mod._cached_link_source(work_root, truth.source.media_key, source_link)
            source = source or (str(audio) if audio is not None else source_link)
            if source is None:
                raise ValueError(f"no local media or source link for {truth.set_id}")
            media_dir, fusion, cost = await corpus_mod._run_real(
                source,
                truth=truth,
                work_root=work_root,
                max_requests=max_requests,
                include_hints=False,
                profile=profile_record.name,
            )
        prediction_sets.append(corpus_mod._prediction_set(truth.set_id, fusion, media_dir))
        costs.append(cost)

    unverified = not truth_is_frozen_verified(corpus_dir, truths)
    document = build_prediction_document(
        corpus_version=corpus_version,
        profile=profile,
        prediction_sets=prediction_sets,
        certification_targets=registered_targets(profile),
        project_root=project_root,
        unverified=unverified,
        cost=BenchmarkCost(
            requests=sum(item.requests for item in costs),
            physical_attempts=sum(item.physical_attempts for item in costs),
            billable_seconds=sum(item.billable_seconds for item in costs),
            usd_e2=sum(item.usd_e2 for item in costs),
            wall_ms=sum(item.wall_ms for item in costs),
        ),
    )
    predictions_path = report_path.with_name(f"predictions-{test_version}.json")
    atomic_write_json(predictions_path, document)
    report = score_corpus(corpus_dir, predictions_path, out_path=report_path)

    atomic_write_json(registry_path, {"test_versions": sorted({*used, test_version})})
    n_test = _population_prediction_count(truths, prediction_sets)
    n_certified = sum(entry.status == "certified" for entry in report.certification)
    return CertifyResult(
        report=report,
        report_path=report_path,
        n_test_predictions=n_test,
        n_certified=n_certified,
    )
