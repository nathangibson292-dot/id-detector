"""Draft, independent annotation, resolution, and corpus-freeze tooling for benchmark truth."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from id_detector.contracts import CERTIFICATION_DISABLED, GroundTruthRecord, TruthRoleSegment
from id_detector.io import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    native_path,
    path_is_file,
    read_bytes,
    read_text,
    sha256_file,
)
from id_detector.truth_paths import (
    PinnedDirectory,
    is_link,
    is_within,
    link_refusal,
    nearest_existing_ancestor,
    path_key,
    pinned_set_directory,
    real_path,
    refuse_link_components,
    strip_namespace_prefix,
)

Input = Callable[[str], str]
Output = Callable[[str], None]
# "H:MM:SS Artist - Title", optionally bulleted after the time ("0:17:09 - Artist - Title"): the
# bullet is list furniture, never the first character of the artist.
_TRACKLIST = re.compile(
    r"^\s*(?:(?P<time>\d+(?::\d{1,2}){1,2})\s+)?(?:[-\u2013\u2014\u2022]\s+)?"
    r"(?P<artist>.+?)\s+-\s+(?P<title>.+?)\s*$"
)
#: The marker an overlays file may carry on each line ("... - Title (w/ overlay)").
_OVERLAY_SUFFIX = re.compile(r"(?i)\s*\(\s*w/\s*overlay\s*\)\s*$")


def _parse_time(value: str) -> int:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        if seconds > 59:
            raise ValueError(f"invalid timestamp: {value}")
        return (minutes * 60 + seconds) * 1000
    if len(parts) == 3:
        hours, minutes, seconds = parts
        if minutes > 59 or seconds > 59:
            raise ValueError(f"invalid timestamp: {value}")
        return (hours * 3600 + minutes * 60 + seconds) * 1000
    raise ValueError(f"invalid timestamp: {value}")


def _read_json_or_jsonl(path: Path) -> list[dict[str, Any]]:
    text = read_text(path)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("episodes", "hints", "tracks"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    raise ValueError(f"unsupported hints shape in {path}")


def _seed_entries(hints: Path | None, tracklist: Path | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if hints is not None:
        for item in _read_json_or_jsonl(hints):
            artist = item.get("artist") or item.get("work", {}).get("artist")
            title = item.get("title") or item.get("work", {}).get("title")
            if not artist or not title:
                continue
            position = item.get("position_range_ms") or item.get("start_ms_range")
            entries.append(
                {
                    "artist": str(artist),
                    "title": str(title),
                    "version_qualifier": item.get("version_qualifier"),
                    "position": list(position) if position is not None else None,
                }
            )
    if tracklist is not None:
        entries.extend(_tracklist_entries(tracklist, what="manual tracklist"))
    return _unique_entries(entries)


def _tracklist_entries(path: Path, *, what: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(read_text(path).splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _TRACKLIST.match(line)
        if not match:
            raise ValueError(f"invalid {what} line {line_number}: {line}")
        at_ms = _parse_time(match.group("time")) if match.group("time") else None
        entries.append(
            {
                "artist": match.group("artist"),
                "title": match.group("title"),
                "version_qualifier": None,
                "position": [at_ms, at_ms] if at_ms is not None else None,
                "line": line_number,
            }
        )
    return entries


def _unique_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | None]] = set()
    for item in entries:
        start = item["position"][0] if item["position"] else None
        key = (item["artist"].casefold(), item["title"].casefold(), start)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _overlay_entries(overlays: Path) -> list[dict[str, Any]]:
    """Lines ``H:MM:SS - Artist - Title (w/ overlay)`` (the suffix optional); every one timed."""

    entries = []
    for item in _tracklist_entries(overlays, what="overlays"):
        if item["position"] is None:
            raise ValueError(
                f"overlays line {item['line']} needs a timestamp: an overlay is placed by the "
                "time it was blended in"
            )
        entries.append({**item, "title": _OVERLAY_SUFFIX.sub("", item["title"]).strip()})
    return _unique_entries(entries)


def _format_ms(value: int) -> str:
    seconds, milliseconds = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" + (f".{milliseconds:03d}" if milliseconds else "")


def _write_source_links(project_root: Path, links: dict[str, str]) -> None:
    if not links:
        return
    path = project_root / "data" / "local" / "source_links.json"
    existing: dict[str, str] = {}
    if path.exists():
        existing = json.loads(read_text(path))
    existing.update(links)
    atomic_write_json(path, existing)


def seed_truth(
    *,
    out_path: Path,
    work_root: Path | None = None,
    corpus_root: Path | None = None,
    timeout: float = 20.0,
    **options: Any,
) -> GroundTruthRecord:
    """Seed draft truth into ``<corpus>/<set>/ground_truth.json`` under the corpus mutation lock.

    Links and unsupported layouts are refused before anything is read; every other predicate
    (existing records, passes, exposure, recovery files) is checked inside the lock, together with
    the write, so a concurrent reveal or save in the same corpus cannot slip in between.
    """

    # Exact depth: the root is exactly out_path's grandparent (or the explicit corpus_root, which
    # must equal it), and it must pass the gateway -- so a seed at
    # release-1/new-group/new-set/ground_truth.json is refused as inside another corpus rather
    # than locked and written on release-1/new-group.
    root = exact_corpus_root(out_path, corpus_root=corpus_root, name=TRUTH_RECORD_NAME)
    # One machine-wide seed lock is taken before any check: two mistyped, overlapping seeds (such
    # as <C>/a/ground_truth.json and <C>/a/b/ground_truth.json) have different corpus roots and
    # so different corpus locks, but they share this one, so the second only validates after the
    # first has written -- and is then refused by the layout and ancestry checks.
    with _held_lock(SEED_LOCK_KEY, timeout):
        # Seed creates only the set directory: the corpus root must already exist.  A mistyped,
        # deeper path therefore cannot quietly start a new corpus inside another, without any
        # ancestor listing, and a corpus is always created deliberately.
        refuse_corpus_ancestry(root, work_root=work_root)
        if not os.path.isdir(native_path(root)):
            raise ValueError(
                f"refusing to seed {out_path}: its corpus directory {root} does not exist. "
                "Seeding never creates a corpus directory: to start a new corpus, create a new "
                "corpus folder outside any existing corpus (never a folder inside another "
                f"corpus) and seed into that; {SUPPORTED_LAYOUT}"
            )
        with open_corpus(
            root, mutate=True, work_root=work_root, require_records=False, timeout=timeout
        ):
            refuse_unsupported_seed_destination(Path(out_path))
            return _seed_truth_locked(out_path=out_path, work_root=work_root, **options)


def _seed_truth_locked(
    *,
    out_path: Path,
    set_id: str,
    duration_ms: int,
    media_key: str,
    hints: Path | None = None,
    tracklist: Path | None = None,
    overlays: Path | None = None,
    split: str = "dev-1",
    stratum: str = "catalogue-covered",
    corpus_version: str = "draft",
    platform: str = "local",
    selection_basis: str = "manual seed assembled before scoring",
    source_url: str | None = None,
    uploader: str | None = None,
    event: str | None = None,
    project_root: Path | None = None,
    work_root: Path | None = None,
) -> GroundTruthRecord:
    # Re-running a seed is the likeliest accident of all: never overwrite a record that may since
    # have been reviewed or frozen, nor seed beside passes or evidence left from an earlier set.
    # Runs inside the corpus mutation lock (see seed_truth), before anything is read or written.
    refuse_link_components(out_path)
    present = [
        name
        for name in set_record_names(Path(out_path))
        if os.path.lexists(Path(out_path).with_name(name))
    ]
    if present:
        raise ValueError(
            f"refusing to seed {out_path}: {', '.join(present)} already exists there. Seeding "
            "never overwrites a truth set, which may already be reviewed or frozen; seed into a "
            "new set directory, or remove the old one deliberately first"
        )
    entries = _seed_entries(hints, tracklist)
    if not entries:
        raise ValueError("seed needs at least one usable hint or manual tracklist entry")
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    has_timed = any(item["position"] is not None for item in entries)
    if has_timed and any(item["position"] is None for item in entries):
        raise ValueError("mixed timed and untimed seeds require an explicit cue for every entry")
    overlay_entries = _overlay_entries(overlays) if overlays is not None else []
    if overlay_entries and not has_timed:
        raise ValueError(
            "overlays need a timestamped tracklist: an overlay is attached to the row playing at "
            "its time, which placeholder equal-slice timings cannot say"
        )
    entries.sort(key=lambda item: item["position"][0] if item["position"] else duration_ms + 1)
    positions: list[list[int]] = []
    for index, item in enumerate(entries):
        if item["position"] is not None:
            position = [
                max(0, int(item["position"][0])),
                min(duration_ms, int(item["position"][1])),
            ]
        else:
            point = index * duration_ms // len(entries)
            position = [point, point]
        positions.append(position)
    # Each row ends where the next begins.  An overlay ("w/": a second track blended in at the same
    # time) cannot be a row of its own in that chain — it would end the row it plays over — so it
    # is attached to the row playing at its timestamp and given that row's end.
    attached: dict[int, list[dict[str, Any]]] = {}
    # Sorted by time (stable, so a file already in order seeds byte-identically): two overlays of
    # the same row must still land in the episode list in the order they were blended in.
    for item in sorted(overlay_entries, key=lambda item: item["position"][0]):
        at_ms = min(duration_ms, max(0, int(item["position"][0])))
        rows = [index for index, position in enumerate(positions) if position[0] <= at_ms]
        if not rows:
            raise ValueError(
                f"overlay at {_format_ms(at_ms)} (line {item['line']}) precedes the first "
                "tracklist row"
            )
        if at_ms >= duration_ms:
            raise ValueError(
                f"overlay at {_format_ms(at_ms)} (line {item['line']}) lies at or beyond the end "
                "of the mix"
            )
        attached.setdefault(rows[-1], []).append({**item, "position": [at_ms, at_ms]})
    occurrences: dict[tuple[str, str], int] = {}

    def _episode(
        item: dict[str, Any], start_range: list[int], end_range: list[int], role: str
    ) -> dict[str, Any]:
        work_identity = (item["artist"].casefold(), item["title"].casefold())
        occurrence_index = occurrences.get(work_identity, 0)
        occurrences[work_identity] = occurrence_index + 1
        return {
            "work": {"artist": item["artist"], "title": item["title"]},
            "version": {"qualifier": item["version_qualifier"], "ids": {}},
            "version_verified": False,
            "verified_against": None,
            "start_ms_range": start_range,
            "end_ms_range": end_range,
            "audible_rule": "manual annotation required",
            "role_segments": [{"from_ms": start_range[0], "to_ms": end_range[1], "role": role}],
            "overlaps_with": [],
            "occurrence_index": occurrence_index,
            "in_reference_pool": False,
            "annotator_ref": None,
            "second_pass_ref": None,
            "disagreement_resolution": None,
            "note": None,
            "draft": True,
        }

    episodes: list[dict[str, Any]] = []
    for index, (item, start_range) in enumerate(zip(entries, positions, strict=True)):
        end_range = (
            positions[index + 1] if index + 1 < len(positions) else [duration_ms, duration_ms]
        )
        row = _episode(item, start_range, end_range, "uncertain")
        episodes.append(row)
        row_index = len(episodes) - 1
        for overlay in attached.get(index, []):
            layer = _episode(overlay, overlay["position"], end_range, "layer")
            layer["note"] = (
                "seeded as an overlay ('w/'): blended in at this time over the row before it; "
                "its end is assumed to be that row's end"
            )
            layer["overlaps_with"] = [row_index]
            episodes.append(layer)
            row["overlaps_with"].append(len(episodes) - 1)
    source_ref = f"source-{set_id}"
    uploader_ref = f"uploader-{set_id}"
    event_ref = f"event-{set_id}" if event else None
    truth = GroundTruthRecord(
        schema_version="1.0.0",
        generated_by="id-detector/0.1.0",
        set_id=set_id,
        source={
            "url_ref": source_ref,
            "media_key": media_key,
            "duration_ms": duration_ms,
            "platform": platform,
            "uploader_ref": uploader_ref,
            "event_ref": event_ref,
            "date": None,
        },
        stratum=stratum,
        split=split,
        corpus_version=corpus_version,
        selection_basis=selection_basis,
        episodes=episodes,
        events=[],
        regions=[],
    )
    # Re-check the layout immediately before the write, still under the corpus lock, scoped to this
    # set's own directory (not the whole corpus): this catches a concurrent seed on an overlapping
    # root whose write landed after this one's pre-checks.  An outer seed sees a nested truth inside
    # its set directory; a nested seed sees its set's parent become a single-set root
    # (:func:`refuse_unsupported_seed_destination`).
    refuse_unsupported_seed_destination(out_path)
    refuse_corpus_ancestry(exact_corpus_root(out_path), work_root=work_root)
    destination = Path(os.path.abspath(out_path))
    nested = [gt for gt in _scan_corpus_tree(destination.parent) if gt != destination]
    if nested:
        raise ValueError(
            f"refusing to seed {out_path}: {nested[0]} is a truth file nested inside the set "
            f"directory; {SUPPORTED_LAYOUT}"
        )
    # Through the pinned, link-refusing writer: a destination that is a link (possibly into work/)
    # is refused, never followed and overwritten.
    _write_record_file(out_path, canonical_json_bytes(truth), work_root=work_root, create_only=True)
    links = {
        key: value
        for key, value in (
            (source_ref, source_url),
            (uploader_ref, uploader),
            (event_ref, event),
        )
        if key is not None and value is not None
    }
    _write_source_links(project_root or Path.cwd(), links)
    return truth


def _parse_range(value: str, current: tuple[int, int] | None = None) -> list[int]:
    if not value.strip() and current is not None:
        return list(current)
    parts = [part.strip() for part in value.replace("-", ",").split(",")]
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise ValueError("range must be START_MS,END_MS")
    result = [int(parts[0]), int(parts[1])]
    if result[1] < result[0]:
        raise ValueError("range end must not precede start")
    return result


def _ffplay_command(audio: Path | None, start_ms: int | None, duration_ms: int | None) -> str:
    args = ["ffplay", "-nodisp", "-autoexit"]
    if start_ms is not None:
        args.extend(("-ss", f"{start_ms / 1000:g}"))
    if duration_ms is not None:
        args.extend(("-t", f"{duration_ms / 1000:g}"))
    args.append(str(audio) if audio is not None else "MIX_AUDIO_FILE")
    return subprocess.list2cmdline(args)


def _annotation_path(truth_path: Path, pass_name: str) -> Path:
    return truth_path.with_name(f"annotation-{pass_name}.json")


_ANNOTATION_NAMES = frozenset(
    f"annotation-{name}.json" for name in ("first", "second", "resolution")
)
EXPOSURE_NAME = "review-exposure.json"
FREEZE_MANIFEST_NAME = "corpus-version.json"
#: The one name a set's truth record has: ``<corpus>/<set>/ground_truth.json``.
TRUTH_RECORD_NAME = "ground_truth.json"

#: The owner's freeze and certification moratorium: one gate, ``False`` in production.  Every
#: freeze and certification emitter reads it through :func:`certification_enabled` at call time;
#: tests of the logic underneath open it with the ``certification_gate_open`` fixture (a
#: monkeypatch), never a production flag, and tests of the closed state may pin it with
#: ``certification_gate_closed``.  :data:`CERTIFICATION_DISABLED` is the exact message.  The
#: certification follow-up closed the two deferred scope defects but its review found more; the
#: gate stays closed until everything under "Required before the certification gate may open" in
#: ``docs/reviews/followup-certification.md`` is built.
CERTIFICATION_ENABLED = False

#: What a closed gate tells a person, after :data:`CERTIFICATION_DISABLED`.
CERTIFICATION_DISABLED_NEXT_STEP = (
    "Nothing is wrong and there is nothing to fix on your side: freezing and certifying stay "
    "switched off until the remaining certification work is built (it is listed in "
    "docs/reviews/followup-certification.md). Scoring draft truth with scripts/score_corpus.py "
    "still works and is how accuracy is measured meanwhile"
)


def certification_enabled() -> bool:
    """Whether freezing and certification are allowed (see :data:`CERTIFICATION_ENABLED`)."""

    return CERTIFICATION_ENABLED


def certifiable_under_gate(independent: bool) -> bool:
    """A ``certifiable`` flag: never true while certification is disabled."""

    return certification_enabled() and independent


def write_corpus_file_through_gateway(path: Path, value: Any) -> None:
    """Write a corpus file into a directory the corpus gateway already validated.

    The one exemption from ``io``'s corpus backstop, for corpora this tool builds itself: the
    controlled renderer's staging corpus (published through ``open_corpus``) and calibration
    validation's scratch corpus.  Owner truth is written by the pinned writer, never here.
    """

    atomic_write_bytes(Path(path), canonical_json_bytes(value), through_corpus_gateway=True)


def exposure_path(truth_path: Path) -> Path:
    """The durable record that IDea's own answers were shown while this set was reviewed."""

    return truth_path.with_name(EXPOSURE_NAME)


