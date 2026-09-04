"""Executable revision-5 alignment baseline and event state machine."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from fractions import Fraction
from statistics import median
from typing import Literal

from id_detector.contracts import AlignmentEvent, AlignmentSegment, ObservationRecord

RESIDUAL_GATE_MS = 1_500
MIN_RATE_E4 = 8_400
MAX_RATE_E4 = 12_000
HYPOTHESIS_AGREEMENT_E4 = 200
DRIFT_DELTA_E4 = 300
CONTINUATION_GAP_MS = 120_000
REPLAY_GAP_MS = 30_000

DecisionType = Literal["continuation", "loop", "reset", "jump", "drift", "replay", "outlier"]


@dataclass(frozen=True)
class AlignmentPoint:
    observation_id: str
    logical_trial_id: str
    candidate_id: str
    mix_anchor_ms: int
    ref_anchor_ms: int
    support_ms: tuple[int, int]
    rate_e4: int
    skew_cost_e6: int


@dataclass(frozen=True)
class AlignmentDecision:
    at_ms: int
    type: DecisionType
    observation_id: str


@dataclass(frozen=True)
class FittedSegment:
    points: tuple[AlignmentPoint, ...]
    rate_e4: int
    intercept_ms: int
    residual_ms: int

    def contract(self) -> AlignmentSegment:
        return AlignmentSegment(
            mix_from_ms=min(point.support_ms[0] for point in self.points),
            mix_to_ms=max(point.support_ms[1] for point in self.points),
            rate_e4=self.rate_e4,
            intercept_ms=self.intercept_ms,
            residual_ms=self.residual_ms,
            n_obs=len(self.points),
        )


@dataclass(frozen=True)
class AlignmentOccurrence:
    points: tuple[AlignmentPoint, ...]
    segments: tuple[FittedSegment, ...]
    decisions: tuple[AlignmentDecision, ...]
    rejected_observation_ids: tuple[str, ...]
    has_global_alignment: bool

    @property
    def episode_events(self) -> list[AlignmentEvent]:
        return [
            AlignmentEvent(at_ms=item.at_ms, type=item.type)
            for item in self.decisions
            if item.type in {"jump", "loop", "reset", "drift"}
        ]


@dataclass(frozen=True)
class TrialSelection:
    points: tuple[AlignmentPoint, ...]
    selected_observation_ids: tuple[str, ...]
    hypothesis_rejected: tuple[str, ...]


def _native_skew_cost(observation: ObservationRecord) -> int:
    matches = observation.native.get("matches", [])
    costs: list[int] = []
    if isinstance(matches, list):
        for item in matches:
            if isinstance(item, dict):
                costs.append(
                    abs(int(item.get("frequencyskew_e6", 0))) + abs(int(item.get("timeskew_e6", 0)))
                )
    return min(costs, default=0)


def _native_rate_e4(observation: ObservationRecord) -> int:
    """Combine the transform hypothesis with Shazam's measured residual time skew."""

    base_rate = (
        observation.transform.rate_e4
        if observation.transform is not None and observation.transform.type in {"resample", "tempo"}
        else 10_000
    )
    matches = observation.native.get("matches", [])
    skews = [
        int(item.get("timeskew_e6", 0))
        for item in matches
        if isinstance(item, dict) and item.get("timeskew_e6") is not None
    ]
    residual_e6 = round(median(skews)) if skews else 0
    return _round_fraction(Fraction(base_rate * (1_000_000 + residual_e6), 1_000_000))


def point_from_observation(
    observation: ObservationRecord, candidate_id: str
) -> AlignmentPoint | None:
    if (
        not observation.is_final
        or observation.status != "match"
        or observation.anchor is None
        or not observation.anchor.reliable
    ):
        return None
    return AlignmentPoint(
        observation_id=observation.id,
        logical_trial_id=observation.logical_trial_id,
        candidate_id=candidate_id,
        mix_anchor_ms=observation.anchor.mix_anchor_ms,
        ref_anchor_ms=observation.anchor.ref_anchor_ms,
        support_ms=observation.support_ms,
        rate_e4=_native_rate_e4(observation),
        skew_cost_e6=_native_skew_cost(observation),
    )


