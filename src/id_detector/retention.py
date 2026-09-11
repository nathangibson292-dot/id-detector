"""Explicit, recoverable retention for local artefact trees."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from id_detector.io import (
    atomic_write_json,
    native_path,
    path_is_file,
    read_text,
    sha256_file,
)
from id_detector.jobs import JobStoreLocked, ProcessLock

Policy = Literal["local", "hosted"]
SUCCESS_STATUSES = frozenset({"complete", "degraded", "partial"})
FAILED_STATUSES = frozenset(
    {
        "failed",
        "cancelled",
        "provider_unavailable",
        "budget_exhausted",
        "source_changed",
        "quota_exceeded",
        "dead_letter",
    }
)
TERMINAL_STATUSES = SUCCESS_STATUSES | FAILED_STATUSES
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: A dated trash directory collects moves made during that UTC day, so its youngest possible entry
#: is seven full days old only once the day *after* it ended is itself seven days behind us.
_TRASH_GRACE = timedelta(days=8)


@dataclass(frozen=True)
class GCAction:
    operation: Literal["move", "purge", "skip"]
    path: Path
    reason: str


@dataclass(frozen=True)
class GCResult:
    actions: tuple[GCAction, ...]
    applied: bool


@dataclass(frozen=True)
class RetentionPlan:
    """When each shared artefact of one media directory may be collected.

    Every artefact under ``work/<source>/<media>/`` is shared by *all* runs of that media, so a
    rule read from a single run can delete what another run still owns (plan §4.6: a local
    ``complete`` keeps its original forever, even if a later refresh fails). Each field is
    therefore the *latest* time any terminal run allows, and ``None`` means "never".
    """

    status: str
    keep_intermediates: bool
    pcm_due: datetime | None
    original_due: datetime | None
    media_due: datetime | None


_EPOCH = datetime.min.replace(tzinfo=UTC)


def _later(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if current is None or candidate is None:
        return None
    return max(current, candidate)


def _due(moment: datetime | None, now: datetime) -> bool:
    return moment is not None and now >= moment


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _journal_entries(media_dir: Path) -> list[dict[str, object]] | None:
    """Every journal entry oldest-first; ``None`` when the journal cannot be read at all."""

    try:
        lines = read_text(media_dir / "invocations.jsonl").splitlines()
    except OSError:
        return None
    entries: list[dict[str, object]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _retention_plan(media_dir: Path, *, policy: Policy, now: datetime) -> RetentionPlan | None:
    """Fold *every* terminal run of one media into one conservative plan, or skip the media.

    ``None`` means "collect nothing here": an unreadable journal, a run still in flight, or a
    terminal run whose age cannot be established are all ambiguity, and ambiguity never licenses
    a delete.
    """

    entries = _journal_entries(media_dir)
    if not entries:
        return None
    if entries[-1].get("status") not in TERMINAL_STATUSES:
        return None
    terminal = [entry for entry in entries if entry.get("status") in TERMINAL_STATUSES]
    pcm_due = original_due = media_due = _EPOCH
    for entry in terminal:
        finished = _timestamp(entry.get("finished_at") or entry.get("started_at"))
        if finished is None:
            return None
        if entry["status"] in FAILED_STATUSES:
            pcm_due = _later(pcm_due, finished)
            original_due = _later(original_due, finished + timedelta(hours=24))
            media_due = _later(media_due, finished + timedelta(days=7))
            continue
        pcm_due = _later(pcm_due, finished + timedelta(hours=48))
        # A successful run keeps its original forever when the policy is local (§4.6), and its
        # media directory is never collected wholesale under either policy.
        original_due = _later(
            original_due, finished + timedelta(days=7) if policy == "hosted" else None
        )
        media_due = None
    return RetentionPlan(
        status=str(terminal[-1]["status"]),
        keep_intermediates=any(entry.get("keep_intermediates") is True for entry in terminal),
        pcm_due=pcm_due,
        original_due=original_due,
        media_due=media_due,
    )


def _real(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def _media_directories(work_root: Path) -> list[Path]:
    """``work/<source>/<media>/`` directories that really live inside *work_root*.

    ``is_dir()`` follows symlinks and Windows junctions, so a reparse point planted in the work
    tree would otherwise let GC move — and eventually purge — files outside it. Every level must
    resolve to exactly the path we walked to reach it.
    """

    result: list[Path] = []
    if not work_root.is_dir():
        return result
    for source_dir in work_root.iterdir():
        if not source_dir.is_dir() or source_dir.name.startswith("."):
            continue
        if _real(source_dir) != work_root / source_dir.name:
            continue
        for media_dir in source_dir.iterdir():
            if not media_dir.is_dir() or not _SHA256.fullmatch(media_dir.name):
                continue
            if _real(media_dir) != source_dir / media_dir.name:
                continue
            result.append(media_dir)
    return sorted(result)


def _within(path: Path, root: Path) -> bool:
    """True only when *path* really lives inside *root*, links and junctions resolved."""

    real = _real(path)
    return real is not None and (real == root or real.is_relative_to(root))


def _original_path(media_dir: Path) -> Path | None:
    try:
        source = json.loads(read_text(media_dir / "ingest/source.json"))
        relative = source["original"]["path"]
        if not isinstance(relative, str):
            return None
        candidate = (media_dir / relative).resolve()
        candidate.relative_to(media_dir.resolve())
        return candidate
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        candidates = sorted((media_dir / "ingest").glob("original*"))
        return candidates[0] if candidates else None


def _is_removed(logical_path: str, targets: list[Path], media_dir: Path) -> bool:
    try:
        logical = (media_dir / logical_path).resolve()
        logical.relative_to(media_dir.resolve())
    except ValueError:
        return False
    for target in targets:
        target = target.resolve()
        if logical == target:
            return True
        try:
            logical.relative_to(target)
            return True
        except ValueError:
            pass
    return False


def _rewrite_sidecars(media_dir: Path, targets: list[Path]) -> None:
    """Seal downstream evidence with the hash it had before an upstream is moved."""

    for sidecar in sorted(media_dir.rglob("*.done.json")):
        relative_parts = sidecar.relative_to(media_dir).parts
        if relative_parts[:2] in {("fuse", "runs"), ("present", "bundles")}:
            # These are immutable copies sealed by their directory manifest. Their recorded
            # hashes remain evidence; changing a copied sidecar would damage the published run.
            continue
        try:
            payload = json.loads(read_text(sidecar))
        except (OSError, json.JSONDecodeError):
            continue
        base = sidecar.name.removesuffix(".done.json")
        candidates = [
            path for path in sidecar.parent.glob(f"{base}.*") if path != sidecar and path.is_file()
        ]
        if len(candidates) != 1:
            continue
        artifact = candidates[0]
        if not path_is_file(artifact) or payload.get("sha256") != sha256_file(artifact):
            continue
        upstream = payload.get("upstream")
        if not isinstance(upstream, dict):
            continue
        changed = False
        for logical_path, recorded_hash in list(upstream.items()):
            if isinstance(recorded_hash, str) and _is_removed(logical_path, targets, media_dir):
                upstream[logical_path] = {"pruned_upstream": recorded_hash}
                changed = True
        if changed:
            atomic_write_json(sidecar, payload)


def _trash_root(work_root: Path) -> Path | None:
    """``work/.trash`` when it is a real directory of this work root, else ``None``.

    GC both writes into and recursively purges this tree, so a symlink or junction standing in
    for it (or for one of its dated directories) is refused rather than followed.
    """

    trash = work_root / ".trash"
    if not trash.exists():
        return trash
    if not trash.is_dir() or _real(trash) != trash:
        return None
    return trash


def _trash_destination(trash_root: Path, target: Path, media_dir: Path, today: date) -> Path:
    relative = target.relative_to(media_dir)
    base = trash_root / today.isoformat() / media_dir.parent.name / media_dir.name
    destination = base / relative
    counter = 1
    while destination.exists():
        destination = base / f"{relative.as_posix()}.{counter}"
        counter += 1
    return destination


def _move_to_trash(trash_root: Path, target: Path, media_dir: Path, today: date) -> None:
    destination = _trash_destination(trash_root, target, media_dir, today)
    os.makedirs(native_path(destination.parent), exist_ok=True)
    if not _within(destination.parent, trash_root):
        raise OSError(f"refusing to write outside the trash root: {destination}")
    os.replace(native_path(target), native_path(destination))


def _index_references(work_root: Path, media_dir: Path) -> bool:
    try:
        payload = json.loads(read_text(work_root / "index.json"))
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError):
        # An index we cannot read is not proof that nothing points here.
        return True
    media = payload.get("media") if isinstance(payload, dict) else None
    if not isinstance(media, dict):
        return True
    relative = media_dir.relative_to(work_root).as_posix()
    for key, entry in media.items():
        if key == media_dir.name:
            return True
        if isinstance(entry, dict) and entry.get("media_dir") == relative:
            return True
        if isinstance(entry, dict) and any(
            isinstance(alias, dict) and alias.get("media_dir") == relative
            for alias in entry.get("aliases", [])
        ):
            return True
    return False


def _has_publication_reference(work_root: Path, media_dir: Path) -> bool:
    """Whether anything may still serve this media. Ambiguity counts as a reference.

    A bundle whose manifest is damaged, unreadable or merely locked is still a publication: the
    plan deletes a run only when nothing references it, and "I could not tell" is not nothing.
    """

    try:
        # Any entry at all counts: ``is_dir()`` on a >MAX_PATH bundle answers False on Windows
        # rather than raising, and that must not read as "no bundle here".
        if any(True for _ in (media_dir / "present/bundles").iterdir()):
            return True
    except FileNotFoundError:
        pass
    except OSError:
        return True
    try:
        if read_text(media_dir / "present/current").strip():
            return True
    except FileNotFoundError:
        pass
    except OSError:
        return True
    return _index_references(work_root, media_dir)


def _write_manifest(media_dir: Path, *, policy: Policy, now: datetime) -> None:
    path = media_dir / "retention-manifest.json"
    files = {
        item.relative_to(media_dir).as_posix(): {"size": item.stat().st_size}
        for item in sorted(media_dir.rglob("*"))
        if item.is_file() and item != path
    }
    atomic_write_json(
        path,
        {
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "policy": policy,
            "total_size": sum(item["size"] for item in files.values()),
            "files": files,
        },
    )


def _purge_actions(trash_root: Path | None, now: datetime) -> list[GCAction]:
    if trash_root is None or not trash_root.is_dir():
        return []
    actions = []
    for path in sorted(trash_root.iterdir()):
        try:
            stamp = date.fromisoformat(path.name)
        except ValueError:
            continue
        if not path.is_dir() or _real(path) != trash_root / path.name:
            continue
        expires = datetime(stamp.year, stamp.month, stamp.day, tzinfo=UTC) + _TRASH_GRACE
        if now >= expires:
            actions.append(GCAction("purge", path, "trash is more than 7 days old"))
    return actions


def _targets(
    media_dir: Path,
    plan: RetentionPlan,
    *,
    work_root: Path,
    now: datetime,
) -> tuple[list[tuple[Path, str]], list[GCAction]]:
    """The artefacts of one media that may be collected now, plus anything refused."""

    refused: list[GCAction] = []
    candidates: list[tuple[Path, str]] = []
    windows = media_dir / "windows"
    pcm = media_dir / "decode/audio.pcm"
    original = _original_path(media_dir)
    if not plan.keep_intermediates:
        if windows.exists():
            candidates.append((windows, f"{plan.status}: windows expire immediately"))
        if path_is_file(pcm) and _due(plan.pcm_due, now):
            candidates.append((pcm, f"{plan.status}: PCM retention expired"))
    if original is not None and path_is_file(original) and _due(plan.original_due, now):
        candidates.append((original, f"{plan.status}: original retention expired"))
    if _due(plan.media_due, now) and not _has_publication_reference(work_root, media_dir):
        candidates = [(media_dir, f"{plan.status}: unreferenced media retention expired")]
    targets = []
    for target, reason in candidates:
        if target != media_dir and not _within(target, media_dir):
            refused.append(GCAction("skip", target, "artefact resolves outside its media dir"))
            continue
        targets.append((target, reason))
    return targets, refused


def collect(
    work_root: Path,
    *,
    policy: Policy,
    apply: bool = False,
    now: datetime | None = None,
) -> GCResult:
    """Plan or apply one explicit retention pass; dry-run is the default."""

    if policy not in {"local", "hosted"}:
        raise ValueError("policy must be local or hosted")
    work_root = work_root.resolve()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    trash_root = _trash_root(work_root)
    actions = _purge_actions(trash_root, now)
    if trash_root is None:
        actions.append(
            GCAction("skip", work_root / ".trash", "trash is not a directory of this work root")
        )
    for media_dir in _media_directories(work_root):
        if not apply:
            plan = _retention_plan(media_dir, policy=policy, now=now)
            if plan is None:
                continue
            targets, refused = _targets(media_dir, plan, work_root=work_root, now=now)
            actions.extend(refused)
            actions.extend(GCAction("move", target, reason) for target, reason in targets)
            continue
        if trash_root is None:
            continue
        # Everything this pass acts on is read *under* the media lock the pipeline holds while it
        # ingests, journals and publishes. Planning outside it would let a run that finished
        # between the scan and the move be collected on a stale plan.
        lock = ProcessLock(media_dir / ".media.lock")
        try:
            lock.acquire()
        except JobStoreLocked:
            actions.append(GCAction("skip", media_dir, "media is active"))
            continue
        try:
            plan = _retention_plan(media_dir, policy=policy, now=now)
            if plan is None:
                continue
            targets, refused = _targets(media_dir, plan, work_root=work_root, now=now)
            actions.extend(refused)
            if not targets:
                continue
            paths = [target for target, _ in targets]
            if media_dir not in paths:
                _rewrite_sidecars(media_dir, paths)
            for target, _ in targets:
                if target.exists():
                    _move_to_trash(trash_root, target, media_dir, now.date())
            actions.extend(GCAction("move", target, reason) for target, reason in targets)
            if media_dir.exists():
                _write_manifest(media_dir, policy=policy, now=now)
        finally:
            lock.release()
    if apply:
        for action in actions:
            if action.operation != "purge":
                continue
            if action.path.is_dir() and not action.path.is_symlink():
                shutil.rmtree(action.path)
    return GCResult(tuple(actions), apply)
