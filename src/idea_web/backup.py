"""Backups and the restore drill (plan §4.6; risk R14), cycle 4b-iii.

Four properties, each structural rather than a check bolted on:

**The corpus is untouchable.** The owner's hand-verified truth corpus is the one thing in this tree
that cannot be regenerated. The frozen ``io`` backstop looks two directories up, which a nested
destination like ``data/corpus/dev-1/backups/snapshot`` walks straight past, so this module walks
**every ancestor to the filesystem root** and refuses if any of them is a corpus. That guard runs on
every final write, rename and deletion target.

**A backup owns its destination and publishes once.** The destination may not be, contain, or sit
inside the work tree or the source database's directory; a sealed destination is never reused.
Everything is assembled in an exclusively created staging directory and moved into place with a
single rename.

**A backup coexists with work, and never drops evidence silently.** A medium's artefact lock is
taken with one non-blocking attempt and held only while a *stable view* is recorded — each file and
its identity. Copying happens after release, and every member is compared against that identity
afterwards: a file that changed or vanished makes the medium be captured again, and if it changes a
second time the backup is refused. A snapshot that quietly lacks a journal would verify, and a later
restore would then delete the owner's surviving copy.

**Restore is one recoverable boundary.** Stage, flush, verify, then publish under a journal that
names every target and whether it had a predecessor. Every rename is a write-through replace
(``io.durable_replace``: ``MoveFileExW`` with ``WRITE_THROUGH`` on Windows, where a directory fsync
does not exist). An interrupted journal is consumed — every displaced original put back, every
publication that had no predecessor removed — before any database is opened or any new restore
begins (:func:`restore_excluded`, held by ``idea serve``'s startup until migration ends).
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from id_detector.io import (
    SCHEMA_VERSION as SIDECAR_SCHEMA_VERSION,
)
from id_detector.io import (
    durable_replace,
    fsync_file,
    native_path,
    path_is_file,
    read_text,
    refuse_corpus_destination,
    sha256_file,
)
from id_detector.jobs import JobStoreLocked, ProcessLock
from id_detector.present.bundles import read_bundle_manifest, read_manifest
from id_detector.retention import TERMINAL_STATUSES
from id_detector.truth import EXPOSURE_LEDGER_NAME, FREEZE_MANIFEST_NAME, TRUTH_RECORD_NAME
from id_detector.truth_paths import is_link
from idea_web.database import (
    LOCAL_DATABASE_DIR,
    LOCAL_DATABASE_NAME,
    SUPERVISOR_LOCK_NAME,
    Database,
)
from idea_web.durable import flush_directory, make_directories, move_directory, remove_file

SCHEMA_VERSION = "idea-snapshot-5"
SNAPSHOT_NAME = "snapshot.json"
DATABASE_NAME = "app.db"
ARTEFACTS_DIR = "artefacts"
STAGING_DIR = "restore-staging"
JOURNAL_NAME = "restore-journal.json"
#: Where a restore keeps what it replaced, inside its own staging tree.
REPLACED_DIR = ".replaced"
#: Database originals live under their own name inside that tree, because their target is named by
#: the journal rather than by an artefact path.
_REPLACED_DATABASE = ".database"
#: Content-addressed, and therefore safe to hard-link: a bundle directory is named
#: ``sha256(run_id, presentation_version)`` and a manifest fixes its bytes.
LINKED_KINDS = ("bundle",)
#: Directories a manifest seals, and whose semantics `verify_artefacts` proves.
SEALED_KINDS = ("bundle", "fuse_run")
_MEDIA_TREES = ("recognise", "hints", "enrich")
_MEDIA_FILES = (("ingest", "source.json"),)
_SQLITE_SIDECARS = ("-wal", "-shm")
#: The restore-owned footprint of every medium. Trees are owned recursively; ``present`` owns only
#: the files directly inside it (``current``, and the legacy flat result); ``ingest/source.json`` is
#: owned alone, because the original audio beside it is never part of a snapshot. ``fuse`` and
#: ``enrich`` are owned because the flat result's sidecars name them; ``local-cache`` holds the
#: hint evidence a snapshot seals (below).
_OWNED_TREES = ("present/bundles", "fuse", "enrich", "recognise", "hints", "local-cache")
_OWNED_FLAT = ("present",)
_OWNED_FILES = ("ingest/source.json",)
#: A directory holding any of these IS a corpus — the gateway's own names, never re-spelled here.
_CORPUS_MARKERS = (TRUTH_RECORD_NAME, FREEZE_MANIFEST_NAME, EXPOSURE_LEDGER_NAME)
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: How many times a medium is captured before a changing member makes the backup refuse.
CAPTURE_ATTEMPTS = 2
#: Held, with the supervisor lock, for a restore's whole life. A server that finds the supervisor
#: lock busy probes this one to learn whether the holder is a restore (refuse) or another window.
RESTORE_LOCK_NAME = "restore.lock"
RESTORE_IN_PROGRESS = "a restore is in progress; wait for it to finish"
#: The hint pipeline's evidence key (``hints/pipeline.py``): ``local-cache/<connector>/<job>/…``,
#: whose bytes live in the project's hint cache at ``<connector>/<source_key>/<job>/result.json``.
#: A snapshot seals those bytes into the medium at exactly that media-relative key.
EVIDENCE_DIR = "local-cache"
_POINTER_KEY = re.compile(r"[a-f0-9]{64}")
_EVIDENCE_KEY = re.compile(r"local-cache/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/result\.json")
#: The ONLY upstreams a snapshot may lack: decoded audio, analysis windows, and the original
#: media. None of them is ever snapshotted. Matched exactly, against the pipeline's own spellings.
_ALLOWED_MISSING = re.compile(
    r"decode/audio\.pcm|decode/pcm\.json"
    r"|windows/windows\.gen[0-9]+\.jsonl|windows/gen[0-9]+/[^/]+\.wav"
    r"|ingest/original(\.[A-Za-z0-9]+)?"
)
#: ``recognise.py`` keys the configuration snapshot as ``provider_configs/<name>`` and stores its
#: bytes beside the recognition invocation — i.e. beside the sidecar that names it.
_PROVIDER_CONFIG = re.compile(r"provider_configs/([A-Za-z0-9._-]+)")
#: Sidecars inside these are immutable copies sealed by their directory manifest. The frozen
#: retention skips them for the same reason (``retention.py``: "Their recorded hashes remain
#: evidence"): their upstreams name mutable files that have legitimately moved on, and the
#: manifest — verified separately — is what proves their bytes.
_SEALED_PREFIXES = ("fuse/runs/", "present/bundles/")


class BackupRefused(RuntimeError):
    """A backup or restore that could produce — or destroy — something untrue."""


class _CaptureChanged(RuntimeError):
    """A member changed or vanished between the locked view and the copy."""


class _EvidenceMissing(RuntimeError):
    """A hint sidecar names cache evidence whose recorded bytes cannot be found."""


@dataclass(frozen=True)
class Entry:
    """One file in the snapshot, addressed by its work-root-relative path."""

    path: str
    kind: str
    sha256: str
    size: int

    def document(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class Skipped:
    """Something the database names that this snapshot deliberately did not carry, and why."""

    media: str
    reason: str
    bundles: tuple[str, ...] = ()
    runs: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "media": self.media,
            "reason": self.reason,
            "bundles": list(self.bundles),
            "runs": list(self.runs),
        }


@dataclass(frozen=True)
class _Member:
    """One file in a medium's captured view: what it was, and what it was when we looked."""

    kind: str
    relative: str
    size: int
    mtime_ns: int
    #: Where the bytes come from, when that is not ``work_root / relative`` (sealed evidence).
    source: Path | None = None


