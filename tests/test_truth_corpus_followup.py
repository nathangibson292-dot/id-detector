"""Regressions for the sol-xhigh retro-review of the truth-review tool and the corpus audit.

Every test here was first run against the unfixed code (``ec32f97``) and failed there; see
``docs/reviews/followup-truth-corpus.md``.  Nothing touches ``data/corpus/``: each test works on a
temporary copy of ``tests/fixtures/truth-review/`` or of the scorer's ``corpus-mini`` fixture.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import id_detector.truth as truth_module
from id_detector.contracts import GroundTruthRecord
from id_detector.io import read_text, sha256_file
from id_detector.truth import _annotation_path, freeze_truth
from id_detector.truth_review import (
    TruthReviewSession,
    exposure_path,
    find_truth_path,
    preview_bulk_offset,
)
from scripts import audit_fixtures
from tests.test_score_corpus import _copy_fixture, _freeze, _mark_verified, _run_time
from tests.test_truth_review import _reveal_stub, _rows, _run_page_js, _truth

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "truth-review"
WINDOWS = os.name == "nt"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    target = tmp_path / "corpus"
    shutil.copytree(FIXTURE, target)
    return target


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - no symlink privilege
        pytest.skip("this platform/account cannot create symlinks")


def _junction(link: Path, target: Path) -> None:
    if not WINDOWS:  # pragma: no cover - junctions are a Windows reparse point
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


def _extended(path: Path) -> Path:
    return Path("\\\\?\\" + str(path.resolve()))


def _exposure_payload(set_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "set_id": set_id,
        "predictions_visible_during_review": True,
        "first_revealed_at_utc": "2026-09-14T12:00:00Z",
        "tool": "idea truth review",
    }


def _saved_and_frozen(corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reveal predictions, save the first pass, then freeze the (dev-1) fixture set."""

    truth_path, truth = _truth(corpus)
    _reveal_stub(monkeypatch)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    session.reveal_predictions()
    session.save({"rows": _rows(truth)})
    freeze_truth(
        corpus, corpus_version="fixture-frozen-v1", out_path=corpus / "corpus-version.json"
    )
    return truth_path


# --------------------------------------------------------------------------------------------
# B2 (P0) — a prediction-exposed set must never read as independent, certifiable truth.
# --------------------------------------------------------------------------------------------


def test_exposed_set_is_never_certifiable_through_the_l3_scorer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy_fixture(tmp_path)
    for set_id in ("mini-a", "mini-b"):
        _mark_verified(root / set_id / "ground_truth.json")
    truth = json.loads((root / "mini-a" / "ground_truth.json").read_text("utf-8"))
    (root / "mini-a" / "review-exposure.json").write_text(
        json.dumps(_exposure_payload(truth["set_id"])), encoding="utf-8"
    )
    _freeze(root, ["mini-a", "mini-b"])
    code, document = _run_time(tmp_path / "scored", root / "run-list.json")
    assert code == 0 and document is not None
    # Scoring still runs and the truth is still frozen and hash-checked ...
    assert document["truth_status"] == "verified"
    # ... but it is not independent of IDea's predictions, so it can back no L3 claim.
    assert document["l3"]["certifiable"] is False
    assert document["l3"]["independent"] is False
    by_set = {mix["mix_id"]: mix for mix in document["mixes"]}
    assert [mix["independent"] for mix in document["mixes"]].count(False) == 1
    assert any(mix["independent"] is False for mix in by_set.values())
    assert "not independent" in capsys.readouterr().out


