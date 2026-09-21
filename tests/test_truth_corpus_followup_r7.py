"""Regressions for the round-6 review, closed in the round-7 fix pass by three exact rules.

Class 1 -- a mutation never lands in the wrong corpus.  Every target sits at exact depth
(``<root>/<set>/ground_truth.json``, or a corpus-level file directly in ``<root>``), and
``open_corpus`` refuses a root reached through a link, beneath the work tree, or inside another
corpus -- before ``path_key``, the lock or any manifest read, and again under the lock.

Class 2 -- verified, independent and certified status need the loaded inventory
``{(set_id, relative path)}`` to equal the frozen manifest's exactly; missing and surplus
entries are refused by name.

Class 3 -- every public corpus path enters the gateway, proved by a test seam in
``open_corpus`` that raises, and readers take only the gateway's vetted lists.

Nothing here sleeps, and nothing touches ``data/corpus/`` or ``work/``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import id_detector.benchmark.controlled as controlled_module
import id_detector.calibrate.certify as certify_module
import id_detector.calibrate.validate as validate_module
import id_detector.truth as truth_module
from id_detector.benchmark import corpus as corpus_module
from id_detector.benchmark.ablations import run_ablations
from id_detector.benchmark.controlled import CONTROLLED_RENDER_MARKER, render_controlled
from id_detector.benchmark.hints import run_hint_gate
from id_detector.benchmark.scorer import (
    corpus_independent,
    find_freeze_manifest,
    frozen_prediction_exposure,
    load_truth_directory,
    load_truth_files,
    score_corpus,
    truth_is_frozen_verified,
)
from id_detector.benchmark.shortlist import run_shortlist
from id_detector.benchmark.transforms_schedule import run_transform_schedule_benchmark
from id_detector.calibrate.certify import (
    CorpusNotIndependent,
    _require_frozen,
    _require_independent,
)
from id_detector.calibrate.validate import run_calibration_validation, validation_report_path
from id_detector.contracts import GroundTruthRecord
from id_detector.io import read_text, sha256_file
from id_detector.truth import (
    exact_corpus_root,
    freeze_truth,
    open_corpus,
    record_exposure_in_ledger,
    resolve_truth,
    second_pass_truth,
    seed_truth,
    truth_write_lock,
    verify_truth,
    write_draft_manifest,
)
from id_detector.truth_paths import is_within
from id_detector.truth_review import TruthReviewSession, find_truth_path
from tests.test_truth_corpus_followup_r3 import _junction
from tests.test_truth_review import _rows

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "truth-review"
SET_ID = "fixture-truth-review"
GUIDANCE = "Pass the resolved real path instead"
INSIDE = "inside another corpus"
WORK_TREE = "beneath the work tree"
NOT_EXACT = re.escape("not exactly <corpus>/<set>/ground_truth.json")


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    target = tmp_path / "root" / "corpus"
    shutil.copytree(FIXTURE, target)
    return target


def _record(path: Path) -> GroundTruthRecord:
    return GroundTruthRecord.model_validate_json(read_text(path))


def _verify(truth_path: Path, tmp_path: Path) -> None:
    TruthReviewSession(truth_path, work_root=tmp_path / "work").save(
        {"rows": _rows(_record(truth_path))}
    )


def _freeze(corpus: Path) -> None:
    freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")


def _tracklist(tmp_path: Path) -> Path:
    path = tmp_path / "tracklist.txt"
    path.write_text("Artist - Title\n", encoding="utf-8")
    return path


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _seed(out_path: Path, tmp_path: Path, **extra: Any) -> GroundTruthRecord:
    return seed_truth(
        out_path=out_path,
        set_id="new-set",
        duration_ms=60_000,
        media_key="a" * 64,
        tracklist=_tracklist(tmp_path),
        project_root=tmp_path / "project",
        **extra,
    )


def _copy_set(corpus: Path, directory: str, set_id: str) -> Path:
    """Add a second set by hand (as an owner would), with its own set_id."""

    target = corpus / directory
    shutil.copytree(corpus / "fixture-set", target)
    payload = json.loads(read_text(target / "ground_truth.json"))
    payload["set_id"] = set_id
    (target / "ground_truth.json").write_text(json.dumps(payload), encoding="utf-8")
    return target / "ground_truth.json"


def _dummy_sources(tmp_path: Path) -> Path:
    """Three files with an audio suffix: enough for the renderer to reach its target checks."""

    sources = tmp_path / "sources"
    sources.mkdir(exist_ok=True)
    for index in range(3):
        (sources / f"source-{index}.wav").write_bytes(b"RIFF")
    return sources


def _render(sources: Path, out: Path, tmp_path: Path, **extra: Any) -> Any:
    return asyncio.run(
        render_controlled(sources, out, seed=1, audio_dir=tmp_path / "rendered-audio", **extra)
    )


# ==================================================================================================
# Class 1 -- exact depth and ancestor refusal: no mutation lands in the wrong corpus
# ==================================================================================================


def test_a_seed_one_level_too_deep_in_a_frozen_corpus_is_refused_as_inside_another_corpus(
    corpus: Path, tmp_path: Path
) -> None:
    _verify(corpus / "fixture-set" / "ground_truth.json", tmp_path)
    _freeze(corpus)
    before = _snapshot(corpus)
    # Round 9: seeding creates only the set directory, so a missing root is refused first.
    with pytest.raises(ValueError, match=f"{INSIDE}|does not exist"):
        _seed(corpus / "new-group" / "new-set" / "ground_truth.json", tmp_path)
    assert _snapshot(corpus) == before
    assert not (corpus / "new-group").exists()


def test_a_seed_one_level_too_deep_in_an_unfrozen_corpus_is_refused(
    corpus: Path, tmp_path: Path
) -> None:
    before = _snapshot(corpus)
    # Round 9: seeding creates only the set directory, so a missing root is refused first.
    with pytest.raises(ValueError, match=f"{INSIDE}|does not exist"):
        _seed(corpus / "new-group" / "new-set" / "ground_truth.json", tmp_path)
    assert _snapshot(corpus) == before


def _nested_calls(tmp_path: Path) -> list[tuple[str, Callable[[Path, Path], object]]]:
    work = tmp_path / "work"
    return [
        ("verify", lambda nested, group: verify_truth(nested, annotator_ref="owner")),
        ("second pass", lambda nested, group: second_pass_truth(nested, annotator_ref="second")),
        ("resolve", lambda nested, group: resolve_truth(nested, annotator_ref="third")),
        (
            "ledger append",
            lambda nested, group: record_exposure_in_ledger(
                nested, set_id="nested", when="2026-09-15T00:00:00Z"
            ),
        ),
        ("record lock", lambda nested, group: _enter(truth_write_lock(nested))),
        ("review load", lambda nested, group: TruthReviewSession(nested, work_root=work)),
        (
            "freeze",
            lambda nested, group: freeze_truth(
                group, corpus_version="fx-v1", out_path=group / "corpus-version.json"
            ),
        ),
        ("draft", lambda nested, group: write_draft_manifest(group, corpus_version="draft")),
    ]


def _enter(manager: Any) -> None:
    with manager:
        pass


@pytest.mark.parametrize("operation", [name for name, _ in _nested_calls(Path("."))])
def test_every_mutation_refuses_a_record_nested_inside_a_frozen_corpus(
    corpus: Path, tmp_path: Path, operation: str
) -> None:
    _verify(corpus / "fixture-set" / "ground_truth.json", tmp_path)
    _freeze(corpus)
    # An earlier accident left a nested set inside the frozen corpus; nothing may lock or write it
    # as though its grandparent were a corpus of its own.
    group = corpus / "group"
    shutil.copytree(corpus / "fixture-set", group / "nested-set")
    nested = group / "nested-set" / "ground_truth.json"
    before = _snapshot(corpus)
    call = dict(_nested_calls(tmp_path))[operation]
    with pytest.raises(ValueError, match=INSIDE):
        call(nested, group)
    assert _snapshot(corpus) == before


def test_an_explicit_corpus_root_must_be_exactly_the_targets_grandparent(
    corpus: Path, tmp_path: Path
) -> None:
    before = _snapshot(corpus)
    with pytest.raises(ValueError, match=NOT_EXACT):
        _seed(corpus / "a" / "b" / "ground_truth.json", tmp_path, corpus_root=corpus)
    with pytest.raises(ValueError, match=NOT_EXACT):
        verify_truth(
            corpus / "fixture-set" / "ground_truth.json",
            corpus_root=corpus / "fixture-set",
            annotator_ref="owner",
        )
    assert _snapshot(corpus) == before
    assert exact_corpus_root(corpus / "fixture-set" / "ground_truth.json", corpus_root=corpus) == (
        corpus
    )


def test_the_gateway_refuses_a_linked_root_before_path_key_the_lock_or_a_manifest_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real" / "corpus"
    shutil.copytree(FIXTURE, real)
    _junction(tmp_path / "linked-corpus", real)
    _junction(tmp_path / "linked-parent", real.parent)
    calls: list[str] = []

    def recording(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return function(*args, **kwargs)

        return wrapped

    for name in ("path_key", "corpus_write_lock", "corpus_manifest_frozen", "read_text"):
        monkeypatch.setattr(truth_module, name, recording(name, getattr(truth_module, name)))
    for mutate in (True, False):
        for root in (tmp_path / "linked-corpus", tmp_path / "linked-parent" / "corpus"):
            with pytest.raises(ValueError, match=GUIDANCE), open_corpus(root, mutate=mutate):
                pytest.fail("the gateway yielded a linked root")
    assert calls == []


def test_the_gateway_refuses_a_root_beneath_the_work_root_for_reads_and_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    shutil.copytree(FIXTURE, work / "corpus")
    locks: list[Path] = []
    real_lock = truth_module.corpus_write_lock

    @contextmanager
    def recording_lock(root: Path, *, timeout: float = 20.0) -> Iterator[None]:
        locks.append(root)
        with real_lock(root, timeout=timeout):
            yield

    monkeypatch.setattr(truth_module, "corpus_write_lock", recording_lock)
    for mutate in (True, False):
        with (
            pytest.raises(ValueError, match=WORK_TREE),
            open_corpus(work / "corpus", mutate=mutate, work_root=work),
        ):
            pytest.fail("the gateway yielded a root beneath the work tree")
    assert locks == []


def test_the_gateway_repeats_the_ancestor_refusal_under_the_lock(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent that becomes a corpus between the pre-check and the lock is still refused."""

    real_lock = truth_module.corpus_write_lock

    @contextmanager
    def lock_then_parent_becomes_a_corpus(root: Path, *, timeout: float = 20.0) -> Iterator[None]:
        with real_lock(root, timeout=timeout):
            (Path(root).parent / "corpus-version.json").write_text("{}", encoding="utf-8")
            yield

    monkeypatch.setattr(truth_module, "corpus_write_lock", lock_then_parent_becomes_a_corpus)
    with pytest.raises(ValueError, match=INSIDE), open_corpus(corpus, mutate=True):
        pytest.fail("the gateway yielded after the parent became a corpus under the lock")