# ---------------------------------------------------------------------------------------------
# Paths: relative, link-free, confined, and never inside a corpus
# ---------------------------------------------------------------------------------------------
def _plain(path: Path) -> Path:
    r"""``Path.resolve()`` without Windows' ``\\?\`` prefix, so ``relative_to`` keeps working."""

    text = str(Path(path).resolve())
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(text)


def _relative(root: Path, path: Path) -> str | None:
    try:
        return _plain(path).relative_to(_plain(root)).as_posix()
    except ValueError:
        return None


def _overlaps(one: Path, other: Path) -> bool:
    """True when either path is the other, or contains it."""

    first, second = _plain(one), _plain(other)
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def is_corpus_directory(directory: Path) -> bool:
    """Whether this directory itself is a corpus root or a truth-bearing set."""

    for marker in _CORPUS_MARKERS:
        if os.path.lexists(native_path(Path(directory) / marker)):
            return True
    # The conventional home, so an empty or not-yet-populated corpus is protected too.
    name = Path(directory).name.casefold()
    parent = Path(directory).parent.name.casefold()
    return name == "corpus" and parent == "data"


class CorpusGuard:
    """Refuses any path lying anywhere beneath a corpus, walking ancestors to the filesystem root.

    ``io.refuse_corpus_destination`` only inspects a destination's parent and grandparent, so a
    nested destination slips past it. A backup chooses whole directory trees, so it walks the whole
    way up. Results are cached per directory: one snapshot touches thousands of files through a
    handful of directories.
    """

    def __init__(self) -> None:
        self._cleared: set[Path] = set()

    def check(self, path: Path) -> Path:
        target = _plain(path)
        directory = target if _is_dir(target) else target.parent
        if directory in self._cleared:
            self._refuse_name(target)
            return target
        for ancestor in (directory, *directory.parents):
            if ancestor in self._cleared:
                break
            if is_corpus_directory(ancestor):
                raise BackupRefused(
                    f"refusing to touch {target}: {ancestor} is a corpus, and the hand-verified "
                    "truth corpus is never written by a backup or a restore"
                )
        self._cleared.add(directory)
        self._refuse_name(target)
        return target

    @staticmethod
    def _refuse_name(target: Path) -> None:
        if target.name.casefold() in {name.casefold() for name in _CORPUS_MARKERS}:
            raise BackupRefused(f"refusing to touch {target}: that is a corpus file")


def refuse_link_root(path: Path, what: str) -> Path:
    """A root may not itself be a symlink, junction or reparse point."""

    candidate = Path(path)
    if is_link(candidate):
        raise BackupRefused(f"the {what} is a link or junction, which is never followed: {path}")
    return _plain(candidate)


def safe_entry_path(relative: str) -> str:
    """One snapshot or journal path, or a refusal."""

    if not isinstance(relative, str) or not relative or relative.strip() != relative:
        raise BackupRefused(f"unsafe snapshot entry path: {relative!r}")
    if "\\" in relative or ":" in relative or relative.startswith("/"):
        raise BackupRefused(f"unsafe snapshot entry path: {relative!r}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise BackupRefused(f"unsafe snapshot entry path: {relative!r}")
    return pure.as_posix()


def contained(root: Path, relative: str) -> Path:
    """``root / relative``, proven to stay under ``root`` with no redirecting name anywhere."""

    relative = safe_entry_path(relative)
    root = refuse_link_root(root, "root")
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate = candidate / part
        if is_link(candidate):
            raise BackupRefused(f"snapshot path passes through a link or junction: {relative}")
    if _relative(root, candidate) is None:
        raise BackupRefused(f"snapshot path escapes its root: {relative}")
    return candidate


def _mkdirs(directory: Path) -> None:
    r"""``mkdir(parents=True)`` through the extended-length form (Windows' 260-character limit)."""

    Path(native_path(directory)).mkdir(parents=True, exist_ok=True)


def _is_dir(path: Path) -> bool:
    r"""``is_dir()`` through the extended-length form; plain pathlib lies past 260 characters."""

    return Path(native_path(path)).is_dir()


def _exists(path: Path) -> bool:
    return Path(native_path(path)).exists()


def _files(directory: Path) -> Iterator[Path]:
    """Every real file under ``directory``; a symlink or junction is never followed."""

    for path in sorted(Path(native_path(directory)).rglob("*")):
        if is_link(path) or not path.is_file():
            continue
        yield path


def _directories(root: Path) -> list[Path]:
    """Every real directory under ``root``, deepest first — the order a flush must go in."""

    found = [
        path for path in Path(native_path(root)).rglob("*") if path.is_dir() and not is_link(path)
    ]
    return sorted(found, key=lambda item: len(item.parts), reverse=True)


def _rename(source: Path, destination: Path, guard: CorpusGuard) -> None:
    """A durable file replace: write-through on Windows, then both parents flushed for real.

    ``io.fsync_directory`` is a no-op on Windows, so the flushes use :mod:`idea_web.durable`.
    """

    guard.check(destination)
    durable_replace(source, destination)
    flush_directory(Path(destination).parent)
    if _plain(Path(source).parent) != _plain(Path(destination).parent):
        flush_directory(Path(source).parent)


def _remove(path: Path, guard: CorpusGuard) -> None:
    """A durable deletion: the entry's removal is flushed before this returns."""

    guard.check(path)
    remove_file(path)


def _write_json(path: Path, document: dict[str, Any], guard: CorpusGuard | None = None) -> None:
    """Write one JSON document durably: a flushed temporary beside it, then a durable replace."""

    (guard or CorpusGuard()).check(path)
    refuse_corpus_destination(path)
    _mkdirs(path.parent)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}"
    temporary.write_bytes(json.dumps(document, indent=2, sort_keys=True).encode("utf-8"))
    durable_replace(temporary, path)


# ---------------------------------------------------------------------------------------------
# Taking a snapshot
# ---------------------------------------------------------------------------------------------
@dataclass
class _Plan:
    """What one medium owes the snapshot."""

    wanted: list[tuple[str, str]]
    bundles: list[str]
    runs: list[str]


def _referenced(
    connection: sqlite3.Connection, work_root: Path
) -> tuple[dict[Path, _Plan], list[Skipped]]:
    """What the database or a current-result pointer names, grouped by medium.

    Startup upkeep can publish a re-fused result without creating an ``analysis_runs`` or
    ``result_bundles`` row.  Its plain ``present/current`` pointer is therefore an independent
    root of the backup graph, not merely an extra file of a database-discovered medium.  The
    pointer is followed and verified later while that medium's lock is held.
    """

    plans: dict[Path, _Plan] = {}
    skipped: list[Skipped] = []

    def plan_for(media_dir: Path) -> _Plan:
        return plans.setdefault(media_dir, _Plan([], [], []))

    for row in connection.execute("SELECT bundle_id, path FROM result_bundles"):
        directory = _plain(Path(str(row["path"])))
        relative = _relative(work_root, directory)
        if relative is None:
            raise BackupRefused(
                f"bundle {row['bundle_id']} is outside the work root and cannot be backed up: "
                f"{directory}"
            )
        plan = plan_for(directory.parents[2])
        plan.bundles.append(str(row["bundle_id"]))
        plan.wanted.append(("bundle", relative))
    for row in connection.execute("SELECT run_id, media_key, status FROM analysis_runs"):
        run_id, media_key, status = str(row["run_id"]), str(row["media_key"]), str(row["status"])
        media_dirs = [
            _plain(media)
            for media in sorted(Path(native_path(work_root)).glob(f"*/{media_key}"))
            if _is_dir(media) and not is_link(media)
        ]
        if status not in TERMINAL_STATUSES:
            where = _relative(work_root, media_dirs[0]) if media_dirs else media_key
            skipped.append(
                Skipped(str(where), f"run is still in flight ({status})", runs=(run_id,))
            )
            continue
        if not media_dirs:
            skipped.append(
                Skipped(
                    media_key,
                    "no media directory for this terminal run; nothing of it is on disk",
                    runs=(run_id,),
                )
            )
            continue
        for media_dir in media_dirs:
            plan = plan_for(media_dir)
            plan.runs.append(run_id)
            relative = _relative(work_root, media_dir / "fuse" / "runs" / run_id)
            if relative is not None:
                plan.wanted.append(("fuse_run", relative))
    for current in sorted(Path(native_path(work_root)).glob("*/*/present/current")):
        media_dir = current.parents[1]
        if (
            path_is_file(current)
            and not is_link(current)
            and _is_dir(media_dir)
            and not is_link(media_dir)
        ):
            try:
                key = read_text(current).strip()
            except OSError:
                continue
            if _POINTER_KEY.fullmatch(key):
                plan_for(_plain(media_dir))
    return plans, skipped


