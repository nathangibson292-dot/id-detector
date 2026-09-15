"""Audit committed corpus-like files for identifiers copied from local raw dumps."""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from id_detector.truth import TRUTH_RECORD_NAME, is_corpus_directory, open_corpus
from id_detector.truth_paths import is_link

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
    Path("docs/PLAN-v2.md"),  # cites vendor endpoints (e.g. the AudD terms-version URL)
    Path("data/fixtures/README.md"),
}


def _pattern_exempt(relative: Path) -> bool:
    return (
        relative in _HISTORICAL_PATTERN_EXEMPT
        or relative.is_relative_to(Path("docs/research"))
        or relative.is_relative_to(Path("docs/reviews"))
        or relative.is_relative_to(Path("docs/legal"))  # verbatim vendor terms + assessments
    )


def _has_binary_content(path: Path) -> bool:
    """Detect binary fixtures without granting any filename extension an audit exemption."""

    with path.open("rb") as handle:
        return b"\x00" in handle.read(8192)


# Stage 6 per-path allow rule.  The committed enrichment artefact `enrich/acquire.json` and the
# `present/tracklist.{json,md}` exports (and their `tests/golden/` fixtures) legitimately carry
# **public catalogue item URLs** — Deezer/Apple/MusicBrainz/Discogs/SoundCloud/Bandcamp/Beatport/
# Traxsource pages and download-gate links.  These are *acquisition targets*, not personal data, so
# on these paths only the URL and long-numeric-id pattern checks are relaxed, and every URL must
# still resolve to a known acquisition host below.  The handle/username, raw-dump-line, raw-dump-
# string, and identifier-field checks stay in force here exactly as everywhere else.
_ACQUISITION_NAMES = frozenset({"acquire.json", "tracklist.json", "tracklist.md"})
_ACQUISITION_PARENTS = frozenset({"enrich", "present", "golden"})
_CATALOGUE_HOSTS = frozenset(
    {
        "deezer.com",
        "apple.com",
        "musicbrainz.org",
        "discogs.com",
        "soundcloud.com",
        "sndcdn.com",
        "bandcamp.com",
        "beatport.com",
        "traxsource.com",
        "hypeddit.com",
        "hypeddit.co",
        "bettergate.com",
        "timbrgate.com",
        "backstaged.com",
        "backstaged.io",
        "fangate.eu",
        "stillhype.com",
        "toneden.io",
        "theartistunion.com",
        "gate.fm",
    }
)


def _acquisition_artifact(relative: Path) -> bool:
    return relative.name in _ACQUISITION_NAMES and relative.parent.name in _ACQUISITION_PARENTS


def _catalogue_host(host: str) -> bool:
    host = host.casefold()
    registrable = ".".join(host.split(".")[-2:])
    return registrable in _CATALOGUE_HOSTS


def _non_catalogue_urls(text: str) -> list[str]:
    offenders: list[str] = []
    for match in _URL.finditer(text):
        raw = match.group(0).strip("\",'")
        candidate = raw if "://" in raw else f"https://{raw}"
        host = urlsplit(candidate).hostname or ""
        if not host or not _catalogue_host(host):
            offenders.append(raw)
    return offenders


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


#: Tracklist furniture must never survive seeding into a committed truth record.  A row like
#: "0:17:09 - Royal-T - Tokyo Dub" carries a list marker after the timestamp; the seed parser strips
#: it (`truth._TRACKLIST`: an optional time, then a marker *followed by whitespace*), but a record
#: seeded by an older parser keeps it, and the owner then reviews 25 rows whose artist reads
#: "- Mall Grab".
#:
#: The check follows that grammar rather than a first-glyph test.  A marker is furniture only
#: where the parser would have consumed it — standing alone, separated from the name by
#: whitespace — so real names that *start* with the same glyph (``*NSYNC``, ``-M-``, ``-``) pass.
#: Before matching, the label is read the way a person sees it: leading whitespace and invisible
#: format characters (a BOM, zero-width spaces and joiners) are skipped, compatibility forms are
#: folded (NFKC: fullwidth and small hyphens, no-break spaces), and every dash, minus and bullet
#: variant becomes one marker.  An invisible leading character is reported on its own, because
#: it is never what the owner transcribed.
_DASH_AND_BULLET_VARIANTS = {
    ord(character): "-"
    for character in (
        "‐‑‒–—―⁃−﹘﹣－"  # dashes, minus
        "•‣∙▪●◦·・･*"  # bullets and asterisk
    )
}
_SEED_FURNITURE = re.compile(r"^(?:\d+(?::\d{1,2}){1,2}\s+)?-\s+\S")


