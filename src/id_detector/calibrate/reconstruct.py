"""Reconstruct calibration feature inputs from persisted fusion artefacts.

At ``analyse`` time the fuser has every observation in hand, but at fit/validation time the
calibrator reads persisted ``episodes.json`` + ``identities`` + ``observations``.  This rebuilds the
:class:`~id_detector.calibrate.features.EpisodeFeatureInputs` from those records so a feature is
computed identically in both places.  It imports no orchestration module, so it is import-safe from
``benchmark.corpus``.
"""

from __future__ import annotations

from id_detector.calibrate.features import EpisodeFeatureInputs, build_features
from id_detector.contracts import (
    CalibrationFeatures,
    EpisodeRecord,
    HintRecord,
    IdentitiesRecord,
    ObservationRecord,
)
from id_detector.fuse.episodes import competing_candidate_count
from id_detector.fuse.identity import (
    candidate_recording_supported,
    recording_node_sources_from_observations,
)
from id_detector.semantics import RECORDING_NAMESPACES, interval_length, normalise_intervals


def _candidate_supports(
    episodes: list[EpisodeRecord], duration_ms: int
) -> dict[str, list[tuple[int, int]]]:
    by_candidate: dict[str, list[tuple[int, int]]] = {}
    for episode in episodes:
        by_candidate.setdefault(episode.candidate_id, []).extend(
            (start, end) for start, end in episode.evidence_support_ms
        )
    return {
        candidate: normalise_intervals(spans, duration_ms)
        for candidate, spans in by_candidate.items()
    }


def feature_inputs_from_record(
    episode: EpisodeRecord,
    *,
    identities: IdentitiesRecord,
    observations_by_id: dict[str, ObservationRecord],
    hints_by_id: dict[str, HintRecord],
    all_episodes: list[EpisodeRecord],
    duration_ms: int,
) -> EpisodeFeatureInputs:
    candidate = next(
        item for item in identities.candidates if item.canonical_id == episode.candidate_id
    )
    votes = tuple(observations_by_id[oid] for oid in episode.evidence if oid in observations_by_id)
    supporting_hints = tuple(hints_by_id[hid] for hid in episode.evidence if hid in hints_by_id)
    supports = normalise_intervals(list(episode.evidence_support_ms), duration_ms)
    span_ms = supports[-1][1] - supports[0][0] if supports else 0
    support_total_ms = interval_length(supports, duration_ms)
    all_supports = _candidate_supports(all_episodes, duration_ms)
    # Same authoritative helpers the fuser uses at analyse time, so the features match exactly.
    n_competing = competing_candidate_count(
        supports, episode.candidate_id, all_supports, duration_ms
    )
    recording_nodes = [
        node for node in candidate.member_nodes if node.split(":", 1)[0] in RECORDING_NAMESPACES
    ]
    recording_node_sources = recording_node_sources_from_observations(observations_by_id.values())
    recording_supported = candidate_recording_supported(
        contested=candidate.contested,
        member_nodes=candidate.member_nodes,
        recording_node_sources=recording_node_sources,
    )
    return EpisodeFeatureInputs(
        episode_id=episode.id,
        candidate_id=episode.candidate_id,
        votes=votes,
        supporting_hints=supporting_hints,
        n_alignment_segments=len(episode.alignment_segments),
        max_residual_ms=max(
            (segment.residual_ms for segment in episode.alignment_segments), default=0
        ),
        n_alignment_events=len(episode.alignment_events),
        has_global_alignment=episode.has_global_alignment,
        span_ms=span_ms,
        support_total_ms=support_total_ms,
        competing=n_competing > 0,
        n_competing_candidates=n_competing,
        identity_conflicts=len(candidate.conflicts),
        contested=candidate.contested,
        recording_supported=recording_supported,
        version_ids_count=len(recording_nodes),
        claim=episode.claim,
        heuristic_work_tier=episode.tiers.work,
        heuristic_version_tier=episode.tiers.version,
        heuristic_boundary_tier=episode.tiers.boundary,
    )


def features_from_record(
    episode: EpisodeRecord,
    *,
    identities: IdentitiesRecord,
    observations_by_id: dict[str, ObservationRecord],
    hints_by_id: dict[str, HintRecord],
    all_episodes: list[EpisodeRecord],
    duration_ms: int,
) -> CalibrationFeatures:
    return build_features(
        feature_inputs_from_record(
            episode,
            identities=identities,
            observations_by_id=observations_by_id,
            hints_by_id=hints_by_id,
            all_episodes=all_episodes,
            duration_ms=duration_ms,
        )
    )
