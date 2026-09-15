"""Regressions for the round-8 review, fixed in round 9.

1. One moratorium gate (``truth.CERTIFICATION_ENABLED``, false in production) closes every freeze
   and certification emitter, each tested here with the gate closed.
2. Every user-selectable report or artifact destination passes ``truth.refuse_generated_output``.
3. The corpus ancestor check is O(depth) ``lstat``, with at most one bounded listing when creating.
4. (``tests/test_stage5_calibration.py``) the repeated-test-version guard is tested directly.

Nothing here sleeps, and nothing touches ``data/corpus/`` or ``work/``.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import id_detector.benchmark.scorer as scorer_module
import id_detector.truth as truth_module
from id_detector import cli
from id_detector.benchmark.scorer import score_corpus
from id_detector.contracts import CERTIFICATION_DISABLED
from id_detector.truth import (
    certifiable_under_gate,
    freeze_truth,
    open_corpus,
    refuse_generated_output,
    seed_truth,
)
from id_detector.truth_review import TruthReviewSession
from scripts.score_corpus import main as score_corpus_main
from scripts.score_corpus import summary
from tests.test_score_corpus import _copy_fixture, _mark_verified
from tests.test_score_corpus import _freeze as _freeze_mini
from tests.test_truth_corpus_followup_r3 import _junction
from tests.test_truth_corpus_followup_r4 import _reveal_stub
from tests.test_truth_corpus_followup_r7 import FIXTURE, GUIDANCE, _record, _snapshot, _tracklist
from tests.test_truth_corpus_followup_sol import _certifiable_corpus
from tests.test_truth_review import _rows


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    target = tmp_path / "root" / "corpus"
    shutil.copytree(FIXTURE, target)
    return target


def _gate(monkeypatch: pytest.MonkeyPatch, *, open_: bool) -> None:
    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", open_)


def _verify(truth_path: Path, tmp_path: Path) -> None:
    TruthReviewSession(truth_path, work_root=tmp_path / "work").save(
        {"rows": _rows(_record(truth_path))}
    )


def _seed(out_path: Path, tmp_path: Path) -> None:
    seed_truth(
        out_path=out_path,
        set_id="nested",
        duration_ms=60_000,
        media_key="a" * 64,
        tracklist=_tracklist(tmp_path),
        project_root=tmp_path / "project",
    )


# ==================================================================================================
# 1 (P0) -- one moratorium gate closes every freeze and certification emitter
# ==================================================================================================


def test_the_production_gate_is_closed() -> None:
    assert truth_module.CERTIFICATION_ENABLED is False
    assert truth_module.certification_enabled() is False
    assert truth_module.CERTIFICATION_DISABLED == CERTIFICATION_DISABLED


def test_freeze_is_refused_while_certification_is_disabled(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate(monkeypatch, open_=False)
    _verify(corpus / "fixture-set" / "ground_truth.json", tmp_path)
    before = _snapshot(corpus)
    with pytest.raises(ValueError, match=f"^{CERTIFICATION_DISABLED}: freezing a corpus"):
        freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    assert _snapshot(corpus) == before


def test_idea_truth_freeze_refuses_with_the_disabled_message(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate(monkeypatch, open_=False)
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    before = _snapshot(corpus)
    result = CliRunner().invoke(
        cli.app,
        [
            "truth",
            "freeze",
            "--truth",
            str(corpus),
            "--corpus-version",
            "fx-v1",
            "--out",
            str(corpus / "corpus-version.json"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert CERTIFICATION_DISABLED in result.output
    assert _snapshot(corpus) == before


def test_a_freeze_manifest_never_records_certifiable_while_the_gate_is_closed(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest's ``certifiable`` field consults the gate itself, not only the freeze entry."""

    _verify(corpus / "fixture-set" / "ground_truth.json", tmp_path)
    _gate(monkeypatch, open_=True)
    real_plan = truth_module._freeze_plan

    def plan_then_close(*args: object, **kwargs: object) -> object:
        planned = real_plan(*args, **kwargs)
        _gate(monkeypatch, open_=False)  # the moratorium holds when the manifest is built
        return planned

    monkeypatch.setattr(truth_module, "_freeze_plan", plan_then_close)
    manifest = freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    assert [item["prediction_exposure"]["certifiable"] for item in manifest["sets"]] == [False]
    assert certifiable_under_gate(True) is False
    _gate(monkeypatch, open_=True)
    assert certifiable_under_gate(True) is True