def transaction_path(truth_path: Path) -> Path:
    """The interrupted-write record that exists only while a truth/annotation pair is replaced."""

    return truth_path.with_name(_transaction_name(truth_path.name))


def _transaction_name(truth_name: str) -> str:
    return f"{Path(truth_name).stem}.transaction.json"


def set_record_names(truth_path: Path) -> tuple[str, ...]:
    """Every file name this tooling reads or writes in a truth record's set directory."""

    return (
        truth_path.name,
        *sorted(_ANNOTATION_NAMES),
        EXPOSURE_NAME,
        _transaction_name(truth_path.name),
    )


#: The corpus-level, append-only second record of exposure.  It lives in the corpus directory (the
#: set directory's parent, beside the corpus manifest), not in the set directory, so tidying one
#: set folder -- deleting its new, still-untracked ``review-exposure.json`` -- cannot erase the fact
#: that IDea's predictions were shown.  The tool only ever appends to it.
EXPOSURE_LEDGER_NAME = "review-exposure-ledger.jsonl"


def exposure_ledger_path(truth_path: Path) -> Path:
    return truth_path.parent.parent / EXPOSURE_LEDGER_NAME


def _set_id_of(truth_path: Path) -> str | None:
    try:
        payload = json.loads(read_text(truth_path))
    except (OSError, ValueError):
        return None
    value = payload.get("set_id") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def ledger_entry_digests(raw: bytes, *, set_id: str | None, set_directory: str) -> list[str]:
    """SHA-256 of each ledger line recording a reveal for this set (by ``set_id`` or folder name).

    An unreadable line is never proof of independence: it is ignored only if it cannot name any
    set, so a damaged ledger cannot silently hide an exposure it still visibly records.
    """

    digests: list[str] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            entry = json.loads(text.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if (set_id and set_id.encode() in text) or set_directory.encode() in text:
                digests.append(sha256(text).hexdigest())
            continue
        if isinstance(entry, dict) and (
            (set_id is not None and entry.get("set_id") == set_id)
            or entry.get("set_directory") == set_directory
        ):
            digests.append(sha256(text).hexdigest())
    return sorted(set(digests))


def ledger_exposure(truth_path: Path) -> list[str]:
    """Digests of the ledger entries that record a reveal for ``truth_path``'s set."""

    ledger = exposure_ledger_path(truth_path)
    if is_link(ledger):
        raise link_refusal(ledger, ledger)
    if not path_is_file(ledger):
        return []
    return ledger_entry_digests(
        read_bytes(ledger), set_id=_set_id_of(truth_path), set_directory=truth_path.parent.name
    )


def record_exposure_in_ledger(
    truth_path: Path, *, set_id: str, when: str, work_root: Path | None = None
) -> None:
    """Append this set's reveal to the corpus ledger unless an entry is already there.

    Written through the pinned corpus directory under the corpus mutation lock (reentrant for a
    reveal that already holds it); the new content must extend the old byte-for-byte, so the
    ledger can only grow.
    """

    corpus_dir = exact_corpus_root(truth_path)
    with (
        # A frozen corpus is terminal: its ledger is bound to the freeze manifest, so the gateway
        # refuses the append (the review route also refuses a frozen reveal before predictions).
        open_corpus(corpus_dir, mutate=True, work_root=work_root, require_records=False),
        # The pin compares the opened directory's real, prefix-free location with this identity,
        # so it must be the canonical path_key: a namespace-prefixed spelling is the same pin.
        pinned_set_directory(
            corpus_dir, expected_key=path_key(corpus_dir), work_root=work_root
        ) as pinned,
    ):
        previous = pinned.read_bytes(EXPOSURE_LEDGER_NAME) or b""
        if ledger_entry_digests(previous, set_id=set_id, set_directory=truth_path.parent.name):
            return
        _ledger_append_window(corpus_dir)  # between this read and the write below
        line = canonical_json_bytes(
            {
                "schema_version": "1.0.0",
                "event": "predictions_revealed",
                "set_id": set_id,
                "set_directory": truth_path.parent.name,
                "revealed_at_utc": when,
                "tool": "idea truth review",
            }
        )
        prefix = previous if not previous or previous.endswith(b"\n") else previous + b"\n"
        pinned.write_bytes(EXPOSURE_LEDGER_NAME, prefix + line + b"\n")


def prediction_exposure(truth_path: Path) -> dict[str, Any]:
    """What is recorded about whether IDea's predictions were visible to this set's annotators.

    Three records can say so, and any one is enough:

    * the ``review-exposure.json`` sidecar, written the instant predictions are revealed (its mere
      existence counts);
    * any annotation pass's ``review_provenance``, written on save and carried forward;
    * the corpus-level ``review-exposure-ledger.jsonl``, appended on reveal, which survives the
      sidecar being tidied away before the first save.

    ``evidence`` names each set file carrying the flag with its SHA-256, and ``ledger_entries``
    the digests of this set's ledger lines, so a freeze can record both in the manifest.  A record
    that is a link is refused rather than followed.
    """

    sidecar = exposure_path(truth_path)
    annotations = [_annotation_path(truth_path, name) for name in ("first", "second", "resolution")]
    for candidate in (sidecar, *annotations):
        if is_link(candidate):
            raise link_refusal(candidate, candidate)
    evidence: dict[str, str] = {}
    if path_is_file(sidecar):
        evidence[sidecar.name] = sha256_file(sidecar)
    for annotation in annotations:
        if not path_is_file(annotation):
            continue
        try:
            payload = json.loads(read_text(annotation))
        except ValueError:
            payload = None
        provenance = payload.get("review_provenance") if isinstance(payload, dict) else None
        if isinstance(provenance, dict) and provenance.get("predictions_visible_during_review"):
            evidence[annotation.name] = sha256_file(annotation)
    ledger_entries = ledger_exposure(truth_path)
    return {
        "predictions_visible_during_review": bool(evidence or ledger_entries),
        "evidence": dict(sorted(evidence.items())),
        "ledger_entries": ledger_entries,
    }


def record_path(path: Path) -> Path:
    """A truth record's location with its directories resolved but the record itself not followed.

    A record that is a symlink or junction is refused outright: writing through it would replace
    whatever it points at (possibly beneath ``work/``), and reading through it would judge a file
    that is not in the set at all.
    """

    refuse_link_components(path)
    absolute = Path(os.path.abspath(path))
    return real_path(absolute.parent) / absolute.name


def manifest_search_directories(truth_file: Path) -> list[Path]:
    """Where a freeze manifest for ``truth_file`` is looked for: ``<corpus>`` only.

    The one supported layout is ``<corpus>/<set>/ground_truth.json`` with ``corpus-version.json``
    directly in ``<corpus>``; the scorer's ``find_freeze_manifest`` and certification look nowhere
    else, so neither does review nor freeze.
    """

    return [real_path(truth_file).parent.parent]


def covering_freeze_manifest(truth_path: Path, set_id: str) -> Path | None:
    """A ``frozen: true`` manifest beside or above a record that lists its ``set_id``, if any."""

    for directory in manifest_search_directories(truth_path):
        manifest = directory / FREEZE_MANIFEST_NAME
        if not path_is_file(manifest):
            continue
        try:
            payload = json.loads(read_text(manifest))
        except ValueError as exc:
            raise ValueError(f"unreadable freeze manifest {manifest}: {exc}") from None
        if not isinstance(payload, dict) or payload.get("frozen") is not True:
            continue
        if any(
            isinstance(item, dict) and item.get("set_id") == set_id
            for item in payload.get("sets") or []
        ):
            return manifest
    return None


def refuse_frozen(truth_path: Path, truth: GroundTruthRecord) -> None:
    """Frozen is terminal: no pass may rewrite, re-attribute or expose a frozen set."""

    manifest = covering_freeze_manifest(truth_path, truth.set_id)
    if manifest is not None:
        raise ValueError(
            f"{truth.set_id} is frozen by {manifest} (corpus_version {truth.corpus_version}); a "
            "frozen set is terminal and cannot be reopened, re-annotated or shown predictions"
        )


def refuse_later_passes(
    truth: GroundTruthRecord, exists: Callable[[str], bool], pass_name: str
) -> None:
    """Truth moves forward only: first pass -> second pass -> resolution -> frozen.

    A pass may not be written over a set that already carries a later one.  Rewriting would clear
    ``second_pass_ref`` / ``disagreement_resolution`` and replace pass files the later annotators
    wrote, stepping the set backwards.  ``exists`` answers whether a sibling record is present.
    """

    second = exists("annotation-second.json") or any(
        episode.second_pass_ref is not None for episode in truth.episodes
    )
    resolved = exists("annotation-resolution.json") or any(
        (episode.disagreement_resolution or "").startswith("resolved-by:")
        for episode in truth.episodes
    )
    if resolved:
        raise ValueError(
            f"{truth.set_id} is already resolved by a third annotator; truth only moves forward, "
            f"so a {pass_name} pass cannot be written over it"
        )
    if pass_name in {"first", "second"} and second:
        raise ValueError(
            f"{truth.set_id} already has a second pass; truth only moves forward, so a "
            f"{pass_name} pass cannot be written over it"
        )


def _sibling_exists(truth_path: Path) -> Callable[[str], bool]:
    return lambda name: os.path.lexists(truth_path.with_name(name))


def _read_record(truth_path: Path) -> tuple[GroundTruthRecord, str]:
    raw = read_bytes(truth_path)
    return GroundTruthRecord.model_validate_json(raw), sha256(raw).hexdigest()


def refuse_reattribution(truth: GroundTruthRecord, annotator_ref: str) -> None:
    """A first pass by ``annotator_ref`` must not claim rows someone else already verified."""

    foreign = [
        (index + 1, episode.annotator_ref or "no recorded annotator")
        for index, episode in enumerate(truth.episodes)
        if not episode.draft and episode.annotator_ref != annotator_ref
    ]
    if foreign:
        who = ", ".join(sorted({name for _, name in foreign}))
        rows = ", ".join(str(row) for row, _ in foreign)
        raise ValueError(
            f"row(s) {rows} were already verified by {who}; a first pass by {annotator_ref} would "
            "re-attribute that work. Finish the set as the same annotator, or start the pass "
            "from a clean draft"
        )


def _lock_contended(key: str) -> None:
    """Test seam: called once when a truth lock is already held by another process."""


def _ledger_append_window(corpus_dir: Path) -> None:
    """Test seam: called inside the ledger append, after its read and before its write."""


def _gateway_entered(root: Path, mutate: bool) -> None:
    """Test seam: the first statement of :func:`open_corpus`; every corpus path reaches it."""


class _TruthLock:
    """One reentrant, cross-process advisory lock over a single truth record.

    A ``threading`` lock alone only serialises writers inside *one* interpreter, so two ``idea
    truth review`` processes (or a review and a ``truth verify``) could both read the same
    ``ground_truth.json`` digest, both believe nothing had changed, and then overwrite each
    other's annotation pass — and each other's rollback.  The OS lock below is what actually
    serialises them; the thread lock keeps the same process reentrant and cheap.

    The lock file lives in the system temp directory, keyed by a digest of the record's resolved
    path.  Nothing is created beside the corpus record itself, so locking never adds a file to a
    corpus set and never writes beneath ``work/``.
    """

    def __init__(self, key: str) -> None:
        self._guard = threading.RLock()
        self._depth = 0
        self._handle: Any = None
        self._key = key
        digest = sha256(key.encode("utf-8")).hexdigest()[:32]
        self._path = Path(tempfile.gettempdir()) / f"idea-truth-{digest}.lock"

    def _lock_os(self, timeout: float) -> None:
        handle = open(self._path, "a+b")  # noqa: SIM115 - held for the lock's lifetime
        deadline = time.monotonic() + timeout
        contended = False
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if not contended:
                    contended = True
                    _lock_contended(self._key)
                if time.monotonic() >= deadline:
                    handle.close()
                    raise ValueError(
                        "another truth writer holds this set; close the other review and retry"
                    ) from None
                time.sleep(0.05)
                continue
            self._handle = handle
            return

    def _unlock_os(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def acquire(self, timeout: float) -> None:
        # One deadline for both halves: another *thread* of this process holding the record must
        # time out exactly like another process does, never block forever.
        deadline = time.monotonic() + timeout
        if not self._guard.acquire(timeout=max(0.0, timeout)):
            raise ValueError(
                "another truth writer holds this set; close the other review and retry"
            )
        if self._depth == 0:
            try:
                self._lock_os(max(0.0, deadline - time.monotonic()))
            except BaseException:
                self._guard.release()
                raise
        self._depth += 1

    def release(self) -> None:
        self._depth -= 1
        try:
            if self._depth == 0:
                self._unlock_os()  # closes the OS handle (releasing its lock) even if unlock raises
        finally:
            self._guard.release()


_TRUTH_LOCKS: dict[str, _TruthLock] = {}
_TRUTH_LOCK_REGISTRY = threading.Lock()


@contextmanager
def _held_lock(key: str, timeout: float) -> Iterator[None]:
    with _TRUTH_LOCK_REGISTRY:
        lock = _TRUTH_LOCKS.setdefault(key, _TruthLock(key))
    lock.acquire(timeout)
    try:
        yield
    finally:
        lock.release()


#: The one machine-wide lock every seed takes before its checks (see :func:`seed_truth`).  It is
#: taken before any corpus lock, and nothing takes it while holding a corpus lock.
SEED_LOCK_KEY = "seed|machine-wide"


def corpus_root_of(truth_path: Path) -> Path:
    """The corpus directory of a record in the one supported layout, ``<corpus>/<set>/<record>``."""

    return Path(os.path.abspath(truth_path)).parent.parent


@contextmanager
def corpus_write_lock(corpus_root: Path, *, timeout: float = 20.0) -> Iterator[None]:
    """The single cross-process mutation lock for a whole corpus.

    Every operation that changes corpus state takes it first, before reading any state it will act
    on, and holds it until its last write is durable: ``seed_truth``, ``verify_truth``,
    ``second_pass_truth``, ``resolve_truth``, the review tool's save and reveal (exposure sidecar
    and ledger append), ``write_draft_manifest`` and ``freeze_truth``.  Two cooperating operations
    on one corpus therefore never interleave, whatever predicates they check.  Read-only paths
    (scoring, certification, the review page's GETs) do not take it.

    **Lock order**, fixed so nothing can deadlock: this corpus lock, then (inside it) per-record
    locks in ``path_key`` order.  :func:`truth_write_lock` enforces the order itself by always
    taking the corpus lock before the record lock.  It is reentrant within a thread, shares the
    one-deadline timeout of :class:`_TruthLock`, and releases in ``finally``.
    """

    with _held_lock("corpus|" + path_key(corpus_root), timeout):
        yield


@contextmanager
def truth_write_lock(truth_path: Path, *, timeout: float = 20.0) -> Iterator[None]:
    """Serialise every writer of one truth record, across threads *and* across processes.

    Takes the record's corpus lock first (see :func:`corpus_write_lock`), then the record lock, so
    the lock order is the same wherever a record lock is taken.  Re-entering is safe.
    """

    # ``path_key`` strips Win32 namespace prefixes around ``realpath``: ``\\?\C:\...`` and
    # ``C:\...`` are one record, so they must be one lock.
    # The record's corpus is exactly its grandparent and must pass the gateway (link, work-tree
    # and inside-another-corpus refusals) before its lock is keyed or taken.  Per-record frozen
    # refusal stays with each caller, so a crash recovery or a reveal check can still run.
    with (
        open_corpus(
            exact_corpus_root(truth_path),
            mutate=True,
            timeout=timeout,
            require_records=False,
            allow_frozen=True,
        ),
        _held_lock(path_key(truth_path), timeout),
    ):
        yield


#: The one supported corpus layout, stated in every layout refusal.
SUPPORTED_LAYOUT = (
    "the one supported corpus layout is <corpus>/<set>/ground_truth.json, with "
    "corpus-version.json and review-exposure-ledger.jsonl directly in <corpus>"
)


def refuse_single_set_root(directory: Path) -> None:
    """Refuse a directory holding ``ground_truth.json`` directly: it is a set, not a corpus."""

    if os.path.lexists(Path(directory) / "ground_truth.json"):
        raise ValueError(
            f"refusing {directory}: it holds ground_truth.json directly, so it is a set, not a "
            f"corpus; {SUPPORTED_LAYOUT}. Pass the <corpus> directory that contains the set folder"
        )


def _walk_corpus_tree(root: Path, *, limit: int | None = None) -> tuple[list[Path], list[Path]]:
    """Every ``ground_truth.json`` under ``root``, found by a link-refusing walk.

    This is the one place the corpus tree is enumerated.  It walks with ``os.scandir`` and never
    follows a link: a symlink or junction component -- a set-directory junction, a linked
    ``corpus-version.json`` or a linked record -- is refused through :func:`link_refusal` *before*
    the linked target is descended into or read, so a descendant link is never traversed.
    """

    found: list[Path] = []
    files: list[Path] = []
    seen = [0]

    def walk(directory: Path) -> None:
        try:
            entries = list(os.scandir(native_path(directory)))
        except (FileNotFoundError, NotADirectoryError):
            return
        for entry in sorted(entries, key=lambda item: item.name):
            seen[0] += 1
            if limit is not None and seen[0] > limit:
                raise ValueError(
                    f"refusing to scan {root}: it holds more than {limit} entries, too many to "
                    "check safely for corpus files; choose a smaller folder"
                )
            child = directory / entry.name
            if is_link(child):
                raise link_refusal(root, child)
            if entry.is_dir(follow_symlinks=False):
                walk(child)
            else:
                files.append(child)
                if entry.name == TRUTH_RECORD_NAME:
                    found.append(child)

    walk(Path(root))
    return found, files


def _scan_corpus_tree(root: Path) -> list[Path]:
    """Every ``ground_truth.json`` under ``root``, by the link-refusing walk."""

    return _walk_corpus_tree(root)[0]


def require_corpus_directory(directory: Path) -> list[Path]:
    """Every record of a corpus in the one supported layout, refusing any other layout.

    Steps, in order: refuse a link component of ``directory``; refuse a single-set root
    (``ground_truth.json`` directly in it); link-safe walk (:func:`_scan_corpus_tree`); reject a
    truth file nested at any other depth than ``<corpus>/<set>/ground_truth.json``.
    """

    return _validated_corpus_tree(directory)[0]


def _validated_corpus_tree(directory: Path) -> tuple[list[Path], list[Path]]:
    """The records and every other file of a corpus in the one supported layout."""

    refuse_link_components(directory)
    refuse_single_set_root(directory)
    found, files = _walk_corpus_tree(Path(directory))
    records = sorted(found)
    root = path_key(directory)
    for record in records:
        if path_key(record.parent.parent) != root:
            raise ValueError(
                f"refusing {record}: it is nested below a set directory, not at "
                f"<corpus>/<set>/{TRUTH_RECORD_NAME}; {SUPPORTED_LAYOUT}"
            )
    return records, sorted(files)


def _lexical_key(path: Path | str) -> str:
    """A comparison key for a path as spelled: absolute, prefix-free, case-folded, unresolved."""

    raw = os.fspath(path)
    if os.name == "nt":
        raw = strip_namespace_prefix(raw)
    return os.path.normcase(os.path.abspath(raw))


def exact_corpus_root(
    target: Path, *, corpus_root: Path | None = None, name: str | None = None
) -> Path:
    """The corpus root of a target that must sit exactly at ``<root>/<set>/<file>``.

    The root is exactly ``target.parent.parent`` -- computed from the spelling, never searched
    for -- and, when the caller gives the root explicitly, it must be that root.  The root then has
    to pass :func:`open_corpus`: a target one level too deep has a grandparent inside another
    corpus, which the ancestor refusal rejects, so it is never locked or written on the wrong root.
    """

    absolute = Path(os.path.abspath(target))
    if name is not None and absolute.name != name:
        raise ValueError(f"refusing {target}: it must be named {name}; {SUPPORTED_LAYOUT}")
    root = absolute.parent.parent
    if corpus_root is not None and _lexical_key(corpus_root) != _lexical_key(root):
        raise ValueError(
            f"refusing {target}: it is not exactly <corpus>/<set>/{absolute.name} directly under "
            f"the corpus {corpus_root}; {SUPPORTED_LAYOUT}"
        )
    return root


def require_corpus_member(handle: CorpusHandle, target: Path) -> Path:
    """``target`` must be one of the gateway's vetted records, ``<root>/<set>/<record>``."""

    absolute = Path(os.path.abspath(target))
    if _lexical_key(absolute) not in {_lexical_key(path) for path in handle.truth_files}:
        raise ValueError(
            f"refusing {target}: it is not a record of the corpus {handle.root}; {SUPPORTED_LAYOUT}"
        )
    return absolute


#: The most entries one corpus-detection listing reads before failing closed.
CORPUS_LISTING_LIMIT = 2_000
#: The most entries a "does this directory contain corpus files" walk reads before failing closed.
CORPUS_CONTENT_SCAN_LIMIT = 20_000
_CORPUS_MARKERS = (FREEZE_MANIFEST_NAME, EXPOSURE_LEDGER_NAME, TRUTH_RECORD_NAME)


def _holds_truth_bearing_set(
    directory: Path, *, skip: str | None, limit: int | None = CORPUS_LISTING_LIMIT
) -> bool:
    """Whether a child directory of ``directory`` (other than ``skip``) holds ground_truth.json.

    Reads at most ``limit`` entries: a larger folder fails closed rather than being scanned.
    """

    try:
        entries = os.scandir(native_path(directory))
    except OSError:
        return False
    with entries:
        for count, entry in enumerate(entries, start=1):
            if limit is not None and count > limit:
                raise ValueError(
                    f"refusing to use {directory}: it holds more than {limit} entries, too many "
                    "to check safely whether it is a corpus; choose a folder with fewer entries"
                )
            if skip is not None and os.path.normcase(entry.name) == skip:
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if os.path.lexists(os.path.join(entry.path, TRUTH_RECORD_NAME)) and not is_link(
                entry.path
            ):
                return True
    return False


def corpus_ancestor(path: Path) -> Path | None:
    """The nearest strict ancestor of ``path`` holding a corpus file directly, found in O(depth).

    Each ancestor, up to the filesystem root, is probed with ``lstat`` for ``corpus-version.json``,
    ``review-exposure-ledger.jsonl`` and ``ground_truth.json`` directly inside it: a corpus with a
    manifest or ledger, or a set directory.  No ancestor is ever listed.
    """

    absolute = Path(os.path.abspath(path))
    for ancestor in absolute.parents:
        if any(os.path.lexists(native_path(ancestor / name)) for name in _CORPUS_MARKERS):
            return ancestor
    return None


def corpus_containing(path: Path) -> Path | None:
    """The corpus a destination lies in, or ``None``.

    :func:`corpus_ancestor` first (O(depth) ``lstat``).  When ``path`` is not an existing directory
    -- a file or directory about to be written -- the nearest existing ancestor of its parent is
    also listed once, bounded by :data:`CORPUS_LISTING_LIMIT` and failing closed beyond it, to catch
    a corpus without a manifest (such as ``data/corpus/release-1``) that holds its sets directly.
    """

    absolute = Path(os.path.abspath(path))
    ancestor = corpus_ancestor(absolute)
    if ancestor is not None:
        return ancestor
    if os.path.isdir(native_path(absolute)):
        return None
    nearest = nearest_existing_ancestor(absolute.parent)
    return nearest if _holds_truth_bearing_set(nearest, skip=None) else None


def _is_new_corpus_root(root: Path) -> bool:
    """A root that does not exist, or exists with no manifest, no ledger and no set, is NEW.

    Such a root -- including an empty folder the owner just created -- gets the one bounded
    listing of its parent, so a corpus inside a manifest-less corpus such as ``release-1`` is
    refused whether or not its folder already exists.
    """

    if not os.path.isdir(native_path(root)):
        return True
    if any(
        os.path.lexists(native_path(root / name))
        for name in (FREEZE_MANIFEST_NAME, EXPOSURE_LEDGER_NAME)
    ):
        return False
    return not _holds_truth_bearing_set(root, skip=None)


def refuse_corpus_ancestry(
    root: Path, *, work_root: Path | None = None, creating: bool = False
) -> Path:
    """Refuse a corpus root reached through a link, beneath the work tree, or inside another corpus.

    In this order, before anything under ``root`` is resolved, locked or read: (1) every existing
    component of ``root``'s absolute spelling is examined with ``lstat`` and a symlink or junction
    is refused (:func:`refuse_link_components`); (2) a root beneath ``work_root`` is refused; (3) a
    root any of whose ancestors, up to the filesystem root, is a corpus (:func:`corpus_ancestor`)
    is refused -- it is inside another corpus.  Returns the absolute spelling of ``root``.
    """

    absolute = Path(os.path.abspath(root))
    refuse_link_components(absolute)
    if work_root is not None and is_within(absolute, work_root):
        raise ValueError(
            f"refusing corpus {absolute}: it lies beneath the work tree {work_root}, which idea gc "
            "prunes; a corpus must live outside it"
        )
    ancestor = corpus_ancestor(absolute)
    if ancestor is None and creating:
        # Creating a new corpus root: at most one bounded listing, of its nearest existing parent,
        # catches a corpus without a manifest (release-1) that would gain a nested corpus.
        nearest = nearest_existing_ancestor(absolute.parent)
        ancestor = nearest if _holds_truth_bearing_set(nearest, skip=None) else None
    if ancestor is not None:
        raise ValueError(
            f"refusing corpus {absolute}: it is inside another corpus, {ancestor}, which holds "
            f"{FREEZE_MANIFEST_NAME} or a set directory with {TRUTH_RECORD_NAME}; "
            f"{SUPPORTED_LAYOUT}"
        )
    return absolute


def is_corpus_directory(directory: Path) -> bool:
    """Whether ``directory`` is a corpus: it holds the manifest or a set with a truth record."""

    directory = Path(directory)
    return os.path.lexists(native_path(directory / FREEZE_MANIFEST_NAME)) or (
        _holds_truth_bearing_set(directory, skip=None)
    )


def corpus_content_below(directory: Path) -> Path | None:
    """The first corpus manifest or truth record under ``directory`` (link-refusing walk)."""

    found, files = _walk_corpus_tree(Path(directory), limit=CORPUS_CONTENT_SCAN_LIMIT)
    for path in [*found, *files]:
        if path.name in (FREEZE_MANIFEST_NAME, TRUTH_RECORD_NAME):
            return path
    return None


def refuse_write_inside_corpus(target: Path) -> None:
    """Refuse a generated file (a report, a model) whose destination lies inside any corpus."""

    ancestor = corpus_containing(target)
    if ancestor is not None:
        raise ValueError(
            f"refusing to write {target}: it is inside the corpus {ancestor}; generated "
            "reports are written outside every corpus root"
        )


def refuse_generated_output(path: Path, *, work_root: Path | None = None) -> Path:
    """Refuse a user-selected report or artifact destination that could damage a corpus.

    Checked on the path as spelled, at validation and again immediately before each write or
    publish: a link anywhere on it; a corpus file's name (``ground_truth.json``,
    ``corpus-version.json``, the ledger); a destination beneath ``work_root``; one that lies inside
    a corpus (:func:`corpus_containing`); and an existing directory that is a corpus or a set, or
    contains corpus files below it (a bounded, link-refusing walk).  Returns the absolute path.
    """

    absolute = Path(os.path.abspath(path))
    refuse_link_components(absolute)
    if absolute.name in _CORPUS_MARKERS:
        raise ValueError(
            f"refusing to write generated output {path}: {absolute.name} is a corpus file name, "
            "so it would overwrite or create corpus truth; write reports outside every corpus"
        )
    if work_root is not None and is_within(absolute, work_root):
        raise ValueError(
            f"refusing to write generated output {path}: it lies beneath the work tree {work_root}"
        )
    inside = corpus_containing(absolute)
    if inside is not None:
        raise ValueError(
            f"refusing to write generated output {path}: it lies inside the corpus {inside}; "
            "write reports outside every corpus"
        )
    if os.path.isdir(native_path(absolute)):
        if any(os.path.lexists(native_path(absolute / name)) for name in _CORPUS_MARKERS) or (
            is_corpus_directory(absolute)
        ):
            raise ValueError(
                f"refusing to write generated output into {path}: it is a corpus or a set"
            )
        below = corpus_content_below(absolute)
        if below is not None:
            raise ValueError(
                f"refusing to write generated output into {path}: it contains corpus files "
                f"({below})"
            )
    return absolute


def scratch_corpus_ancestor(path: Path) -> Path | None:
    """The corpus ``path`` lies in, at ANY depth, whether or not that corpus has a manifest.

    The gateway's two ancestor checks, applied at every ancestor up to the filesystem root rather
    than only at the nearest one: :func:`corpus_ancestor` (an ancestor holding a corpus file
    directly, ``lstat`` only) and :func:`_holds_truth_bearing_set` (an ancestor holding set folders
    directly, as a corpus without a manifest such as ``release-1`` does).  So an ordinary existing
    subfolder several levels inside ``release-1`` is still inside ``release-1``.  Each existing
    ancestor is listed once and in full -- the system temporary folder routinely holds more entries
    than :data:`CORPUS_LISTING_LIMIT` -- which is affordable because this runs once per calibration
    validation, never per corpus open (the gateway itself stays O(depth) ``lstat``).
    """

    absolute = Path(os.path.abspath(path))
    marked = corpus_ancestor(absolute)
    if marked is not None:
        return marked
    for ancestor in absolute.parents:
        if os.path.isdir(native_path(ancestor)) and _holds_truth_bearing_set(
            ancestor, skip=None, limit=None
        ):
            return ancestor
    return None


def refuse_scratch_destination(scratch: Path, *, work_root: Path | None) -> Path:
    """Refuse a scratch corpus folder that is not yet created unless it is safe to create.

    Checked on the path as spelled, BEFORE any file or folder is made: a link anywhere on it; a
    folder beneath ``work_root`` (which ``idea gc`` prunes, and which the owner may point anywhere,
    the system temporary folder included); a folder that already exists; and a folder inside any
    corpus AT ANY DEPTH (:func:`scratch_corpus_ancestor`).  Returns the absolute path.
    """

    absolute = Path(os.path.abspath(scratch))
    refuse_link_components(absolute)
    if work_root is not None and is_within(absolute, work_root):
        raise ValueError(
            f"refusing to build the scratch corpus at {absolute}: it is inside the work folder "
            f"{work_root}, which `idea gc` cleans up. Choose a --work-root that is not the "
            "temporary folder (or a folder above it), or point the TEMP environment variable at "
            "a folder outside the work folder, and run again"
        )
    if os.path.lexists(native_path(absolute)):
        raise ValueError(
            f"refusing to build the scratch corpus at {absolute}: something already exists "
            "there. Run the command again; a fresh folder name is chosen each time"
        )
    inside = scratch_corpus_ancestor(absolute)
    if inside is not None:
        raise ValueError(
            f"refusing to build the scratch corpus at {absolute}: it is inside the corpus "
            f"{inside}. Point the TEMP environment variable at an ordinary folder that holds no "
            "truth files, and run again"
        )
    return absolute


def refuse_unsupported_seed_destination(out_path: Path) -> None:
    """A seed lands at ``<corpus>/<set>/ground_truth.json``: never a corpus root or single-set root.

    Enumerates nothing: the whole-corpus layout is re-validated under the lock by
    :func:`open_corpus`, which rejects a nested truth file created concurrently.
    """

    if Path(out_path).name != "ground_truth.json":
        raise ValueError(f"refusing to seed {out_path}: a seed must be named ground_truth.json")
    set_dir = Path(os.path.abspath(out_path)).parent
    refuse_single_set_root(set_dir.parent)  # the corpus root must not itself be a set
    markers = [
        name
        for name in (FREEZE_MANIFEST_NAME, EXPOSURE_LEDGER_NAME)
        if os.path.lexists(set_dir / name)
    ]
    if markers:
        raise ValueError(
            f"refusing to seed {out_path}: {set_dir} is a corpus directory, not a set directory; "
            f"{SUPPORTED_LAYOUT}"
        )


@dataclass(frozen=True)
class CorpusHandle:
    """The vetted result of :func:`open_corpus`: a canonical root and its truth-file list.

    Read paths (scoring, certification, independence scans, the draft inventory) read only from
    ``truth_files``; nothing enumerates the corpus itself again.
    """

    root: Path
    truth_files: tuple[Path, ...]
    manifest_path: Path
    ledger_path: Path
    mutate: bool
    work_root: Path | None
    #: Every regular file the gateway's link-refusing walk found under ``root`` (records included).
    files: tuple[Path, ...] = ()


def corpus_manifest_frozen(root: Path) -> bool:
    """Whether ``<root>/corpus-version.json`` exists and records ``frozen: true``; links refused."""

    manifest = Path(root) / FREEZE_MANIFEST_NAME
    if is_link(manifest):
        raise link_refusal(manifest, manifest)
    if not path_is_file(manifest):
        return False
    try:
        payload = json.loads(read_text(manifest))
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("frozen") is True


_HELD_CORPORA = threading.local()


def _held_handles() -> dict[str, CorpusHandle]:
    held = getattr(_HELD_CORPORA, "handles", None)
    if held is None:
        held = _HELD_CORPORA.handles = {}
    return held


@contextmanager
def open_corpus(
    root: Path,
    *,
    mutate: bool,
    work_root: Path | None = None,
    timeout: float = 20.0,
    require_records: bool = True,
    allow_frozen: bool = False,
) -> Iterator[CorpusHandle]:
    """The single gateway every corpus-touching path goes through, before reading corpus state.

    In order:

    1. :func:`refuse_corpus_ancestry` -- ``lstat`` link refusal of the root and every ancestor
       component, then a root beneath ``work_root``, then a root inside another corpus -- all
       before ``path_key``, the lock or any manifest read;
    2. for a mutation, take the corpus lock and repeat step 1 under it, then refuse a frozen
       manifest unless ``allow_frozen``;
    3. validate the one supported layout with the link-refusing walk
       (:func:`require_corpus_directory`) and return the vetted truth-file list, which readers use
       instead of enumerating the corpus again.

    A mutation re-entered on the same thread while the corpus lock is held reuses the handle the
    outer entry validated under that lock (the frozen refusal is still re-applied).
    """

    _gateway_entered(Path(root), mutate)
    # Before the lock: lstat-only ancestry (links, the work tree, corpus files in any ancestor).
    root = refuse_corpus_ancestry(root, work_root=work_root)

    def build() -> CorpusHandle:
        if os.path.lexists(native_path(root)) and not os.path.isdir(native_path(root)):
            raise ValueError(
                f"refusing {root}: a corpus is a directory, not a file; {SUPPORTED_LAYOUT}"
            )
        truth_files, files = _validated_corpus_tree(root)
        if require_records and not truth_files:
            raise ValueError(f"no {TRUTH_RECORD_NAME} under {root}; {SUPPORTED_LAYOUT}")
        return CorpusHandle(
            root=root,
            truth_files=tuple(truth_files),
            manifest_path=root / FREEZE_MANIFEST_NAME,
            ledger_path=root / EXPOSURE_LEDGER_NAME,
            mutate=mutate,
            work_root=work_root,
            files=tuple(files),
        )

    def refuse_frozen_corpus() -> None:
        if not allow_frozen and corpus_manifest_frozen(root):
            raise ValueError(
                f"refusing to mutate {root}: it is frozen ({FREEZE_MANIFEST_NAME} records "
                "frozen:true); a frozen corpus is terminal and cannot take a new or changed set"
            )

    if not mutate:
        yield build()
        return
    key = "corpus|" + path_key(root)
    held = _held_handles()
    if key in held:
        refuse_frozen_corpus()
        yield held[key]
        return
    with corpus_write_lock(root, timeout=timeout):
        # Under the lock, once per mutation: a new root (missing, or with no manifest, ledger or
        # set) has its parent listed once, bounded, to refuse a corpus inside another corpus.
        refuse_corpus_ancestry(root, work_root=work_root, creating=_is_new_corpus_root(root))
        refuse_frozen_corpus()
        handle = build()
        held[key] = handle
        try:
            yield handle
        finally:
            held.pop(key, None)


def frozen_manifest(handle: CorpusHandle) -> dict[str, Any] | None:
    """The corpus manifest if it records ``frozen: true``, else ``None``; a link is refused."""

    manifest = handle.manifest_path
    if is_link(manifest):
        raise link_refusal(manifest, manifest)
    if not path_is_file(manifest):
        return None
    try:
        payload = json.loads(read_text(manifest))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) and payload.get("frozen") is True else None


def require_frozen_inventory(handle: CorpusHandle, manifest: dict[str, Any]) -> None:
    """Refuse unless the loaded inventory exactly equals the frozen manifest's inventory.

    The loaded inventory is ``{(set_id, relative path)}`` over the gateway's vetted truth files: the
    corpus population on disk, never a caller's subset.  A set deleted from, or added to, a frozen
    corpus therefore makes verified, independent and certified status impossible, and the refusal
    names every missing and surplus entry.
    """

    loaded: set[tuple[str, str]] = set()
    for path in handle.truth_files:
        try:
            payload = json.loads(read_text(path))
            set_id = str(payload.get("set_id")) if isinstance(payload, dict) else "<unreadable>"
        except (OSError, ValueError):
            set_id = "<unreadable>"
        loaded.add((set_id, path.relative_to(handle.root).as_posix()))
    recorded = {
        (str(item.get("set_id")), str(item.get("path")))
        for item in manifest.get("sets", [])
        if isinstance(item, dict)
    }
    if loaded != recorded:
        missing = ", ".join(f"{set_id} ({path})" for set_id, path in sorted(recorded - loaded))
        surplus = ", ".join(f"{set_id} ({path})" for set_id, path in sorted(loaded - recorded))
        raise ValueError(
            f"the loaded truth inventory of {handle.root} does not exactly equal its frozen "
            f"manifest inventory: missing {missing or 'none'}; surplus {surplus or 'none'}. "
            "Verified, independent and certified status need exactly the frozen population"
        )


def ledger_line_digests(raw: bytes) -> set[str]:
    """SHA-256 of every non-blank ledger line, hashed as :func:`ledger_entry_digests` does."""

    return {sha256(line.strip()).hexdigest() for line in raw.splitlines() if line.strip()}


def require_frozen_ledger(handle: CorpusHandle, manifest: dict[str, Any]) -> None:
    """Refuse unless the corpus ledger holds exactly the lines its freeze manifest recorded.

    Every line of the current ledger must be one the manifest recorded for some set, and every
    recorded line must still be there.  A line appended after the freeze -- by hand, or by older
    tooling that allowed it -- is therefore caught while it is present, rather than silently
    restoring verified status once it is deleted again.
    """

    ledger = handle.ledger_path
    if is_link(ledger):
        raise link_refusal(ledger, ledger)
    current = ledger_line_digests(read_bytes(ledger)) if path_is_file(ledger) else set()
    recorded: set[str] = set()
    for item in manifest.get("sets", []):
        exposure = item.get("prediction_exposure") if isinstance(item, dict) else None
        entries = exposure.get("ledger_entries") if isinstance(exposure, dict) else None
        recorded.update(str(digest) for digest in entries or [])
    if current != recorded:
        raise ValueError(
            f"the exposure ledger {ledger} does not exactly equal the ledger its freeze manifest "
            f"recorded: {len(current - recorded)} line(s) added and {len(recorded - current)} "
            "line(s) missing since the freeze; a frozen corpus's ledger never changes"
        )


@contextmanager
def _record_mutation(
    truth_path: Path, corpus_root: Path | None, work_root: Path | None
) -> Iterator[CorpusHandle]:
    """Enter the gateway for one record: exact depth, then membership of the vetted list."""

    root = exact_corpus_root(truth_path, corpus_root=corpus_root, name=TRUTH_RECORD_NAME)
    with open_corpus(root, mutate=True, work_root=work_root) as handle:
        require_corpus_member(handle, truth_path)
        yield handle


def _episode_content(episode: Any) -> dict[str, Any]:
    payload = episode.model_dump(mode="json") if hasattr(episode, "model_dump") else dict(episode)
    for key in ("annotator_ref", "second_pass_ref", "disagreement_resolution", "draft"):
        payload.pop(key, None)
    return payload


def _pass_content(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "episodes": payload["episodes"],
        "events": payload.get("events") or [],
        "regions": payload["regions"],
    }


def _annotation_content(truth: GroundTruthRecord) -> dict[str, Any]:
    return {
        "episodes": [_episode_content(episode) for episode in truth.episodes],
        "events": [event.model_dump(mode="json") for event in truth.events],
        "regions": [region.model_dump(mode="json") for region in truth.regions],
    }


def _annotation_bytes(
    set_id: str,
    pass_name: str,
    *,
    annotator_ref: str,
    mode: str,
    content: dict[str, Any],
    provenance: dict[str, Any] | None = None,
) -> bytes:
    digest = sha256(canonical_json_bytes(content)).hexdigest()
    return canonical_json_bytes(
        {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "set_id": set_id,
            "pass": pass_name,
            "annotator_ref": annotator_ref,
            "mode": mode,
            "content_sha256": digest,
            **content,
            **({"review_provenance": provenance} if provenance is not None else {}),
        }
    )


def _write_set_file(pinned: PinnedDirectory, name: str, content: bytes) -> None:
    """The one place a truth set's files are replaced (and the seam tests inject failures at)."""

    pinned.write_bytes(name, content)


def _encode(content: bytes | None) -> dict[str, str] | None:
    if content is None:
        return None
    try:
        return {"text": content.decode("utf-8")}
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(content).decode("ascii")}