def test_controlled_render_refuses_an_output_inside_another_corpus(
    corpus: Path, tmp_path: Path
) -> None:
    before = _snapshot(corpus)
    with pytest.raises(ValueError, match=INSIDE):
        _render(_dummy_sources(tmp_path), corpus / "new-controlled", tmp_path)
    assert _snapshot(corpus) == before
    assert not (corpus / "new-controlled").exists()


def test_controlled_render_refuses_an_output_beneath_the_work_root(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match=WORK_TREE):
        _render(_dummy_sources(tmp_path), work / "controlled", tmp_path, work_root=work)
    assert list(work.iterdir()) == []


def _marker_payload(target: Path) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "corpus_version": "controlled-test",
        "set_count": 1,
        "sets": [
            {
                "set_id": "fixture-set",
                "ground_truth_sha256": sha256_file(target / "fixture-set" / "ground_truth.json"),
            }
        ],
    }


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "no render_manifest.json"),
        ("not json", "is not valid JSON"),
        ("wrong schema", "is not a render manifest"),
        ("copied from another corpus", "does not name exactly this target's set directories"),
        ("stale truth hash", "does not match the truth of fixture-set"),
    ],
)
def test_the_controlled_marker_must_be_bound_to_exactly_this_target(
    tmp_path: Path, case: str, message: str
) -> None:
    target = tmp_path / "target"
    shutil.copytree(FIXTURE, target)
    marker = target / CONTROLLED_RENDER_MARKER
    payload = _marker_payload(target)
    if case == "not json":
        marker.write_text("not json", encoding="utf-8")
    elif case == "wrong schema":
        marker.write_text(json.dumps({"sets": "nope"}), encoding="utf-8")
    elif case == "copied from another corpus":
        payload["sets"] = [{"set_id": "controlled-001-length-3s", "ground_truth_sha256": "0" * 64}]
        marker.write_text(json.dumps(payload), encoding="utf-8")
    elif case == "stale truth hash":
        payload["sets"][0]["ground_truth_sha256"] = "0" * 64
        marker.write_text(json.dumps(payload), encoding="utf-8")
    before = _snapshot(target)
    with pytest.raises(ValueError, match=re.escape(message)):
        _render(_dummy_sources(tmp_path), target, tmp_path)
    assert _snapshot(target) == before


