"""Conventional flattened JSON and Markdown tracklist exports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from id_detector.contracts import (
    AcquireEpisode,
    AcquireFile,
    EpisodeRecord,
    EpisodesFile,
    IdentitiesRecord,
)
from id_detector.fuse.episodes import plausible_crowd_label
from id_detector.io import atomic_write_bytes, atomic_write_json, write_completion_sidecar
from id_detector.semantics import interval_length, subtract_intervals

_ROLE_PRECEDENCE = {
    "incoming": 0,
    "dominant": 1,
    "outgoing": 2,
    "layer": 3,
    "component": 4,
    "uncertain": 5,
}


@dataclass(frozen=True)
class ExportResult:
    json_path: Path
    markdown_path: Path
    entries: tuple[dict[str, Any], ...]
    cue_path: Path | None = None


class ProjectionEntry(TypedDict):
    """One ordered presentation row, with every consumer-facing field made explicit."""

    kind: Literal["track", "id"]
    identity: str
    display_label: str
    tier: str | None
    start_ms: int
    end_ms: int
    hidden_reason: str | None
    episode_id: str | None
    candidate_id: str | None
    acquire: dict[str, Any] | None
    gap_id: NotRequired[str]
    artist: NotRequired[str]
    title: NotRequired[str]


@dataclass(frozen=True)
class CanonicalProjection:
    """The immutable, ordered result-page projection for one frozen run.

    Every presentation surface — page rows, hero tiles, timeline lanes, Copy, CUE, Markdown, JSON
    and the library card — reads this one object, so an analysis can only ever have ONE tracklist.
    ``covered_ms`` is the hero's honest-coverage numerator: the run's evidence-backed (plus
    calibrated predicted) listening time with the time proved ONLY by hidden rows removed, so the
    percentage can never describe tracks the tracklist does not list.

    "Immutable" is enforced, not merely promised: ``frozen=True`` protects the tuple, but the rows
    inside it are plain dicts, and one projection object is handed to the page, the exports and the
    library card in turn.  A surface that edited a row in place would hand the next surface a
    different tracklist — the exact defect this class exists to make impossible — so the row
    accessors hand out copies and the stored rows are never exposed directly.
    """

    _entries: tuple[ProjectionEntry, ...]
    covered_ms: int

    @property
    def entries(self) -> tuple[ProjectionEntry, ...]:
        """Every ordered row, hidden ones included (the page tucks those behind its reveal)."""

        return tuple(ProjectionEntry(**entry) for entry in self._entries)  # type: ignore[typeddict-item]

    @property
    def shown_entries(self) -> tuple[ProjectionEntry, ...]:
        """The one filtered tracklist: what the page lists and every export writes."""

        return tuple(
            ProjectionEntry(**entry)  # type: ignore[typeddict-item]
            for entry in self._entries
            if entry["hidden_reason"] is None
        )

    @property
    def suppressed_count(self) -> int:
        return sum(1 for entry in self._entries if entry["hidden_reason"] is not None)

    @property
    def gap_count(self) -> int:
        """ID gaps as the tracklist shows them (gaps are never hidden, but never assume it)."""

        return sum(
            1 for entry in self._entries if entry["kind"] == "id" and entry["hidden_reason"] is None
        )


def _format_time(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _cue_index(milliseconds: int) -> str:
    """Format a millisecond position as CUE ``MM:SS:FF`` (75 frames per second).

    Minutes are allowed to exceed two digits so a set longer than 99 minutes still flattens to a
    monotonic sheet; every mainstream CUE reader we target tolerates a wider minute field.
    """

    frames_total = round(milliseconds * 75 / 1000)
    minutes, remainder = divmod(frames_total, 60 * 75)
    seconds, frames = divmod(remainder, 75)
    return f"{minutes:02d}:{seconds:02d}:{frames:02d}"


def _cue_quote(value: str) -> str:
    """Quote a CUE string field, stripping the double quote CUE cannot escape."""

    return '"' + value.replace('"', "'").replace("\n", " ").replace("\r", " ") + '"'


def _split_label(label: str) -> tuple[str, str]:
    if " - " not in label:
        return "Unknown artist", label
    artist, title = label.split(" - ", 1)
    return artist, title


def _candidate_label(identities: IdentitiesRecord, candidate_id: str) -> tuple[str, str]:
    candidate = next(item for item in identities.candidates if item.canonical_id == candidate_id)
    labels = [
        node.label
        for node in identities.nodes
        if node.id in candidate.member_nodes and node.ns != "text"
    ]
    if not labels:
        # A text-only candidate (a crowd ID, an engine match with no catalogue id) is labelled
        # from the whole work, whose text nodes include tracklist and comment lines: one may be a
        # lead-in ("FULL TRACK LIST: - …") or carry an @handle rather than a name.  Prefer a label
        # that reads as a track (U-F13); only if none does is the bare minimum shown.
        work = next(item for item in identities.works if item.work_id == candidate.work_id)
        labels = [node.label for node in identities.nodes if node.id in work.member_nodes]
        labels = [item for item in labels if plausible_crowd_label(*_split_label(item))] or labels
    return _split_label(min(labels) if labels else "Unknown artist - Unknown title")


def _display_start(episode: EpisodeRecord) -> int:
    """Role-aware row placement: an ``incoming`` episode starts at ``best_start_ms`` (its mix-in),
    any other primary role starts at that role segment's ``from_ms``."""

    primary = min(
        episode.role_segments,
        key=lambda item: (_ROLE_PRECEDENCE[item.role], item.from_ms, item.to_ms),
        default=None,
    )
    if primary is None or primary.role == "incoming":
        return episode.best_start_ms
    return primary.from_ms


