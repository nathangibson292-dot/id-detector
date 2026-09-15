"""Keyboard-first, loopback-only owner review for one ground-truth set."""

# Embedded CSS/JavaScript and HTML fragments stay compact in the generated single-file page.
# ruff: noqa: E501

from __future__ import annotations

import html
import json
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from id_detector import __version__
from id_detector.contracts import (
    EpisodesFile,
    GroundTruthRecord,
    IdentitiesRecord,
    SourceRecord,
    TruthRoleSegment,
    TruthWork,
)
from id_detector.io import canonical_json_bytes, path_is_file, read_text, sha256_file
from id_detector.present.exports import _candidate_label
from id_detector.present.index import media_dir_for_key_read_only
from id_detector.present.theme import head_html, topbar_html
from id_detector.truth import (
    EXPOSURE_NAME,
    TRUTH_RECORD_NAME,
    exact_corpus_root,
    exposure_path,
    open_corpus,
    prediction_exposure,
    record_exposure_in_ledger,
    record_path,
    recover_interrupted_write,
    refuse_frozen,
    refuse_reattribution,
    require_corpus_member,
    set_record_names,
    truth_write_lock,
    verify_truth,
)
from id_detector.truth_paths import (
    PinnedDirectory,
    is_link,
    is_within,
    link_refusal,
    path_key,
    pinned_set_directory,
    real_path,
    refuse_link_components,
)

_SET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_HANDLE = re.compile(r"(?<!\w)@[A-Za-z0-9_]\w*")
_LONG_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d{10,}(?![A-Za-z0-9])")
_SUGGESTED_OFFSETS_MS = {
    "release1-mall-grab-boiler-room-melbourne-22": 48_000,
    "release1-dj-heartstring-youtube-set": 51_000,
}
_MAX_BODY = 1 << 20
#: How long a save waits for another process's write lock before refusing.  Bounded so a second
#: review window reports a clear conflict instead of appearing to hang.
_SAVE_LOCK_TIMEOUT = 20.0


def reject_work_destination(path: Path, work_root: Path | None) -> None:
    """Refuse to treat anything beneath the work tree as a corpus record.

    Both sides are compared by :func:`truth_paths.is_within`, on ``path_key`` spellings:
    ``realpath`` follows symlinks, junctions, 8.3 short names and substituted drives; a Win32
    namespace prefix (``\\\\?\\C:\\...``, ``\\\\?\\UNC\\...``) that ``realpath`` keeps is stripped;
    and ``normcase`` removes letter case, so no alias spelling gets past the check.

    ``work/`` is a regenerable cache that ``idea gc`` prunes: a truth record written there would
    be the one kind of corpus loss nothing can undo.  ``--corpus`` takes an arbitrary directory,
    so the prohibition has to be enforced on the *resolved* destination rather than trusted.
    """

    if work_root is None:
        return
    if is_within(path, work_root):
        raise ValueError(
            f"refusing to review a truth record beneath the work tree: {path} resolves inside "
            f"{work_root}"
        )


def find_truth_path(corpus: Path, set_id: str, *, work_root: Path | None = None) -> Path:
    """Find exactly one corpus record by its contract ``set_id``, never by an input path."""

    if not _SET_ID.fullmatch(set_id):
        raise ValueError("set id must contain only letters, numbers, dot, underscore or hyphen")
    # Checked on the path as given, *before* anything is resolved, enumerated or read: a corpus
    # reached through a junction or symlink is refused with "pass the real path", never followed,
    # and only the supported <corpus>/<set>/ground_truth.json layout is accepted.
    with open_corpus(corpus, mutate=False, work_root=work_root) as handle:
        candidates = list(handle.truth_files)
    root = real_path(corpus)
    reject_work_destination(root, work_root)
    matches: list[Path] = []
    for candidate in candidates:
        refuse_link_components(candidate)
        resolved = real_path(candidate)
        reject_work_destination(resolved, work_root)
        if not is_within(resolved, root):
            continue
        try:
            truth = GroundTruthRecord.model_validate_json(read_text(resolved))
        except (OSError, ValueError):
            continue
        if truth.set_id == set_id:
            matches.append(resolved)
    if not matches:
        raise ValueError(f"set not found in corpus: {set_id}")
    if len(matches) != 1:
        raise ValueError(f"set id is duplicated in corpus: {set_id}")
    return matches[0]


def _shift(value: int, offset_ms: int, maximum: int) -> tuple[int, bool]:
    shifted = value + offset_ms
    clamped = max(0, min(maximum, shifted))
    return clamped, clamped != shifted


def collapsed_rows(truth: GroundTruthRecord) -> list[int]:
    """Row numbers (1-based) whose audible span is no longer positive.

    Clamping a negative offset pins both endpoints of an early row to zero, which still validates
    as a record but can never be saved: :func:`reviewed_record` requires a positive audible span.
    Preview has to surface that per row *before* the offset is applied, not at save time.
    """

    return [
        index + 1
        for index, episode in enumerate(truth.episodes)
        if episode.start_ms_range[1] > episode.end_ms_range[0]
        or episode.start_ms_range[0] >= episode.end_ms_range[1]
    ]


