"""Regressions for the certification follow-up: the two defects deferred by the truth-corpus cycle.

1. A run list over a SUBSET of a frozen corpus can never be certifiable.  Every truth path must
   belong to ONE frozen corpus, the run list must name EXACTLY that corpus's frozen inventory, and
   independence is judged over that complete inventory (``scorer.certification_scope``).  A partial
   run list is still scored for development, and the report says why it is not certifiable.
2. Calibration validation's scratch corpus is validated against the configured ``work_root`` and
   every corpus BEFORE any file or folder is created (``truth.refuse_scratch_destination``).

Both directions of the certification gate are tested.  Nothing here touches ``data/corpus/`` or
``work/``: every corpus is a temporary copy of a test fixture.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

import id_detector.benchmark.scorer as scorer_module
import id_detector.calibrate.certify as certify_module
import id_detector.calibrate.validate as validate_module
import id_detector.truth as truth_module
from id_detector.benchmark.scorer import PredictionSet, certification_scope, score_corpus
from id_detector.calibrate.certify import CorpusNotFrozen, run_certify
from id_detector.calibrate.validate import run_calibration_validation
from id_detector.contracts import GroundTruthRecord
from id_detector.io import atomic_write_json
from id_detector.truth import (
    CERTIFICATION_DISABLED,
    CORPUS_LISTING_LIMIT,
    FREEZE_REFUSAL_LIMIT,
    freeze_truth,
    refuse_scratch_destination,
)
from scripts.score_corpus import main as score_main
from tests.conftest import write_corpus_fixture
from tests.test_score_corpus import FIXTURE as MINI
from tests.test_score_corpus import RUN_LIST as MINI_RUN_LIST
from tests.test_score_corpus import _mark_verified, _write_json
from tests.test_stage2a_scorer import _config, _vector, _write_freeze_manifest
from tests.test_truth_corpus_followup import _exposure_payload
from tests.test_truth_corpus_followup_r7 import _freeze as _freeze_review_corpus
from tests.test_truth_corpus_followup_r7 import _snapshot, _verify

ROOT = Path(__file__).resolve().parents[1]
REVIEW_FIXTURE = ROOT / "tests" / "fixtures" / "truth-review"
REVIEW_SET_ID = "fixture-truth-review"


@pytest.fixture
def gate_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", True)


@pytest.fixture
def gate_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", False)


# ==================================================================================================
# Defect 1 -- a run list over a subset of a frozen corpus is never certifiable
# ==================================================================================================


def _mini_corpus(root: Path, set_ids: tuple[str, ...] = ("mini-a", "mini-b")) -> Path:
    """A private, verified copy of corpus-mini holding exactly ``set_ids``."""

    shutil.copytree(MINI, root)
    for set_id in ("mini-a", "mini-b"):
        if set_id in set_ids:
            _mark_verified(root / set_id / "ground_truth.json")
        else:
            shutil.rmtree(root / set_id)
    return root


def _freeze_mini(
    root: Path, set_ids: tuple[str, ...], *, manifest_exposed: str | None = None
) -> None:
    sets: list[dict[str, Any]] = []
    for set_id in set_ids:
        entry: dict[str, Any] = {
            "set_id": set_id,
            "path": f"{set_id}/ground_truth.json",
            "sha256": sha256((root / set_id / "ground_truth.json").read_bytes()).hexdigest(),
        }
        if set_id == manifest_exposed:
            entry["prediction_exposure"] = {"predictions_visible_during_review": True}
        sets.append(entry)
    _write_json(
        root / "corpus-version.json",
        {
            "schema_version": "1.0.0",
            "generated_by": "test",
            "corpus_version": "corpus-mini",
            "frozen": True,
            "sets": sets,
        },
    )


def _run_list(path: Path, entries: list[tuple[Path, str]]) -> Path:
    """A run list naming ``(corpus root, mix id)`` pairs by absolute path."""

    _write_json(
        path,
        {
            "recipe": "deep",
            "runs": [
                {
                    "mix_id": mix_id,
                    "truth": str(root / mix_id / "ground_truth.json"),
                    "episodes": str(root / mix_id / "fuse" / "episodes.json"),
                    "media_key": json.loads(
                        (root / mix_id / "ground_truth.json").read_text("utf-8")
                    )["source"]["media_key"],
                }
                for root, mix_id in entries
            ],
        },
    )
    return path


def _score(tmp_path: Path, run_list: Path, name: str) -> dict[str, Any]:
    out = tmp_path / name / "out.json"
    assert score_main(["--run-list", str(run_list), "--out", str(out), "--match", "time"]) == 0
    return json.loads(out.read_text("utf-8"))


def test_a_whole_clean_frozen_corpus_is_certifiable_with_the_gate_open(
    tmp_path: Path, gate_open: None
) -> None:
    """The control: the rule below refuses subsets, not certification itself."""

    root = _mini_corpus(tmp_path / "corpus")
    _freeze_mini(root, ("mini-a", "mini-b"))
    whole = _run_list(tmp_path / "whole.json", [(root, "mini-a"), (root, "mini-b")])
    document = _score(tmp_path, whole, "whole")
    assert document["truth_status"] == "verified"
    assert document["l3"]["certifiable"] is True
    assert document["l3"]["independent"] is True
    assert document["l3"]["certification"] is None
    assert document["l3"]["not_certifiable_because"] == []
    assert document["l3"]["scope"] == {
        "whole_frozen_corpus": True,
        "corpus_independent": True,
        "reasons": [],
    }


@pytest.mark.parametrize("evidence", ["sidecar beside the set", "recorded in the manifest"])
def test_leaving_the_exposed_set_out_of_the_run_list_never_certifies_the_rest(
    tmp_path: Path, gate_open: None, evidence: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _mini_corpus(tmp_path / "corpus")
    if evidence == "sidecar beside the set":
        _write_json(root / "mini-b" / "review-exposure.json", _exposure_payload("mini-b"))
        _freeze_mini(root, ("mini-a", "mini-b"))
    else:
        _freeze_mini(root, ("mini-a", "mini-b"), manifest_exposed="mini-b")
    before = _snapshot(root)

    # The whole corpus is honest: mini-b was exposed, so nothing over this corpus certifies.
    whole = _run_list(tmp_path / "whole.json", [(root, "mini-a"), (root, "mini-b")])
    assert _score(tmp_path, whole, "whole")["l3"]["certifiable"] is False

    # The attack: leave the exposed set out.  Still scored, never certifiable, and it says why.
    partial = _run_list(tmp_path / "partial.json", [(root, "mini-a")])
    capsys.readouterr()
    document = _score(tmp_path, partial, "partial")
    printed = capsys.readouterr().out
    assert document["truth_status"] == "verified"
    assert [mix["mix_id"] for mix in document["mixes"]] == ["mini-a"]
    assert document["mixes"][0]["independent"] is True  # the set itself is clean
    assert document["l3"]["certifiable"] is False
    assert document["l3"]["independent"] is False  # judged over the whole frozen corpus
    assert document["l3"]["scope"]["whole_frozen_corpus"] is False
    assert document["l3"]["scope"]["corpus_independent"] is False
    reasons = " | ".join(document["l3"]["not_certifiable_because"])
    assert "scores only 1 of the 2 sets" in reasons
    assert "leaves out mini-b (mini-b/ground_truth.json)" in reasons
    assert "add the missing sets to the run list" in reasons
    assert "mini-b was reviewed with the predictions visible" in reasons
    assert "NOT CERTIFIABLE: this run list scores only 1 of the 2 sets" in printed
    assert _snapshot(root) == before  # scoring never writes into the corpus


def test_a_partial_run_list_over_a_clean_frozen_corpus_is_scored_but_not_certifiable(
    tmp_path: Path, gate_open: None, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _mini_corpus(tmp_path / "corpus")
    _freeze_mini(root, ("mini-a", "mini-b"))
    partial = _run_list(tmp_path / "partial.json", [(root, "mini-b")])
    document = _score(tmp_path, partial, "partial")
    assert document["l3"]["certifiable"] is False
    assert document["l3"]["independent"] is True
    assert document["l3"]["scope"] == {
        "whole_frozen_corpus": False,
        "corpus_independent": True,
        "reasons": document["l3"]["not_certifiable_because"],
    }
    (reason,) = document["l3"]["not_certifiable_because"]
    assert "leaves out mini-a (mini-a/ground_truth.json)" in reason
    assert "A partial run list is fine for development" in reason
    # The plain-English paragraph says it too.
    capsys.readouterr()
    assert score_main(["--run-list", str(partial), "--print", "--match", "time"]) == 0
    assert "NOT CERTIFIABLE: this run list scores only 1 of the 2 sets" in capsys.readouterr().out


def test_a_run_list_spanning_two_frozen_corpora_is_never_certifiable(
    tmp_path: Path, gate_open: None
) -> None:
    first = _mini_corpus(tmp_path / "first", ("mini-a",))
    second = _mini_corpus(tmp_path / "second", ("mini-b",))
    _freeze_mini(first, ("mini-a",))
    _freeze_mini(second, ("mini-b",))
    # Each corpus alone is whole, frozen and clean ...
    alone = _run_list(tmp_path / "alone.json", [(first, "mini-a")])
    assert _score(tmp_path, alone, "alone")["l3"]["certifiable"] is True
    # ... but one claim never pools two corpora.
    both = _run_list(tmp_path / "both.json", [(first, "mini-a"), (second, "mini-b")])
    document = _score(tmp_path, both, "both")
    assert document["truth_status"] == "verified"
    assert document["l3"]["certifiable"] is False
    (reason,) = document["l3"]["not_certifiable_because"]
    assert "come from 2 different corpora" in reason
    assert "give each corpus its own run list" in reason


def test_certification_scope_judges_exposure_over_the_complete_frozen_inventory(
    tmp_path: Path,
) -> None:
    root = _mini_corpus(tmp_path / "corpus")
    _write_json(root / "mini-b" / "review-exposure.json", _exposure_payload("mini-b"))
    _freeze_mini(root, ("mini-a", "mini-b"))
    clean, exposed = (root / name / "ground_truth.json" for name in ("mini-a", "mini-b"))

    subset = certification_scope([clean])
    assert (subset.complete, subset.independent, subset.certifiable) == (False, False, False)
    whole = certification_scope([clean, exposed])
    assert (whole.complete, whole.independent, whole.certifiable) == (True, False, False)
    assert certification_scope([root]).complete is True  # the corpus folder names all of it

    standalone = tmp_path / "loose-truth.json"
    standalone.write_bytes(clean.read_bytes())
    loose = certification_scope([standalone])
    assert loose.certifiable is False
    assert "standalone truth file" in loose.reasons[0]

    # A frozen corpus that lost a set on disk is refused outright, exactly as everywhere else.
    shutil.rmtree(root / "mini-b")
    with pytest.raises(ValueError, match="missing mini-b"):
        certification_scope([clean])


def _vector_corpus(tmp_path: Path, name: str, set_ids: list[str]) -> tuple[Path, Path]:
    """A frozen corpus of the stage-2a vector, plus predictions for its FIRST set only."""

    truth_root = tmp_path / name
    truths = []
    prediction_sets = []
    for set_id in set_ids:
        truth, predictions = _vector()
        truth = GroundTruthRecord.model_validate(
            {**truth.model_dump(mode="json"), "set_id": set_id, "split": "test"}
        )
        (truth_root / set_id).mkdir(parents=True)
        write_corpus_fixture(truth_root / set_id / "ground_truth.json", truth)
        truths.append(truth)
        prediction_sets.append(
            PredictionSet(
                set_id=set_id, identities=predictions.identities, episodes=predictions.episodes
            )
        )
    _write_freeze_manifest(truth_root, truths)
    targets = [{"profile": "vector", "dimension": "work", "tier": "possible", "target_e4": 1}]
    snapshot, config_hash = _config(31, targets)
    predictions_path = tmp_path / f"{name}-predictions.json"
    atomic_write_json(
        predictions_path,
        {
            "corpus_version": "vector-v1",
            "profile": "vector",
            "config_hash": config_hash,
            "config_snapshot": snapshot,
            "sets": prediction_sets[:1],
            "unverified_seed_comparison": False,
        },
    )
    return truth_root, predictions_path


def test_the_generic_scorer_never_certifies_one_record_named_out_of_a_frozen_corpus(
    tmp_path: Path, gate_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ten sets are normally needed, which a single record can never supply; lower the bar so that
    # the scope rule -- not the set count -- is what this test exercises.
    monkeypatch.setattr(scorer_module, "CERTIFICATION_MIN_SETS", 1)

    def status(truth: Path, predictions: Path) -> str:
        report = score_corpus(truth, predictions)
        return {(e.dimension, e.tier): e.status for e in report.certification}[("work", "possible")]

    # Control: a one-set frozen corpus named by its record IS its whole inventory.
    alone, alone_predictions = _vector_corpus(tmp_path, "alone", ["set-00"])
    assert status(alone / "set-00" / "ground_truth.json", alone_predictions) == "certified"

    # One clean record named out of a two-set frozen corpus whose other set was exposed.
    pair, pair_predictions = _vector_corpus(tmp_path, "pair", ["set-00", "set-01"])
    (pair / "set-01" / "review-exposure.json").write_text(
        json.dumps(_exposure_payload("set-01")), encoding="utf-8"
    )
    assert status(pair / "set-00" / "ground_truth.json", pair_predictions) == "provisional"


# ==================================================================================================
# Draft truth is still scored for development, labelled a draft, and never certifiable
# ==================================================================================================


@pytest.mark.parametrize("gate", ["open", "closed"])
def test_draft_truth_is_scored_labelled_a_draft_and_never_certifiable(
    tmp_path: Path, gate: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", gate == "open")
    out = tmp_path / "out.json"
    assert score_main(["--run-list", str(MINI_RUN_LIST), "--out", str(out), "--print"]) == 0
    document = json.loads(out.read_text("utf-8"))
    printed = capsys.readouterr().out
    assert document["truth_status"] == "draft"
    assert document["work_recall_e4"] == 6_667  # the development numbers are all there
    assert document["l3"]["certifiable"] is False
    assert document["l3"]["scope"]["whole_frozen_corpus"] is False
    reasons = " | ".join(document["l3"]["not_certifiable_because"])
    assert "is not frozen, so this is a development score of draft truth" in reasons
    assert "Keep checking the tracklists by ear" in reasons
    assert (CERTIFICATION_DISABLED in reasons) is (gate == "closed")
    assert "DRAFT truth" in printed
    assert "working numbers, not release numbers" in printed


# ==================================================================================================
# Defect 2 -- the calibration scratch corpus, the configured work root, and every corpus
# ==================================================================================================


@pytest.fixture
def frozen_review_corpus(tmp_path: Path, gate_open: None) -> Path:
    corpus = tmp_path / "root" / "corpus"
    shutil.copytree(REVIEW_FIXTURE, corpus)
    _verify(corpus / "fixture-set" / "ground_truth.json", tmp_path)
    _freeze_review_corpus(corpus)
    return corpus


def _score_certification(corpus: Path, tmp_path: Path, work_root: Path) -> Any:
    return validate_module._score_certification(
        corpus_version="fx-v1",
        project_root=tmp_path / "project",
        test_ids=[REVIEW_SET_ID],
        prediction_sets=[],
        corpus_dir=corpus,
        work_root=work_root,
    )


@pytest.fixture
def stubbed_scoring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_score(truth_dir: Path, predictions_path: Path) -> Any:
        from types import SimpleNamespace

        seen["truth_dir"] = Path(truth_dir)
        seen["existed"] = Path(truth_dir).is_dir() and Path(predictions_path).is_file()
        return SimpleNamespace(certification=[])

    monkeypatch.setattr(validate_module, "score_corpus", fake_score)
    monkeypatch.setattr(certify_module, "build_prediction_document", lambda **_: {})
    monkeypatch.setattr(certify_module, "registered_targets", lambda _profile: [])
    return seen


@pytest.mark.parametrize("work_root_is", ["the temp folder", "a folder above the temp folder"])
def test_the_scratch_corpus_is_refused_before_anything_is_created_inside_the_work_root(
    frozen_review_corpus: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_scoring: dict[str, Any],
    work_root_is: str,
) -> None:
    temp = tmp_path / "machine" / "temp"
    temp.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    work_root = temp if work_root_is == "the temp folder" else temp.parent
    with pytest.raises(ValueError, match="inside the work folder") as refusal:
        _score_certification(frozen_review_corpus, tmp_path, work_root)
    assert "Choose a --work-root that is not the temporary folder" in str(refusal.value)
    assert list(temp.iterdir()) == []  # validated BEFORE any file or folder was created
    assert "truth_dir" not in stubbed_scoring


def test_the_scratch_corpus_lives_outside_the_work_root_and_is_removed(
    frozen_review_corpus: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_scoring: dict[str, Any],
) -> None:
    temp = tmp_path / "machine" / "temp"
    temp.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    entries = _score_certification(frozen_review_corpus, tmp_path, tmp_path / "work")
    assert stubbed_scoring["existed"] is True
    assert stubbed_scoring["truth_dir"].parent.parent == temp
    assert list(temp.iterdir()) == []  # removed after scoring
    assert all(entry.status == "provisional" for entry in entries)


@pytest.mark.parametrize(
    "temp_is",
    [
        "a corpus without a manifest",
        "a frozen corpus",
        "a set",
        "an ordinary folder inside a corpus without a manifest",
        "several levels inside a corpus without a manifest",
    ],
)
def test_the_scratch_corpus_is_never_created_inside_a_corpus(
    frozen_review_corpus: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_scoring: dict[str, Any],
    temp_is: str,
) -> None:
    other = tmp_path / "elsewhere" / "release-like"
    shutil.copytree(REVIEW_FIXTURE, other)  # <corpus>/fixture-set/ground_truth.json, no manifest
    temp = {
        "a corpus without a manifest": other,
        "a frozen corpus": frozen_review_corpus,
        "a set": other / "fixture-set",
        # Review P0 4: `release-1/temp-folder` holds no corpus file and no set, and neither does
        # any folder between it and the scratch -- only an ancestor walk finds the corpus root.
        "an ordinary folder inside a corpus without a manifest": other / "temp-folder",
        "several levels inside a corpus without a manifest": other / "a" / "b" / "c",
    }[temp_is]
    temp.mkdir(parents=True, exist_ok=True)  # existing, ordinary folders
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    before = (_snapshot(other), _snapshot(frozen_review_corpus))
    with pytest.raises(ValueError, match="it is inside the corpus") as refusal:
        _score_certification(frozen_review_corpus, tmp_path, tmp_path / "work")
    assert "Point the TEMP environment variable at an ordinary folder" in str(refusal.value)
    assert (_snapshot(other), _snapshot(frozen_review_corpus)) == before
    assert list(temp.glob("idea-calibration-validation-*")) == []  # nothing was created
    assert "truth_dir" not in stubbed_scoring


def test_calibration_validation_refuses_a_temp_work_root_before_it_opens_any_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp = tmp_path / "machine" / "temp"
    temp.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(temp))

    def reached(root: Path, mutate: bool) -> None:
        raise AssertionError(f"the corpus gateway was entered for {root} before the refusal")

    monkeypatch.setattr(truth_module, "_gateway_entered", reached)
    with pytest.raises(ValueError, match="inside the work folder"):
        asyncio.run(
            run_calibration_validation(
                corpus_version="fx-v1", project_root=tmp_path / "project", work_root=temp
            )
        )
    assert list(temp.iterdir()) == []
    assert not (tmp_path / "project").exists()


def test_refuse_scratch_destination_accepts_a_crowded_ordinary_temp_folder(tmp_path: Path) -> None:
    """The real temp folder holds thousands of entries; the bounded listing would fail closed."""

    crowded = tmp_path / "crowded"
    crowded.mkdir()
    for index in range(CORPUS_LISTING_LIMIT + 5):
        (crowded / f"entry-{index}").touch()
    scratch = crowded / "idea-calibration-validation-new"
    assert refuse_scratch_destination(scratch, work_root=tmp_path / "work") == scratch
    assert not scratch.exists()  # validation creates nothing
    scratch.mkdir()
    with pytest.raises(ValueError, match="something already exists there"):
        refuse_scratch_destination(scratch, work_root=tmp_path / "work")


# ==================================================================================================
# Certification's own outputs, and refusals that say what to do next
# ==================================================================================================


def test_certify_refuses_a_report_inside_a_corpus_before_it_opens_any_corpus(
    tmp_path: Path, gate_open: None
) -> None:
    corpus = tmp_path / "elsewhere" / "release-like"
    shutil.copytree(REVIEW_FIXTURE, corpus)  # no manifest: only the explicit guard catches this
    before = _snapshot(corpus)
    for out_path, message in (
        (corpus / "certification.json", "inside the corpus"),
        (tmp_path / "work" / "certification.json", "beneath the work tree"),
    ):
        with pytest.raises(ValueError, match=message):
            asyncio.run(
                run_certify(
                    corpus_version="fx-v1",
                    profile="free",
                    test_version="t1",
                    project_root=tmp_path / "project",
                    work_root=tmp_path / "work",
                    out_path=out_path,
                )
            )
    assert _snapshot(corpus) == before


def test_certify_refuses_a_registry_beneath_the_work_root_before_it_opens_any_corpus(
    tmp_path: Path, gate_open: None
) -> None:
    """The test-version registry is a third output; it is guarded like the other two (P1 6)."""

    work = tmp_path / "w"
    with pytest.raises(ValueError, match=r"registry\.json: it lies beneath the work tree"):
        asyncio.run(
            run_certify(
                corpus_version="fx-v1",
                profile="free",
                test_version="t1",
                project_root=work
                / "project",  # so data/local/certification/... is in the work tree
                work_root=work,
                out_path=tmp_path / "out" / "certification.json",
            )
        )
    assert not work.exists() and not (tmp_path / "out").exists()


def test_each_certify_artefact_is_revalidated_with_the_work_root_just_before_its_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    paths = {
        "predictions": tmp_path / "out" / "predictions-t1.json",
        "report": tmp_path / "out" / "certification-t1.json",
        "registry": tmp_path / "local" / "registry.json",
    }
    names = {path: name for name, path in paths.items()}
    calls: list[tuple[str, str, object]] = []
    monkeypatch.setattr(
        certify_module,
        "refuse_generated_output",
        lambda path, *, work_root=None: calls.append(("guard", names[path], work_root)),
    )
    monkeypatch.setattr(
        certify_module,
        "atomic_write_json",
        lambda path, value: calls.append(("write", names[path], None)),
    )
    monkeypatch.setattr(
        certify_module,
        "score_corpus",
        lambda corpus_dir, predictions, out_path=None: (
            calls.append(("score", "report", out_path)),
            SimpleNamespace(certification=[]),
        )[1],
    )
    work = tmp_path / "work"
    certify_module._publish_certification(
        {},  # type: ignore[arg-type]
        corpus_dir=tmp_path / "corpus",
        predictions_path=paths["predictions"],
        report_path=paths["report"],
        registry_path=paths["registry"],
        test_versions=["t1"],
        work_root=work,
    )
    assert calls == [
        ("guard", "predictions", work),
        ("write", "predictions", None),
        ("score", "report", None),  # the scorer no longer writes the report itself
        ("guard", "report", work),
        ("write", "report", None),
        ("guard", "registry", work),
        ("write", "registry", None),
    ]


@pytest.mark.parametrize(("n_certified", "verb"), [(0, "evaluated"), (3, "certified")])
def test_idea_benchmark_certify_says_certified_only_when_something_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n_certified: int, verb: str
) -> None:
    from typer.testing import CliRunner

    from id_detector import cli
    from id_detector.calibrate.certify import CertifyResult

    async def fake_run_certify(**_: Any) -> CertifyResult:
        return CertifyResult(
            report=None,  # type: ignore[arg-type]
            report_path=tmp_path / "certification.json",
            n_test_predictions=12,
            n_certified=n_certified,
        )

    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setattr(cli, "run_certify", fake_run_certify)
    result = CliRunner().invoke(
        cli.app,
        ["benchmark", "certify", "--corpus", "fx-v1", "--profile", "free", "--test-version", "t1"],
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"{verb} corpus=fx-v1 profile=free test_version=t1; ")
    assert f"certified_triples={n_certified};" in result.output
    assert ("certified corpus=" in result.output) is (n_certified > 0)


def test_certify_on_an_unfrozen_corpus_says_what_to_do_next(
    tmp_path: Path, gate_open: None
) -> None:
    project = tmp_path / "project"
    shutil.copytree(REVIEW_FIXTURE, project / "data" / "corpus" / "fx-v1")
    with pytest.raises(CorpusNotFrozen) as refusal:
        asyncio.run(
            run_certify(
                corpus_version="fx-v1",
                profile="free",
                test_version="t1",
                project_root=project,
                work_root=tmp_path / "work",
            )
        )
    message = str(refusal.value)
    assert "has not been frozen, so it cannot be certified" in message
    assert "checked by ear" in message
    assert "`idea truth freeze`" in message
    assert "score it as a draft" in message


def test_freezing_a_draft_corpus_is_refused_with_next_steps_and_changes_nothing(
    tmp_path: Path, gate_open: None
) -> None:
    corpus = tmp_path / "root" / "corpus"
    shutil.copytree(REVIEW_FIXTURE, corpus)  # every row is still a draft, like release-1
    before = _snapshot(corpus)
    with pytest.raises(ValueError, match="cannot freeze") as refusal:
        freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    message = str(refusal.value)
    assert "nothing was changed" in message
    assert "has not been checked by ear yet" in message
    assert "`idea truth review`" in message
    assert "is still draft" in message
    assert len(message.splitlines()) <= FREEZE_REFUSAL_LIMIT + 2  # capped, with "... and N more"
    assert _snapshot(corpus) == before
    assert not (corpus / "corpus-version.json").exists()


# ==================================================================================================
# The gate: CLOSED in production (the review of this follow-up found more to do), both states tested
# ==================================================================================================


def test_the_production_gate_stays_closed_and_a_whole_clean_corpus_is_not_certifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert truth_module.CERTIFICATION_ENABLED is False  # production: certification is off
    root = _mini_corpus(tmp_path / "corpus")
    _freeze_mini(root, ("mini-a", "mini-b"))
    whole = _run_list(tmp_path / "whole.json", [(root, "mini-a"), (root, "mini-b")])
    closed = _score(tmp_path, whole, "closed")  # the production default, not a fixture
    assert closed["l3"]["certifiable"] is False
    assert closed["l3"]["thresholds_met"] is not True
    assert closed["l3"]["certification"] == CERTIFICATION_DISABLED
    assert closed["l3"]["not_certifiable_because"] == [CERTIFICATION_DISABLED]
    assert closed["l3"]["scope"]["whole_frozen_corpus"] is True  # the scope verdict is still given
    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", True)  # the logic underneath
    assert _score(tmp_path, whole, "open")["l3"]["certifiable"] is True


def test_a_closed_gate_tells_the_owner_nothing_is_wrong_and_draft_scoring_still_works(
    tmp_path: Path, gate_closed: None, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out.json"
    assert score_main(["--run-list", str(MINI_RUN_LIST), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert f"NOT CERTIFIABLE: {CERTIFICATION_DISABLED}. " in printed
    assert "there is nothing to fix on your side" in printed
    assert "Scoring draft truth with scripts/score_corpus.py still works" in printed