#: One thing a medium owes the snapshot: its kind, its snapshot path, and where its bytes are when
#: that is not ``work_root / path``.
_Wanted = tuple[str, str, "Path | None"]


def _default_hint_cache() -> Path:
    from id_detector.cli import PROJECT_ROOT

    return Path(PROJECT_ROOT) / "data" / "local" / "hints"


def _evidence(
    work_root: Path, media_dir: Path, hint_cache: Path | None
) -> list[tuple[str, str, Path]]:
    """The hint-cache bytes this medium's hint sidecars name, found by their recorded digest.

    Raises :class:`_EvidenceMissing` when a plainly recorded piece cannot be found: a snapshot
    that seals a sidecar without its evidence could never verify, and one that silently dropped
    the sidecar would misdescribe the medium.
    """

    hints = media_dir / "hints"
    if not _is_dir(hints):
        return []
    media_relative = _relative(work_root, media_dir)
    if media_relative is None:
        return []
    found: dict[str, tuple[str, str, Path]] = {}
    for sidecar in _files(hints):
        if not sidecar.name.endswith(".done.json"):
            continue
        try:
            upstream = json.loads(read_text(sidecar)).get("upstream")
        except (OSError, ValueError):
            continue  # judged, and reported, by verification
        if not isinstance(upstream, dict):
            continue
        for logical, expected in sorted(upstream.items()):
            match = _EVIDENCE_KEY.fullmatch(str(logical))
            if match is None or not isinstance(expected, str) or logical in found:
                continue  # a pruned marker is satisfied by absence
            connector, job = match.groups()
            located = None
            if hint_cache is not None and _is_dir(hint_cache / connector):
                pattern = f"*/{glob.escape(job)}/result.json"
                for candidate in sorted(Path(native_path(hint_cache / connector)).glob(pattern)):
                    if (
                        candidate.is_file()
                        and not is_link(candidate)
                        and sha256_file(candidate) == expected
                    ):
                        located = candidate
                        break
            if located is None:
                raise _EvidenceMissing(str(logical))
            relative = safe_entry_path(f"{media_relative}/{logical}")
            found[str(logical)] = ("evidence", relative, located)
    return list(found.values())


def _media_extras(
    work_root: Path, media_dir: Path, hint_cache: Path | None = None
) -> list[_Wanted]:
    """The per-medium artefacts beside the sealed directories — the rest of the owned footprint."""

    extras: list[_Wanted] = []
    for name in _MEDIA_TREES:
        relative = _relative(work_root, media_dir / name)
        if relative is not None and _is_dir(media_dir / name):
            extras.append((name, relative, None))
    fuse = media_dir / "fuse"
    if _is_dir(fuse):
        # The live fuse outputs the flat result's sidecars name; frozen runs come via the database.
        for child in sorted(Path(native_path(fuse)).iterdir()):
            if child.name == "runs" or is_link(child):
                continue
            relative = _relative(work_root, child)
            if relative is not None and (child.is_file() or child.is_dir()):
                extras.append(("fuse", relative, None))
    for parts in _MEDIA_FILES:
        path = media_dir.joinpath(*parts)
        relative = _relative(work_root, path)
        if relative is not None and path_is_file(path):
            extras.append(("source", relative, None))
    present = media_dir / "present"
    if _is_dir(present):
        # ``present/current`` and a legacy flat result: restore owns them, so backup carries them.
        for child in sorted(Path(native_path(present)).iterdir()):
            if child.is_file() and not is_link(child):
                relative = _relative(work_root, child)
                if relative is not None:
                    extras.append(("present", relative, None))
    extras.extend(_evidence(work_root, media_dir, hint_cache))
    return extras


def _pointed_at(work_root: Path, media_dir: Path) -> list[tuple[str, str]]:
    """What ``present/current`` names: the sealed bundle and the frozen run that bundle references.

    The database is not the only author of a current result: an offline RE-FUSION
    (:mod:`id_detector.refusion`) and a stale-page refresh publish a bundle, and move the pointer,
    without an ``analysis_runs`` or ``result_bundles`` row.  A snapshot that carried the pointer
    but not what it points at would restore to the OLDER result; so the pointer is followed.
    Only a bundle and run that verify are added — a dangling or damaged pointer adds nothing, and
    the medium is captured exactly as it was before this rule.
    """

    try:
        key = read_text(media_dir / "present" / "current").strip()
    except OSError:
        return []
    if not _POINTER_KEY.fullmatch(key):
        return []
    bundle = media_dir / "present" / "bundles" / key
    manifest = read_bundle_manifest(bundle) if _is_dir(bundle) and not is_link(bundle) else None
    if manifest is None:
        return []
    wanted: list[tuple[str, str]] = []
    relative = _relative(work_root, bundle)
    if relative is not None:
        wanted.append(("bundle", relative))
    fuse_run = (media_dir / str(manifest["fuse_run"])).resolve()
    runs = (media_dir / "fuse" / "runs").resolve()
    if (
        fuse_run.is_relative_to(runs)
        and fuse_run.parent == runs
        and not is_link(fuse_run)
        and read_manifest(fuse_run) is not None
    ):
        run_relative = _relative(work_root, media_dir / "fuse" / "runs" / fuse_run.name)
        if run_relative is not None:
            wanted.append(("fuse_run", run_relative))
    return wanted


def _verified_manifest(directory: Path, kind: str) -> dict[str, Any] | None:
    return read_bundle_manifest(directory) if kind == "bundle" else read_manifest(directory)


def _view(work_root: Path, required: Sequence[_Wanted]) -> list[_Member]:
    """Under the artefact lock: every file and its identity. Proportional to entries, not bytes.

    A file that is already gone here is simply not in this view; the caller compares views across
    attempts, so something seen once and then lost is never dropped silently.
    """

    members: dict[str, _Member] = {}
    for kind, relative, origin in required:
        if origin is not None:
            relative = safe_entry_path(relative)
            if relative in members:
                continue
            try:
                status = Path(native_path(origin)).stat()
            except FileNotFoundError:
                continue
            members[relative] = _Member(
                kind, relative, status.st_size, status.st_mtime_ns, Path(origin)
            )
            continue
        source_path = work_root / relative
        if path_is_file(source_path):
            found = [source_path]
        elif _is_dir(source_path):
            found = list(_files(source_path))
        else:
            continue
        for member in found:
            member_relative = _relative(work_root, member)
            if member_relative is None:
                continue
            member_relative = safe_entry_path(member_relative)
            if member_relative in members:
                continue
            try:
                status = Path(native_path(member)).stat()
            except FileNotFoundError:
                continue
            members[member_relative] = _Member(
                kind, member_relative, status.st_size, status.st_mtime_ns
            )
    return list(members.values())