def preview_bulk_offset(
    truth: GroundTruthRecord, offset_ms: int
) -> tuple[GroundTruthRecord, int, list[int]]:
    """Return a no-I/O offset preview, the clamped-field count, and any collapsed row numbers."""

    duration = truth.source.duration_ms
    episodes = []
    clamped_count = 0
    for episode in truth.episodes:
        start_values = []
        for value in episode.start_ms_range:
            shifted, clamped = _shift(value, offset_ms, max(0, duration - 1))
            start_values.append(shifted)
            clamped_count += int(clamped)
        end_values = []
        for value in episode.end_ms_range:
            shifted, clamped = _shift(value, offset_ms, duration)
            end_values.append(shifted)
            clamped_count += int(clamped)
        if end_values[0] < start_values[1]:
            end_values[0] = start_values[1]
        if end_values[1] < end_values[0]:
            end_values[1] = end_values[0]
        roles = []
        for role in episode.role_segments:
            # The identical shift and clamp as the row: a role's start obeys the start clamp
            # (last millisecond), its end the end clamp (media end), so a segment moves with its
            # row even where the row's own endpoints are pinned to a bound.
            role_start, start_clamped = _shift(role.from_ms, offset_ms, max(0, duration - 1))
            role_end, end_clamped = _shift(role.to_ms, offset_ms, duration)
            clamped_count += int(start_clamped) + int(end_clamped)
            role_start = max(start_values[0], min(role_start, end_values[1]))
            role_end = max(role_start, min(role_end, end_values[1]))
            if role_end > role_start:
                roles.append(role.model_copy(update={"from_ms": role_start, "to_ms": role_end}))
        episodes.append(
            episode.model_copy(
                update={
                    "start_ms_range": tuple(start_values),
                    "end_ms_range": tuple(end_values),
                    "role_segments": roles,
                }
            )
        )
    preview = truth.model_copy(update={"episodes": episodes})
    result = GroundTruthRecord.model_validate(preview.model_dump(mode="json"))
    return result, clamped_count, collapsed_rows(result)


def _safe_label(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError(f"{field} must be 1..500 characters")
    result = value.strip()
    if _URL.search(result) or _HANDLE.search(result) or _LONG_NUMBER.search(result):
        raise ValueError(f"{field} violates the committed-corpus identifier policy")
    return result


def _range(value: object, field: str, duration_ms: int) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise ValueError(f"{field} must be a two-integer range")
    lo, hi = value
    if lo < 0 or hi < lo or hi > duration_ms:
        raise ValueError(f"{field} lies outside the media duration")
    return lo, hi


def reconciled_role_segments(
    episode: Any, start: tuple[int, int], end: tuple[int, int]
) -> list[TruthRoleSegment]:
    """Carry an episode's hand-made role annotation through a timing edit.

    The owner transcribed these by hand: ``uncertain`` where the layering could not be called,
    ``layer`` where a track sits under another.  Replacing them with one ``dominant`` span both
    destroys that work and *fabricates* a claim the owner never made, so this tool never invents a
    role.  Instead:

    * an unchanged or purely translated span (every endpoint moved by the same delta — what a bulk
      offset does) moves the existing segments by that same delta, preserving layering and any
      time-varying structure exactly;
    * any other timing edit keeps the same segments and roles but clips them into the new audible
      span, dropping only a segment the edit moved entirely outside it.

    A segment that no longer has positive duration is dropped rather than stretched; an episode
    that had no role annotation still has none.
    """

    segments = list(episode.role_segments)
    if not segments:
        return []
    deltas = {
        start[0] - episode.start_ms_range[0],
        start[1] - episode.start_ms_range[1],
        end[0] - episode.end_ms_range[0],
        end[1] - episode.end_ms_range[1],
    }
    if len(deltas) == 1:
        delta = deltas.pop()
        moved = [
            segment.model_copy(
                update={"from_ms": segment.from_ms + delta, "to_ms": segment.to_ms + delta}
            )
            for segment in segments
            if segment.from_ms + delta >= 0
        ]
        if all(segment.from_ms >= start[0] and segment.to_ms <= end[1] for segment in moved):
            return moved
    clipped = []
    for segment in segments:
        from_ms = max(start[0], min(segment.from_ms, end[1]))
        to_ms = max(start[0], min(segment.to_ms, end[1]))
        if to_ms > from_ms:
            clipped.append(segment.model_copy(update={"from_ms": from_ms, "to_ms": to_ms}))
    return clipped


def _offsets(value: object, duration_ms: int) -> list[int]:
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or len(value) > 1_000
        or any(
            isinstance(item, bool) or not isinstance(item, int) or abs(item) > duration_ms
            for item in value
        )
    ):
        raise ValueError("offsets_ms must be a list of whole-millisecond offsets within the media")
    return value


