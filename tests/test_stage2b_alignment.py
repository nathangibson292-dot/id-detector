from __future__ import annotations

import pytest

from id_detector.fuse.alignment import AlignmentPoint, align_candidate_points


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


def _non_continuations(points: list[AlignmentPoint]) -> list[str]:
    occurrences = align_candidate_points(points)
    return [
        decision.type
        for occurrence in occurrences
        for decision in occurrence.decisions
        if decision.type != "continuation"
    ]


def test_continuation_fires_without_any_other_event() -> None:
    occurrences = align_candidate_points(
        [_point(1, 0, 0), _point(2, 10_000, 10_000), _point(3, 20_000, 20_000)]
    )
    assert [event.type for event in occurrences[0].decisions] == [
        "continuation",
        "continuation",
    ]
    assert len(occurrences) == 1


@pytest.mark.parametrize(
    ("event", "points"),
    [
        (
            "loop",
            [
                _point(1, 0, 10_000),
                _point(2, 10_000, 20_000),
                _point(3, 20_000, 30_000),
                _point(4, 25_000, 15_000),
            ],
        ),
        (
            "reset",
            [
                _point(1, 0, 10_000),
                _point(2, 10_000, 20_000),
                _point(3, 20_000, 30_000),
                _point(4, 30_000, 0),
            ],
        ),
        (
            "jump",
            [
                _point(1, 0, 0),
                _point(2, 10_000, 10_000),
                _point(3, 20_000, 20_000),
                _point(4, 30_000, 50_000),
                _point(5, 40_000, 60_000),
                _point(6, 50_000, 70_000),
            ],
        ),
        (
            "drift",
            [
                _point(1, 0, 0),
                _point(2, 10_000, 10_000),
                _point(3, 20_000, 20_000),
                _point(4, 30_000, 35_000, 10_500),
                _point(5, 40_000, 45_500, 10_500),
                _point(6, 50_000, 56_000, 10_500),
            ],
        ),
        (
            "replay",
            [
                _point(1, 0, 0),
                _point(2, 10_000, 10_000),
                _point(3, 20_000, 20_000),
                _point(4, 60_000, 50_000),
            ],
        ),
        (
            "outlier",
            [
                _point(1, 0, 0),
                _point(2, 10_000, 10_000),
                _point(3, 20_000, 20_000),
                _point(4, 30_000, 100_000),
                _point(5, 40_000, 40_000),
            ],
        ),
    ],
)
def test_event_precision_each_intended_event_fires_and_no_other_does(
    event: str, points: list[AlignmentPoint]
) -> None:
    assert _non_continuations(points) == [event]


def test_rate_and_residual_gates_reject_implausible_refit() -> None:
    points = [
        _point(1, 0, 0),
        _point(2, 10_000, 10_000),
        _point(3, 20_000, 20_000),
        _point(4, 30_000, 45_000, 15_000),
        _point(5, 40_000, 60_000, 15_000),
        _point(6, 50_000, 75_000, 15_000),
    ]
    occurrences = align_candidate_points(points)
    assert len(occurrences) == 1
    assert "outlier" not in _non_continuations(points)
    assert all(len(segment.points) < 3 for segment in occurrences[0].segments[1:])


def test_two_point_shift_is_a_jump_not_two_outliers() -> None:
    points = [
        _point(1, 0, 0),
        _point(2, 10_000, 10_000),
        _point(3, 20_000, 20_000),
        _point(4, 30_000, 50_000),
        _point(5, 40_000, 60_000),
    ]
    assert _non_continuations(points) == ["jump"]


@pytest.mark.parametrize(
    ("gap_ms", "ref_ms", "expected_occurrences"),
    [
        (30_000, 80_000, 1),
        (30_001, 80_001, 2),
        (120_000, 140_000, 1),
        (120_001, 140_001, 1),
    ],
)
def test_replay_and_continuation_gap_boundaries(
    gap_ms: int, ref_ms: int, expected_occurrences: int
) -> None:
    points = [
        _point(1, 0, 0),
        _point(2, 10_000, 10_000),
        _point(3, 20_000, 20_000),
        _point(4, 20_000 + gap_ms, ref_ms),
    ]
    assert len(align_candidate_points(points)) == expected_occurrences


def test_real_dev1_intro_replay_is_granularity_not_fragmentation() -> None:
    """Anonymised (mix, ref) anchor pairs from the real dev-1 candidate ``dcd68806bb24``.

    The intro region (ref near 0) is played, then recurs near 0 again after a 36 s mix gap. That is
    a genuine *replay* (a second occurrence), not a continuous forward run chopped over a small gap:
    the resolution of the 39-vs-55 question is that every dev-1 multi-occurrence candidate is such a
    reference recurrence, never fragmentation. The first four forward points on their own stay a
    single occurrence, proving the intro itself is not fragmented.
    """

    anchors = [
        (396_000, -577),
        (405_000, 433),
        (414_000, 1_440),
        (423_000, 2_447),
        (459_000, -1_551),
        (468_000, -539),
        (477_000, 474),
        (486_000, 1_472),
    ]
    points = [_point(index, mix, ref) for index, (mix, ref) in enumerate(anchors, 1)]
    occurrences = align_candidate_points(points)
    assert len(occurrences) == 2
    assert "replay" in _non_continuations(points)
    # The continuous intro on its own is one occurrence: no fragmentation over the in-region gaps.
    assert len(align_candidate_points(points[:4])) == 1


def test_reference_consistent_forward_run_across_a_long_drought_stays_one_occurrence() -> None:
    """A forward-advancing, reference-consistent match after a 36 s recognition drought continues.

    This is the anti-fragmentation guarantee behind the 39-vs-55 resolution: continuation-by-
    reference-consistency fires for any gap <= 120 s where the reference keeps advancing, so a
    genuinely continuous track is never split just because recognition went quiet for > 30 s.
    """

    points = [
        _point(1, 0, 0),
        _point(2, 9_000, 9_000),
        _point(3, 18_000, 18_000),
        _point(4, 54_000, 54_000),  # 36 s drought, reference still on the same line
        _point(5, 63_000, 63_000),
        _point(6, 72_000, 72_000),
    ]
    occurrences = align_candidate_points(points)
    assert len(occurrences) == 1
    assert "replay" not in _non_continuations(points)


def test_anonymised_real_anchor_excerpt_is_one_continuous_occurrence() -> None:
    anchors = [
        (225_000, 4_694),
        (234_000, 14_095),
        (243_000, 23_493),
        (252_000, 32_888),
        (261_000, 42_285),
        (270_000, 51_679),
        (279_000, 61_075),
        (288_000, 70_476),
    ]
    points = [_point(index, mix, ref, 10_442) for index, (mix, ref) in enumerate(anchors, 1)]
    occurrences = align_candidate_points(points)
    assert len(occurrences) == 1
    assert len(occurrences[0].points) == len(anchors)
    assert occurrences[0].has_global_alignment
    assert "replay" not in _non_continuations(points)