def _copy_view(
    work_root: Path,
    staging: Path,
    members: Sequence[_Member],
    guard: CorpusGuard,
    written: list[Path],
) -> tuple[int, int]:
    """Outside the lock: bring each member into staging, flush it, and prove it did not change.

    Every staged path is appended to ``written`` as soon as it exists, so a caller can discard a
    medium's partial capture before trying again. A member that changed or vanished raises
    :class:`_CaptureChanged`; any other copy failure refuses the backup. Nothing is skipped.
    """

    linked = copied = 0
    for member in members:
        source = member.source if member.source is not None else work_root / member.relative
        destination = contained(staging / ARTEFACTS_DIR, member.relative)
        guard.check(destination)
        refuse_corpus_destination(destination)
        _mkdirs(destination.parent)
        try:
            made_link = False
            if member.kind in LINKED_KINDS:
                try:
                    os.link(native_path(source), native_path(destination))
                    made_link = True
                except FileNotFoundError:
                    raise
                except OSError:
                    made_link = False  # a different filesystem: copy instead
            if not made_link:
                shutil.copy2(native_path(source), native_path(destination))
            written.append(destination)
            if not made_link:
                fsync_file(destination)
            after = Path(native_path(source)).stat()
            staged_size = Path(native_path(destination)).stat().st_size
        except FileNotFoundError:
            raise _CaptureChanged(member.relative) from None
        except OSError as exc:
            raise BackupRefused(f"could not capture {member.relative}: {exc}") from None
        if (after.st_size, after.st_mtime_ns) != (member.size, member.mtime_ns) or (
            staged_size != member.size
        ):
            raise _CaptureChanged(member.relative)
        if made_link:
            linked += 1
        else:
            copied += 1
    return linked, copied


def _discard(paths: Sequence[Path]) -> None:
    for path in reversed(paths):
        with contextlib.suppress(FileNotFoundError):
            os.remove(native_path(path))


def _try_lock(media_dir: Path) -> ProcessLock | None:
    """One non-blocking attempt at a medium's artefact lock. Never waits, never retries."""

    lock = ProcessLock(media_dir / ".media.lock")
    try:
        lock.acquire()
    except JobStoreLocked:
        return None
    return lock


def backup(
    database: Database,
    work_root: Path,
    destination: Path,
    *,
    now: float | None = None,
    hint_cache: Path | None = None,
) -> dict[str, Any]:
    """Take one snapshot of ``database`` and the artefacts it names; returns ``snapshot.json``.

    ``hint_cache`` is where the hint pipeline keeps its connector results (read only); by default
    the project's own ``data/local/hints``.
    """

    moment = time.time() if now is None else now
    if hint_cache is None:
        hint_cache = _default_hint_cache()
    guard = CorpusGuard()
    work_root = refuse_link_root(work_root, "work root")
    destination = Path(destination)
    if _exists(destination):
        destination = refuse_link_root(destination, "backup destination")
    else:
        refuse_link_root(destination.parent, "backup destination's parent")
        destination = _plain(destination)
    source_database = _plain(Path(database.path))

    guard.check(destination)
    guard.check(source_database)
    for guarded, what in ((work_root, "work tree"), (source_database.parent, "source database")):
        if _overlaps(destination, guarded):
            raise BackupRefused(
                f"a backup destination may not overlap the {what}: {destination} and {guarded}"
            )
    if _exists(destination / SNAPSHOT_NAME):
        raise BackupRefused(
            f"destination already holds a sealed snapshot; snapshots are never rewritten: "
            f"{destination}"
        )
    if _exists(destination) and any(Path(native_path(destination)).iterdir()):
        raise BackupRefused(f"backup destination is not empty: {destination}")
    refuse_corpus_destination(destination / SNAPSHOT_NAME)

    staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex[:8]}"
    guard.check(staging)
    Path(native_path(staging)).mkdir(parents=True, exist_ok=False)
    try:
        # (1) The online database copy: a consistent point in time of its own.
        staged_database = staging / DATABASE_NAME
        guard.check(staged_database)
        with database.read() as source:
            target = sqlite3.connect(staged_database)
            try:
                source.backup(target)
            finally:
                target.close()
        fsync_file(staged_database)

        copy = sqlite3.connect(staged_database)
        copy.row_factory = sqlite3.Row
        try:
            plans, skipped = _referenced(copy, work_root)
        finally:
            copy.close()

        linked = copied = 0
        busy: set[Path] = set()
        for media_dir in sorted(plans):
            plan = plans[media_dir]
            media_relative = _relative(work_root, media_dir) or str(media_dir)

            def skip(reason: str, plan: _Plan = plan, where: str = media_relative) -> None:
                skipped.append(
                    Skipped(
                        where,
                        reason,
                        tuple(sorted(set(plan.bundles))),
                        tuple(sorted(set(plan.runs))),
                    )
                )

            # Every member any attempt has seen. A later view that lacks one of them is not a
            # smaller medium, it is an unstable one: the whole medium is skipped, with the reason.
            seen: set[str] = set()
            for attempt in range(1, CAPTURE_ATTEMPTS + 1):
                # (2) One non-blocking attempt; the lock is held ONLY for the listing.
                lock = _try_lock(media_dir)
                if lock is None:
                    busy.add(media_dir)
                    skip(
                        "media is active: an analysis or a bundle commit holds the artefact lock"
                        if attempt == 1
                        else "media became active while it was being captured"
                    )
                    break
                try:
                    # Under the lock, so the pointer and what it names are read as one state.
                    for item in _pointed_at(work_root, media_dir):
                        if item not in plan.wanted:
                            plan.wanted.append(item)
                    required: list[_Wanted] = [
                        (kind, relative, None) for kind, relative in sorted(set(plan.wanted))
                    ]
                    required += _media_extras(work_root, media_dir, hint_cache)
                    members = _view(work_root, required)
                except _EvidenceMissing as exc:
                    busy.add(media_dir)
                    skip(
                        f"hint evidence {exc} is missing from the hint cache, so this medium's "
                        "hint results could not be proven"
                    )
                    break
                finally:
                    lock.release()  # (3) before a single byte is copied or hashed
                current = {member.relative for member in members}
                lost = sorted(seen - current)
                if lost:
                    busy.add(media_dir)
                    skip(
                        f"{lost[0]} vanished while the medium was being captured; the medium is "
                        "left out whole rather than captured without it"
                    )
                    break
                seen |= current
                written: list[Path] = []
                try:
                    one, two = _copy_view(work_root, staging, members, guard, written)
                except _CaptureChanged as exc:
                    _discard(written)
                    if attempt == CAPTURE_ATTEMPTS:
                        raise BackupRefused(
                            f"{exc} changed while it was being captured, on every attempt; "
                            "refusing to seal a snapshot that would omit or misdescribe it"
                        ) from None
                    continue
                linked += one
                copied += two
                break

        # (4) Outside every lock: prove what was captured, then hash it.
        entries: list[Entry] = []
        manifests: dict[str, str] = {}
        root = staging / ARTEFACTS_DIR
        for media_dir in sorted(plans):
            if media_dir in busy:
                continue
            for kind, relative in sorted(set(plans[media_dir].wanted)):
                staged = root / relative
                manifest = _verified_manifest(staged, kind) if _is_dir(staged) else None
                if kind == "bundle" and manifest is None:
                    raise BackupRefused(
                        f"result bundle is missing or does not verify, so the snapshot would be "
                        f"incomplete: {relative}"
                    )
                if manifest is None:
                    skipped.append(
                        Skipped(
                            _relative(work_root, media_dir) or str(media_dir),
                            "the run's frozen artefacts are missing or do not verify",
                            runs=(relative.rsplit("/", 1)[-1],),
                        )
                    )
                    continue
                manifests[safe_entry_path(relative)] = sha256_file(staged / "manifest.json")
        for path in _files(root):
            relative = _relative(root, path)
            if relative is None:
                continue
            relative = safe_entry_path(relative)
            entries.append(
                Entry(
                    relative,
                    _kind_of(relative),
                    sha256_file(path),
                    Path(native_path(path)).stat().st_size,
                )
            )

        document = _seal_into(
            staging,
            entries=entries,
            manifests=manifests,
            created_at=moment,
            skipped=skipped,
            work_root=work_root,
            linked=linked,
            copied=copied,
            guard=guard,
        )

        # (5) Flush every directory this backup created, deepest first, then publish once with a
        # write-through directory move whose parent is flushed before this returns.
        for directory in _directories(staging):
            flush_directory(directory)
        flush_directory(staging)
        guard.check(destination)
        if _exists(destination):
            Path(native_path(destination)).rmdir()  # proven empty above
        move_directory(staging, destination)
        staging = None  # type: ignore[assignment]
        return document
    finally:
        if staging is not None:
            shutil.rmtree(native_path(staging), ignore_errors=True)


