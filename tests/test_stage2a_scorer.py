from __future__ import annotations

import json
import math
from hashlib import sha1, sha256
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from id_detector.benchmark.scorer import (
    PredictionDocument,
    PredictionSet,
    ScoredEpisode,
    _interval_values,
    clopper_pearson_lower_e4,
    exact_equivalent,
    paired_non_inferiority,
    score_corpus,
    score_set,
)
from id_detector.contracts import GroundTruthRecord, TruthVersion, TruthWork
from id_detector.io import atomic_write_json, canonical_json_bytes


def _config(
    seed: int, targets: list[dict] | None = None, *, profile: str = "vector"
) -> tuple[dict, str]:
    snapshot = {
        "schema_version": "1.0.0",
        "config_version": "vector-registration-v1",
        "profile": profile,
        "bootstrap_seed": seed,
        "certification_targets": targets or [],
    }
    return snapshot, sha256(canonical_json_bytes(snapshot)).hexdigest()


def _contains_float(value: object) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_float(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_float(item) for item in value)
    return False


def _truth_episode(
    title: str,
    start: int,
    end: int,
    *,
    occurrence: int = 0,
    role: str = "dominant",
    overlaps: list[int] | None = None,
    note: str | None = None,
) -> dict:
    return {
        "work": {"artist": "Vector Artist", "title": title},
        "version": {"qualifier": "Exact Mix", "ids": {"isrc": f"VECTOR-{title}"}},
        "version_verified": True,
        "verified_against": "source_recording",
        "start_ms_range": [start, start],
        "end_ms_range": [end, end],
        "audible_rule": "hand-built exact vector",
        "role_segments": [{"from_ms": start, "to_ms": end, "role": role}],
        "overlaps_with": overlaps or [],
        "occurrence_index": occurrence,
        "in_reference_pool": True,
        "annotator_ref": "vector-first",
        "second_pass_ref": "vector-second",
        "disagreement_resolution": "agreed",
        "note": note,
        "draft": False,
    }


def _prediction(
    title: str,
    start: int,
    end: int,
    *,
    support: tuple[int, int] | None = None,
    supports: list[tuple[int, int]] | None = None,
    occurrence: int = 0,
    role: str = "dominant",
    start_bound: int | None = None,
    end_bound: int | None = None,
    start_pi: tuple[int, int] | None = None,
    end_pi: tuple[int, int] | None = None,
    event: tuple[str, int] | None = None,
) -> dict:
    supports = supports or [support or (start, min(end, start + 12_000))]

    def pi(value: tuple[int, int] | None) -> dict | None:
        return (
            {
                "lo": value[0],
                "hi": value[1],
                "coverage_target": 9_000,
                "method": "vector",
                "calibrated": True,
            }
            if value
            else None
        )

    return {
        "work": {"artist": "vector artist", "title": title.casefold()},
        "version": {"qualifier": "Exact Mix", "ids": {"isrc": f"VECTOR-{title}"}},
        "candidate_id": sha1(f"recording|{title.casefold()}".encode()).hexdigest(),
        "evidence_support_ms": [list(item) for item in supports],
        "start_no_later_than_ms": (
            min(item[1] for item in supports) if start_bound is None else start_bound
        ),
        "end_no_earlier_than_ms": (
            max(item[0] for item in supports) if end_bound is None else end_bound
        ),
        "start_pi": pi(start_pi or (max(0, start - 1_000), start + 1_000)),
        "end_pi": pi(end_pi or (end - 1_000, end + 1_000)),
        "best_start_ms": start,
        "best_end_ms": end,
        "role_segments": [{"from_ms": start, "to_ms": end, "role": role}],
        "occurrence_index": occurrence,
        "claim": "performed",
        "scores": {"work": 10_000, "version": 10_000, "boundary": 10_000},
        "tiers": {"work": "likely", "version": "likely", "boundary": "possible"},
        "alignment_events": ([{"type": event[0], "at_ms": event[1]}] if event else []),
    }