def _decode(value: object) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"].encode("utf-8")
    if isinstance(value, dict) and isinstance(value.get("base64"), str):
        return base64.b64decode(value["base64"], validate=True)
    raise ValueError("invalid transaction content")


def _roll_back(pinned: PinnedDirectory, record: dict[str, Any]) -> None:
    truth_name, annotation_name = record["truth"], record["annotation"]
    previous_truth = _decode(record["previous_truth"])
    if previous_truth is None:
        raise ValueError("interrupted-write record lacks the previous truth")
    previous_annotation = _decode(record["previous_annotation"])
    if previous_annotation is None:
        pinned.remove(annotation_name)
    else:
        _write_set_file(pinned, annotation_name, previous_annotation)
    if pinned.read_bytes(truth_name) != previous_truth:
        _write_set_file(pinned, truth_name, previous_truth)
    pinned.remove(_transaction_name(truth_name))


def _recover(pinned: PinnedDirectory, truth_name: str) -> str | None:
    """Complete or undo an interrupted pair replacement; ``None`` when nothing was pending.

    A pair whose files both already carry the new digests was committed and only lost its record
    to the crash, so it is kept.  Anything else -- the annotation replaced but not the truth, or
    neither -- is restored byte-for-byte to what the record says was there before.
    """

    journal = _transaction_name(truth_name)
    raw = pinned.read_bytes(journal)
    if raw is None:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
        if record.get("truth") != truth_name or record.get("annotation") not in _ANNOTATION_NAMES:
            raise ValueError("record names another file")
        next_truth = str(record["next_truth_sha256"])
        next_annotation = str(record["next_annotation_sha256"])
        _decode(record["previous_truth"])
        _decode(record["previous_annotation"])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(
            f"{pinned.path / journal} is not a valid interrupted-write record ({exc}); restore "
            "this set from version control before writing it again"
        ) from None
    truth_now = pinned.read_bytes(truth_name)
    annotation_now = pinned.read_bytes(record["annotation"])
    if (
        truth_now is not None
        and annotation_now is not None
        and sha256(truth_now).hexdigest() == next_truth
        and sha256(annotation_now).hexdigest() == next_annotation
    ):
        pinned.remove(journal)
        return "completed"
    _roll_back(pinned, record)
    return "rolled-back"