def _support_ms(intervals: list[tuple[int, int]] | list[list[int]]) -> int:
    """Summed proved support: the union length of evidence intervals (overlaps merged).

    This counts only the seconds an engine actually matched — never the unproven time between two
    detections — so it is the honest "how long was this heard" figure.  Adjacent windows that
    overlap are merged rather than double-counted.
    """

    total = 0
    current_lo: int | None = None
    current_hi = 0
    for lo, hi in sorted((int(span[0]), int(span[1])) for span in intervals):
        if current_lo is None or lo > current_hi:
            if current_lo is not None:
                total += max(0, current_hi - current_lo)
            current_lo, current_hi = lo, hi
        else:
            current_hi = max(current_hi, hi)
    if current_lo is not None:
        total += max(0, current_hi - current_lo)
    return total


def _on_air_ms(episode: EpisodeRecord) -> int:
    """Proved on-air time of one episode: its summed evidence support (see ``_support_ms``)."""

    return _support_ms(list(episode.evidence_support_ms))


#: Badges that keep a short match listed regardless of how briefly it played.
_KEEP_SHORT_BADGES = frozenset({"likely", "verified"})


def short_track(entry: dict[str, Any], min_track_ms: int) -> bool:
    """Whether a flattened track row played too briefly to be listed as a track.

    On-air duration cleanly separates real tracks from false positives (most false positives are a
    single 12 s window), so a ``kind == "track"`` row whose summed proved support is under
    ``min_track_ms`` is treated as short — UNLESS its badge is ``likely``/``verified``, a text
    hint supports it, or two trust families agreed on it at two moments far enough apart
    (``engine_corroborated_separated``; one shared window is "confirmed twice" but still short —
    plan §2.3.4 step 5).  ``0`` disables the rule; ID gaps are never short.
    """

    if min_track_ms <= 0 or entry.get("kind") != "track":
        return False
    if entry.get("badge") in _KEEP_SHORT_BADGES or entry.get("hint_supported"):
        return False
    if entry.get("engine_corroborated_separated"):  # two families agree twice, well apart
        return False
    return int(entry.get("on_air_ms") or 0) < min_track_ms