def _identities(predictions: list[dict]) -> dict:
    nodes: dict[str, dict] = {}
    works: dict[str, dict] = {}
    candidates: dict[str, dict] = {}
    for prediction in predictions:
        title = prediction["work"]["title"]
        work_id = sha1(f"work|vector artist|{title.casefold()}".encode()).hexdigest()
        text_node = f"text:vector artist|{title.casefold()}"
        isrc_node = f"isrc:{prediction['version']['ids']['isrc']}"
        for node_id, namespace in ((text_node, "text"), (isrc_node, "isrc")):
            nodes[node_id] = {
                "schema_version": "1.0.0",
                "generated_by": "test-vector",
                "id": node_id,
                "ns": namespace,
                "label": f"Vector Artist - {title}",
            }
        work = works.setdefault(
            work_id,
            {
                "schema_version": "1.0.0",
                "generated_by": "test-vector",
                "work_id": work_id,
                "member_nodes": [],
            },
        )
        work["member_nodes"] = sorted(set(work["member_nodes"]) | {text_node, isrc_node})
        candidates[prediction["candidate_id"]] = {
            "schema_version": "1.0.0",
            "generated_by": "test-vector",
            "canonical_id": prediction["candidate_id"],
            "work_id": work_id,
            "member_nodes": [isrc_node],
            "alternatives": [],
            "contested": False,
            "conflicts": [],
        }
    return {
        "schema_version": "1.0.0",
        "generated_by": "test-vector",
        "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
        "assertions": [],
        "works": sorted(works.values(), key=lambda item: item["work_id"]),
        "candidates": sorted(candidates.values(), key=lambda item: item["canonical_id"]),
    }


def _vector() -> tuple[GroundTruthRecord, PredictionSet]:
    truth_episodes = [
        _truth_episode("Long", 10_000, 310_000, note="event:jump@110000"),
        _truth_episode("Repeat", 320_000, 360_000, note="event:loop@340000"),
        _truth_episode(
            "Layer A",
            370_000,
            430_000,
            role="outgoing",
            overlaps=[3],
            note="event:reset@400000",
        ),
        _truth_episode(
            "Layer B",
            390_000,
            450_000,
            role="incoming",
            overlaps=[2],
            note="event:drift@410000",
        ),
        _truth_episode("Repeat", 450_000, 490_000, occurrence=1),
    ]
    truth = GroundTruthRecord.model_validate(
        {
            "schema_version": "1.0.0",
            "generated_by": "test-vector",
            "set_id": "hand-vector",
            "source": {
                "url_ref": "source-vector",
                "media_key": "a" * 64,
                "duration_ms": 600_000,
                "platform": "file",
                "uploader_ref": "uploader-vector",
                "event_ref": None,
                "date": None,
            },
            "stratum": "catalogue-covered",
            "split": "dev-1",
            "corpus_version": "vector-v1",
            "selection_basis": "authored before implementation",
            "episodes": truth_episodes,
            "regions": [
                {"start_ms": 500_000, "end_ms": 520_000, "type": "out_of_pool"},
                {"start_ms": 520_000, "end_ms": 540_000, "type": "unresolved"},
                {"start_ms": 540_000, "end_ms": 560_000, "type": "silence_or_speech"},
            ],
        }
    )
    predictions = [
        # A five-minute truth occurrence with only 12 seconds of support is still an identity TP.
        _prediction(
            "Long",
            10_000,
            310_000,
            supports=[(0, 5_000), (315_000, 320_000)],
            start_pi=(0, 60_000),
            end_pi=(260_000, 360_000),
            event=("jump", 110_000),
        ),
        _prediction("Repeat", 320_000, 360_000, event=("loop", 340_000)),
        _prediction("Layer A", 370_000, 430_000, role="outgoing", event=("reset", 400_000)),
        _prediction("Layer B", 390_000, 450_000, role="incoming", event=("drift", 410_000)),
        _prediction("Repeat", 450_000, 490_000, occurrence=1),
        # Any ID is correct in out-of-pool time. Unresolved is excluded from precision; silence is
        # excluded completely. None of these becomes an identity false positive.
        _prediction("Unknown OOP", 500_000, 520_000, support=(500_000, 512_000)),
        _prediction("Unknown unresolved", 520_000, 540_000, support=(520_000, 532_000)),
        _prediction("Unknown silence", 540_000, 560_000, support=(540_000, 552_000)),
    ]
    return truth, PredictionSet(
        set_id=truth.set_id, identities=_identities(predictions), episodes=predictions
    )


