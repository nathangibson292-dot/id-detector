"""Display-layer grouping: collapse runs of competing near-duplicate matches into one row.

A heavily-sampled vocal (the canonical example is Pupa Nas-T / Denise Belfon "Work") makes Shazam
return many *different* official releases of the *same* underlying track across consecutive windows,
so the raw tracklist shows six-plus near-duplicate "Work (X Remix)" rows over ~72 s when it is
really one track.  This module folds such a contiguous cluster into a single **display track**: the
closest match is the primary and the other candidates become "could also be" alternatives.

This is a pure, deterministic *presentation* transform.  It never touches the committed
``fuse/episodes.json``, the fusion contracts, or the certification path — it only regroups the
existing episodes (with their catalogue-resolved labels) for the exports and the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from id_detector.contracts import EpisodeRecord, IdentitiesRecord
from id_detector.present.exports import _ROLE_PRECEDENCE, _candidate_label
from id_detector.semantics import interval_length, normalise_intervals

#: Two episodes join the same display track only when their time spans overlap or sit within this
#: gap.  A clean gap wider than this (or an unrelated track wedged between two appearances) keeps
#: them as separate display tracks — unless they name the *same exact track* (see the bridge below).
DEFAULT_MERGE_GAP_MS = 20_000

#: Two episodes of the SAME exact track (equal full work key) stack into one display track even when
#: they sit further apart than ``DEFAULT_MERGE_GAP_MS`` — up to this bridge (a typical track length,
#: ~3 min) — as long as no *different* confident (≥ ``possible``) track starts between them.  A
#: different track in between means the first play ended, so the two appearances stay separate; a
#: genuine repeat later in the set (beyond the bridge, or across a confident different track) is
#: likewise never folded into the earlier appearance.
DEFAULT_SAME_TRACK_BRIDGE_MS = 180_000

#: Fraction of the shorter proved-support total two episodes must share to count as "competing".
_COMPETE_FRACTION = 0.5

_BADGE_RANK = {"verified": 0, "likely": 1, "possible": 2, "unclear": 3}

#: A different-work episode this confident (badge rank ≤ ``possible``) is a hard separator: it ends
#: any earlier play, so a later reappearance of an earlier track across it is a genuine repeat.
_SEPARATOR_BADGE_RANK = _BADGE_RANK["possible"]

#: Version / remix qualifiers stripped from a title when they appear as standalone words (the
#: bracketed forms — ``(Kevin McKay ViP)``, ``[feat. …]`` — are removed wholesale first).  Stripping
#: only ever applies when at least one content word survives, so a track literally titled "Dub" or
#: "Remix" keeps its identity.
_VERSION_TOKENS = frozenset(
    {
        "remix",
        "rmx",
        "edit",
        "reedit",
        "rework",
        "refix",
        "flip",
        "bootleg",
        "boot",
        "vip",
        "acapella",
        "acappella",
        "accapella",
        "dub",
        "mix",
        "version",
        "ver",
        "extended",
        "radio",
        "club",
        "instrumental",
        "original",
        "mixed",
        "remaster",
        "remastered",
        "live",
        "mashup",
        "rerub",
        "rub",
    }
)

_BRACKETS = re.compile(r"[(\[{][^)\]}]*[)\]}]")
_FEAT = re.compile(r"\b(feat|ft|featuring)\b")
_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    """Casefold, drop bracketed asides and ``feat.`` credits, strip punctuation, then split."""

    lowered = text.casefold()
    lowered = _BRACKETS.sub(" ", lowered)
    lowered = _FEAT.split(lowered)[0]
    lowered = lowered.replace("&", " and ")
    lowered = _NON_WORD.sub(" ", lowered)
    return lowered.split()


def normalise_title(title: str) -> str:
    """Normalised title stem: the underlying-track key with remix/version qualifiers stripped.

    ``"Work (Kevin McKay ViP)"``, ``"Work Dub (feat. Denise Belfon)"`` and
    ``"Work (Full Acapella)"`` all reduce to ``"work"``; ``"Ibiza (Bootleg Version)"`` reduces to
    ``"ibiza"`` and stays a distinct work.
    """

    tokens = _tokens(title)
    content = [token for token in tokens if token not in _VERSION_TOKENS]
    chosen = content or tokens
    return " ".join(chosen)


def normalise_artist(artist: str) -> str:
    """Normalised artist stem: casefolded, ``feat.``/bracket asides gone, punctuation dropped."""

    return " ".join(_tokens(artist))


def work_key(artist: str, title: str) -> str:
    """Full ``artist|title`` identity used to recognise the *same* release across windows."""

    return f"{normalise_artist(artist)}|{normalise_title(title)}"


def _title_tokens(title: str) -> frozenset[str]:
    return frozenset(normalise_title(title).split())


@dataclass(frozen=True)
class DisplayTrack:
    """One collapsed tracklist row: the closest match plus its de-duplicated alternatives."""

    primary: EpisodeRecord
    alternatives: tuple[EpisodeRecord, ...]
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class _Meta:
    title_key: str
    work_key: str
    title_tokens: frozenset[str]
    span: tuple[int, int]
    display_start: int


def _episode_span(episode: EpisodeRecord) -> tuple[int, int]:
    """Time span used for adjacency: the hull of the proved support and the display bounds.

    Single-window episodes carry inverted ``best_start_ms``/``best_end_ms`` (each names a proved
    one-sided bound), so the evidence-support hull is the robust source for "are these adjacent".
    """

    support = episode.evidence_support_ms
    lo = min(episode.best_start_ms, episode.best_end_ms, support[0][0])
    hi = max(episode.best_start_ms, episode.best_end_ms, support[-1][1])
    return lo, hi


def display_start_ms(episode: EpisodeRecord) -> int:
    """Where this episode's row is placed — the role-aware start the flat tracklist already uses."""

    primary = min(
        episode.role_segments,
        key=lambda item: (_ROLE_PRECEDENCE[item.role], item.from_ms, item.to_ms),
        default=None,
    )
    if primary is None or primary.role == "incoming":
        return episode.best_start_ms
    return primary.from_ms