def test_the_generic_scorer_emits_the_disabled_status_and_never_certified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_root, predictions = _certifiable_corpus(tmp_path)
    _gate(monkeypatch, open_=False)
    report = score_corpus(truth_root, predictions)
    assert {entry.status for entry in report.certification} == {CERTIFICATION_DISABLED}
    _gate(monkeypatch, open_=True)  # the control: the same corpus certifies with the gate open
    statuses = {
        (entry.dimension, entry.tier): entry.status
        for entry in score_corpus(truth_root, predictions).certification
    }
    assert statuses[("work", "possible")] == "certified"


def test_idea_benchmark_score_prints_and_writes_the_disabled_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate(monkeypatch, open_=False)
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    truth_root, predictions = _certifiable_corpus(tmp_path)
    out = tmp_path / "reports" / "report.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "benchmark",
            "score",
            "--truth",
            str(truth_root),
            "--episodes",
            str(predictions),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"certification: {CERTIFICATION_DISABLED}" in result.output
    document = json.loads(out.read_text("utf-8"))
    assert {entry["status"] for entry in document["certification"]} == {CERTIFICATION_DISABLED}


def test_score_corpus_json_and_printed_summaries_show_the_disabled_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gate(monkeypatch, open_=False)
    root = _copy_fixture(tmp_path)
    for set_id in ("mini-a", "mini-b"):
        _mark_verified(root / set_id / "ground_truth.json")
    _freeze_mini(root, ["mini-a", "mini-b"])
    out = tmp_path / "scored" / "out.json"
    run_list = str(root / "run-list.json")
    assert score_corpus_main(["--run-list", run_list, "--out", str(out), "--match", "time"]) == 0
    assert f"NOT CERTIFIABLE: {CERTIFICATION_DISABLED}" in capsys.readouterr().out
    document = json.loads(out.read_text("utf-8"))
    assert document["l3"]["certifiable"] is False
    assert document["l3"]["certification"] == CERTIFICATION_DISABLED
    assert CERTIFICATION_DISABLED in summary(document, out)
    assert score_corpus_main(["--run-list", run_list, "--print", "--match", "time"]) == 0
    assert CERTIFICATION_DISABLED in capsys.readouterr().out


# ==================================================================================================
# 2 (P0) -- generated output destinations never damage a corpus
# ==================================================================================================