def test_hand_computed_scorer_vector_covers_every_metric_family() -> None:
    truth, predictions = _vector()
    metrics = score_set(truth, predictions).metrics.model_dump(mode="json")

    perfect_prf = {"precision_e4": 10_000, "recall_e4": 10_000, "f1_e4": 10_000}
    perfect_pr = {"precision_e4": 10_000, "recall_e4": 10_000}
    assert metrics["identification_work"] == perfect_prf  # four unique works
    assert metrics["identification_version"] == perfect_prf
    assert metrics["occurrence"] == perfect_prf  # five occurrences, including occurrence 2
    assert metrics["segment_micro"] == perfect_pr
    assert metrics["segment_macro_by_set"] == perfect_pr
    assert metrics["selective_precision_e4"] == 10_000
    assert metrics["selective_recall_e4"] == 10_000
    assert metrics["selective_coverage_e4"] == 10_000
    assert metrics["false_discovery_rate_e4"] == 0
    assert metrics["calibration_error_e4"] == 0

    assert metrics["start_median_absolute_error_ms"] == 0
    assert metrics["start_p90_error_ms"] == 0
    assert metrics["end_median_absolute_error_ms"] == 0
    assert metrics["end_p90_error_ms"] == 0
    for side in ("start", "end"):
        assert metrics[f"{side}_within_5s_e4"] == 10_000
        assert metrics[f"{side}_within_10s_e4"] == 10_000
        assert metrics[f"{side}_within_30s_e4"] == 10_000
        assert metrics[f"{side}_bound_n"] == 5
        assert metrics[f"{side}_bound_violation_e4"] == 2_000  # one of five

    # Four tight 2 s PIs and one wide PI on each side; all contain the exact truth point.
    assert metrics["start_interval_coverage_e4"] == 10_000
    assert metrics["start_interval_median_width_ms"] == 2_000
    assert metrics["start_interval_p90_width_ms"] == 60_000
    assert metrics["start_interval_winkler_score"] == 13_600
    assert metrics["end_interval_coverage_e4"] == 10_000
    assert metrics["end_interval_median_width_ms"] == 2_000
    assert metrics["end_interval_p90_width_ms"] == 100_000
    assert metrics["end_interval_winkler_score"] == 21_600
    assert metrics["boundary_interval_coverage_e4"] == 10_000
    assert metrics["boundary_interval_median_width_ms"] == 2_000
    assert metrics["boundary_interval_p90_width_ms"] == 60_000
    assert metrics["boundary_winkler_score"] == 17_600

    assert metrics["episode_iou_e4"] == 10_000
    assert metrics["repeated_occurrence_recall_e4"] == 10_000
    assert metrics["overlap_recall_e4"] == 10_000
    for event in ("jump", "loop", "reset", "drift"):
        assert metrics[f"event_{event}"] == {
            "precision_e4": 10_000,
            "recall_e4": 10_000,
            "n": 1,
        }
    assert metrics["performed_component_confusion"] == {
        "performed_as_performed": 5,
        "performed_as_component": 0,
        "component_as_performed": 0,
        "component_as_component": 0,
    }
    assert metrics["dominant_layer"] == perfect_pr
    assert metrics["secondary_layer"] == perfect_pr
    assert metrics["unknown_region"] == {"precision_e4": 3_333, "recall_e4": 10_000}
    assert metrics["physical_attempts"] == 0

    # Cumulative at-or-above-tier: all five are possible/likely, none is verified.
    assert metrics["empirical_tier_precision_e4"] == {
        "possible": 10_000,
        "likely": 10_000,
        "verified": 0,
    }
    expected_five_of_five = math.floor(0.05 ** (1 / 5) * 10_000)
    assert metrics["empirical_tier_lower_bound_e4"] == {
        "possible": expected_five_of_five,
        "likely": expected_five_of_five,
        "verified": 0,
    }


def test_wide_vague_interval_has_worse_winkler_score_than_tight_interval() -> None:
    tight = _interval_values(
        ScoredEpisode.model_validate(_prediction("X", 10_000, 20_000)).start_pi,
        (10_000, 10_000),
    )
    wide_episode = ScoredEpisode.model_validate(
        _prediction("X", 10_000, 20_000, start_pi=(0, 40_000))
    )
    wide = _interval_values(wide_episode.start_pi, (10_000, 10_000))
    assert tight == (1, 2_000, 2_000)
    assert wide == (1, 40_000, 40_000)
    assert wide[2] > tight[2]