def _seal_into(
    directory: Path,
    *,
    entries: Sequence[Entry],
    manifests: dict[str, str],
    created_at: float,
    skipped: Sequence[Skipped] = (),
    work_root: Path | None = None,
    linked: int | None = None,
    copied: int | None = None,
    guard: CorpusGuard | None = None,
) -> dict[str, Any]:
    database_path = directory / DATABASE_NAME
    document = {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "work_root": str(work_root) if work_root is not None else None,
        "database": {
            "path": DATABASE_NAME,
            "sha256": sha256_file(database_path),
            "size": Path(native_path(database_path)).stat().st_size,
        },
        "artefacts": [entry.document() for entry in sorted(entries, key=lambda item: item.path)],
        "manifests": dict(sorted(manifests.items())),
        "skipped": [
            item.document() for item in sorted(skipped, key=lambda item: (item.media, item.reason))
        ],
        "linked": linked if linked is not None else 0,
        "copied": copied if copied is not None else len(entries),
    }
    _write_json(directory / SNAPSHOT_NAME, document, guard)
    return document


def seal(
    destination: Path,
    *,
    entries: Sequence[Entry],
    manifests: dict[str, str],
    created_at: float,
    skipped: Sequence[Skipped] = (),
    work_root: Path | None = None,
) -> dict[str, Any]:
    """Seal a snapshot assembled by some route other than :func:`backup` (the drill fixture)."""

    listing = list(entries)
    return _seal_into(
        Path(destination),
        entries=listing,
        manifests=manifests,
        created_at=created_at,
        skipped=skipped,
        work_root=work_root,
        linked=0,
        copied=len(listing),
    )


def _kind_of(relative: str) -> str:
    """The kind of a ``<source>/<media>/…`` path, judged by its place inside the medium."""

    within = relative.split("/")[2:]
    if within[:2] == ["present", "bundles"]:
        return "bundle"
    if within[:2] == ["fuse", "runs"]:
        return "fuse_run"
    if within[:1] == [EVIDENCE_DIR]:
        return "evidence"
    if within[:1] == ["fuse"]:
        return "fuse"
    if within[:1] and within[0] in _MEDIA_TREES:
        return within[0]
    if len(within) == 2 and within[0] == "present":
        return "present"
    return "source"


def describe(destination: Path) -> dict[str, Any]:
    """Derive a snapshot's listing from the artefacts already in it."""

    destination = Path(destination)
    root = destination / ARTEFACTS_DIR
    entries: list[Entry] = []
    manifests: dict[str, str] = {}
    for path in _files(root):
        relative = _relative(root, path)
        if relative is None:
            continue
        relative = safe_entry_path(relative)
        kind = _kind_of(relative)
        entries.append(
            Entry(relative, kind, sha256_file(path), Path(native_path(path)).stat().st_size)
        )
        if path.name == "manifest.json" and kind in SEALED_KINDS:
            parent = _relative(root, path.parent)
            if parent is not None:
                manifests[safe_entry_path(parent)] = sha256_file(path)
    return {"entries": entries, "manifests": manifests}


def read_snapshot(destination: Path) -> dict[str, Any]:
    document = json.loads(read_text(Path(destination) / SNAPSHOT_NAME))
    if document.get("schema_version") != SCHEMA_VERSION:
        raise BackupRefused(f"unknown snapshot schema: {document.get('schema_version')!r}")
    return document


def entries_of(document: dict[str, Any]) -> list[Entry]:
    """The listing, each path proven safe, and each path listed once.

    A duplicate would let a tampered snapshot publish two different bytes to one target — the
    second silently replacing the first, and the rollback record for it.
    """

    entries: list[Entry] = []
    seen: set[str] = set()
    for item in document["artefacts"]:
        path = safe_entry_path(str(item["path"]))
        folded = path.casefold()
        if folded in seen:
            raise BackupRefused(f"snapshot lists one path twice: {path}")
        seen.add(folded)
        entries.append(Entry(path, str(item["kind"]), str(item["sha256"]), int(item["size"])))
    return entries


def verify_snapshot(destination: Path) -> list[str]:
    """Every listed artefact exists and hashes as recorded, and so does the database copy."""

    destination = Path(destination)
    errors: list[str] = []
    document = read_snapshot(destination)
    database_path = destination / DATABASE_NAME
    if not path_is_file(database_path):
        errors.append("database copy is missing")
    elif sha256_file(database_path) != document["database"]["sha256"]:
        errors.append("database copy hash differs")
    try:
        entries = entries_of(document)
    except BackupRefused as exc:
        return errors + [str(exc)]
    for entry in entries:
        try:
            path = contained(destination / ARTEFACTS_DIR, entry.path)
        except BackupRefused as exc:
            errors.append(str(exc))
            continue
        if not path_is_file(path):
            errors.append(f"missing artefact: {entry.path}")
            continue
        if sha256_file(path) != entry.sha256:
            errors.append(f"artefact hash differs: {entry.path}")
    return errors


def verify_artefacts(work_root: Path, document: dict[str, Any]) -> list[str]:
    """A tree's own proof: every sealed directory is complete and verifies, sidecars included."""

    work_root = _plain(work_root)
    errors: list[str] = []
    entries = entries_of(document)
    sealed = {entry.path.rsplit("/", 1)[0] for entry in entries if entry.kind in SEALED_KINDS}
    recorded = dict(document.get("manifests", {}))
    for directory in sorted(sealed - set(recorded)):
        errors.append(f"sealed directory has no recorded manifest: {directory}")
    for relative, digest in sorted(recorded.items()):
        try:
            directory = contained(work_root, relative)
        except BackupRefused as exc:
            errors.append(str(exc))
            continue
        manifest = directory / "manifest.json"
        if not path_is_file(manifest):
            errors.append(f"missing manifest: {relative}")
            continue
        if sha256_file(manifest) != digest:
            errors.append(f"manifest hash differs: {relative}")
        kind = "bundle" if "/present/bundles/" in f"/{relative}" else "fuse_run"
        if _verified_manifest(directory, kind) is None:
            errors.append(f"manifest does not verify: {relative}")
    for entry in entries:
        if entry.path.endswith(".done.json"):
            errors.extend(_verify_sidecar(work_root, entry.path))
    return errors


def verify(destination: Path) -> list[str]:
    """The whole check a snapshot can answer on its own: hashes, then artefact semantics."""

    problems = verify_snapshot(destination)
    document = read_snapshot(destination)
    try:
        return problems + verify_artefacts(Path(destination) / ARTEFACTS_DIR, document)
    except BackupRefused as exc:
        return problems + [str(exc)]


