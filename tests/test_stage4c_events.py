from __future__ import annotations

import json
from pathlib import Path

import pytest

from id_detector.benchmark.ablations import (
    GATE_EVENT_E4,
    GATE_MIN_BOUNDARIES,
    GATE_MIN_EVENT_CASES,
)
from id_detector.benchmark.scorer import (
    EVENT_DETECTION_HORIZON_MS,
    EVENT_TOLERANCE_MS,
    EVENT_TYPES,
    ScoredEpisode,
    ScoreState,
    _score_events,
    load_truth_directory,
    predicted_events,
)
from id_detector.contracts import GroundTruthRecord
from id_detector.fuse.alignment import AlignmentPoint, align_candidate_points
from id_detector.io import read_text

ROOT = Path(__file__).resolve().parents[1]
EVENT_CORPUS = ROOT / "data" / "corpus" / "controlled-events-1"


def _point(index: int, mix: int, ref: int, rate_e4: int = 10_000) -> AlignmentPoint:
    return AlignmentPoint(
        observation_id=f"{index:040x}",
        logical_trial_id=f"{100 + index:040x}",
        candidate_id="a" * 40,
        mix_anchor_ms=mix,
        ref_anchor_ms=ref,
        support_ms=(mix, mix + 4_000),
        rate_e4=rate_e4,
        skew_cost_e6=0,
    )


def _events(points: list[AlignmentPoint]) -> list[str]:
    return [
        decision.type
        for occurrence in align_candidate_points(points)
        for decision in occurrence.decisions
        if decision.type != "continuation"
    ]


def test_a_multi_point_recurrence_after_thirty_seconds_is_a_replay_not_a_jump() -> None:
    """Before Stage 4c ``jump`` consumed every replay that had points after it."""

    points = [
        _point(1, 0, 40_000),
        _point(2, 10_000, 50_000),
        _point(3, 20_000, 60_000),
        _point(4, 60_000, 40_000),
        _point(5, 70_000, 50_000),
        _point(6, 80_000, 60_000),
    ]
    assert _events(points) == ["replay"]
    occurrences = align_candidate_points(points)
    assert len(occurrences) == 2
    assert [event.type for event in occurrences[1].episode_events] == ["replay"]
    assert occurrences[1].episode_events[0].at_ms == 60_000


def test_a_short_gap_intercept_shift_is_still_a_jump() -> None:
    points = [
        _point(1, 0, 0),
        _point(2, 10_000, 10_000),
        _point(3, 20_000, 20_000),
        _point(4, 30_000, 50_000),
        _point(5, 40_000, 60_000),
        _point(6, 50_000, 70_000),
    ]
    assert _events(points) == ["jump"]


def test_replay_is_dated_in_the_episode_contract() -> None:
    points = [
        _point(1, 0, 40_000),
        _point(2, 10_000, 50_000),
        _point(3, 60_000, 40_000),
        _point(4, 70_000, 50_000),
    ]
    occurrences = align_candidate_points(points)
    assert len(occurrences) == 2
    payload = [event.model_dump(mode="json") for event in occurrences[1].episode_events]
    assert payload == [{"at_ms": 60_000, "type": "replay"}]


def _episode(**overrides: object) -> ScoredEpisode:
    payload: dict[str, object] = {
        "work": {"artist": "A", "title": "B"},
        "version": {"qualifier": None, "ids": {}},
        "candidate_id": "f" * 40,
        "evidence_support_ms": [(10_000, 22_000)],
        "start_no_later_than_ms": 22_000,
        "end_no_earlier_than_ms": 10_000,
        "start_pi": None,
        "end_pi": None,
        "best_start_ms": 22_000,
        "best_end_ms": 10_000,
        "role_segments": [],
        "occurrence_index": 0,
        "claim": "performed",
        "scores": {"work": 6_000, "version": 2_500, "boundary": 6_000},
        "tiers": {"work": "possible", "version": "unclear", "boundary": "possible"},
        "alignment_events": [],
    }
    payload.update(overrides)
    return ScoredEpisode.model_validate(payload)


def test_predicted_replay_prefers_the_dated_event_and_falls_back_to_the_occurrence() -> None:
    dated = _episode(occurrence_index=1, alignment_events=[{"at_ms": 15_000, "type": "replay"}])
    assert predicted_events([dated])["replay"] == [15_000]
    undated = _episode(occurrence_index=1)
    assert predicted_events([undated])["replay"] == [10_000]
    assert predicted_events([_episode()])["replay"] == []