def recover_interrupted_write(truth_path: Path, *, work_root: Path | None = None) -> str | None:
    """Finish or undo a pass save that a crash interrupted; ``None`` when none is pending.

    Only an existing record takes the write lock, so an idle set opens without waiting on
    another writer; a record that exists while another writer is mid-save waits for that writer.
    """

    truth_path = record_path(truth_path)
    if not os.path.lexists(transaction_path(truth_path)):
        return None
    with (
        truth_write_lock(truth_path),
        pinned_set_directory(truth_path.parent, work_root=work_root) as pinned,
    ):
        return _recover(pinned, truth_path.name)


def _commit_pair(
    pinned: PinnedDirectory,
    truth_name: str,
    annotation_name: str,
    *,
    annotation: bytes,
    truth: bytes,
) -> None:
    """Replace an annotation pass and the truth it justifies as one recoverable transaction.

    Each replacement is atomic, but two replacements are not; so before either, a durable record
    of both previous files and both new digests is written beside them.  An exception rolls back
    in-process; process death or power loss leaves the record, and :func:`_recover` completes or
    restores the pair the next time the set is opened or written.
    """

    previous_truth = pinned.read_bytes(truth_name)
    if previous_truth is None:
        raise ValueError(f"{truth_name} disappeared before it could be replaced")
    record = {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "truth": truth_name,
        "annotation": annotation_name,
        "previous_truth": _encode(previous_truth),
        "previous_annotation": _encode(pinned.read_bytes(annotation_name)),
        "next_truth_sha256": sha256(truth).hexdigest(),
        "next_annotation_sha256": sha256(annotation).hexdigest(),
    }
    _write_set_file(pinned, _transaction_name(truth_name), canonical_json_bytes(record))
    try:
        _write_set_file(pinned, annotation_name, annotation)
        _write_set_file(pinned, truth_name, truth)
    except BaseException:
        _roll_back(pinned, record)
        raise
    pinned.remove(_transaction_name(truth_name))