def reviewed_record(
    original: GroundTruthRecord,
    rows: object,
    *,
    annotator_ref: str,
    offsets_ms: object = None,
) -> GroundTruthRecord:
    """Apply only the fields the review screen owns and complete the first-pass decisions.

    ``offsets_ms`` is the sequence of bulk offsets the owner applied (net of undo).  Replaying it
    through :func:`preview_bulk_offset` gives every role endpoint the identical shift and clamp the
    row's own start and end received, so a clamped row's segments move with it instead of being
    guessed at from four endpoint deltas.  A row whose times the owner then changed further is
    reconciled against that shifted state.
    """

    if not isinstance(rows, list) or len(rows) != len(original.episodes):
        raise ValueError("save must contain every truth row exactly once")
    shifted = original
    for offset in _offsets(offsets_ms, original.source.duration_ms):
        shifted, _, _ = preview_bulk_offset(shifted, offset)
    updated = []
    for index, (episode, moved, row) in enumerate(
        zip(original.episodes, shifted.episodes, rows, strict=True)
    ):
        if not isinstance(row, dict) or row.get("index") != index:
            raise ValueError("truth rows are missing or out of order")
        artist = _safe_label(row.get("artist"), f"row {index + 1} artist")
        title = _safe_label(row.get("title"), f"row {index + 1} title")
        start = _range(
            row.get("start_ms_range"), f"row {index + 1} start", original.source.duration_ms
        )
        end = _range(row.get("end_ms_range"), f"row {index + 1} end", original.source.duration_ms)
        if start[1] > end[0] or start[0] >= end[1]:
            raise ValueError(f"row {index + 1} needs a positive audible span")
        if episode.draft and row.get("confirmed") is not True:
            raise ValueError(f"row {index + 1} still needs listening and confirmation")
        if episode.draft:
            if start == moved.start_ms_range and end == moved.end_ms_range:
                roles = list(moved.role_segments)
            else:
                roles = reconciled_role_segments(moved, start, end)
            if episode.role_segments and not roles:
                raise ValueError(
                    f"row {index + 1}: this timing edit would discard the row's hand-made role "
                    "annotation; adjust its times or undo the edit"
                )
            updated.append(
                episode.model_copy(
                    update={
                        "work": TruthWork(artist=artist, title=title),
                        "start_ms_range": start,
                        "end_ms_range": end,
                        "verified_against": "audio",
                        "version_verified": False,
                        "annotator_ref": annotator_ref,
                        "role_segments": roles,
                        "draft": False,
                    }
                )
            )
        else:
            # A completed row is display-only here: later state-machine stages own it.
            if (
                artist != episode.work.artist
                or title != episode.work.title
                or start != episode.start_ms_range
                or end != episode.end_ms_range
            ):
                raise ValueError(f"row {index + 1} is already verified and cannot be edited")
            updated.append(episode)
    candidate = original.model_copy(update={"episodes": updated})
    return GroundTruthRecord.model_validate(candidate.model_dump(mode="json"))


def _audio_for(media_dir: Path | None, media_key: str) -> Path | None:
    if media_dir is None:
        return None
    source_path = media_dir / "ingest" / "source.json"
    try:
        source = SourceRecord.model_validate_json(read_text(source_path))
        candidate = (media_dir / source.original.path).resolve()
    except (OSError, ValueError):
        return None
    root = media_dir.resolve()
    if (
        source.media_key != media_key
        or not candidate.is_relative_to(root)
        or not path_is_file(candidate)
    ):
        return None
    return candidate


def _predictions(media_dir: Path | None) -> list[dict[str, Any]]:
    if media_dir is None:
        return []
    try:
        episodes = EpisodesFile.model_validate_json(read_text(media_dir / "fuse" / "episodes.json"))
        identities = IdentitiesRecord.model_validate_json(
            read_text(media_dir / "fuse" / f"identities.gen{episodes.generation}.json")
        )
        result = []
        for episode in episodes.episodes:
            artist, title = _candidate_label(identities, episode.candidate_id)
            result.append(
                {
                    "artist": artist,
                    "title": title,
                    "start_ms": episode.best_start_ms,
                    "end_ms": episode.best_end_ms,
                    "tier": episode.tiers.work,
                }
            )
        return result
    except (OSError, ValueError, StopIteration):
        return []


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _write_exposure(pinned: PinnedDirectory, *, set_id: str, when: str) -> None:
    """Write the exposure sidecar inside the held, verified set directory -- never through a link."""

    pinned.write_bytes(
        EXPOSURE_NAME,
        canonical_json_bytes(
            {
                "schema_version": "1.0.0",
                "generated_by": "id-detector/0.1.0",
                "set_id": set_id,
                "predictions_visible_during_review": True,
                "first_revealed_at_utc": when,
                "tool": "idea truth review",
            }
        ),
    )


