"""Deep-scan secondary targeting — ``targeting:1`` (plan §2.3.4 step 4).

After the AudD primary sweep, the hints and the first fuse, the Deep recipe sends a bounded number
of Shazam clips to the spans where a second opinion is worth most.  Candidates are the first
fuse's spans ranked by class (``hint_only`` → ``listed_not_confident`` →
``suppressed_challengeable`` → ``blank``); a frozen window is *eligible* for a span when it
overlaps it by at least ``eligibility_min_intersection_ms``.

The run's capacity is ``C = ⌈duration_min × secondary_clips_per_minute⌉`` and a reserve
``R = ⌊secondary_reserve_fraction × C⌋`` is held back for confirmations.  The allocation
``A = C − R`` is spent in two rounds: (i) one window per candidate in priority order (ties by
longer span, then earlier start) until ``A`` is exhausted; (ii) the remainder by largest-remainder
proportional to span duration (ties by earlier start).  Within a span windows rank by intersection
desc, RMS energy desc, start asc; a window already picked for another span is replaced by the
next-ranked window of the same span, and a span with no eligible window left returns its quota to
(ii).  Shazam windows may and should coincide with AudD windows — the only exclusion is a Shazam
window already picked in this run.

The reserve serves *confirmations*: a new identity found in a blank at ``[m₀, m₁]`` queues the
two windows eligible for that blank with starts in ``[m₀ − reserve_search_ms, m₁ +
reserve_search_ms]`` (the probe excluded) that are farthest apart and at least
``reserve_min_separation_ms`` apart — one if only one exists — first-come until ``R`` is
exhausted; later discoveries are listed uncorroborated and unused reserve is distributed by (ii).

Blank spans are the complement of the listed episodes' evidence hulls, not the fuse's ``gaps``
records: those describe ``no_evidence`` stretches *between* observations only, so the tail after a
primary that stopped early would never be probed.
"""

from __future__ import annotations

import wave
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from id_detector.contracts import (
    EpisodeRecord,
    EpisodesFile,
    IdentitiesRecord,
    ObservationRecord,
    WindowRecord,
)
from id_detector.fuse.identity import work_text_key
from id_detector.io import native_path
from id_detector.scan_targeting import CONFIDENT_BADGES, CONFIDENT_FLAGS, Span, _merge

#: The priority classes, highest first (plan §2.3.1 ``secondary_priority``).
PRIORITY: tuple[str, ...] = (
    "hint_only",
    "listed_not_confident",
    "suppressed_challengeable",
    "blank",
)

#: Which allocation step queued a window.
PickRound = Literal["first", "proportional", "confirmation", "reserve_unused"]

#: RMS energy of a frozen window (0..1), the second key of the within-span ranking.
EnergyFn = Callable[[WindowRecord], float]


@dataclass(frozen=True)
class SecondaryCandidate:
    """One span the secondary should probe, with the class that ranks it."""

    priority: str
    span: Span
    episode_id: str | None = None


@dataclass(frozen=True)
class SecondaryPick:
    """A frozen window queued for the secondary, and the span and round that chose it."""

    window: WindowRecord
    candidate: SecondaryCandidate
    round: PickRound


@dataclass(frozen=True)
class Discovery:
    """A new identity a blank probe turned up: the probe, its match and the identity's text key."""

    pick: SecondaryPick
    observation: ObservationRecord
    text_key: str


