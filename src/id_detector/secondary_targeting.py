"""Provisional Deep-scan secondary targeting — ``targeting:0`` (plan §2.3.4 step 4).

After the AudD primary sweep, the hints and the first fuse, the Deep recipe sends a bounded number
of Shazam clips to the spans where a second opinion is worth most.  This provisional scheduler is
deliberately simple: candidates are ranked by class (``hint_only`` → ``listed_not_confident`` →
``suppressed_challengeable`` → ``blank``), every frozen window that overlaps a candidate span by at
least ``eligibility_min_intersection_ms`` is eligible, and the queue is filled in priority order
then start order up to ``C = ⌈duration_min × secondary_clips_per_minute⌉``.  There is no reserve
and no per-candidate quota; the full allocation (``targeting:1``, plan 1b-i) replaces it under a
new ``algorithm_version``.  Shazam windows may and should coincide with AudD windows — the only
exclusion is a window already queued in this run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from id_detector.contracts import EpisodeRecord, EpisodesFile, WindowRecord
from id_detector.scan_targeting import CONFIDENT_BADGES, CONFIDENT_FLAGS, Span, _merge

#: The provisional priority classes, highest first (plan §2.3.1 ``secondary_priority``).
PRIORITY: tuple[str, ...] = (
    "hint_only",
    "listed_not_confident",
    "suppressed_challengeable",
    "blank",
)


@dataclass(frozen=True)
class SecondaryCandidate:
    """One span the secondary should probe, with the class that ranks it."""

    priority: str
    span: Span
    episode_id: str | None = None


def secondary_capacity(duration_ms: int, clips_per_minute: int) -> int:
    """``C = ⌈duration_min × clips_per_minute⌉`` — the run's whole Shazam secondary allocation."""

    if duration_ms <= 0 or clips_per_minute <= 0:
        return 0
    return -(-duration_ms * clips_per_minute // 60_000)


def _episode_span(episode: EpisodeRecord) -> Span:
    """The span an episode's evidence actually covers (first support start to last support end).

    ``best_start_ms``/``best_end_ms`` are the *estimated* boundaries and cross for a short episode
    whose end is proved before its start; the evidence hull is what the engines really heard.
    """

    supports = episode.evidence_support_ms
    if supports:
        return (min(start for start, _ in supports), max(end for _, end in supports))
    return (episode.best_start_ms, max(episode.best_start_ms, episode.best_end_ms))


def _is_confident(episode: EpisodeRecord) -> bool:
    return episode.badge in CONFIDENT_BADGES or any(
        flag in CONFIDENT_FLAGS for flag in episode.flags
    )


def blank_spans(episodes: EpisodesFile, duration_ms: int, *, min_ms: int) -> list[Span]:
    """The mix not covered by any listed episode's evidence — no match of any confidence.

    Computed from the episodes rather than the fuse's gap records: those only describe
    ``no_evidence`` stretches *between* observations, so the tail after a primary that stopped
    early (an unresolved boundary) would never be probed.  Spans shorter than ``min_ms`` cannot
    make a window eligible and are dropped.
    """

    covered = _merge(
        [_episode_span(episode) for episode in episodes.episodes if not episode.suppressed]
    )
    blanks: list[Span] = []
    cursor = 0
    for start, end in covered:
        if start - cursor >= min_ms:
            blanks.append((cursor, start))
        cursor = max(cursor, end)
    if duration_ms - cursor >= min_ms:
        blanks.append((cursor, duration_ms))
    return blanks


def select_secondary_candidates(
    episodes: EpisodesFile,
    *,
    duration_ms: int,
    suppressed_min_votes: int,
    min_intersection_ms: int,
    hint_ids: Iterable[str] = (),
) -> tuple[SecondaryCandidate, ...]:
    """Rank the first fuse's spans into the provisional priority classes.

    ``hint_ids`` separates hint records from selected audio votes inside ``episode.evidence``, so
    a suppressed candidate is *challengeable* only with ``suppressed_min_votes`` selected votes.
    ``blank`` spans are :func:`blank_spans`.  Within a class candidates are in start order.
    """

    hints = frozenset(hint_ids)
    ranked: dict[str, list[SecondaryCandidate]] = {name: [] for name in PRIORITY}
    for episode in episodes.episodes:
        span = _episode_span(episode)
        if span[1] <= span[0]:
            continue
        if episode.suppressed:
            votes = sum(1 for item in episode.evidence if item not in hints)
            if votes >= suppressed_min_votes:
                ranked["suppressed_challengeable"].append(
                    SecondaryCandidate("suppressed_challengeable", span, episode.id)
                )
            continue
        if "hint_only" in episode.flags:
            ranked["hint_only"].append(SecondaryCandidate("hint_only", span, episode.id))
        elif not _is_confident(episode):
            ranked["listed_not_confident"].append(
                SecondaryCandidate("listed_not_confident", span, episode.id)
            )
    for span in blank_spans(episodes, duration_ms, min_ms=min_intersection_ms):
        ranked["blank"].append(SecondaryCandidate("blank", span))
    ordered: list[SecondaryCandidate] = []
    for name in PRIORITY:
        ordered.extend(sorted(ranked[name], key=lambda item: (item.span, item.episode_id or "")))
    return tuple(ordered)


def _intersection_ms(left: Span, right: Span) -> int:
    return max(0, min(left[1], right[1]) - max(left[0], right[0]))


def frozen_windows(windows: Iterable[WindowRecord]) -> list[WindowRecord]:
    """The generation-0 untransformed windows in start order — the set every recipe counts."""

    return sorted(
        (
            window
            for window in windows
            if window.generation == 0 and window.transform.type == "none"
        ),
        key=lambda window: (window.support_ms[0], window.id),
    )


def schedule_secondary_windows(
    windows: Sequence[WindowRecord],
    candidates: Sequence[SecondaryCandidate],
    *,
    capacity: int,
    min_intersection_ms: int,
) -> tuple[WindowRecord, ...]:
    """Queue eligible frozen windows in priority order then start order, up to ``capacity``.

    A window eligible for several candidates is queued once, at its highest class.
    """

    if capacity <= 0:
        return ()
    frozen = frozen_windows(windows)
    queued: list[WindowRecord] = []
    seen: set[str] = set()
    for name in PRIORITY:
        spans = [item.span for item in candidates if item.priority == name]
        if not spans:
            continue
        for window in frozen:
            if window.id in seen:
                continue
            if any(
                _intersection_ms(window.support_ms, span) >= min_intersection_ms for span in spans
            ):
                seen.add(window.id)
                queued.append(window)
                if len(queued) >= capacity:
                    return tuple(queued)
    return tuple(queued)