_EXPOSURE_KEY = "predictions_visible_during_review"


def _pinned_exposure(pinned: PinnedDirectory, truth_path: Path) -> bool:
    """Whether the held set directory, or the corpus ledger, already records visible predictions."""

    if pinned.exists(EXPOSURE_NAME) or ledger_exposure(truth_path):
        return True
    for name in ("first", "second", "resolution"):
        raw = pinned.read_bytes(f"annotation-{name}.json")
        if raw is None:
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # Unreadable evidence is not proof of independence: stay pessimistic.
            return True
        provenance = payload.get("review_provenance") if isinstance(payload, dict) else None
        if isinstance(provenance, dict) and provenance.get(_EXPOSURE_KEY):
            return True
    return False


def _carried_exposure(already_exposed: bool) -> dict[str, Any] | None:
    """Provenance a later pass records when the set is already exposed (else none, unchanged)."""

    return {_EXPOSURE_KEY: True, "exposure_carried_forward": True} if already_exposed else None


def _commit_annotated_truth(
    truth_path: Path,
    pass_name: str,
    *,
    annotation: bytes | Callable[[bool], bytes],
    updated: GroundTruthRecord,
    base_sha256: str,
    work_root: Path | None = None,
    expected_dir_key: str | None = None,
) -> None:
    """Lock the record, hold and verify its set directory, recover, re-check, then commit the pair.

    Everything a pass decided from its earlier read is re-validated here, inside the lock and
    against the held directory: the truth bytes must be exactly those the pass was prepared from,
    the set must not be frozen, and no later pass may exist.  ``annotation`` may be a builder given
    whether the directory already records prediction exposure, so a replacement annotation can
    carry that flag forward rather than erase it.
    """

    truth_path = record_path(truth_path)
    with (
        truth_write_lock(truth_path),
        pinned_set_directory(
            truth_path.parent, expected_key=expected_dir_key, work_root=work_root
        ) as pinned,
    ):
        _recover(pinned, truth_path.name)
        current = pinned.read_bytes(truth_path.name)
        if current is None or sha256(current).hexdigest() != base_sha256:
            raise ValueError(
                f"{truth_path.name} changed on disk while this {pass_name} pass was being "
                "prepared; nothing was written, re-run the pass"
            )
        record = GroundTruthRecord.model_validate_json(current)
        refuse_frozen(truth_path, record)
        refuse_later_passes(record, pinned.exists, pass_name)
        content = (
            annotation(_pinned_exposure(pinned, truth_path)) if callable(annotation) else annotation
        )
        _commit_pair(
            pinned,
            truth_path.name,
            _annotation_path(truth_path, pass_name).name,
            annotation=content,
            truth=canonical_json_bytes(updated),
        )


