"""Audit committed corpus-like files for identifiers copied from local raw dumps."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    ROOT / "data" / "corpus",
    ROOT / "data" / "fixtures",
    ROOT / "docs",
    ROOT / "tests" / "fixtures",
    ROOT / "tests" / "golden",
)
RAW_ROOT = ROOT / "data" / "raw" / "comments"

_HANDLE = re.compile(r"(?<!\w)@[A-Za-z0-9_]\w*")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
# Durations and millisecond positions are legitimate. Platform identifiers in the source dumps
# are substantially longer; contextual *_id checks below catch shorter identifier fields.
_LONG_NUMERIC_ID = re.compile(r"(?<![A-Za-z0-9\"'])\d{10,}(?![A-Za-z0-9\"'])")
_ID_FIELD = re.compile(
    r'(?i)["\'](?:user|author|comment|platform|track)_?id["\']\s*:\s*["\']?\d{6,}'
)
_DERIVED_PATH = Path("data/fixtures/hints/derived")
_DERIVED_NAME = re.compile(r"^source-set-\d{3}\.jsonl$")
_DERIVED_AUTHOR = re.compile(r"^author_\d{3,}$")
_WORD = re.compile(r"[^\W\d_]+(?:['.’][^\W\d_]+)*", re.UNICODE)
_DERIVED_FIELDS = {"author", "category", "position_ms", "text"}
_DERIVED_CATEGORIES = {
    "tracklist_like",
    "id_question",
    "correction",
    "id_answer",
    "noise",
}
_DERIVED_WORDS = {
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
    "artistlocal",
    "titlelocal",
    "pointer",
    "removed",
    "mention",
    "fixturetoken",
}

# These immutable pre-Stage-0 documents intentionally contain research citations, endpoint URLs,
# and quoted examples. They are still read for raw-line comparison, but leak-pattern rules target
# newly generated schemas/reports/goldens and committed fixtures.
_HISTORICAL_PATTERN_EXEMPT = {
    Path("docs/PLAN.md"),
    Path("data/fixtures/README.md"),
}


def _pattern_exempt(relative: Path) -> bool:
    return (
        relative in _HISTORICAL_PATTERN_EXEMPT
        or relative.is_relative_to(Path("docs/research"))
        or relative.is_relative_to(Path("docs/reviews"))
    )


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value.strip()
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _raw_fragments() -> set[str]:
    fragments: set[str] = set()
    if not RAW_ROOT.is_dir():
        return fragments
    for path in RAW_ROOT.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        fragments.update(line.strip() for line in text.splitlines() if len(line.strip()) >= 16)
        try:
            fragments.update(item for item in _strings(json.loads(text)) if len(item) >= 16)
        except json.JSONDecodeError:
            for line in text.splitlines():
                try:
                    fragments.update(item for item in _strings(json.loads(line)) if len(item) >= 16)
                except json.JSONDecodeError:
                    continue
    return fragments


def _json_values(text: str) -> list[Any]:
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        values: list[Any] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return values


def _identifier_paths(value: Any, path: str = "$") -> Iterator[str]:
    """Find raw-source identifier fields by context, including opaque string values."""

    if isinstance(value, dict):
        for key, item in value.items():
            normalised = re.sub(r"[^a-z]", "", str(key).casefold())
            is_raw_identifier = normalised in {
                "platformid",
                "userid",
                "uploaderid",
                "authorid",
                "commentid",
                "sourceid",
                "sourcerecordid",
            }
            # In JSON Schemas the value of a named property is itself a schema object, not an
            # identifier. Real artefacts use scalar identifier values.
            if is_raw_identifier and item is not None and not isinstance(item, dict):
                yield f"{path}.{key}"
            yield from _identifier_paths(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _identifier_paths(item, f"{path}[{index}]")


def _audit_derived(relative: Path, text: str) -> list[str]:
    failures: list[str] = []
    if not _DERIVED_NAME.fullmatch(relative.name):
        failures.append(f"{relative}: derived filename does not use source-set-NNN.jsonl")
    authors: list[str] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            failures.append(f"{relative}:{line_number}: derived line is not valid JSON")
            continue
        if not isinstance(record, dict) or set(record) != _DERIVED_FIELDS:
            failures.append(f"{relative}:{line_number}: derived record has an invalid shape")
            continue
        author = record["author"]
        if not isinstance(author, str) or not _DERIVED_AUTHOR.fullmatch(author):
            failures.append(f"{relative}:{line_number}: invalid fixture-local author token")
        elif author not in authors:
            authors.append(author)
        if record["category"] not in _DERIVED_CATEGORIES:
            failures.append(f"{relative}:{line_number}: invalid derived category")
        position = record["position_ms"]
        if position is not None and (
            isinstance(position, bool)
            or not isinstance(position, int)
            or position < 0
            or position % 1000
        ):
            failures.append(f"{relative}:{line_number}: invalid derived position_ms")
        fixture_text = record["text"]
        if not isinstance(fixture_text, str) or not 0 < len(fixture_text) <= 500:
            failures.append(f"{relative}:{line_number}: invalid derived text")
            continue
        unexpected = sorted(
            {word.casefold() for word in _WORD.findall(fixture_text)} - _DERIVED_WORDS
        )
        if unexpected:
            failures.append(
                f"{relative}:{line_number}: derived text is outside the safe vocabulary"
            )
    expected_authors = [f"author_{index:03d}" for index in range(1, len(authors) + 1)]
    if authors != expected_authors:
        failures.append(f"{relative}: author tokens are not sequential in first-seen order")
    return failures


def audit() -> list[str]:
    failures: list[str] = []
    raw_fragments = _raw_fragments()
    scanned = 0
    for scan_root in SCAN_ROOTS:
        if not scan_root.exists():
            continue
        for path in sorted(item for item in scan_root.rglob("*") if item.is_file()):
            scanned += 1
            relative = path.relative_to(ROOT)
            text = path.read_text(encoding="utf-8", errors="replace")
            if relative.is_relative_to(_DERIVED_PATH):
                failures.extend(_audit_derived(relative, text))
            if not _pattern_exempt(relative):
                if re.search(r"\d{6,}", relative.as_posix()):
                    failures.append(f"{relative}: filename contains a numeric platform ID")
                for label, pattern in (
                    ("handle", _HANDLE),
                    ("URL", _URL),
                    ("long numeric platform ID", _LONG_NUMERIC_ID),
                    ("numeric identifier field", _ID_FIELD),
                ):
                    match = pattern.search(text)
                    if match:
                        line = text.count("\n", 0, match.start()) + 1
                        failures.append(f"{relative}:{line}: contains {label}")
            for line_number, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if len(stripped) >= 16 and stripped in raw_fragments:
                    failures.append(f"{relative}:{line_number}: matches a raw-dump line verbatim")
            parsed = _json_values(text)
            for value in parsed:
                for identifier_path in _identifier_paths(value):
                    failures.append(
                        f"{relative}: contains non-null raw identifier field at {identifier_path}"
                    )
            values = {item for value in parsed for item in _strings(value)}
            for value in values & raw_fragments:
                failures.append(f"{relative}: contains a raw-dump string verbatim: {value[:40]!r}")
    print(f"audited {scanned} files")
    return sorted(set(failures))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failures = audit()
    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        print(f"fixture audit failed with {len(failures)} finding(s)")
        return 1
    print("fixture audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
