"""Conventional flattened JSON and Markdown tracklist exports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from id_detector.contracts import AcquireEpisode, AcquireFile, EpisodesFile, IdentitiesRecord
from id_detector.io import atomic_write_bytes, atomic_write_json, write_completion_sidecar

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


def _candidate_label(identities: IdentitiesRecord, candidate_id: str) -> tuple[str, str]:
    candidate = next(item for item in identities.candidates if item.canonical_id == candidate_id)
    labels = [
        node.label
        for node in identities.nodes
        if node.id in candidate.member_nodes and node.ns != "text"
    ]
    if not labels:
        work = next(item for item in identities.works if item.work_id == candidate.work_id)
        labels = [node.label for node in identities.nodes if node.id in work.member_nodes]
    label = min(labels) if labels else "Unknown artist - Unknown title"
    if " - " not in label:
        return "Unknown artist", label
    return tuple(label.split(" - ", 1))  # type: ignore[return-value]


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


def flatten_tracklist(
    episodes: EpisodesFile,
    identities: IdentitiesRecord,
    acquire: AcquireFile | None = None,
) -> tuple[dict[str, Any], ...]:
    """Flatten overlapping episodes using primary-role precedence and honest ID gaps."""

    acquire_by_episode = (
        {item.episode_id: item for item in acquire.episodes} if acquire is not None else {}
    )
    entries: list[dict[str, Any]] = []
    for episode in episodes.episodes:
        artist, title = _candidate_label(identities, episode.candidate_id)
        primary = min(
            episode.role_segments,
            key=lambda item: (_ROLE_PRECEDENCE[item.role], item.from_ms, item.to_ms),
            default=None,
        )
        primary_role = primary.role if primary is not None else "uncertain"
        start_ms = (
            episode.best_start_ms
            if primary_role == "incoming" or primary is None
            else primary.from_ms
        )
        acquire_episode = acquire_by_episode.get(episode.id)
        entry = {
            "kind": "track",
            "start_ms": start_ms,
            "episode_id": episode.id,
            "candidate_id": episode.candidate_id,
            "artist": artist,
            "title": title,
            "occurrence_index": episode.occurrence_index,
            "primary_role": primary_role,
            "badge": episode.badge,
            "version_status": episode.version_status,
            "hint_supported": "hint_supported" in episode.flags,
            "n_rejected_hypotheses": len(episode.rejected_evidence),
            "tiers": episode.tiers.model_dump(mode="json"),
            "acquire": _acquire_summary(acquire_episode) if acquire_episode is not None else None,
        }
        entries.append(entry)
    entries.extend(
        {
            "kind": "id",
            "start_ms": gap.start_ms,
            "end_ms": gap.end_ms,
            "gap_id": gap.id,
            "label": "ID",
            "reason": gap.reason,
        }
        for gap in episodes.gaps
    )
    return tuple(
        sorted(
            entries,
            key=lambda item: (
                item["start_ms"],
                0 if item["kind"] == "track" else 1,
                item.get("episode_id", item.get("gap_id", "")),
            ),
        )
    )


def _acquire_cell(entry: dict[str, Any], key: str) -> str:
    acquire = entry.get("acquire")
    if not acquire:
        return "—"
    return "yes" if acquire.get(key) else "—"


def export_tracklist(
    *,
    media_dir: Path,
    media_key: str,
    duration_ms: int,
    episodes: EpisodesFile,
    identities: IdentitiesRecord,
    episodes_path: Path,
    identities_path: Path,
    acquire: AcquireFile | None = None,
    acquire_path: Path | None = None,
    title: str | None = None,
) -> ExportResult:
    entries = flatten_tracklist(episodes, identities, acquire)
    json_path = media_dir / "present" / "tracklist.json"
    markdown_path = media_dir / "present" / "tracklist.md"
    atomic_write_json(
        json_path,
        {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "media_key": media_key,
            "duration_ms": duration_ms,
            "generation": episodes.generation,
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
        "| Time | Badge | Version | Role | Track | Free DL | Gate | Buy | Search |",
        "|---:|:---:|:---:|:---:|---|:---:|:---:|:---:|:---:|",
    ]
    for entry in entries:
        if entry["kind"] == "id":
            label = f"ID — no evidence through {_format_time(entry['end_ms'])}"
            lines.append(
                f"| {_format_time(entry['start_ms'])} | — | — | gap | {label} | — | — | — | — |"
            )
        else:
            badge = str(entry["badge"]).upper()
            if entry["hint_supported"]:
                badge += " +HINT"
            version_status = str(entry["version_status"]).upper()
            label = f"{entry['artist']} — {entry['title']}"
            search_cell = "yes" if entry.get("acquire") else "—"
            lines.append(
                f"| {_format_time(entry['start_ms'])} | {badge} | {version_status} | "
                f"{entry['primary_role']} | {label} | "
                f"{_acquire_cell(entry, 'free_download')} | {_acquire_cell(entry, 'gate')} | "
                f"{_acquire_cell(entry, 'buy')} | {search_cell} |"
            )
    atomic_write_bytes(markdown_path, ("\n".join(lines) + "\n").encode("utf-8"))
    write_completion_sidecar(markdown_path, upstream)

    cue_path = media_dir / "present" / "tracklist.cue"
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
        body.append(f"    INDEX 01 {_cue_index(int(entry['start_ms']))}")
    return "\n".join(header + body) + "\n"