def _verify_sidecar(work_root: Path, relative: str) -> list[str]:
    """A completion sidecar, judged by the frozen verifier's rules (`io.verify_completion_sidecar`).

    - The schema version must match, and so must the artefact's own hash.
    - Every recorded digest is a lowercase 64-hex string, or ``{"pruned_upstream": <that>}``;
      anything else fails, whether or not the upstream exists.
    - Keys are media-relative, as retention resolves them; ``provider_configs/<name>`` is the
      configuration stored beside the invocation, i.e. beside this sidecar.
    - A plain digest needs the upstream present with those bytes. The ONE exception is the set
      of upstreams no snapshot ever carries — decoded audio, analysis windows, the original
      media (:data:`_ALLOWED_MISSING`, matched exactly) — which may be absent but never wrong.
    - A pruned marker is satisfied by absence; a re-derived upstream must reproduce it.
    - Sidecars inside a manifest-sealed directory are proven by that manifest instead, as the
      frozen retention treats them (:data:`_SEALED_PREFIXES`).
    """

    parts = relative.split("/")
    if len(parts) < 3:
        return [f"sidecar is not inside a medium: {relative}"]
    if "/".join(parts[2:]).startswith(_SEALED_PREFIXES):
        return []
    media_dir = work_root / parts[0] / parts[1]
    sidecar = work_root / relative
    errors: list[str] = []
    try:
        payload = json.loads(read_text(sidecar))
    except (OSError, ValueError) as exc:
        return [f"unreadable sidecar {relative}: {exc}"]
    if payload.get("schema_version") != SIDECAR_SCHEMA_VERSION:
        errors.append(f"sidecar schema_version differs: {relative}")
    stem = sidecar.name.removesuffix(".done.json")
    native_parent = Path(native_path(sidecar.parent))
    candidates = [native_parent / stem] + [
        path
        for path in sorted(native_parent.glob(glob.escape(stem) + ".*"))
        if path.name != sidecar.name
    ]
    found = next((path for path in candidates if path.is_file()), None)
    if found is None:
        errors.append(f"sidecar has no artefact: {relative}")
    elif payload.get("sha256") != sha256_file(found):
        errors.append(f"sidecar artefact hash differs: {relative}")
    upstream = payload.get("upstream")
    if not isinstance(upstream, dict):
        errors.append(f"sidecar upstream is not an object: {relative}")
        return errors
    for logical, expected in sorted(upstream.items()):
        logical = str(logical)
        try:
            config = _PROVIDER_CONFIG.fullmatch(logical)
            if config is not None:
                path = sidecar.parent / safe_entry_path(config.group(1))
            else:
                path = media_dir / safe_entry_path(logical)
        except BackupRefused:
            errors.append(f"sidecar names an unsafe upstream: {relative} -> {logical}")
            continue
        if isinstance(expected, dict) and set(expected) == {"pruned_upstream"}:
            pruned = expected["pruned_upstream"]
            if not isinstance(pruned, str) or not _SHA256.fullmatch(pruned):
                errors.append(f"invalid pruned upstream: {relative} -> {logical}")
            elif path_is_file(path) and sha256_file(path) != pruned:
                errors.append(f"re-derived upstream differs: {relative} -> {logical}")
            continue
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            errors.append(f"invalid upstream digest: {relative} -> {logical}")
        elif not path_is_file(path):
            if _ALLOWED_MISSING.fullmatch(logical) is None:
                errors.append(f"sidecar upstream is missing: {relative} -> {logical}")
        elif expected != sha256_file(path):
            errors.append(f"sidecar upstream differs: {relative} -> {logical}")
    return errors


# ---------------------------------------------------------------------------------------------
# Restoring, and recovering an interrupted restore
# ---------------------------------------------------------------------------------------------
def _supervisor_lock(work_root: Path) -> Path:
    return _plain(work_root) / LOCAL_DATABASE_DIR / SUPERVISOR_LOCK_NAME


def _restore_lock(work_root: Path) -> Path:
    return _plain(work_root) / LOCAL_DATABASE_DIR / RESTORE_LOCK_NAME


def _stage(source: Path, destination: Path, guard: CorpusGuard) -> None:
    """Copy one file into the staging tree and flush it."""

    guard.check(destination)
    refuse_corpus_destination(destination)
    make_directories(destination.parent)
    shutil.copy2(native_path(source), native_path(destination))
    fsync_file(destination)


def _normalise_database(staged: Path, *, old_root: str | None, new_root: Path) -> int:
    """Re-root the absolute bundle paths the worker stores, in the STAGED database."""

    changed = 0
    connection = sqlite3.connect(staged)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT bundle_id, path FROM result_bundles").fetchall()
        for row in rows:
            current = Path(str(row["path"]))
            relative = _relative(Path(old_root), current) if old_root else None
            if relative is None:
                parts = current.parts
                relative = "/".join(parts[-5:]) if len(parts) >= 5 else None
            if relative is None:
                continue
            rerooted = str(_plain(new_root / relative))
            if rerooted != str(current):
                connection.execute(
                    "UPDATE result_bundles SET path=? WHERE bundle_id=?",
                    (rerooted, row["bundle_id"]),
                )
                changed += 1
        connection.commit()
    finally:
        connection.close()
    fsync_file(staged)
    return changed


def _media_directories(work_root: Path) -> list[Path]:
    """Every ``<source>/<media>`` directory really inside the work root."""

    found: list[Path] = []
    root = Path(native_path(work_root))
    if not _is_dir(root):
        return found
    for source_dir in sorted(root.iterdir()):
        if not _is_dir(source_dir) or is_link(source_dir) or source_dir.name.startswith("."):
            continue
        for media_dir in sorted(source_dir.iterdir()):
            if _is_dir(media_dir) and not is_link(media_dir):
                found.append(media_dir)
    return found


def _single_name(value: object, what: str) -> str:
    name = safe_entry_path(str(value))
    if "/" in name:
        raise BackupRefused(f"snapshot names an unsafe {what}: {name}")
    return name


def _bundles_of_runs(media_dir: Path, runs: set[str]) -> set[str]:
    """Bundle directories under this medium whose own manifest says one of ``runs`` owns them."""

    owned: set[str] = set()
    bundles = media_dir / "present" / "bundles"
    if not runs or not _is_dir(bundles):
        return owned
    for directory in sorted(Path(native_path(bundles)).iterdir()):
        manifest = directory / "manifest.json"
        if is_link(directory) or not manifest.is_file():
            continue
        try:
            run_id = json.loads(read_text(manifest)).get("run_id")
        except (OSError, ValueError, AttributeError):
            continue
        if run_id in runs:
            owned.add(directory.name)
    return owned


def _preserved_media(
    work_root: Path, document: dict[str, Any], entries: Sequence[Entry]
) -> dict[str, str]:
    """What the snapshot knows exists but did not capture; a restore leaves it exactly as is.

    A ``skipped`` record says "this is real, and this snapshot does not hold it"; removing it would
    destroy owner data the backup never had. Keys are work-root-relative prefixes:

    - a whole medium (``<source>/<media>``) when the snapshot holds nothing under it;
    - otherwise, inside a captured medium, each skipped run's own subtree —
      ``fuse/runs/<run_id>`` and every bundle that run owns (named by the record, or by the
      bundle's own manifest) — unless the snapshot holds something under that subtree.

    A medium the snapshot never mentions is post-backup state and is removed as usual.
    """

    listed = [entry.path for entry in entries]
    captured = {"/".join(path.split("/")[:2]) for path in listed}

    def holds(prefix: str) -> bool:
        return any(path == prefix or path.startswith(prefix + "/") for path in listed)

    reasons: dict[str, list[str]] = {}

    def keep(prefix: str, note: str) -> None:
        found = reasons.setdefault(prefix, [])
        if note not in found:
            found.append(note)

    on_disk: list[Path] | None = None
    for item in document.get("skipped", []):
        media = safe_entry_path(str(item.get("media", "")))
        reason = str(item.get("reason", "")).strip() or "no reason recorded"
        runs = {_single_name(run, "run") for run in item.get("runs", [])}
        bundles = {_single_name(bundle, "bundle") for bundle in item.get("bundles", [])}
        parts = media.split("/")
        if len(parts) == 2:
            candidates = [media]
        elif len(parts) == 1:
            # Recorded by media key alone (no directory existed at backup time): any directory
            # with that key that exists now is the same medium.
            if on_disk is None:
                on_disk = _media_directories(work_root)
            candidates = [
                str(_relative(work_root, path)) for path in on_disk if path.name == parts[0]
            ]
        else:
            raise BackupRefused(f"snapshot skips something that is not a medium: {media}")
        for relative in candidates:
            if relative not in captured:
                keep(relative, reason)
                continue
            owned = bundles | _bundles_of_runs(work_root / relative, runs)
            subtrees = [(f"{relative}/fuse/runs/{run}", run) for run in sorted(runs)]
            subtrees += [(f"{relative}/present/bundles/{bundle}", None) for bundle in sorted(owned)]
            for prefix, run in subtrees:
                if not holds(prefix):
                    keep(prefix, f"run {run}: {reason}" if run else reason)
    return {
        prefix: f"preserved: not in this snapshot ({'; '.join(found)})"
        for prefix, found in sorted(reasons.items())
    }


