"""Minimise local raw comment dumps into safe parser fixtures.

The raw directory is read-only. Derived authors receive deterministic, sequential fixture-local
identities; handles are never hashed or retained.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from id_detector.hints import is_track_question
from id_detector.io import atomic_write_bytes, canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "comments"
OUTPUT = ROOT / "data" / "fixtures" / "hints" / "derived"
MAX_LINES_PER_SOURCE = 300

_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_MENTION = re.compile(r"(?<!\w)@[\w.-]+")
_LONG_ID = re.compile(r"\b\d{9,}\b")
_TIMESTAMP = re.compile(r"(?<!\d)\d+[:.]\d{2}(?::\d{2})?(?!\d)")
_MINUTE_CUE = re.compile(r"(?:^|\s)[\[(]\d{1,3}[\])]\s*")
_SPACED_SEPARATOR = re.compile(r"\s(?:-|–|—|~|:|\|)\s")
_WORD = re.compile(r"[^\W\d_]+(?:['.’][^\W\d_]+)*", re.UNICODE)
_KEEP_WORDS = {
    "tracklist",
    "track",
    "tune",
    "song",
    "name",
    "id",
    "at",
    "around",
    "actually",
    "is",
    "by",
    "remix",
    "edit",
    "rework",
    "bootleg",
    "mix",
    "vip",
    "dub",
    "version",
    "unreleased",
    "forthcoming",
    "dubplate",
    "this",
    "one",
    "anyone",
    "what's",
}


def _walk_records(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if any(key in value for key in ("body", "text", "comment")):
            yield value
        for item in value.values():
            yield from _walk_records(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_records(item)


def _read_records(path: Path) -> Iterator[Mapping[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        yield from _walk_records(json.loads(text))
        return
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        try:
            yield from _walk_records(json.loads(line))
        except json.JSONDecodeError:
            continue


def _body(record: Mapping[str, Any]) -> str:
    return str(record.get("body") or record.get("text") or record.get("comment") or "").strip()


def _author_key(record: Mapping[str, Any]) -> str:
    user = record.get("user") if isinstance(record.get("user"), Mapping) else {}
    return str(
        record.get("u")
        or record.get("user_id")
        or record.get("author_id")
        or user.get("permalink")
        or record.get("author")
        or "anonymous"
    )


def _position_ms(record: Mapping[str, Any]) -> int | None:
    if record.get("start_time") is not None:
        try:
            return max(0, round(float(record["start_time"]))) * 1000
        except (TypeError, ValueError):
            return None
    raw = record.get("ts", record.get("timestamp"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    # Unix timestamps are not positions; compact research ``ts`` values are milliseconds.
    if value > 7 * 24 * 60 * 60 * 1000:
        return None
    return max(0, ((value + 500) // 1000) * 1000)


def _category(text: str) -> str | None:
    lowered = text.casefold()
    timestamp_count = len(_TIMESTAMP.findall(text))
    if "tracklist" in lowered or timestamp_count >= 2 or _MINUTE_CUE.search(text):
        return "tracklist_like"
    if is_track_question(text):
        return "id_question"
    if "actually" in lowered or "correction" in lowered or "correct" in lowered:
        return "correction"
    if re.search(r"(?i)\b(?:track|song|tune)\s*(?:is|=|:)", text):
        return "id_answer"
    if _TIMESTAMP.search(text) and _SPACED_SEPARATOR.search(text):
        return "tracklist_like"
    return None


def _sanitise(text: str) -> str:
    original = text.strip()
    text = _URL.sub("[pointer removed]", text)
    text = _MENTION.sub("[mention]", text)
    text = _LONG_ID.sub("[id removed]", text)
    replacement_index = 0

    def replace_word(match: re.Match[str]) -> str:
        nonlocal replacement_index
        word = match.group(0)
        if word.casefold() in _KEEP_WORDS:
            return word
        replacement = "ArtistLocal" if replacement_index % 2 == 0 else "TitleLocal"
        replacement_index += 1
        return replacement

    sanitised = re.sub(r"\s+", " ", _WORD.sub(replace_word, text)).strip()
    if len(sanitised) > 500:
        sanitised = sanitised[:500].rsplit(" ", 1)[0].rstrip()
    if sanitised == original:
        first_word = _WORD.search(sanitised)
        if first_word:
            word = first_word.group(0)
            toggled = word[:1].swapcase() + word[1:]
            sanitised = f"{sanitised[: first_word.start()]}{toggled}{sanitised[first_word.end() :]}"
        else:
            sanitised = "FixtureToken"
    return sanitised


def derive_file(path: Path, destination: Path | None = None) -> tuple[Path, int]:
    authors: dict[str, str] = {}
    selected: list[tuple[Mapping[str, Any], str]] = []
    noise: list[Mapping[str, Any]] = []
    for record in _read_records(path):
        text = _body(record)
        if not text:
            continue
        category = _category(text)
        if category:
            selected.append((record, category))
        elif len(noise) < 1000:
            noise.append(record)

    noise_cap = min(15, max(3, len(selected) // 10), len(noise))
    selected = selected[: MAX_LINES_PER_SOURCE - noise_cap]
    selected.extend((record, "noise") for record in noise[:noise_cap])
    output_records: list[dict[str, Any]] = []
    for record, category in selected:
        author = _author_key(record)
        authors.setdefault(author, f"author_{len(authors) + 1:03d}")
        output_records.append(
            {
                "author": authors[author],
                "category": category,
                "position_ms": _position_ms(record),
                "text": _sanitise(_body(record)),
            }
        )

    if destination is None:
        relative = path.relative_to(RAW)
        destination = OUTPUT / relative.with_suffix(".jsonl")
    payload = b"\n".join(canonical_json_bytes(record) for record in output_records)
    if payload:
        payload += b"\n"
    atomic_write_bytes(destination, payload)
    return destination, len(output_records)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not RAW.is_dir():
        print(f"no raw comment directory at {RAW}; nothing to derive")
        return 0
    paths = [path for path in RAW.rglob("*") if path.is_file() and path.name != "README.md"]
    total = 0
    source_numbers: dict[Path, int] = {}
    for path in sorted(paths):
        relative_parent = path.relative_to(RAW).parent
        source_numbers[relative_parent] = source_numbers.get(relative_parent, 0) + 1
        safe_destination = (
            OUTPUT / relative_parent / f"source-set-{source_numbers[relative_parent]:03}.jsonl"
        )
        destination, count = derive_file(path, safe_destination)
        total += count
        print(f"{path.relative_to(ROOT)} -> {destination.relative_to(ROOT)} ({count} records)")
    print(f"derived {total} records from {len(paths)} source sets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