def _label_defect(value: str) -> str | None:
    """Why a human-read truth label is not what the owner transcribed, or ``None``.

    Invisible format characters (Unicode ``Cf``) are removed from the *whole* label before the
    grammar is matched, so one hidden between a timestamp, a marker and its whitespace
    (``"-\\u200b Mall Grab"``) cannot disguise furniture.  One that appears before the first
    letter is also reported in its own right; a joiner inside a name (after its first letter) is
    left alone.
    """

    categories = [unicodedata.category(character) for character in value]
    first_letter = next(
        (index for index, category in enumerate(categories) if category[0] == "L"), len(value)
    )
    invisible = "Cf" in categories[:first_letter]
    visible = "".join(ch for ch in value if unicodedata.category(ch) != "Cf").lstrip()
    readable = unicodedata.normalize("NFKC", visible).translate(_DASH_AND_BULLET_VARIANTS)
    furniture = _SEED_FURNITURE.match(readable) is not None
    if furniture and invisible:
        return "keeps tracklist furniture behind an invisible leading character"
    if furniture:
        return "keeps tracklist furniture"
    if invisible:
        return "starts with an invisible character"
    return None


def _audit_truth_record(relative: Path, text: str) -> list[str]:
    """Ground-truth records must not carry seed furniture in the fields a human reads."""

    if relative.name != TRUTH_RECORD_NAME:
        return []
    try:
        record = json.loads(text)
    except ValueError:
        return [f"{relative}: is not valid JSON"]
    failures: list[str] = []
    for index, episode in enumerate(record.get("episodes") or []):
        work = episode.get("work") or {}
        for field in ("artist", "title"):
            value = work.get(field)
            defect = _label_defect(value) if isinstance(value, str) else None
            if defect is not None:
                failures.append(f"{relative}: episode {index} {field} {defect}: {value[:40]!r}")
    return failures


def _audited_files(scan_root: Path) -> tuple[list[Path], list[Path]]:
    """Every regular file under ``scan_root`` to audit, and every link that was refused.

    A corpus directory -- one holding corpus-version.json or a set directory with ground_truth.json
    -- is read only through the corpus gateway, ``open_corpus(..., mutate=False)``: its vetted truth
    files plus the other files of the tree the gateway validated.  Any other, non-corpus fixture
    folder is walked directly with ``os.scandir``, refusing every symlink or junction.
    """

    files: list[Path] = []
    links: list[Path] = []

    def walk(directory: Path) -> None:
        if is_corpus_directory(directory):
            with open_corpus(directory, mutate=False, require_records=False) as handle:
                vetted = set(handle.truth_files)
                files.extend(handle.truth_files)
                files.extend(path for path in handle.files if path not in vetted)
            return
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            child = directory / entry.name
            if is_link(child):
                links.append(child)
            elif entry.is_dir(follow_symlinks=False):
                walk(child)
            elif entry.is_file(follow_symlinks=False):
                files.append(child)

    walk(scan_root)
    return sorted(files), links


def audit() -> list[str]:
    failures: list[str] = []
    raw_fragments = _raw_fragments()
    scanned = 0
    for scan_root in SCAN_ROOTS:
        if not scan_root.exists():
            continue
        files, links = _audited_files(scan_root)
        failures.extend(
            f"{link.relative_to(ROOT)}: is a symlink or junction; the audit refuses links "
            "(pass the resolved real path instead)"
            for link in links
        )
        for path in files:
            scanned += 1
            relative = path.relative_to(ROOT)
            if not _pattern_exempt(relative) and re.search(r"\d{6,}", relative.as_posix()):
                failures.append(f"{relative}: filename contains a numeric platform ID")
            if _has_binary_content(path):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if relative.is_relative_to(_DERIVED_PATH):
                failures.extend(_audit_derived(relative, text))
            failures.extend(_audit_truth_record(relative, text))
            if not _pattern_exempt(relative):
                acquisition = _acquisition_artifact(relative)
                # `enrich/acquire.json` and the tracklist exports may carry public catalogue URLs
                # (acquisition targets, not personal data); there the URL and long-numeric-id checks
                # are replaced by a catalogue-host allowlist. Handle/username and identifier-field
                # checks stay in force on every path.
                checks = [("handle", _HANDLE), ("numeric identifier field", _ID_FIELD)]
                if not acquisition:
                    checks += [("URL", _URL), ("long numeric platform ID", _LONG_NUMERIC_ID)]
                for label, pattern in checks:
                    match = pattern.search(text)
                    if match:
                        line = text.count("\n", 0, match.start()) + 1
                        failures.append(f"{relative}:{line}: contains {label}")
                if acquisition:
                    for offender in _non_catalogue_urls(text):
                        failures.append(
                            f"{relative}: contains a non-catalogue URL {offender[:60]!r}"
                        )
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