def secondary_capacity(duration_ms: int, clips_per_minute: int) -> int:
    """``C = ⌈duration_min × clips_per_minute⌉`` — the run's whole Shazam secondary allocation."""

    if duration_ms <= 0 or clips_per_minute <= 0:
        return 0
    return -(-duration_ms * clips_per_minute // 60_000)


def secondary_reserve(capacity: int, reserve_fraction: float) -> int:
    """``R = ⌊reserve_fraction × C⌋`` — the windows held back for confirmations."""

    if capacity <= 0 or reserve_fraction <= 0:
        return 0
    return min(capacity, int(reserve_fraction * capacity))


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
    """Rank the first fuse's spans into the priority classes.

    ``hint_ids`` separates hint records from selected audio votes inside ``episode.evidence``, so
    a suppressed candidate is *challengeable* only with ``suppressed_min_votes`` selected votes.
    ``blank`` spans are :func:`blank_spans`.  Within a class candidates are in start order; the
    allocation orders them again by span length (:func:`allocation_order`).
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


def _span_ms(span: Span) -> int:
    return max(0, span[1] - span[0])


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


def window_rms_energy(path: Path) -> float:
    """The RMS energy of a window's WAV clip, as a fraction of full scale (0..1).

    Energy is only the *second* ranking key, so a clip that cannot be read — pruned, truncated,
    not 16-bit — ranks as 0 and the tie falls through to start order.  It must never raise: the
    secondary runs after the paid primary is already spent, and losing a ranking hint may not
    cost the run.
    """

    try:
        with wave.open(native_path(path), "rb") as clip:
            width = clip.getsampwidth()
            frames = clip.readframes(clip.getnframes())
    except (OSError, wave.Error, EOFError):
        return 0.0
    if width != 2 or len(frames) < 2:
        return 0.0
    samples = np.frombuffer(frames[: len(frames) // 2 * 2], dtype="<i2").astype(np.float64)
    return float(np.sqrt(np.mean(samples * samples)) / 32_768.0)


def energy_reader(media_dir: Path) -> EnergyFn:
    """An :data:`EnergyFn` over the run's window clips, reading each clip at most once."""

    cache: dict[str, float] = {}

    def read(window: WindowRecord) -> float:
        if window.id not in cache:
            cache[window.id] = window_rms_energy(media_dir / window.wav_path)
        return cache[window.id]

    return read


def _no_energy(_window: WindowRecord) -> float:
    return 0.0


def allocation_order(candidates: Iterable[SecondaryCandidate]) -> list[SecondaryCandidate]:
    """Step (i)'s order: priority class, then longer span, then earlier start."""

    return sorted(
        candidates,
        key=lambda item: (
            PRIORITY.index(item.priority),
            -_span_ms(item.span),
            item.span[0],
            item.span[1],
            item.episode_id or "",
        ),
    )


def rank_windows(
    windows: Iterable[WindowRecord],
    span: Span,
    *,
    min_intersection_ms: int,
    energy: EnergyFn | None = None,
) -> list[WindowRecord]:
    """Step (iii): the span's eligible windows by intersection desc, RMS energy desc, start asc."""

    energy = energy or _no_energy
    eligible = [
        window
        for window in windows
        if _intersection_ms(window.support_ms, span) >= min_intersection_ms
    ]
    return sorted(
        eligible,
        key=lambda window: (
            -_intersection_ms(window.support_ms, span),
            -energy(window),
            window.support_ms[0],
            window.id,
        ),
    )


def _take(
    ranked: Sequence[WindowRecord],
    picked: set[str],
    limit: int,
) -> list[WindowRecord]:
    """The first ``limit`` ranked windows not already picked — a duplicate is replaced by the
    next-ranked window of the same span."""

    taken: list[WindowRecord] = []
    for window in ranked:
        if len(taken) >= limit:
            break
        if window.id in picked:
            continue
        picked.add(window.id)
        taken.append(window)
    return taken


def _proportional(
    ordered: Sequence[SecondaryCandidate],
    ranked: Sequence[Sequence[WindowRecord]],
    quota: int,
    picked: set[str],
    round_name: PickRound,
) -> list[SecondaryPick]:
    """Step (ii): ``quota`` windows by largest-remainder proportional to span duration.

    Exact integer arithmetic (``quota × span / total``); the leftover after the floors goes to
    the largest remainders, ties by earlier start.  A span that cannot fill its share (no eligible
    window left) returns the surplus, which is shared out again among the spans that still can.
    """

    picks: list[SecondaryPick] = []
    # A zero-length span carries no proportional weight — and if every remaining span were one,
    # ``total`` would be zero — so it never takes part in (ii).  ``blank_spans`` emits one when
    # its floor is 0, and with no intersection floor every window "intersects" it.
    active = [
        index
        for index in range(len(ordered))
        if _span_ms(ordered[index].span) > 0
        and any(window.id not in picked for window in ranked[index])
    ]
    while quota > 0 and active:
        total = sum(_span_ms(ordered[index].span) for index in active)
        shares = {index: quota * _span_ms(ordered[index].span) for index in active}
        quotas = {index: shares[index] // total for index in active}
        leftover = quota - sum(quotas.values())
        by_remainder = sorted(
            active,
            key=lambda index: (-(shares[index] % total), ordered[index].span[0], index),
        )
        for index in by_remainder[:leftover]:
            quotas[index] += 1
        taken = 0
        for index in active:
            for window in _take(ranked[index], picked, quotas[index]):
                picks.append(SecondaryPick(window, ordered[index], round_name))
                taken += 1
        quota -= taken
        active = [
            index for index in active if any(window.id not in picked for window in ranked[index])
        ]
        if taken == 0:
            break
    return picks


def allocate_secondary_windows(
    windows: Sequence[WindowRecord],
    candidates: Sequence[SecondaryCandidate],
    *,
    allocation: int,
    min_intersection_ms: int,
    energy: EnergyFn | None = None,
    picked: Iterable[str] = (),
) -> tuple[SecondaryPick, ...]:
    """Spend ``A = C − R``: step (i) one window per candidate, then step (ii) proportionally.

    ``picked`` seeds the same-engine exclusion set (windows this run already sent to Shazam).
    """

    if allocation <= 0:
        return ()
    frozen = frozen_windows(windows)
    ordered = allocation_order(candidates)
    ranked = [
        rank_windows(frozen, item.span, min_intersection_ms=min_intersection_ms, energy=energy)
        for item in ordered
    ]
    taken = set(picked)
    picks: list[SecondaryPick] = []
    for index, candidate in enumerate(ordered):
        if len(picks) >= allocation:
            break
        for window in _take(ranked[index], taken, 1):
            picks.append(SecondaryPick(window, candidate, "first"))
    picks.extend(_proportional(ordered, ranked, allocation - len(picks), taken, "proportional"))
    return tuple(picks)


def distribute_secondary_windows(
    windows: Sequence[WindowRecord],
    candidates: Sequence[SecondaryCandidate],
    *,
    quota: int,
    min_intersection_ms: int,
    energy: EnergyFn | None = None,
    picked: Iterable[str] = (),
    round_name: PickRound = "reserve_unused",
) -> tuple[SecondaryPick, ...]:
    """Step (ii) on its own — how the reserve left after the confirmations is spent."""

    if quota <= 0:
        return ()
    frozen = frozen_windows(windows)
    ordered = allocation_order(candidates)
    ranked = [
        rank_windows(frozen, item.span, min_intersection_ms=min_intersection_ms, energy=energy)
        for item in ordered
    ]
    return tuple(_proportional(ordered, ranked, quota, set(picked), round_name))


def confirmation_windows(
    windows: Sequence[WindowRecord],
    discovery: Span,
    *,
    within: Span,
    probe_id: str,
    picked: Iterable[str],
    search_ms: int,
    min_separation_ms: int,
    min_intersection_ms: int,
    limit: int,
) -> tuple[WindowRecord, ...]:
    """The reserve windows that confirm a new identity found in a blank at ``discovery``.

    The windows eligible for the blank span ``within`` whose start lies in
    ``[m₀ − search_ms, m₁ + search_ms]``, the probe and any window already picked excluded; the
    two farthest apart when they are at least ``min_separation_ms`` apart and the reserve
    (``limit``) still holds two, else the earliest one.  Empty when nothing qualifies or the
    reserve is exhausted.
    """

    if limit <= 0:
        return ()
    excluded = set(picked) | {probe_id}
    low, high = discovery[0] - search_ms, discovery[1] + search_ms
    eligible = [
        window
        for window in frozen_windows(windows)
        if low <= window.support_ms[0] <= high
        and window.id not in excluded
        and _intersection_ms(window.support_ms, within) >= min_intersection_ms
    ]
    if not eligible:
        return ()
    first, last = eligible[0], eligible[-1]
    if limit >= 2 and last.support_ms[0] - first.support_ms[0] >= min_separation_ms:
        return (first, last)
    return (first,)


def serve_confirmations(
    windows: Sequence[WindowRecord],
    discoveries: Sequence[tuple[Span, str, Span]],
    *,
    reserve: int,
    picked: Iterable[str],
    search_ms: int,
    min_separation_ms: int,
    min_intersection_ms: int,
) -> tuple[tuple[tuple[WindowRecord, ...], ...], int]:
    """Serve confirmations first-come until the reserve is exhausted.

    ``discoveries`` are ``(discovery_span, probe_window_id, blank_span)`` in the order they came;
    the result pairs each with its confirmation windows (empty once the reserve is gone — that
    discovery is listed uncorroborated) and returns the reserve left over for
    :func:`distribute_secondary_windows`.  Every window served joins the exclusion set for the
    next discovery.
    """

    taken = set(picked)
    served: list[tuple[WindowRecord, ...]] = []
    for span, probe_id, within in discoveries:
        chosen = confirmation_windows(
            windows,
            span,
            within=within,
            probe_id=probe_id,
            picked=taken,
            search_ms=search_ms,
            min_separation_ms=min_separation_ms,
            min_intersection_ms=min_intersection_ms,
            limit=min(2, reserve),
        )
        taken.update(window.id for window in chosen)
        reserve -= len(chosen)
        served.append(chosen)
    return tuple(served), reserve


def listed_text_keys(episodes: EpisodesFile, identities: IdentitiesRecord) -> frozenset[str]:
    """The ``artist|title`` text keys of every identity a listed (non-suppressed) episode names."""

    listed: set[str] = set()
    for episode in episodes.episodes:
        if episode.suppressed:
            continue
        listed.add(episode.candidate_id)
        listed.update(episode.alternatives)
    keys: set[str] = set()
    for candidate in identities.candidates:
        if candidate.canonical_id not in listed:
            continue
        keys.update(
            node.removeprefix("text:")
            for node in candidate.member_nodes
            if node.startswith("text:")
        )
    return frozenset(keys)


def _window_id(observation: ObservationRecord) -> str | None:
    for source_id in observation.source_ids:
        if source_id.startswith("window:"):
            return source_id.removeprefix("window:")
    return None


def new_identity_discoveries(
    observations: Iterable[ObservationRecord],
    picks: Iterable[SecondaryPick],
    known_text_keys: Iterable[str],
) -> tuple[Discovery, ...]:
    """The blank probes that matched an identity no listed episode names, in start order.

    A window picked for a ``blank`` candidate whose match carries a text key outside
    ``known_text_keys`` is a discovery; the same identity found by a second blank probe is not
    counted again.
    """

    by_window = {pick.window.id: pick for pick in picks if pick.candidate.priority == "blank"}
    seen = set(known_text_keys)
    found: list[Discovery] = []
    for observation in sorted(observations, key=lambda item: (item.support_ms, item.id)):
        if observation.status != "match":
            continue
        pick = by_window.get(_window_id(observation) or "")
        if pick is None:
            continue
        key = work_text_key(observation)
        if key is None or key in seen:
            continue
        seen.add(key)
        found.append(Discovery(pick, observation, key))
    return tuple(found)
