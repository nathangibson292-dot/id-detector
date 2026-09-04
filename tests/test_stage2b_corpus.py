from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from id_detector.benchmark.corpus import _regression, _scoring_config, _validate_source_media
from id_detector.contracts import BenchmarkReportRecord, GroundTruthRecord
from id_detector.io import canonical_json_bytes, read_text

ROOT = Path(__file__).resolve().parents[1]


def test_dev1_is_six_set_unverified_draft_inventory() -> None:
    corpus = ROOT / "data" / "corpus" / "dev-1"
    manifest = json.loads(read_text(corpus / "corpus-version.json"))
    truths = [
        GroundTruthRecord.model_validate_json(read_text(path))
        for path in sorted(corpus.rglob("ground_truth.json"))
    ]
    assert manifest["frozen"] is False
    assert manifest["verification_status"] == "unverified_seed_drafts_not_truth"
    assert len(truths) == 6
    assert sum(len(truth.episodes) for truth in truths) >= 60
    assert all(episode.draft for truth in truths for episode in truth.episodes)
    assert all("://" not in truth.source.url_ref for truth in truths)


def test_controlled_baseline_and_real_seed_comparison_are_distinct() -> None:
    controlled = BenchmarkReportRecord.model_validate_json(
        read_text(ROOT / "data" / "corpus" / "controlled-synth-1" / "baseline-free.json")
    )
    unverified = BenchmarkReportRecord.model_validate_json(
        read_text(ROOT / "data" / "corpus" / "dev-1" / "unverified-seed-comparison-free.json")
    )
    assert len(controlled.sets) == 25
    assert controlled.unverified_seed_comparison is False
    assert controlled.overall.identification_work.precision_e4 == 10_000
    assert controlled.overall.identification_work.recall_e4 == 10_000
    assert len(unverified.sets) == 1
    assert unverified.unverified_seed_comparison is True
    assert controlled.config_hash != unverified.config_hash


def test_named_baseline_regression_is_seeded_and_noninferior_to_itself() -> None:
    baseline_path = ROOT / "data" / "corpus" / "controlled-synth-1" / "baseline-free.json"
    report = BenchmarkReportRecord.model_validate_json(read_text(baseline_path))
    regression = _regression(report, baseline_path, seed=20_260_904)
    assert regression["baseline_report_ref"] == "baseline-free.json"
    assert regression["deltas"]["work_precision_e4"] == 0
    assert regression["deltas"]["segment_recall_e4"] == 0
    assert all(gate["pass"] for gate in regression["gates"])
    assert {gate["name"] for gate in regression["gates"]} == {
        "work_precision_noninferior_1pp",
        "work_recall_noninferior_1pp",
        "segment_precision_noninferior_1pp",
        "segment_recall_noninferior_1pp",
    }


def test_regression_rejects_missing_or_incompatible_pairs() -> None:
    baseline_path = ROOT / "data" / "corpus" / "controlled-synth-1" / "baseline-free.json"
    report = BenchmarkReportRecord.model_validate_json(read_text(baseline_path))
    partial = report.model_copy(update={"sets": report.sets[:-1]})
    with pytest.raises(ValueError, match="identical set populations"):
        _regression(partial, baseline_path, seed=20_260_904)
    incompatible = report.model_copy(update={"profile": "different"})
    with pytest.raises(ValueError, match="same profile"):
        _regression(incompatible, baseline_path, seed=20_260_904)


def test_corpus_source_validation_binds_hash_and_duration_to_truth() -> None:
    truth = GroundTruthRecord.model_validate_json(
        read_text(ROOT / "data" / "corpus" / "dev-1" / "dev1-set-004" / "ground_truth.json")
    )
    _validate_source_media(
        truth,
        media_key=truth.source.media_key,
        duration_ms=truth.source.duration_ms + 500,
    )
    with pytest.raises(ValueError, match="SHA-256"):
        _validate_source_media(
            truth,
            media_key="0" * 64,
            duration_ms=truth.source.duration_ms,
        )
    with pytest.raises(ValueError, match="duration"):
        _validate_source_media(
            truth,
            media_key=truth.source.media_key,
            duration_ms=truth.source.duration_ms + 501,
        )


def test_benchmark_config_hash_covers_population_provider_schedule_fuser_and_budget() -> None:
    truths = [
        GroundTruthRecord.model_validate_json(read_text(path))
        for path in sorted((ROOT / "data" / "corpus" / "dev-1").rglob("ground_truth.json"))[:2]
    ]
    one = _scoring_config(truths[:1], profile="free", max_requests=10, project_root=ROOT)
    two = _scoring_config(truths, profile="free", max_requests=10, project_root=ROOT)
    different_budget = _scoring_config(
        truths[:1], profile="free", max_requests=11, project_root=ROOT
    )
    assert one.run_config is not None
    assert set(one.run_config) == {
        "set_ids",
        "providers",
        "window_schedule",
        "fuser",
        "budget",
        "source_validation",
    }
    hashes = {
        sha256(canonical_json_bytes(config)).hexdigest() for config in (one, two, different_budget)
    }
    assert len(hashes) == 3
