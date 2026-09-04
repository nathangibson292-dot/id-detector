"""Deterministic, integer/fixed-point feature extraction for the calibrator (Stage 5).

The features are exactly the plan's calibrator inputs: ``T`` (independent trials) and ``S`` (span),
alignment residuals and segment count, ``score_raw`` where available, transform consistency, engine
agreement discounted by the correlation prior, one vote per ``logical_trial_id`` and per
``provenance_group``, contradictions, identity conflicts, and version agreement.

Every output is an integer, a boolean, or a fixed enum, so a calibrator fit on these features
re-derives byte-for-byte from the evidence.  The module owns no float arithmetic.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import median

from id_detector.contracts import GENERATED_BY, SCHEMA_VERSION, CalibrationFeatures, HintRecord
from id_detector.contracts import ObservationRecord as _ObservationRecord

# ``_independent_trials_e4``/``discounted_providers`` are owned by the fuser; importing them here is
# safe because ``fuse.episodes`` never imports this module (it applies a duck-typed calibrator).
from id_detector.fuse.episodes import _independent_trials_e4, discounted_providers

FEATURE_NAMES: tuple[str, ...] = (
    "t_ind_e4",
    "n_logical_trials",
    "n_selected_observations",
    "span_ms",
    "support_total_ms",
    "n_alignment_segments",
    "max_residual_ms",
    "n_alignment_events",
    "has_global_alignment",
    "n_providers",
    "engine_agreement_e4",
    "transform_consistency_e4",
    "n_score_raw",
    "median_score_raw",
    "n_provenance_groups",
    "hint_vote_e4",
    "competing",
    "n_competing_candidates",
    "identity_conflicts",
    "contested",
    "recording_supported",
    "version_ids_count",
)


@dataclass(frozen=True)
class EpisodeFeatureInputs:
    """Everything needed to compute one episode's features, from fusion or a persisted run."""

    episode_id: str
    candidate_id: str
    votes: tuple[_ObservationRecord, ...]
    supporting_hints: tuple[HintRecord, ...] = ()
    n_alignment_segments: int = 0
    max_residual_ms: int = 0
    n_alignment_events: int = 0
    has_global_alignment: bool = False
    span_ms: int = 0
    support_total_ms: int = 0
    competing: bool = False
    n_competing_candidates: int = 0
    identity_conflicts: int = 0
    contested: bool = False
    recording_supported: bool = False
    version_ids_count: int = 0
    claim: str = "performed"
    heuristic_work_tier: str = "unclear"
    heuristic_version_tier: str = "unclear"
    heuristic_boundary_tier: str = "unclear"
    extra: dict[str, int] = field(default_factory=dict)


def _engine_agreement_e4(votes: tuple[_ObservationRecord, ...]) -> tuple[int, int]:
    providers = sorted({item.provider for item in votes})
    n_providers = len(providers)
    if n_providers <= 1:
        return 0, n_providers
    winners = Counter(item.provider for item in votes)
    primary = min(winners, key=lambda name: (-winners[name], name))
    corroborating = sorted(name for name in providers if name != primary)
    agreement = min(10_000, len(corroborating) * 10_000)
    discounted = discounted_providers(list(votes))
    if corroborating and all(name in discounted for name in corroborating):
        agreement //= 2
    return agreement, n_providers