def _primary_sort_key(episode: EpisodeRecord, duration_ms: int) -> tuple:
    """Rank within a group; the minimum is the "closest match" primary.

    Order: highest badge, then the most independent trials and the largest evidence support, then
    the tightest proved bounds (a wider proved-present span pins more of the boundary by evidence),
    then the earliest start, with a deterministic candidate/episode-id tie-break.
    """

    support_total = interval_length(episode.evidence_support_ms, duration_ms)
    proved_span = episode.best_end_ms - episode.best_start_ms
    return (
        _BADGE_RANK.get(episode.badge, 99),
        -len(episode.evidence_support_ms),
        -support_total,
        -proved_span,
        episode.best_start_ms,
        episode.candidate_id,
        episode.id,
    )


def _competes(left: list[tuple[int, int]], right: list[tuple[int, int]], duration_ms: int) -> bool:
    left_len = interval_length(left, duration_ms)
    right_len = interval_length(right, duration_ms)
    if left_len == 0 or right_len == 0:
        return False
    overlap = interval_length(
        [
            (max(a[0], b[0]), min(a[1], b[1]))
            for a in left
            for b in right
            if min(a[1], b[1]) > max(a[0], b[0])
        ],
        duration_ms,
    )
    return overlap >= _COMPETE_FRACTION * min(left_len, right_len)


class _OpenGroup:
    __slots__ = (
        "title_key",
        "work_key",
        "title_tokens",
        "members",
        "max_end",
        "supports",
        "bridge_blocked",
    )

    def __init__(self, meta: _Meta, episode: EpisodeRecord) -> None:
        self.title_key = meta.title_key
        self.work_key = meta.work_key
        self.title_tokens = meta.title_tokens
        self.members: list[EpisodeRecord] = [episode]
        self.max_end = meta.span[1]
        self.supports = list(episode.evidence_support_ms)
        # Set once a *different* confident track starts after this group's latest member: from then
        # on a same-work reappearance is a genuine repeat and must not bridge across the separator.
        self.bridge_blocked = False


def _same_underlying(
    meta: _Meta, group: _OpenGroup, episode: EpisodeRecord, duration_ms: int
) -> bool:
    """Whether ``episode`` names the same underlying track as ``group`` (the merge predicate)."""

    if meta.title_key and meta.title_key == group.title_key:
        return True
    if meta.work_key == group.work_key:
        return True
    # Different titles are clearly distinct works and never merge — unless one title's tokens are a
    # subset of the other's AND their proved intervals heavily compete (e.g. "Work" vs "Work Dub"
    # when the qualifier survived normalisation).
    subset = bool(
        meta.title_tokens
        and group.title_tokens
        and (meta.title_tokens <= group.title_tokens or group.title_tokens <= meta.title_tokens)
    )
    return subset and _competes(
        list(episode.evidence_support_ms),
        normalise_intervals(group.supports, duration_ms),
        duration_ms,
    )


