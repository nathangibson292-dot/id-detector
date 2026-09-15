"""Regressions for the round-3 (sol xhigh) review: the four REALISTIC findings only.

Each test was first run against the follow-up as it stood before this round's fixes and failed
there (see ``docs/reviews/followup-truth-corpus.md``).  They model accidents an owner can plausibly
cause -- tidying an untracked file, re-running a command, freezing to a different folder, reaching
the corpus through a linked folder -- not a hostile program (out of scope by owner decision,
2026-09-14).
Nothing touches ``data/corpus/``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from id_detector import cli
from id_detector.benchmark.scorer import corpus_independent, truth_is_frozen_verified
from id_detector.calibrate.certify import (
    CorpusNotIndependent,
    _require_frozen,
    _require_independent,
)
from id_detector.contracts import GroundTruthRecord
from id_detector.io import atomic_write_json, read_text
from id_detector.truth import (
    freeze_truth,
    prediction_exposure,
    second_pass_truth,
    seed_truth,
    write_draft_manifest,
)
from id_detector.truth_review import TruthReviewSession, exposure_path, find_truth_path
from scripts import score_corpus
from tests.conftest import write_corpus_fixture
from tests.test_stage2a_scorer import _config, _vector
from tests.test_truth_review import _reveal_stub, _rows, _truth

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


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _verify(truth_path: Path, tmp_path: Path) -> None:
    TruthReviewSession(truth_path, work_root=tmp_path / "work").save(
        {"rows": _rows(_record(truth_path))}
    )


def _frozen(corpus: Path, tmp_path: Path) -> Path:
    truth_path = corpus / "fixture-set" / "ground_truth.json"
    _verify(truth_path, tmp_path)
    freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    return truth_path


def _tracklist(tmp_path: Path) -> Path:
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text("00:10 Artist One - Track One\n", encoding="utf-8")
    return tracklist


# --------------------------------------------------------------------------------------------
# R-P0-1 — exposure survives the owner tidying away the untracked sidecar before the first save.
# --------------------------------------------------------------------------------------------


def test_exposure_survives_sidecar_deletion_through_save_second_pass_freeze_and_scoring(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, truth = _truth(corpus)
    _reveal_stub(monkeypatch)
    TruthReviewSession(truth_path, work_root=tmp_path / "work").reveal_predictions()
    # The reveal has returned; the owner deletes the new, untracked sidecar while tidying the set.
    exposure_path(truth_path).unlink()
    assert score_corpus.truth_independent(truth_path, _record(truth_path)) is False

    restarted = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    assert restarted.predictions_visible is True
    restarted.save({"rows": _rows(truth)})
    assert prediction_exposure(truth_path)["predictions_visible_during_review"] is True
    exposure_path(truth_path).unlink(missing_ok=True)

    second = tmp_path / "second.json"
    atomic_write_json(second, json.loads(read_text(truth_path)))
    second_pass_truth(truth_path, annotator_ref="second-annotator", annotation_path=second)
    assert prediction_exposure(truth_path)["predictions_visible_during_review"] is True

    manifest = freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    assert manifest["exposed_sets"] == [truth.set_id]
    assert manifest["sets"][0]["prediction_exposure"]["certifiable"] is False
    frozen = _record(truth_path)
    assert truth_is_frozen_verified(truth_path, [frozen]) is True  # frozen and intact ...
    assert score_corpus.truth_independent(truth_path, frozen) is False  # ... but not independent
    assert corpus_independent(corpus, [frozen]) is False
    with pytest.raises(CorpusNotIndependent):
        _require_independent(corpus, "fx-v1")


# --------------------------------------------------------------------------------------------
# R-P0-2 — re-running seed or manifest-draft never overwrites reviewed or frozen state.
# --------------------------------------------------------------------------------------------


def test_seed_refuses_an_existing_reviewed_record_and_changes_nothing(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, truth = _truth(corpus)
    _reveal_stub(monkeypatch)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    session.reveal_predictions()
    session.save({"rows": _rows(truth)})
    before = _snapshot(corpus)
    with pytest.raises(ValueError, match="already"):
        seed_truth(
            out_path=truth_path,
            set_id=truth.set_id,
            duration_ms=60_000,
            media_key=truth.source.media_key,
            tracklist=_tracklist(tmp_path),
            source_url="local-source-reference",
            project_root=tmp_path / "project",
        )
    assert _snapshot(corpus) == before
    assert not (tmp_path / "project").exists()  # no partial write anywhere


def test_seed_refuses_a_frozen_record_and_changes_nothing(corpus: Path, tmp_path: Path) -> None:
    truth_path = _frozen(corpus, tmp_path)
    before = _snapshot(corpus)
    # Round 6: seeding into a frozen corpus is refused at the corpus level (frozen manifest),
    # before the destination is even examined.
    with pytest.raises(ValueError, match="already|frozen"):
        seed_truth(
            out_path=truth_path,
            set_id="fixture-truth-review",
            duration_ms=60_000,
            media_key="a" * 64,
            tracklist=_tracklist(tmp_path),
            project_root=tmp_path / "project",
        )
    assert _snapshot(corpus) == before


def test_seed_refuses_a_set_directory_holding_leftover_pass_files(tmp_path: Path) -> None:
    set_dir = tmp_path / "corpus" / "set"
    set_dir.mkdir(parents=True)
    (set_dir / "annotation-first.json").write_text("{}", encoding="utf-8")
    before = _snapshot(set_dir)
    with pytest.raises(ValueError, match="already"):
        seed_truth(
            out_path=set_dir / "ground_truth.json",
            set_id="leftover",
            duration_ms=60_000,
            media_key="b" * 64,
            tracklist=_tracklist(tmp_path),
            project_root=tmp_path / "project",
        )
    assert _snapshot(set_dir) == before


def test_cli_seed_refuses_an_existing_record(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path = corpus / "fixture-set" / "ground_truth.json"
    _verify(truth_path, tmp_path)
    before = _snapshot(corpus)
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    result = CliRunner().invoke(
        cli.app,
        [
            "truth",
            "seed",
            "--out",
            str(truth_path),
            "--set-id",
            "fixture-truth-review",
            "--duration-ms",
            "60000",
            "--media-key",
            "a" * 64,
            "--tracklist",
            str(_tracklist(tmp_path)),
        ],
    )
    assert result.exit_code != 0, result.output
    assert _snapshot(corpus) == before


def test_manifest_draft_refuses_to_overwrite_a_frozen_manifest(
    corpus: Path, tmp_path: Path
) -> None:
    _frozen(corpus, tmp_path)
    before = _snapshot(corpus)
    # Round 6: the draft output is fixed to <corpus>/corpus-version.json; a frozen corpus refuses.
    with pytest.raises(ValueError, match="frozen"):
        write_draft_manifest(corpus, corpus_version="fx-v1")
    assert _snapshot(corpus) == before


def test_manifest_draft_refuses_non_draft_records(corpus: Path, tmp_path: Path) -> None:
    _verify(corpus / "fixture-set" / "ground_truth.json", tmp_path)
    before = _snapshot(corpus)
    with pytest.raises(ValueError, match="draft"):
        write_draft_manifest(corpus, corpus_version="draft")
    assert not (corpus / "corpus-version.json").exists()
    assert _snapshot(corpus) == before


def test_cli_manifest_draft_refuses_a_frozen_manifest(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _frozen(corpus, tmp_path)
    before = _snapshot(corpus)
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    result = CliRunner().invoke(
        cli.app,
        [
            "truth",
            "manifest-draft",
            "--truth",
            str(corpus),
            "--corpus-version",
            "fx-v1",
        ],
    )
    assert result.exit_code != 0, result.output
    assert _snapshot(corpus) == before


# --------------------------------------------------------------------------------------------
# R-P1-3 — the one accepted manifest placement works for directory-based scoring and certify.
# --------------------------------------------------------------------------------------------


def _vector_corpus(tmp_path: Path) -> tuple[Path, Path]:
    truth, predictions = _vector()
    corpus_dir = tmp_path / "root" / "vector-corpus"
    (corpus_dir / truth.set_id).mkdir(parents=True)
    write_corpus_fixture(corpus_dir / truth.set_id / "ground_truth.json", truth)
    return corpus_dir, corpus_dir / truth.set_id / "ground_truth.json"


def _predictions_for(tmp_path: Path, corpus_version: str) -> Path:
    _, predictions = _vector()
    snapshot, config_hash = _config(31, [])
    path = tmp_path / "predictions.json"
    atomic_write_json(
        path,
        {
            "corpus_version": corpus_version,
            "profile": "vector",
            "config_hash": config_hash,
            "config_snapshot": snapshot,
            "sets": [predictions],
            "unverified_seed_comparison": False,
        },
    )
    return path


@pytest.mark.parametrize("placement", ["corpus-root", "set-directory", "directory-above"])
def test_manifest_placement_works_for_directory_scoring_or_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placement: str
) -> None:
    corpus_dir, truth_path = _vector_corpus(tmp_path)
    out_dir = {
        "corpus-root": corpus_dir,
        "set-directory": truth_path.parent,
        "directory-above": corpus_dir.parent,
    }[placement]
    if placement != "corpus-root":
        # Only the corpus root is found by every consumer given a directory; anything else would
        # freeze a corpus that `idea benchmark score --truth <dir>` and certify cannot see.
        before = _snapshot(corpus_dir)
        with pytest.raises(ValueError, match="manifest"):
            freeze_truth(
                corpus_dir, corpus_version="vec-v1", out_path=out_dir / "corpus-version.json"
            )
        assert _snapshot(corpus_dir) == before
        return
    freeze_truth(corpus_dir, corpus_version="vec-v1", out_path=out_dir / "corpus-version.json")
    _require_frozen(corpus_dir, "vec-v1")
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    out = tmp_path / "report.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "benchmark",
            "score",
            "--truth",
            str(corpus_dir),
            "--episodes",
            str(_predictions_for(tmp_path, "vec-v1")),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output  # verified through the directory argument
    assert json.loads(out.read_text("utf-8"))["corpus_version"] == "vec-v1"


# --------------------------------------------------------------------------------------------
# R-P1-4 — links are refused before resolution, everywhere, with "pass the real path" guidance.
# --------------------------------------------------------------------------------------------


def test_review_refuses_a_corpus_reached_through_a_link(corpus: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked-corpus"
    _junction(linked, corpus)
    with pytest.raises(ValueError, match="real path"):
        find_truth_path(linked, "fixture-truth-review", work_root=tmp_path / "work")


def test_scorer_refuses_a_manifest_root_reached_through_an_ancestor_link(
    corpus: Path, tmp_path: Path
) -> None:
    truth_path = _frozen(corpus, tmp_path)
    frozen = _record(truth_path)
    linked_root = tmp_path / "linked-root"
    _junction(linked_root, corpus.parent)
    via_link = linked_root / "corpus" / "fixture-set" / "ground_truth.json"
    with pytest.raises(ValueError, match="real path"):
        truth_is_frozen_verified(via_link, [frozen])
    with pytest.raises(ValueError, match="real path"):
        truth_is_frozen_verified(linked_root / "corpus", [frozen])


def test_freeze_link_refusal_tells_the_owner_to_pass_the_real_path(
    corpus: Path, tmp_path: Path
) -> None:
    _verify(corpus / "fixture-set" / "ground_truth.json", tmp_path)
    linked_root = tmp_path / "linked-root"
    _junction(linked_root, corpus.parent)
    with pytest.raises(ValueError, match="real path"):
        freeze_truth(
            linked_root / "corpus",
            corpus_version="fx-v1",
            out_path=linked_root / "corpus" / "corpus-version.json",
        )


def test_seed_link_refusal_tells_the_owner_to_pass_the_real_path(tmp_path: Path) -> None:
    real = tmp_path / "real-corpus"
    real.mkdir()
    linked = tmp_path / "linked-corpus"
    _junction(linked, real)
    with pytest.raises(ValueError, match="real path"):
        seed_truth(
            out_path=linked / "set" / "ground_truth.json",
            set_id="linked-seed",
            duration_ms=60_000,
            media_key="c" * 64,
            tracklist=_tracklist(tmp_path),
            project_root=tmp_path / "project",
        )
    assert list(real.iterdir()) == []


# Round 9: these tests exercise freezing and certification underneath the owner's moratorium, so the
# single gate is opened for them by fixture (production keeps it closed).  Tests that assert the
# moratorium itself close it again explicitly.
pytestmark = pytest.mark.usefixtures("certification_gate_open")
