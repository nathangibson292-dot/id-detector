"""Conservative mirror quarantine release policy."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from id_detector.contracts import HintRecord, SourceRecord


@dataclass(frozen=True)
class MirrorMetadata:
    platform_id: str | None
    uploader_id: str | None
    upload_date: str | None
    duration_ms: int


def _normalise(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _agrees(left: HintRecord, right: HintRecord) -> bool:
    if not left.artist or not left.title or not right.artist or not right.title:
        return False
    if (_normalise(left.artist), _normalise(left.title)) != (
        _normalise(right.artist),
        _normalise(right.title),
    ):
        return False
    if left.position_range_ms is None or right.position_range_ms is None:
        return False
    distance = max(
        0,
        max(left.position_range_ms[0], right.position_range_ms[0])
        - min(left.position_range_ms[1], right.position_range_ms[1]),
    )
    return distance <= 60_000


def _distinct_timeline_agreements(
    source_hints: list[HintRecord] | tuple[HintRecord, ...],
    mirror_hints: list[HintRecord] | tuple[HintRecord, ...],
) -> int:
    pairs = sorted(
        (
            source_hint.provenance_group,
            mirror_hint.provenance_group,
            _normalise(source_hint.artist),
            _normalise(source_hint.title),
            source_hint.position_range_ms,
            mirror_hint.position_range_ms,
        )
        for source_hint in source_hints
        for mirror_hint in mirror_hints
        if _agrees(source_hint, mirror_hint)
    )
    used_source: set[str] = set()
    used_mirror: set[str] = set()
    used_timeline: set[tuple[str, str, tuple[int, int] | None, tuple[int, int] | None]] = set()
    count = 0
    for source_group, mirror_group, artist, title, source_range, mirror_range in pairs:
        timeline = (artist, title, source_range, mirror_range)
        if source_group in used_source or mirror_group in used_mirror or timeline in used_timeline:
            continue
        used_source.add(source_group)
        used_mirror.add(mirror_group)
        used_timeline.add(timeline)
        count += 1
    return count


def mirror_is_verified(
    source: SourceRecord,
    *,
    source_duration_ms: int,
    mirror: MirrorMetadata,
    source_hints: list[HintRecord] | tuple[HintRecord, ...],
    mirror_hints: list[HintRecord] | tuple[HintRecord, ...],
    manual_confirmation: bool = False,
) -> bool:
    """Require identity/date, duration, and two timeline agreements unless manually confirmed."""

    if manual_confirmation:
        return True
    platform_agreement = bool(
        source.platform_id and mirror.platform_id and source.platform_id == mirror.platform_id
    )
    uploader_date_agreement = bool(
        source.uploader_id
        and mirror.uploader_id
        and source.uploader_id == mirror.uploader_id
        and source.upload_date
        and mirror.upload_date
        and source.upload_date == mirror.upload_date
    )
    duration_agreement = (
        source_duration_ms > 0
        and mirror.duration_ms > 0
        and abs(mirror.duration_ms - source_duration_ms) * 100 <= source_duration_ms * 2
    )
    timeline_agreements = _distinct_timeline_agreements(source_hints, mirror_hints)
    return (
        (platform_agreement or uploader_date_agreement)
        and duration_agreement
        and (timeline_agreements >= 2)
    )
