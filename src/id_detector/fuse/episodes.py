"""Generation-zero episodes, proved bounds, roles, gaps, tiers, and rescan requests."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    CertificationBlock,
    CertificationEntry,
    DurationsRecord,
    EpisodeRecord,
    EpisodesFile,
    GapEvidence,
    GapRecord,
    HintRecord,
    ObservationRecord,
    RescanPolicy,
    RescanRequestRecord,
    RoleSegment,
    Transform,
    WindowRecord,
    compose_natural_key,
    make_id,
)
from id_detector.fuse.alignment import (
    AlignmentOccurrence,
    align_selected_points,
    select_logical_trial_points,
)
from id_detector.fuse.identity import (
    IdentityBuildResult,
    build_identity_graph,
    write_identity_graph,
)
from id_detector.io import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    sha256_file,
    write_completion_sidecar,
)
from id_detector.semantics import (
    RECORDING_NAMESPACES,
    gap_intervals,
    interval_length,
    normalise_intervals,
    partition_durations,
    proved_bounds,
)


@dataclass(frozen=True)
class FusionResult:
    identities: IdentityBuildResult
    episodes: EpisodesFile
    identities_path: Path
    generation_path: Path
    final_path: Path
    rescan_path: Path


def _intersects(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return min(left[1], right[1]) > max(left[0], right[0])


def _interval_overlap_ms(
    left: list[tuple[int, int]], right: list[tuple[int, int]], duration_ms: int
) -> int:
    intersections = [
        (max(a_start, b_start), min(a_end, b_end))
        for a_start, a_end in left
        for b_start, b_end in right
        if min(a_end, b_end) > max(a_start, b_start)
    ]
    return interval_length(intersections, duration_ms)


def _independent_trials(votes: list[ObservationRecord]) -> int:
    """Return ``T_ind`` (rev 5.2): the largest set of non-overlapping trial supports.

    Each logical trial contributes one interval — the hull of the supports of its selected
    observations — and greedy interval scheduling (earliest finishing interval first) yields the
    maximum pairwise non-overlapping subset.  Overlapping windows from a dense hop or a rescan
    therefore cannot inflate the tier without new, disjoint evidence.
    """

    by_trial: dict[str, tuple[int, int]] = {}
    for item in votes:
        start, end = item.support_ms
        current = by_trial.get(item.logical_trial_id)
        by_trial[item.logical_trial_id] = (
            (start, end) if current is None else (min(current[0], start), max(current[1], end))
        )
    count = 0
    last_end: int | None = None
    for start, end in sorted(by_trial.values(), key=lambda item: (item[1], item[0])):
        if last_end is None or start >= last_end:
            count += 1
            last_end = end
    return count


def _tier_score(tier: str) -> int:
    return {"unclear": 2_500, "possible": 6_000, "likely": 8_000}[tier]


def _badge(work: str, version: str) -> str:
    return "likely" if work == "verified" and version != "verified" else work


def _candidate_recording_specific(identity: IdentityBuildResult, candidate_id: str) -> bool:
    candidate = next(
        item for item in identity.record.candidates if item.canonical_id == candidate_id
    )
    return any(node.split(":", 1)[0] in RECORDING_NAMESPACES for node in candidate.member_nodes)


def _eligible_tracklist_hint(hint: HintRecord) -> bool:
    return (
        hint.kind == "tracklist_line"
        and hint.mirror_status == "verified"
        and not hint.flags.id_unknown
        and (hint.author.is_uploader or hint.is_pinned or hint.connector in {"mixesdb", "1001tl"})
    )


def _competition(
    supports: list[tuple[int, int]],
    candidate_id: str,
    all_supports: dict[str, list[tuple[int, int]]],
    duration_ms: int,
) -> bool:
    support_length = interval_length(supports, duration_ms)
    if support_length == 0:
        return False
    return any(
        other != candidate_id
        and _interval_overlap_ms(supports, other_supports, duration_ms) * 2 >= support_length
        for other, other_supports in all_supports.items()
    )


def _assign_observations(
    final_matches: list[ObservationRecord],
    selected_ids: set[str],
    alignments: dict[str, tuple[AlignmentOccurrence, ...]],
    identity: IdentityBuildResult,
    hints: list[HintRecord] | tuple[HintRecord, ...] = (),
) -> dict[
    str,
    list[tuple[list[ObservationRecord], list[ObservationRecord], AlignmentOccurrence | None]],
]:
    selected_owner: dict[tuple[str, str], str] = {}
    for observation in final_matches:
        if observation.id not in selected_ids:
            continue
        candidate = identity.observation_candidates.get(observation.id)
        if candidate is None:
            continue
        source = observation.native.get("simultaneous_source")
        source_key = str(source) if source is not None else "primary"
        selected_owner[(observation.logical_trial_id, source_key)] = candidate

    by_candidate: dict[str, list[ObservationRecord]] = defaultdict(list)
    for observation in final_matches:
        source = observation.native.get("simultaneous_source")
        source_key = str(source) if source is not None else "primary"
        candidate = selected_owner.get(
            (observation.logical_trial_id, source_key),
            identity.observation_candidates.get(observation.id),
        )
        if candidate is not None:
            by_candidate[candidate].append(observation)

    result: dict[
        str,
        list[tuple[list[ObservationRecord], list[ObservationRecord], AlignmentOccurrence | None]],
    ] = {}
    for candidate, observations in sorted(by_candidate.items()):
        occurrences = list(alignments.get(candidate, ()))
        if not occurrences:
            evidence = sorted(observations, key=lambda item: item.id)
            result[candidate] = [
                (evidence, [item for item in evidence if item.id in selected_ids], None)
            ]
            continue
        buckets: list[list[ObservationRecord]] = [[] for _ in occurrences]
        point_owner = {
            point.observation_id: index
            for index, occurrence in enumerate(occurrences)
            for point in occurrence.points
        }
        trial_owner = {
            point.logical_trial_id: index
            for index, occurrence in enumerate(occurrences)
            for point in occurrence.points
        }
        for observation in observations:
            owner = point_owner.get(observation.id)
            if owner is None:
                owner = trial_owner.get(observation.logical_trial_id)
            if owner is None:
                centre = (observation.support_ms[0] + observation.support_ms[1]) // 2
                owner = min(
                    range(len(occurrences)),
                    key=lambda index: (
                        min(
                            abs(centre - point.mix_anchor_ms) for point in occurrences[index].points
                        ),
                        index,
                    ),
                )
            buckets[owner].append(observation)
        result[candidate] = [
            (
                sorted(bucket, key=lambda item: item.id),
                sorted(
                    (item for item in bucket if item.id in selected_ids),
                    key=lambda item: item.id,
                ),
                occurrence,
            )
            for bucket, occurrence in zip(buckets, occurrences, strict=True)
            if bucket
        ]
    return result


def _role_segments(episodes: list[dict[str, Any]]) -> list[list[RoleSegment]]:
    roles: list[list[RoleSegment]] = []
    for index, episode in enumerate(episodes):
        start, end = episode["outer_hull"]
        if end <= start:
            roles.append([])
            continue
        breakpoints = {start, end}
        overlaps: list[int] = []
        for other_index, other in enumerate(episodes):
            if other_index == index:
                continue
            other_start, other_end = other["outer_hull"]
            overlap_start, overlap_end = max(start, other_start), min(end, other_end)
            if overlap_end > overlap_start:
                overlaps.append(other_index)
                breakpoints.update((overlap_start, overlap_end))
        ordered = sorted(breakpoints)
        pieces: list[RoleSegment] = []
        for left, right in zip(ordered, ordered[1:], strict=False):
            if right <= left:
                continue
            if episode["claim"] == "component_evidence":
                role = "component"
            else:
                active = [
                    other_index
                    for other_index in overlaps
                    if _intersects((left, right), episodes[other_index]["outer_hull"])
                ]
                layer = active and all(
                    episode["trials_in"](left, right) >= 2
                    and episodes[other_index]["trials_in"](left, right) >= 2
                    for other_index in active
                )
                if layer:
                    role = "layer"
                elif active:
                    contenders = [index, *active]
                    first = min(
                        contenders,
                        key=lambda item: (
                            episodes[item]["outer_hull"][0],
                            episodes[item]["candidate_id"],
                        ),
                    )
                    role = "outgoing" if first == index else "incoming"
                else:
                    role = "dominant"
            if pieces and pieces[-1].role == role and pieces[-1].to_ms == left:
                pieces[-1] = pieces[-1].model_copy(update={"to_ms": right})
            else:
                pieces.append(RoleSegment(from_ms=left, to_ms=right, role=role))
        roles.append(pieces)
    return roles


def _certification(profile: str) -> CertificationBlock:
    return CertificationBlock(
        profile=profile,
        per=[
            CertificationEntry(
                dimension=dimension,
                tier=tier,
                status="provisional",
                n_test_predictions=0,
                lower_bound_e4=0,
                test_version="not-run",
            )
            for dimension in ("work", "version", "start", "end", "boundary")
            for tier in ("possible", "likely", "verified")
        ],
    )


def build_episodes(
    *,
    media_key: str,
    duration_ms: int,
    observations: list[ObservationRecord] | tuple[ObservationRecord, ...],
    windows: list[WindowRecord] | tuple[WindowRecord, ...],
    identity: IdentityBuildResult,
    hints: list[HintRecord] | tuple[HintRecord, ...] = (),
    generation: int = 0,
    profile: str = "free",
    rescan_transforms: tuple[Transform, ...] | list[Transform] | None = None,
) -> tuple[EpisodesFile, list[RescanRequestRecord]]:
    final = [item for item in observations if item.is_final]
    selection = select_logical_trial_points(final, identity.observation_candidates)
    selected_ids = set(selection.selected_observation_ids)
    final_matches = [item for item in final if item.status == "match"]
    alignments = align_selected_points(selection)
    assigned = _assign_observations(final_matches, selected_ids, alignments, identity)
    candidate_by_id = {item.canonical_id: item for item in identity.record.candidates}

    # rev 5.2: proved bounds, evidence support and competition are read only from the per-trial
    # selected observations. A rejected hypothesis sibling — which may even name another track —
    # never contributes support to the candidate that won its logical trial.
    all_supports = {
        candidate: normalise_intervals(
            [item.support_ms for _, votes, _ in groups for item in votes], duration_ms
        )
        for candidate, groups in assigned.items()
    }
    provisional: list[dict[str, Any]] = []
    for candidate_id, groups in sorted(assigned.items()):
        for occurrence_index, (evidence, votes, alignment) in enumerate(groups):
            raw_supports = [item.support_ms for item in votes]
            supports = normalise_intervals(raw_supports, duration_ms)
            if not supports:
                continue
            start_bound, end_bound, start_censored, end_censored = proved_bounds(raw_supports)
            independent_trials = _independent_trials(votes)
            candidate_work_id = candidate_by_id[candidate_id].work_id
            supporting_hints = [
                hint
                for hint in hints
                if _eligible_tracklist_hint(hint)
                and identity.hint_work_ids.get(hint.id) == candidate_work_id
                and hint.position_range_ms is not None
                and any(_intersects(hint.position_range_ms, support) for support in supports)
            ]
            hint_vote = int(bool({hint.provenance_group for hint in supporting_hints}))
            span = supports[-1][1] - supports[0][0]
            competing = _competition(supports, candidate_id, all_supports, duration_ms)
            has_global = alignment.has_global_alignment if alignment is not None else False
            if independent_trials >= 4 and span >= 40_000 and not competing and has_global:
                audio_work_tier = "likely"
            elif independent_trials >= 2 and span >= 20_000 and not competing:
                audio_work_tier = "possible"
            else:
                audio_work_tier = "unclear"
            effective_trials = independent_trials + hint_vote
            if effective_trials >= 4 and span >= 40_000 and not competing and has_global:
                work_tier = "likely"
            elif effective_trials >= 2 and span >= 20_000 and not competing:
                work_tier = "possible"
            else:
                work_tier = "unclear"
            candidate = candidate_by_id[candidate_id]
            version_tier = (
                audio_work_tier
                if candidate_id in identity.recording_supported and not candidate.contested
                else "unclear"
            )
            boundary_tier = "possible" if has_global else "unclear"
            claim = (
                "performed"
                if _candidate_recording_specific(identity, candidate_id)
                else "component_evidence"
            )
            natural = {
                "candidate_id": candidate_id,
                "occurrence_index": occurrence_index,
                "first_support_start_ms": supports[0][0],
            }
            episode_id = make_id(media_key, "episode", compose_natural_key("episode", natural))
            rejected = sorted(set(selection.hypothesis_rejected) & {item.id for item in evidence})
            outliers = set(alignment.rejected_observation_ids) if alignment is not None else set()

            def trials_in(left: int, right: int, items: list[ObservationRecord] = votes) -> int:
                return len(
                    {
                        item.logical_trial_id
                        for item in items
                        if _intersects(item.support_ms, (left, right))
                    }
                )

            flags = []
            if candidate.contested:
                flags.append("contested_identity")
            if rejected:
                flags.append("hypothesis_rejected")
            if outliers:
                flags.append("alignment_outlier")
            if supporting_hints:
                flags.append("hint_supported")
            provisional.append(
                {
                    "id": episode_id,
                    "candidate_id": candidate_id,
                    "candidate": candidate,
                    "evidence": evidence,
                    "votes": votes,
                    "rejected_evidence": rejected,
                    "supports": supports,
                    "outer_hull": (supports[0][0], supports[-1][1]),
                    "trials_in": trials_in,
                    "claim": claim,
                    "start_bound": start_bound,
                    "end_bound": end_bound,
                    "start_censored": start_censored,
                    "end_censored": end_censored,
                    "occurrence_index": occurrence_index,
                    "alignment": alignment,
                    "has_global": has_global,
                    "work_tier": work_tier,
                    "version_tier": version_tier,
                    "boundary_tier": boundary_tier,
                    "flags": flags,
                    "supporting_hints": supporting_hints,
                }
            )
    provisional.sort(key=lambda item: (item["outer_hull"][0], item["id"]))
    roles = _role_segments(provisional)
    overlaps: list[list[str]] = [[] for _ in provisional]
    for index, episode in enumerate(provisional):
        for other_index in range(index + 1, len(provisional)):
            if _intersects(episode["outer_hull"], provisional[other_index]["outer_hull"]):
                overlaps[index].append(provisional[other_index]["id"])
                overlaps[other_index].append(episode["id"])

    episode_records: list[EpisodeRecord] = []
    for index, item in enumerate(provisional):
        alignment = item["alignment"]
        work_tier = item["work_tier"]
        version_tier = item["version_tier"]
        boundary_tier = item["boundary_tier"]
        episode_records.append(
            EpisodeRecord(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                id=item["id"],
                candidate_id=item["candidate_id"],
                alternatives=item["candidate"].alternatives,
                claim=item["claim"],
                start_no_later_than_ms=item["start_bound"],
                end_no_earlier_than_ms=item["end_bound"],
                evidence_support_ms=item["supports"],
                start_no_earlier_than_ms=item["start_censored"],
                end_no_later_than_ms=item["end_censored"],
                start_pi=None,
                end_pi=None,
                best_start_ms=item["start_bound"],
                best_end_ms=item["end_bound"],
                role_segments=roles[index],
                occurrence_index=item["occurrence_index"],
                overlaps=sorted(overlaps[index]),
                alignment_segments=(
                    [segment.contract() for segment in alignment.segments]
                    if alignment is not None
                    else []
                ),
                alignment_events=(alignment.episode_events if alignment is not None else []),
                has_global_alignment=item["has_global"],
                scores={
                    "work": _tier_score(work_tier),
                    "version": _tier_score(version_tier),
                    "boundary": _tier_score(boundary_tier),
                },
                score_kind="heuristic",
                tiers={
                    "work": work_tier,
                    "version": version_tier,
                    "boundary": boundary_tier,
                },
                badge=_badge(work_tier, version_tier),
                version_status=(
                    "contested"
                    if item["candidate"].contested
                    else "verified"
                    if version_tier == "verified"
                    else "unverified"
                ),
                evidence=[
                    *[observation.id for observation in item["votes"]],
                    *[hint.id for hint in item["supporting_hints"]],
                ],
                rejected_evidence=item["rejected_evidence"],
                flags=item["flags"],
                rescan_state="requested",
            )
        )

    scanned = normalise_intervals([item.support_ms for item in windows], duration_ms)
    duration_values, duration_intervals = partition_durations(duration_ms, episode_records, scanned)
    gaps: list[GapRecord] = []
    for start, end in gap_intervals(duration_intervals["no_evidence_ms"]):
        interval = (start, end)
        prior = [
            episode for episode in episode_records if episode.evidence_support_ms[-1][1] <= start
        ]
        following = [
            episode for episode in episode_records if episode.evidence_support_ms[0][0] >= end
        ]
        bounded = []
        if prior:
            bounded.append(max(prior, key=lambda item: item.evidence_support_ms[-1][1]).id)
        if following:
            bounded.append(min(following, key=lambda item: item.evidence_support_ms[0][0]).id)
        values = {"start_ms": start, "end_ms": end}
        gap_id = make_id(media_key, "gap", compose_natural_key("gap", values))
        gaps.append(
            GapRecord(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                id=gap_id,
                start_ms=start,
                end_ms=end,
                bounded_by=bounded,
                evidence=GapEvidence(
                    n_windows=sum(_intersects(item.support_ms, interval) for item in windows),
                    n_no_match=sum(
                        item.status == "no_match" and _intersects(item.support_ms, interval)
                        for item in final
                    ),
                    n_error=sum(
                        item.status == "error" and _intersects(item.support_ms, interval)
                        for item in final
                    ),
                    n_unclear_candidates=sum(
                        episode.badge == "unclear"
                        and any(
                            _intersects(support, interval)
                            for support in episode.evidence_support_ms
                        )
                        for episode in episode_records
                    ),
                    n_hint_events=sum(
                        hint.position_range_ms is not None
                        and _intersects(hint.position_range_ms, interval)
                        for hint in hints
                    ),
                    n_novelty_events=0,
                ),
                reason="no_evidence",
                truncated=start == 0 or end == duration_ms,
                best_unclear_candidate=None,
            )
        )

    episodes_file = EpisodesFile(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        generation=generation,
        episodes=episode_records,
        gaps=gaps,
        durations=DurationsRecord(
            schema_version=SCHEMA_VERSION,
            generated_by=GENERATED_BY,
            **duration_values,
        ),
        certification=_certification(profile),
    )
    input_hashes: dict[str, str] = {}
    requests: list[RescanRequestRecord] = []
    policy = RescanPolicy(
        window_ms=8_000,
        hop_ms=4_000,
        phase_ms=2_000,
        transforms=list(rescan_transforms)
        if rescan_transforms is not None
        else [Transform(type="none", rate_e4=10_000, semitones=0)],
    )

    def add_request(trigger: str, start: int, end: int, priority: int) -> None:
        if end <= start:
            return
        natural = {
            "generation": generation,
            "trigger": trigger,
            "start_ms": start,
            "end_ms": end,
            "policy": policy.model_dump(mode="json"),
        }
        requests.append(
            RescanRequestRecord(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                id=make_id(
                    media_key,
                    "rescan_request",
                    compose_natural_key("rescan_request", natural),
                ),
                generation=generation,
                trigger=trigger,
                start_ms=start,
                end_ms=end,
                policy=policy,
                priority=priority,
                input_hashes=input_hashes,
            )
        )

    for gap in gaps:
        add_request("gap", gap.start_ms, gap.end_ms, 100)
    for episode in episode_records:
        candidate = candidate_by_id[episode.candidate_id]
        if candidate.contested:
            add_request(
                "contested",
                episode.evidence_support_ms[0][0],
                episode.evidence_support_ms[-1][1],
                90,
            )
        add_request(
            "edge",
            max(0, episode.best_start_ms - 20_000),
            min(duration_ms, episode.best_start_ms + 20_000),
            70,
        )
        hull_start = episode.evidence_support_ms[0][0]
        hull_end = episode.evidence_support_ms[-1][1]
        if hull_end - hull_start > 12 * 60 * 1_000:
            add_request("long_episode", hull_start, hull_end, 60)
        add_request(
            "edge",
            max(0, episode.best_end_ms - 20_000),
            min(duration_ms, episode.best_end_ms + 20_000),
            70,
        )
    used_hint_ids = {hint.id for item in provisional for hint in item["supporting_hints"]}
    for hint in hints:
        if (
            hint.id not in used_hint_ids
            and hint.kind in {"tracklist_line", "answer", "correction"}
            and hint.mirror_status == "verified"
            and hint.id in identity.hint_work_ids
            and hint.position_range_ms is not None
        ):
            add_request(
                "hint_cluster",
                hint.position_range_ms[0],
                hint.position_range_ms[1],
                80,
            )

    questions = sorted(
        (
            ((hint.position_range_ms[0] + hint.position_range_ms[1]) // 2, hint)
            for hint in hints
            if hint.kind == "question"
            and hint.mirror_status == "verified"
            and hint.position_range_ms is not None
        ),
        key=lambda item: (item[0], item[1].id),
    )
    index = 0
    while index < len(questions):
        end = index
        while end + 1 < len(questions) and questions[end + 1][0] - questions[index][0] <= 90_000:
            end += 1
        cluster = questions[index : end + 1]
        if len(cluster) >= 3:
            add_request(
                "question_cluster",
                min(item.position_range_ms[0] for _, item in cluster if item.position_range_ms),
                max(item.position_range_ms[1] for _, item in cluster if item.position_range_ms),
                95,
            )
            index = end + 1
        else:
            index += 1
    return episodes_file, sorted(requests, key=lambda item: (item.start_ms, item.id))


def _jsonl_bytes(records: list[Any]) -> bytes:
    payload = b"\n".join(canonical_json_bytes(item) for item in records)
    return payload + (b"\n" if payload else b"")


def fuse_generation_zero(
    *,
    media_key: str,
    media_dir: Path,
    duration_ms: int,
    observations: list[ObservationRecord] | tuple[ObservationRecord, ...],
    observations_path: Path,
    windows: list[WindowRecord] | tuple[WindowRecord, ...],
    windows_path: Path,
    pcm_path: Path,
    hints: list[HintRecord] | tuple[HintRecord, ...] = (),
    hints_path: Path | None = None,
    profile: str = "free",
    rescan_transforms: tuple[Transform, ...] | list[Transform] | None = None,
) -> FusionResult:
    identity = build_identity_graph(media_key, observations, hints=hints)
    identities_path = write_identity_graph(
        media_dir,
        0,
        identity,
        observations_path=observations_path,
        hints_path=hints_path,
    )
    episodes, requests = build_episodes(
        media_key=media_key,
        duration_ms=duration_ms,
        observations=observations,
        windows=windows,
        identity=identity,
        hints=hints,
        generation=0,
        profile=profile,
        rescan_transforms=rescan_transforms,
    )
    generation_path = media_dir / "fuse" / "episodes.gen0.json"
    upstream = {
        observations_path.relative_to(media_dir).as_posix(): observations_path,
        windows_path.relative_to(media_dir).as_posix(): windows_path,
        pcm_path.relative_to(media_dir).as_posix(): pcm_path,
        identities_path.relative_to(media_dir).as_posix(): identities_path,
    }
    if hints_path is not None:
        upstream[hints_path.relative_to(media_dir).as_posix()] = hints_path
    atomic_write_json(generation_path, episodes)
    write_completion_sidecar(generation_path, upstream)
    final_path = media_dir / "fuse" / "episodes.json"
    atomic_write_bytes(final_path, canonical_json_bytes(episodes))
    write_completion_sidecar(
        final_path,
        {generation_path.relative_to(media_dir).as_posix(): generation_path},
    )
    input_hashes = {
        path.relative_to(media_dir).as_posix(): sha256_file(path)
        for path in (
            observations_path,
            windows_path,
            identities_path,
            *([hints_path] if hints_path is not None else []),
        )
    }
    requests = [request.model_copy(update={"input_hashes": input_hashes}) for request in requests]
    rescan_path = media_dir / "fuse" / "rescan_plan.gen0.jsonl"
    atomic_write_bytes(rescan_path, _jsonl_bytes(requests))
    write_completion_sidecar(rescan_path, upstream)
    return FusionResult(
        identities=identity,
        episodes=episodes,
        identities_path=identities_path,
        generation_path=generation_path,
        final_path=final_path,
        rescan_path=rescan_path,
    )