def _under_any(relative: str | None, preserved: dict[str, str]) -> bool:
    if relative is None:
        return False
    return any(relative == prefix or relative.startswith(prefix + "/") for prefix in preserved)


def _stale_artefacts(work_root: Path, listed: set[str]) -> list[Path]:
    """Every restore-owned file in the whole locked work root that the snapshot does not list."""

    stale: list[Path] = []

    def consider(path: Path) -> None:
        relative = _relative(work_root, path)
        if relative is not None and relative not in listed:
            stale.append(path)

    for media_dir in _media_directories(work_root):
        for subtree in _OWNED_TREES:
            directory = media_dir.joinpath(*subtree.split("/"))
            if _is_dir(directory):
                for path in _files(directory):
                    consider(path)
        for flat in _OWNED_FLAT:
            directory = media_dir / flat
            if _is_dir(directory):
                for child in sorted(Path(native_path(directory)).iterdir()):
                    if child.is_file() and not is_link(child):
                        consider(child)
        for owned in _OWNED_FILES:
            path = media_dir.joinpath(*owned.split("/"))
            if path_is_file(path) and not is_link(path):
                consider(path)
    return stale


def _recover_one(work_root: Path, staging: Path, guard: CorpusGuard) -> dict[str, Any] | None:
    """Undo one interrupted publication. Every target is confined BEFORE anything is created."""

    journal = staging / JOURNAL_NAME
    try:
        document = json.loads(read_text(journal)) if path_is_file(journal) else {}
    except (OSError, ValueError):
        document = {}
    if document.get("state") != "publishing":
        return None

    # (1) Prove every path first. A tampered journal must not make recovery write anywhere.
    fresh: list[Path] = []
    for item in document.get("targets", []):
        target = contained(work_root, str(item["path"]))
        if not bool(item["existed"]):
            fresh.append(target)
    database = contained(work_root, str(document["database"]))
    database_names = {database.name} | {database.name + suffix for suffix in _SQLITE_SIDECARS}
    keep = staging / REPLACED_DIR
    restorations: list[tuple[Path, Path]] = []
    if _is_dir(keep):
        for kept in _files(keep):
            relative = _relative(keep, kept)
            if relative is None:
                continue
            relative = safe_entry_path(relative)
            if relative.startswith(f"{_REPLACED_DATABASE}/"):
                name = relative.split("/", 1)[1]
                if name not in database_names:
                    raise BackupRefused(f"unexpected file in a restore's recovery tree: {relative}")
                original = database.parent / name
            else:
                original = contained(work_root, relative)
            restorations.append((kept, original))
    for path in fresh + [database] + [original for _kept, original in restorations]:
        guard.check(path)

    # (2) Remove what this restore published where nothing stood before; each deletion is flushed.
    for target in fresh:
        if path_is_file(target):
            _remove(target, guard)
    if not bool(document.get("database_existed", True)) and path_is_file(database):
        _remove(database, guard)
    # (3) Put every displaced original back.
    for kept, original in restorations:
        make_directories(original.parent)
        _rename(kept, original, guard)
    # (4) Durably mark the journal done BEFORE its tree is deleted: if a power cut resurrected the
    # tree, a journal still saying "publishing" would delete files written after this recovery.
    document["state"] = "rolled-back"
    _write_json(journal, document, guard)
    return document


def recover_interrupted(work_root: Path) -> list[dict[str, Any]]:
    """Roll back every interrupted restore under ``work_root``; the caller holds the lock."""

    work_root = refuse_link_root(work_root, "work root")
    base = work_root / LOCAL_DATABASE_DIR
    if not _is_dir(base):
        return []
    guard = CorpusGuard()
    recovered: list[dict[str, Any]] = []
    for staging in sorted(Path(native_path(base)).glob(f"{STAGING_DIR}-*")):
        if not _is_dir(staging) or is_link(staging):
            continue
        document = _recover_one(work_root, staging, guard)
        if document is not None:
            recovered.append(document)
        shutil.rmtree(native_path(staging), ignore_errors=True)
        flush_directory(base)
    return recovered


class RestoreInProgress(BackupRefused):
    """A live restore owns this work root; nothing may open or serve it until it finishes."""


def _has_journal(work_root: Path) -> bool:
    base = Path(work_root) / LOCAL_DATABASE_DIR
    return _is_dir(base) and any(Path(native_path(base)).glob(f"{STAGING_DIR}-*"))


def restore_running(work_root: Path) -> bool:
    """Whether a restore holds this work root right now (one non-blocking probe of its lock)."""

    probe = ProcessLock(_restore_lock(Path(work_root)))
    try:
        probe.acquire()
    except JobStoreLocked:
        return True
    probe.release()
    return False


@contextlib.contextmanager
def restore_excluded(work_root: Path) -> Iterator[list[dict[str, Any]]]:
    """Hold restore exclusion for the whole ``with`` block: refuse, recover, then keep it held.

    - A live restore (its lock is held) → :class:`RestoreInProgress`. Nothing is opened.
    - A recovery tree, and the supervisor lock can be taken here → the interrupted restore is
      undone (the list of recovered journals is what the block receives).
    - A recovery tree, but the supervisor lock is busy → refused: whoever holds it cannot be proven
      not to be publishing, so this fails closed rather than build on a half-published tree.

    The restore lock stays held until the block ends — callers construct and migrate the database
    inside it — so a restore that starts meanwhile is refused and can never overlap that open.
    """

    root = Path(work_root)
    gate = ProcessLock(_restore_lock(root))
    try:
        gate.acquire()
    except JobStoreLocked:
        raise RestoreInProgress(RESTORE_IN_PROGRESS) from None
    try:
        recovered: list[dict[str, Any]] = []
        if _has_journal(root):
            lock = ProcessLock(_supervisor_lock(root))
            try:
                lock.acquire()
            except JobStoreLocked:
                raise RestoreInProgress(RESTORE_IN_PROGRESS) from None
            try:
                recovered = recover_interrupted(root)
            finally:
                lock.release()
        yield recovered
    finally:
        gate.release()


def recover_before_use(work_root: Path) -> list[dict[str, Any]]:
    """:func:`restore_excluded` for a caller that opens nothing afterwards."""

    with restore_excluded(work_root) as recovered:
        return recovered