class TruthReviewSession:
    """The bounded mutable state for one server and one selected corpus set."""

    def __init__(
        self,
        truth_path: Path,
        *,
        work_root: Path,
        annotator_ref: str = "owner",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.work_root = work_root
        # The gateway first: the record sits exactly at <corpus>/<set>/ground_truth.json, its
        # corpus passes the link, work-tree and inside-another-corpus refusals and the layout
        # check, and the record is one of the vetted files -- before anything is resolved or read.
        with open_corpus(
            exact_corpus_root(truth_path, name=TRUTH_RECORD_NAME),
            mutate=False,
            work_root=work_root,
        ) as handle:
            require_corpus_member(handle, truth_path)
        self.truth_path = real_path(truth_path)
        reject_work_destination(self.truth_path, work_root)
        record_path(truth_path)
        # Every record this tool reads or writes beside the truth must be a real file in the set
        # directory.  A link out of it -- into work/, which idea gc prunes, or anywhere else --
        # is refused before anything is read through it.
        for name in set_record_names(self.truth_path):
            sibling = self.truth_path.with_name(name)
            if is_link(sibling):
                raise link_refusal(sibling, sibling)
        #: Where the set directory resolved when it was vetted; every write re-checks it.
        self.set_dir_key = path_key(self.truth_path.parent)
        recover_interrupted_write(self.truth_path, work_root=work_root)
        self.truth = GroundTruthRecord.model_validate_json(read_text(self.truth_path))
        refuse_frozen(self.truth_path, self.truth)
        if any(episode.second_pass_ref is not None for episode in self.truth.episodes):
            raise ValueError("truth review cannot reopen a set after its second pass")
        if any(episode.disagreement_resolution is not None for episode in self.truth.episodes):
            raise ValueError("truth review cannot reopen a resolved set")
        refuse_reattribution(self.truth, annotator_ref)
        self.original_sha256 = sha256_file(self.truth_path)
        self.annotator_ref = annotator_ref
        self.clock = clock
        self.lock = threading.Lock()
        self.exposure_path = exposure_path(self.truth_path)
        self.media_dir = media_dir_for_key_read_only(work_root, self.truth.source.media_key)
        self.audio_path = _audio_for(self.media_dir, self.truth.source.media_key)
        self.predictions_visible = self._recorded_prediction_visibility()
        self.suggested_offset_ms = _SUGGESTED_OFFSETS_MS.get(self.truth.set_id)

    def _recorded_prediction_visibility(self) -> bool:
        """Read the exposure flag back from disk: the sidecar first, then any saved annotation.

        Both are consulted because they become durable at different moments -- the sidecar the
        instant predictions are revealed, the annotation when a pass is saved -- and the flag is
        one-way: once either says the owner saw our answers, this set is exposed.
        """

        return bool(prediction_exposure(self.truth_path)["predictions_visible_during_review"])

    def reveal_predictions(self) -> list[dict[str, Any]]:
        """Record the exposure durably *before* any prediction can reach the screen.

        This flag is the corpus's own evidence that its truth was made independently of IDea.
        Holding it only in memory made it survivable: reveal, stop the server, reopen, save, and
        the record would claim an independence the owner no longer had.  So the write happens
        first, and a failed write refuses the reveal -- the flag may be pessimistic, never
        optimistic.
        """

        with (
            self.lock,
            truth_write_lock(self.truth_path, timeout=_SAVE_LOCK_TIMEOUT),
            pinned_set_directory(
                self.truth_path.parent,
                expected_key=self.set_dir_key,
                work_root=self.work_root,
            ) as pinned,
        ):
            # Every reveal re-reads the disk under the record's lock rather than trusting the
            # cached flag: a sidecar deleted since the last reveal is written again before any
            # prediction is assembled.  Exposure only ever moves from false to true.
            refuse_frozen(self.truth_path, self.truth)
            self._ensure_exposure(pinned)
            self.predictions_visible = True
            predictions = _predictions(self.media_dir)
            # An actor that ignores the advisory lock could have removed the evidence while the
            # predictions were being assembled.  So it is reasserted now and verified while held
            # open without delete sharing, and the predictions are returned only from inside that
            # hold; if the evidence cannot be reasserted and verified, nothing is returned.
            for _ in range(3):
                self._ensure_exposure(pinned)
                try:
                    with pinned.hold_undeletable(EXPOSURE_NAME) as raw:
                        self._verify_exposure(raw)
                        self._evidence_held()
                        return predictions
                except FileNotFoundError:
                    continue
            raise ValueError(
                "the exposure record could not be reasserted on disk; predictions withheld"
            )

    def _ensure_exposure(self, pinned: PinnedDirectory) -> None:
        when = self.clock().astimezone(UTC).isoformat().replace("+00:00", "Z")
        if not pinned.exists(EXPOSURE_NAME):
            _write_exposure(pinned, set_id=self.truth.set_id, when=when)
        # The second, corpus-level record: it survives the sidecar being tidied away before the
        # first save, and is consulted by every later pass, freeze, scorer and certification.
        record_exposure_in_ledger(
            self.truth_path, set_id=self.truth.set_id, when=when, work_root=self.work_root
        )

    def _verify_exposure(self, raw: bytes) -> None:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = None
        if not (
            isinstance(payload, dict)
            and payload.get("predictions_visible_during_review") is True
            and payload.get("set_id") == self.truth.set_id
        ):
            raise ValueError(
                "the exposure record on disk does not record this reveal; predictions withheld"
            )

    def _evidence_held(self) -> None:
        """Called while the exposure record is held undeletable, just before returning (a seam)."""

    def save(self, payload: object) -> GroundTruthRecord:
        if not isinstance(payload, dict):
            raise ValueError("save body must be a JSON object")
        with self.lock, truth_write_lock(self.truth_path, timeout=_SAVE_LOCK_TIMEOUT):
            # Inside the cross-process lock: re-check the record *and* the exposure evidence, so
            # another review process cannot slip a write (or a reveal) between check and commit.
            if sha256_file(self.truth_path) != self.original_sha256:
                raise ValueError("ground truth changed on disk; reload review before saving")
            refuse_frozen(self.truth_path, self.truth)
            exposed = self.predictions_visible or self._recorded_prediction_visibility()
            self.predictions_visible = exposed
            updated = reviewed_record(
                self.truth,
                payload.get("rows"),
                annotator_ref=self.annotator_ref,
                offsets_ms=payload.get("offsets_ms"),
            )
            provenance = {
                "predictions_visible_during_review": exposed,
                "tool": "idea truth review",
                "version": __version__,
                "reviewed_at_utc": self.clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
            }
            saved = verify_truth(
                self.truth_path,
                annotator_ref=self.annotator_ref,
                annotation_record=updated,
                annotation_provenance=provenance,
                work_root=self.work_root,
                expected_dir_key=self.set_dir_key,
            )
            self.truth = saved
            self.original_sha256 = sha256_file(self.truth_path)
            return saved


_CSS = r"""
.review{max-width:1220px}.intro{display:flex;justify-content:space-between;gap:20px;align-items:end}
h1{font:800 34px/1.1 var(--display);letter-spacing:-.04em;margin:28px 0 8px}.muted{color:var(--muted)}
.protect{padding:12px 14px;border:1px solid rgba(251,191,36,.35);background:rgba(251,191,36,.08);
border-radius:10px;color:#f5d98a}.transport{position:sticky;top:56px;z-index:10;background:rgba(19,19,27,.95);
border:1px solid var(--line2);border-radius:14px;padding:14px;margin:16px 0;box-shadow:0 12px 35px #0008}
.times{display:flex;gap:26px;align-items:baseline}.time{font:800 34px/1 var(--mono)}.claimed{font:700 20px/1 var(--mono);color:var(--cyan)}
.controls,.offset{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px}
.offset input{width:110px;background:var(--bg);color:var(--fg);border:1px solid var(--line2);padding:8px;
border-radius:8px;font:14px var(--mono)}.message{min-height:22px;color:var(--warn);margin:8px 0 0}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse}
th{position:sticky;top:0;background:var(--card2);text-align:left;color:var(--muted);font-size:11px;
text-transform:uppercase;letter-spacing:.1em}th,td{padding:10px;border-bottom:1px solid var(--line)}
tr.truth-row{cursor:pointer}tr.truth-row:hover{background:#ffffff08}tr.current{background:rgba(139,92,246,.17)}
td.num,td.when{font-family:var(--mono);font-variant-numeric:tabular-nums}.state{font-size:11px;text-transform:uppercase}
.state.verified{color:var(--ok)}.state.needs-listening{color:var(--warn)}.state.draft{color:var(--muted)}
.state.no-span{color:var(--bad)}tr.collapsed{background:rgba(239,68,68,.16)}
.edit{width:100%;min-width:150px;background:transparent;border:1px solid transparent;color:var(--fg);padding:5px}
.editing .edit{background:var(--bg);border-color:var(--accent);border-radius:6px}.guess{display:none;color:var(--pink)}
.guesses-on .guess{display:table-row}.guess td{background:rgba(255,61,138,.07);font-style:italic}
.modal{display:none;position:fixed;inset:0;z-index:50;background:#000b;align-items:center;justify-content:center}
.modal.open{display:flex}.help{max-width:580px;background:var(--card);border:1px solid var(--line2);padding:24px;
border-radius:14px}.help-grid{display:grid;grid-template-columns:auto 1fr;gap:9px 16px}.audio-missing{color:var(--bad)}
@media(max-width:720px){.intro{display:block}.times{justify-content:space-between}.time{font-size:26px}}
"""

_JS = r"""
const audio=document.getElementById('audio'), body=document.body, table=document.getElementById('rows');
const playhead=document.getElementById('playhead'), claimed=document.getElementById('claimed');
const message=document.getElementById('message'), help=document.getElementById('help');
const offsetInput=document.getElementById('offset');
// rows is mutated in place and never replaced: an offset owns only the two time fields it wrote,
// so undoing one can never take an artist or title correction with it.
let rows=TRUTH_ROWS, current=0, dirty=false, editBase=null;
// appliedOffsets is the offset intent the server replays, so every role segment gets exactly the
// shift and clamp its row got; it grows on apply and shrinks on undo, never on a mere preview.
let offsetBase=null, offsetUndo=[], appliedOffsets=[], previewOffset=0, collapsed=[];
function rowEls(){return document.querySelectorAll('.truth-row');}
function fmt(ms){let s=Math.max(0,Math.floor(ms/1000)),h=Math.floor(s/3600),m=Math.floor(s%3600/60);
 let z=String(s%60).padStart(2,'0');return h?h+':'+String(m).padStart(2,'0')+':'+z:m+':'+z;}
function clamp(v,max){return Math.max(0,Math.min(max,v));}
// The same rule the server applies at save time, checked here so a preview can say which row.
function isCollapsed(r){return r.start_ms_range[1]>r.end_ms_range[0]||r.start_ms_range[0]>=r.end_ms_range[1];}
function rowList(list){return list.map(n=>'row '+n).join(', ');}
function render(){rowEls().forEach((tr,i)=>{let r=rows[i];
 tr.classList.toggle('current',i===current);let bad=isCollapsed(r);tr.classList.toggle('collapsed',bad);
 tr.querySelector('.start').textContent=fmt(r.start_ms_range[0]);
 tr.querySelector('.end').textContent=fmt(r.end_ms_range[1]);
 tr.querySelectorAll('input').forEach(el=>{if(el!==document.activeElement)el.value=r[el.dataset.field];});
 let state=tr.querySelector('.state');
 let label=bad?'no-span':(r.preverified||r.confirmed)?'verified':r.touched?'needs-listening':'draft';
 state.textContent=label;state.className='state '+label;});claimed.textContent=fmt(rows[current].start_ms_range[0]);}
function select(i,seek=true){rows[current].touched=rows[current].touched||!rows[current].confirmed;
 if(editBase!==null)endEdit();current=clamp(i,rows.length-1);
 if(seek&&audio)audio.currentTime=rows[current].start_ms_range[0]/1000;render();}
function edit(){let r=rows[current];
 if(r.preverified){message.textContent='Row '+(current+1)+' is already verified; later passes own it.';return;}
 let tr=rowEls()[current];tr.classList.add('editing');editBase={artist:r.artist,title:r.title};
 let el=tr.querySelector('.artist');el.focus();if(el.select)el.select();}
function endEdit(){let tr=rowEls()[current];tr.classList.remove('editing');editBase=null;
 tr.querySelectorAll('input').forEach(el=>el.blur());}
function cancelEdit(){if(editBase){rows[current].artist=editBase.artist;rows[current].title=editBase.title;}
 endEdit();render();message.textContent='Edit discarded; row '+(current+1)+' is unchanged.';}
function confirmRow(){if(rows[current].preverified){message.textContent='Row '+(current+1)+' is already verified; later passes own it.';return;}
 endEdit();rows[current].confirmed=true;rows[current].touched=true;dirty=true;render();select(current+1);}
function stamp(){if(!audio)return;
 if(offsetBase!==null){message.textContent='Apply or undo the offset preview before stamping a start.';return;}
 let v=clamp(Math.round(audio.currentTime*1000),DURATION_MS-1);
 rows[current].start_ms_range=[v,v];rows[current].confirmed=false;rows[current].touched=true;dirty=true;render();}
function captureTimes(){return rows.map(r=>({start:r.start_ms_range.slice(),end:r.end_ms_range.slice()}));}
function restoreTimes(base){base.forEach((t,i)=>{rows[i].start_ms_range=t.start.slice();rows[i].end_ms_range=t.end.slice();});}
function shiftTimes(base,ms){let clamps=0;collapsed=[];base.forEach((t,i)=>{
 let s=t.start.map(v=>{let n=clamp(v+ms,DURATION_MS-1);if(n!==v+ms)clamps++;return n;});
 let e=t.end.map(v=>{let n=clamp(v+ms,DURATION_MS);if(n!==v+ms)clamps++;return n;});
 if(e[0]<s[1])e[0]=s[1];if(e[1]<e[0])e[1]=e[0];
 rows[i].start_ms_range=s;rows[i].end_ms_range=e;if(isCollapsed(rows[i]))collapsed.push(i+1);});return clamps;}
function preview(){let raw=String(offsetInput.value).trim(), seconds=Number(raw);
 if(raw===''||!Number.isFinite(seconds)){message.textContent='Enter an offset in seconds.';return;}
 if(offsetBase===null)offsetBase=captureTimes();
 previewOffset=Math.round(seconds*1000);let clamps=shiftTimes(offsetBase,previewOffset);render();
 if(collapsed.length){message.textContent='Cannot apply '+(previewOffset/1000).toFixed(1)+' s: '+rowList(collapsed)+
  ' would be clamped to a zero-length span, and a row with no audible span cannot be saved. Undo, or use a smaller shift.';return;}
 message.textContent='Preview only \u2014 nothing written.'+(clamps?' '+clamps+' boundary value(s) clamped.':'');}
function applyOffset(){if(offsetBase===null)preview();if(offsetBase===null)return;
 if(collapsed.length){message.textContent='Offset not applied: '+rowList(collapsed)+
  ' would have no audible span at '+(previewOffset/1000).toFixed(1)+' s. Undo, choose a smaller shift, or fix those rows first.';return;}
 offsetUndo.push(offsetBase);appliedOffsets.push(previewOffset);offsetBase=null;dirty=true;
 message.textContent='Applied '+(previewOffset/1000).toFixed(1)+' s in this review. Save is still required.';}
function undo(){let base=null;
 if(offsetBase!==null){base=offsetBase;offsetBase=null;}else if(offsetUndo.length){base=offsetUndo.pop();appliedOffsets.pop();}
 else{message.textContent='Nothing to undo.';return;}
 restoreTimes(base);collapsed=[];dirty=true;render();
 message.textContent='Offset undone. Artist and title edits are untouched; save is still required.';}
async function reveal(){let box=document.getElementById('show-guesses');
 if(!box.checked){body.classList.remove('guesses-on');return;}
 let res=await fetch('/predictions',{method:'POST',headers:{'X-CSRF-Token':CSRF_TOKEN}});
 if(!res.ok){box.checked=false;message.textContent='Could not reveal guesses.';return;}let guesses=(await res.json()).predictions;
 document.querySelectorAll('.guess').forEach(e=>e.remove());guesses.forEach(g=>{let tr=document.createElement('tr');tr.className='guess';
 tr.innerHTML='<td><\/td><td class="when">'+fmt(g.start_ms)+'<\/td><td colspan="2">Our guess: '+escapeHtml(g.artist)+' \u2014 '+escapeHtml(g.title)+'<\/td><td>'+escapeHtml(g.tier)+'<\/td>';table.appendChild(tr);});
 body.classList.add('guesses-on');message.textContent='Guesses revealed. This set is now recorded on disk as prediction-exposed; that cannot be taken back.';}
function escapeHtml(s){let d=document.createElement('div');d.textContent=s;return d.innerHTML;}
async function save(){if(offsetBase!==null){message.textContent='Apply or undo the offset preview before saving.';return;}
 rowEls().forEach((tr,i)=>{tr.querySelectorAll('input').forEach(el=>{rows[i][el.dataset.field]=el.value;});});
 let bad=rows.map((r,i)=>isCollapsed(r)?i+1:0).filter(Boolean);
 if(bad.length){message.textContent='Not saved: '+rowList(bad)+' has no audible span.';return;}
 let res=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF_TOKEN},body:JSON.stringify({rows:rows,offsets_ms:appliedOffsets})});
 let data=await res.json();if(!res.ok){message.textContent=data.error||'Save failed.';return;}dirty=false;
 message.textContent='Saved first-pass truth and annotation atomically.';}
rowEls().forEach((tr,i)=>{tr.addEventListener('click',e=>{if(!e.target.matches('input'))select(i);});
 tr.querySelectorAll('input').forEach(el=>el.addEventListener('input',()=>{rows[i][el.dataset.field]=el.value;rows[i].confirmed=false;rows[i].touched=true;dirty=true;render();}));});
offsetInput.addEventListener('input',preview);
document.getElementById('offset-minus').onclick=()=>{offsetInput.value=(Number(offsetInput.value||0)-1).toFixed(1);preview();};
document.getElementById('offset-plus').onclick=()=>{offsetInput.value=(Number(offsetInput.value||0)+1).toFixed(1);preview();};
document.getElementById('apply-offset').onclick=applyOffset;document.getElementById('undo-offset').onclick=undo;
document.getElementById('show-guesses').onchange=reveal;document.getElementById('save').onclick=save;
if(audio){audio.addEventListener('timeupdate',()=>playhead.textContent=fmt(Math.round(audio.currentTime*1000)));}
document.addEventListener('keydown',e=>{
 if(help.classList.contains('open')){if(e.key==='Escape'||e.key==='?'){help.classList.remove('open');e.preventDefault();}return;}
 if(e.key==='?'&&!e.ctrlKey){help.classList.add('open');e.preventDefault();return;}
 // Typing in a field: Enter commits and hands focus back to the row loop, Escape discards it.
 // Without these two transitions the documented edit -> confirm -> navigate loop dead-ends in
 // the input, and every later N/P/S is typed into the artist box instead.
 if(e.target.matches('input')){
  if(e.key==='Escape'){e.preventDefault();if(e.target===offsetInput)offsetInput.blur();else cancelEdit();return;}
  if(e.key==='Enter'){e.preventDefault();if(e.target===offsetInput){offsetInput.blur();applyOffset();}else confirmRow();return;}
  return;}
 let k=e.key.toLowerCase();
 if(e.code==='Space'){if(audio){audio.paused?audio.play():audio.pause();}e.preventDefault();}
 else if(k==='j'&&audio){audio.currentTime=Math.max(0,audio.currentTime-(e.shiftKey?30:5));}
 else if(k==='l'&&audio){audio.currentTime=Math.min(audio.duration||DURATION_MS/1000,audio.currentTime+(e.shiftKey?30:5));}
 else if(e.key==='Enter')confirmRow();else if(k==='e'){edit();e.preventDefault();}else if(k==='t')stamp();
 else if(k==='n')select(current+1);else if(k==='p')select(current-1);
 else if(k==='s'){save();e.preventDefault();}else if(e.key==='Escape')message.textContent='';});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});render();
"""


def _page(session: TruthReviewSession, csrf_token: str) -> bytes:
    truth = session.truth
    rows = [
        {
            "index": index,
            "artist": episode.work.artist,
            "title": episode.work.title,
            "start_ms_range": list(episode.start_ms_range),
            "end_ms_range": list(episode.end_ms_range),
            "confirmed": not episode.draft,
            "preverified": not episode.draft,
            "touched": False,
        }
        for index, episode in enumerate(truth.episodes)
    ]
    table_rows = "".join(
        '<tr class="truth-row" data-index="{index}"><td class="num">{number}</td>'
        '<td class="when start">{start}</td><td><input class="edit artist" data-field="artist" '
        'value="{artist}" aria-label="Artist row {number}"></td><td><input class="edit title" '
        'data-field="title" value="{title}" aria-label="Title row {number}"></td>'
        '<td><span class="state {state}">{state}</span></td><td class="when end">{end}</td></tr>'.format(
            index=index,
            number=index + 1,
            start=_format_ms(episode.start_ms_range[0]),
            end=_format_ms(episode.end_ms_range[1]),
            artist=html.escape(episode.work.artist, quote=True),
            title=html.escape(episode.work.title, quote=True),
            state="draft" if episode.draft else "verified",
        )
        for index, episode in enumerate(truth.episodes)
    )
    suggestion = ""
    if session.suggested_offset_ms is not None:
        value = session.suggested_offset_ms / 1000
        suggestion = (
            f'<button class="btn" type="button" data-offset="{value:g}" '
            "onclick=\"document.getElementById('offset').value=this.dataset.offset;preview()\">"
            f"Use scorer suggestion {value:+g} s</button>"
        )
    audio = (
        f'<audio id="audio" preload="metadata" src="/media/{truth.source.media_key}/audio"></audio>'
        if session.audio_path is not None
        else '<p class="audio-missing">Audio is not in the local media index. Review and editing still work; playback and playhead stamping do not.</p>'
    )
    previous = (
        '<p class="audio-missing">This set already records on disk that IDea guesses were visible during a review; its truth is no longer independent of our predictions.</p>'
        if session.predictions_visible
        else ""
    )
    body = (
        topbar_html(new=False)
        + '<main class="review"><div class="intro"><div><p class="muted">OWNER TRUTH REVIEW</p>'
        f"<h1>{html.escape(truth.set_id)}</h1><p>{len(rows)} truth episodes · explicit save only</p></div>"
        '<button class="btn primary" id="save" type="button">Save first pass <kbd>S</kbd></button></div>'
        '<p class="protect">Predictions are hidden so truth stays independent; revealing “our guess” never fills or confirms a truth row, and that choice is written to disk the moment you make it.</p>'
        + previous
        + '<section class="transport"><div class="times"><div><span class="muted">PLAYHEAD</span><br>'
        '<span class="time" id="playhead">0:00</span></div><div><span class="muted">CLAIMED START</span><br>'
        '<span class="claimed" id="claimed">0:00</span></div></div>'
        + audio
        + '<div class="controls"><button class="btn" type="button" onclick="audio&&audio.paused?audio.play():audio&&audio.pause()">Play / pause <kbd>Space</kbd></button>'
        '<label><input id="show-guesses" type="checkbox"> Show what IDea found (our guess)</label>'
        '<button class="btn" type="button" onclick="help.classList.add(\'open\')">Keyboard help <kbd>?</kbd></button></div>'
        '<div class="offset"><strong>Bulk offset preview</strong><input id="offset" type="number" step="0.1" value="0" aria-label="Offset seconds">'
        '<button class="btn" id="offset-minus" type="button">−1 s</button><button class="btn" id="offset-plus" type="button">+1 s</button>'
        + suggestion
        + '<button class="btn primary" id="apply-offset" type="button">Confirm offset</button><button class="btn" id="undo-offset" type="button">Undo</button></div>'
        '<p class="message" id="message"></p></section><div class="table-wrap"><table><thead><tr><th>#</th><th>Start</th><th>Artist</th><th>Title</th><th>State</th><th>End</th></tr></thead>'
        f'<tbody id="rows">{table_rows}</tbody></table></div></main>'
        '<div class="modal" id="help" role="dialog" aria-modal="true"><div class="help"><h2>Keyboard map</h2><div class="help-grid">'
        "<kbd>Space</kbd><span>Play / pause</span><kbd>J / L</kbd><span>Seek −5 s / +5 s</span>"
        "<kbd>Shift+J / Shift+L</kbd><span>Seek −30 s / +30 s</span><kbd>Enter</kbd><span>Confirm current row and advance</span>"
        "<kbd>E</kbd><span>Edit artist/title inline</span><kbd>T</kbd><span>Stamp start from playhead</span>"
        "<kbd>Enter</kbd><span>In a field: commit the edit, leave the field, confirm the row. In the offset box: apply the preview</span>"
        "<kbd>Escape</kbd><span>In a field: discard this edit and leave the field. In the offset box: leave it (preview stays)</span>"
        "<kbd>N / P</kbd><span>Next / previous row</span><kbd>S</kbd><span>Save</span><kbd>?</kbd><span>Open / close this help</span>"
        '</div><button class="btn" type="button" onclick="help.classList.remove(\'open\')">Close</button></div></div>'
    )
    data = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    script = (
        f"const CSRF_TOKEN={json.dumps(csrf_token)};const DURATION_MS={truth.source.duration_ms};"
        f"const TRUTH_ROWS={data};" + _JS
    )
    return (
        head_html(f"Truth review — {truth.set_id}", _CSS)
        + f"<body>{body}<script>{script}</script></body></html>"
    ).encode("utf-8")


def _format_ms(value: int) -> str:
    seconds = value // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


# HTTP routing for this screen lives in :mod:`idea_web.truth_review` (4a-ii): the retired stdlib
# handler is gone, and every file decision above stays here, in one place.