def _choose_target(
    meta: _Meta,
    episode: EpisodeRecord,
    open_groups: list[_OpenGroup],
    duration_ms: int,
    *,
    gap_ms: int,
    same_track_bridge_ms: int,
) -> _OpenGroup | None:
    """Pick the display group ``episode`` extends, or ``None`` to start a new one.

    An adjacency / competing-versions merge (spans overlap or sit within ``gap_ms``) always wins.
    Failing that, an episode naming the SAME exact track (equal non-empty work key) bridges a wider
    gap — up to ``same_track_bridge_ms`` — into an earlier appearance, unless a different confident
    track has intervened (``bridge_blocked``).
    """

    start = meta.span[0]
    bridge_target: _OpenGroup | None = None
    for group in open_groups:
        gap = start - group.max_end
        if gap <= gap_ms and _same_underlying(meta, group, episode, duration_ms):
            return group
        if (
            bridge_target is None
            and meta.work_key
            and meta.work_key == group.work_key
            and not group.bridge_blocked
            and 0 <= gap <= same_track_bridge_ms
        ):
            bridge_target = group
    return bridge_target


def group_display_tracks(
    episodes: list[EpisodeRecord] | tuple[EpisodeRecord, ...],
    identities: IdentitiesRecord,
    duration_ms: int,
    *,
    gap_ms: int = DEFAULT_MERGE_GAP_MS,
    same_track_bridge_ms: int = DEFAULT_SAME_TRACK_BRIDGE_MS,
) -> list[DisplayTrack]:
    """Collapse contiguous runs of same-underlying-track episodes into ordered display tracks.

    Two kinds of merge feed one display track: a contiguous run of competing near-duplicate matches
    (spans overlap or sit within ``gap_ms``), and — added on top — two appearances of the SAME exact
    track (equal work key) bridged across a gap up to ``same_track_bridge_ms`` when no *different*
    confident (≥ ``possible``) track starts between them.  A genuine repeat later in the set (beyond
    the bridge, or across a confident different track) stays its own display track.
    """

    meta: dict[str, _Meta] = {}
    for episode in episodes:
        artist, title = _candidate_label(identities, episode.candidate_id)
        meta[episode.id] = _Meta(
            title_key=normalise_title(title),
            work_key=work_key(artist, title),
            title_tokens=_title_tokens(title),
            span=_episode_span(episode),
            display_start=display_start_ms(episode),
        )

    ordered = sorted(
        episodes, key=lambda ep: (meta[ep.id].span[0], meta[ep.id].display_start, ep.id)
    )
    open_groups: list[_OpenGroup] = []
    finished: list[_OpenGroup] = []
    # Keep a group open until the cursor moves clean past the widest window it could still merge
    # across — the same-track bridge.  (Adjacency and competing-versions merges still require the
    # tighter ``gap_ms``; only an exact same-work bridge reaches out to ``same_track_bridge_ms``.)
    retire_gap = max(gap_ms, same_track_bridge_ms)
    for episode in ordered:
        info = meta[episode.id]
        start = info.span[0]
        # Retire any open group the cursor has moved clean past — this is what keeps a later
        # re-appearance (or an unrelated track wedged between two appearances) from being merged.
        still_open: list[_OpenGroup] = []
        for group in open_groups:
            if start - group.max_end > retire_gap:
                finished.append(group)
            else:
                still_open.append(group)
        open_groups = still_open

        target = _choose_target(
            info,
            episode,
            open_groups,
            duration_ms,
            gap_ms=gap_ms,
            same_track_bridge_ms=same_track_bridge_ms,
        )
        if target is None:
            target = _OpenGroup(info, episode)
            open_groups.append(target)
        else:
            target.members.append(episode)
            target.max_end = max(target.max_end, info.span[1])
            target.supports.extend(episode.evidence_support_ms)

        # A different confident (≥ possible) track ends any earlier play: block every OTHER open
        # group from later bridging a same-work reappearance across this separator.  The group this
        # episode landed in is exempt (it is the same play, not a separator for itself).
        if _BADGE_RANK.get(episode.badge, 99) <= _SEPARATOR_BADGE_RANK:
            for group in open_groups:
                if group is not target:
                    group.bridge_blocked = True

    finished.extend(open_groups)

    tracks: list[DisplayTrack] = []
    for group in finished:
        members = group.members
        primary = min(members, key=lambda ep: _primary_sort_key(ep, duration_ms))
        rest = sorted(
            (ep for ep in members if ep.id != primary.id),
            key=lambda ep: _primary_sort_key(ep, duration_ms),
        )
        seen = {primary.candidate_id}
        alternatives: list[EpisodeRecord] = []
        for candidate in rest:
            if candidate.candidate_id in seen:
                continue
            seen.add(candidate.candidate_id)
            alternatives.append(candidate)
        start_ms = min(meta[ep.id].display_start for ep in members)
        end_ms = max(meta[ep.id].span[1] for ep in members)
        tracks.append(
            DisplayTrack(
                primary=primary,
                alternatives=tuple(alternatives),
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )
    tracks.sort(key=lambda track: (track.start_ms, track.primary.id))
    return tracks