def hidden_reason(entry: dict[str, Any], min_track_ms: int) -> str | None:
    """Why a flattened track row is left out of the exports and tucked away on the page, or
    ``None`` when it is listed normally.

    Two sources: a fusion-side ``suppressed`` reason on the episode (a short lowercase token such as
    ``buried`` — set when a more confident or hint-supported track covers the same time, a trusted
    comment contradicts it, or its detections are scattered) and the on-air floor (``"short"``,
    see :func:`short_track`).  ID gaps are never hidden.
    """

    if entry.get("kind") != "track":
        return None
    suppressed = entry.get("suppressed")
    if suppressed:
        return str(suppressed)
    return "short" if short_track(entry, min_track_ms) else None


def _acquire_summary(episode: AcquireEpisode) -> dict[str, Any]:
    classification = episode.soundcloud.classification if episode.soundcloud else "none"
    free_download = classification == "free_download_native"
    gate = classification == "gate_link"
    buy = classification == "buy_link" or any(link.kind == "purchase" for link in episode.direct)
    return {
        "free_download": free_download,
        "gate": gate,
        "buy": buy,
        "search": True,
        "version_status": episode.version_status,
        "direct": [link.model_dump(mode="json") for link in episode.direct],
        "search_links": [link.model_dump(mode="json") for link in episode.search],
        "soundcloud": (episode.soundcloud.model_dump(mode="json") if episode.soundcloud else None),
    }


def _track_entry(
    episode: EpisodeRecord,
    identities: IdentitiesRecord,
    acquire_by_episode: dict[str, AcquireEpisode],
    label_by_episode: dict[str, str],
) -> dict[str, Any]:
    """The one-episode tracklist row shared by the collapsed and ungrouped views."""

    artist, title = _candidate_label(identities, episode.candidate_id)
    overlap_labels = sorted(
        {label_by_episode[other] for other in episode.overlaps if other in label_by_episode}
    )
    has_layer = any(segment.role == "layer" for segment in episode.role_segments)
    primary = min(
        episode.role_segments,
        key=lambda item: (_ROLE_PRECEDENCE[item.role], item.from_ms, item.to_ms),
        default=None,
    )
    primary_role = primary.role if primary is not None else "uncertain"
    start_ms = (
        episode.best_start_ms if primary_role == "incoming" or primary is None else primary.from_ms
    )
    acquire_episode = acquire_by_episode.get(episode.id)
    # A crowd row's ``alternatives`` are the contradicting comment answers at the same timestamp
    # (fusion lists the best-supported one and carries the others as candidate ids).
    crowd_alternatives = (
        [_crowd_alternative_summary(other, identities, episode) for other in episode.alternatives]
        if "hint_only" in episode.flags
        else []
    )
    return {
        "kind": "track",
        "identity": episode.id,
        "display_label": f"{artist} — {title}",
        "tier": episode.badge,
        "start_ms": start_ms,
        "end_ms": episode.best_end_ms,
        "hidden_reason": None,
        "episode_id": episode.id,
        "candidate_id": episode.candidate_id,
        "artist": artist,
        "title": title,
        "occurrence_index": episode.occurrence_index,
        "primary_role": primary_role,
        "overlap_labels": overlap_labels,
        "has_layer": has_layer,
        "badge": episode.badge,
        "version_status": episode.version_status,
        "hint_supported": "hint_supported" in episode.flags,
        # Two trust families (e.g. Shazam + AudD) matched this track at the same time: shown as
        # "confirmed twice".
        "engine_corroborated": "engine_corroborated" in episode.flags,
        # ...and did so at two moments far enough apart — the one cross-engine signal that keeps
        # a short row listed (see ``short_track``).
        "engine_corroborated_separated": "engine_corroborated_separated" in episode.flags,
        # A crowd ID: named by a confident comment answer, but no engine matched the audio.
        "hint_only": "hint_only" in episode.flags,
        "on_air_ms": _on_air_ms(episode),
        # Fusion may mark an episode as suppressed (a short reason token); read leniently so
        # the page/exports work with episodes written before the field existed.
        "suppressed": getattr(episode, "suppressed", None) or None,
        "n_rejected_hypotheses": len(episode.rejected_evidence),
        "tiers": episode.tiers.model_dump(mode="json"),
        "acquire": _acquire_summary(acquire_episode) if acquire_episode is not None else None,
        "alternatives": crowd_alternatives,
        "also_count": len(crowd_alternatives),
    }