def _write_first_pass_atomically(
    truth_path: Path,
    updated: GroundTruthRecord,
    *,
    annotator_ref: str,
    mode: str,
    content: dict[str, Any],
    base_sha256: str,
    provenance: dict[str, Any] | None = None,
    work_root: Path | None = None,
    expected_dir_key: str | None = None,
) -> None:
    """Replace the first-pass annotation and then the truth, as one recoverable transaction.

    Exposure is one-way: if the set already records that predictions were visible (the sidecar,
    or the annotation being replaced), the new annotation says so too, whatever provenance the
    caller supplied -- a re-verification can never make an exposed set look independent.
    """

    def build(already_exposed: bool) -> bytes:
        carried = provenance
        if already_exposed and not (isinstance(provenance, dict) and provenance.get(_EXPOSURE_KEY)):
            carried = {**(provenance or {}), _EXPOSURE_KEY: True, "exposure_carried_forward": True}
        return _annotation_bytes(
            updated.set_id,
            "first",
            annotator_ref=annotator_ref,
            mode=mode,
            content=content,
            provenance=carried,
        )

    _commit_annotated_truth(
        truth_path,
        "first",
        annotation=build,
        updated=updated,
        base_sha256=base_sha256,
        work_root=work_root,
        expected_dir_key=expected_dir_key,
    )


def _read_annotation_pass(truth_path: Path, pass_name: str) -> dict[str, Any]:
    path = _annotation_path(truth_path, pass_name)
    if is_link(path):
        raise link_refusal(path, path)
    if not path_is_file(path):
        raise ValueError(f"missing {pass_name} annotation pass: {path.name}")
    payload = json.loads(read_text(path))
    content = {
        "episodes": payload.get("episodes"),
        "events": payload.get("events") or [],
        "regions": payload.get("regions"),
    }
    digest = sha256(canonical_json_bytes(content)).hexdigest()
    if payload.get("pass") != pass_name or payload.get("content_sha256") != digest:
        raise ValueError(f"invalid {pass_name} annotation pass")
    return payload


def _independent_annotation_content(
    annotation: GroundTruthRecord, base: GroundTruthRecord
) -> dict[str, Any]:
    if annotation.set_id != base.set_id:
        raise ValueError("annotation set_id differs from the seeded truth")
    if annotation.source.media_key != base.source.media_key:
        raise ValueError("annotation media_key differs from the seeded truth")
    if annotation.source.duration_ms != base.source.duration_ms:
        raise ValueError("annotation duration differs from the seeded truth")
    for index, episode in enumerate(annotation.episodes):
        if episode.draft or episode.verified_against is None:
            raise ValueError(f"annotation episode {index} is not a completed work annotation")
    return _annotation_content(annotation)


def _load_independent_annotation(annotation_path: Path, base: GroundTruthRecord) -> dict[str, Any]:
    annotation = GroundTruthRecord.model_validate_json(read_text(annotation_path))
    return _independent_annotation_content(annotation, base)


def _truth_with_content(
    base: GroundTruthRecord,
    content: dict[str, Any],
    *,
    first_ref: str,
    second_ref: str | None,
    resolution: str | None,
) -> GroundTruthRecord:
    episodes = [
        {
            **episode,
            "annotator_ref": first_ref,
            "second_pass_ref": second_ref,
            "disagreement_resolution": resolution,
            "draft": False,
        }
        for episode in content["episodes"]
    ]
    return GroundTruthRecord.model_validate(
        {
            **base.model_dump(mode="json"),
            "episodes": episodes,
            "events": content.get("events") or [],
            "regions": content["regions"],
        }
    )


def verify_truth(
    truth_path: Path, *, corpus_root: Path | None = None, **options: Any
) -> GroundTruthRecord:
    """A first pass, run entirely inside the corpus mutation lock (:func:`corpus_write_lock`)."""

    with _record_mutation(truth_path, corpus_root, options.get("work_root")):
        return _verify_truth_locked(truth_path, **options)


def _verify_truth_locked(
    truth_path: Path,
    *,
    annotator_ref: str,
    audio: Path | None = None,
    annotation_path: Path | None = None,
    input_fn: Input = input,
    output_fn: Output = print,
    annotation_provenance: dict[str, Any] | None = None,
    annotation_record: GroundTruthRecord | None = None,
    work_root: Path | None = None,
    expected_dir_key: str | None = None,
) -> GroundTruthRecord:
    truth_path = record_path(truth_path)
    recover_interrupted_write(truth_path, work_root=work_root)
    truth, base_sha256 = _read_record(truth_path)
    refuse_frozen(truth_path, truth)
    # Checked early for a clear message, and again inside the write lock before committing.
    refuse_later_passes(truth, _sibling_exists(truth_path), "first")
    if annotation_path is not None and annotation_record is not None:
        raise ValueError("provide annotation_path or annotation_record, not both")
    if annotation_path is not None or annotation_record is not None:
        # This mode stamps one annotator on every row, so rows already verified by someone else
        # would be silently re-attributed: refuse rather than falsify provenance.
        refuse_reattribution(truth, annotator_ref)
        content = (
            _load_independent_annotation(annotation_path, truth)
            if annotation_path is not None
            else _independent_annotation_content(annotation_record, truth)
        )
        updated = _truth_with_content(
            truth,
            content,
            first_ref=annotator_ref,
            second_ref=None,
            resolution=None,
        )
        _write_first_pass_atomically(
            truth_path,
            updated,
            annotator_ref=annotator_ref,
            mode="independent",
            content=content,
            base_sha256=base_sha256,
            provenance=annotation_provenance,
            work_root=work_root,
            expected_dir_key=expected_dir_key,
        )
        return updated
    retained: list[Any] = []
    for index, episode in enumerate(truth.episodes):
        if not episode.draft:
            retained.append(episode)
            continue
        region_start = max(0, episode.start_ms_range[0] - 10_000)
        region_end = min(truth.source.duration_ms, episode.end_ms_range[1] + 10_000)
        output_fn(
            f"episode {index + 1}/{len(truth.episodes)}: "
            f"{episode.work.artist} - {episode.work.title}; "
            f"mix {region_start}..{region_end} ms"
        )
        output_fn(_ffplay_command(audio, region_start, region_end - region_start))
        decision = input_fn("decision [accept/reject/skip]: ").strip().casefold()
        if decision == "skip":
            retained.append(episode)
            continue
        if decision == "reject":
            continue
        if decision != "accept":
            raise ValueError(f"unknown decision: {decision}")
        start_range = _parse_range(
            input_fn("start range START_MS,END_MS (blank keeps draft): "), episode.start_ms_range
        )
        end_range = _parse_range(
            input_fn("end range START_MS,END_MS (blank keeps draft): "), episode.end_ms_range
        )
        verified_against = input_fn(
            "verified against [audio/source_recording/authoritative_metadata]: "
        ).strip()
        if verified_against not in {"audio", "source_recording", "authoritative_metadata"}:
            raise ValueError("invalid verified_against decision")
        version_verified = input_fn("exact version verified [y/n]: ").strip().casefold() in {
            "y",
            "yes",
        }
        roles = [
            TruthRoleSegment(
                from_ms=start_range[0],
                to_ms=end_range[1],
                role="dominant",
            )
        ]
        retained.append(
            episode.model_copy(
                update={
                    "start_ms_range": tuple(start_range),
                    "end_ms_range": tuple(end_range),
                    "verified_against": verified_against,
                    "version_verified": version_verified,
                    "annotator_ref": annotator_ref,
                    "role_segments": roles,
                    "draft": False,
                }
            )
        )
    updated = truth.model_copy(update={"episodes": retained})
    updated = GroundTruthRecord.model_validate(updated.model_dump(mode="json"))
    _write_first_pass_atomically(
        truth_path,
        updated,
        annotator_ref=annotator_ref,
        mode="seed-review",
        content=_annotation_content(updated),
        base_sha256=base_sha256,
        provenance=annotation_provenance,
        work_root=work_root,
        expected_dir_key=expected_dir_key,
    )
    return updated