def test_freeze_records_and_hashes_the_exposure_evidence(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path = _saved_and_frozen(corpus, tmp_path, monkeypatch)
    manifest = json.loads((corpus / "corpus-version.json").read_text("utf-8"))
    entry = manifest["sets"][0]
    exposure = entry["prediction_exposure"]
    assert exposure["predictions_visible_during_review"] is True
    assert exposure["certifiable"] is False
    assert exposure["evidence"] == {
        "annotation-first.json": sha256_file(_annotation_path(truth_path, "first")),
        "review-exposure.json": sha256_file(exposure_path(truth_path)),
    }
    assert manifest["exposed_sets"] == [entry["set_id"]]

    # Removing the evidence after the freeze is detected, not silently accepted.
    from id_detector.benchmark.scorer import truth_is_frozen_verified

    frozen = GroundTruthRecord.model_validate_json(read_text(truth_path))
    exposure_path(truth_path).unlink()
    with pytest.raises(ValueError, match="exposure evidence"):
        truth_is_frozen_verified(truth_path, [frozen])


def test_certification_refuses_a_prediction_exposed_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from id_detector.calibrate.certify import run_certify

    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", False)  # the moratorium

    corpus_dir = tmp_path / "data" / "corpus" / "exposed-v1"
    shutil.copytree(FIXTURE / "fixture-set", corpus_dir / "fixture-set")
    truth_path = corpus_dir / "fixture-set" / "ground_truth.json"
    (corpus_dir / "fixture-set" / "review-exposure.json").write_text(
        json.dumps(_exposure_payload("fixture-truth-review")), encoding="utf-8"
    )
    (corpus_dir / "corpus-version.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "generated_by": "test",
                "corpus_version": "exposed-v1",
                "frozen": True,
                "sets": [
                    {
                        "set_id": "fixture-truth-review",
                        "path": "fixture-set/ground_truth.json",
                        "sha256": sha256_file(truth_path),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    from id_detector.calibrate.certify import _require_independent

    # The independence guard still refuses the exposed corpus...
    with pytest.raises(ValueError, match="not independent"):
        _require_independent(corpus_dir, "exposed-v1")
    # ...and the entry point is refused outright until the certification follow-up lands.
    with pytest.raises(ValueError, match="certification is disabled"):
        asyncio.run(
            run_certify(
                corpus_version="exposed-v1",
                profile="free",
                test_version="v1",
                project_root=tmp_path,
                work_root=tmp_path / "work",
            )
        )


# --------------------------------------------------------------------------------------------
# B3 (P0) — every destination is checked after link resolution, at the moment it is written.
# --------------------------------------------------------------------------------------------


def test_exposure_sidecar_linked_into_work_is_refused(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, _ = _truth(corpus)
    prunable = tmp_path / "work" / "media" / "prunable.json"
    prunable.parent.mkdir(parents=True)
    prunable.write_text("{}", encoding="utf-8")
    _symlink(exposure_path(truth_path), prunable)
    _reveal_stub(monkeypatch)
    with pytest.raises(ValueError, match="link"):
        TruthReviewSession(truth_path, work_root=tmp_path / "work").reveal_predictions()
    assert prunable.read_text("utf-8") == "{}"


def test_annotation_linked_into_work_is_refused(corpus: Path, tmp_path: Path) -> None:
    truth_path, truth = _truth(corpus)
    original = truth_path.read_bytes()
    elsewhere = tmp_path / "work" / "media" / "annotation.json"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("{}", encoding="utf-8")
    _symlink(_annotation_path(truth_path, "first"), elsewhere)
    with pytest.raises(ValueError, match="link"):
        TruthReviewSession(truth_path, work_root=tmp_path / "work").save({"rows": _rows(truth)})
    assert elsewhere.read_text("utf-8") == "{}"
    assert truth_path.read_bytes() == original


def test_sibling_replaced_by_a_link_after_opening_is_refused_at_write_time(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, _ = _truth(corpus)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    _symlink(exposure_path(truth_path), outside)
    _reveal_stub(monkeypatch)
    with pytest.raises(ValueError, match="link"):
        session.reveal_predictions()
    assert outside.read_text("utf-8") == "{}"


def test_set_directory_swapped_for_a_junction_into_work_is_refused_at_save(
    corpus: Path, tmp_path: Path
) -> None:
    truth_path, truth = _truth(corpus)
    work = tmp_path / "work"
    session = TruthReviewSession(truth_path, work_root=work)
    moved = work / "moved-set"
    work.mkdir()
    shutil.move(str(truth_path.parent), str(moved))
    _junction(truth_path.parent, moved)
    before = (moved / "ground_truth.json").read_bytes()
    with pytest.raises(ValueError, match="work tree|link"):
        session.save({"rows": _rows(truth)})
    assert (moved / "ground_truth.json").read_bytes() == before
    assert not (moved / "annotation-first.json").exists()


# --------------------------------------------------------------------------------------------
# B1 (P0) — a bulk offset moves every role endpoint with its row, clamps included.
# --------------------------------------------------------------------------------------------


def test_plus_25_seconds_moves_every_role_segment_with_its_row(
    corpus: Path, tmp_path: Path
) -> None:
    truth_path, before = _truth(corpus)
    preview, _, collapsed = preview_bulk_offset(before, 25_000)
    assert collapsed == []
    expected = [
        [(25_000, 45_000, "uncertain")],
        # Row 2 is 20-40 s; +25 s clamps its end to the 60 s media end.  Its role must follow.
        [(45_000, 60_000, "uncertain")],
        # Row 3's start clamps to the last millisecond, exactly as the row's start does.
        [(59_999, 60_000, "uncertain")],
    ]
    assert [
        [(s.from_ms, s.to_ms, s.role) for s in episode.role_segments]
        for episode in preview.episodes
    ] == expected

    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    saved = session.save({"rows": _rows(preview), "offsets_ms": [25_000]})
    assert [
        [(s.from_ms, s.to_ms, s.role) for s in episode.role_segments] for episode in saved.episodes
    ] == expected


def test_without_the_offset_intent_a_clamped_row_refuses_rather_than_dropping_its_roles(
    corpus: Path, tmp_path: Path
) -> None:
    """An API client that sends only the final times cannot silently lose a hand annotation."""

    truth_path, before = _truth(corpus)
    preview, _, _ = preview_bulk_offset(before, 25_000)
    original = truth_path.read_bytes()
    with pytest.raises(ValueError, match="row 2: this timing edit would discard"):
        TruthReviewSession(truth_path, work_root=tmp_path / "work").save({"rows": _rows(preview)})
    assert truth_path.read_bytes() == original


def test_offsets_intent_is_validated(corpus: Path, tmp_path: Path) -> None:
    truth_path, before = _truth(corpus)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    for bad in ("25000", [True], [1.5], [10_000_000], [0] * 1_001):
        with pytest.raises(ValueError, match="offsets_ms"):
            session.save({"rows": _rows(before), "offsets_ms": bad})


def test_shipped_js_sends_the_applied_offsets_net_of_undo(corpus: Path, tmp_path: Path) -> None:
    result = _run_page_js(
        corpus,
        tmp_path,
        """
        let sent=null;
        globalThis.fetch=async(url,opts)=>{sent=JSON.parse(opts.body);
          return {ok:true,json:async()=>({saved:true})};};
        setOffset(25); applyOffset();
        setOffset(-3); applyOffset();
        undo();
        setOffset(7);  // previewed, never applied: not part of the intent
        undo();
        await save();
        const RESULT={offsets:sent.offsets_ms, start:sent.rows[1].start_ms_range,
          end:sent.rows[1].end_ms_range};
        """,
    )
    assert result["offsets"] == [25_000]
    assert result["start"] == [45_000, 45_000]
    assert result["end"] == [60_000, 60_000]


# --------------------------------------------------------------------------------------------
# B4 (P1) — Windows namespace spellings neither escape containment nor split the lock.
# --------------------------------------------------------------------------------------------


@pytest.mark.skipif(not WINDOWS, reason="Win32 namespace prefixes")
def test_extended_length_spelling_cannot_escape_the_work_prohibition(tmp_path: Path) -> None:
    work = tmp_path / "work"
    inside = work / "corpus"
    inside.mkdir(parents=True)
    shutil.copytree(FIXTURE / "fixture-set", inside / "fixture-set")
    with pytest.raises(ValueError, match="beneath the work tree"):
        find_truth_path(_extended(inside), "fixture-truth-review", work_root=work)
    with pytest.raises(ValueError, match="beneath the work tree"):
        find_truth_path(inside, "fixture-truth-review", work_root=_extended(work))
    with pytest.raises(ValueError, match="beneath the work tree"):
        TruthReviewSession(_extended(inside / "fixture-set" / "ground_truth.json"), work_root=work)


@pytest.mark.skipif(not WINDOWS, reason="Win32 namespace prefixes")
def test_two_spellings_of_one_record_take_one_write_lock(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import id_detector.truth_review as review_module

    truth_path, truth = _truth(corpus)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys, time
                from pathlib import Path
                sys.path.insert(0, sys.argv[2])
                from id_detector.truth import truth_write_lock
                with truth_write_lock(Path(sys.argv[1])):
                    print("held", flush=True)
                    time.sleep(30)
                """
            ),
            str(_extended(truth_path)),
            str(ROOT / "src"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
        monkeypatch.setattr(review_module, "_SAVE_LOCK_TIMEOUT", 1.0)
        with pytest.raises(ValueError, match="another truth writer holds this set"):
            session.save({"rows": _rows(truth)})
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_namespace_prefixes_canonicalise_to_one_key() -> None:
    from id_detector.truth_paths import strip_namespace_prefix

    assert strip_namespace_prefix("\\\\?\\C:\\Corpus\\set") == "C:\\Corpus\\set"
    assert strip_namespace_prefix("\\\\.\\C:\\Corpus\\set") == "C:\\Corpus\\set"
    assert strip_namespace_prefix("\\\\?\\UNC\\server\\share\\set") == "\\\\server\\share\\set"
    assert strip_namespace_prefix("//?/UNC/server/share/set") == "\\\\server\\share\\set"
    assert strip_namespace_prefix("\\\\server\\share\\set") == "\\\\server\\share\\set"
    assert strip_namespace_prefix("C:\\Corpus\\set") == "C:\\Corpus\\set"


# --------------------------------------------------------------------------------------------
# A1 (P1) — the annotation/truth pair survives process death between the two replacements.
# --------------------------------------------------------------------------------------------

#: Kill the process (no ``finally``, no ``except``) at the named point of a save.  At ``ec32f97``
#: the seam was ``truth.atomic_write_json``; the fix routes every set-file replacement through
#: ``truth._write_set_file``, so that is where the process now dies.
_DIE_DURING_SAVE = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[4])
import id_detector.truth as truth_module
from id_detector.truth_review import TruthReviewSession
truth_path = Path(sys.argv[1])
when = sys.argv[5]
real = truth_module._write_set_file
def die(pinned, name, content):
    if name == "ground_truth.json" and when == "before-truth":
        os._exit(9)
    real(pinned, name, content)
    if name == "ground_truth.json" and when == "after-truth":
        os._exit(9)
truth_module._write_set_file = die
TruthReviewSession(truth_path, work_root=Path(sys.argv[3])).save({"rows": json.loads(sys.argv[2])})
"""


def _die_during_save(truth_path: Path, truth: GroundTruthRecord, work: Path, when: str) -> None:
    died = subprocess.run(
        [
            sys.executable,
            "-c",
            _DIE_DURING_SAVE,
            str(truth_path),
            json.dumps(_rows(truth)),
            str(work),
            str(ROOT / "src"),
            when,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert died.returncode == 9, died.stderr


def test_process_death_between_annotation_and_truth_is_recovered_on_restart(
    corpus: Path, tmp_path: Path
) -> None:
    truth_path, truth = _truth(corpus)
    original_truth = truth_path.read_bytes()
    annotation = _annotation_path(truth_path, "first")
    previous_annotation = b'{"earlier":"annotation bytes the crash must not lose"}'
    annotation.write_bytes(previous_annotation)
    _die_during_save(truth_path, truth, tmp_path / "work", "before-truth")

    # Restart: opening the set must restore (or complete) the pair, never leave it mismatched.
    TruthReviewSession(truth_path, work_root=tmp_path / "work")
    assert truth_path.read_bytes() == original_truth
    assert annotation.read_bytes() == previous_annotation
    assert not truth_module.transaction_path(truth_path).exists()


def test_process_death_after_both_replacements_is_completed_on_restart(
    corpus: Path, tmp_path: Path
) -> None:
    truth_path, truth = _truth(corpus)
    _die_during_save(truth_path, truth, tmp_path / "work", "after-truth")
    assert truth_module.transaction_path(truth_path).exists()  # the crash left the record

    TruthReviewSession(truth_path, work_root=tmp_path / "work")
    saved = GroundTruthRecord.model_validate_json(read_text(truth_path))
    assert all(not episode.draft for episode in saved.episodes)
    annotation = json.loads(read_text(_annotation_path(truth_path, "first")))
    assert annotation["episodes"] == truth_module._annotation_content(saved)["episodes"]
    assert not truth_module.transaction_path(truth_path).exists()


def test_a_freeze_refuses_a_set_with_an_unrecovered_write(corpus: Path, tmp_path: Path) -> None:
    truth_path, truth = _truth(corpus)
    _die_during_save(truth_path, truth, tmp_path / "work", "after-truth")
    with pytest.raises(ValueError, match="interrupted write"):
        freeze_truth(corpus, corpus_version="fx-v1", out_path=tmp_path / "manifest.json")


def test_in_process_failure_restores_an_existing_annotation_byte_for_byte(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, truth = _truth(corpus)
    annotation = _annotation_path(truth_path, "first")
    annotation.write_bytes(b'{"earlier":"kept"}')
    original = truth_path.read_bytes()
    real_write = truth_module._write_set_file

    def fail_truth(pinned: object, name: str, content: bytes) -> None:
        if name == truth_path.name:
            raise OSError("injected truth failure")
        real_write(pinned, name, content)

    monkeypatch.setattr(truth_module, "_write_set_file", fail_truth)
    with pytest.raises(OSError, match="injected truth failure"):
        TruthReviewSession(truth_path, work_root=tmp_path / "work").save({"rows": _rows(truth)})
    assert annotation.read_bytes() == b'{"earlier":"kept"}'
    assert truth_path.read_bytes() == original
    assert not truth_module.transaction_path(truth_path).exists()


# --------------------------------------------------------------------------------------------
# A2/A3 (P1) — no re-attribution of another annotator's rows, and no reopening a frozen set.
# --------------------------------------------------------------------------------------------


def test_rows_verified_by_another_annotator_are_not_reattributed(
    corpus: Path, tmp_path: Path
) -> None:
    truth_path, _ = _truth(corpus)
    payload = json.loads(read_text(truth_path))
    payload["episodes"][0].update(annotator_ref="alice", verified_against="audio", draft=False)
    truth_path.write_text(json.dumps(payload), encoding="utf-8")
    truth_path, truth = _truth(corpus)
    original = truth_path.read_bytes()
    with pytest.raises(ValueError, match="alice"):
        TruthReviewSession(truth_path, work_root=tmp_path / "work").save({"rows": _rows(truth)})
    assert truth_path.read_bytes() == original
    assert not _annotation_path(truth_path, "first").exists()


def test_a_frozen_set_cannot_be_reopened_or_exposed(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path = _saved_and_frozen(corpus, tmp_path, monkeypatch)
    frozen_bytes = truth_path.read_bytes()
    with pytest.raises(ValueError, match="frozen"):
        TruthReviewSession(truth_path, work_root=tmp_path / "work")
    assert truth_path.read_bytes() == frozen_bytes


# --------------------------------------------------------------------------------------------
# A4 (P1) — the furniture audit: real names with leading punctuation pass, disguised furniture
# does not.
# --------------------------------------------------------------------------------------------


def _audit_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artist: str) -> list[str]:
    root = tmp_path / "audit-root"
    record = json.loads((FIXTURE / "fixture-set" / "ground_truth.json").read_text("utf-8"))
    record["episodes"][1]["work"]["artist"] = artist
    target = root / "data" / "corpus" / "audit-set" / "ground_truth.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(audit_fixtures, "ROOT", root)
    monkeypatch.setattr(audit_fixtures, "RAW_ROOT", root / "absent-raw")
    monkeypatch.setattr(audit_fixtures, "SCAN_ROOTS", (root / "data" / "corpus",))
    return audit_fixtures.audit()


@pytest.mark.parametrize(
    "artist",
    ["*NSYNC", "-M-", "!!!", "+44", "¡Forward, Russia!", "(hed) p.e.", "-", "Mall Grab"],
)
def test_legitimate_leading_punctuation_is_not_furniture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artist: str
) -> None:
    assert _audit_labels(tmp_path, monkeypatch, artist) == []


@pytest.mark.parametrize(
    "artist",
    [
        "- Mall Grab",
        "   - Mall Grab",
        "\t- Mall Grab",
        "\u2212 Mall Grab",  # MINUS SIGN
        "\u2010 Mall Grab",  # HYPHEN
        "\u2011 Mall Grab",  # NON-BREAKING HYPHEN
        "\u2013 Mall Grab",  # EN DASH
        "\u2014 Mall Grab",  # EM DASH
        "\ufe63 Mall Grab",  # SMALL HYPHEN-MINUS
        "\uff0d Mall Grab",  # FULLWIDTH HYPHEN-MINUS
        "\u2022 Mall Grab",  # BULLET
        "* Mall Grab",
        "\u00a0- Mall Grab",  # NO-BREAK SPACE before the bullet
        "-\u00a0Mall Grab",  # NO-BREAK SPACE after the bullet
        "\ufeff- Mall Grab",  # BOM
        "\u200b- Mall Grab",  # ZERO WIDTH SPACE
        "\u200d\u2060- Mall Grab",  # ZERO WIDTH JOINER + WORD JOINER
        "0:17:09 - Mall Grab",  # the timestamp the old seeding parser also left behind
        "\u200bMall Grab",  # an invisible prefix alone is still not what the owner reads
    ],
)
def test_disguised_tracklist_furniture_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artist: str
) -> None:
    failures = _audit_labels(tmp_path, monkeypatch, artist)
    assert len(failures) == 1, failures
    assert "episode 1 artist" in failures[0]


# Round 9: these tests exercise freezing and certification underneath the owner's moratorium, so the
# single gate is opened for them by fixture (production keeps it closed).  Tests that assert the
# moratorium itself close it again explicitly.
pytestmark = pytest.mark.usefixtures("certification_gate_open")