def _transform_consistency_e4(votes: tuple[_ObservationRecord, ...]) -> int:
    if not votes:
        return 0
    signatures = [
        None
        if item.transform is None
        else (item.transform.type, item.transform.rate_e4, item.transform.semitones)
        for item in votes
    ]
    dominant = Counter(signatures).most_common(1)[0][0]
    matching = sum(signature == dominant for signature in signatures)
    return min(10_000, (matching * 10_000 + len(votes) // 2) // len(votes))


def _score_raw_summary(votes: tuple[_ObservationRecord, ...]) -> tuple[int, int | None]:
    present = [int(item.score_raw) for item in votes if item.score_raw is not None]
    if not present:
        return 0, None
    return len(present), int(median(sorted(present)))


def build_features(inputs: EpisodeFeatureInputs) -> CalibrationFeatures:
    """Compute the deterministic feature record for one episode."""

    votes = tuple(inputs.votes)
    engine_agreement_e4, n_providers = _engine_agreement_e4(votes)
    n_score_raw, median_score_raw = _score_raw_summary(votes)
    provenance_groups = {
        hint.provenance_group for hint in inputs.supporting_hints if hint.provenance_group
    }
    return CalibrationFeatures(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        episode_id=inputs.episode_id,
        candidate_id=inputs.candidate_id,
        t_ind_e4=_independent_trials_e4(list(votes)),
        n_logical_trials=len({item.logical_trial_id for item in votes}),
        n_selected_observations=len(votes),
        span_ms=max(0, inputs.span_ms),
        support_total_ms=max(0, inputs.support_total_ms),
        n_alignment_segments=max(0, inputs.n_alignment_segments),
        max_residual_ms=max(0, inputs.max_residual_ms),
        n_alignment_events=max(0, inputs.n_alignment_events),
        has_global_alignment=bool(inputs.has_global_alignment),
        n_providers=n_providers,
        engine_agreement_e4=engine_agreement_e4,
        transform_consistency_e4=_transform_consistency_e4(votes),
        n_score_raw=n_score_raw,
        median_score_raw=median_score_raw,
        n_provenance_groups=len(provenance_groups),
        hint_vote_e4=10_000 if inputs.supporting_hints else 0,
        competing=bool(inputs.competing),
        n_competing_candidates=max(0, inputs.n_competing_candidates),
        identity_conflicts=max(0, inputs.identity_conflicts),
        contested=bool(inputs.contested),
        recording_supported=bool(inputs.recording_supported),
        version_ids_count=max(0, inputs.version_ids_count),
        claim=inputs.claim if inputs.claim in {"performed", "component_evidence"} else "performed",
        heuristic_work_tier=inputs.heuristic_work_tier,  # type: ignore[arg-type]
        heuristic_version_tier=inputs.heuristic_version_tier,  # type: ignore[arg-type]
        heuristic_boundary_tier=inputs.heuristic_boundary_tier,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------------------------------
# Monotone ordering indices (documented, integer, no black boxes)
# --------------------------------------------------------------------------------------------------
SPAN_CAP_MS = 120_000
RESIDUAL_CAP_MS = 10_000

WORK_INDEX_FORMULA = (
    "t_ind_e4 + min(span_ms,120000)*10000//120000 + hint_vote_e4//4 "
    "+ engine_agreement_e4//4 + (10000 if has_global_alignment else 0) "
    "- (20000 if competing else 0)"
)
VERSION_INDEX_FORMULA = (
    "work_index + (30000 if recording_supported and not contested else 0) "
    "- (40000 if contested or identity_conflicts else 0)"
)
BOUNDARY_INDEX_FORMULA = (
    "(20000 if has_global_alignment else 0) + (10000 - min(max_residual_ms,10000)) "
    "+ min(n_alignment_segments,4)*1000"
)


def _span_component(span_ms: int) -> int:
    return min(span_ms, SPAN_CAP_MS) * 10_000 // SPAN_CAP_MS


def work_index(features: CalibrationFeatures) -> int:
    return (
        features.t_ind_e4
        + _span_component(features.span_ms)
        + features.hint_vote_e4 // 4
        + features.engine_agreement_e4 // 4
        + (10_000 if features.has_global_alignment else 0)
        - (20_000 if features.competing else 0)
    )


def version_index(features: CalibrationFeatures) -> int:
    base = work_index(features)
    if features.recording_supported and not features.contested:
        base += 30_000
    if features.contested or features.identity_conflicts:
        base -= 40_000
    return base


def boundary_index(features: CalibrationFeatures) -> int:
    return (
        (20_000 if features.has_global_alignment else 0)
        + (10_000 - min(features.max_residual_ms, RESIDUAL_CAP_MS))
        + min(features.n_alignment_segments, 4) * 1_000
    )


DIMENSION_INDEX = {
    "work": work_index,
    "version": version_index,
    "boundary": boundary_index,
}
DIMENSION_INDEX_FORMULA = {
    "work": WORK_INDEX_FORMULA,
    "version": VERSION_INDEX_FORMULA,
    "boundary": BOUNDARY_INDEX_FORMULA,
}