def select_logical_trial_points(
    observations: list[ObservationRecord] | tuple[ObservationRecord, ...],
    observation_candidates: dict[str, str],
) -> TrialSelection:
    """Keep exactly one best majority-candidate hypothesis per logical trial/source."""

    grouped: dict[tuple[str, str], list[tuple[ObservationRecord, str]]] = defaultdict(list)
    for observation in observations:
        candidate = observation_candidates.get(observation.id)
        if candidate is None:
            continue
        source = observation.native.get("simultaneous_source")
        source_key = str(source) if source is not None else "primary"
        grouped[(observation.logical_trial_id, source_key)].append((observation, candidate))

    selected: list[AlignmentPoint] = []
    selected_observation_ids: list[str] = []
    rejected: list[str] = []
    for key in sorted(grouped):
        variants = grouped[key]
        counts = Counter(candidate for _, candidate in variants)
        majority = min(counts, key=lambda item: (-counts[item], item))
        eligible = [item for item in variants if item[1] == majority]
        chosen_observation, _ = min(
            eligible, key=lambda item: (_native_skew_cost(item[0]), item[0].id)
        )
        selected_observation_ids.append(chosen_observation.id)
        rejected.extend(
            observation.id for observation, _ in variants if observation != chosen_observation
        )
        point = point_from_observation(chosen_observation, majority)
        if point is not None:
            selected.append(point)
    return TrialSelection(
        points=tuple(sorted(selected, key=lambda item: (item.mix_anchor_ms, item.observation_id))),
        selected_observation_ids=tuple(sorted(selected_observation_ids)),
        hypothesis_rejected=tuple(sorted(rejected)),
    )


def _round_fraction(value: Fraction) -> int:
    sign = -1 if value < 0 else 1
    value = abs(value)
    quotient, remainder = divmod(value.numerator, value.denominator)
    return sign * (quotient + int(remainder * 2 >= value.denominator))


def _median_fraction(values: list[Fraction]) -> Fraction:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _fit(points: list[AlignmentPoint]) -> FittedSegment:
    hypothesis_rate = round(median(point.rate_e4 for point in points))
    if len(points) >= 3:
        slopes = [
            Fraction(
                (right.ref_anchor_ms - left.ref_anchor_ms) * 10_000,
                right.mix_anchor_ms - left.mix_anchor_ms,
            )
            for index, left in enumerate(points)
            for right in points[index + 1 :]
            if right.mix_anchor_ms != left.mix_anchor_ms
        ]
        rate_e4 = _round_fraction(_median_fraction(slopes)) if slopes else hypothesis_rate
    else:
        rate_e4 = points[0].rate_e4
    intercepts = [
        Fraction(point.ref_anchor_ms * 10_000 - rate_e4 * point.mix_anchor_ms, 10_000)
        for point in points
    ]
    intercept_ms = _round_fraction(_median_fraction(intercepts))
    residual = max(
        abs(
            point.ref_anchor_ms
            - _round_fraction(Fraction(rate_e4 * point.mix_anchor_ms, 10_000))
            - intercept_ms
        )
        for point in points
    )
    return FittedSegment(tuple(points), rate_e4, intercept_ms, residual)


def _fit_is_valid(segment: FittedSegment) -> bool:
    hypothesis_rate = round(median(point.rate_e4 for point in segment.points))
    return (
        MIN_RATE_E4 <= segment.rate_e4 <= MAX_RATE_E4
        and abs(segment.rate_e4 - hypothesis_rate) <= HYPOTHESIS_AGREEMENT_E4
        and segment.residual_ms <= RESIDUAL_GATE_MS
    )


def _predicted_ref(segment: FittedSegment, mix_ms: int) -> int:
    return _round_fraction(Fraction(segment.rate_e4 * mix_ms, 10_000)) + segment.intercept_ms


def _consistent(segment: FittedSegment, point: AlignmentPoint) -> bool:
    if abs(point.ref_anchor_ms - _predicted_ref(segment, point.mix_anchor_ms)) > RESIDUAL_GATE_MS:
        return False
    proposed = _fit([*segment.points, point])
    return len(proposed.points) < 3 or _fit_is_valid(proposed)


def _lookahead_fit(
    points: list[AlignmentPoint], index: int, count: int = 3
) -> FittedSegment | None:
    subset = points[index : index + count]
    if len(subset) < count:
        return None
    fitted = _fit(subset)
    return fitted if _fit_is_valid(fitted) else None


def _segment_coverage(
    points: tuple[AlignmentPoint, ...], all_points: tuple[AlignmentPoint, ...]
) -> int:
    def union_length(source: tuple[AlignmentPoint, ...]) -> int:
        intervals = sorted(point.support_ms for point in source)
        merged: list[list[int]] = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return sum(end - start for start, end in merged)

    total = union_length(all_points)
    return 10_000 if total == 0 else union_length(points) * 10_000 // total


