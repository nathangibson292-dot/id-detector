"""Overlap-aware, occurrence-aware benchmark scorer.

The scorer deliberately associates identity from supporting observations, not from an episode
boundary IoU.  All persisted values use integer fixed-point representations.
"""

from __future__ import annotations

import json
import math
import random
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from id_detector.contracts import (
    AlignmentEvent,
    BenchmarkCost,
    BenchmarkEngine,
    BenchmarkMetrics,
    BenchmarkReportRecord,
    ContractModel,
    EpisodeScores,
    EpisodeTiers,
    GroundTruthRecord,
    IdentitiesRecord,
    PredictionInterval,
    RoleSegment,
    SpanMs,
    TruthVersion,
    TruthWork,
)
from id_detector.io import atomic_write_json, canonical_json_bytes, read_text, sha256_file

ASSOCIATION_MARGIN_MS = 30_000
BOUNDARY_TOLERANCE_MS = 10_000
EVENT_TOLERANCE_MS = 2_000
BOOTSTRAP_REPLICATES = 2_000
TIER_ORDER = {"unclear": 0, "possible": 1, "likely": 2, "verified": 3}
_RECORDING_NAMESPACES = frozenset(
    {
        "isrc",
        "mb_recording",
        "shazam",
        "deezer",
        "apple",
        "spotify",
        "acr",
        "audd",
        "beatport",
        "soundcloud",
    }
)
_DIMENSIONS = ("work", "version", "start", "end", "boundary")
_CERTIFICATION_TIERS = ("possible", "likely", "verified")


class CertificationTarget(ContractModel):
    profile: str
    dimension: Literal["work", "version", "start", "end", "boundary"]
    tier: Literal["possible", "likely", "verified"]
    target_e4: int = Field(ge=0, le=10_000)


class ScoringConfigSnapshot(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    config_version: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    bootstrap_seed: int = Field(ge=0)
    certification_targets: list[CertificationTarget]
    run_config: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _targets_are_unique(self) -> ScoringConfigSnapshot:
        keys = {(item.profile, item.dimension, item.tier) for item in self.certification_targets}
        if len(keys) != len(self.certification_targets):
            raise ValueError("certification targets must be unique by profile, dimension, and tier")
        return self


class ScoredEpisode(ContractModel):
    """Identity-labelled projection of an episode artefact used by the benchmark."""

    work: TruthWork
    version: TruthVersion
    candidate_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    evidence_support_ms: list[SpanMs]
    start_no_later_than_ms: int = Field(ge=0)
    end_no_earlier_than_ms: int = Field(ge=0)
    start_pi: PredictionInterval | None
    end_pi: PredictionInterval | None
    best_start_ms: int = Field(ge=0)
    best_end_ms: int = Field(ge=0)
    role_segments: list[RoleSegment]
    occurrence_index: int = Field(ge=0)
    claim: Literal["performed", "component_evidence"]
    scores: EpisodeScores
    tiers: EpisodeTiers
    alignment_events: list[AlignmentEvent]

    @model_validator(mode="after")
    def _ordered(self) -> ScoredEpisode:
        if not self.evidence_support_ms:
            raise ValueError("evidence_support_ms must contain a supporting observation")
        for start, end in self.evidence_support_ms:
            if end <= start:
                raise ValueError("evidence support spans must have positive duration")
        # The artefact stores a union of supports, while the proved bounds use every raw final
        # match. Nested raw supports can therefore put a proof point inside a union interval.
        if not any(
            start < self.start_no_later_than_ms <= end for start, end in self.evidence_support_ms
        ):
            raise ValueError("start_no_later_than_ms must equal min evidence support end")
        if not any(
            start <= self.end_no_earlier_than_ms < end for start, end in self.evidence_support_ms
        ):
            raise ValueError("end_no_earlier_than_ms must equal max evidence support start")
        # One-sided proofs can cross when all positive windows overlap.  They are bounds on
        # different latent events, not the endpoints of a conventional closed interval.
        expected_start = (
            (self.start_pi.lo + self.start_pi.hi) // 2
            if self.start_pi is not None and self.start_pi.calibrated
            else self.start_no_later_than_ms
        )
        expected_end = (
            (self.end_pi.lo + self.end_pi.hi) // 2
            if self.end_pi is not None and self.end_pi.calibrated
            else self.end_no_earlier_than_ms
        )
        if self.best_start_ms != expected_start:
            raise ValueError("best_start_ms must equal the calibrated PI centre or proved bound")
        if self.best_end_ms != expected_end:
            raise ValueError("best_end_ms must equal the calibrated PI centre or proved bound")
        return self


class PredictionSet(ContractModel):
    set_id: str
    identities: IdentitiesRecord
    episodes: list[ScoredEpisode]

    @model_validator(mode="after")
    def _identity_references_resolve(self) -> PredictionSet:
        candidates = {item.canonical_id: item for item in self.identities.candidates}
        if len(candidates) != len(self.identities.candidates):
            raise ValueError("identity graph contains duplicate canonical_id values")
        for episode in self.episodes:
            candidate = candidates.get(episode.candidate_id)
            if candidate is None:
                raise ValueError(f"unknown episode candidate_id: {episode.candidate_id}")
            if candidate.contested and episode.tiers.version != "unclear":
                raise ValueError("a contested candidate must have an unclear version tier")
        conflict_pairs = [
            {assertion.a, assertion.b}
            for assertion in self.identities.assertions
            if assertion.relation == "conflicts"
        ]
        for candidate in candidates.values():
            members = set(candidate.member_nodes)
            if any(pair <= members for pair in conflict_pairs) and not candidate.contested:
                raise ValueError("a recording component containing a conflict must be contested")
        return self


class PredictionDocument(ContractModel):
    corpus_version: str
    profile: str
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_snapshot: ScoringConfigSnapshot
    sets: list[PredictionSet]
    engines: list[BenchmarkEngine] = Field(default_factory=list)
    cost: BenchmarkCost = Field(
        default_factory=lambda: BenchmarkCost(
            requests=0,
            physical_attempts=0,
            billable_seconds=0,
            usd_e2=0,
            wall_ms=0,
        )
    )
    unverified_seed_comparison: bool

    @model_validator(mode="after")
    def _config_hash_matches_snapshot(self) -> PredictionDocument:
        actual = sha256(canonical_json_bytes(self.config_snapshot)).hexdigest()
        if self.config_hash != actual:
            raise ValueError("config_hash does not match config_snapshot")
        if self.profile != self.config_snapshot.profile:
            raise ValueError("prediction profile does not match config_snapshot")
        return self


@dataclass
class RatioCount:
    correct: int = 0
    predicted: int = 0
    truth: int = 0

    def add(self, other: RatioCount) -> None:
        self.correct += other.correct
        self.predicted += other.predicted
        self.truth += other.truth


@dataclass
class SegmentCount:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, other: SegmentCount) -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn


