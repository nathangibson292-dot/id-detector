"""Regressions for the second-model (sol xhigh) review of the truth/corpus follow-up.

Each test was first run against the follow-up as it stood before this fix pass and failed there;
see ``docs/reviews/followup-truth-corpus.md`` § "Second-model review (sol xhigh) + fix pass".
Nothing touches ``data/corpus/``: every test works on temporary copies of test fixtures.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

import id_detector.truth as truth_module
import id_detector.truth_review as review_module
from id_detector import cli
from id_detector.benchmark.scorer import PredictionSet, score_corpus, truth_is_frozen_verified
from id_detector.contracts import GroundTruthRecord
from id_detector.io import atomic_write_json, read_text
from id_detector.truth import (
    CERTIFICATION_DISABLED,
    _annotation_path,
    freeze_truth,
    prediction_exposure,
    resolve_truth,
    second_pass_truth,
    seed_truth,
    verify_truth,
)
from id_detector.truth_review import TruthReviewSession, exposure_path
from scripts import audit_fixtures
from tests.conftest import write_corpus_fixture
from tests.test_stage2a_scorer import _config, _vector, _write_freeze_manifest
from tests.test_truth_corpus_followup import _audit_labels, _exposure_payload, _symlink
from tests.test_truth_review import _reveal_stub, _rows, _truth

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "truth-review"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    target = tmp_path / "corpus"
    shutil.copytree(FIXTURE, target)
    return target


def _verified(corpus: Path, tmp_path: Path) -> Path:
    truth_path, truth = _truth(corpus)
    TruthReviewSession(truth_path, work_root=tmp_path / "work").save({"rows": _rows(truth)})
    return truth_path


def _frozen(corpus: Path, tmp_path: Path) -> tuple[Path, GroundTruthRecord]:
    truth_path = _verified(corpus, tmp_path)
    freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    return truth_path, GroundTruthRecord.model_validate_json(read_text(truth_path))


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir()) if path.is_file()}


# --------------------------------------------------------------------------------------------
# sol B1 (P0) — every reveal re-asserts durable exposure evidence before answering.
# --------------------------------------------------------------------------------------------


def test_reveal_after_the_sidecar_was_deleted_rewrites_it_before_answering(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, _ = _truth(corpus)
    seen: list[bool] = []
    monkeypatch.setattr(
        review_module,
        "_predictions",
        lambda media_dir: (seen.append(exposure_path(truth_path).exists()), [])[1],
    )
    TruthReviewSession(truth_path, work_root=tmp_path / "work").reveal_predictions()
    reopened = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    assert reopened.predictions_visible is True
    exposure_path(truth_path).unlink()

    reopened.reveal_predictions()
    assert seen == [True, True]
    assert exposure_path(truth_path).exists()


# --------------------------------------------------------------------------------------------
# sol B2 (P0) — no first-pass writer can downgrade recorded exposure.
# --------------------------------------------------------------------------------------------


def _exposed_first_pass(corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    truth_path, truth = _truth(corpus)
    _reveal_stub(monkeypatch)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    session.reveal_predictions()
    session.save({"rows": _rows(truth)})
    exposure_path(truth_path).unlink()  # only the annotation's provenance still says so
    assert prediction_exposure(truth_path)["predictions_visible_during_review"] is True
    return truth_path


def test_cli_reverification_with_an_annotation_cannot_downgrade_exposure(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path = _exposed_first_pass(corpus, tmp_path, monkeypatch)
    annotation = tmp_path / "reverify.json"
    annotation.write_text(read_text(truth_path), encoding="utf-8")
    verify_truth(truth_path, annotator_ref="owner", annotation_path=annotation)
    assert prediction_exposure(truth_path)["predictions_visible_during_review"] is True
    payload = json.loads(read_text(_annotation_path(truth_path, "first")))
    assert payload["review_provenance"]["predictions_visible_during_review"] is True


def test_interactive_reverification_cannot_downgrade_exposure(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path = _exposed_first_pass(corpus, tmp_path, monkeypatch)

    def no_prompt(prompt: str) -> str:  # every row is already verified: nothing to ask
        raise AssertionError(prompt)

    verify_truth(truth_path, annotator_ref="owner", input_fn=no_prompt, output_fn=lambda _: None)
    assert prediction_exposure(truth_path)["predictions_visible_during_review"] is True


# --------------------------------------------------------------------------------------------
# sol B3 (P0) — the generic benchmark scorer (and `idea benchmark score`) never certifies
# exposed truth.
# --------------------------------------------------------------------------------------------


def _certifiable_corpus(tmp_path: Path) -> tuple[Path, Path]:
    """The stage-2a vector that certifies ``work/possible`` when every set is independent."""

    truth_root = tmp_path / "truth"
    prediction_sets = []
    truths = []
    for index in range(10):
        truth, predictions = _vector()
        set_id = f"test-set-{index:02d}"
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
    targets = [{"profile": "vector", "dimension": "work", "tier": "possible", "target_e4": 9_000}]
    snapshot, config_hash = _config(31, targets)
    predictions_path = tmp_path / "predictions.json"
    atomic_write_json(
        predictions_path,
        {
            "corpus_version": "vector-v1",
            "profile": "vector",
            "config_hash": config_hash,
            "config_snapshot": snapshot,
            "sets": prediction_sets,
            "unverified_seed_comparison": False,
        },
    )
    return truth_root, predictions_path


def test_generic_scorer_certifies_independent_truth_but_never_exposed_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_root, predictions = _certifiable_corpus(tmp_path)
    # Round 9: under the owner's moratorium the generic scorer never emits "certified"; every
    # entry carries the disabled message as its non-certified status instead.
    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", False)  # the moratorium
    gated = score_corpus(truth_root, predictions)
    assert {entry.status for entry in gated.certification} == {CERTIFICATION_DISABLED}
    # The gate-enabled variant keeps the logic underneath tested.
    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", True)
    control = score_corpus(truth_root, predictions)
    statuses = {(entry.dimension, entry.tier): entry.status for entry in control.certification}
    assert statuses[("work", "possible")] == "certified"  # the control: independent truth

    (truth_root / "test-set-03" / "review-exposure.json").write_text(
        json.dumps(_exposure_payload("test-set-03")), encoding="utf-8"
    )
    report = score_corpus(truth_root, predictions)
    assert {entry.status for entry in report.certification} == {"provisional"}

    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    out = tmp_path / "cli-report.json"
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
    document = json.loads(out.read_text("utf-8"))
    assert {entry["status"] for entry in document["certification"]} == {"provisional"}


# --------------------------------------------------------------------------------------------
# sol B4 (P0) — every frozen annotation pass and evidence file is re-verified, links refused.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper", ["delete", "alter", "link", "appear-second", "truth-link", "escaping-path"]
)
def test_frozen_annotation_passes_and_records_are_reverified(
    corpus: Path, tmp_path: Path, tamper: str
) -> None:
    truth_path, frozen = _frozen(corpus, tmp_path)
    assert truth_is_frozen_verified(truth_path, [frozen]) is True  # the control
    first = _annotation_path(truth_path, "first")
    if tamper == "delete":
        first.unlink()
    elif tamper == "alter":
        first.write_bytes(first.read_bytes() + b" ")
    elif tamper == "link":
        moved = tmp_path / "moved-annotation.json"
        shutil.move(str(first), str(moved))
        _symlink(first, moved)  # identical bytes, but no longer a record in the set
    elif tamper == "appear-second":
        _annotation_path(truth_path, "second").write_text("{}", encoding="utf-8")
    elif tamper == "truth-link":
        moved = tmp_path / "moved-truth.json"
        shutil.move(str(truth_path), str(moved))
        _symlink(truth_path, moved)
    else:
        outside = tmp_path / "outside" / "ground_truth.json"
        outside.parent.mkdir()
        shutil.copyfile(truth_path, outside)
        manifest_path = corpus / "corpus-version.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["sets"][0]["path"] = "../outside/ground_truth.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        truth_is_frozen_verified(truth_path, [frozen])


def test_linked_exposure_evidence_is_refused_even_with_identical_bytes(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, truth = _truth(corpus)
    _reveal_stub(monkeypatch)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    session.reveal_predictions()
    session.save({"rows": _rows(truth)})
    freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    frozen = GroundTruthRecord.model_validate_json(read_text(truth_path))
    moved = tmp_path / "moved-exposure.json"
    shutil.move(str(exposure_path(truth_path)), str(moved))
    _symlink(exposure_path(truth_path), moved)
    with pytest.raises(ValueError):
        truth_is_frozen_verified(truth_path, [frozen])


# --------------------------------------------------------------------------------------------
# sol B5 (P0) — seed and freeze write through the pinned, link-refusing, work-aware writer.
# --------------------------------------------------------------------------------------------


def test_freeze_refuses_a_linked_truth_record(corpus: Path, tmp_path: Path) -> None:
    real_set = tmp_path / "elsewhere" / "fixture-set"
    shutil.copytree(FIXTURE / "fixture-set", real_set)
    target = real_set / "ground_truth.json"
    record = GroundTruthRecord.model_validate_json(read_text(target))
    TruthReviewSession(target, work_root=tmp_path / "work").save({"rows": _rows(record)})
    before = target.read_bytes()
    linked_corpus = tmp_path / "linked-corpus"
    (linked_corpus / "fixture-set").mkdir(parents=True)
    _symlink(linked_corpus / "fixture-set" / "ground_truth.json", target)
    with pytest.raises(ValueError, match="link"):
        freeze_truth(
            linked_corpus,
            corpus_version="fx-v1",
            out_path=linked_corpus / "corpus-version.json",
        )
    assert target.read_bytes() == before
    assert not (linked_corpus / "corpus-version.json").exists()


def test_freeze_refuses_a_linked_manifest_destination(corpus: Path, tmp_path: Path) -> None:
    truth_path = _verified(corpus, tmp_path)
    before = truth_path.read_bytes()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    _symlink(corpus / "corpus-version.json", outside)
    with pytest.raises(ValueError, match="link"):
        freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    assert outside.read_text("utf-8") == "{}"
    assert truth_path.read_bytes() == before


def test_cli_freeze_refuses_a_corpus_beneath_the_work_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inside = tmp_path / "work" / "corpus"
    shutil.copytree(FIXTURE, inside)
    truth_path = inside / "fixture-set" / "ground_truth.json"
    record = GroundTruthRecord.model_validate_json(read_text(truth_path))
    TruthReviewSession(truth_path, work_root=tmp_path / "other-work").save({"rows": _rows(record)})
    before = truth_path.read_bytes()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    result = CliRunner().invoke(
        cli.app,
        [
            "truth",
            "freeze",
            "--truth",
            "work/corpus",
            "--corpus-version",
            "fx-v1",
            "--out",
            "work/corpus/corpus-version.json",
        ],
    )
    assert result.exit_code != 0, result.output
    assert truth_path.read_bytes() == before
    assert not (inside / "corpus-version.json").exists()


def test_seed_refuses_a_linked_destination(tmp_path: Path) -> None:
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text("00:10 Artist One - Track One\n", encoding="utf-8")
    outside = tmp_path / "work" / "prunable.json"
    outside.parent.mkdir()
    outside.write_text("{}", encoding="utf-8")
    link = tmp_path / "corpus" / "set" / "ground_truth.json"
    link.parent.mkdir(parents=True)
    _symlink(link, outside)
    with pytest.raises(ValueError, match="link"):
        seed_truth(
            out_path=link,
            set_id="linked-seed",
            duration_ms=60_000,
            media_key="c" * 64,
            tracklist=tracklist,
            project_root=tmp_path / "project",
        )
    assert outside.read_text("utf-8") == "{}"


# --------------------------------------------------------------------------------------------
# sol B6 (P0) — freeze holds the record's write lock across its exposure snapshot and publish.
# --------------------------------------------------------------------------------------------


def test_a_reveal_cannot_land_between_the_freeze_snapshot_and_its_manifest(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path = _verified(corpus, tmp_path)
    _reveal_stub(monkeypatch)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    outcome: dict[str, object] = {}

    def reveal() -> None:
        try:
            session.reveal_predictions()
            outcome["revealed"] = True
        except ValueError as exc:
            outcome["refused"] = str(exc)

    real_snapshot = truth_module.prediction_exposure
    racer = threading.Thread(target=reveal, daemon=True)

    def snapshot_then_race(path: Path) -> dict[str, object]:
        result = real_snapshot(path)
        if not racer.is_alive() and not outcome:
            racer.start()
            racer.join(timeout=1.5)  # a lock-free freeze lets the reveal finish right here
        return result

    monkeypatch.setattr(truth_module, "prediction_exposure", snapshot_then_race)
    manifest = freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    racer.join(timeout=60)
    assert not racer.is_alive()
    exposed_in_manifest = manifest["sets"][0]["prediction_exposure"][
        "predictions_visible_during_review"
    ]
    # Either the reveal was serialised after the freeze (and refused, the set being frozen) or
    # the manifest records it; an "independent" manifest beside a revealed set is the bug.
    assert not (outcome.get("revealed") and not exposed_in_manifest)
    assert exposure_path(truth_path).exists() == bool(exposed_in_manifest)
    assert "frozen" in str(outcome.get("refused", ""))


# --------------------------------------------------------------------------------------------
# sol B7 (P0) — forward-only state through every CLI pass entry point.
# --------------------------------------------------------------------------------------------


def _two_pass_set(tmp_path: Path, *, resolve: bool) -> tuple[Path, Path, Path]:
    """The stage-2a state machine: seed -> first pass -> disagreeing second pass (-> resolve)."""

    truth_dir = tmp_path / "truth" / "set-one"
    truth_dir.mkdir(parents=True)
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text(
        "00:10 Artist One - Track One\n01:00 Artist Two - Track Two\n", encoding="utf-8"
    )
    truth_path = truth_dir / "ground_truth.json"
    seeded = seed_truth(
        out_path=truth_path,
        set_id="set-one",
        duration_ms=120_000,
        media_key="a" * 64,
        tracklist=tracklist,
        split="test",
        project_root=tmp_path / "project",
    )
    first_payload = seeded.model_dump(mode="json")
    for episode, start, end in zip(
        first_payload["episodes"],
        ([9_000, 11_000], [59_000, 61_000]),
        ([59_000, 61_000], [119_000, 120_000]),
        strict=True,
    ):
        episode.update(
            {
                "verified_against": "audio",
                "start_ms_range": start,
                "end_ms_range": end,
                "role_segments": [{"from_ms": start[0], "to_ms": end[1], "role": "dominant"}],
                "draft": False,
            }
        )
    first_annotation = tmp_path / "first.json"
    atomic_write_json(first_annotation, first_payload)
    verify_truth(truth_path, annotator_ref="annotator-first", annotation_path=first_annotation)
    second_payload = json.loads(json.dumps(first_payload))
    second_payload["episodes"][1].update(
        {
            "start_ms_range": [60_000, 62_000],
            "end_ms_range": [118_000, 120_000],
            "role_segments": [{"from_ms": 60_000, "to_ms": 120_000, "role": "dominant"}],
        }
    )
    second_annotation = tmp_path / "second.json"
    atomic_write_json(second_annotation, second_payload)
    second_pass_truth(
        truth_path, annotator_ref="annotator-second", annotation_path=second_annotation
    )
    if resolve:
        resolve_truth(truth_path, resolver_ref="annotator-third", annotation_path=second_annotation)
    return truth_path, first_annotation, second_annotation


@pytest.mark.parametrize("resolved", [True, False], ids=["resolved", "second-pass"])
@pytest.mark.parametrize(
    "entry", ["verify-annotation", "verify-interactive", "second-pass", "resolve"]
)
def test_no_cli_pass_can_step_truth_backwards(tmp_path: Path, resolved: bool, entry: str) -> None:
    truth_path, first_annotation, second_annotation = _two_pass_set(tmp_path, resolve=resolved)
    before = _snapshot(truth_path.parent)

    def no_prompt(prompt: str) -> str:
        raise AssertionError(prompt)

    if entry == "resolve" and not resolved:
        pytest.skip("resolving an unresolved disagreement is the forward transition")
    with pytest.raises(ValueError, match="already"):
        if entry == "verify-annotation":
            verify_truth(
                truth_path, annotator_ref="annotator-first", annotation_path=first_annotation
            )
        elif entry == "verify-interactive":
            verify_truth(
                truth_path,
                annotator_ref="annotator-first",
                input_fn=no_prompt,
                output_fn=lambda _: None,
            )
        elif entry == "second-pass":
            second_pass_truth(
                truth_path, annotator_ref="annotator-fourth", annotation_path=second_annotation
            )
        else:
            resolve_truth(
                truth_path, resolver_ref="annotator-fifth", annotation_path=first_annotation
            )
    assert _snapshot(truth_path.parent) == before


# --------------------------------------------------------------------------------------------
# sol B8 (P1) — the freeze manifest must land where frozen status is discovered.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "where", ["off-tree", "wrong-name"], ids=["manifest-off-tree", "manifest-wrong-name"]
)
def test_freeze_refuses_a_manifest_that_frozen_status_could_not_find(
    corpus: Path, tmp_path: Path, where: str
) -> None:
    truth_path = _verified(corpus, tmp_path)
    before = truth_path.read_bytes()
    out = (
        tmp_path / "elsewhere" / "deep" / "er" / "corpus-version.json"
        if where == "off-tree"
        else corpus / "frozen-manifest.json"
    )
    with pytest.raises(ValueError, match="manifest"):
        freeze_truth(corpus, corpus_version="fx-v1", out_path=out)
    assert truth_path.read_bytes() == before
    assert not out.exists()
    TruthReviewSession(truth_path, work_root=tmp_path / "work")  # still not frozen, still open


# --------------------------------------------------------------------------------------------
# sol B9 (P1) — invisible characters anywhere in the furniture prefix.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "artist",
    [
        "-​ Mall Grab",  # between the marker and its whitespace
        "0:17:09​ - Mall Grab",  # between the timestamp and its whitespace
        "0:17:09 ⁠- Mall Grab",  # between the whitespace and the marker
        "0:17:09 -‍ Mall Grab",
        "−﻿ Mall Grab",
        "0​:17:09 - Mall Grab",  # inside the timestamp
    ],
)
def test_invisible_characters_inside_the_furniture_prefix_are_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artist: str
) -> None:
    failures = _audit_labels(tmp_path, monkeypatch, artist)
    assert len(failures) == 1, failures


def test_audit_still_passes_real_names_and_the_committed_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive controls (they pass before and after): never evidence of the fix on their own."""

    for artist in ("*NSYNC", "-M-", "Mall‍Grab"):
        assert _audit_labels(tmp_path / artist.encode().hex(), monkeypatch, artist) == []
    monkeypatch.undo()
    assert audit_fixtures.audit() == []


# Round 9: these tests exercise freezing and certification underneath the owner's moratorium, so the
# single gate is opened for them by fixture (production keeps it closed).  Tests that assert the
# moratorium itself close it again explicitly.
pytestmark = pytest.mark.usefixtures("certification_gate_open")
