"""Conventional flattened JSON and Markdown tracklist exports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from id_detector.contracts import EpisodesFile, IdentitiesRecord
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


def _format_time(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


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


def flatten_tracklist(
    episodes: EpisodesFile, identities: IdentitiesRecord
) -> tuple[dict[str, Any], ...]:
    """Flatten overlapping episodes using primary-role precedence and honest ID gaps."""

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
        entries.append(
            {
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
                "tiers": episode.tiers.model_dump(mode="json"),
            }
        )
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


def export_tracklist(
    *,
    media_dir: Path,
    media_key: str,
    duration_ms: int,
    episodes: EpisodesFile,
    identities: IdentitiesRecord,
    episodes_path: Path,
    identities_path: Path,
) -> ExportResult:
    entries = flatten_tracklist(episodes, identities)
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
    write_completion_sidecar(json_path, upstream)

    lines = [
        "# Tracklist",
        "",
        "| Time | Badge | Version | Role | Track |",
        "|---:|:---:|:---:|:---:|---|",
    ]
    for entry in entries:
        if entry["kind"] == "id":
            label = f"ID — no evidence through {_format_time(entry['end_ms'])}"
            lines.append(f"| {_format_time(entry['start_ms'])} | — | — | gap | {label} |")
        else:
            badge = str(entry["badge"]).upper()
            if entry["hint_supported"]:
                badge += " +HINT"
            version_status = str(entry["version_status"]).upper()
            label = f"{entry['artist']} — {entry['title']}"
            lines.append(
                f"| {_format_time(entry['start_ms'])} | {badge} | {version_status} | "
                f"{entry['primary_role']} | {label} |"
            )
    atomic_write_bytes(markdown_path, ("\n".join(lines) + "\n").encode("utf-8"))
    write_completion_sidecar(markdown_path, upstream)
    return ExportResult(json_path, markdown_path, entries)