def restore(
    source: Path,
    work_root: Path,
    *,
    database_path: Path | None = None,
    verify_snapshot_first: bool = True,
) -> dict[str, Any]:
    """Restore a snapshot as one recoverable boundary: stage, prove, publish (§4.6)."""

    guard = CorpusGuard()
    source = Path(source)
    work_root = refuse_link_root(work_root, "restore work root")
    target_database = (
        _plain(Path(database_path))
        if database_path is not None
        else work_root / LOCAL_DATABASE_DIR / LOCAL_DATABASE_NAME
    )
    database_relative = _relative(work_root, target_database)
    if database_relative is None:
        raise BackupRefused(
            "the restored database must live inside the work root whose supervisor lock is held: "
            f"{target_database} is outside {work_root}"
        )
    guard.check(work_root)
    guard.check(target_database)
    refuse_corpus_destination(target_database)

    lock_path = _supervisor_lock(work_root)
    make_directories(lock_path.parent)
    # Ownership is explicit: the restore lock FIRST, then the supervisor lock, both held to the
    # end. A server that finds the supervisor lock busy probes the restore lock and refuses; a
    # server that took the supervisor lock first makes this restore refuse instead.
    gate = ProcessLock(_restore_lock(work_root))
    try:
        gate.acquire()
    except JobStoreLocked:
        raise BackupRefused(
            "another restore, or an ID'er that is starting up, holds this work folder; "
            "try again once it has finished"
        ) from None
    lock = ProcessLock(lock_path)
    try:
        lock.acquire()
    except JobStoreLocked:
        gate.release()
        raise BackupRefused(
            "ID'er is still running on this work folder: stop it before restoring a snapshot"
        ) from None

    recovered: list[dict[str, Any]] = []
    staging: Path | None = None
    replaced: list[tuple[Path, Path]] = []
    published: list[Path] = []
    incomplete = False
    try:
        recovered = recover_interrupted(work_root)
        target_database = contained(work_root, database_relative)
        staging = work_root / LOCAL_DATABASE_DIR / f"{STAGING_DIR}-{uuid.uuid4().hex[:8]}"
        keep = staging / REPLACED_DIR
        journal = staging / JOURNAL_NAME
        guard.check(staging)
        Path(native_path(staging)).mkdir(parents=True, exist_ok=False)
        flush_directory(staging.parent)

        document = read_snapshot(source)
        if verify_snapshot_first:
            errors = verify_snapshot(source)
            if errors:
                raise BackupRefused("snapshot does not verify: " + "; ".join(errors[:5]))
        entries = entries_of(document)
        targets = [(entry, contained(work_root, entry.path)) for entry in entries]
        for _entry, target in targets:
            guard.check(target)

        # (1) Stage every byte and flush it, touching nothing that is already in place.
        staged_database = staging / DATABASE_NAME
        _stage(source / DATABASE_NAME, staged_database, guard)
        for entry, _target in targets:
            _stage(contained(source / ARTEFACTS_DIR, entry.path), staging / entry.path, guard)
        rerooted = _normalise_database(
            staged_database, old_root=document.get("work_root"), new_root=work_root
        )

        # (2) Prove the staged tree BEFORE anything is published.
        problems = verify_artefacts(staging, document)
        if problems:
            raise BackupRefused("restored artefacts do not verify: " + "; ".join(problems[:5]))

        # (3) The journal names every target and whether something stood there, so recovery can
        # tell a publication it must remove from an original it must leave alone.
        listed = {entry.path for entry in entries}
        preserved = _preserved_media(work_root, document, entries)
        stale = [
            path
            for path in _stale_artefacts(work_root, listed)
            if not _under_any(_relative(work_root, path), preserved)
        ]
        for path in stale:
            guard.check(path)
        removed_paths = sorted(str(_relative(work_root, path)) for path in stale)
        _write_json(
            journal,
            {
                "work_root": str(work_root),
                "database": database_relative,
                "database_existed": path_is_file(target_database),
                "targets": [
                    {"path": entry.path, "existed": path_is_file(target)}
                    for entry, target in targets
                ],
                "preserved": preserved,
                "removed": removed_paths,
                "recovered": recovered,
                "state": "publishing",
                "at": time.time(),
            },
            guard,
        )

        # (4) Publish. Every rename is a durable replace.
        make_directories(target_database.parent)
        for stale_path in stale:
            kept = keep / (_relative(work_root, stale_path) or stale_path.name)
            make_directories(kept.parent)
            _rename(stale_path, kept, guard)
            replaced.append((stale_path, kept))
        for suffix in _SQLITE_SIDECARS:
            sidecar = Path(str(target_database) + suffix)
            if _exists(sidecar):
                kept = keep / _REPLACED_DATABASE / (target_database.name + suffix)
                make_directories(kept.parent)
                _rename(sidecar, kept, guard)
                replaced.append((sidecar, kept))
        for entry, target in targets:
            if path_is_file(target):
                kept = keep / entry.path
                make_directories(kept.parent)
                _rename(target, kept, guard)
                replaced.append((target, kept))
            make_directories(target.parent)
            _rename(staging / entry.path, target, guard)
            published.append(target)
        if path_is_file(target_database):
            kept = keep / _REPLACED_DATABASE / target_database.name
            make_directories(kept.parent)
            _rename(target_database, kept, guard)
            replaced.append((target_database, kept))
        _rename(staged_database, target_database, guard)
        published.append(target_database)
        _write_json(journal, {"state": "committed", "at": time.time()}, guard)
    except BaseException:
        for path in reversed(published):
            try:
                if path_is_file(path):
                    _remove(path, guard)
            except OSError:
                incomplete = True
        for original, kept in reversed(replaced):
            try:
                make_directories(original.parent)
                _rename(kept, original, guard)
            except OSError:
                incomplete = True
        if incomplete:
            raise BackupRefused(
                f"restore failed and its rollback could not finish; the originals that are still "
                f"displaced are kept in {staging} — the next start of ID'er recovers them"
            ) from None
        raise
    finally:
        if staging is not None and not incomplete:
            shutil.rmtree(native_path(staging), ignore_errors=True)
            with contextlib.suppress(OSError):
                flush_directory(staging.parent)
        lock.release()
        gate.release()
    return {
        "database": str(target_database),
        "work_root": str(work_root),
        "artefacts": len(targets),
        "removed": len(stale),
        "removed_paths": removed_paths,
        "preserved": preserved,
        "rerooted": rerooted,
        "recovered": len(recovered),
        "skipped": document.get("skipped", []),
        "created_at": document["created_at"],
    }


def main(argv: list[str] | None = None) -> int:
    """``python -m idea_web.backup backup|verify|restore ...`` (6a-iv owns the nightly schedule)."""

    import argparse

    parser = argparse.ArgumentParser(prog="idea_web.backup", description="IDea snapshots (§4.6)")
    actions = parser.add_subparsers(dest="action", required=True)
    take = actions.add_parser("backup")
    take.add_argument("--work-root", type=Path, required=True)
    take.add_argument("--database", type=Path, default=None)
    take.add_argument("--into", type=Path, required=True)
    take.add_argument("--hint-cache", type=Path, default=None)
    check = actions.add_parser("verify")
    check.add_argument("--snapshot", type=Path, required=True)
    put = actions.add_parser("restore")
    put.add_argument("--snapshot", type=Path, required=True)
    put.add_argument("--work-root", type=Path, required=True)
    put.add_argument("--database", type=Path, default=None)
    args = parser.parse_args(argv)
    status = 0
    if args.action == "backup":
        path = (
            args.database
            if args.database is not None
            else Path(args.work_root) / LOCAL_DATABASE_DIR / LOCAL_DATABASE_NAME
        )
        payload: Any = backup(Database(path), args.work_root, args.into, hint_cache=args.hint_cache)
    elif args.action == "verify":
        problems = verify(args.snapshot)
        payload = {"ok": not problems, "errors": problems}
        status = 1 if problems else 0
    else:
        payload = restore(args.snapshot, args.work_root, database_path=args.database)
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return status


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