def _alternative_summary(episode: EpisodeRecord, identities: IdentitiesRecord) -> dict[str, Any]:
    artist, title = _candidate_label(identities, episode.candidate_id)
    return {
        "badge": episode.badge,
        "version_status": episode.version_status,
        "artist": artist,
        "title": title,
        "track": f"{artist} — {title}",
        "candidate_id": episode.candidate_id,
        "episode_id": episode.id,
        "start_ms": _display_start(episode),
    }


def _crowd_alternative_summary(
    candidate_id: str, identities: IdentitiesRecord, episode: EpisodeRecord
) -> dict[str, Any]:
    """A contradicting comment answer at the listed crowd row's timestamp: the same row shape as
    a folded-in version, carrying the crowd row's own (comment-only) confidence."""

    artist, title = _candidate_label(identities, candidate_id)
    return {
        "badge": episode.badge,
        "version_status": "unverified",
        "artist": artist,
        "title": title,
        "track": f"{artist} — {title}",
        "candidate_id": candidate_id,
        "episode_id": episode.id,
        "start_ms": _display_start(episode),
    }


def _derive_projection_entries(
    episodes: EpisodesFile,
    identities: IdentitiesRecord,
    acquire: AcquireFile | None = None,
    *,
    collapse: bool = True,
    same_track_bridge_ms: int | None = None,
    min_track_ms: int = 0,
) -> tuple[tuple[ProjectionEntry, ...], dict[str, list[tuple[int, int]]]]:
    """Derive all presentation rows and apply suppression exactly once.

    With ``collapse`` (the default), a contiguous run of competing near-duplicate matches of the
    same underlying track becomes ONE row: the closest match is shown and the others ride along as
    ``alternatives``.  Two appearances of the SAME exact track up to ``same_track_bridge_ms`` apart
    (``None`` → the grouping default) with no different confident track between them likewise stack
    into one row.  ``collapse=False`` restores the historical one-row-per-episode view.

    ``min_track_ms`` (default ``0`` = off) marks track rows that played too briefly to be a real
    track, as well as any row fusion marked ``suppressed``.  Rows remain in this canonical list;
    :class:`CanonicalProjection` exposes the one shared shown subset.

    The two suppression sources are applied at the two different moments they belong to.  A
    fusion-side ``suppressed`` verdict is about ONE match's identity, so it is applied BEFORE
    collapsing: a suppressed episode never joins a display group, so it can neither ride into a
    shown row as a silent "could also be" alternative nor win primary selection on badge and drag
    an honest group into hiding.  It becomes its own hidden row instead.  The on-air floor is a
    property of the COLLAPSED row (two short appearances of one track legitimately stack past the
    floor), so it is applied after grouping.

    Returns the ordered rows and, beside them, the proved-evidence spans each row stands on —
    what the hero's honest-coverage figure has to subtract when a row is hidden.
    """

    acquire_by_episode = (
        {item.episode_id: item for item in acquire.episodes} if acquire is not None else {}
    )
    suppressed_episodes = [
        episode for episode in episodes.episodes if getattr(episode, "suppressed", None)
    ]
    live_episodes = [
        episode for episode in episodes.episodes if not getattr(episode, "suppressed", None)
    ]
    # Overlap notes name other episodes by label and reach the CUE: only live identities may.
    label_by_episode = {
        episode.id: " - ".join(_candidate_label(identities, episode.candidate_id))
        for episode in live_episodes
    }
    entries: list[dict[str, Any]] = []
    spans: dict[str, list[tuple[int, int]]] = {}
    if collapse:
        from id_detector.present.grouping import (
            DEFAULT_SAME_TRACK_BRIDGE_MS,
            group_display_tracks,
        )

        bridge_ms = (
            DEFAULT_SAME_TRACK_BRIDGE_MS if same_track_bridge_ms is None else same_track_bridge_ms
        )
        duration_ms = 0
        for episode in episodes.episodes:
            duration_ms = max(
                duration_ms, episode.best_end_ms, *(s[1] for s in episode.evidence_support_ms)
            )
        for track in group_display_tracks(
            live_episodes, identities, duration_ms, same_track_bridge_ms=bridge_ms
        ):
            entry = _track_entry(track.primary, identities, acquire_by_episode, label_by_episode)
            entry["start_ms"] = track.start_ms
            entry["end_ms"] = track.end_ms
            member_spans = [
                (int(span[0]), int(span[1]))
                for member in (track.primary, *track.alternatives)
                for span in member.evidence_support_ms
            ]
            spans[entry["identity"]] = member_spans
            entry["on_air_ms"] = _support_ms(member_spans)
            # A crowd row keeps the contradicting answers it already carries; the folded-in
            # versions follow them.
            alternatives = list(entry["alternatives"])
            seen = {alt["candidate_id"] for alt in alternatives}
            for alt in track.alternatives:
                if alt.candidate_id not in seen:
                    seen.add(alt.candidate_id)
                    alternatives.append(_alternative_summary(alt, identities))
            entry["alternatives"] = alternatives
            entry["also_count"] = len(alternatives)
            # The folded-in versions are already listed as alternatives, so drop them from the
            # primary's overlap note to keep the CUE REM lines about genuinely co-sounding tracks.
            folded = {alt["track"].replace(" — ", " - ") for alt in alternatives}
            entry["overlap_labels"] = [
                label for label in entry["overlap_labels"] if label not in folded
            ]
            entries.append(entry)
    else:
        for episode in live_episodes:
            entry = _track_entry(episode, identities, acquire_by_episode, label_by_episode)
            spans[entry["identity"]] = [
                (int(span[0]), int(span[1])) for span in episode.evidence_support_ms
            ]
            entries.append(entry)
    # Every suppressed match keeps its own row so the page's reveal and the hidden count still see
    # it — but as a row of its own, never folded into someone else's.
    for episode in suppressed_episodes:
        entry = _track_entry(episode, identities, acquire_by_episode, label_by_episode)
        spans[entry["identity"]] = [
            (int(span[0]), int(span[1])) for span in episode.evidence_support_ms
        ]
        entries.append(entry)
    entries.extend(
        {
            "kind": "id",
            "identity": gap.id,
            "display_label": "ID",
            "tier": None,
            "start_ms": gap.start_ms,
            "end_ms": gap.end_ms,
            "hidden_reason": None,
            "episode_id": None,
            "candidate_id": None,
            "acquire": None,
            "gap_id": gap.id,
            "label": "ID",
            "reason": gap.reason,
        }
        for gap in episodes.gaps
    )
    ordered = tuple(
        sorted(
            entries,
            key=lambda item: (
                item["start_ms"],
                0 if item["kind"] == "track" else 1,
                item.get("episode_id", item.get("gap_id", "")),
            ),
        )
    )
    judged = [dict(entry, hidden_reason=hidden_reason(entry, min_track_ms)) for entry in ordered]
    # A hidden identity must not survive as a shown row's overlap note either — that note is what
    # the CUE prints as its ``REM`` lines, and it would be the fourth tracklist all over again.
    hidden_labels = {
        str(entry["display_label"]).replace(" — ", " - ")
        for entry in judged
        if entry["hidden_reason"] is not None
    }
    for entry in judged:
        if entry["hidden_reason"] is None and entry.get("overlap_labels"):
            entry["overlap_labels"] = [
                label for label in entry["overlap_labels"] if label not in hidden_labels
            ]
    return tuple(ProjectionEntry(entry) for entry in judged), spans  # type: ignore[typeddict-item]


