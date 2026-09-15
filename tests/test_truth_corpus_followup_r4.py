"""Regressions for the round-4 (sol xhigh) review: the class-closing fix pass.

Two design changes close the class of interleaving bugs: one corpus-wide mutation lock taken by
every state-changing operation, and one supported layout (``<corpus>/<set>/ground_truth.json``).
The two-process tests use explicit stdin/stdout barriers, never sleeps.  Nothing touches
``data/corpus/``.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

import id_detector.benchmark.scorer as scorer_module
import id_detector.calibrate.certify as certify_module
import id_detector.truth_review as review_module
from id_detector import cli
from id_detector.benchmark.scorer import (
    corpus_independent,
    load_truth_directory,
    load_truth_files,
    score_corpus,
    truth_is_frozen_verified,
)
from id_detector.calibrate.certify import (
    CorpusNotIndependent,
    _require_frozen,
    _require_independent,
)
from id_detector.contracts import GroundTruthRecord
from id_detector.io import atomic_write_json, read_text
from id_detector.truth import (
    freeze_truth,
    ledger_exposure,
    record_exposure_in_ledger,
    second_pass_truth,
    seed_truth,
    write_draft_manifest,
)
from id_detector.truth_review import TruthReviewSession, exposure_path, find_truth_path
from tests.test_score_corpus import _copy_fixture, _freeze, _mark_verified, _run_time
from tests.test_truth_corpus_followup_r3 import _junction
from tests.test_truth_corpus_followup_sol import _certifiable_corpus
from tests.test_truth_review import _reveal_stub, _rows

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "truth-review"
GUIDANCE = "Pass the resolved real path instead"
LAYOUT = re.escape("<corpus>/<set>/ground_truth.json")
LEDGER = "review-exposure-ledger.jsonl"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    target = tmp_path / "root" / "corpus"
    shutil.copytree(FIXTURE, target)
    return target


def _record(path: Path) -> GroundTruthRecord:
    return GroundTruthRecord.model_validate_json(read_text(path))


def _truth_path(corpus: Path) -> Path:
    return corpus / "fixture-set" / "ground_truth.json"


def _verify(truth_path: Path, tmp_path: Path) -> None:
    TruthReviewSession(truth_path, work_root=tmp_path / "work").save(
        {"rows": _rows(_record(truth_path))}
    )


def _tracklist(tmp_path: Path) -> Path:
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text("00:10 Artist One - Track One\n", encoding="utf-8")
    return tracklist


def _child(script: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args, str(ROOT / "src")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _release(child: subprocess.Popen) -> tuple[str, str]:
    try:
        assert child.stdin is not None
        child.stdin.write("go\n")
        child.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    return child.communicate(timeout=120)


# --------------------------------------------------------------------------------------------
# P0-2 (class) — one corpus mutation lock serialises every state-changing operation.
# --------------------------------------------------------------------------------------------

_SEED_PAUSED_AFTER_PREFLIGHT = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[-1])
import id_detector.truth as truth
real = truth._seed_entries
def paused(*args, **kwargs):
    print("preflighted", flush=True)
    if sys.stdin.readline().strip() != "go":
        raise SystemExit(3)
    return real(*args, **kwargs)
truth._seed_entries = paused
truth.seed_truth(out_path=Path(sys.argv[1]), set_id="new-set", duration_ms=60000,
                 media_key="f" * 64, tracklist=Path(sys.argv[2]), project_root=Path(sys.argv[3]))
print("seeded", flush=True)
"""