def second_pass_truth(
    truth_path: Path, *, corpus_root: Path | None = None, **options: Any
) -> GroundTruthRecord:
    """A second pass, run entirely inside the corpus mutation lock."""

    with _record_mutation(truth_path, corpus_root, options.get("work_root")):
        return _second_pass_truth_locked(truth_path, **options)


def _second_pass_truth_locked(
    truth_path: Path,
    *,
    annotator_ref: str,
    audio: Path | None = None,
    annotation_path: Path | None = None,
    input_fn: Input = input,
    output_fn: Output = print,
    work_root: Path | None = None,
) -> GroundTruthRecord:
    truth_path = record_path(truth_path)
    recover_interrupted_write(truth_path, work_root=work_root)
    truth, base_sha256 = _read_record(truth_path)
    refuse_frozen(truth_path, truth)
    refuse_later_passes(truth, _sibling_exists(truth_path), "second")
    first = _read_annotation_pass(truth_path, "first")
    first_ref = str(first["annotator_ref"])
    if annotator_ref == first_ref:
        raise ValueError("second-pass annotator must differ from the first-pass annotator")
    if annotation_path is not None:
        second_content = _load_independent_annotation(annotation_path, truth)
        mode = "independent"
    else:
        if truth.split == "test":
            raise ValueError("test truth requires an independent second-pass annotation file")
        guided_episodes: list[Any] = []
        for index, episode in enumerate(truth.episodes):
            if episode.draft:
                raise ValueError("second pass cannot annotate a draft episode")
            output_fn(
                f"blind episode {index + 1}/{len(truth.episodes)}: "
                f"{episode.work.artist} - {episode.work.title}; first-pass boundaries hidden"
            )
            output_fn(_ffplay_command(audio, None, None))
            start_range = _parse_range(input_fn("blind start range START_MS,END_MS: "))
            end_range = _parse_range(input_fn("blind end range START_MS,END_MS: "))
            verified_against = input_fn(
                "blind verified against [audio/source_recording/authoritative_metadata]: "
            ).strip()
            if verified_against not in {"audio", "source_recording", "authoritative_metadata"}:
                raise ValueError("invalid verified_against decision")
            version_verified = input_fn(
                "blind exact version verified [y/n]: "
            ).strip().casefold() in {"y", "yes"}
            guided_episodes.append(
                episode.model_copy(
                    update={
                        "start_ms_range": tuple(start_range),
                        "end_ms_range": tuple(end_range),
                        "verified_against": verified_against,
                        "version_verified": version_verified,
                        "role_segments": [
                            TruthRoleSegment(
                                from_ms=start_range[0],
                                to_ms=end_range[1],
                                role="dominant",
                            )
                        ],
                    }
                )
            )
        guided = truth.model_copy(update={"episodes": guided_episodes})
        second_content = _annotation_content(
            GroundTruthRecord.model_validate(guided.model_dump(mode="json"))
        )
        mode = "guided"
    first_content = _pass_content(first)
    agrees = canonical_json_bytes(first_content) == canonical_json_bytes(second_content)
    resolution = "agreed" if agrees else "unresolved:third-annotator-required"
    updated = _truth_with_content(
        truth,
        first_content,
        first_ref=first_ref,
        second_ref=annotator_ref,
        resolution=resolution,
    )
    # One writer at a time per record: the annotation pass and the truth it justifies land
    # together, as one recoverable transaction, whichever command is writing them.
    _commit_annotated_truth(
        truth_path,
        "second",
        # Exposure recorded anywhere (sidecar, earlier pass, corpus ledger) is carried into this
        # pass's own file too, so it is never the only file left that says "independent".
        annotation=lambda already_exposed: _annotation_bytes(
            truth.set_id,
            "second",
            annotator_ref=annotator_ref,
            mode=mode,
            content=second_content,
            provenance=_carried_exposure(already_exposed),
        ),
        updated=updated,
        base_sha256=base_sha256,
        work_root=work_root,
    )
    return updated


def resolve_truth(
    truth_path: Path, *, corpus_root: Path | None = None, **options: Any
) -> GroundTruthRecord:
    """A third-annotator resolution, run entirely inside the corpus mutation lock."""

    with _record_mutation(truth_path, corpus_root, options.get("work_root")):
        return _resolve_truth_locked(truth_path, **options)


def _resolve_truth_locked(
    truth_path: Path,
    *,
    resolver_ref: str,
    annotation_path: Path,
    work_root: Path | None = None,
) -> GroundTruthRecord:
    truth_path = record_path(truth_path)
    recover_interrupted_write(truth_path, work_root=work_root)
    truth, base_sha256 = _read_record(truth_path)
    refuse_frozen(truth_path, truth)
    refuse_later_passes(truth, _sibling_exists(truth_path), "resolution")
    first = _read_annotation_pass(truth_path, "first")
    second = _read_annotation_pass(truth_path, "second")
    annotators = {str(first["annotator_ref"]), str(second["annotator_ref"])}
    if len(annotators) != 2:
        raise ValueError("first and second annotation passes must use distinct annotators")
    if resolver_ref in annotators:
        raise ValueError("resolver must be a third, distinct annotator")
    first_content = _pass_content(first)
    second_content = _pass_content(second)
    if canonical_json_bytes(first_content) == canonical_json_bytes(second_content):
        raise ValueError("matching passes do not need third-annotator resolution")
    resolved_content = _load_independent_annotation(annotation_path, truth)
    updated = _truth_with_content(
        truth,
        resolved_content,
        first_ref=str(first["annotator_ref"]),
        second_ref=str(second["annotator_ref"]),
        resolution=f"resolved-by:{resolver_ref}",
    )
    _commit_annotated_truth(
        truth_path,
        "resolution",
        annotation=lambda already_exposed: _annotation_bytes(
            truth.set_id,
            "resolution",
            annotator_ref=resolver_ref,
            mode="independent",
            content=resolved_content,
            provenance=_carried_exposure(already_exposed),
        ),
        updated=updated,
        base_sha256=base_sha256,
        work_root=work_root,
    )
    return updated


def _write_record_file(
    path: Path,
    content: bytes,
    *,
    work_root: Path | None,
    create_only: bool = False,
    record_lock: bool = True,
) -> None:
    """Replace one corpus file through the pinned writer, under the record's write lock.

    Everything is validated before anything is created: no existing component of the destination
    may be a link, and neither the destination nor the nearest existing ancestor it would be built
    under may lie beneath ``work_root``.  The directory identity computed then is the identity the
    pin must find afterwards, so a directory swapped for a link in between is refused.
    """

    absolute = Path(os.path.abspath(path))
    record_path(absolute)  # refuses a linked destination or any linked existing ancestor
    if work_root is not None and is_within(absolute, work_root):
        raise ValueError(
            f"refusing to write a truth record beneath the work tree: {path} resolves inside "
            f"{work_root}"
        )
    existing = nearest_existing_ancestor(absolute.parent)
    expected_key = os.path.normcase(
        str(real_path(existing) / absolute.parent.relative_to(existing))
    )
    os.makedirs(absolute.parent, exist_ok=True)
    target = record_path(absolute)
    # A record takes its record lock (corpus lock first); a manifest is written by a caller that
    # already holds the corpus mutation lock, so no second, differently keyed lock is taken.
    with (
        truth_write_lock(target) if record_lock else nullcontext(),
        pinned_set_directory(
            target.parent, expected_key=expected_key, work_root=work_root
        ) as pinned,
    ):
        if create_only and pinned.exists(target.name):
            raise ValueError(
                f"refusing to overwrite {path}: it already exists (it may be reviewed or frozen)"
            )
        _write_set_file(pinned, target.name, content)


def _freeze_plan(
    truth_dir: Path, work_root: Path | None, handle: CorpusHandle
) -> list[tuple[Path, str]]:
    """Every record a freeze will touch, validated before anything is locked or written.

    Returns each canonical record with the identity of its set directory *as validated here*.  The
    freeze pins exactly that identity (``expected_key``), so a set moved, or its name replaced by a
    link, between planning and pinning is refused rather than followed.
    """

    if not truth_dir.is_dir():
        raise ValueError(
            f"freeze takes the corpus directory, not a file: {truth_dir}; {SUPPORTED_LAYOUT}"
        )
    candidates = list(handle.truth_files)  # the gateway's vetted list, never a second walk
    if not candidates:
        raise ValueError("freeze found no ground_truth.json files")
    root = truth_dir
    records: list[tuple[Path, str]] = []
    for candidate in candidates:
        record = record_path(candidate)
        # rglob can walk a directory junction; a record that really lives outside the corpus
        # being frozen (or beneath work/) is not this corpus's record.
        if not is_within(record, root):
            raise ValueError(
                f"refusing to freeze {candidate}: it resolves outside {truth_dir}; pass the "
                "resolved real path of the corpus, whose sets are real directories inside it"
            )
        if work_root is not None and is_within(record, work_root):
            raise ValueError(
                f"refusing to freeze a truth record beneath the work tree: {candidate} resolves "
                f"inside {work_root}"
            )
        records.append((record, path_key(record.parent)))
    return records


def _manifest_destination(
    out_path: Path, records: list[Path], work_root: Path | None, truth_dir: Path
) -> Path:
    """A freeze manifest has exactly one place: ``corpus-version.json`` in the corpus directory.

    That is the single location every consumer finds -- ``idea benchmark score --truth <corpus>``,
    ``idea benchmark certify`` (corpus root only), scoring a set file (its corpus directory is
    searched) and review -- so a freeze can never produce a manifest some consumer misses.
    """

    if Path(out_path).name != FREEZE_MANIFEST_NAME:
        raise ValueError(
            f"the freeze manifest must be named {FREEZE_MANIFEST_NAME}, the only name the scorer, "
            "certification and review look for; got "
            f"{Path(out_path).name}"
        )
    manifest = record_path(out_path)
    if path_key(manifest.parent) != path_key(truth_dir):
        raise ValueError(
            f"the freeze manifest {out_path} must be written as {FREEZE_MANIFEST_NAME} in the "
            f"corpus directory being frozen ({truth_dir}), the one location scoring, certification "
            "and review all look for it"
        )
    for record in records:
        if path_key(manifest.parent) not in {
            path_key(directory) for directory in manifest_search_directories(record)
        }:
            raise ValueError(f"the freeze manifest {out_path} would not be found for {record}")
    if work_root is not None and is_within(manifest, work_root):
        raise ValueError(f"refusing to write a freeze manifest beneath the work tree: {out_path}")
    return manifest


