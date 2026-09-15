"""Regressions for the round-2 (sol xhigh) review of the truth/corpus follow-up.

Each test was first run against the follow-up as it stood before this round's fixes and failed
there (or, where noted in ``docs/reviews/followup-truth-corpus.md``, was already refused).  Tests
that could block forever against the old lock run their competitor in a thread with a bounded
join, so a hang shows up as a failure rather than a stuck run.  Nothing touches ``data/corpus/``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import id_detector.truth as truth_module
import id_detector.truth_review as review_module
from id_detector.benchmark.scorer import corpus_independent, truth_is_frozen_verified
from id_detector.contracts import GroundTruthRecord
from id_detector.io import read_text
from id_detector.truth import freeze_truth, seed_truth, truth_write_lock
from id_detector.truth_review import TruthReviewSession, exposure_path
from scripts import score_corpus
from tests.test_truth_corpus_followup import _symlink
from tests.test_truth_review import _rows, _truth

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "truth-review"
WINDOWS = os.name == "nt"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    target = tmp_path / "root" / "corpus"
    shutil.copytree(FIXTURE, target)
    return target


def _junction(link: Path, target: Path) -> None:
    if not WINDOWS:  # pragma: no cover
        link.symlink_to(target, target_is_directory=True)
        return
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if made.returncode != 0:  # pragma: no cover
        pytest.skip(f"cannot create a junction: {made.stderr.strip()}")


def _record(path: Path) -> GroundTruthRecord:
    return GroundTruthRecord.model_validate_json(read_text(path))


def _verify_set(set_dir: Path, tmp_path: Path) -> Path:
    truth_path = set_dir / "ground_truth.json"
    TruthReviewSession(truth_path, work_root=tmp_path / "work").save(
        {"rows": _rows(_record(truth_path))}
    )
    return truth_path


def _frozen(corpus: Path, tmp_path: Path) -> Path:
    truth_path = _verify_set(corpus / "fixture-set", tmp_path)
    freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    return truth_path


def _in_thread(target, timeout: float = 15.0) -> dict[str, object]:
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = target()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    outcome["finished"] = not worker.is_alive()
    return outcome


# --------------------------------------------------------------------------------------------
# r2 P0-1 — the manifest must authenticate the very file (and bytes) being scored.
# --------------------------------------------------------------------------------------------


def test_an_altered_copy_of_a_frozen_set_is_not_verified_by_the_originals_entry(
    corpus: Path, tmp_path: Path
) -> None:
    original = _frozen(corpus, tmp_path)
    assert truth_is_frozen_verified(original, [_record(original)]) is True  # the control

    copy_dir = corpus / "copy-set"
    shutil.copytree(original.parent, copy_dir)
    payload = json.loads((copy_dir / "ground_truth.json").read_text("utf-8"))
    payload["episodes"][0]["work"]["title"] = "Silently Altered Title"
    (copy_dir / "ground_truth.json").write_text(json.dumps(payload), encoding="utf-8")
    copy = copy_dir / "ground_truth.json"

    with pytest.raises(ValueError):
        truth_is_frozen_verified(copy, [_record(copy)])
    with pytest.raises(ValueError):
        score_corpus.truth_status(copy, _record(copy))


def test_a_linked_freeze_manifest_is_refused(corpus: Path, tmp_path: Path) -> None:
    original = _frozen(corpus, tmp_path)
    moved = tmp_path / "moved-manifest.json"
    shutil.move(str(corpus / "corpus-version.json"), str(moved))
    _symlink(corpus / "corpus-version.json", moved)
    with pytest.raises(ValueError):
        truth_is_frozen_verified(original, [_record(original)])


def test_a_set_directory_junction_escaping_the_corpus_is_refused(
    corpus: Path, tmp_path: Path
) -> None:
    original = _frozen(corpus, tmp_path)
    record = _record(original)
    elsewhere = tmp_path / "elsewhere-set"
    shutil.move(str(original.parent), str(elsewhere))
    _junction(original.parent, elsewhere)  # identical bytes, reached through a reparse point
    with pytest.raises(ValueError):
        truth_is_frozen_verified(original, [record])


# --------------------------------------------------------------------------------------------
# r2 P0-2 — exposure deleted while predictions are assembled is reasserted before returning.
# --------------------------------------------------------------------------------------------


def test_exposure_deleted_during_prediction_assembly_is_restored_and_blocks_certification(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, truth = _truth(corpus)

    def assemble_while_an_unlocked_actor_deletes(media_dir: object) -> list[dict[str, object]]:
        os.remove(exposure_path(truth_path))  # ignores the advisory lock
        return [{"artist": "A", "title": "T", "start_ms": 0, "end_ms": 1, "tier": "likely"}]

    monkeypatch.setattr(review_module, "_predictions", assemble_while_an_unlocked_actor_deletes)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    assert session.reveal_predictions()
    assert exposure_path(truth_path).exists()

    # Restart, finish the pass, freeze and try to certify: the set must read as exposed.
    reopened = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    assert reopened.predictions_visible is True
    reopened.save({"rows": _rows(truth)})
    manifest = freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    assert manifest["exposed_sets"] == [truth.set_id]
    frozen = _record(truth_path)
    assert score_corpus.truth_independent(truth_path, frozen) is False
    assert corpus_independent(corpus, [frozen]) is False


@pytest.mark.skipif(not WINDOWS, reason="deletion-denying handles are a Windows sharing mode")
def test_exposure_cannot_be_deleted_while_the_reveal_returns(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, _ = _truth(corpus)
    monkeypatch.setattr(review_module, "_predictions", lambda media_dir: [])
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    attempts: list[str] = []

    def unlocked_actor() -> None:
        try:
            os.remove(exposure_path(truth_path))
            attempts.append("deleted")
        except PermissionError:
            attempts.append("denied")

    session._evidence_held = unlocked_actor  # type: ignore[method-assign]
    session.reveal_predictions()
    assert attempts == ["denied"]
    assert exposure_path(truth_path).exists()


def test_reveal_fails_closed_when_the_evidence_cannot_be_reasserted(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, _ = _truth(corpus)
    real_write = review_module._write_exposure
    writes: list[int] = []

    def write_once_then_fail(pinned: object, **kwargs: object) -> None:
        writes.append(1)
        if len(writes) > 1:
            raise OSError("injected: evidence cannot be rewritten")
        real_write(pinned, **kwargs)

    def assemble_and_delete(media_dir: object) -> list[dict[str, object]]:
        os.remove(exposure_path(truth_path))
        return [{"artist": "Secret", "title": "Guess", "start_ms": 0, "end_ms": 1, "tier": "x"}]

    monkeypatch.setattr(review_module, "_write_exposure", write_once_then_fail)
    monkeypatch.setattr(review_module, "_predictions", assemble_and_delete)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    with pytest.raises((OSError, ValueError)):
        session.reveal_predictions()


# --------------------------------------------------------------------------------------------
# r2 P0-3 — the identity validated at planning time is the identity pinned and written.
# --------------------------------------------------------------------------------------------


def _swap_after_planning(
    monkeypatch: pytest.MonkeyPatch, set_dir: Path, parked: Path, target: Path
) -> None:
    real_plan = truth_module._freeze_plan

    def plan_then_swap(*args: object, **kwargs: object) -> object:
        planned = real_plan(*args, **kwargs)
        shutil.move(str(set_dir), str(parked))
        _junction(set_dir, target)
        return planned

    monkeypatch.setattr(truth_module, "_freeze_plan", plan_then_swap)


def test_freeze_refuses_a_set_swapped_for_a_junction_into_another_set_after_planning(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verify_set(corpus / "fixture-set", tmp_path)
    # The other corpus sits beside this one, and the manifest goes in their shared parent: the
    # manifest window then still covers the swapped-in set, so only identity pinning can refuse it.
    other = corpus.parent / "other-corpus" / "other-set"
    shutil.copytree(FIXTURE / "fixture-set", other)
    other_truth = _verify_set(other, tmp_path)
    before = other_truth.read_bytes()
    _swap_after_planning(monkeypatch, corpus / "fixture-set", tmp_path / "parked", other)
    with pytest.raises(ValueError):
        freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus.parent / "corpus-version.json")
    assert other_truth.read_bytes() == before
    assert not (corpus.parent / "corpus-version.json").exists()


def test_freeze_refuses_a_set_swapped_for_a_junction_into_work_after_planning(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verify_set(corpus / "fixture-set", tmp_path)
    inside_work = tmp_path / "work" / "stolen-set"
    shutil.copytree(FIXTURE / "fixture-set", inside_work)
    before = (inside_work / "ground_truth.json").read_bytes()
    _swap_after_planning(monkeypatch, corpus / "fixture-set", tmp_path / "parked", inside_work)
    with pytest.raises(ValueError):
        freeze_truth(
            corpus,
            corpus_version="fx-v1",
            out_path=corpus / "corpus-version.json",
            work_root=tmp_path / "work",
        )
    assert (inside_work / "ground_truth.json").read_bytes() == before


def test_a_rejected_seed_creates_no_directories_beneath_work(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text("00:10 Artist One - Track One\n", encoding="utf-8")
    with pytest.raises(ValueError):
        seed_truth(
            out_path=tmp_path / "work" / "new-corpus" / "set" / "ground_truth.json",
            set_id="work-seed",
            duration_ms=60_000,
            media_key="d" * 64,
            tracklist=tracklist,
            project_root=tmp_path / "project",
            work_root=tmp_path / "work",
        )
    assert not (tmp_path / "work" / "new-corpus").exists()


def test_seed_refuses_a_pre_existing_ancestor_link(tmp_path: Path) -> None:
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text("00:10 Artist One - Track One\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    _junction(corpus_dir / "linked-set", elsewhere)
    with pytest.raises(ValueError, match="link"):
        seed_truth(
            out_path=corpus_dir / "linked-set" / "ground_truth.json",
            set_id="linked-ancestor",
            duration_ms=60_000,
            media_key="e" * 64,
            tracklist=tracklist,
            project_root=tmp_path / "project",
        )
    assert list(elsewhere.iterdir()) == []


# --------------------------------------------------------------------------------------------
# r2 P1-4 — every accepted manifest placement verifies end to end.  Round 3 (R-P1-3) narrowed the
# accepted placements to the corpus directory alone; the other two are now refused, and
# `tests/test_truth_corpus_followup_r3.py` covers them through directory-based CLI scoring.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("placement", ["set-directory", "corpus-directory", "directory-above"])
def test_every_accepted_manifest_placement_verifies_through_review_and_scoring(
    corpus: Path, tmp_path: Path, placement: str
) -> None:
    truth_path = _verify_set(corpus / "fixture-set", tmp_path)
    out_dir = {
        "set-directory": corpus / "fixture-set",
        "corpus-directory": corpus,
        "directory-above": corpus.parent,
    }[placement]
    if placement != "corpus-directory":
        with pytest.raises(ValueError, match="manifest"):
            freeze_truth(corpus, corpus_version="fx-v1", out_path=out_dir / "corpus-version.json")
        return
    freeze_truth(corpus, corpus_version="fx-v1", out_path=out_dir / "corpus-version.json")
    frozen = _record(truth_path)
    assert truth_is_frozen_verified(truth_path, [frozen]) is True
    assert score_corpus.truth_status(truth_path, frozen) == "verified"
    with pytest.raises(ValueError, match="frozen"):
        TruthReviewSession(truth_path, work_root=tmp_path / "work")


# --------------------------------------------------------------------------------------------
# r2 P1-5 — the lock deadline covers competing threads, and release survives an unlock failure.
# --------------------------------------------------------------------------------------------


def _hold_in_thread(path: Path) -> tuple[threading.Event, threading.Event, threading.Thread]:
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with truth_write_lock(path):
            held.set()
            release.wait(60)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(10)
    return held, release, holder


def test_a_competing_thread_times_out_instead_of_hanging(corpus: Path) -> None:
    truth_path, _ = _truth(corpus)
    _, release, holder = _hold_in_thread(truth_path)
    try:

        def contend() -> None:
            with truth_write_lock(truth_path, timeout=0.3):
                pass

        outcome = _in_thread(contend, timeout=10)
    finally:
        release.set()
        holder.join(10)
    assert outcome["finished"] is True, "the in-process lock ignored its deadline"
    assert isinstance(outcome.get("error"), ValueError)


def test_a_timed_out_freeze_releases_the_records_it_had_already_locked(
    corpus: Path, tmp_path: Path
) -> None:
    first = _verify_set(corpus / "fixture-set", tmp_path)
    other = corpus / "other-set"
    shutil.copytree(FIXTURE / "fixture-set", other)
    payload = json.loads((other / "ground_truth.json").read_text("utf-8"))
    payload["set_id"] = "fixture-other-set"
    (other / "ground_truth.json").write_text(json.dumps(payload), encoding="utf-8")
    second = _verify_set(other, tmp_path)
    assert truth_module.path_key(first) < truth_module.path_key(second)  # locked in this order
    _, release, holder = _hold_in_thread(second)
    try:
        outcome = _in_thread(
            lambda: freeze_truth(
                corpus,
                corpus_version="fx-v1",
                out_path=corpus / "corpus-version.json",
                lock_timeout=0.3,
            ),
            timeout=10,
        )
        assert outcome["finished"] is True, "freeze hung on a thread-held record lock"
        assert isinstance(outcome.get("error"), ValueError)
    finally:
        release.set()
        holder.join(10)
    # Round 4: a record lock is always taken under its corpus lock, so the holder above also held
    # the corpus lock, and nothing in this corpus is free until it releases.  Once it has, the timed
    # out freeze must have left nothing held behind: another thread takes the lock at once.
    free = _in_thread(lambda: truth_write_lock(first, timeout=0.5).__enter__(), timeout=10)
    assert free["finished"] is True and "error" not in free


@pytest.mark.skipif(not WINDOWS, reason="injects the failure at msvcrt's unlock")
def test_an_unlock_failure_still_releases_the_in_process_lock(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msvcrt

    truth_path, _ = _truth(corpus)
    real_locking = msvcrt.locking

    def failing_unlock(fd: int, mode: int, nbytes: int) -> None:
        if mode == msvcrt.LK_UNLCK:
            raise OSError("injected unlock failure")
        real_locking(fd, mode, nbytes)

    monkeypatch.setattr(msvcrt, "locking", failing_unlock)
    with pytest.raises(OSError, match="injected unlock failure"), truth_write_lock(truth_path):
        pass
    monkeypatch.undo()

    def contend() -> None:
        with truth_write_lock(truth_path, timeout=1.0):
            pass

    outcome = _in_thread(contend, timeout=10)
    assert outcome["finished"] is True, "the in-process lock leaked after the unlock failure"
    assert "error" not in outcome


# --------------------------------------------------------------------------------------------
# r2 P1-6 — freeze refuses a corpus that is already frozen.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["fx-v1", "fx-v2"], ids=["same-version", "other-version"])
def test_freeze_refuses_an_already_frozen_corpus(
    corpus: Path, tmp_path: Path, version: str
) -> None:
    truth_path = _frozen(corpus, tmp_path)
    truth_before = truth_path.read_bytes()
    manifest_before = (corpus / "corpus-version.json").read_bytes()
    with pytest.raises(ValueError, match="frozen"):
        freeze_truth(corpus, corpus_version=version, out_path=corpus / "corpus-version.json")
    assert truth_path.read_bytes() == truth_before
    assert (corpus / "corpus-version.json").read_bytes() == manifest_before


# Round 9: these tests exercise freezing and certification underneath the owner's moratorium, so the
# single gate is opened for them by fixture (production keeps it closed).  Tests that assert the
# moratorium itself close it again explicitly.
pytestmark = pytest.mark.usefixtures("certification_gate_open")