def test_proved_bounds_must_be_derived_from_evidence_support() -> None:
    forged = _prediction("X", 10_000, 20_000, support=(12_000, 16_000))
    forged["start_no_later_than_ms"] = 10_000
    with pytest.raises(ValidationError, match="min evidence support end"):
        ScoredEpisode.model_validate(forged)


def test_exact_equivalence_requires_consistent_recording_specific_ids() -> None:
    work = TruthWork(artist="Artist", title="Title")
    assert not exact_equivalent(
        work,
        TruthVersion(qualifier="Original", ids={"mb_work": "same-work"}),
        work,
        TruthVersion(qualifier="Remix", ids={"mb_work": "same-work"}),
    )
    assert not exact_equivalent(
        work,
        TruthVersion(qualifier=None, ids={"isrc": "same", "mb_recording": "left"}),
        work,
        TruthVersion(qualifier=None, ids={"isrc": "same", "mb_recording": "right"}),
    )
    assert exact_equivalent(
        work,
        TruthVersion(qualifier="Different label", ids={"isrc": "same"}),
        work,
        TruthVersion(qualifier="Truth label", ids={"isrc": "same"}),
    )


def test_contested_recording_identity_cannot_claim_a_version_tier() -> None:
    _, prediction_set = _vector()
    payload = prediction_set.model_dump(mode="json")
    candidate_id = payload["episodes"][0]["candidate_id"]
    candidate = next(
        item for item in payload["identities"]["candidates"] if item["canonical_id"] == candidate_id
    )
    candidate["contested"] = True
    candidate["conflicts"] = ["isrc:conflict"]
    with pytest.raises(ValidationError, match="unclear version tier"):
        PredictionSet.model_validate(payload)


def test_work_only_truth_is_excluded_from_exact_version_metrics() -> None:
    truth, predictions = _vector()
    only_truth = truth.episodes[0].model_copy(
        update={"version_verified": False, "verified_against": "audio"}
    )
    truth = GroundTruthRecord.model_validate(
        truth.model_copy(update={"episodes": [only_truth], "regions": []}).model_dump(mode="json")
    )
    prediction = predictions.episodes[0]
    identities = predictions.identities.model_copy(
        update={
            "works": [
                item
                for item in predictions.identities.works
                if item.work_id
                == next(
                    candidate.work_id
                    for candidate in predictions.identities.candidates
                    if candidate.canonical_id == prediction.candidate_id
                )
            ],
            "candidates": [
                item
                for item in predictions.identities.candidates
                if item.canonical_id == prediction.candidate_id
            ],
        }
    )
    node_ids = {
        node for item in [*identities.works, *identities.candidates] for node in item.member_nodes
    }
    identities = identities.model_copy(
        update={"nodes": [node for node in identities.nodes if node.id in node_ids]}
    )
    scored = score_set(
        truth,
        PredictionSet(set_id=truth.set_id, identities=identities, episodes=[prediction]),
    )
    assert scored.state.identification_work == scored.state.identification_work.__class__(1, 1, 1)
    assert scored.state.identification_version == scored.state.identification_version.__class__(
        0, 0, 0
    )
    assert scored.state.certification[("version", "possible")] == (0, 0)


def test_clopper_pearson_and_paired_non_inferiority_are_deterministic() -> None:
    assert clopper_pearson_lower_e4(0, 20) == 0
    assert clopper_pearson_lower_e4(5, 5) == math.floor(0.05 ** (1 / 5) * 10_000)
    baseline = {"a": (95, 100), "b": (90, 100), "c": (100, 100)}
    same = paired_non_inferiority(baseline, baseline, seed=7, replicates=200)
    assert same == {
        "delta_e4": 0,
        "lower_bound_e4": 0,
        "margin_e4": 100,
        "pass": True,
        "n_sets": 3,
    }
    assert paired_non_inferiority(baseline, baseline, seed=7, replicates=200) == same
    worse = {key: (correct - 5, total) for key, (correct, total) in baseline.items()}
    assert paired_non_inferiority(baseline, worse, seed=7, replicates=200)["pass"] is False


