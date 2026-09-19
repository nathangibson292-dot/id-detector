"""Generation-zero episodes, proved bounds, roles, gaps, tiers, and rescan requests."""

from __future__ import annotations

import re
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
from id_detector.providers.base import AppConfig
from id_detector.rescan import (
    TRIGGER_PRIORITY,
    policy_for_trigger,
    priority_for_trigger,
    schedule_rescan_windows,
)
from id_detector.semantics import (
    RECORDING_NAMESPACES,
    gap_intervals,
    interval_length,
    normalise_intervals,
    partition_durations,
    proved_bounds,
)
from id_detector.truth import certification_enabled


@dataclass(frozen=True)
class FusionResult:
    identities: IdentityBuildResult
    episodes: EpisodesFile
    identities_path: Path
    generation_path: Path
    final_path: Path
    rescan_path: Path
    requests: tuple[RescanRequestRecord, ...] = ()


def _intersects(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return min(left[1], right[1]) > max(left[0], right[0])


def hint_reaches_supports(position: tuple[int, int], supports: list[tuple[int, int]]) -> bool:
    """Whether a hint at ``position`` is about the play these (sorted) supports prove.

    It is when it falls anywhere from ``HINT_LEAD_IN_MS`` before the first support to
    ``HINT_TRAIL_MS`` after the last — the gaps between supports included, where the old
    window-by-window test dropped a comment that landed between two matched windows.
    """

    if not supports:
        return False
    reach = (supports[0][0] - HINT_LEAD_IN_MS, supports[-1][1] + HINT_TRAIL_MS)
    return _intersects(position, reach)


def hint_backed_play(
    position: tuple[int, int], plays: dict[Any, list[tuple[int, int]]], duration_ms: int
) -> tuple[Any, bool] | None:
    """The ONE play of its own work a hint at ``position`` backs, and whether it is DIRECT.

    ``plays`` maps a key to that play's sorted proved supports.  One hint is one listener talking
    about one moment, so it never backs two episodes:

    * DIRECT — the hint sits on a matched window.  Among several, the play it overlaps most wins,
      then the one with more proved on-air time, then the earlier, then the smaller key.
    * INDIRECT — it sits on no window but within reach (:func:`hint_reaches_supports`).  A play
      whose hull it falls inside (between two of its windows) comes first; then the play with the
      most proved on-air time — a track's main body earns the vote, not a stray 12 s fragment of
      the same track beside it; then the nearer, the earlier, the smaller key.  Plays of one work
      270 s apart or less are all but always pieces of one performance, which is why on-air time
      ranks above distance.

    Every tie-break is a total order over the play's own numbers, so the choice is deterministic.
    """

    direct = [
        (-overlap, -interval_length(supports, duration_ms), supports[0][0], repr(key), key)
        for key, supports in plays.items()
        if (
            overlap := sum(
                max(0, min(position[1], support[1]) - max(position[0], support[0]))
                for support in supports
            )
        )
        > 0
    ]
    if direct:
        return min(direct)[-1], True
    in_reach = [
        (
            not _intersects(position, (supports[0][0], supports[-1][1])),
            -interval_length(supports, duration_ms),
            min(abs(supports[0][0] - position[1]), abs(position[0] - supports[-1][1])),
            supports[0][0],
            repr(key),
            key,
        )
        for key, supports in plays.items()
        if hint_reaches_supports(position, supports)
    ]
    return (min(in_reach)[-1], False) if in_reach else None


def hint_backed_plays(
    position: tuple[int, int], plays: dict[Any, list[tuple[int, int]]], duration_ms: int
) -> set[Any]:
    """:func:`hint_backed_play` as a set: empty, or the one play backed."""

    backed = hint_backed_play(position, plays, duration_ms)
    return set() if backed is None else {backed[0]}


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


#: Engines that sell the same commercial catalogue coverage. The plan's dependence prior says a
#: *second* commercial engine's agreement is worth half a trial until dependence is measured.
COMMERCIAL_PROVIDERS = frozenset({"audd", "acrcloud"})
FULL_TRIAL_E4 = 10_000
DISCOUNTED_TRIAL_E4 = 5_000

#: Trust families (plan §2.3.4 step 5): the commercial catalogue engines are ONE family — they
#: sell the same coverage, so their agreement is one catalogue queried twice (review M1) — Shazam
#: is another and the local Panako index a third.  Corroboration counts only across families; a
#: provider outside the table (a test fixture) is its own family.  Crowd hints are never in it:
#: they stay hint corroboration.  The catalogue half is *derived* from
#: :data:`COMMERCIAL_PROVIDERS` rather than repeated, so a commercial engine added there can never
#: become its own family — which is exactly how review M1's flag would be gamed again.
CATALOGUE_FAMILY = "catalogue"
TRUST_FAMILIES: dict[str, str] = {
    **{provider: CATALOGUE_FAMILY for provider in sorted(COMMERCIAL_PROVIDERS)},
    "shazam": "shazam",
    "panako": "local_index",
}
#: The Deep recipe's ``overlap_min_ms`` / ``separation_min_ms``, and what every other fuse uses
#: (the Free recipe defines neither, yet Shazam + a local index can still agree under it).
CORROBORATION_OVERLAP_MIN_MS = 6_000
CORROBORATION_SEPARATION_MIN_MS = 60_000
#: How far outside an episode's proved support a hint for the SAME work may sit and still be about
#: that play.  A tracklist cue and a listener's "ID?" mark where the track is first HEARD — the
#: blend in — while the recogniser only locks on once the track dominates, so the hint is early:
#: on the cached mixes 30 of 36 same-work hints that missed sat before the first matched window
#: (median 45 s, all within 178 s), and the corpus scorer measures the same lag from the other
#: side (the tool's start is a median 17-51 s after the hand-checked start).  After the last
#: matched window the track is only fading out, so the allowance there is shorter.  This never
#: decides WHAT a hint names — that stays the identity graph's both-parts test — only whether a
#: hint naming this very work is close enough to be about this play of it.
HINT_LEAD_IN_MS = 180_000
HINT_TRAIL_MS = 90_000


def trust_family(provider: str) -> str:
    return TRUST_FAMILIES.get(provider, provider)


def engine_agreements(
    votes: list[ObservationRecord] | tuple[ObservationRecord, ...],
    *,
    overlap_min_ms: int = CORROBORATION_OVERLAP_MIN_MS,
) -> list[tuple[int, int]]:
    """The cross-family agreements among the selected votes for one normalised work.

    An agreement is a pair of selected votes from different trust families whose supports overlap
    by at least ``overlap_min_ms``; it is reported as the overlap interval.  Two votes of one
    family never agree with each other, however many engines that family holds (review M1), and
    a coincidence under the overlap floor is not the same moment.  Sorted, de-duplicated.
    """

    ordered = sorted(votes, key=lambda item: (item.support_ms, item.provider, item.id))
    found: set[tuple[int, int]] = set()
    for index, left in enumerate(ordered):
        left_family = trust_family(left.provider)
        for right in ordered[index + 1 :]:
            if right.support_ms[0] >= left.support_ms[1]:
                break  # start order: nothing later can overlap ``left`` any more
            if trust_family(right.provider) == left_family:
                continue
            low = max(left.support_ms[0], right.support_ms[0])
            high = min(left.support_ms[1], right.support_ms[1])
            if high - low >= overlap_min_ms:
                found.add((low, high))
    return sorted(found)


def separated_agreements(
    agreements: list[tuple[int, int]] | tuple[tuple[int, int], ...],
    *,
    separation_min_ms: int = CORROBORATION_SEPARATION_MIN_MS,
) -> bool:
    """Whether two agreements lie at least ``separation_min_ms`` apart (start to start).

    One cross-family coincidence is shown ("confirmed twice"); only two of them this far apart —
    or the unchanged ``likely`` rule — let an episode past suppression and the on-air floor
    (plan §2.3.4 step 5), so a single shared phantom window cannot list a track on its own.
    """

    if len(agreements) < 2:
        return False
    starts = sorted(start for start, _ in agreements)
    return starts[-1] - starts[0] >= separation_min_ms


def discounted_providers(votes: list[ObservationRecord]) -> frozenset[str]:
    """Return the commercial engines whose trials carry the initial 0.5 dependence prior."""

    counts: dict[str, int] = {}
    for item in votes:
        if item.provider in COMMERCIAL_PROVIDERS:
            counts[item.provider] = counts.get(item.provider, 0) + 1
    if len(counts) <= 1:
        return frozenset()
    ordered = sorted(counts, key=lambda name: (-counts[name], name))
    return frozenset(ordered[1:])


def _independent_trials_e4(votes: list[ObservationRecord]) -> int:
    """Return ``T_ind`` in ten-thousandths (rev 5.2 + the Stage 4c dependence prior).

    Each logical trial contributes one interval — the hull of the supports of its selected
    observations — and greedy interval scheduling (earliest finishing interval first) yields the
    maximum pairwise non-overlapping subset.  Overlapping windows from a dense hop or a rescan
    therefore cannot inflate the tier without new, disjoint evidence.  A trial is attributed by
    family (review L5): it contributes ``0.5`` rather than ``1`` only when *every* engine that
    voted in it is a discounted second commercial engine — a Shazam, local-index or leading
    catalogue vote on the same window makes it a full trial, whatever the other voters were.
    """

    discounted = discounted_providers(votes)
    by_trial: dict[str, tuple[int, int]] = {}
    providers_by_trial: dict[str, set[str]] = {}
    for item in votes:
        start, end = item.support_ms
        current = by_trial.get(item.logical_trial_id)
        by_trial[item.logical_trial_id] = (
            (start, end) if current is None else (min(current[0], start), max(current[1], end))
        )
        providers_by_trial.setdefault(item.logical_trial_id, set()).add(item.provider)
    total = 0
    last_end: int | None = None
    for trial_id, (start, end) in sorted(
        by_trial.items(), key=lambda item: (item[1][1], item[1][0], item[0])
    ):
        if last_end is None or start >= last_end:
            total += (
                DISCOUNTED_TRIAL_E4 if providers_by_trial[trial_id] <= discounted else FULL_TRIAL_E4
            )
            last_end = end
    return total


def _engine_corroborated(
    votes: list[ObservationRecord], *, overlap_min_ms: int = CORROBORATION_OVERLAP_MIN_MS
) -> bool:
    """True when selected votes from two trust families agree on this work at the same moment.

    Two independent engines (e.g. Shazam and AudD) landing on the same track at the same time is
    a strong precision signal — the statistical basis for a paid cross-check.  One such agreement
    is shown ("confirmed twice"); see :func:`separated_agreements` for what it takes to bypass
    suppression and the on-air floor.
    """

    return bool(engine_agreements(votes, overlap_min_ms=overlap_min_ms))


NOVELTY_REGION_PAD_MS = 10_000


def region_request_key(trigger: str, start_ms: int, end_ms: int, policy: RescanPolicy) -> str:
    """Generation-independent identity of a rescan region, used to stop re-requesting it."""

    return compose_natural_key(
        "rescan_request",
        {
            "generation": 0,
            "trigger": trigger,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "policy": policy.model_dump(mode="json"),
        },
    )


def _novelty_regions(
    change_points_ms: tuple[int, ...] | list[int], duration_ms: int
) -> list[tuple[int, int]]:
    """Merge nearby spectral change points into padded, non-overlapping rescan regions."""

    merged: list[tuple[int, int]] = []
    for at_ms in sorted({int(value) for value in change_points_ms}):
        start = max(0, at_ms - NOVELTY_REGION_PAD_MS)
        end = min(duration_ms, at_ms + NOVELTY_REGION_PAD_MS)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


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
    # A compiled tracklist line only counts from a trusted source (uploader/pinned/catalogue site).
    # A position-anchored comment ANSWER/CORRECTION ("ID? -> Artist - Title") counts too: it is only
    # ever used to CORROBORATE, and corroboration additionally requires the answer to resolve to the
    # SAME identity work as the audio match, so an off-base answer simply never lines up and cannot
    # confirm the wrong track.  This is what lets crowd-sourced IDs back up the recogniser.
    trusted_tracklist_line = hint.kind == "tracklist_line" and (
        hint.author.is_uploader or hint.is_pinned or hint.connector in {"mixesdb", "1001tl"}
    )
    positioned_answer = hint.kind in {"answer", "correction"}
    return (
        (trusted_tracklist_line or positioned_answer)
        and hint.mirror_status == "verified"
        and not hint.flags.id_unknown
    )


#: A crowd ID is listed only when its parsed label could be a track name (U-F13): both fields,
#: nothing a question, a link or a handle belongs in, and short enough to be a name rather than
#: the sentence a comment is.  Structural on purpose — real titles start with "Is", "What" and
#: "Who" and run to nine words, and answers are as often "Title - Artist" as the other way round —
#: so neither field's words nor its position decides; only the shape of the whole label does.
CROWD_LABEL_MAX_WORDS = 12
CROWD_LABEL_MAX_CHARS = 120
_CROWD_LINK_OR_HANDLE = re.compile(r"(?i)https?://|www\.|(?<!\w)@\w")


def plausible_crowd_label(artist: str | None, title: str | None) -> bool:
    """Whether a parsed comment answer reads as ``Artist - Title`` rather than comment text."""

    if not artist or not title or not artist.strip() or not title.strip():
        return False
    # A field ending in a colon is a comment lead-in ("FULL TRACK LIST:", "ID:", "Track:"), never
    # an artist or a title — the one shape the length and punctuation rules below let through.
    if artist.strip().endswith(":") or title.strip().endswith(":"):
        return False
    text = f"{artist} {title}"
    if "?" in text or "\n" in text or _CROWD_LINK_OR_HANDLE.search(text):
        return False
    if len(text) > CROWD_LABEL_MAX_CHARS:
        return False
    return len(re.findall(r"\w+", text)) <= CROWD_LABEL_MAX_WORDS


#: One listable crowd answer: the hint, its identity work and that work's candidate.
CrowdAnswer = tuple[HintRecord, str, str]
#: One work named at one timestamp: ``(work_id, candidate_id, its answers in position order)``.
CrowdWork = tuple[str, str, list[HintRecord]]


def crowd_answer_clusters(
    hints: list[HintRecord] | tuple[HintRecord, ...],
    identity: IdentityBuildResult,
    listed_spans: list[tuple[int, int]],
) -> list[list[CrowdAnswer]]:
    """The listable crowd answers in position order, clustered where their position ranges
    intersect — "at one timestamp" — by a transitive sweep, so the clusters are disjoint.

    Listable: a verified, positioned answer or correction with a plausible label whose work the
    identity graph knows and that no listed track's span covers.  Whether the work is already
    listed elsewhere is the caller's to decide, cluster by cluster.
    """

    eligible = [
        hint
        for hint in hints
        if hint.kind in {"answer", "correction"}
        and hint.mirror_status == "verified"
        and not hint.flags.id_unknown
        and hint.position_range_ms is not None
        and hint.position_range_ms[1] > hint.position_range_ms[0]
        and plausible_crowd_label(hint.artist, hint.title)
    ]
    clusters: list[list[CrowdAnswer]] = []
    hull: tuple[int, int] | None = None
    for hint in sorted(eligible, key=lambda item: (item.position_range_ms, item.id)):
        work_id = identity.hint_work_ids.get(hint.id)
        candidate_id = identity.hint_candidates.get(hint.id)
        span = hint.position_range_ms
        if work_id is None or candidate_id is None or span is None:
            continue
        if any(_intersects(span, listed) for listed in listed_spans):
            continue
        if hull is not None and _intersects(span, hull):
            clusters[-1].append((hint, work_id, candidate_id))
            hull = (hull[0], max(hull[1], span[1]))
        else:
            clusters.append([(hint, work_id, candidate_id)])
            hull = span
    return clusters


def rank_crowd_works(cluster: list[CrowdAnswer]) -> list[CrowdWork]:
    """The works one cluster of answers names, best-supported first.

    Support is the number of independent answers (provenance groups) naming the work, then
    whether a trusted voice (the uploader, a pinned comment) named it, then the strongest parse,
    then the earliest position, then the work id — deterministic, and never the opaque hint id
    order that used to pick the listed row (review M4).
    """

    by_work: dict[str, tuple[str, list[HintRecord]]] = {}
    for hint, work_id, candidate_id in cluster:
        by_work.setdefault(work_id, (candidate_id, []))[1].append(hint)

    def support(item: tuple[str, tuple[str, list[HintRecord]]]) -> tuple[int, int, int, int, str]:
        work_id, (_, answers) = item
        return (
            -len({hint.provenance_group for hint in answers}),
            -int(any(hint.author.is_uploader or hint.is_pinned for hint in answers)),
            -max(hint.parse_confidence for hint in answers),
            min(hint.position_range_ms[0] for hint in answers),  # type: ignore[index]
            work_id,
        )

    return [
        (
            work_id,
            candidate_id,
            sorted(answers, key=lambda hint: (hint.position_range_ms, hint.id)),
        )
        for work_id, (candidate_id, answers) in sorted(by_work.items(), key=support)
    ]


def competing_candidate_count(
    supports: list[tuple[int, int]],
    candidate_id: str,
    all_supports: dict[str, list[tuple[int, int]]],
    duration_ms: int,
) -> int:
    """Number of *other* candidates whose evidence support covers >= 50% of ``supports``.

    Shared by the fuser (analyse time) and :mod:`id_detector.calibrate.reconstruct` (fit time) so
    the ``competing`` flag and the ``n_competing_candidates`` feature are computed identically in
    both places, from the per-trial selected supports only (rev 5.2).
    """

    support_length = interval_length(supports, duration_ms)
    if support_length == 0:
        return 0
    return sum(
        other != candidate_id
        and _interval_overlap_ms(supports, other_supports, duration_ms) * 2 >= support_length
        for other, other_supports in all_supports.items()
    )


def _competition(
    supports: list[tuple[int, int]],
    candidate_id: str,
    all_supports: dict[str, list[tuple[int, int]]],
    duration_ms: int,
) -> bool:
    return competing_candidate_count(supports, candidate_id, all_supports, duration_ms) > 0


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


def _certification(profile: str, calibrator: Any | None = None) -> CertificationBlock:
    if calibrator is not None:
        return CertificationBlock(
            profile=profile,
            per=[
                CertificationEntry(
                    dimension=entry.dimension,
                    tier=entry.tier,
                    # A calibration model's certification never propagates while disabled.
                    status=(
                        entry.status
                        if certification_enabled() or entry.status != "certified"
                        else "provisional"
                    ),
                    n_test_predictions=entry.n_test_predictions,
                    lower_bound_e4=entry.lower_bound_e4,
                    test_version=entry.test_version,
                )
                for entry in calibrator.model.certification
            ],
        )
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
    novelty_change_points_ms: tuple[int, ...] | list[int] = (),
    prior_request_keys: frozenset[str] | set[str] | None = None,
    scanned_window_shapes: frozenset[tuple[int, int]] | None = None,
    config: AppConfig | None = None,
    calibrator: Any | None = None,
    overlap_min_ms: int = CORROBORATION_OVERLAP_MIN_MS,
    separation_min_ms: int = CORROBORATION_SEPARATION_MIN_MS,
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
    # Cross-family agreement is read per normalised WORK, not per candidate (plan §2.3.4 step 5):
    # an AudD match carrying an ISRC and a Shazam match carrying its key are two recording
    # components of one work — nothing asserts ``same_recording`` between them — so their selected
    # votes never share a candidate, yet they are the two engines agreeing.  An episode carries
    # the agreements of its work that fall inside its own supports.
    work_votes: dict[str, list[ObservationRecord]] = defaultdict(list)
    for candidate_id, groups in assigned.items():
        for _, votes, _ in groups:
            work_votes[candidate_by_id[candidate_id].work_id].extend(votes)
    work_agreements = {
        work_id: engine_agreements(votes, overlap_min_ms=overlap_min_ms)
        for work_id, votes in work_votes.items()
    }
    # Which plays each eligible hint backs, decided per WORK before any play is judged, so a hint
    # that sits just ahead of its track backs that track's main body and nothing else.
    plays_by_work: dict[str, dict[tuple[str, int], list[tuple[int, int]]]] = defaultdict(dict)
    for candidate_id, groups in sorted(assigned.items()):
        for occurrence_index, (_, votes, _) in enumerate(groups):
            play_supports = normalise_intervals([item.support_ms for item in votes], duration_ms)
            if play_supports:
                plays_by_work[candidate_by_id[candidate_id].work_id][
                    (candidate_id, occurrence_index)
                ] = play_supports
    hints_by_play: dict[tuple[str, int], list[HintRecord]] = defaultdict(list)
    directly_backed: set[tuple[str, int]] = set()
    for hint in hints:
        hint_work_id = identity.hint_work_ids.get(hint.id)
        if (
            hint_work_id is None
            or hint.position_range_ms is None
            or not _eligible_tracklist_hint(hint)
        ):
            continue
        backed = hint_backed_play(
            hint.position_range_ms, plays_by_work.get(hint_work_id, {}), duration_ms
        )
        if backed is not None:
            hints_by_play[backed[0]].append(hint)
            if backed[1]:
                directly_backed.add(backed[0])

    #: Episodes whose every supporting hint is INDIRECT (near the play, on none of its windows).
    indirectly_backed_episodes: set[str] = set()
    #: … and, of those, the ones the audio alone would not have badged ``likely``.
    badge_owed_to_indirect_hint: set[str] = set()
    provisional: list[dict[str, Any]] = []
    for candidate_id, groups in sorted(assigned.items()):
        for occurrence_index, (evidence, votes, alignment) in enumerate(groups):
            raw_supports = [item.support_ms for item in votes]
            supports = normalise_intervals(raw_supports, duration_ms)
            if not supports:
                continue
            start_bound, end_bound, start_censored, end_censored = proved_bounds(raw_supports)
            independent_trials_e4 = _independent_trials_e4(votes)
            candidate_work_id = candidate_by_id[candidate_id].work_id
            supporting_hints = hints_by_play.get((candidate_id, occurrence_index), [])
            hint_vote_e4 = FULL_TRIAL_E4 * int(
                bool({hint.provenance_group for hint in supporting_hints})
            )
            span = supports[-1][1] - supports[0][0]
            n_competing = competing_candidate_count(
                supports, candidate_id, all_supports, duration_ms
            )
            competing = n_competing > 0
            has_global = alignment.has_global_alignment if alignment is not None else False
            if (
                independent_trials_e4 >= 4 * FULL_TRIAL_E4
                and span >= 40_000
                and not competing
                and has_global
            ):
                audio_work_tier = "likely"
            elif independent_trials_e4 >= 2 * FULL_TRIAL_E4 and span >= 20_000 and not competing:
                audio_work_tier = "possible"
            else:
                audio_work_tier = "unclear"
            effective_trials_e4 = independent_trials_e4 + hint_vote_e4
            if (
                effective_trials_e4 >= 4 * FULL_TRIAL_E4
                and span >= 40_000
                and not competing
                and has_global
            ):
                work_tier = "likely"
            elif effective_trials_e4 >= 2 * FULL_TRIAL_E4 and span >= 20_000 and not competing:
                work_tier = "possible"
            else:
                work_tier = "unclear"
            candidate = candidate_by_id[candidate_id]
            recording_supported = (
                candidate_id in identity.recording_supported and not candidate.contested
            )
            version_tier = audio_work_tier if recording_supported else "unclear"
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
                if (candidate_id, occurrence_index) not in directly_backed:
                    indirectly_backed_episodes.add(episode_id)
                    if audio_work_tier != "likely":
                        # Any ``likely`` badge it ends up with is owed to the indirect vote.
                        badge_owed_to_indirect_hint.add(episode_id)
            # One cross-family agreement here is "confirmed twice"; two of them
            # ``separation_min_ms`` apart are what may bypass suppression and the on-air floor
            # (with the ``likely`` rule above left exactly as it is — E-S6).
            agreements = [
                agreement
                for agreement in work_agreements.get(candidate_work_id, [])
                if any(_intersects(agreement, support) for support in supports)
            ]
            if agreements:
                flags.append("engine_corroborated")
            if separated_agreements(agreements, separation_min_ms=separation_min_ms):
                flags.append("engine_corroborated_separated")
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
                    "competing": competing,
                    "n_competing": n_competing,
                    "recording_supported": recording_supported,
                    "version_ids_count": sum(
                        node.split(":", 1)[0] in RECORDING_NAMESPACES
                        for node in candidate.member_nodes
                    ),
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
        scores = {
            "work": _tier_score(work_tier),
            "version": _tier_score(version_tier),
            "boundary": _tier_score(boundary_tier),
        }
        score_kind = "heuristic"
        start_pi = None
        end_pi = None
        best_start_ms = item["start_bound"]
        best_end_ms = item["end_bound"]
        if calibrator is not None:
            calibration = calibrator.apply_episode(
                episode_id=item["id"],
                candidate_id=item["candidate_id"],
                votes=tuple(item["votes"]),
                supporting_hints=tuple(item["supporting_hints"]),
                n_alignment_segments=len(alignment.segments) if alignment is not None else 0,
                max_residual_ms=(
                    max((segment.residual_ms for segment in alignment.segments), default=0)
                    if alignment is not None
                    else 0
                ),
                n_alignment_events=len(alignment.episode_events) if alignment is not None else 0,
                has_global_alignment=item["has_global"],
                span_ms=item["outer_hull"][1] - item["outer_hull"][0],
                support_total_ms=interval_length(item["supports"], duration_ms),
                competing=item["competing"],
                n_competing_candidates=item["n_competing"],
                identity_conflicts=len(item["candidate"].conflicts),
                contested=item["candidate"].contested,
                recording_supported=item["recording_supported"],
                version_ids_count=item["version_ids_count"],
                claim=item["claim"],
                heuristic_work_tier=work_tier,
                heuristic_version_tier=version_tier,
                heuristic_boundary_tier=boundary_tier,
                start_proved_ms=item["start_bound"],
                end_proved_ms=item["end_bound"],
            )
            score_kind = "calibrated"
            work_tier = calibration.tiers.work
            version_tier = calibration.tiers.version
            boundary_tier = calibration.tiers.boundary
            scores = {
                "work": calibration.scores.work,
                "version": calibration.scores.version,
                "boundary": calibration.scores.boundary,
            }
            start_pi = calibration.start_pi
            end_pi = calibration.end_pi
            best_start_ms = calibration.best_start_ms
            best_end_ms = calibration.best_end_ms
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
                start_pi=start_pi,
                end_pi=end_pi,
                best_start_ms=best_start_ms,
                best_end_ms=best_end_ms,
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
                scores=scores,
                score_kind=score_kind,
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
                suppressed=None,
            )
        )

    # Presentation suppression (free false-positive control): flag episodes that should not be
    # LISTED as tracks, from three signals a confident, continuously-played match never trips.  The
    # presentation layer honours EpisodeRecord.suppressed (hidden behind the "hidden matches"
    # toggle — never deleted).  A likely/verified badge, a corroborating hint or two cross-family
    # agreements ``separation_min_ms`` apart make it immune; one agreement alone does not (plan
    # §2.3.4 step 5).
    answer_positions = [
        (hint.position_range_ms, identity.hint_work_ids.get(hint.id))
        for hint in hints
        if hint.kind in {"answer", "correction"}
        and hint.mirror_status == "verified"
        and hint.position_range_ms is not None
    ]

    def _covered_fraction(target: tuple[int, int], spans: list[tuple[int, int]]) -> float:
        lo, hi = target
        if hi <= lo:
            return 0.0
        clipped = normalise_intervals(
            [(max(lo, a), min(hi, b)) for a, b in spans if b > lo and a < hi], duration_ms
        )
        return sum(b - a for a, b in clipped) / (hi - lo)

    def _own_fault(episode: EpisodeRecord) -> str | None:
        """``scatter`` / ``contradicted``: what is wrong with this episode's OWN evidence."""

        span = (episode.evidence_support_ms[0][0], episode.evidence_support_ms[-1][1])
        hull = span[1] - span[0]
        merged = normalise_intervals(
            [tuple(item) for item in episode.evidence_support_ms], duration_ms
        )
        summed = sum(b - a for a, b in merged)
        # scatter: a few matches fused across a span far larger than the time actually matched — a
        # real track plays continuously (hull ≈ summed); this is noise smeared over minutes.
        if hull >= 120_000 and summed > 0 and hull >= 3 * summed:
            return "scatter"
        # contradicted: a trusted comment answer at this position names a DIFFERENT identity work.
        episode_work = candidate_by_id[episode.candidate_id].work_id
        for position, work in answer_positions:
            if work is not None and work != episode_work and _intersects(position, span):
                return "contradicted"
        return None

    def _immune(episode: EpisodeRecord) -> bool:
        """A strong badge, separated engine agreement, or a hint ON one of the matched windows.

        A hint that is only NEAR the play (the lead-in / trail reach) still votes and still keeps a
        short row listed, but it never overrides ``scatter`` or ``contradicted``: those say the
        episode's own evidence is smeared or that a listener named another track right here, and a
        comment a minute away is not proof against either.  That holds whichever way the hint
        would have got there — its flag, or the ``likely`` badge its one extra trial bought.
        """

        if (
            episode.badge in {"likely", "verified"}
            and episode.id not in badge_owed_to_indirect_hint
        ):
            return True
        if "engine_corroborated_separated" in episode.flags:
            return True
        if "hint_supported" not in episode.flags:
            return False
        return episode.id not in indirectly_backed_episodes or _own_fault(episode) is None

    confident_spans = [
        (episode.evidence_support_ms[0][0], episode.evidence_support_ms[-1][1])
        for episode in episode_records
        if _immune(episode)
    ]

    def _suppressed_reason(episode: EpisodeRecord) -> str | None:
        if _immune(episode):
            return None
        fault = _own_fault(episode)
        if fault is not None:
            return fault
        span = (episode.evidence_support_ms[0][0], episode.evidence_support_ms[-1][1])
        # buried: most of this span already sits under OTHER, more-confident/hint-backed tracks.
        others = [item for item in confident_spans if item != span]
        if span[1] > span[0] and _covered_fraction(span, others) >= 0.6:
            return "buried"
        return None

    episode_records = [
        episode.model_copy(update={"suppressed": reason})
        if (reason := _suppressed_reason(episode)) is not None
        else episode
        for episode in episode_records
    ]

    # Hint-only tracks: a confident, position-anchored comment answer whose work no engine matched —
    # and that no listed track already covers — still names the track.  Surface it as a low-
    # confidence "from comments" episode so a crowd ID (e.g. an opener Shazam can't fingerprint that
    # a listener named in the comments) appears on the tracklist instead of an empty gap.  Only a
    # plausible ``Artist - Title`` qualifies (U-F13), the answers are read in position order, and
    # contradictory answers at one timestamp become ONE row — the best-supported work listed, the
    # others as its alternatives — instead of a track per answer (review M4).
    known_work_ids = {
        candidate_by_id[episode.candidate_id].work_id
        for episode in episode_records
        if episode.candidate_id in candidate_by_id
    }
    listed_spans = [
        (episode.evidence_support_ms[0][0], episode.evidence_support_ms[-1][1])
        for episode in episode_records
        if not episode.suppressed
    ]
    hint_only_episodes: list[EpisodeRecord] = []
    for cluster in crowd_answer_clusters(hints, identity, listed_spans):
        # A work already listed — by an engine, or from an earlier timestamp — is not named again.
        ranked = rank_crowd_works([item for item in cluster if item[1] not in known_work_ids])
        if not ranked:
            continue
        winner, *others = ranked
        work_id, candidate_id, winning_hints = winner
        known_work_ids.add(work_id)
        lo = min(hint.position_range_ms[0] for hint in winning_hints)  # type: ignore[index]
        hi = max(hint.position_range_ms[1] for hint in winning_hints)  # type: ignore[index]
        # Defensive only: the clusters are disjoint and each yields at most one row, so no crowd
        # row can land under another (review M4's duplicate is prevented by the clustering, not by
        # this list, which :func:`crowd_answer_clusters` has already read).
        listed_spans.append((lo, hi))
        natural = {
            "candidate_id": candidate_id,
            "occurrence_index": 0,
            "first_support_start_ms": lo,
        }
        hint_only_episodes.append(
            EpisodeRecord(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                id=make_id(media_key, "episode", compose_natural_key("episode", natural)),
                candidate_id=candidate_id,
                # The contradicting answers' works, best-supported first: "could also be".
                alternatives=[other_candidate for _, other_candidate, _ in others],
                claim="component_evidence",
                start_no_later_than_ms=lo,
                end_no_earlier_than_ms=hi,
                evidence_support_ms=[(lo, hi)],
                start_no_earlier_than_ms=None,
                end_no_later_than_ms=None,
                start_pi=None,
                end_pi=None,
                best_start_ms=lo,
                best_end_ms=hi,
                role_segments=[RoleSegment(from_ms=lo, to_ms=hi, role="dominant")],
                occurrence_index=0,
                overlaps=[],
                alignment_segments=[],
                alignment_events=[],
                has_global_alignment=False,
                scores={"work": 2500, "version": 0, "boundary": 0},
                score_kind="heuristic",
                tiers={"work": "possible", "version": "unclear", "boundary": "unclear"},
                badge="possible",
                version_status="unverified",
                evidence=sorted(hint.id for hint in winning_hints),
                rejected_evidence=[],
                # "hint_supported" keeps it listed (it is not a short phantom) and immune to
                # suppression; "hint_only" marks that it has NO audio match — a pure crowd ID.
                flags=["hint_only", "hint_supported"],
                rescan_state="not_requested",
                suppressed=None,
            )
        )
    episode_records = list(episode_records) + hint_only_episodes

    scanned = normalise_intervals([item.support_ms for item in windows], duration_ms)
    # A suppressed episode is hidden from the tracklist, so its span is effectively unidentified:
    # count it as an ID gap, not as coverage, so the coverage stat and the ID gaps match what the
    # timeline actually shows (otherwise a suppressed phantom leaves a visible hole that still reads
    # as "covered, 0 gaps").
    listed_episodes = [episode for episode in episode_records if not episode.suppressed]
    duration_values, duration_intervals = partition_durations(duration_ms, listed_episodes, scanned)
    gaps: list[GapRecord] = []
    for start, end in gap_intervals(duration_intervals["no_evidence_ms"]):
        interval = (start, end)
        prior = [
            episode for episode in listed_episodes if episode.evidence_support_ms[-1][1] <= start
        ]
        following = [
            episode for episode in listed_episodes if episode.evidence_support_ms[0][0] >= end
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
                    n_novelty_events=sum(
                        start <= at_ms <= end for at_ms in set(novelty_change_points_ms)
                    ),
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
        certification=_certification(profile, calibrator),
    )
    input_hashes: dict[str, str] = {}
    requests: list[RescanRequestRecord] = []
    transforms = (
        list(rescan_transforms)
        if rescan_transforms is not None
        else [Transform(type="none", rate_e4=10_000, semitones=0)]
    )
    policies = {
        trigger: policy_for_trigger(trigger, transforms=transforms, config=config)
        for trigger in TRIGGER_PRIORITY
    }
    suppressed = frozenset(prior_request_keys or ())
    scanned = frozenset(scanned_window_shapes or ())
    emitted_keys: set[str] = set()

    # Skip the boundary/verification rescans for episodes the presentation floor will drop anyway:
    # a below-floor, low-confidence match never reaches the tracklist, so refining its edges spends
    # rescan budget for a row nobody sees — and per-episode "edge" requests are the bulk of the
    # gen>=1 window blow-up.  Gap/novelty/hint/question scans stay unconditional: those FIND new
    # tracks, so they protect recall.  A strong badge, a verified version, or a supporting hint
    # keeps an episode eligible regardless of length.  min_track_ms == 0 disables the filter (and
    # therefore this skip), preserving the historical behaviour exactly.
    rescan_min_track_ms = config.present_min_track_ms if config is not None else 0

    def _episode_listed(episode: EpisodeRecord) -> bool:
        if episode.suppressed:
            return False
        if rescan_min_track_ms <= 0:
            return True
        if episode.badge == "likely" or episode.version_status == "verified":
            return True
        if "hint_supported" in episode.flags or "engine_corroborated_separated" in episode.flags:
            return True
        hull = episode.evidence_support_ms
        return hull[-1][1] - hull[0][0] >= rescan_min_track_ms

    def add_request(trigger: str, start: int, end: int) -> None:
        if end <= start:
            return
        policy = policies[trigger]
        key = region_request_key(trigger, start, end, policy)
        if key in emitted_keys:
            # Two episodes can nominate the same edge or gap region. The request is the region,
            # so it is emitted once and its deterministic id stays unique within the plan.
            return
        emitted_keys.add(key)
        natural = {
            "generation": generation,
            "trigger": trigger,
            "start_ms": start,
            "end_ms": end,
            "policy": policy.model_dump(mode="json"),
        }
        if region_request_key(trigger, start, end, policy) in suppressed:
            return
        # A region whose every planned window already exists cannot add an independent support
        # interval, so re-requesting it would spend budget for no new evidence. This is what makes
        # the generation loop converge instead of running to ``max_generations`` every time.
        planned = schedule_rescan_windows(
            start_ms=start, end_ms=end, policy=policy, duration_ms=duration_ms
        )
        if not planned or all((item.start_ms, item.output_ms) in scanned for item in planned):
            return
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
                priority=priority_for_trigger(trigger),
                input_hashes=input_hashes,
            )
        )

    for gap in gaps:
        add_request("gap", gap.start_ms, gap.end_ms)
    for episode in episode_records:
        if not _episode_listed(episode):
            # Below the presentation floor and low-confidence → it won't be listed; don't rescan it.
            continue
        candidate = candidate_by_id[episode.candidate_id]
        if candidate.contested:
            add_request(
                "contested",
                episode.evidence_support_ms[0][0],
                episode.evidence_support_ms[-1][1],
            )
        add_request(
            "edge",
            max(0, episode.best_start_ms - 20_000),
            min(duration_ms, episode.best_start_ms + 20_000),
        )
        hull_start = episode.evidence_support_ms[0][0]
        hull_end = episode.evidence_support_ms[-1][1]
        if hull_end - hull_start > 12 * 60 * 1_000:
            add_request("long_episode", hull_start, hull_end)
        add_request(
            "edge",
            max(0, episode.best_end_ms - 20_000),
            min(duration_ms, episode.best_end_ms + 20_000),
        )
    for cluster_start, cluster_end in _novelty_regions(novelty_change_points_ms, duration_ms):
        add_request("novelty", cluster_start, cluster_end)
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
            )
            index = end + 1
        else:
            index += 1
    return episodes_file, sorted(requests, key=lambda item: (item.start_ms, item.id))