@dataclass
class ScoreState:
    identification_work: RatioCount = field(default_factory=RatioCount)
    identification_version: RatioCount = field(default_factory=RatioCount)
    occurrence: RatioCount = field(default_factory=RatioCount)
    segment: SegmentCount = field(default_factory=SegmentCount)
    dominant: SegmentCount = field(default_factory=SegmentCount)
    secondary: SegmentCount = field(default_factory=SegmentCount)
    unknown: SegmentCount = field(default_factory=SegmentCount)
    macro_segment_precision_e4: list[int] = field(default_factory=list)
    macro_segment_recall_e4: list[int] = field(default_factory=list)
    work_calibration_errors: list[int] = field(default_factory=list)
    start_errors: list[int] = field(default_factory=list)
    end_errors: list[int] = field(default_factory=list)
    start_bound_violations: int = 0
    start_bound_n: int = 0
    end_bound_violations: int = 0
    end_bound_n: int = 0
    start_pi_covered: list[int] = field(default_factory=list)
    start_pi_widths: list[int] = field(default_factory=list)
    start_pi_winkler: list[int] = field(default_factory=list)
    end_pi_covered: list[int] = field(default_factory=list)
    end_pi_widths: list[int] = field(default_factory=list)
    end_pi_winkler: list[int] = field(default_factory=list)
    iou_e4: list[int] = field(default_factory=list)
    repeat_matched: int = 0
    repeat_truth: int = 0
    overlap_matched: int = 0
    overlap_truth: int = 0
    event_counts: dict[str, RatioCount] = field(
        default_factory=lambda: {name: RatioCount() for name in ("jump", "loop", "reset", "drift")}
    )
    confusion: dict[str, int] = field(
        default_factory=lambda: {
            "performed_as_performed": 0,
            "performed_as_component": 0,
            "component_as_performed": 0,
            "component_as_component": 0,
        }
    )
    tier_work: dict[str, tuple[int, int]] = field(default_factory=dict)
    certification: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)

    def add(self, other: ScoreState) -> None:
        self.identification_work.add(other.identification_work)
        self.identification_version.add(other.identification_version)
        self.occurrence.add(other.occurrence)
        self.segment.add(other.segment)
        self.dominant.add(other.dominant)
        self.secondary.add(other.secondary)
        self.unknown.add(other.unknown)
        for name in (
            "macro_segment_precision_e4",
            "macro_segment_recall_e4",
            "work_calibration_errors",
            "start_errors",
            "end_errors",
            "start_pi_covered",
            "start_pi_widths",
            "start_pi_winkler",
            "end_pi_covered",
            "end_pi_widths",
            "end_pi_winkler",
            "iou_e4",
        ):
            getattr(self, name).extend(getattr(other, name))
        for name in (
            "start_bound_violations",
            "start_bound_n",
            "end_bound_violations",
            "end_bound_n",
            "repeat_matched",
            "repeat_truth",
            "overlap_matched",
            "overlap_truth",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for event, count in other.event_counts.items():
            self.event_counts[event].add(count)
        for key, value in other.confusion.items():
            self.confusion[key] += value
        for target in ("tier_work", "certification"):
            own = getattr(self, target)
            for key, (correct, total) in getattr(other, target).items():
                old_correct, old_total = own.get(key, (0, 0))
                own[key] = (old_correct + correct, old_total + total)


@dataclass(frozen=True)
class SetScore:
    truth: GroundTruthRecord
    state: ScoreState
    metrics: BenchmarkMetrics


def _normalise_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def work_key(work: TruthWork) -> str:
    return f"{_normalise_text(work.artist)}|{_normalise_text(work.title)}"


def work_equivalent(left: TruthWork, right: TruthWork) -> bool:
    return work_key(left) == work_key(right)


def _normalise_identifier(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _recording_ids(version: TruthVersion) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for namespace, value in version.ids.items():
        normalised_namespace = _normalise_identifier(namespace)
        if normalised_namespace in _RECORDING_NAMESPACES:
            result[normalised_namespace].add(_normalise_identifier(value))
    return result


def _version_key(work: TruthWork, version: TruthVersion) -> tuple[Any, ...] | None:
    ids = tuple(
        sorted(
            (namespace, value)
            for namespace, values in _recording_ids(version).items()
            for value in values
        )
    )
    return (work_key(work), ids) if ids else None


def exact_equivalent(
    predicted_work: TruthWork,
    predicted_version: TruthVersion,
    truth_work: TruthWork,
    truth_version: TruthVersion,
) -> bool:
    if not work_equivalent(predicted_work, truth_work):
        return False
    predicted_ids = _recording_ids(predicted_version)
    truth_ids = _recording_ids(truth_version)
    common_namespaces = set(predicted_ids) & set(truth_ids)
    if any(predicted_ids[name].isdisjoint(truth_ids[name]) for name in common_namespaces):
        return False
    return any(predicted_ids[name] & truth_ids[name] for name in common_namespaces)


class _IdentityResolver:
    def __init__(self, identities: IdentitiesRecord) -> None:
        self.candidates = {item.canonical_id: item for item in identities.candidates}
        self.works = {item.work_id: item for item in identities.works}
        self.node_keys: dict[str, tuple[str, str]] = {}
        for node in identities.nodes:
            prefix, separator, value = node.id.partition(":")
            if not separator or prefix.casefold() != node.ns:
                raise ValueError(f"identity node id must use its namespace prefix: {node.id}")
            self.node_keys[node.id] = (node.ns, _normalise_identifier(value))
        if len(self.node_keys) != len(identities.nodes):
            raise ValueError("identity graph contains duplicate node ids")
        for work in identities.works:
            if any(node not in self.node_keys for node in work.member_nodes):
                raise ValueError(f"work {work.work_id} contains an unknown identity node")
        for candidate in identities.candidates:
            if candidate.work_id not in self.works:
                raise ValueError(f"candidate {candidate.canonical_id} has an unknown work_id")
            if any(node not in self.node_keys for node in candidate.member_nodes):
                raise ValueError(f"candidate {candidate.canonical_id} contains an unknown node")

    def work_id(self, episode: ScoredEpisode) -> str:
        return self.candidates[episode.candidate_id].work_id

    def _truth_work_nodes(self, episode: Any) -> set[tuple[str, str]]:
        result = {("text", work_key(episode.work))}
        for namespace, value in episode.version.ids.items():
            normalised_namespace = _normalise_identifier(namespace)
            if normalised_namespace == "mb_work" or normalised_namespace in _RECORDING_NAMESPACES:
                result.add((normalised_namespace, _normalise_identifier(value)))
        return result

    def matching_work_ids(self, episode: Any) -> set[str]:
        truth_nodes = self._truth_work_nodes(episode)
        return {
            work_id
            for work_id, work in self.works.items()
            if truth_nodes & {self.node_keys[node] for node in work.member_nodes}
        }

    def truth_work_id(self, episode: Any) -> str:
        matches = self.matching_work_ids(episode)
        if len(matches) > 1:
            raise ValueError("truth identity resolves to multiple canonical work components")
        return next(iter(matches), f"truth:{work_key(episode.work)}")

    def work_equivalent(self, predicted: ScoredEpisode, truth: Any) -> bool:
        return self.work_id(predicted) in self.matching_work_ids(truth)

    def recording_ids(self, episode: ScoredEpisode) -> dict[str, set[str]]:
        result: dict[str, set[str]] = defaultdict(set)
        candidate = self.candidates[episode.candidate_id]
        for node in candidate.member_nodes:
            namespace, value = self.node_keys[node]
            if namespace in _RECORDING_NAMESPACES:
                result[namespace].add(value)
        return result

    def has_resolved_recording(self, episode: ScoredEpisode) -> bool:
        candidate = self.candidates[episode.candidate_id]
        return (
            not candidate.contested
            and not candidate.conflicts
            and bool(self.recording_ids(episode))
        )

    def exact_equivalent(self, predicted: ScoredEpisode, truth: Any) -> bool:
        if not self.work_equivalent(predicted, truth) or not self.has_resolved_recording(predicted):
            return False
        predicted_ids = self.recording_ids(predicted)
        truth_ids = _recording_ids(truth.version)
        common_namespaces = set(predicted_ids) & set(truth_ids)
        if any(predicted_ids[name].isdisjoint(truth_ids[name]) for name in common_namespaces):
            return False
        return any(predicted_ids[name] & truth_ids[name] for name in common_namespaces)


def _support_start(episode: ScoredEpisode) -> int:
    return min(span[0] for span in episode.evidence_support_ms)


def _temporally_compatible(episode: ScoredEpisode, truth: Any) -> bool:
    hull_start = max(0, truth.start_ms_range[0] - ASSOCIATION_MARGIN_MS)
    hull_end = truth.end_ms_range[1] + ASSOCIATION_MARGIN_MS
    return any(
        start <= hull_end and end >= hull_start for start, end in episode.evidence_support_ms
    )


def associate_occurrences(
    predictions: list[ScoredEpisode], truth_episodes: list[Any], identities: _IdentityResolver
) -> dict[int, int]:
    """Greedily associate equivalent occurrences in temporal order, one-to-one."""

    result: dict[int, int] = {}
    truths = sorted(
        range(len(truth_episodes)),
        key=lambda index: (
            truth_episodes[index].start_ms_range[0],
            truth_episodes[index].occurrence_index,
            index,
        ),
    )
    preds = sorted(
        range(len(predictions)),
        key=lambda index: (
            _support_start(predictions[index]),
            predictions[index].occurrence_index,
            index,
        ),
    )
    unused = set(preds)
    for truth_index in truths:
        compatible = [
            index
            for index in preds
            if index in unused
            and identities.work_equivalent(predictions[index], truth_episodes[truth_index])
            and _temporally_compatible(predictions[index], truth_episodes[truth_index])
        ]
        if compatible:
            chosen = compatible[0]
            result[chosen] = truth_index
            unused.remove(chosen)
    return result


def _interval_covered(start: int, end: int, regions: list[tuple[int, int]]) -> int:
    covered = 0
    for left, right in regions:
        covered += max(0, min(end, right) - max(start, left))
    return min(max(0, end - start), covered)


def _prediction_is_evaluable(episode: ScoredEpisode, truth: GroundTruthRecord) -> bool:
    unknown = [(region.start_ms, region.end_ms) for region in truth.regions]
    supported = sum(end - start for start, end in episode.evidence_support_ms)
    hidden = sum(
        _interval_covered(start, end, unknown) for start, end in episode.evidence_support_ms
    )
    return supported > hidden


def _episode_segments(episode: Any, truth: bool) -> list[tuple[int, int, str]]:
    if episode.role_segments:
        return [(item.from_ms, item.to_ms, item.role) for item in episode.role_segments]
    if truth:
        return [(episode.start_ms_range[1], episode.end_ms_range[0], "uncertain")]
    return [(episode.best_start_ms, episode.best_end_ms, "uncertain")]


def _region_type_at(truth: GroundTruthRecord, point: int) -> str | None:
    for region in truth.regions:
        if region.start_ms <= point < region.end_ms:
            return region.type
    return None


def _active_labels(
    episodes: list[Any],
    point: int,
    *,
    truth: bool,
    identity_key: Any,
    roles: set[str] | None = None,
) -> set[str]:
    labels: set[str] = set()
    for episode in episodes:
        for start, end, role in _episode_segments(episode, truth):
            if start <= point < end and (roles is None or role in roles):
                labels.add(identity_key(episode))
    return labels


def _segment_counts(
    truth: GroundTruthRecord,
    predictions: list[ScoredEpisode],
    identities: _IdentityResolver,
    *,
    roles: set[str] | None = None,
) -> tuple[SegmentCount, SegmentCount]:
    boundaries = {0, truth.source.duration_ms}
    for episode in truth.episodes:
        for start, end, _ in _episode_segments(episode, True):
            boundaries.update((start, end))
    for episode in predictions:
        for start, end, _ in _episode_segments(episode, False):
            boundaries.update((max(0, start), min(truth.source.duration_ms, end)))
    for region in truth.regions:
        boundaries.update((region.start_ms, region.end_ms))
    ordered = sorted(point for point in boundaries if 0 <= point <= truth.source.duration_ms)
    counts = SegmentCount()
    for start, end in zip(ordered, ordered[1:], strict=False):
        if end <= start:
            continue
        point = (start + end) // 2
        region_type = _region_type_at(truth, point)
        if region_type in {"silence_or_speech", "unresolved"}:
            continue
        predicted = _active_labels(
            predictions,
            point,
            truth=False,
            identity_key=identities.work_id,
            roles=roles,
        )
        if region_type == "out_of_pool":
            continue
        actual = _active_labels(
            truth.episodes,
            point,
            truth=True,
            identity_key=identities.truth_work_id,
            roles=roles,
        )
        counts.tp += len(actual & predicted) * (end - start)
        counts.fp += len(predicted - actual) * (end - start)
        counts.fn += len(actual - predicted) * (end - start)
    return counts


def _spans_intersect(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _unknown_region_counts(
    truth: GroundTruthRecord, predictions: list[ScoredEpisode]
) -> SegmentCount:
    """Score one binary emission target per annotated unknown region."""

    counts = SegmentCount()
    for region in truth.regions:
        emitted = any(
            any(
                _spans_intersect(support, (region.start_ms, region.end_ms))
                for support in episode.evidence_support_ms
            )
            for episode in predictions
        )
        if region.type == "out_of_pool":
            if emitted:
                counts.tp += 1
            else:
                counts.fn += 1
        elif emitted:
            counts.fp += 1
    return counts


def _ratio_e4(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return min(10_000, (numerator * 10_000 + denominator // 2) // denominator)


def _prf(count: RatioCount) -> dict[str, int]:
    precision = _ratio_e4(count.correct, count.predicted)
    recall = _ratio_e4(count.correct, count.truth)
    f1 = (
        (2 * precision * recall + (precision + recall) // 2) // (precision + recall)
        if precision + recall
        else 0
    )
    return {"precision_e4": precision, "recall_e4": recall, "f1_e4": f1}


def _pr(count: SegmentCount) -> dict[str, int]:
    return {
        "precision_e4": _ratio_e4(count.tp, count.tp + count.fp),
        "recall_e4": _ratio_e4(count.tp, count.tp + count.fn),
    }


def _percentile(values: list[int], numerator: int, denominator: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, (len(ordered) * numerator + denominator - 1) // denominator - 1)
    return ordered[index]


def _range_error(point: int, interval: tuple[int, int]) -> int:
    if point < interval[0]:
        return interval[0] - point
    if point > interval[1]:
        return point - interval[1]
    return 0


def _interval_values(pi: PredictionInterval, truth_range: tuple[int, int]) -> tuple[int, int, int]:
    covered = int(pi.lo <= truth_range[1] and pi.hi >= truth_range[0])
    width = pi.hi - pi.lo
    distance = max(truth_range[0] - pi.hi, pi.lo - truth_range[1], 0)
    alpha_e4 = 10_000 - pi.coverage_target
    if distance and alpha_e4 <= 0:
        winkler = width + distance * 20_000
    else:
        winkler = width + (2 * 10_000 * distance + alpha_e4 // 2) // max(1, alpha_e4)
    return covered, width, winkler


def _iou_e4(predicted: tuple[int, int], actual: tuple[int, int]) -> int:
    intersection = max(0, min(predicted[1], actual[1]) - max(predicted[0], actual[0]))
    union = max(predicted[1], actual[1]) - min(predicted[0], actual[0])
    return _ratio_e4(intersection, union)


def clopper_pearson_lower_e4(successes: int, total: int, *, alpha_e4: int = 500) -> int:
    """One-sided Clopper-Pearson lower bound using an integer-parameter beta CDF."""

    if total < 0 or successes < 0 or successes > total:
        raise ValueError("successes must be between zero and total")
    if not 0 < alpha_e4 < 10_000:
        raise ValueError("alpha_e4 must be between zero and 10000")
    if successes == 0 or total == 0:
        return 0
    alpha = alpha_e4 / 10_000

    def beta_cdf(value: float) -> float:
        # I_x(k, n-k+1) is the upper binomial tail P(Bin(n, x) >= k).
        return sum(
            math.comb(total, index) * value**index * (1.0 - value) ** (total - index)
            for index in range(successes, total + 1)
        )

    low, high = 0.0, 1.0
    for _ in range(80):
        middle = (low + high) / 2
        if beta_cdf(middle) < alpha:
            low = middle
        else:
            high = middle
    return max(0, min(10_000, math.floor(low * 10_000 + 1e-9)))


_EVENT_NOTE = re.compile(r"(?:^|[;, ]+)event:(jump|loop|reset|drift)@(\d+)(?:$|[;, ]+)")


def _truth_events(truth: GroundTruthRecord) -> dict[str, list[int]]:
    events: dict[str, list[int]] = {name: [] for name in ("jump", "loop", "reset", "drift")}
    for episode in truth.episodes:
        for event, at_ms in _EVENT_NOTE.findall(episode.note or ""):
            events[event].append(int(at_ms))
    return events


def _score_events(
    state: ScoreState, truth: GroundTruthRecord, predictions: list[ScoredEpisode]
) -> None:
    actual = _truth_events(truth)
    emitted: dict[str, list[int]] = {name: [] for name in actual}
    for episode in predictions:
        for event in episode.alignment_events:
            emitted[event.type].append(event.at_ms)
    for event in actual:
        unused = set(range(len(emitted[event])))
        matched = 0
        for at_ms in sorted(actual[event]):
            candidates = sorted(
                (abs(emitted[event][index] - at_ms), index)
                for index in unused
                if abs(emitted[event][index] - at_ms) <= EVENT_TOLERANCE_MS
            )
            if candidates:
                _, index = candidates[0]
                unused.remove(index)
                matched += 1
        state.event_counts[event] = RatioCount(matched, len(emitted[event]), len(actual[event]))


def _populate_tiers(
    state: ScoreState,
    predictions: list[ScoredEpisode],
    evaluable: set[int],
    associations: dict[int, int],
    truth: GroundTruthRecord,
    identities: _IdentityResolver,
) -> None:
    for tier in _CERTIFICATION_TIERS:
        work_indexes = [
            index
            for index in evaluable
            if TIER_ORDER[predictions[index].tiers.work] >= TIER_ORDER[tier]
        ]
        work_correct = sum(index in associations for index in work_indexes)
        state.tier_work[tier] = (work_correct, len(work_indexes))
        for dimension in ("work", "version", "start", "end", "boundary"):
            tier_field = "boundary" if dimension in {"start", "end", "boundary"} else dimension
            indexes = [
                index
                for index in evaluable
                if TIER_ORDER[getattr(predictions[index].tiers, tier_field)] >= TIER_ORDER[tier]
                and not (
                    dimension == "version"
                    and (
                        not identities.has_resolved_recording(predictions[index])
                        or (
                            index in associations
                            and not truth.episodes[associations[index]].version_verified
                        )
                    )
                )
            ]
            correct = 0
            for index in indexes:
                truth_index = associations.get(index)
                if truth_index is None:
                    continue
                predicted = predictions[index]
                actual = truth.episodes[truth_index]
                start_ok = (
                    _range_error(predicted.best_start_ms, actual.start_ms_range)
                    <= BOUNDARY_TOLERANCE_MS
                )
                end_ok = (
                    _range_error(predicted.best_end_ms, actual.end_ms_range)
                    <= BOUNDARY_TOLERANCE_MS
                )
                if dimension == "work":
                    correct += 1
                elif dimension == "version":
                    correct += int(
                        actual.version_verified and identities.exact_equivalent(predicted, actual)
                    )
                elif dimension == "start":
                    correct += int(start_ok)
                elif dimension == "end":
                    correct += int(end_ok)
                else:
                    correct += int(start_ok and end_ok)
            state.certification[(dimension, tier)] = (correct, len(indexes))


def score_set(truth: GroundTruthRecord, prediction_set: PredictionSet) -> SetScore:
    predictions = prediction_set.episodes
    identities = _IdentityResolver(prediction_set.identities)
    evaluable = {
        index
        for index, episode in enumerate(predictions)
        if _prediction_is_evaluable(episode, truth)
    }
    evaluable_order = sorted(evaluable)
    local_associations = associate_occurrences(
        [predictions[index] for index in evaluable_order], truth.episodes, identities
    )
    associations = {
        evaluable_order[prediction_index]: truth_index
        for prediction_index, truth_index in local_associations.items()
    }
    state = ScoreState()

    associated_works = {identities.work_id(predictions[index]) for index in associations}
    predicted_works = {identities.work_id(predictions[index]) for index in evaluable}
    truth_works = {identities.truth_work_id(episode) for episode in truth.episodes}
    state.identification_work = RatioCount(
        len(associated_works), len(predicted_works), len(truth_works)
    )

    exact_truth_keys = {
        key
        for episode in truth.episodes
        if episode.version_verified
        if (key := _version_key(episode.work, episode.version)) is not None
    }
    version_evaluable = {
        index
        for index in evaluable
        if identities.has_resolved_recording(predictions[index])
        and not (index in associations and not truth.episodes[associations[index]].version_verified)
    }
    exact_pred_keys = {predictions[index].candidate_id for index in version_evaluable}
    exact_correct_keys: set[str] = set()
    for prediction_index, truth_index in associations.items():
        predicted = predictions[prediction_index]
        actual = truth.episodes[truth_index]
        if actual.version_verified and identities.exact_equivalent(predicted, actual):
            exact_correct_keys.add(predicted.candidate_id)
    state.identification_version = RatioCount(
        len(exact_correct_keys), len(exact_pred_keys), len(exact_truth_keys)
    )
    state.occurrence = RatioCount(len(associations), len(evaluable), len(truth.episodes))

    state.segment = _segment_counts(truth, predictions, identities)
    state.unknown = _unknown_region_counts(truth, predictions)
    state.dominant = _segment_counts(truth, predictions, identities, roles={"dominant"})
    state.secondary = _segment_counts(
        truth,
        predictions,
        identities,
        roles={"incoming", "outgoing", "layer", "component"},
    )
    segment_pr = _pr(state.segment)
    state.macro_segment_precision_e4.append(segment_pr["precision_e4"])
    state.macro_segment_recall_e4.append(segment_pr["recall_e4"])

    for index in evaluable:
        state.work_calibration_errors.append(
            abs(predictions[index].scores.work - (10_000 if index in associations else 0))
        )
    for prediction_index, truth_index in associations.items():
        predicted = predictions[prediction_index]
        actual = truth.episodes[truth_index]
        state.start_errors.append(_range_error(predicted.best_start_ms, actual.start_ms_range))
        state.end_errors.append(_range_error(predicted.best_end_ms, actual.end_ms_range))
        state.start_bound_n += 1
        state.end_bound_n += 1
        state.start_bound_violations += int(
            predicted.start_no_later_than_ms < actual.start_ms_range[0]
        )
        state.end_bound_violations += int(predicted.end_no_earlier_than_ms > actual.end_ms_range[1])
        if predicted.start_pi is not None:
            covered, width, winkler = _interval_values(predicted.start_pi, actual.start_ms_range)
            state.start_pi_covered.append(covered)
            state.start_pi_widths.append(width)
            state.start_pi_winkler.append(winkler)
        if predicted.end_pi is not None:
            covered, width, winkler = _interval_values(predicted.end_pi, actual.end_ms_range)
            state.end_pi_covered.append(covered)
            state.end_pi_widths.append(width)
            state.end_pi_winkler.append(winkler)
        state.iou_e4.append(
            _iou_e4(
                (predicted.best_start_ms, predicted.best_end_ms),
                (actual.start_ms_range[0], actual.end_ms_range[1]),
            )
        )
        actual_component = any(segment.role == "component" for segment in actual.role_segments)
        predicted_component = predicted.claim == "component_evidence"
        key = (
            ("component" if actual_component else "performed")
            + "_as_"
            + ("component" if predicted_component else "performed")
        )
        state.confusion[key] += 1

    state.iou_e4.extend([0] * (len(truth.episodes) - len(associations)))

    state.repeat_truth = sum(episode.occurrence_index > 0 for episode in truth.episodes)
    state.repeat_matched = sum(
        truth.episodes[index].occurrence_index > 0 for index in associations.values()
    )
    state.overlap_truth = sum(bool(episode.overlaps_with) for episode in truth.episodes)
    state.overlap_matched = sum(
        bool(truth.episodes[index].overlaps_with) for index in associations.values()
    )
    _score_events(state, truth, predictions)
    _populate_tiers(state, predictions, evaluable, associations, truth, identities)
    return SetScore(truth, state, _metrics_from_state(state, physical_attempts=0))


def _pi_metrics(covered: list[int], widths: list[int], winkler: list[int]) -> dict[str, int]:
    return {
        "coverage_e4": _ratio_e4(sum(covered), len(covered)),
        "median_width_ms": _percentile(widths, 1, 2),
        "p90_width_ms": _percentile(widths, 9, 10),
        "winkler_score": ((sum(winkler) + len(winkler) // 2) // len(winkler) if winkler else 0),
    }


def _metrics_from_state(state: ScoreState, *, physical_attempts: int) -> BenchmarkMetrics:
    segment = _pr(state.segment)
    start_pi = _pi_metrics(state.start_pi_covered, state.start_pi_widths, state.start_pi_winkler)
    end_pi = _pi_metrics(state.end_pi_covered, state.end_pi_widths, state.end_pi_winkler)
    combined_covered = state.start_pi_covered + state.end_pi_covered
    combined_widths = state.start_pi_widths + state.end_pi_widths
    combined_winkler = state.start_pi_winkler + state.end_pi_winkler
    combined_pi = _pi_metrics(combined_covered, combined_widths, combined_winkler)
    tier_precision: dict[str, int] = {}
    tier_lower: dict[str, int] = {}
    for tier in ("possible", "likely", "verified"):
        correct, total = state.tier_work.get(tier, (0, 0))
        tier_precision[tier] = _ratio_e4(correct, total)
        tier_lower[tier] = clopper_pearson_lower_e4(correct, total)
    events = {
        name: {
            "precision_e4": _ratio_e4(count.correct, count.predicted),
            "recall_e4": _ratio_e4(count.correct, count.truth),
            "n": count.truth,
        }
        for name, count in state.event_counts.items()
    }
    return BenchmarkMetrics(
        identification_work=_prf(state.identification_work),
        identification_version=_prf(state.identification_version),
        occurrence=_prf(state.occurrence),
        segment_micro=segment,
        segment_macro_by_set={
            "precision_e4": (
                sum(state.macro_segment_precision_e4) + len(state.macro_segment_precision_e4) // 2
            )
            // max(1, len(state.macro_segment_precision_e4)),
            "recall_e4": (
                sum(state.macro_segment_recall_e4) + len(state.macro_segment_recall_e4) // 2
            )
            // max(1, len(state.macro_segment_recall_e4)),
        },
        selective_precision_e4=_prf(state.occurrence)["precision_e4"],
        selective_recall_e4=_prf(state.occurrence)["recall_e4"],
        selective_coverage_e4=_prf(state.occurrence)["recall_e4"],
        empirical_tier_precision_e4=tier_precision,
        empirical_tier_lower_bound_e4=tier_lower,
        calibration_error_e4=(
            (sum(state.work_calibration_errors) + len(state.work_calibration_errors) // 2)
            // len(state.work_calibration_errors)
            if state.work_calibration_errors
            else 0
        ),
        false_discovery_rate_e4=10_000 - _prf(state.occurrence)["precision_e4"],
        start_median_absolute_error_ms=_percentile(state.start_errors, 1, 2),
        start_p90_error_ms=_percentile(state.start_errors, 9, 10),
        start_within_5s_e4=_ratio_e4(
            sum(value <= 5_000 for value in state.start_errors), len(state.start_errors)
        ),
        start_within_10s_e4=_ratio_e4(
            sum(value <= 10_000 for value in state.start_errors), len(state.start_errors)
        ),
        start_within_30s_e4=_ratio_e4(
            sum(value <= 30_000 for value in state.start_errors), len(state.start_errors)
        ),
        start_bound_violation_e4=_ratio_e4(state.start_bound_violations, state.start_bound_n),
        start_bound_n=state.start_bound_n,
        start_interval_coverage_e4=start_pi["coverage_e4"],
        start_interval_median_width_ms=start_pi["median_width_ms"],
        start_interval_p90_width_ms=start_pi["p90_width_ms"],
        start_interval_winkler_score=start_pi["winkler_score"],
        end_median_absolute_error_ms=_percentile(state.end_errors, 1, 2),
        end_p90_error_ms=_percentile(state.end_errors, 9, 10),
        end_within_5s_e4=_ratio_e4(
            sum(value <= 5_000 for value in state.end_errors), len(state.end_errors)
        ),
        end_within_10s_e4=_ratio_e4(
            sum(value <= 10_000 for value in state.end_errors), len(state.end_errors)
        ),
        end_within_30s_e4=_ratio_e4(
            sum(value <= 30_000 for value in state.end_errors), len(state.end_errors)
        ),
        end_bound_violation_e4=_ratio_e4(state.end_bound_violations, state.end_bound_n),
        end_bound_n=state.end_bound_n,
        end_interval_coverage_e4=end_pi["coverage_e4"],
        end_interval_median_width_ms=end_pi["median_width_ms"],
        end_interval_p90_width_ms=end_pi["p90_width_ms"],
        end_interval_winkler_score=end_pi["winkler_score"],
        boundary_interval_coverage_e4=combined_pi["coverage_e4"],
        boundary_interval_median_width_ms=combined_pi["median_width_ms"],
        boundary_interval_p90_width_ms=combined_pi["p90_width_ms"],
        boundary_winkler_score=combined_pi["winkler_score"],
        episode_iou_e4=(sum(state.iou_e4) + len(state.iou_e4) // 2) // len(state.iou_e4)
        if state.iou_e4
        else 0,
        repeated_occurrence_recall_e4=_ratio_e4(state.repeat_matched, state.repeat_truth),
        overlap_recall_e4=_ratio_e4(state.overlap_matched, state.overlap_truth),
        event_jump=events["jump"],
        event_loop=events["loop"],
        event_reset=events["reset"],
        event_drift=events["drift"],
        performed_component_confusion=state.confusion,
        dominant_layer=_pr(state.dominant),
        secondary_layer=_pr(state.secondary),
        unknown_region=_pr(state.unknown),
        physical_attempts=physical_attempts,
    )


def _aggregate(states: list[ScoreState]) -> ScoreState:
    result = ScoreState()
    for state in states:
        result.add(state)
    return result


def _bootstrap_lower(
    scores: list[SetScore], metric: Any, *, seed: int, replicates: int = BOOTSTRAP_REPLICATES
) -> int:
    if not scores:
        return 0
    rng = random.Random(seed)
    estimates: list[int] = []
    for _ in range(replicates):
        sample = [scores[rng.randrange(len(scores))].state for _ in scores]
        estimates.append(metric(_aggregate(sample)))
    return _percentile(estimates, 5, 100)


def _bootstrap_ci(scores: list[SetScore], seed: int) -> dict[str, int]:
    return {
        "work_precision_lower_e4": _bootstrap_lower(
            scores,
            lambda state: _prf(state.identification_work)["precision_e4"],
            seed=seed,
        ),
        "work_recall_lower_e4": _bootstrap_lower(
            scores,
            lambda state: _prf(state.identification_work)["recall_e4"],
            seed=seed + 1,
        ),
        "occurrence_precision_lower_e4": _bootstrap_lower(
            scores, lambda state: _prf(state.occurrence)["precision_e4"], seed=seed + 2
        ),
        "segment_precision_lower_e4": _bootstrap_lower(
            scores, lambda state: _pr(state.segment)["precision_e4"], seed=seed + 3
        ),
    }


def _cert_cluster_lower(scores: list[SetScore], key: tuple[str, str], seed: int) -> int:
    def metric(state: ScoreState) -> int:
        correct, total = state.certification.get(key, (0, 0))
        return _ratio_e4(correct, total)

    return _bootstrap_lower(scores, metric, seed=seed)


def paired_non_inferiority(
    baseline: dict[str, tuple[int, int]],
    challenger: dict[str, tuple[int, int]],
    *,
    seed: int,
    margin_e4: int = 100,
    replicates: int = BOOTSTRAP_REPLICATES,
) -> dict[str, int | bool]:
    """Paired one-sided cluster bootstrap for a 1 percentage-point default margin."""

    keys = sorted(set(baseline) & set(challenger))
    if not keys:
        raise ValueError("paired non-inferiority requires at least one common set")

    def estimate(sample: list[str], values: dict[str, tuple[int, int]]) -> int:
        correct = sum(values[key][0] for key in sample)
        total = sum(values[key][1] for key in sample)
        return _ratio_e4(correct, total)

    observed = estimate(keys, challenger) - estimate(keys, baseline)
    rng = random.Random(seed)
    deltas: list[int] = []
    for _ in range(replicates):
        sample = [keys[rng.randrange(len(keys))] for _ in keys]
        deltas.append(estimate(sample, challenger) - estimate(sample, baseline))
    lower = _percentile(deltas, 5, 100)
    return {
        "delta_e4": observed,
        "lower_bound_e4": lower,
        "margin_e4": margin_e4,
        "pass": lower >= -margin_e4,
        "n_sets": len(keys),
    }


def load_truth_directory(path: Path) -> list[GroundTruthRecord]:
    path = path.resolve()
    if path.is_file():
        candidates = [path]
    else:
        candidates = sorted(path.rglob("ground_truth.json"))
        if not candidates:
            candidates = sorted(path.glob("*.json"))
    truths: list[GroundTruthRecord] = []
    for candidate in candidates:
        try:
            truths.append(GroundTruthRecord.model_validate_json(read_text(candidate)))
        except Exception:
            if candidate.name == "ground_truth.json" or path.is_file():
                raise
    if not truths:
        raise ValueError(f"no ground_truth.json files found under {path}")
    if len({truth.set_id for truth in truths}) != len(truths):
        raise ValueError("truth directory contains duplicate set_id values")
    return truths


def truth_is_frozen_verified(path: Path, truths: list[GroundTruthRecord]) -> bool:
    """Return verified state only for non-draft truth covered by a hash-checked freeze manifest."""

    resolved = path.resolve()
    directories = [resolved] if resolved.is_dir() else [resolved.parent, *resolved.parents[1:3]]
    manifest_path = next(
        (
            directory / "corpus-version.json"
            for directory in directories
            if (directory / "corpus-version.json").is_file()
        ),
        None,
    )
    if manifest_path is None:
        return False
    manifest = json.loads(read_text(manifest_path))
    if manifest.get("frozen") is not True:
        return False
    corpus_versions = {truth.corpus_version for truth in truths}
    if corpus_versions != {manifest.get("corpus_version")}:
        raise ValueError("freeze manifest corpus_version differs from loaded truth")
    entries = {str(item.get("set_id")): item for item in manifest.get("sets", [])}
    for truth in truths:
        if any(
            episode.draft or episode.verified_against is None or episode.annotator_ref is None
            for episode in truth.episodes
        ):
            return False
        entry = entries.get(truth.set_id)
        if entry is None:
            raise ValueError(f"freeze manifest does not cover truth set {truth.set_id}")
        truth_file = manifest_path.parent / str(entry.get("path", ""))
        if not truth_file.is_file() or sha256_file(truth_file) != entry.get("sha256"):
            raise ValueError(f"freeze manifest hash mismatch for truth set {truth.set_id}")
    return True


def score_corpus(
    truth_path: Path,
    predictions_path: Path,
    *,
    out_path: Path | None = None,
) -> BenchmarkReportRecord:
    report, _ = score_corpus_detailed(truth_path, predictions_path, out_path=out_path)
    return report


def score_corpus_detailed(
    truth_path: Path,
    predictions_path: Path,
    *,
    out_path: Path | None = None,
) -> tuple[BenchmarkReportRecord, list[SetScore]]:
    """Score a corpus and also return the per-set states, which carry the raw numerators."""

    truths = load_truth_directory(truth_path)
    raw_predictions = json.loads(read_text(predictions_path))
    document = PredictionDocument.model_validate(raw_predictions)
    verified = truth_is_frozen_verified(truth_path, truths)
    derived_unverified = not verified
    if document.unverified_seed_comparison != derived_unverified:
        raise ValueError(
            "unverified_seed_comparison contradicts loaded truth and freeze manifest state"
        )
    truth_by_id = {truth.set_id: truth for truth in truths}
    prediction_by_id = {item.set_id: item for item in document.sets}
    if set(truth_by_id) != set(prediction_by_id):
        raise ValueError("prediction set_ids must exactly match truth set_ids")
    corpus_versions = {truth.corpus_version for truth in truths}
    if corpus_versions != {document.corpus_version}:
        raise ValueError("truth and prediction corpus_version values differ")
    scores = [score_set(truth, prediction_by_id[truth.set_id]) for truth in truths]
    states = [score.state for score in scores]
    overall_state = _aggregate(states)
    overall = _metrics_from_state(overall_state, physical_attempts=document.cost.physical_attempts)
    strata: list[dict[str, Any]] = []
    for stratum in sorted({score.truth.stratum for score in scores}):
        group = [score for score in scores if score.truth.stratum == stratum]
        stratum_seed = document.config_snapshot.bootstrap_seed + int.from_bytes(
            sha256(stratum.encode()).digest()[:4], "big"
        )
        strata.append(
            {
                "stratum": stratum,
                "metrics": _metrics_from_state(
                    _aggregate([item.state for item in group]), physical_attempts=0
                ),
                "ci": _bootstrap_ci(group, stratum_seed),
            }
        )

    test_scores = [
        score
        for score in scores
        if score.truth.split == "test"
        and "controlled" not in score.truth.stratum.casefold()
        and "self-index" not in score.truth.stratum.casefold()
    ]
    registered_targets = {
        (item.dimension, item.tier): item.target_e4
        for item in document.config_snapshot.certification_targets
        if item.profile == document.profile
    }
    certification: list[dict[str, Any]] = []
    for dimension in _DIMENSIONS:
        for tier in _CERTIFICATION_TIERS:
            key = (dimension, tier)
            correct = sum(score.state.certification.get(key, (0, 0))[0] for score in test_scores)
            total = sum(score.state.certification.get(key, (0, 0))[1] for score in test_scores)
            cp_lower = clopper_pearson_lower_e4(correct, total)
            cluster_lower = _cert_cluster_lower(
                test_scores,
                key,
                document.config_snapshot.bootstrap_seed + len(certification) * 17,
            )
            n_sets = sum(score.state.certification.get(key, (0, 0))[1] > 0 for score in test_scores)
            target = registered_targets.get(key)
            certification.append(
                {
                    "dimension": dimension,
                    "tier": tier,
                    "n": total,
                    "errors": total - correct,
                    "lower_bound_e4": cp_lower,
                    "cluster_lower_bound_e4": cluster_lower,
                    "n_sets": n_sets,
                    "target_e4": target,
                    "registration_version": (
                        document.config_snapshot.config_version if target is not None else None
                    ),
                    "status": (
                        "certified"
                        if verified
                        and target is not None
                        and cp_lower >= target
                        and cluster_lower >= target
                        and n_sets >= 10
                        else "provisional"
                    ),
                }
            )
    report = BenchmarkReportRecord(
        schema_version="1.0.0",
        generated_by="id-detector/0.1.0",
        corpus_version=document.corpus_version,
        profile=document.profile,
        config_hash=document.config_hash,
        sets=[
            {
                "set_id": score.truth.set_id,
                "stratum": score.truth.stratum,
                "split": score.truth.split,
                "metrics": score.metrics,
            }
            for score in scores
        ],
        strata=strata,
        overall=overall,
        engines=document.engines,
        cost=document.cost,
        certification=certification,
        regression={"baseline_report_ref": None, "deltas": {}, "gates": []},
        unverified_seed_comparison=derived_unverified,
    )
    if out_path is not None:
        atomic_write_json(out_path, report)
    return report, scores