def test_a_linked_controlled_marker_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    shutil.copytree(FIXTURE, target)
    real_marker = tmp_path / "elsewhere.json"
    real_marker.write_text(json.dumps(_marker_payload(target)), encoding="utf-8")
    try:
        (target / CONTROLLED_RENDER_MARKER).symlink_to(real_marker)
    except OSError:  # pragma: no cover - Windows without symlink rights
        pytest.skip("this account cannot create file symlinks")
    with pytest.raises(ValueError, match=f"symlink or junction|{GUIDANCE}"):
        _render(_dummy_sources(tmp_path), target, tmp_path)


def test_a_marker_bound_to_this_target_authorises_replacement(tmp_path: Path) -> None:
    target = tmp_path / "target"
    shutil.copytree(FIXTURE, target)
    (target / CONTROLLED_RENDER_MARKER).write_text(
        json.dumps(_marker_payload(target)), encoding="utf-8"
    )
    with open_corpus(target, mutate=False) as handle:
        controlled_module._refuse_controlled_overwrite(handle)  # does not raise


def test_calibration_validation_report_is_never_written_inside_a_corpus(
    corpus: Path, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    assert validation_report_path(project, "fx-v1", None) == (
        project / "data" / "local" / "calibration" / "fx-v1" / "calibration-validation.json"
    )
    for inside in (
        corpus / "calibration-validation.json",
        corpus / "fixture-set" / "calibration-validation.json",
    ):
        with pytest.raises(ValueError, match="inside the corpus"):
            validation_report_path(project, "fx-v1", inside)


def test_calibration_scores_a_scratch_corpus_outside_work_and_every_corpus(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verify(corpus / "fixture-set" / "ground_truth.json", tmp_path)
    _freeze(corpus)
    seen: dict[str, Any] = {}

    def fake_score(truth_dir: Path, predictions_path: Path) -> SimpleNamespace:
        seen["truth_dir"] = Path(truth_dir)
        with open_corpus(truth_dir, mutate=False) as handle:
            seen["inventory"] = [
                path.relative_to(handle.root).as_posix() for path in handle.truth_files
            ]
            manifest = json.loads(read_text(handle.manifest_path))
            seen["manifest"] = [item["path"] for item in manifest["sets"]]
        return SimpleNamespace(certification=[])

    monkeypatch.setattr(validate_module, "score_corpus", fake_score)
    monkeypatch.setattr(certify_module, "build_prediction_document", lambda **_: {})
    monkeypatch.setattr(certify_module, "registered_targets", lambda _profile: [])
    # The certification follow-up: the configured work root is passed in and the scratch
    # destination validated against it (tests/test_certification_followup.py).
    assert "work_root" in inspect.signature(validate_module._score_certification).parameters
    entries = validate_module._score_certification(
        corpus_version="fx-v1",
        project_root=tmp_path / "project",
        test_ids=[SET_ID],
        prediction_sets=[],
        corpus_dir=corpus,
        work_root=tmp_path / "work",
    )
    truth_dir = seen["truth_dir"]
    assert is_within(truth_dir, tempfile.gettempdir())
    assert not is_within(truth_dir, corpus)
    assert seen["inventory"] == seen["manifest"] == [f"{SET_ID}/ground_truth.json"]
    assert not truth_dir.exists()  # the scratch corpus is removed after scoring
    assert all(entry.status == "provisional" for entry in entries)


# ==================================================================================================
# Class 2 -- verified, independent and certified need exactly the frozen population
# ==================================================================================================


def test_deleting_an_exposed_set_from_a_frozen_corpus_makes_certification_impossible(
    corpus: Path, tmp_path: Path
) -> None:
    exposed = corpus / "fixture-set" / "ground_truth.json"
    other = _copy_set(corpus, "other-set", "fixture-other-set")
    session = TruthReviewSession(exposed, work_root=tmp_path / "work")
    session.reveal_predictions()
    session.save({"rows": _rows(_record(exposed))})
    _verify(other, tmp_path)
    _freeze(corpus)
    truths = load_truth_directory(corpus)
    assert truth_is_frozen_verified(corpus, truths) is True
    assert corpus_independent(corpus, truths) is False
    with pytest.raises(CorpusNotIndependent):
        _require_independent(corpus, "fx-v1")

    shutil.rmtree(corpus / "fixture-set")  # the exposed set disappears from the frozen corpus
    remaining = load_truth_directory(corpus)
    assert [truth.set_id for truth in remaining] == ["fixture-other-set"]
    missing = re.escape(f"missing {SET_ID} (fixture-set/ground_truth.json)")
    with pytest.raises(ValueError, match=missing):
        truth_is_frozen_verified(corpus, remaining)
    with pytest.raises(ValueError, match=missing):
        truth_is_frozen_verified(other, remaining)
    with pytest.raises(ValueError, match=missing):
        corpus_independent(corpus, remaining)
    with pytest.raises(ValueError, match=missing):
        _require_frozen(corpus, "fx-v1")


def test_a_set_added_to_a_frozen_corpus_is_refused_as_surplus(corpus: Path, tmp_path: Path) -> None:
    _verify(corpus / "fixture-set" / "ground_truth.json", tmp_path)
    _freeze(corpus)
    _copy_set(corpus, "late-set", "fixture-late-set")
    truths = load_truth_directory(corpus)
    surplus = re.escape("surplus fixture-late-set (late-set/ground_truth.json)")
    with pytest.raises(ValueError, match=surplus):
        truth_is_frozen_verified(corpus, truths)
    with pytest.raises(ValueError, match=surplus):
        _require_frozen(corpus, "fx-v1")


# ==================================================================================================
# Class 3 -- every public corpus path enters the gateway
# ==================================================================================================


class _GatewayReached(Exception):
    pass


def _gateway_reached(root: Path, mutate: bool) -> None:
    raise _GatewayReached(f"{root} mutate={mutate}")


def _script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"r7_script_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class _Env:
    tmp: Path
    corpus: Path
    truth_path: Path
    project: Path
    version: str
    session: TruthReviewSession

    @property
    def work(self) -> Path:
        return self.tmp / "work"


@pytest.fixture
def env(corpus: Path, tmp_path: Path) -> _Env:
    version = "fx-probe"
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project / "data" / "corpus" / version)
    truth_path = corpus / "fixture-set" / "ground_truth.json"
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    return _Env(tmp_path, corpus, truth_path, project, version, session)


PUBLIC_ENTRY_POINTS: list[tuple[str, Callable[[_Env], object]]] = [
    ("truth.seed_truth", lambda e: _seed(e.corpus / "new-set" / "ground_truth.json", e.tmp)),
    ("truth.verify_truth", lambda e: verify_truth(e.truth_path, annotator_ref="owner")),
    ("truth.second_pass_truth", lambda e: second_pass_truth(e.truth_path, annotator_ref="s")),
    ("truth.resolve_truth", lambda e: resolve_truth(e.truth_path, annotator_ref="t")),
    (
        "truth.freeze_truth",
        lambda e: freeze_truth(
            e.corpus, corpus_version="fx-v1", out_path=e.corpus / "corpus-version.json"
        ),
    ),
    ("truth.write_draft_manifest", lambda e: write_draft_manifest(e.corpus, corpus_version="d")),
    (
        "truth.record_exposure_in_ledger",
        lambda e: record_exposure_in_ledger(e.truth_path, set_id="x", when="2026-09-15T00:00:00Z"),
    ),
    ("truth.truth_write_lock", lambda e: _enter(truth_write_lock(e.truth_path))),
    ("truth_review.find_truth_path", lambda e: find_truth_path(e.corpus, SET_ID, work_root=e.work)),
    (
        "truth_review.TruthReviewSession",
        lambda e: TruthReviewSession(e.truth_path, work_root=e.work),
    ),
    (
        "truth_review.TruthReviewSession.save",
        lambda e: e.session.save({"rows": _rows(_record(e.truth_path))}),
    ),
    ("truth_review.TruthReviewSession.reveal", lambda e: e.session.reveal_predictions()),
    ("scorer.load_truth_files(corpus)", lambda e: load_truth_files(e.corpus)),
    ("scorer.load_truth_files(record)", lambda e: load_truth_files(e.truth_path)),
    ("scorer.load_truth_directory", lambda e: load_truth_directory(e.corpus)),
    ("scorer.find_freeze_manifest", lambda e: find_freeze_manifest(e.corpus)),
    (
        "scorer.frozen_prediction_exposure",
        lambda e: frozen_prediction_exposure(e.truth_path, SET_ID),
    ),
    ("scorer.truth_is_frozen_verified", lambda e: truth_is_frozen_verified(e.corpus, [])),
    ("scorer.corpus_independent", lambda e: corpus_independent(e.corpus, [])),
    ("scorer.score_corpus", lambda e: score_corpus(e.corpus, e.tmp / "predictions.json")),
    ("certify._require_frozen", lambda e: _require_frozen(e.corpus, "fx-v1")),
    ("certify._require_independent", lambda e: _require_independent(e.corpus, "fx-v1")),
    # run_certify is refused before it opens any corpus while certification is disabled; that
    # refusal is tested in test_truth_corpus_followup_r8.
    (
        "corpus.run_corpus",
        lambda e: asyncio.run(
            corpus_module.run_corpus(
                corpus_version=e.version,
                profile="free",
                out_path=e.tmp / "run.json",
                project_root=e.project,
                work_root=e.work,
            )
        ),
    ),
    (
        "shortlist.run_shortlist",
        lambda e: asyncio.run(
            run_shortlist(
                corpus_version=e.version,
                out_path=e.tmp / "shortlist.json",
                project_root=e.project,
                work_root=e.work,
                app_config=None,  # type: ignore[arg-type]
                cli_confirmation=False,
            )
        ),
    ),
    (
        "hints.run_hint_gate",
        lambda e: asyncio.run(
            run_hint_gate(
                corpus_version=e.version,
                out_path=e.tmp / "hints.json",
                project_root=e.project,
                work_root=e.work,
            )
        ),
    ),
    (
        "ablations.run_ablations",
        lambda e: run_ablations(
            corpus_version=e.version,
            out_path=e.tmp / "ablations.json",
            project_root=e.project,
            work_root=e.work,
        ),
    ),
    (
        "transforms_schedule.run_transform_schedule_benchmark",
        lambda e: run_transform_schedule_benchmark(
            corpus_version=e.version,
            out_path=e.tmp / "schedule.json",
            project_root=e.project,
            work_root=e.work,
        ),
    ),
    (
        "validate.run_calibration_validation",
        lambda e: asyncio.run(
            run_calibration_validation(
                corpus_version=e.version, project_root=e.project, work_root=e.work
            )
        ),
    ),
    (
        "controlled.render_controlled",
        lambda e: _render(_dummy_sources(e.tmp), e.tmp / "rendered", e.tmp),
    ),
    (
        "scripts/score_corpus.truth_status",
        lambda e: _script("score_corpus").truth_status(e.truth_path, _record(e.truth_path)),
    ),
    (
        "scripts/score_corpus.truth_independent",
        lambda e: _script("score_corpus").truth_independent(e.truth_path, _record(e.truth_path)),
    ),
]


@pytest.mark.parametrize(
    "entry",
    [call for _, call in PUBLIC_ENTRY_POINTS],
    ids=[name for name, _ in PUBLIC_ENTRY_POINTS],
)
def test_every_public_corpus_entry_point_calls_open_corpus(
    env: _Env, entry: Callable[[_Env], object], monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus_before = _snapshot(env.corpus)
    project_before = _snapshot(env.project)
    monkeypatch.setattr(truth_module, "_gateway_entered", _gateway_reached)
    with pytest.raises(_GatewayReached):
        entry(env)
    assert _snapshot(env.corpus) == corpus_before
    assert _snapshot(env.project) == project_before


def test_the_audit_reads_corpora_through_the_gateway_and_refuses_links_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = _script("audit_fixtures")
    repository = tmp_path / "repository"
    fixtures = repository / "fixtures"
    shutil.copytree(FIXTURE, fixtures / "truth-review")
    plain = fixtures / "plain"
    plain.mkdir()
    (plain / "note.txt").write_text("plain fixture\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "outside.txt").write_text("outside\n", encoding="utf-8")
    _junction(plain / "linked", elsewhere)
    monkeypatch.setattr(audit, "ROOT", repository)
    monkeypatch.setattr(audit, "SCAN_ROOTS", (fixtures,))
    monkeypatch.setattr(audit, "RAW_ROOT", repository / "raw" / "comments")
    failures = audit.audit()
    expected = (
        f"{Path('fixtures') / 'plain' / 'linked'}: is a symlink or junction; the audit refuses "
        "links (pass the resolved real path instead)"
    )
    assert [failure for failure in failures if "symlink or junction" in failure] == [expected]
    monkeypatch.setattr(truth_module, "_gateway_entered", _gateway_reached)
    with pytest.raises(_GatewayReached):
        audit.audit()


# Round 9: these tests exercise freezing and certification underneath the owner's moratorium, so the
# single gate is opened for them by fixture (production keeps it closed).  Tests that assert the
# moratorium itself close it again explicitly.
pytestmark = pytest.mark.usefixtures("certification_gate_open")