def build_projection(
    episodes: EpisodesFile,
    identities: IdentitiesRecord,
    acquire: AcquireFile | None = None,
    *,
    collapse: bool = True,
    same_track_bridge_ms: int | None = None,
    min_track_ms: int = 0,
) -> CanonicalProjection:
    """Build the single typed projection consumed by the page, exports and library card."""

    entries, spans = _derive_projection_entries(
        episodes,
        identities,
        acquire,
        collapse=collapse,
        same_track_bridge_ms=same_track_bridge_ms,
        min_track_ms=min_track_ms,
    )
    return CanonicalProjection(entries, _covered_ms(episodes, entries, spans))


def _covered_ms(
    episodes: EpisodesFile,
    entries: tuple[ProjectionEntry, ...],
    spans: dict[str, list[tuple[int, int]]],
) -> int:
    """Evidence-backed listening time the SHOWN rows account for.

    The fused partition's evidence-supported (plus calibrated predicted) time is the honest
    numerator for "of the set identified" — but it covers every fused episode, including the ones
    this projection hides.  Time proved only by a hidden row is removed, so the hero can never
    claim more of the set than the tracklist under it lists.  Time a hidden row merely shares with
    a shown one still counts: the shown row proves it.
    """

    durations = episodes.durations
    base = int(durations.evidence_supported_ms) + int(durations.predicted_episode_ms)
    limit = max(
        [span[1] for value in spans.values() for span in value]
        + [int(entry["end_ms"]) for entry in entries]
        + [base, 0]
    )
    hidden = [
        span
        for entry in entries
        if entry["hidden_reason"] is not None
        for span in spans.get(entry["identity"], ())
    ]
    if not hidden:
        return base
    shown = [
        span
        for entry in entries
        if entry["hidden_reason"] is None
        for span in spans.get(entry["identity"], ())
    ]
    hidden_only = interval_length(subtract_intervals(hidden, shown, limit), limit)
    return max(0, base - hidden_only)


