"""Explicit, recoverable retention for local artefact trees."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import stat
from collections import defaultdict
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
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_UNLISTABLE = "media tree cannot be fully listed; nothing here is collected"


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


class _Refused(OSError):
    """GC declined to mutate a path; the pass records a skip instead of acting."""


_EPOCH = datetime.min.replace(tzinfo=UTC)


# --- Long-path-safe, link-refusing filesystem primitives ---------------------------------------
#
# A real work tree nests two 64-hex directories, so bundle and recognise artefacts routinely pass
# MAX_PATH. Without LongPathsEnabled, plain ``pathlib``/``shutil`` calls on such paths answer
# "absent" (``exists``/``is_dir``/``is_file``), stop descending silently (``rglob``) or raise
# (``stat``/``iterdir``/``rmtree``). Every filesystem question GC asks goes through these instead;
# the ``Path`` values GC reports and compares stay in their ordinary spelling.
#
# Every *mutation* — a rename's source and destination, a directory it creates, a sidecar or
# manifest it rewrites, a tree it purges — is additionally proven to be reached without passing
# through a symlink, junction or other reparse point, and is handed to the OS in the unresolved
# extended form, so the OS acts on the entry GC checked rather than on whatever a link names.


def _extended(path: Path) -> str:
    """Win32's extended form of *path* **without** resolving links.

    ``io.native_path`` resolves first, which is right for reading a file but wrong wherever the
    link itself is the subject: resolving a path (the input must be the unresolved spelling),
    asking whether it *is* a link, or mutating it (a rename, ``makedirs`` or ``rmtree`` must never
    be handed a link's target).
    """

    absolute = os.path.abspath(path)
    if os.name != "nt" or absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _resolved(path: Path) -> Path:
    """``Path.resolve()`` computed from the unresolved extended form, prefix stripped.

    (On CPython 3.12 plain ``Path.resolve()`` was measured to resolve junctions past MAX_PATH as
    well; this spelling keeps every GC path question in one form and never compares a prefixed
    path with an unprefixed one.)
    """

    real = os.path.realpath(_extended(path))
    if os.name == "nt" and real.startswith("\\\\?\\UNC\\"):
        real = "\\\\" + real[8:]
    elif os.name == "nt" and real.startswith("\\\\?\\"):
        real = real[4:]
    return Path(real)


def _exists(path: Path) -> bool:
    """Whether *path* names something, links followed. For read-side questions only."""

    return os.path.exists(native_path(path))


def _lexists(path: Path) -> bool:
    """Whether an entry exists at *path* itself — a dangling link counts."""

    return os.path.lexists(_extended(path))


def _is_dir(path: Path) -> bool:
    return os.path.isdir(native_path(path))


def _stat_is_link(status: os.stat_result) -> bool:
    """A symlink, a junction, or any other reparse point.

    Only name-surrogate reparse points redirect a path, but GC cannot tell a benign one from a
    redirect it does not know, so every reparse point is refused: failing closed only means an
    artefact is kept.
    """

    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_link(path: Path) -> bool:
    """Whether the entry at *path* is itself a link; raises ``FileNotFoundError`` when absent."""

    return _stat_is_link(os.lstat(_extended(path)))


def _is_real_directory(path: Path) -> bool:
    try:
        status = os.lstat(_extended(path))
    except OSError:
        return False
    return stat.S_ISDIR(status.st_mode) and not _stat_is_link(status)


def _is_real_file(path: Path) -> bool:
    try:
        status = os.lstat(_extended(path))
    except OSError:
        return False
    return stat.S_ISREG(status.st_mode) and not _stat_is_link(status)


def _unlinked(path: Path, root: Path) -> bool:
    """*path* lies lexically inside *root*, and no existing step from *root* down to it — *root*
    and *path* included — is a link or reparse point.

    Steps that do not exist yet are fine: nothing redirects through an entry that is not there,
    and GC creates missing directories itself in the unresolved form. Anything that cannot be
    inspected fails closed.
    """

    if path != root and not path.is_relative_to(root):
        return False
    parts = path.relative_to(root).parts
    if any(part in {"", ".", ".."} for part in parts):
        return False
    current = root
    for index in range(len(parts) + 1):
        if index:
            current = current / parts[index - 1]
        try:
            if _is_link(current):
                return False
        except FileNotFoundError:
            return True
        except OSError:
            return False
    return True


def _children(directory: Path) -> list[Path]:
    return [directory / name for name in os.listdir(native_path(directory))]


def _files(root: Path) -> list[Path]:
    """Every regular file under *root*, sorted, walked in extended form so nothing past MAX_PATH
    is skipped.

    Links and reparse points — files or directories — are neither listed nor followed: whatever
    they name is not this media's to inventory or rewrite. A directory that cannot be listed
    raises rather than being skipped, because a partial listing silently omits evidence.
    """

    found: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(_extended(directory)) as entries:
            for entry in entries:
                child = directory / entry.name
                status = entry.stat(follow_symlinks=False)
                if _stat_is_link(status):
                    continue
                if stat.S_ISDIR(status.st_mode):
                    pending.append(child)
                elif stat.S_ISREG(status.st_mode):
                    found.append(child)
    return sorted(found)


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
        return _resolved(path)
    except OSError:
        return None


def _media_directories(work_root: Path) -> list[Path]:
    """``work/<source>/<media>/`` directories that really live inside *work_root*.

    ``is_dir()`` follows symlinks and Windows junctions, so a reparse point planted in the work
    tree would otherwise let GC move — and eventually purge — files outside it. Every level must
    resolve to exactly the path we walked to reach it.
    """

    result: list[Path] = []
    if not _is_dir(work_root):
        return result
    for source_dir in _children(work_root):
        if not _is_dir(source_dir) or source_dir.name.startswith("."):
            continue
        if _real(source_dir) != work_root / source_dir.name:
            continue
        for media_dir in _children(source_dir):
            if not _is_dir(media_dir) or not _SHA256.fullmatch(media_dir.name):
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
    """The original's own spelling inside *media_dir* — never the target of a link to it.

    Resolving here would make a linked original collect whatever it points at; the lexical path is
    kept so the link itself is what ``_targets`` inspects (and refuses).
    """

    try:
        source = json.loads(read_text(media_dir / "ingest/source.json"))
        relative = source["original"]["path"]
        if not isinstance(relative, str):
            return None
        candidate = Path(os.path.normpath(media_dir / relative))
        candidate.relative_to(media_dir)
        return candidate
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        try:
            children = _children(media_dir / "ingest")
        except OSError:
            return None
        candidates = sorted(path for path in children if fnmatch.fnmatch(path.name, "original*"))
        return candidates[0] if candidates else None


def _is_removed(logical_path: str, targets: list[Path], media_dir: Path) -> bool:
    try:
        logical = _resolved(media_dir / logical_path)
        logical.relative_to(_resolved(media_dir))
    except ValueError:
        return False
    for target in targets:
        target = _resolved(target)
        if logical == target:
            return True
        try:
            logical.relative_to(target)
            return True
        except ValueError:
            pass
    return False


def _rewrite_sidecars(
    work_root: Path, media_dir: Path, targets: list[Path], inventory: list[Path]
) -> None:
    """Seal downstream evidence with the hash it had before an upstream is moved.

    *inventory* is the media's complete, link-free file listing, so a sidecar that is a link (or
    sits under one) is never read as evidence and never written through.
    """

    by_directory: defaultdict[Path, list[Path]] = defaultdict(list)
    for item in inventory:
        by_directory[item.parent].append(item)
    for sidecar in inventory:
        if not sidecar.name.endswith(".done.json"):
            continue
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
            path
            for path in by_directory[sidecar.parent]
            if path != sidecar and fnmatch.fnmatch(path.name, f"{base}.*")
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
            # Re-proven at the moment of writing: the atomic writer resolves its destination.
            if not _is_real_file(sidecar) or not _unlinked(sidecar, work_root):
                raise _Refused(f"sidecar is no longer a plain file of this media: {sidecar}")
            atomic_write_json(sidecar, payload)


def _trash_root(work_root: Path) -> Path | None:
    """``work/.trash`` when it is a real directory of this work root, else ``None``.

    GC both writes into and recursively purges this tree, so a symlink or junction standing in
    for it (or for one of its dated directories) is refused rather than followed. A dangling link
    counts as present, and is refused too.
    """

    trash = work_root / ".trash"
    if not _lexists(trash):
        return trash
    if not _is_real_directory(trash) or _real(trash) != trash:
        return None
    return trash


def _trash_destination(trash_root: Path, target: Path, media_dir: Path, today: date) -> Path:
    relative = target.relative_to(media_dir)
    base = trash_root / today.isoformat() / media_dir.parent.name / media_dir.name
    destination = base / relative
    counter = 1
    # ``lexists``: a dangling link at the destination is an occupied name, never a free one.
    while _lexists(destination):
        destination = base / f"{relative.as_posix()}.{counter}"
        counter += 1
    return destination


def _checked_destination(
    work_root: Path, trash_root: Path, target: Path, media_dir: Path, today: date
) -> Path:
    """Where *target* would land, proven reachable without passing through any link."""

    if trash_root != work_root / ".trash":
        raise _Refused(f"trash is not a directory of this work root: {trash_root}")
    destination = _trash_destination(trash_root, target, media_dir, today)
    if not _unlinked(destination.parent, work_root):
        raise _Refused(f"trash destination passes through a link or reparse point: {destination}")
    return destination


def _move_to_trash(
    work_root: Path, trash_root: Path, target: Path, media_dir: Path, today: date
) -> None:
    if not _unlinked(target, work_root) or not _lexists(target):
        raise _Refused(f"artefact or one of its parents is a link or reparse point: {target}")
    # Containment is proven *before* anything is created, and creation uses the unresolved form,
    # so no directory can appear outside the trash through a link that was already there.
    destination = _checked_destination(work_root, trash_root, target, media_dir, today)
    os.makedirs(_extended(destination.parent), exist_ok=True)
    if (
        not _unlinked(destination.parent, work_root)
        or not _within(destination.parent, trash_root)
        or _lexists(destination)
        or not _unlinked(target, work_root)
    ):
        raise _Refused(f"trash destination changed while moving: {destination}")
    # Unresolved operands: the rename acts on these entries, not on anything a link names.
    os.replace(_extended(target), _extended(destination))


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
        # Any entry at all counts: whatever it is, it must not read as "no bundle here".
        if os.listdir(native_path(media_dir / "present/bundles")):
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


def _manifest_refusal(media_dir: Path, work_root: Path) -> str | None:
    """Why the manifest may not be written, or ``None``. Shared by the dry run and apply, so the
    preview announces exactly the refusal that ``--apply`` will record."""

    path = media_dir / "retention-manifest.json"
    if not _unlinked(path, work_root) or (_lexists(path) and not _is_real_file(path)):
        return f"manifest not written: {path} is not a plain file of this media"
    return None


def _write_manifest(media_dir: Path, *, work_root: Path, policy: Policy, now: datetime) -> None:
    path = media_dir / "retention-manifest.json"
    try:
        inventory = [item for item in _files(media_dir) if item != path]
        files = {
            item.relative_to(media_dir).as_posix(): {"size": os.lstat(_extended(item)).st_size}
            for item in inventory
        }
    except OSError as exc:
        # An inventory that silently omits a subtree is worse than none.
        raise _Refused(f"manifest not written: {_UNLISTABLE}") from exc
    refusal = _manifest_refusal(media_dir, work_root)
    if refusal is not None:
        raise _Refused(refusal)
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
    if trash_root is None or not _is_real_directory(trash_root):
        return []
    actions = []
    for path in sorted(_children(trash_root)):
        try:
            stamp = date.fromisoformat(path.name)
        except ValueError:
            continue
        if not _is_real_directory(path) or _real(path) != trash_root / path.name:
            continue
        expires = datetime(stamp.year, stamp.month, stamp.day, tzinfo=UTC) + _TRASH_GRACE
        if now >= expires:
            actions.append(GCAction("purge", path, "trash is more than 7 days old"))
    return actions


def _purgeable(work_root: Path, path: Path) -> bool:
    """Re-proven immediately before ``rmtree``: planning happened long before, and ``.trash`` or
    the dated directory may have been replaced by a link since."""

    trash = work_root / ".trash"
    return (
        path.parent == trash
        and _is_real_directory(trash)
        and _is_real_directory(path)
        and _unlinked(path, work_root)
        and _real(trash) == trash
        and _real(path) == path
    )


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
        if _exists(windows):
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
        if not _unlinked(target, work_root):
            refused.append(
                GCAction(
                    "skip", target, "artefact or one of its parents is a link or reparse point"
                )
            )
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
    work_root = _resolved(work_root)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    trash_root = _trash_root(work_root)
    actions = _purge_actions(trash_root, now)
    if trash_root is None:
        actions.append(
            GCAction("skip", work_root / ".trash", "trash is not a directory of this work root")
        )
    for media_dir in _media_directories(work_root):
        if trash_root is None:
            # Nothing can be moved without a real trash, so neither branch plans any media: the
            # dry run is the owner's preview and must list exactly what ``--apply`` returns.
            continue
        if not apply:
            plan = _retention_plan(media_dir, policy=policy, now=now)
            if plan is None:
                continue
            targets, refused = _targets(media_dir, plan, work_root=work_root, now=now)
            actions.extend(refused)
            if not targets:
                continue
            try:
                _files(media_dir)
            except OSError:
                actions.append(GCAction("skip", media_dir, _UNLISTABLE))
                continue
            media_moves = False
            for target, reason in targets:
                try:
                    _checked_destination(work_root, trash_root, target, media_dir, now.date())
                except _Refused as exc:
                    actions.append(GCAction("skip", target, str(exc)))
                    continue
                actions.append(GCAction("move", target, reason))
                media_moves = media_moves or target == media_dir
            if not media_moves:
                refusal = _manifest_refusal(media_dir, work_root)
                if refusal is not None:
                    actions.append(GCAction("skip", media_dir / "retention-manifest.json", refusal))
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
            try:
                # The complete listing comes first: a subtree we cannot read may hold a sidecar
                # that must be sealed before its upstream moves, so nothing moves without it.
                inventory = _files(media_dir)
            except OSError:
                actions.append(GCAction("skip", media_dir, _UNLISTABLE))
                continue
            paths = [target for target, _ in targets]
            if media_dir not in paths:
                try:
                    _rewrite_sidecars(work_root, media_dir, paths, inventory)
                except _Refused as exc:
                    actions.append(GCAction("skip", media_dir, str(exc)))
                    continue
            for target, reason in targets:
                if _lexists(target):
                    try:
                        _move_to_trash(work_root, trash_root, target, media_dir, now.date())
                    except _Refused as exc:
                        actions.append(GCAction("skip", target, str(exc)))
                        continue
                actions.append(GCAction("move", target, reason))
            if _lexists(media_dir):
                try:
                    _write_manifest(media_dir, work_root=work_root, policy=policy, now=now)
                except _Refused as exc:
                    actions.append(
                        GCAction("skip", media_dir / "retention-manifest.json", str(exc))
                    )
        finally:
            lock.release()
    if apply:
        final: list[GCAction] = []
        for action in actions:
            if action.operation == "purge" and _lexists(action.path):
                if not _purgeable(work_root, action.path):
                    final.append(
                        GCAction("skip", action.path, "trash changed since planning; not purged")
                    )
                    continue
                # Extended and unresolved: the dated directory itself is removed, and ``rmtree``
                # unlinks (never descends) any link or junction it meets inside it.
                shutil.rmtree(_extended(action.path))
            final.append(action)
        actions = final
    return GCResult(tuple(actions), apply)
