from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.audit_fixtures as audit_module
from id_detector.benchmark.ablations import (
    GATE_EVENT_E4,
    GATE_MIN_BOUNDARIES,
    GATE_MIN_EVENT_CASES,
    GATE_P90_IMPROVEMENT_E4,
    ArmOptions,
    engine_status_rows,
    paired_p90_improvement,
    run_fixture_set,
)
from id_detector.contracts import GroundTruthRecord
from id_detector.io import read_text

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "data" / "corpus" / "controlled-synth-1" / "ablations.json"
EVENT_CORPUS = ROOT / "data" / "corpus" / "controlled-events-1"


@pytest.fixture(scope="module")
def report() -> dict:
    assert REPORT.is_file(), "the Stage 4c ablation decision record must be committed"
    return json.loads(read_text(REPORT))


def test_committed_ablation_report_records_every_arm_and_comparison(report: dict) -> None:
    assert report["benchmark"] == "stage-4c-ablations"
    assert report["corpus_version"] == "controlled-events-1"
    assert report["n_sets"] == 145
    assert report["n_boundaries"] >= GATE_MIN_BOUNDARIES
    assert set(report["arms"]) == {
        "rescans_off",
        "rescans_on",
        "rescans_on_no_novelty",
        "transforms_off",
        "transforms_global",
        "schedule_12_5_0",
        "schedule_8_5_0",
        "schedule_12_9_0_gen0_only",
    }
    assert set(report["comparisons"]) == {
        "rescans_on_minus_off",
        "novelty_on_minus_off",
        "transforms_rescan_only_minus_off",
        "transforms_global_minus_rescan_only",
        "schedule_12_5_minus_12_9",
        "schedule_8_5_minus_12_9",
    }
    for comparison in report["comparisons"].values():
        assert set(comparison) >= {
            "work_precision",
            "work_recall",
            "segment_precision",
            "segment_recall",
            "best_start_p90",
        }
    assert report["bootstrap"]["method"] == "paired-one-sided-95-percent-set-cluster"
    assert report["bootstrap"]["replicates"] == 2_000


def test_committed_report_contains_only_integers_and_no_leaked_identifiers(report: dict) -> None:
    def walk(value: object, path: str = "$") -> None:
        assert not isinstance(value, float), f"floating-point value at {path}"
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(report)
    assert REPORT.relative_to(ROOT) in {
        path.relative_to(ROOT)
        for scan_root in audit_module.SCAN_ROOTS
        if scan_root.exists()
        for path in scan_root.rglob("*")
        if path.is_file()
    }
    assert audit_module.audit() == []


def test_committed_gates_are_reported_with_their_numbers(report: dict) -> None:
    gates = {gate["name"]: gate for gate in report["gates"]}
    assert gates["controlled_boundaries_at_least_100"]["observed"] >= GATE_MIN_BOUNDARIES

    p90 = gates["best_start_p90_improves_20_percent_relative_with_rescans"]
    assert p90["target_e4"] == GATE_P90_IMPROVEMENT_E4
    assert p90["baseline_p90_ms"] > p90["challenger_p90_ms"]
    assert p90["observed_e4"] >= GATE_P90_IMPROVEMENT_E4
    assert p90["pass"] is True

    for event in ("loop", "jump", "drift", "replay"):
        gate = gates[f"event_{event}_precision_and_recall_at_least_80_percent"]
        assert gate["n_cases"] >= GATE_MIN_EVENT_CASES
        assert gate["precision_e4"] >= GATE_EVENT_E4
        assert gate["recall_e4"] >= GATE_EVENT_E4
        assert gate["pass"] is True


def test_committed_report_states_what_could_not_be_evaluated(report: dict) -> None:
    providers = {engine["provider"]: engine for engine in report["engines"]}
    assert providers["panako"]["status"] == "excluded from v1 pending JDK"
    assert providers["local_fixture"]["in_ablation"] is True
    for name in ("audd", "acrcloud"):
        assert providers[name]["in_ablation"] is False
        assert providers[name]["fusion_validated_on_fixtures"] is True
    assert any(item["feature"] == "hints on/off" for item in report["not_evaluable"])
    assert report["events"]["rescans_on"]["reset"]["n_cases"] == 0


def test_engine_status_rows_are_honest_about_missing_credentials() -> None:
    rows = {row["provider"]: row for row in engine_status_rows(ROOT)}
    assert set(rows) == {"local_fixture", "shazam", "audd", "acrcloud", "panako"}
    assert rows["shazam"]["in_ablation"] is False
    assert "JDK" in rows["panako"]["detail"] or "JDK" in rows["panako"]["status"]


def test_paired_p90_improvement_is_a_relative_cluster_bootstrap() -> None:
    baseline = {f"set-{index}": [10_000, 12_000] for index in range(10)}
    challenger = {f"set-{index}": [1_000, 6_000] for index in range(10)}
    result = paired_p90_improvement(baseline, challenger, seed=7)
    assert result["baseline_p90_ms"] == 12_000
    assert result["challenger_p90_ms"] == 6_000
    assert result["relative_improvement_e4"] == 5_000
    assert result["lower_bound_e4"] <= result["relative_improvement_e4"]
    assert result["n_sets"] == 10
    empty = paired_p90_improvement({"a": []}, {"a": []}, seed=7)
    assert empty["relative_improvement_e4"] is None


def test_rescans_lower_the_start_bound_on_a_committed_event_set() -> None:
    truth = GroundTruthRecord.model_validate_json(
        read_text(EVENT_CORPUS / "controlled-026-ev-loop-01" / "ground_truth.json")
    )
    manifest = json.loads(read_text(EVENT_CORPUS / "render_manifest.json"))
    offset = next(
        item["source_offset_ms"] for item in manifest["sets"] if item["set_id"] == truth.set_id
    )
    off = run_fixture_set(
        truth,
        options=ArmOptions(name="off", rescans=False),
        source_offset_ms=offset,
        novelty_points=(),
    )
    on = run_fixture_set(
        truth,
        options=ArmOptions(name="on"),
        source_offset_ms=offset,
        novelty_points=(11_000,),
    )
    assert off.generations == 1
    assert on.generations > 1
    assert min(item["best_start_ms"] for item in on.prediction["episodes"]) < min(
        item["best_start_ms"] for item in off.prediction["episodes"]
    )
    assert len(on.windows) > len(off.windows)