def align_candidate_points(
    points: list[AlignmentPoint] | tuple[AlignmentPoint, ...],
) -> tuple[AlignmentOccurrence, ...]:
    """Fit piecewise alignments using the plan's exact decision precedence."""

    ordered = sorted(points, key=lambda item: (item.mix_anchor_ms, item.observation_id))
    if not ordered:
        return ()

    occurrences: list[AlignmentOccurrence] = []
    occurrence_points: list[AlignmentPoint] = [ordered[0]]
    segment_points: list[AlignmentPoint] = [ordered[0]]
    segments: list[FittedSegment] = []
    decisions: list[AlignmentDecision] = []
    rejected: list[str] = []

    def finish_occurrence() -> None:
        nonlocal occurrence_points, segment_points, segments, decisions, rejected
        if segment_points:
            segments.append(_fit(segment_points))
        point_tuple = tuple(occurrence_points)
        segment_tuple = tuple(segments)
        global_alignment = any(
            _segment_coverage(segment.points, point_tuple) >= 8_000 for segment in segment_tuple
        )
        occurrences.append(
            AlignmentOccurrence(
                points=point_tuple,
                segments=segment_tuple,
                decisions=tuple(decisions),
                rejected_observation_ids=tuple(sorted(rejected)),
                has_global_alignment=global_alignment,
            )
        )
        occurrence_points, segment_points, segments, decisions, rejected = [], [], [], [], []

    for index in range(1, len(ordered)):
        point = ordered[index]
        active = _fit(segment_points)
        previous = ordered[index - 1]
        gap = point.mix_anchor_ms - previous.mix_anchor_ms

        ref_values = [item.ref_anchor_ms for item in segment_points]
        ref_min, ref_max = min(ref_values), max(ref_values)
        same_ref_region = (
            ref_min - RESIDUAL_GATE_MS <= point.ref_anchor_ms <= ref_max + RESIDUAL_GATE_MS
        )
        reference_consistent = _consistent(active, point)

        # 1. Reference-consistent evidence continues across ordinary recognition droughts.
        if (
            gap <= CONTINUATION_GAP_MS
            and reference_consistent
            and not (gap > REPLAY_GAP_MS and same_ref_region)
        ):
            segment_points.append(point)
            occurrence_points.append(point)
            decisions.append(
                AlignmentDecision(point.mix_anchor_ms, "continuation", point.observation_id)
            )
            continue

        event: DecisionType | None = None

        # 2. A short-gap backwards recurrence is a loop. A >30 s recurrence is reserved for replay.
        if gap <= REPLAY_GAP_MS and same_ref_region and point.ref_anchor_ms < ref_max - 2_000:
            event = "loop"
        # 3. A return to the start of the held reference is a reset.
        elif abs(point.ref_anchor_ms) <= 5_000 and ref_max > 7_000:
            event = "reset"
        else:
            future_pair = _lookahead_fit(ordered, index, count=2)
            future_fit = _lookahead_fit(ordered, index)
            # 4. A stable old rate with a shifted intercept is a jump.
            if (
                not reference_consistent
                and future_pair is not None
                and abs(future_pair.rate_e4 - active.rate_e4) <= HYPOTHESIS_AGREEMENT_E4
                and abs(point.rate_e4 - active.rate_e4) <= HYPOTHESIS_AGREEMENT_E4
            ):
                event = "jump"
            # 5. A stable new slope over at least three points is drift.
            elif (
                future_fit is not None and abs(future_fit.rate_e4 - active.rate_e4) > DRIFT_DELTA_E4
            ):
                event = "drift"
            # 6. Long-gap reference inconsistency or recurrence starts another occurrence.
            elif gap > REPLAY_GAP_MS and (not reference_consistent or same_ref_region):
                event = "replay"
            # 7. Only an isolated point inconsistent with both adjacent trends is an outlier.
            elif index + 1 < len(ordered) and _consistent(active, ordered[index + 1]):
                event = "outlier"

        if event is None:
            # A reference-consistent point beyond the continuation horizon, or the beginning of
            # an as-yet unclassified transition, remains pending in this occurrence.  It starts a
            # fresh segment and is not mislabeled as either replay or outlier.
            segments.append(active)
            segment_points = [point]
            occurrence_points.append(point)
            continue

        decisions.append(AlignmentDecision(point.mix_anchor_ms, event, point.observation_id))
        if event == "outlier":
            rejected.append(point.observation_id)
            continue
        if event == "replay":
            decisions.pop()
            finish_occurrence()
            occurrence_points = [point]
            segment_points = [point]
            decisions = [AlignmentDecision(point.mix_anchor_ms, "replay", point.observation_id)]
            continue
        segments.append(active)
        segment_points = [point]
        occurrence_points.append(point)

    finish_occurrence()
    return tuple(occurrences)


def align_selected_points(selection: TrialSelection) -> dict[str, tuple[AlignmentOccurrence, ...]]:
    by_candidate: dict[str, list[AlignmentPoint]] = defaultdict(list)
    for point in selection.points:
        by_candidate[point.candidate_id].append(point)
    return {
        candidate: align_candidate_points(candidate_points)
        for candidate, candidate_points in sorted(by_candidate.items())
    }