def test_score_cli_plumbing_report_validates_schema_and_has_only_integers(tmp_path: Path) -> None:
    truth, predictions = _vector()
    truth_dir = tmp_path / "truth" / truth.set_id
    truth_dir.mkdir(parents=True)
    atomic_write_json(truth_dir / "ground_truth.json", truth)
    prediction_path = tmp_path / "predictions.json"
    config_snapshot, config_hash = _config(11)
    atomic_write_json(
        prediction_path,
        {
            "corpus_version": "vector-v1",
            "profile": "vector",
            "config_hash": config_hash,
            "config_snapshot": config_snapshot,
            "sets": [predictions],
            "cost": {
                "requests": 5,
                "physical_attempts": 7,
                "billable_seconds": 0,
                "usd_e2": 0,
                "wall_ms": 10,
            },
        },
    )
    report_path = tmp_path / "report.json"
    report = score_corpus(tmp_path / "truth", prediction_path, out_path=report_path)
    repeated_path = tmp_path / "report-repeated.json"
    score_corpus(tmp_path / "truth", prediction_path, out_path=repeated_path)
    assert report_path.read_bytes() == repeated_path.read_bytes()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1] / "docs/schemas/benchmark_report.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(payload)
    assert not _contains_float(payload)
    assert report.overall.physical_attempts == 7
    assert report.cost.usd_e2 == 0
    assert all(isinstance(item.lower_bound_e4, int) for item in report.certification)
    assert all(item.status == "provisional" for item in report.certification)
    assert all(item.target_e4 is None for item in report.certification)


def test_prediction_document_rejects_a_forged_config_hash() -> None:
    _, predictions = _vector()
    snapshot, _ = _config(7)
    with pytest.raises(ValidationError, match="config_hash does not match"):
        PredictionDocument.model_validate(
            {
                "corpus_version": "vector-v1",
                "profile": "vector",
                "config_hash": "0" * 64,
                "config_snapshot": snapshot,
                "sets": [predictions],
            }
        )


def test_certification_uses_profile_dimension_tier_preregistration(tmp_path: Path) -> None:
    truth_root = tmp_path / "truth"
    prediction_sets = []
    for index in range(10):
        truth, predictions = _vector()
        set_id = f"test-set-{index:02d}"
        truth = GroundTruthRecord.model_validate(
            {
                **truth.model_dump(mode="json"),
                "set_id": set_id,
                "split": "test",
            }
        )
        set_dir = truth_root / set_id
        set_dir.mkdir(parents=True)
        atomic_write_json(set_dir / "ground_truth.json", truth)
        prediction_sets.append(
            PredictionSet(
                set_id=set_id,
                identities=predictions.identities,
                episodes=predictions.episodes,
            )
        )
    targets = [
        {"profile": "vector", "dimension": "work", "tier": "possible", "target_e4": 9_000},
        {
            "profile": "vector",
            "dimension": "version",
            "tier": "possible",
            "target_e4": 9_500,
        },
        {"profile": "other", "dimension": "start", "tier": "possible", "target_e4": 0},
    ]
    snapshot, config_hash = _config(31, targets)
    predictions_path = tmp_path / "predictions.json"
    atomic_write_json(
        predictions_path,
        {
            "corpus_version": "vector-v1",
            "profile": "vector",
            "config_hash": config_hash,
            "config_snapshot": snapshot,
            "sets": prediction_sets,
        },
    )
    report = score_corpus(truth_root, predictions_path)
    entries = {(item.dimension, item.tier): item for item in report.certification}
    assert entries[("work", "possible")].status == "certified"
    assert entries[("work", "possible")].target_e4 == 9_000
    assert entries[("work", "possible")].registration_version == "vector-registration-v1"
    assert entries[("version", "possible")].status == "provisional"
    assert entries[("version", "possible")].target_e4 == 9_500
    assert entries[("start", "possible")].status == "provisional"
    assert entries[("start", "possible")].target_e4 is None


def test_scorer_uses_resolved_work_identity_instead_of_episode_text() -> None:
    truth, predictions = _vector()
    episodes = list(predictions.episodes)
    episodes[0] = episodes[0].model_copy(
        update={"work": TruthWork(artist="Misleading label", title="Not the truth title")}
    )
    scored = score_set(
        truth,
        PredictionSet(
            set_id=predictions.set_id,
            identities=predictions.identities,
            episodes=episodes,
        ),
    )
    assert scored.metrics.identification_work.precision_e4 == 10_000
    assert scored.metrics.identification_work.recall_e4 == 10_000