def test_benchmark_score_refuses_out_at_the_truth_file_and_leaves_it_byte_identical(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    set_dir = corpus / "fixture-set"
    truth_path = set_dir / "ground_truth.json"
    before = truth_path.read_bytes()
    result = CliRunner().invoke(
        cli.app,
        [
            "benchmark",
            "score",
            "--truth",
            str(set_dir),
            "--episodes",
            str(tmp_path / "predictions.json"),
            "--out",
            str(truth_path),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "refusing to write generated output" in result.output
    assert truth_path.read_bytes() == before
    with pytest.raises(ValueError, match="refusing to write generated output"):
        score_corpus(truth_path, tmp_path / "predictions.json", out_path=truth_path)
    assert truth_path.read_bytes() == before


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("corpus file name", "is a corpus file name"),
        ("inside a set directory", "lies inside the corpus"),
        ("new file in a corpus without a manifest", "lies inside the corpus"),
        ("an existing corpus directory", "is a corpus or a set"),
        ("a directory containing a corpus", "contains corpus files"),
        ("through a link", GUIDANCE),
        ("beneath the work root", "beneath the work tree"),
    ],
)
def test_generated_output_destinations_that_could_damage_a_corpus_are_refused(
    corpus: Path, tmp_path: Path, case: str, message: str
) -> None:
    work_root: Path | None = None
    if case == "corpus file name":
        target = tmp_path / "reports" / "corpus-version.json"
    elif case == "inside a set directory":
        target = corpus / "fixture-set" / "report.json"
    elif case == "new file in a corpus without a manifest":
        assert not (corpus / "corpus-version.json").exists()
        target = corpus / "report.json"
    elif case == "an existing corpus directory":
        target = corpus
    elif case == "a directory containing a corpus":
        target = tmp_path / "holder"
        shutil.copytree(FIXTURE, target / "nested" / "corpus")
    elif case == "through a link":
        real = tmp_path / "real-reports"
        real.mkdir()
        _junction(tmp_path / "linked-reports", real)
        target = tmp_path / "linked-reports" / "report.json"
    else:
        work_root = tmp_path / "work"
        target = work_root / "report.json"
    before = _snapshot(corpus)
    with pytest.raises(ValueError, match=message):
        refuse_generated_output(target, work_root=work_root)
    assert _snapshot(corpus) == before
    allowed = tmp_path / "reports" / "report.json"
    assert refuse_generated_output(allowed) == Path(os.path.abspath(allowed))


def test_the_scorer_revalidates_its_output_immediately_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_root, predictions = _certifiable_corpus(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    out = reports / "report.json"
    real_score_set = scorer_module.score_set

    def score_while_a_corpus_appears(*args: object, **kwargs: object) -> object:
        (reports / "corpus-version.json").write_text("{}", encoding="utf-8")
        return real_score_set(*args, **kwargs)

    monkeypatch.setattr(scorer_module, "score_set", score_while_a_corpus_appears)
    with pytest.raises(ValueError, match="lies inside the corpus"):
        score_corpus(truth_root, predictions, out_path=out)
    assert not out.exists()


def test_score_corpus_refuses_an_output_inside_a_corpus(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy_fixture(tmp_path)
    before = _snapshot(root)
    out = root / "mini-a" / "out.json"
    assert score_corpus_main(["--run-list", str(root / "run-list.json"), "--out", str(out)]) == 1
    assert "refusing to write generated output" in capsys.readouterr().err
    assert _snapshot(root) == before


# ==================================================================================================
# 3 (P1) -- the ancestor check is bounded
# ==================================================================================================


def _recording_scandir(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    listed: list[str] = []
    real = os.scandir

    def recording(path: object = ".") -> object:
        listed.append(os.path.normcase(os.fspath(path)).removeprefix("\\\\?\\"))  # type: ignore[arg-type]
        return real(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", recording)
    return listed


def test_a_big_ancestor_folder_is_never_enumerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "big"
    big.mkdir()
    for index in range(7_500):
        (big / f"entry-{index:04d}.txt").write_bytes(b"")
    corpus = big / "corpus"
    shutil.copytree(FIXTURE, corpus)
    truth_path = corpus / "fixture-set" / "ground_truth.json"
    _reveal_stub(monkeypatch)
    listed = _recording_scandir(monkeypatch)
    with open_corpus(corpus, mutate=False):
        pass
    with open_corpus(corpus, mutate=True, require_records=False):
        pass
    TruthReviewSession(truth_path, work_root=tmp_path / "work").reveal_predictions()
    _seed(corpus / "new-set" / "ground_truth.json", tmp_path)
    key = os.path.normcase(str(big))
    assert [item for item in listed if item == key] == []
    assert (corpus / "new-set" / "ground_truth.json").is_file()


def test_creating_a_corpus_root_in_an_oversized_folder_fails_closed(tmp_path: Path) -> None:
    big = tmp_path / "big"
    big.mkdir()
    for index in range(2_001):
        (big / f"entry-{index:04d}.txt").write_bytes(b"")
    with (
        pytest.raises(ValueError, match="more than 2000 entries"),
        open_corpus(big / "new-corpus", mutate=True, require_records=False),
    ):
        pytest.fail("the gateway created a corpus root it could not check")
    assert not (big / "new-corpus").exists()
    small = tmp_path / "small"
    small.mkdir()
    with open_corpus(small / "new-corpus", mutate=True, require_records=False) as handle:
        assert handle.truth_files == ()


def test_reveal_on_the_real_layout_corpus_lists_only_the_corpus_and_stays_fast(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path = corpus / "fixture-set" / "ground_truth.json"
    _reveal_stub(monkeypatch)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    listed = _recording_scandir(monkeypatch)
    started = time.perf_counter()
    session.reveal_predictions()
    elapsed = time.perf_counter() - started
    root = os.path.normcase(str(corpus))
    assert all(item.startswith(root) for item in listed), listed
    assert elapsed < 5.0, f"reveal took {elapsed:.2f}s"


def test_nested_seeds_into_a_release_style_corpus_without_a_manifest_are_refused(
    tmp_path: Path,
) -> None:
    release = tmp_path / "data" / "corpus" / "release-like"
    shutil.copytree(FIXTURE, release)
    assert not (release / "corpus-version.json").exists()
    before = _snapshot(release)
    with pytest.raises(ValueError, match="does not exist"):
        _seed(release / "new-group" / "new-set" / "ground_truth.json", tmp_path)
    with pytest.raises(ValueError, match="inside another corpus|is a set, not a corpus"):
        _seed(release / "fixture-set" / "deeper" / "ground_truth.json", tmp_path)
    assert _snapshot(release) == before
    assert not (release / "new-group").exists()


def test_seed_never_creates_a_corpus_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        _seed(tmp_path / "new-corpus" / "set" / "ground_truth.json", tmp_path)
    assert not (tmp_path / "new-corpus").exists()