def freeze_truth(
    truth_dir: Path,
    *,
    corpus_version: str,
    out_path: Path,
    work_root: Path | None = None,
    lock_timeout: float = 20.0,
) -> dict[str, Any]:
    """Freeze ``<corpus>`` into ``<corpus>/corpus-version.json`` under the corpus mutation lock.

    The lock is taken before the corpus is enumerated and held until the manifest is durable, so a
    draft inventory, seed, pass, save or reveal in the same corpus cannot interleave with it.
    """

    if not certification_enabled():
        raise ValueError(
            f"{CERTIFICATION_DISABLED}: freezing a corpus is part of certification, so freeze "
            f"refuses until then; nothing was read or written. {CERTIFICATION_DISABLED_NEXT_STEP}"
        )
    with open_corpus(
        truth_dir,
        mutate=True,
        work_root=work_root,
        timeout=lock_timeout,
        require_records=False,
        allow_frozen=True,  # a re-freeze is refused below with its own, more specific message
    ) as handle:
        return _freeze_truth_locked(
            truth_dir,
            handle=handle,
            corpus_version=corpus_version,
            out_path=out_path,
            work_root=work_root,
            lock_timeout=lock_timeout,
        )


def _freeze_truth_locked(
    truth_dir: Path,
    *,
    handle: CorpusHandle,
    corpus_version: str,
    out_path: Path,
    work_root: Path | None = None,
    lock_timeout: float = 20.0,
) -> dict[str, Any]:
    """Validate, lock, snapshot and publish a frozen corpus as one serialised step.

    Every record's write lock and set directory are held from before its exposure snapshot until
    after the manifest is published, so a reveal or save cannot land between "this set is
    independent" and the manifest that says so.  Truth files and the manifest are written through
    the pinned, link-refusing writer; the manifest may only be written where frozen status is
    discovered afterwards.
    """

    planned = _freeze_plan(truth_dir, work_root, handle)
    records = [record for record, _ in planned]
    # The directory identities the plan validated, captured by the plan itself: every pin is
    # checked against these, never recomputed from whatever the path resolves to by then.
    planned_dirs = {key: record.parent for record, key in planned}
    with ExitStack() as held:
        for record in sorted(records, key=path_key):
            held.enter_context(truth_write_lock(record, timeout=lock_timeout))
        pinned_dirs: dict[str, PinnedDirectory] = {}
        for key, directory in sorted(planned_dirs.items()):
            pinned_dirs[key] = held.enter_context(
                pinned_set_directory(directory, expected_key=key, work_root=work_root)
            )
        pinned_by_dir: dict[Path, PinnedDirectory] = {
            record: pinned_dirs[key] for record, key in planned
        }
        return _freeze_locked(
            truth_dir,
            records,
            pinned_by_dir,
            out_path=out_path,
            work_root=work_root,
            corpus_version=corpus_version,
        )


#: The most problems a refused freeze lists; a half-checked corpus has hundreds of draft rows.
FREEZE_REFUSAL_LIMIT = 25


def _freeze_refusal(truth_dir: Path, errors: list[str]) -> str:
    """The refusal a freeze gives: nothing changed, what is wrong, and what to do next."""

    shown = errors[:FREEZE_REFUSAL_LIMIT]
    more = len(errors) - len(shown)
    return (
        f"cannot freeze {truth_dir}: {len(errors)} problem(s) must be fixed first, and nothing "
        "was changed. A row that is still draft or has no verification has not been checked by "
        "ear yet: open its set with `idea truth review`, check it and save, then run freeze "
        "again. Freezing is final, so only freeze once every tracklist has been checked.\n"
        + "\n".join(shown)
        + (f"\n... and {more} more" if more else "")
    )


def _freeze_locked(
    truth_dir: Path,
    candidates: list[Path],
    pinned_by_dir: dict[Path, PinnedDirectory],
    *,
    out_path: Path,
    work_root: Path | None,
    corpus_version: str,
) -> dict[str, Any]:
    truths: list[tuple[Path, GroundTruthRecord]] = []
    exposures: dict[Path, dict[str, Any]] = {}
    errors: list[str] = []
    for path in candidates:
        pinned = pinned_by_dir[path]
        raw = pinned.read_bytes(path.name)
        if raw is None:
            raise ValueError(f"{path} disappeared before it could be frozen")
        truth = GroundTruthRecord.model_validate_json(raw)
        try:
            # Frozen is terminal for freeze too: re-freezing would rewrite every truth file and the
            # manifest, identical version or not.
            refuse_frozen(path, truth)
        except ValueError as exc:
            errors.append(str(exc))
        if pinned.exists(_transaction_name(path.name)):
            errors.append(
                f"{truth.set_id} has an interrupted write awaiting recovery "
                f"({transaction_path(path).name}); reopen it with idea truth review or re-run its "
                "last pass first"
            )
        try:
            exposures[path] = prediction_exposure(path)
        except ValueError as exc:
            errors.append(f"{truth.set_id} {exc}")
        for index, episode in enumerate(truth.episodes):
            prefix = f"{truth.set_id} episode {index}"
            if episode.draft:
                errors.append(f"{prefix} is still draft")
            if episode.verified_against is None or episode.annotator_ref is None:
                errors.append(f"{prefix} has no completed first-pass verification")
            if truth.split == "test" and episode.second_pass_ref is None:
                errors.append(f"{prefix} lacks the required blind second pass")
            if episode.disagreement_resolution and episode.disagreement_resolution.startswith(
                "unresolved"
            ):
                errors.append(f"{prefix} has an unresolved disagreement")
        if truth.split == "test":
            try:
                first = _read_annotation_pass(path, "first")
                second = _read_annotation_pass(path, "second")
                first_ref = str(first["annotator_ref"])
                second_ref = str(second["annotator_ref"])
                if first.get("set_id") != truth.set_id or second.get("set_id") != truth.set_id:
                    errors.append(f"{truth.set_id} annotation pass set_id differs")
                if first_ref == second_ref:
                    errors.append(f"{truth.set_id} annotation passes are not independent")
                if second.get("mode") != "independent":
                    errors.append(f"{truth.set_id} second pass was not independently authored")
                first_content = _pass_content(first)
                second_content = _pass_content(second)
                disagree = canonical_json_bytes(first_content) != canonical_json_bytes(
                    second_content
                )
                expected = first_content
                if disagree:
                    resolution = _read_annotation_pass(path, "resolution")
                    resolver_ref = str(resolution["annotator_ref"])
                    if resolution.get("set_id") != truth.set_id:
                        errors.append(f"{truth.set_id} resolution set_id differs")
                    if resolver_ref in {first_ref, second_ref}:
                        errors.append(f"{truth.set_id} resolver is not a distinct third annotator")
                    expected = {
                        "episodes": resolution["episodes"],
                        "events": resolution.get("events") or [],
                        "regions": resolution["regions"],
                    }
                    if any(
                        episode.disagreement_resolution != f"resolved-by:{resolver_ref}"
                        for episode in truth.episodes
                    ):
                        errors.append(f"{truth.set_id} does not record third-annotator resolution")
                elif any(episode.disagreement_resolution != "agreed" for episode in truth.episodes):
                    errors.append(f"{truth.set_id} does not record pass agreement")
                if any(
                    episode.annotator_ref != first_ref or episode.second_pass_ref != second_ref
                    for episode in truth.episodes
                ):
                    errors.append(f"{truth.set_id} final truth has inconsistent annotator refs")
                if canonical_json_bytes(_annotation_content(truth)) != canonical_json_bytes(
                    expected
                ):
                    errors.append(f"{truth.set_id} frozen truth differs from its annotation record")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"{truth.set_id} invalid independent annotation record: {exc}")
        truths.append((path, truth))
    if errors:
        raise ValueError(_freeze_refusal(truth_dir, errors))
    if len({truth.set_id for _, truth in truths}) != len(truths):
        raise ValueError("cannot freeze duplicate set_id values")
    manifest_file = _manifest_destination(out_path, candidates, work_root, real_path(truth_dir))
    with pinned_set_directory(
        manifest_file.parent, expected_key=path_key(manifest_file.parent), work_root=work_root
    ) as manifest_dir:
        return _publish_freeze(
            manifest_file.parent,
            truths,
            exposures,
            pinned_by_dir,
            manifest_dir,
            corpus_version=corpus_version,
        )


def _publish_freeze(
    manifest_root: Path,
    truths: list[tuple[Path, GroundTruthRecord]],
    exposures: dict[Path, dict[str, Any]],
    pinned_by_dir: dict[Path, PinnedDirectory],
    manifest_dir: PinnedDirectory,
    *,
    corpus_version: str,
) -> dict[str, Any]:
    # Hash the annotation passes from the held directories (links refused) before writing truth.
    passes: dict[Path, dict[str, str | None]] = {}
    for path, _ in truths:
        pinned = pinned_by_dir[path]
        passes[path] = {}
        for name in ("first", "second", "resolution"):
            content = pinned.read_bytes(_annotation_path(path, name).name)
            passes[path][name] = None if content is None else sha256(content).hexdigest()
    digests: dict[Path, str] = {}
    for path, truth in truths:
        frozen = truth.model_copy(update={"corpus_version": corpus_version})
        content = canonical_json_bytes(
            GroundTruthRecord.model_validate(frozen.model_dump(mode="json"))
        )
        _write_set_file(pinned_by_dir[path], path.name, content)
        digests[path] = sha256(content).hexdigest()
    # Entry paths are relative to the directory the manifest is actually written in -- the one the
    # verifier resolves them against -- whichever accepted placement that is.
    base = manifest_root

    def exposed(path: Path) -> bool:
        return bool(exposures[path]["predictions_visible_during_review"])

    # A set whose annotator saw IDea's predictions is frozen -- its truth may be accurate -- but
    # recorded as not independent: the evidence is hashed here, so deleting the sidecar or editing
    # the annotation afterwards is detected, and the scorer and certification refuse to count it.
    manifest = {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "corpus_version": corpus_version,
        "frozen": True,
        "exposed_sets": sorted(truth.set_id for path, truth in truths if exposed(path)),
        "sets": [
            {
                "set_id": truth.set_id,
                "path": path.relative_to(base).as_posix(),
                "sha256": digests[path],
                "annotation_passes": passes[path],
                "prediction_exposure": {
                    "predictions_visible_during_review": exposed(path),
                    "certifiable": certifiable_under_gate(not exposed(path)),
                    "evidence": exposures[path]["evidence"],
                    "ledger_entries": exposures[path]["ledger_entries"],
                },
            }
            for path, truth in sorted(truths, key=lambda item: item[1].set_id)
        ],
    }
    manifest_dir.write_bytes(FREEZE_MANIFEST_NAME, canonical_json_bytes(manifest))
    return manifest


def write_draft_manifest(
    truth_dir: Path, *, corpus_version: str, work_root: Path | None = None
) -> dict[str, Any]:
    """Inventory draft truth without implying that human verification has happened.

    The output is fixed to ``<corpus>/corpus-version.json`` -- there is no arbitrary destination, so
    a draft of corpus A can never target corpus B's manifest.  The whole operation runs through the
    gateway: link components refused, layout validated, a frozen corpus refused, and the vetted
    truth-file list read under the corpus lock, so a concurrent freeze of the same corpus can never
    be overwritten by a stale ``frozen: false`` inventory.
    """

    with open_corpus(truth_dir, mutate=True, work_root=work_root) as handle:
        entries: list[dict[str, Any]] = []
        for path in handle.truth_files:
            truth = GroundTruthRecord.model_validate_json(read_text(path))
            if not all(episode.draft for episode in truth.episodes):
                raise ValueError(
                    f"refusing to inventory {truth.set_id} as a draft: it holds reviewed "
                    "(non-draft) rows; a draft manifest lists only unreviewed seed drafts"
                )
            if truth.corpus_version != corpus_version:
                raise ValueError(f"{truth.set_id} corpus_version differs from {corpus_version}")
            entries.append(
                {
                    "set_id": truth.set_id,
                    "path": path.relative_to(handle.root).as_posix(),
                    "sha256": sha256_file(path),
                    "episode_count": len(truth.episodes),
                    "all_episodes_draft": all(episode.draft for episode in truth.episodes),
                }
            )
        manifest = {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "corpus_version": corpus_version,
            "frozen": False,
            "verification_status": "unverified_seed_drafts_not_truth",
            "warning": "Do not use these seeds as verified benchmark truth.",
            "sets": sorted(entries, key=lambda item: item["set_id"]),
        }
        _write_record_file(
            handle.manifest_path,
            canonical_json_bytes(manifest),
            work_root=work_root,
            record_lock=False,
        )
        return manifest