def _truth_with_events(events: list[dict[str, object]]) -> GroundTruthRecord:
    return GroundTruthRecord.model_validate(
        {
            "schema_version": "1.0.0",
            "generated_by": "test-vector",
            "set_id": "event-vector",
            "source": {
                "url_ref": "source-vector",
                "media_key": "a" * 64,
                "duration_ms": 120_000,
                "platform": "file",
                "uploader_ref": "uploader-vector",
                "event_ref": None,
                "date": None,
            },
            "stratum": "controlled",
            "split": "controlled",
            "corpus_version": "vector",
            "selection_basis": "authored before implementation",
            "episodes": [],
            "events": events,
            "regions": [],
        }
    )


def test_event_matching_admits_the_detection_lag_but_not_an_early_prediction() -> None:
    truth = _truth_with_events(
        [{"type": "drift", "at_ms": 40_000, "episode_index": None, "note": None}]
    )
    late = _episode(
        alignment_events=[{"at_ms": 40_000 + EVENT_DETECTION_HORIZON_MS, "type": "drift"}]
    )
    too_late = _episode(
        alignment_events=[{"at_ms": 40_000 + EVENT_DETECTION_HORIZON_MS + 1, "type": "drift"}]
    )
    early = _episode(alignment_events=[{"at_ms": 40_000 - EVENT_TOLERANCE_MS - 1, "type": "drift"}])

    for episodes, matched in ((late, 1), (too_late, 0), (early, 0)):
        state = ScoreState()
        _score_events(state, truth, [episodes])
        assert state.event_counts["drift"].correct == matched
        assert state.event_counts["drift"].truth == 1
        assert state.event_counts["drift"].predicted == 1

    strict = ScoreState()
    _score_events(strict, truth, [_episode(alignment_events=[{"at_ms": 41_000, "type": "drift"}])])
    assert strict.event_counts_strict["drift"].correct == 1


def test_events_are_read_from_the_contract_not_from_the_note_field() -> None:
    truth = _truth_with_events([])
    noted = truth.model_copy(update={"selection_basis": "event:loop@40000"})
    state = ScoreState()
    _score_events(state, noted, [_episode(alignment_events=[{"at_ms": 40_000, "type": "loop"}])])
    assert state.event_counts["loop"].truth == 0
    assert state.event_counts["loop"].predicted == 1
    assert state.event_counts["loop"].correct == 0


@pytest.mark.parametrize("event_type", ["loop", "jump", "drift", "replay"])
def test_committed_event_corpus_carries_at_least_thirty_cases_per_type(event_type: str) -> None:
    truths = load_truth_directory(EVENT_CORPUS)
    counts = {name: 0 for name in EVENT_TYPES}
    boundaries = 0
    for truth in truths:
        boundaries += 2 * len(truth.episodes)
        for event in truth.events:
            counts[event.type] += 1
    assert boundaries >= GATE_MIN_BOUNDARIES
    assert counts[event_type] >= GATE_MIN_EVENT_CASES


def test_committed_event_corpus_is_frozen_and_pseudonymous() -> None:
    manifest = json.loads(read_text(EVENT_CORPUS / "corpus-version.json"))
    assert manifest["frozen"] is True
    assert manifest["corpus_version"] == "controlled-events-1"
    assert len(manifest["sets"]) == 145
    truths = load_truth_directory(EVENT_CORPUS)
    assert {truth.split for truth in truths} == {"controlled"}
    assert all(truth.source.url_ref.startswith("controlled-source-") for truth in truths)
    assert all(not episode.draft for truth in truths for episode in truth.episodes)
    replay_sets = [truth for truth in truths if any(e.type == "replay" for e in truth.events)]
    assert replay_sets
    for truth in replay_sets:
        assert [episode.occurrence_index for episode in truth.episodes] == [0, 1]
        assert truth.episodes[0].work == truth.episodes[1].work
        gap = truth.episodes[1].start_ms_range[0] - truth.episodes[0].end_ms_range[1]
        assert gap > 30_000, "a replay case must exceed the plan's 30 s replay gap"


def test_gate_thresholds_match_the_plan() -> None:
    assert GATE_EVENT_E4 == 8_000
    assert GATE_MIN_EVENT_CASES == 30
    assert GATE_MIN_BOUNDARIES == 100
