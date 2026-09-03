"""Pure hint parsing primitives fixed by the Stage 0 contract."""

from __future__ import annotations

import re

_COLON_TIMESTAMP = re.compile(r"(?<![\d:])(\d+):(\d{1,2})(?::(\d{1,2}))?(?![\d:])")
_DOTTED_TIMESTAMP = re.compile(r"(?i)(?:\bat\b|\baround\b|@|\btrack\b)\s*(\d+)\.(\d{2})(?!\d)")
_TRACK_WORD = re.compile(r"(?i)\b(track|tune|song)\b")
_ID_WORD = re.compile(r"(?i)\bid\b")


def parse_hint_timestamp(text: str, *, media_duration_ms: int | None = None) -> int | None:
    """Parse by component count; dotted forms require an explicit nearby context word."""

    dotted = _DOTTED_TIMESTAMP.search(text)
    match = _COLON_TIMESTAMP.search(text)
    if match:
        first, second, third = match.groups()
        if third is None:
            minutes, seconds = int(first), int(second)
            if seconds > 59:
                return None
            total_seconds = minutes * 60 + seconds
        else:
            hours, minutes, seconds = int(first), int(second), int(third)
            if minutes > 59 or seconds > 59:
                return None
            total_seconds = hours * 3600 + minutes * 60 + seconds
    elif dotted:
        minutes, seconds = int(dotted.group(1)), int(dotted.group(2))
        if seconds > 59:
            return None
        total_seconds = minutes * 60 + seconds
    else:
        return None
    result = total_seconds * 1000
    if media_duration_ms is not None and result > media_duration_ms:
        return None
    return result


def is_track_question(text: str) -> bool:
    """Questions require a track word and either punctuation or the standalone word 'id'."""

    has_track_word = bool(_TRACK_WORD.search(text))
    return has_track_word and ("?" in text or bool(_ID_WORD.search(text)))