def test_the_corpus_lock_serialises_seed_against_a_concurrent_reveal(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path = _truth_path(corpus)
    _reveal_stub(monkeypatch)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    child = _child(
        _SEED_PAUSED_AFTER_PREFLIGHT,
        str(corpus / "new-set" / "ground_truth.json"),
        str(_tracklist(tmp_path)),
        str(tmp_path / "project"),
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "preflighted"  # barrier: seed is mid-operation
        monkeypatch.setattr(review_module, "_SAVE_LOCK_TIMEOUT", 1.0)
        with pytest.raises(ValueError, match="another truth writer"):
            session.reveal_predictions()
        assert not exposure_path(truth_path).exists()
    finally:
        out, err = _release(child)
    assert child.returncode == 0, err
    assert "seeded" in out
    session.reveal_predictions()  # the seed has finished: the corpus is free again
    assert exposure_path(truth_path).exists()


_DRAFT_PAUSED_BEFORE_WRITE = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[-1])
import id_detector.truth as truth
real = truth._write_record_file
def paused(*args, **kwargs):
    print("validated", flush=True)
    if sys.stdin.readline().strip() != "go":
        raise SystemExit(3)
    return real(*args, **kwargs)
truth._write_record_file = paused
truth.write_draft_manifest(Path(sys.argv[1]), corpus_version="draft")
print("drafted", flush=True)
"""


def test_the_corpus_lock_serialises_draft_manifest_against_a_concurrent_freeze(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path = _truth_path(corpus)
    manifest_path = corpus / "corpus-version.json"
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    child = _child(_DRAFT_PAUSED_BEFORE_WRITE, str(corpus), str(manifest_path))
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "validated"  # barrier: draft is mid-operation
        monkeypatch.setattr(review_module, "_SAVE_LOCK_TIMEOUT", 1.0)
        with pytest.raises(ValueError, match="another truth writer"):
            session.save({"rows": _rows(_record(truth_path))})
        with pytest.raises(ValueError, match="another truth writer"):
            freeze_truth(corpus, corpus_version="fx-v1", out_path=manifest_path, lock_timeout=1.0)
    finally:
        out, err = _release(child)
    assert child.returncode == 0, err
    assert json.loads(manifest_path.read_text("utf-8"))["frozen"] is False
    # Only after the draft inventory is durable can the corpus be verified and frozen -- and the
    # freeze is never overwritten by a stale draft.
    _verify(truth_path, tmp_path)
    freeze_truth(corpus, corpus_version="fx-v1", out_path=manifest_path)
    assert json.loads(manifest_path.read_text("utf-8"))["frozen"] is True


# The barrier is INSIDE the append.  Each child installs two test seams in truth.py: the
# ledger-append window (after the append's own read of the ledger, before its write) prints
# "read" and waits for "go"; the lock-contention seam prints "blocked" the first time the
# corpus lock is already held by the other process.  The parent waits until one child has read
# and the other has either also read
# -- no serialisation: both hold the same pre-append state, so releasing both makes one write
# clobber the other -- or reported itself blocked on the lock (serialised).  Only then does it
# release.  Deterministic both ways, never a sleep: with the corpus lock removed it fails every
# time.
_APPEND_WITH_SEAMS = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[-1])
import id_detector.truth as truth
def window(corpus_dir):
    print("read", flush=True)
    if sys.stdin.readline().strip() != "go":
        raise SystemExit(3)
def contended(key):
    print("blocked", flush=True)
truth._ledger_append_window = window
truth._lock_contended = contended
truth.record_exposure_in_ledger(Path(sys.argv[1]), set_id=sys.argv[2], when="2026-09-14T00:00:00Z")
print("appended", flush=True)
"""


def test_two_processes_appending_for_two_sets_both_survive_in_the_ledger(
    corpus: Path,
) -> None:
    other = corpus / "other-set"
    shutil.copytree(corpus / "fixture-set", other)
    children = [
        _child(_APPEND_WITH_SEAMS, str(corpus / "fixture-set" / "ground_truth.json"), "set-alpha"),
        _child(_APPEND_WITH_SEAMS, str(other / "ground_truth.json"), "set-beta"),
    ]
    events: queue.Queue[tuple[int, str]] = queue.Queue()

    def pump(index: int, child: subprocess.Popen) -> None:
        assert child.stdout is not None
        for line in child.stdout:
            events.put((index, line.strip()))
        events.put((index, "<eof>"))

    for index, child in enumerate(children):
        threading.Thread(target=pump, args=(index, child), daemon=True).start()
    seen: dict[int, list[str]] = {0: [], 1: []}
    released: set[int] = set()

    def wait_until(condition: Callable[[], bool]) -> None:
        while not condition():
            index, line = events.get(timeout=120)
            if line == "<eof>":
                if index in released:
                    continue  # a released child finishing normally
                child = children[index]
                child.wait(timeout=120)
                error = child.stderr.read() if child.stderr is not None else ""
                pytest.fail(f"child {index} exited before the barrier: {seen[index]} {error}")
            seen[index].append(line)

    def go(index: int) -> None:
        released.add(index)
        child = children[index]
        assert child.stdin is not None
        child.stdin.write("go\n")
        child.stdin.flush()

    try:
        # Barrier 1: one child has read the ledger inside the append and is paused before writing.
        wait_until(lambda: "read" in seen[0] or "read" in seen[1])
        first = 0 if "read" in seen[0] else 1
        second = 1 - first
        # Barrier 2: the other child has also read (not serialised) or is blocked on the lock.
        wait_until(lambda: "read" in seen[second] or "blocked" in seen[second])
        if "read" in seen[second]:
            go(first)  # both hold the empty pre-append state: release both together
            go(second)
        else:
            go(first)  # serialised: the second reads only after the first has written
            wait_until(lambda: "read" in seen[second])
            go(second)
        codes = [child.wait(timeout=120) for child in children]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=120)
    errors = [child.stderr.read() if child.stderr is not None else "" for child in children]
    assert codes == [0, 0], errors
    entries = [
        json.loads(line)
        for line in (corpus / LEDGER).read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert sorted(entry["set_id"] for entry in entries) == ["set-alpha", "set-beta"]


# --------------------------------------------------------------------------------------------
# P0-1 (class) — one supported layout, refused everywhere else, with the layout in the message.
# --------------------------------------------------------------------------------------------


@pytest.fixture
def single_set_root(tmp_path: Path) -> Path:
    target = tmp_path / "single-set-root"
    shutil.copytree(FIXTURE / "fixture-set", target)
    return target


def test_freeze_refuses_a_single_set_root(single_set_root: Path) -> None:
    with pytest.raises(ValueError, match=LAYOUT):
        freeze_truth(
            single_set_root,
            corpus_version="fx-v1",
            out_path=single_set_root / "corpus-version.json",
        )
    assert not (single_set_root / "corpus-version.json").exists()


def test_draft_manifest_refuses_a_single_set_root(single_set_root: Path) -> None:
    with pytest.raises(ValueError, match=LAYOUT):
        write_draft_manifest(single_set_root, corpus_version="draft")
    assert not (single_set_root / "corpus-version.json").exists()


def test_review_refuses_a_single_set_root(single_set_root: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=LAYOUT):
        find_truth_path(single_set_root, "fixture-truth-review", work_root=tmp_path / "work")


def test_scoring_refuses_a_single_set_root(single_set_root: Path) -> None:
    with pytest.raises(ValueError, match=LAYOUT):
        load_truth_directory(single_set_root)


def test_certification_refuses_a_single_set_root(single_set_root: Path) -> None:
    with pytest.raises(ValueError, match=LAYOUT):
        _require_frozen(single_set_root, "fx-v1")


def test_seed_refuses_to_turn_a_corpus_root_into_a_set(corpus: Path, tmp_path: Path) -> None:
    before = sorted(path.name for path in corpus.iterdir())
    with pytest.raises(ValueError, match=LAYOUT):
        seed_truth(
            out_path=corpus / "ground_truth.json",
            set_id="corpus-as-set",
            duration_ms=60_000,
            media_key="e" * 64,
            tracklist=_tracklist(tmp_path),
            project_root=tmp_path / "project",
        )
    assert sorted(path.name for path in corpus.iterdir()) == before


def test_a_copied_corpus_keeps_its_ledger_and_stays_exposed(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path = _truth_path(corpus)
    _reveal_stub(monkeypatch)
    TruthReviewSession(truth_path, work_root=tmp_path / "work").reveal_predictions()
    exposure_path(truth_path).unlink()
    copied = tmp_path / "copied-root" / "corpus"
    shutil.copytree(corpus, copied)
    copied_truth = _truth_path(copied)
    restarted = TruthReviewSession(copied_truth, work_root=tmp_path / "work")
    assert restarted.predictions_visible is True
    restarted.save({"rows": _rows(_record(copied_truth))})
    manifest = freeze_truth(copied, corpus_version="fx-v1", out_path=copied / "corpus-version.json")
    assert manifest["exposed_sets"] == ["fixture-truth-review"]


# --------------------------------------------------------------------------------------------
# P1-5 — the ledger alone produces a non-certifiable L3 report and a certification refusal; a
# tampered ledger after freeze is rejected.
# --------------------------------------------------------------------------------------------


def test_a_ledger_only_exposure_produces_a_non_certifiable_l3_report_and_refuses_certification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy_fixture(tmp_path)
    for set_id in ("mini-a", "mini-b"):
        _mark_verified(root / set_id / "ground_truth.json")
    mini_a = root / "mini-a" / "ground_truth.json"
    # Only the corpus ledger records the exposure: no sidecar, no annotation provenance.
    record_exposure_in_ledger(mini_a, set_id=_record(mini_a).set_id, when="2026-09-14T00:00:00Z")
    assert not exposure_path(mini_a).exists()
    _freeze(root, ["mini-a", "mini-b"])
    # The corpus-mini freeze helper writes its manifest by hand; record the ledger line exactly as
    # freeze_truth does, so the frozen ledger is manifest-bound (round 8 requires exact equality).
    manifest_path = root / "corpus-version.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    for entry in manifest["sets"]:
        if entry["set_id"] == _record(mini_a).set_id:
            entry["prediction_exposure"] = {
                "predictions_visible_during_review": True,
                "evidence": {},
                "ledger_entries": ledger_exposure(mini_a),
            }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    code, document = _run_time(tmp_path / "scored", root / "run-list.json")
    assert code == 0 and document is not None
    assert document["truth_status"] == "verified"
    assert document["l3"]["independent"] is False
    assert document["l3"]["certifiable"] is False
    assert "NOT CERTIFIABLE" in capsys.readouterr().out
    with pytest.raises(CorpusNotIndependent):
        _require_independent(root, "corpus-mini")

    truth_root, predictions = _certifiable_corpus(tmp_path / "generic")
    exposed = truth_root / "test-set-03" / "ground_truth.json"
    # Round 8: a frozen corpus refuses the append outright, and a line added to its ledger by
    # hand is not manifest-bound, so verification -- and with it certification -- is refused.
    ledger = truth_root / LEDGER
    with pytest.raises(ValueError, match="frozen"):
        record_exposure_in_ledger(exposed, set_id="test-set-03", when="2026-09-14T00:00:00Z")
    assert not ledger.exists()
    line = {"event": "predictions_revealed", "set_id": "test-set-03"}
    ledger.write_text(json.dumps(line) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not exactly equal"):
        score_corpus(truth_root, predictions)


@pytest.mark.parametrize("tamper", ["delete", "truncate", "rewrite"])
def test_tampering_with_the_ledger_after_freeze_is_rejected(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    truth_path = _truth_path(corpus)
    _reveal_stub(monkeypatch)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    session.reveal_predictions()
    session.save({"rows": _rows(_record(truth_path))})
    freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    frozen = _record(truth_path)
    assert truth_is_frozen_verified(truth_path, [frozen]) is True  # the control
    ledger = corpus / LEDGER
    if tamper == "delete":
        ledger.unlink()
    elif tamper == "truncate":
        ledger.write_bytes(b"")
    else:
        ledger.write_text(
            ledger.read_text("utf-8").replace("fixture-truth-review", "someone-else"),
            encoding="utf-8",
        )
    with pytest.raises(ValueError):
        truth_is_frozen_verified(truth_path, [frozen])


# --------------------------------------------------------------------------------------------
# P1-3 — link components are refused BEFORE any resolve, enumeration or read, with the guidance.
# --------------------------------------------------------------------------------------------


def _linked_corpus(corpus: Path, tmp_path: Path) -> Path:
    linked = tmp_path / "linked-corpus"
    _junction(linked, corpus)
    return linked


def _touching(calls: list[str], linked: Path) -> list[str]:
    return [call for call in calls if call.lower().startswith(str(linked).lower())]


def test_review_find_refuses_a_linked_corpus_before_resolving_or_enumerating(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked = _linked_corpus(corpus, tmp_path)
    calls: list[str] = []
    real_real_path = review_module.real_path
    real_rglob = Path.rglob
    monkeypatch.setattr(
        review_module, "real_path", lambda path: (calls.append(str(path)), real_real_path(path))[1]
    )
    monkeypatch.setattr(
        Path, "rglob", lambda self, pattern: (calls.append(str(self)), real_rglob(self, pattern))[1]
    )
    with pytest.raises(ValueError, match=GUIDANCE):
        find_truth_path(linked, "fixture-truth-review", work_root=tmp_path / "work")
    assert _touching(calls, linked) == []


def test_review_session_refuses_a_linked_record_before_resolving_or_reading(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked = _linked_corpus(corpus, tmp_path)
    calls: list[str] = []
    real_real_path = review_module.real_path
    real_read_text = review_module.read_text
    monkeypatch.setattr(
        review_module, "real_path", lambda path: (calls.append(str(path)), real_real_path(path))[1]
    )
    monkeypatch.setattr(
        review_module, "read_text", lambda path: (calls.append(str(path)), real_read_text(path))[1]
    )
    with pytest.raises(ValueError, match=GUIDANCE):
        TruthReviewSession(_truth_path(linked), work_root=tmp_path / "work")
    assert _touching(calls, linked) == []


@pytest.mark.parametrize("entry", ["load", "independence"])
def test_scoring_refuses_a_linked_corpus_before_resolving(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    linked = _linked_corpus(corpus, tmp_path)
    calls: list[str] = []
    real_resolve = Path.resolve
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, strict=False: (calls.append(str(self)), real_resolve(self, strict))[1],
    )
    with pytest.raises(ValueError, match=GUIDANCE):
        if entry == "load":
            load_truth_files(linked)
        else:
            corpus_independent(linked, [])
    monkeypatch.undo()
    assert _touching(calls, linked) == []
    assert scorer_module.load_truth_files is load_truth_files


def test_certification_refuses_a_linked_corpus_before_reading(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verify(_truth_path(corpus), tmp_path)
    freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    linked = _linked_corpus(corpus, tmp_path)
    calls: list[str] = []
    real_read_text = certify_module.read_text
    monkeypatch.setattr(
        certify_module, "read_text", lambda path: (calls.append(str(path)), real_read_text(path))[1]
    )
    with pytest.raises(ValueError, match=GUIDANCE):
        _require_frozen(linked, "fx-v1")
    with pytest.raises(ValueError, match=GUIDANCE):
        _require_independent(linked, "fx-v1")
    assert _touching(calls, linked) == []


def test_draft_manifest_refuses_a_linked_corpus_before_enumerating(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked = _linked_corpus(corpus, tmp_path)
    calls: list[str] = []
    real_scandir = os.scandir
    monkeypatch.setattr(
        os, "scandir", lambda path: (calls.append(str(path)), real_scandir(path))[1]
    )
    with pytest.raises(ValueError, match=GUIDANCE):
        write_draft_manifest(linked, corpus_version="draft")
    assert _touching(calls, linked) == []


def test_a_linked_annotation_pass_is_refused_with_the_guidance(
    corpus: Path, tmp_path: Path
) -> None:
    truth_path = _truth_path(corpus)
    _verify(truth_path, tmp_path)
    first = truth_path.with_name("annotation-first.json")
    moved = tmp_path / "moved-first.json"
    shutil.move(str(first), str(moved))
    try:
        first.symlink_to(moved)
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("this platform/account cannot create symlinks")
    second = tmp_path / "second.json"
    atomic_write_json(second, json.loads(read_text(truth_path)))
    with pytest.raises(ValueError, match=GUIDANCE):
        second_pass_truth(truth_path, annotator_ref="second-annotator", annotation_path=second)
    with pytest.raises(ValueError, match=GUIDANCE):
        TruthReviewSession(truth_path, work_root=tmp_path / "work")


def test_a_set_moved_or_replaced_after_checking_is_refused_with_the_guidance(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import id_detector.truth as truth_module

    _verify(_truth_path(corpus), tmp_path)
    other = tmp_path / "root" / "other-corpus" / "other-set"
    shutil.copytree(FIXTURE / "fixture-set", other)
    real_plan = truth_module._freeze_plan

    def plan_then_swap(*args: object, **kwargs: object) -> object:
        planned = real_plan(*args, **kwargs)
        # Parked one level down: a set directly under tmp_path would make tmp_path itself a corpus,
        # and the ancestor refusal would fire before the pinned-identity check this test targets.
        shutil.move(str(corpus / "fixture-set"), str(tmp_path / "parked" / "fixture-set"))
        _junction(corpus / "fixture-set", other)
        return planned

    monkeypatch.setattr(truth_module, "_freeze_plan", plan_then_swap)
    with pytest.raises(ValueError, match=GUIDANCE):
        freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")


# --------------------------------------------------------------------------------------------
# P1-4 — the freeze help describes only <corpus>/corpus-version.json.
# --------------------------------------------------------------------------------------------


def test_freeze_help_describes_only_the_corpus_root_manifest() -> None:
    result = CliRunner().invoke(cli.app, ["truth", "freeze", "--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "<corpus>/corpus-version.json" in text
    assert "directory above" not in text
    assert "set directory" not in text


# Round 9: these tests exercise freezing and certification underneath the owner's moratorium, so the
# single gate is opened for them by fixture (production keeps it closed).  Tests that assert the
# moratorium itself close it again explicitly.
pytestmark = pytest.mark.usefixtures("certification_gate_open")
