"""Deterministic parser for comments, descriptions, chapters, and tracklists."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import urlsplit

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    HintAuthor,
    HintFlags,
    HintRecord,
    compose_natural_key,
    make_id,
)
from id_detector.io import redact_text, url_has_credentials

_COLON_TIMESTAMP = re.compile(r"(?<![\d:])(\d+):(\d{1,2})(?::(\d{1,2}))?(?![\d:])")
_DOTTED_TIMESTAMP = re.compile(r"(?i)(?:\bat\b|\baround\b|@|\btrack\b)\s*(\d+)\.(\d{2})(?!\d)")
_MINUTE_CUE = re.compile(r"^\s*[\[(](\d+)[\])]\s*(.+?)\s*$")
_TRACK_WORD = re.compile(r"(?i)\b(track|tune|song)\b")
_ID_WORD = re.compile(r"(?i)\bid\b")
_POINTER = re.compile(r"https://[^\s<>\]\[\)\(]+", re.IGNORECASE)
_ALLOWED_POINTER_HOSTS = {
    "www.1001tracklists.com",
    "1001.tl",
    "www.mixesdb.com",
    "boilerroom.tv",
    "www.boilerroom.tv",
    "blrrm.tv",
    "soundcloud.com",
    "www.soundcloud.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "mixcloud.com",
    "www.mixcloud.com",
}
_SPACED_SEPARATORS = (" - ", " – ", " — ", " ~ ", " : ", " | ")
_VERSION = re.compile(
    r"(?i)(?:\(([^()]*(?:remix|edit|rework|bootleg|mix|vip|dub|version)[^()]*)\)"
    r"|\[([^\[\]]*(?:remix|edit|rework|bootleg|mix|vip|dub|version)[^\[\]]*)\])"
)
_LABEL = re.compile(r"\[([^\[\]]+)\]\s*$")
_UNRELEASED = re.compile(r"(?i)\b(unreleased\*?|forthcoming(?:\s+on)?|dubplate)\b")
_MASHUP = re.compile(r"(?i)(?:(?<!\w)w/(?!\w)|\bvs\.?\b|\s+x\s+)")
_EDIT = re.compile(r"(?i)\b(edit|rework|vip|dub)\b")
_BOOTLEG = re.compile(r"(?i)\bbootleg\b")
_UNKNOWN = re.compile(r"(?i)^\s*(?:id\s*-\s*id|id|\?)\s*(?:\([^)]*\))?\s*$")
_REACTION = re.compile(r"(?i)^\s*(?:this is|so good|fire\b|what a set|love this)\b")
_ANSWER_PREFIX = re.compile(
    r"(?ix)^\s*(?:@[\w.-]+:?\s*|\[mention\]\s*)?"
    r"(?:(?:this|it|that|track|song|tune)\s*(?:is|=|:)\s*|"
    r"(?:track\s+)?id\s*[:=-]\s*)"
)
_CORRECTION_PREFIX = re.compile(
    r"(?ix)^\s*(?:@[\w.-]+:?\s*|\[mention\]\s*)?"
    r"(?:actually\s+|(?:the\s+)?track\s+at\s+"
    r"(?:\d+:)?\d{1,3}:\d{2}\s+is\s+(?:actually\s+)?)"
)
_CORRECTION_CLEAN = re.compile(
    r"(?i)^\s*(?:@[\w.-]+:?\s*|\[mention\]\s*)?"
    r"(?:(?:the\s+)?track\s+at\s+(?:is\s+)?(?:actually\s+)?|actually\s+)"
)
_MENTION_PREFIX = re.compile(r"^\s*(?:@[\w.-]+:?\s*|\[mention\]\s*)", re.IGNORECASE)
_TRACKLIST_PREFIX = re.compile(r"(?i)^\s*(?:track\s*list|tracklist|tl)(?:\s+so\s+far)?\s*:?\s*")

PositionKind = Literal["cue_hms", "cue_minute", "comment_timestamp", "chapter", "section", "none"]


@dataclass(frozen=True)
class HintInput:
    """One connector record before parsing; all identifiers are already pseudonymous."""

    connector: str
    source_record_id: str
    text: str
    position_ms: int | None = None
    position_end_ms: int | None = None
    position_kind: PositionKind = "none"
    author_pseudo_id: str = "unknown"
    author_permalink: str | None = None
    is_uploader: bool = False
    is_verified: bool = False
    follower_count: int | None = None
    like_count: int | None = None
    is_pinned: bool = False
    parent_source_id: str | None = None
    mirror_of: str | None = None
    mirror_status: Literal["verified", "quarantined"] = "verified"
    truncated: bool = False
    structured_tracklist: bool = False


@dataclass(frozen=True)
class ParsedUnit:
    raw_text: str
    kind: Literal["tracklist_line", "answer", "correction", "question", "pointer", "keyword"]
    artist: str | None
    title: str | None
    version_qualifier: str | None
    label: str | None
    flags: HintFlags
    position_range_ms: tuple[int, int] | None
    position_kind: PositionKind
    parse_confidence: int
    identity_specificity: int
    temporal_precision_ms: int | None
    cue_ms: int | None
    cue_form: Literal["hms", "minute", "range", "none"]


def _colon_value(match: re.Match[str]) -> int | None:
    first, second, third = match.groups()
    if third is None:
        minutes, seconds = int(first), int(second)
        if seconds > 59:
            return None
        return (minutes * 60 + seconds) * 1_000
    hours, minutes, seconds = int(first), int(second), int(third)
    if minutes > 59 or seconds > 59:
        return None
    return (hours * 3_600 + minutes * 60 + seconds) * 1_000


def _timestamp_matches(text: str, media_duration_ms: int | None) -> list[tuple[re.Match[str], int]]:
    matches: list[tuple[re.Match[str], int]] = []
    for match in _COLON_TIMESTAMP.finditer(text):
        value = _colon_value(match)
        if value is not None and (media_duration_ms is None or value <= media_duration_ms):
            matches.append((match, value))
    return matches


def parse_hint_timestamp(text: str, *, media_duration_ms: int | None = None) -> int | None:
    """Parse the first valid cue by component count, including context-gated dotted cues."""

    colon = _timestamp_matches(text, media_duration_ms)
    if colon:
        return colon[0][1]
    dotted = _DOTTED_TIMESTAMP.search(text)
    if dotted is None or int(dotted.group(2)) > 59:
        return None
    value = (int(dotted.group(1)) * 60 + int(dotted.group(2))) * 1_000
    return value if media_duration_ms is None or value <= media_duration_ms else None


def is_track_question(text: str) -> bool:
    """A question needs a track/tune/song word and either ``?`` or standalone ``id``."""

    if re.match(r"(?i)^\s*(?:track\s+)?id\s*[:=-]\s*\S", text):
        return False
    return bool(_TRACK_WORD.search(text)) and ("?" in text or bool(_ID_WORD.search(text)))


def _clip_range(start: int, end: int, duration_ms: int) -> tuple[int, int] | None:
    start = min(max(0, start), duration_ms)
    end = min(max(0, end), duration_ms)
    if end <= start:
        if duration_ms <= 0:
            return None
        if start == duration_ms:
            start = max(0, duration_ms - 1)
            end = duration_ms
        else:
            end = min(duration_ms, start + 1)
    return (start, end) if end > start else None


def _cue_range(cue_ms: int, form: str, duration_ms: int) -> tuple[int, int] | None:
    if form == "minute":
        return _clip_range(cue_ms, cue_ms + 60_000, duration_ms)
    return _clip_range(cue_ms - 5_000, cue_ms + 5_000, duration_ms)


def _split_mega(text: str, duration_ms: int) -> list[str]:
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _TRACKLIST_PREFIX.sub("", text, count=1)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    result: list[str] = []
    for line in lines:
        bigos = list(re.finditer(r"\((\d{1,3})\)(?=\s*\S)", line))
        valid_bigos = [item for item in bigos if int(item.group(1)) * 60_000 <= duration_ms]
        if len(valid_bigos) >= 2:
            for index, item in enumerate(valid_bigos):
                end = valid_bigos[index + 1].start() if index + 1 < len(valid_bigos) else len(line)
                result.append(line[item.start() : end].strip())
            continue
        stamps = _timestamp_matches(line, duration_ms)
        if len(stamps) >= 2:
            # Do not split a leading start/end range ("10:00 - 12:00 Track").
            start_index = 1 if re.match(r"^\s*\d+:\d{2}\s+[-–—]\s+\d+:\d{2}\b", line) else 0
            selected = stamps[start_index:]
            if start_index and len(selected) == 1:
                result.append(line)
                continue
            prefix = line[: selected[0][0].start()].strip()
            for index, (item, _) in enumerate(selected):
                end = selected[index + 1][0].start() if index + 1 < len(selected) else len(line)
                unit = line[item.start() : end].strip()
                if index == 0 and prefix:
                    unit = f"{prefix} {unit}"
                result.append(unit)
            continue
        result.append(line)
    return result


def _strip_cues(text: str, duration_ms: int) -> tuple[str, int | None, str, tuple[int, int] | None]:
    minute = _MINUTE_CUE.match(text)
    if minute:
        cue = int(minute.group(1)) * 60_000
        if cue <= duration_ms:
            return minute.group(2).strip(), cue, "minute", _cue_range(cue, "minute", duration_ms)

    stamps = _timestamp_matches(text, duration_ms)
    if not stamps:
        dotted = _DOTTED_TIMESTAMP.search(text)
        if dotted is not None and int(dotted.group(2)) <= 59:
            cue = (int(dotted.group(1)) * 60 + int(dotted.group(2))) * 1_000
            if cue <= duration_ms:
                cleaned = (text[: dotted.start()] + text[dotted.end() :]).strip(" -–—:|@")
                return cleaned.strip(), cue, "hms", _cue_range(cue, "hms", duration_ms)
        return text.strip(), None, "none", None

    first_match, first_value = stamps[0]
    if len(stamps) >= 2:
        second_match, second_value = stamps[1]
        between = text[first_match.end() : second_match.start()]
        if re.fullmatch(r"\s*[-–—]\s*", between) and second_value >= first_value:
            cleaned = (text[: first_match.start()] + text[second_match.end() :]).strip()
            return (
                cleaned.lstrip("-–—:.) "),
                first_value,
                "range",
                _clip_range(first_value, second_value, duration_ms),
            )
    first_at_start = not text[: first_match.start()].strip(" [(#")
    last_at_end = not text[first_match.end() :].strip(" ]).")
    if first_at_start:
        cleaned = text[first_match.end() :].lstrip(" -–—:.)]")
    elif last_at_end:
        cleaned = text[: first_match.start()].rstrip(" -–—:.)[")
    else:
        cleaned = (text[: first_match.start()] + text[first_match.end() :]).strip()
    return cleaned.strip(), first_value, "hms", _cue_range(first_value, "hms", duration_ms)


def _artist_title(text: str, *, split_no_space: bool) -> tuple[str | None, str | None, int]:
    clean = _MENTION_PREFIX.sub("", text).strip()
    by_match = re.fullmatch(r"(.+?)\s+by\s+(.+)", clean, re.IGNORECASE)
    if by_match:
        return by_match.group(2).strip(), by_match.group(1).strip(), 8_500
    for separator in _SPACED_SEPARATORS:
        if separator in clean:
            artist, title = clean.split(separator, 1)
            if artist.strip() and title.strip():
                return artist.strip(), title.strip(), 9_000
    if split_no_space and "-" in clean:
        artist, title = clean.split("-", 1)
        if artist.strip() and title.strip():
            return artist.strip(), title.strip(), 5_000
    return None, clean or None, 3_000


def _flags_and_qualifiers(
    text: str, artist: str | None, title: str | None
) -> tuple[str | None, str | None, HintFlags, int]:
    unknown = bool(_UNKNOWN.fullmatch(text)) or bool(
        artist and title and artist.casefold() == "id" and title.casefold().startswith("id")
    )
    version_match = _VERSION.search(text)
    qualifier = None
    if version_match:
        qualifier = (version_match.group(1) or version_match.group(2) or "").strip() or None
    label_match = _LABEL.search(text)
    label = None
    if label_match and not re.search(
        r"(?i)\b(remix|edit|rework|bootleg|mix|vip|dub|version)\b", label_match.group(1)
    ):
        label = label_match.group(1).strip()
    flags = HintFlags(
        unreleased=bool(_UNRELEASED.search(text)),
        id_unknown=unknown,
        mashup_with=bool(_MASHUP.search(text)),
        edit=bool(_EDIT.search(text)),
        bootleg=bool(_BOOTLEG.search(text)),
    )
    specificity = 0 if unknown else 10_000 if artist and title else 5_000 if title else 0
    return qualifier, label, flags, specificity


def _clean_identity_field(
    value: str | None, qualifier: str | None, label: str | None
) -> str | None:
    if value is None:
        return None
    cleaned = value
    for extra in (qualifier, label):
        if not extra:
            continue
        cleaned = re.sub(
            rf"\s*(?:\({re.escape(extra)}\)|\[{re.escape(extra)}\])\s*",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _pointer_unit(text: str) -> ParsedUnit | None:
    urls = [
        match.group(0).rstrip(".,;:")
        for match in _POINTER.finditer(text)
        if (urlsplit(match.group(0).rstrip(".,;:")).hostname or "").casefold()
        in _ALLOWED_POINTER_HOSTS
        and not url_has_credentials(match.group(0).rstrip(".,;:"))
    ]
    if not urls:
        return None
    return ParsedUnit(
        raw_text=text,
        kind="pointer",
        artist=None,
        title=urls[0],
        version_qualifier=None,
        label=None,
        flags=HintFlags(
            unreleased=False,
            id_unknown=False,
            mashup_with=False,
            edit=False,
            bootleg=False,
        ),
        position_range_ms=None,
        position_kind="none",
        parse_confidence=10_000,
        identity_specificity=0,
        temporal_precision_ms=None,
        cue_ms=None,
        cue_form="none",
    )


def parse_text_units(
    text: str,
    *,
    media_duration_ms: int,
    comment_timestamp_ms: int | None = None,
    comment_position_kind: PositionKind = "comment_timestamp",
    structured_tracklist: bool = False,
    enforce_block_acceptance: bool = False,
) -> list[ParsedUnit]:
    """Classify units and expose cue parsing before source-record materialisation.

    With ``enforce_block_acceptance``, timestamped lines form a tracklist only when at least two
    cues are valid, non-decreasing, and in duration. The default keeps one-line fixture inspection
    useful; connector materialisation always enables the stricter work-level rule.
    """

    units = _split_mega(text, media_duration_ms)
    no_space_candidates = [
        unit
        for unit in units
        if not any(separator in unit for separator in _SPACED_SEPARATORS)
        and re.search(r"\S-\S", _strip_cues(unit, media_duration_ms)[0])
    ]
    split_no_space = len(no_space_candidates) >= 3 and not any(
        separator in unit for unit in units for separator in _SPACED_SEPARATORS
    )
    prepared = []
    cue_values = []
    for unit in units:
        clean, cue, cue_form, cue_range = _strip_cues(unit, media_duration_ms)
        prepared.append((unit, clean, cue, cue_form, cue_range))
        if cue is not None:
            cue_values.append(cue)
    valid_timed_block = (
        not enforce_block_acceptance and len(units) == 1 and len(cue_values) == 1
    ) or (
        len(cue_values) >= 2
        and cue_values == sorted(cue_values)
        and all(value <= media_duration_ms for value in cue_values)
    )
    valid_block = valid_timed_block or (structured_tracklist and not cue_values and len(units) >= 2)

    parsed: list[ParsedUnit] = []
    for raw, clean, cue, cue_form, cue_range in prepared:
        pointer = _pointer_unit(raw)
        if pointer is not None:
            parsed.append(pointer)
            continue
        if cue is not None and len(units) > 1 and not valid_timed_block:
            continue
        question = is_track_question(raw)
        correction = bool(_CORRECTION_PREFIX.match(raw))
        answer_prefix = bool(_ANSWER_PREFIX.match(raw))
        potential_tracklist = cue is not None and valid_block
        if correction:
            body = _CORRECTION_CLEAN.sub("", clean)
        elif question or potential_tracklist:
            body = clean
        else:
            body = _ANSWER_PREFIX.sub("", clean)
        artist, title, confidence = _artist_title(body, split_no_space=split_no_space)
        qualifier, label, flags, specificity = _flags_and_qualifiers(body, artist, title)
        artist = _clean_identity_field(artist, qualifier, label)
        title = _clean_identity_field(title, qualifier, label)
        if not flags.id_unknown:
            specificity = 10_000 if artist and title else 5_000 if title else 0

        position_range = cue_range
        position_kind: PositionKind = "cue_minute" if cue_form == "minute" else "cue_hms"
        temporal = (
            60_000
            if cue_form == "minute"
            else 5_000
            if cue_form == "hms"
            else None
            if cue_range is None
            else cue_range[1] - cue_range[0]
        )
        if cue is None:
            position_kind = "none"
            if comment_timestamp_ms is not None:
                if question:
                    position_range = _clip_range(
                        comment_timestamp_ms - 120_000,
                        comment_timestamp_ms + 15_000,
                        media_duration_ms,
                    )
                else:
                    position_range = _clip_range(
                        comment_timestamp_ms - 5_000,
                        comment_timestamp_ms + 5_000,
                        media_duration_ms,
                    )
                position_kind = comment_position_kind
                temporal = None if position_range is None else position_range[1] - position_range[0]

        structured_position = structured_tracklist and comment_timestamp_ms is not None
        if structured_position:
            position_range = _clip_range(
                comment_timestamp_ms,
                comment_timestamp_ms + 1,
                media_duration_ms,
            )
            position_kind = comment_position_kind
            temporal = 1 if position_range is not None else None

        if question:
            kind = "question"
            artist = title = qualifier = label = None
            specificity = 0
            confidence = 8_000
            flags = flags.model_copy(update={"id_unknown": False})
        elif correction:
            kind = "correction"
        elif potential_tracklist or structured_position or (structured_tracklist and valid_block):
            kind = "tracklist_line"
            confidence = min(confidence, 8_500 if structured_tracklist else 7_500)
        elif answer_prefix or (artist is not None and title is not None):
            kind = "answer"
        elif title and re.search(r"\S-\S", title):
            kind = "answer"
            confidence = min(confidence, 3_000)
        elif flags.unreleased or flags.id_unknown:
            kind = "keyword"
        else:
            continue

        if cue is None and re.match(r"^\s*\d+(?::\d+){1,3}\b", raw):
            continue

        if _REACTION.match(body) and kind == "answer":
            continue
        if not split_no_space and artist is None and title and re.search(r"\S-\S", title):
            confidence = min(confidence, 3_000)
            qualifier = None
        parsed.append(
            ParsedUnit(
                raw_text=raw,
                kind=kind,
                artist=artist,
                title=title,
                version_qualifier=qualifier,
                label=label,
                flags=flags,
                position_range_ms=position_range,
                position_kind=position_kind,
                parse_confidence=confidence,
                identity_specificity=specificity,
                temporal_precision_ms=temporal,
                cue_ms=cue,
                cue_form=cue_form,  # type: ignore[arg-type]
            )
        )
    return parsed


def parse_hint_inputs(
    media_key: str,
    media_duration_ms: int,
    inputs: list[HintInput] | tuple[HintInput, ...],
) -> list[HintRecord]:
    """Parse connector inputs into deterministic contract records."""

    records: list[HintRecord] = []
    for source in sorted(inputs, key=lambda item: (item.connector, item.source_record_id)):
        parsed = parse_text_units(
            source.text,
            media_duration_ms=media_duration_ms,
            comment_timestamp_ms=source.position_ms,
            comment_position_kind=source.position_kind,
            structured_tracklist=source.structured_tracklist,
            enforce_block_acceptance=True,
        )
        for index, unit in enumerate(parsed):
            if source.structured_tracklist and source.position_ms is not None:
                end = source.position_end_ms or source.position_ms + 1
                range_override = _clip_range(source.position_ms, end, media_duration_ms)
                unit = replace(
                    unit,
                    position_range_ms=range_override,
                    position_kind=source.position_kind,
                    temporal_precision_ms=(1_000 if range_override is not None else None),
                )
            source_record_id = f"{source.source_record_id}:{index}"
            hint_id = make_id(
                media_key,
                "hint",
                compose_natural_key(
                    "hint", {"connector": source.connector, "source_record_id": source_record_id}
                ),
            )
            records.append(
                HintRecord(
                    schema_version=SCHEMA_VERSION,
                    generated_by=GENERATED_BY,
                    id=hint_id,
                    connector=source.connector,
                    kind=unit.kind,
                    raw_text=redact_text(unit.raw_text),
                    artist=redact_text(unit.artist) if unit.artist is not None else None,
                    title=redact_text(unit.title) if unit.title is not None else None,
                    version_qualifier=(
                        redact_text(unit.version_qualifier)
                        if unit.version_qualifier is not None
                        else None
                    ),
                    label=redact_text(unit.label) if unit.label is not None else None,
                    flags=unit.flags,
                    position_range_ms=unit.position_range_ms,
                    position_kind=unit.position_kind,
                    author=HintAuthor(
                        pseudo_id=source.author_pseudo_id,
                        is_uploader=source.is_uploader,
                        is_verified=source.is_verified,
                        follower_count=source.follower_count,
                        like_count=source.like_count,
                    ),
                    is_pinned=source.is_pinned,
                    parse_confidence=unit.parse_confidence,
                    identity_specificity=unit.identity_specificity,
                    temporal_precision_ms=unit.temporal_precision_ms,
                    relations=[],
                    provenance_group=hint_id,
                    mirror_of=source.mirror_of,
                    mirror_status=source.mirror_status,
                    truncated=source.truncated,
                )
            )
    return sorted(
        records,
        key=lambda item: (
            item.position_range_ms[0] if item.position_range_ms is not None else media_duration_ms,
            item.id,
        ),
    )