def flatten_tracklist(
    episodes: EpisodesFile,
    identities: IdentitiesRecord,
    acquire: AcquireFile | None = None,
    *,
    collapse: bool = True,
    same_track_bridge_ms: int | None = None,
    min_track_ms: int = 0,
    include_hidden: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Compatibility wrapper around :func:`build_projection`."""

    projection = build_projection(
        episodes,
        identities,
        acquire,
        collapse=collapse,
        same_track_bridge_ms=same_track_bridge_ms,
        min_track_ms=min_track_ms,
    )
    entries = projection.entries if include_hidden else projection.shown_entries
    return tuple(dict(entry) for entry in entries)


def _acquire_cell(entry: dict[str, Any], key: str) -> str:
    acquire = entry.get("acquire")
    if not acquire:
        return "—"
    return "yes" if acquire.get(key) else "—"


def export_tracklist(
    *,
    media_dir: Path,
    output_dir: Path | None = None,
    media_key: str,
    duration_ms: int,
    episodes: EpisodesFile,
    identities: IdentitiesRecord,
    episodes_path: Path,
    identities_path: Path,
    acquire: AcquireFile | None = None,
    acquire_path: Path | None = None,
    title: str | None = None,
    collapse: bool = True,
    same_track_bridge_ms: int | None = None,
    min_track_ms: int = 0,
    status: str | None = None,
    reason: str | None = None,
    achieved: str | None = None,
    projection: CanonicalProjection | None = None,
) -> ExportResult:
    """Write the flattened exports.

    ``status`` / ``reason`` / ``achieved`` are the run's plan §2.3.5 outcome (``complete``,
    ``degraded`` or ``partial``, why, and which recipe produced it); a re-export that does not know
    them (``acquire``, the benchmark) carries the previous values forward or leaves them null.
    """

    projection = projection or build_projection(
        episodes,
        identities,
        acquire,
        collapse=collapse,
        same_track_bridge_ms=same_track_bridge_ms,
        min_track_ms=min_track_ms,
    )
    entries = tuple(dict(entry) for entry in projection.shown_entries)
    output_dir = output_dir or media_dir / "present"
    json_path = output_dir / "tracklist.json"
    markdown_path = output_dir / "tracklist.md"
    atomic_write_json(
        json_path,
        {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "media_key": media_key,
            "duration_ms": duration_ms,
            "generation": episodes.generation,
            "status": status,
            "reason": reason,
            "achieved": achieved,
            "suppressed_count": projection.suppressed_count,
            "entries": list(entries),
        },
    )
    upstream = {
        episodes_path.relative_to(media_dir).as_posix(): episodes_path,
        identities_path.relative_to(media_dir).as_posix(): identities_path,
    }
    if acquire is not None and acquire_path is not None:
        upstream[acquire_path.relative_to(media_dir).as_posix()] = acquire_path
    write_completion_sidecar(json_path, upstream)

    lines = [
        "# Tracklist",
        "",
        "| Time | Confidence | Track | Free DL | Gate | Buy | Search |",
        "|---:|:---:|---|:---:|:---:|:---:|:---:|",
    ]
    for entry in entries:
        if entry["kind"] == "id":
            label = f"ID — no evidence through {_format_time(entry['end_ms'])}"
            lines.append(f"| {_format_time(entry['start_ms'])} | — | {label} | — | — | — | — |")
        else:
            badge = str(entry["badge"]).upper()
            if entry.get("hint_only"):
                badge += " FROM COMMENTS"
            elif entry["hint_supported"]:
                badge += " +HINT"
            if entry.get("engine_corroborated"):
                badge += " +CONFIRMED TWICE"
            label = f"{entry['artist']} — {entry['title']}"
            if entry.get("also_count"):
                others = "; ".join(alt["track"] for alt in entry["alternatives"])
                if entry.get("hint_only"):
                    label += f"<br>also named in the comments — {others}"
                else:
                    label += (
                        f"<br>also: {entry['also_count']} other version"
                        f"{'s' if entry['also_count'] != 1 else ''} matched — {others}"
                    )
            search_cell = "yes" if entry.get("acquire") else "—"
            lines.append(
                f"| {_format_time(entry['start_ms'])} | {badge} | {label} | "
                f"{_acquire_cell(entry, 'free_download')} | {_acquire_cell(entry, 'gate')} | "
                f"{_acquire_cell(entry, 'buy')} | {search_cell} |"
            )
    atomic_write_bytes(markdown_path, ("\n".join(lines) + "\n").encode("utf-8"))
    write_completion_sidecar(markdown_path, upstream)

    cue_path = output_dir / "tracklist.cue"
    atomic_write_bytes(cue_path, render_cue(entries, title=title).encode("utf-8"))
    write_completion_sidecar(cue_path, upstream)

    return ExportResult(json_path, markdown_path, entries, cue_path)


def render_cue(entries: tuple[dict[str, Any], ...], *, title: str | None = None) -> str:
    """Render a flattened CUE sheet.

    The flattening rule is the plan's: entries already carry the primary-role start (an ``incoming``
    role starts at ``best_start_ms``; the outgoing track therefore ends where the next one starts),
    so a CUE ``INDEX 01`` at each entry's ``start_ms`` reproduces "outgoing ends there" implicitly —
    the next track's index is the previous track's out point. ID gaps are emitted as their own
    ``ID`` tracks so the sheet stays a monotonic partition with no silent, unexplained holes.
    """

    header = [
        f"TITLE {_cue_quote(title)}" if title else 'TITLE "DJ set"',
        'FILE "audio" WAVE',
    ]
    body: list[str] = []
    for number, entry in enumerate(entries, 1):
        if entry["kind"] == "id":
            performer, track_title = "ID", "ID"
        else:
            performer, track_title = str(entry["artist"]), str(entry["title"])
        body.append(f"  TRACK {number:02d} AUDIO")
        body.append(f"    TITLE {_cue_quote(track_title)}")
        body.append(f"    PERFORMER {_cue_quote(performer)}")
        # Overlapping episodes (loops, layers, mixes-in-progress) are noted on REM lines: a flat CUE
        # sheet can hold only one track per instant, so the co-sounding tracks are recorded here for
        # honesty rather than silently dropped.
        for other in entry.get("overlap_labels", ()):
            keyword = "LAYER" if entry.get("has_layer") else "OVERLAP"
            body.append(f"    REM {keyword} {_cue_quote(str(other))}")
        body.append(f"    INDEX 01 {_cue_index(int(entry['start_ms']))}")
    return "\n".join(header + body) + "\n"


def _m3u_seconds(milliseconds: int) -> int:
    return max(0, milliseconds // 1000)


def render_m3u(entries: tuple[dict[str, Any], ...], *, media_target: str) -> str:
    """Render an extended M3U whose entries seek with VLC's ``#EXTVLCOPT:start-time``.

    Every entry points at the same ``media_target`` (the mix's URL or a local file) and carries a
    ``#EXTVLCOPT:start-time=<seconds>`` so opening the playlist in VLC and picking a track jumps to
    that moment of the one continuous set.  ID gaps become their own labelled entries so the
    playlist stays a faithful partition of the mix.
    """

    lines = ["#EXTM3U"]
    for entry in entries:
        if entry["kind"] == "id":
            label = f"ID - no evidence through {_format_time(int(entry['end_ms']))}"
        else:
            label = f"{entry['artist']} - {entry['title']}"
        label = label.replace("\n", " ").replace("\r", " ")
        lines.append(f"#EXTINF:-1,{label}")
        lines.append(f"#EXTVLCOPT:start-time={_m3u_seconds(int(entry['start_ms']))}")
        lines.append(media_target)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------------
# Reading a published projection back
# --------------------------------------------------------------------------------------------------
#: Confidence buckets, strongest first — the one order every surface prints them in.
BADGE_ORDER: tuple[str, ...] = ("verified", "likely", "possible", "unclear")


@dataclass(frozen=True)
class ProjectedSummary:
    """The figures of one published run, read back from its ``present/tracklist.json``.

    ``tracklist.json`` *is* the canonical projection's shown entry list (``export_tracklist``
    writes ``projection.shown_entries`` verbatim), so reading it is reading the projection — not
    filtering a second time.  Every surface that needs a summary of a finished run (the library
    card, the library totals, the completion screen) comes through here, so "N tracks" can only
    ever mean one number.

    ``badges`` counts **audio-supported** rows only: a crowd row (``hint_only``) is somebody's
    comment, never evidence, so it must not move a confidence bar.  It is reported separately as
    ``crowd`` so the surface can still say it is there.
    """

    tracks: int
    crowd: int
    duration_ms: int
    badges: dict[str, int]
    suppressed_count: int
    status: str | None
    reason: str | None
    achieved: str | None

    @property
    def audio_tracks(self) -> int:
        """Rows whose confidence came from the audio — the denominator of ``badges``."""

        return sum(self.badges.values())

    def majority_badge(self) -> tuple[str, int] | None:
        """The largest audio-confidence bucket **only when it is an actual majority**.

        "Mostly likely" over a 40 % plurality is a claim the numbers do not support (U-F13/F16), so
        a plurality returns ``None`` and the caller words it as the mix it is.
        """

        total = self.audio_tracks
        if not total:
            return None
        strongest = max(
            BADGE_ORDER, key=lambda key: (self.badges.get(key, 0), -BADGE_ORDER.index(key))
        )
        count = self.badges.get(strongest, 0)
        return (strongest, count) if count * 2 > total else None


def read_projected_summary(path: Path) -> ProjectedSummary | None:
    """Read one published ``tracklist.json`` as a :class:`ProjectedSummary` (``None`` if absent)."""

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = [
            entry
            for entry in document.get("entries", ())
            if isinstance(entry, dict) and entry.get("kind") == "track"
        ]
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    badges: dict[str, int] = {}
    crowd = 0
    for entry in entries:
        if entry.get("hint_only"):
            crowd += 1
            continue
        badge = str(entry.get("badge", "unclear"))
        badges[badge] = badges.get(badge, 0) + 1
    return ProjectedSummary(
        tracks=len(entries),
        crowd=crowd,
        duration_ms=int(document.get("duration_ms") or 0),
        badges=badges,
        suppressed_count=int(document.get("suppressed_count") or 0),
        status=document.get("status"),
        reason=document.get("reason"),
        achieved=document.get("achieved"),
    )