def _jsonl_bytes(records: list[Any]) -> bytes:
    payload = b"\n".join(canonical_json_bytes(item) for item in records)
    return payload + (b"\n" if payload else b"")


def fuse_generation(
    *,
    media_key: str,
    media_dir: Path,
    duration_ms: int,
    observations: list[ObservationRecord] | tuple[ObservationRecord, ...],
    observation_paths: list[Path] | tuple[Path, ...],
    windows: list[WindowRecord] | tuple[WindowRecord, ...],
    window_paths: list[Path] | tuple[Path, ...],
    pcm_path: Path,
    generation: int = 0,
    hints: list[HintRecord] | tuple[HintRecord, ...] = (),
    hints_path: Path | None = None,
    profile: str = "free",
    rescan_transforms: tuple[Transform, ...] | list[Transform] | None = None,
    novelty_change_points_ms: tuple[int, ...] | list[int] = (),
    prior_request_keys: frozenset[str] | set[str] | None = None,
    scanned_window_shapes: frozenset[tuple[int, int]] | None = None,
    config: AppConfig | None = None,
    calibrator: Any | None = None,
    write_final: bool = True,
    overlap_min_ms: int = CORROBORATION_OVERLAP_MIN_MS,
    separation_min_ms: int = CORROBORATION_SEPARATION_MIN_MS,
) -> FusionResult:
    """Fuse the union of every generation's evidence and publish generation ``N``'s artefacts.

    ``observation_paths``/``window_paths`` list **every** input generation, so the completion
    sidecar of ``fuse/episodes.gen<N>.json`` records the hash of each one, as the plan requires.
    ``overlap_min_ms`` / ``separation_min_ms`` are the recipe's corroboration thresholds.
    """

    identity = build_identity_graph(media_key, observations, hints=hints)
    identities_path = write_identity_graph(
        media_dir,
        generation,
        identity,
        observations_path=observation_paths[-1],
        hints_path=hints_path,
    )
    episodes, requests = build_episodes(
        media_key=media_key,
        duration_ms=duration_ms,
        observations=observations,
        windows=windows,
        identity=identity,
        hints=hints,
        generation=generation,
        profile=profile,
        rescan_transforms=rescan_transforms,
        novelty_change_points_ms=novelty_change_points_ms,
        prior_request_keys=prior_request_keys,
        scanned_window_shapes=scanned_window_shapes,
        config=config,
        calibrator=calibrator,
        overlap_min_ms=overlap_min_ms,
        separation_min_ms=separation_min_ms,
    )
    generation_path = media_dir / "fuse" / f"episodes.gen{generation}.json"
    upstream = {
        path.relative_to(media_dir).as_posix(): path for path in (*observation_paths, *window_paths)
    }
    upstream[pcm_path.relative_to(media_dir).as_posix()] = pcm_path
    upstream[identities_path.relative_to(media_dir).as_posix()] = identities_path
    if hints_path is not None:
        upstream[hints_path.relative_to(media_dir).as_posix()] = hints_path
    atomic_write_json(generation_path, episodes)
    write_completion_sidecar(generation_path, upstream)
    final_path = media_dir / "fuse" / "episodes.json"
    if write_final:
        atomic_write_bytes(final_path, canonical_json_bytes(episodes))
        write_completion_sidecar(
            final_path,
            {generation_path.relative_to(media_dir).as_posix(): generation_path},
        )
    input_hashes = {
        path.relative_to(media_dir).as_posix(): sha256_file(path)
        for path in (
            *observation_paths,
            *window_paths,
            identities_path,
            *([hints_path] if hints_path is not None else []),
        )
    }
    requests = [request.model_copy(update={"input_hashes": input_hashes}) for request in requests]
    rescan_path = media_dir / "fuse" / f"rescan_plan.gen{generation}.jsonl"
    atomic_write_bytes(rescan_path, _jsonl_bytes(requests))
    write_completion_sidecar(rescan_path, upstream)
    return FusionResult(
        identities=identity,
        episodes=episodes,
        identities_path=identities_path,
        generation_path=generation_path,
        final_path=final_path,
        rescan_path=rescan_path,
        requests=tuple(requests),
    )


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
    novelty_change_points_ms: tuple[int, ...] | list[int] = (),
    config: AppConfig | None = None,
    calibrator: Any | None = None,
    write_final: bool = True,
    overlap_min_ms: int = CORROBORATION_OVERLAP_MIN_MS,
    separation_min_ms: int = CORROBORATION_SEPARATION_MIN_MS,
) -> FusionResult:
    """Generation-0 convenience wrapper used by the single-generation callers."""

    return fuse_generation(
        media_key=media_key,
        media_dir=media_dir,
        duration_ms=duration_ms,
        observations=observations,
        observation_paths=(observations_path,),
        windows=windows,
        window_paths=(windows_path,),
        pcm_path=pcm_path,
        generation=0,
        hints=hints,
        hints_path=hints_path,
        profile=profile,
        rescan_transforms=rescan_transforms,
        novelty_change_points_ms=novelty_change_points_ms,
        config=config,
        calibrator=calibrator,
        write_final=write_final,
        overlap_min_ms=overlap_min_ms,
        separation_min_ms=separation_min_ms,
    )
