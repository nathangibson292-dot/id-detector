"""Regressions for the round-9 review, fixed in round 10 (the final fix round of this cycle).

1. Every positive certification claim is gated: no emitter says certified, certifiable, or
   thresholds met while ``truth.CERTIFICATION_ENABLED`` is false.
2. Explicit output guards on the remaining writers, plus a bounded backstop under ``io``'s atomic
   writers that refuses corpus files and corpus directories.
3. An existing root with no set, manifest or ledger is a new root: the seed retry is refused.
4. A new root's parent is listed once per mutation, under the corpus lock.
5. Each run module's final publication revalidates its destination.

Nothing here sleeps, and nothing touches ``data/corpus/`` or ``work/``.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from typer.testing import CliRunner

import id_detector.benchmark.ablations as ablations_module
import id_detector.benchmark.corpus as corpus_module
import id_detector.benchmark.shortlist as shortlist_module
import id_detector.benchmark.transforms_schedule as schedule_module
import id_detector.truth as truth_module
import scripts.score_corpus as score_script
from id_detector import cli
from id_detector.contracts import CERTIFICATION_DISABLED
from id_detector.fuse.episodes import _certification
from id_detector.io import atomic_write_bytes, atomic_write_json
from id_detector.truth import open_corpus, seed_truth, write_corpus_file_through_gateway
from tests.test_score_corpus import _copy_fixture, _mark_verified
from tests.test_score_corpus import _freeze as _freeze_mini
from tests.test_stage4d_profiles import ABLATIONS, SHORTLIST
from tests.test_truth_corpus_followup_r7 import FIXTURE, _snapshot, _tracklist
from tests.test_truth_corpus_followup_sol import _certifiable_corpus

WRITER_REFUSAL = "refusing to write generated output"
POSITIVE_CLAIMS = {
    "a certified status": re.compile(r'"status"\s*:\s*"certified"'),
    "certifiable true": re.compile(r'"certifiable"\s*:\s*true'),
    "certified true": re.compile(r'"certified"\s*:\s*true'),
    "thresholds_met true": re.compile(r'"thresholds_met"\s*:\s*true'),
    "thresholds are met": re.compile(r"thresholds are met"),
}
#: Round 11: link-score's gate claims, scanned separately in its JSON and its console output
#: (other reports legitimately carry non-certification ``"pass"`` fields).
LINK_CLAIMS = {
    "pass true": re.compile(r'"pass"\s*:\s*true'),
    "gate_pass=true": re.compile(r"gate_pass=true"),
}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    target = tmp_path / "root" / "corpus"
    shutil.copytree(FIXTURE, target)
    return target


@pytest.fixture
def release_like(tmp_path: Path) -> Path:
    """A manifest-less corpus holding its sets directly, like ``data/corpus/release-1``."""

    target = tmp_path / "data" / "corpus" / "release-like"
    shutil.copytree(FIXTURE, target)
    assert not (target / "corpus-version.json").exists()
    return target


def _gate(monkeypatch: pytest.MonkeyPatch, *, open_: bool) -> None:
    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", open_)


def _claims(text: str, patterns: dict[str, re.Pattern[str]] | None = None) -> list[str]:
    scanned = POSITIVE_CLAIMS if patterns is None else patterns
    return sorted(name for name, pattern in scanned.items() if pattern.search(text))


def _seed(out_path: Path, tmp_path: Path) -> None:
    seed_truth(
        out_path=out_path,
        set_id="retry-set",
        duration_ms=60_000,
        media_key="a" * 64,
        tracklist=_tracklist(tmp_path),
        project_root=tmp_path / "project",
    )


# ==================================================================================================
# 1 (P0) -- every positive certification claim goes through the gate
# ==================================================================================================


def _emit_every_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[str, str, str]:
    """The JSON and printed text of every result-emitting CLI or script path this cycle knows.

    Returns all of it joined, plus link-score's JSON and console output on their own.
    """

    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    runner = CliRunner()
    texts: list[str] = []

    # `idea benchmark score`: the generic scorer, on a corpus that certifies when allowed.
    truth_root, predictions = _certifiable_corpus(tmp_path / "score")
    score_out = tmp_path / "score-out" / "report.json"
    result = runner.invoke(
        cli.app,
        [
            "benchmark",
            "score",
            "--truth",
            str(truth_root),
            "--episodes",
            str(predictions),
            "--out",
            str(score_out),
        ],
    )
    assert result.exit_code == 0, result.output
    texts += [result.output, score_out.read_text("utf-8")]

    # `scripts/score_corpus.py`: --out and --print, with the L3 thresholds reachable.
    for recipe in list(score_script.L3_THRESHOLDS):
        zeros = {key: 0 for key in score_script.L3_THRESHOLDS[recipe]}
        monkeypatch.setitem(score_script.L3_THRESHOLDS, recipe, zeros)
    root = _copy_fixture(tmp_path / "mini")
    for set_id in ("mini-a", "mini-b"):
        _mark_verified(root / set_id / "ground_truth.json")
    _freeze_mini(root, ["mini-a", "mini-b"])
    mini_out = tmp_path / "mini-out" / "out.json"
    run_list = str(root / "run-list.json")
    capsys.readouterr()
    assert (
        score_script.main(["--run-list", run_list, "--out", str(mini_out), "--match", "time"]) == 0
    )
    assert score_script.main(["--run-list", run_list, "--print", "--match", "time"]) == 0
    texts.append(capsys.readouterr().out)
    texts += [path.read_text("utf-8") for path in sorted(mini_out.parent.rglob("*.json"))]

    # `idea benchmark links-score`: a sample that passes its gate.
    marked = tmp_path / "links" / "marked.json"
    marked.parent.mkdir(parents=True)
    sheet = {
        "gate": {"target_e4": 9_500, "min_links": 60},
        "links": [{"mark": "correct"} for _ in range(100)],
    }
    marked.write_text(json.dumps(sheet), encoding="utf-8")
    links_out = tmp_path / "links" / "score.json"
    result = runner.invoke(
        cli.app, ["benchmark", "links-score", "--marked", str(marked), "--out", str(links_out)]
    )
    assert result.exit_code == 0, result.output
    link_console, link_json = result.output, links_out.read_text("utf-8")
    texts += [link_console, link_json]

    # `idea benchmark freeze-profiles`: the committed reports.
    profiles_out = tmp_path / "profiles-out"
    result = runner.invoke(
        cli.app,
        [
            "benchmark",
            "freeze-profiles",
            "--ablations",
            str(ABLATIONS),
            "--shortlist",
            str(SHORTLIST),
            "--out",
            str(profiles_out),
        ],
    )
    assert result.exit_code == 0, result.output
    texts += [
        result.output,
        *(path.read_text("utf-8") for path in sorted(profiles_out.rglob("*.json"))),
    ]

    # Calibrated episode certification: a loaded model that says certified.
    calibrator = SimpleNamespace(
        model=SimpleNamespace(
            certification=[
                SimpleNamespace(
                    dimension="work",
                    tier="possible",
                    status="certified",
                    n_test_predictions=12,
                    lower_bound_e4=9_100,
                    test_version="t1",
                )
            ]
        )
    )
    texts.append(json.dumps(_certification("free", calibrator).model_dump(mode="json")))
    return "\n".join(texts), link_json, link_console


def test_no_emitter_claims_certification_while_the_gate_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _gate(monkeypatch, open_=False)
    text, link_json, link_console = _emit_every_result(tmp_path, monkeypatch, capsys)
    corpus = tmp_path / "freeze-corpus"
    shutil.copytree(FIXTURE, corpus)
    runner = CliRunner()
    freeze = runner.invoke(
        cli.app,
        [
            "truth",
            "freeze",
            "--truth",
            str(corpus),
            "--corpus-version",
            "fx",
            "--out",
            str(corpus / "corpus-version.json"),
        ],
    )
    certify = runner.invoke(
        cli.app,
        ["benchmark", "certify", "--corpus", "fx", "--profile", "free", "--test-version", "v1"],
    )
    everything = "\n".join([text, freeze.output, certify.output])
    assert _claims(everything) == []
    assert CERTIFICATION_DISABLED in everything
    # Round 11: link-score, asserted on its own -- JSON and console separately.
    link_score = json.loads(link_json)
    assert link_score["gate"]["pass"] is None
    assert link_score["gate"]["status"] == CERTIFICATION_DISABLED
    assert link_score["precision_e4"] == 10_000  # the raw metrics stay visible
    assert link_score["one_sided_95_lower_e4"] >= 9_500
    assert _claims(link_json, LINK_CLAIMS) == []
    assert _claims(link_console, LINK_CLAIMS) == []
    assert f"gate_status={CERTIFICATION_DISABLED}" in link_console


def test_the_same_emitters_do_claim_certification_with_the_gate_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: every pattern the closed test scans for really is emitted when allowed."""

    _gate(monkeypatch, open_=True)
    text, link_json, link_console = _emit_every_result(tmp_path, monkeypatch, capsys)
    assert _claims(text) == sorted(POSITIVE_CLAIMS)
    assert _claims(link_json, LINK_CLAIMS) == ["pass true"]
    assert _claims(link_console, LINK_CLAIMS) == ["gate_pass=true"]


# ==================================================================================================
# 2 (P0) -- the io backstop under every atomic write
# ==================================================================================================


def test_the_io_backstop_refuses_direct_writes_into_a_corpus(corpus: Path, tmp_path: Path) -> None:
    truth_path = corpus / "fixture-set" / "ground_truth.json"
    before = _snapshot(corpus)
    with pytest.raises(ValueError, match="ground_truth.json is a corpus file"):
        atomic_write_json(truth_path, {"overwritten": True})
    with pytest.raises(ValueError, match="holds ground_truth.json"):
        atomic_write_bytes(corpus / "fixture-set" / "notes.txt", b"x")
    with pytest.raises(ValueError, match="holds ground_truth.json"):
        atomic_write_bytes(corpus / "fixture-set" / "sub" / "notes.txt", b"x")
    manifested = tmp_path / "manifested"
    manifested.mkdir()
    (manifested / "corpus-version.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="holds corpus-version.json"):
        atomic_write_json(manifested / "report.json", {})
    with pytest.raises(ValueError, match="holds corpus-version.json"):
        atomic_write_json(manifested / "reports" / "report.json", {})
    assert _snapshot(corpus) == before
    assert not (corpus / "fixture-set" / "sub").exists()
    assert not (manifested / "reports").exists()


def test_the_io_backstop_leaves_work_writes_alone_with_six_probes_and_no_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "work" / "source" / "media" / "fuse" / "episodes.json"
    probes: list[str] = []
    listings: list[str] = []
    real_lexists = os.path.lexists
    real_scandir = os.scandir

    def counting_lexists(path: object) -> bool:
        probes.append(os.fspath(path))  # type: ignore[arg-type]
        return real_lexists(path)  # type: ignore[arg-type]

    def counting_scandir(path: object = ".") -> object:
        listings.append(os.fspath(path))  # type: ignore[arg-type]
        return real_scandir(path)  # type: ignore[arg-type]

    # A running pipeline writes into directories that already exist, so the counted write is the
    # second one: an injected listing of the destination's parent would be visible here.
    atomic_write_json(target, {"ok": True})
    monkeypatch.setattr(os.path, "lexists", counting_lexists)
    monkeypatch.setattr(os, "scandir", counting_scandir)
    atomic_write_json(target, {"ok": "again"})
    monkeypatch.undo()
    assert len(probes) <= 6, probes
    assert listings == []
    assert json.loads(target.read_text("utf-8")) == {"ok": "again"}


def test_only_the_gateway_helper_writes_a_corpus_file(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "set" / "ground_truth.json"
    with pytest.raises(ValueError, match="is a corpus file"):
        atomic_write_json(staging, {"set_id": "x"})
    write_corpus_file_through_gateway(staging, {"set_id": "x"})
    assert json.loads(staging.read_text("utf-8")) == {"set_id": "x"}


# ==================================================================================================
# 3 (P1) -- the seed retry after creating the named folder is still refused
# ==================================================================================================


def test_the_seed_retry_after_creating_the_named_folder_is_still_refused(
    release_like: Path, tmp_path: Path
) -> None:
    out_path = release_like / "new-group" / "new-set" / "ground_truth.json"
    before = _snapshot(release_like)
    with pytest.raises(ValueError, match="does not exist") as refused:
        _seed(out_path, tmp_path)
    message = str(refused.value)
    assert "create a new corpus folder outside any existing corpus" in message
    assert "create the corpus directory first" not in message
    (release_like / "new-group").mkdir()  # the owner creates the folder the refusal named
    with pytest.raises(ValueError, match="inside another corpus"):
        _seed(out_path, tmp_path)
    assert _snapshot(release_like) == before
    assert not (release_like / "new-group" / "new-set").exists()


def test_seeding_into_a_deliberately_created_empty_corpus_folder_still_works(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "new-corpus"
    corpus.mkdir()
    _seed(corpus / "first-set" / "ground_truth.json", tmp_path)
    assert (corpus / "first-set" / "ground_truth.json").is_file()


# ==================================================================================================
# 4 (P1) -- a new root's parent is listed once per mutation, under the lock
# ==================================================================================================


def test_a_new_corpus_root_parent_is_listed_once_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "other.txt").write_text("x", encoding="utf-8")
    established = parent / "established"
    shutil.copytree(FIXTURE, established)
    state = {"locked": False}
    real_lock = truth_module.corpus_write_lock

    @contextmanager
    def tracking_lock(root: Path, *, timeout: float = 20.0) -> Iterator[None]:
        with real_lock(root, timeout=timeout):
            state["locked"] = True
            try:
                yield
            finally:
                state["locked"] = False

    key = os.path.normcase(str(parent))
    listings: list[bool] = []
    real_scandir = os.scandir

    def recording(path: object = ".") -> object:
        text = os.path.normcase(os.fspath(path)).removeprefix("\\\\?\\")  # type: ignore[arg-type]
        if text == key:
            listings.append(state["locked"])
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(truth_module, "corpus_write_lock", tracking_lock)
    monkeypatch.setattr(os, "scandir", recording)
    with open_corpus(parent / "new-root", mutate=True, require_records=False):
        pass
    assert listings == [True], "the parent must be listed exactly once, under the corpus lock"
    listings.clear()
    with open_corpus(established, mutate=True):
        pass
    assert listings == [], "an established corpus root's parent is never listed"


# ==================================================================================================
# 5 (P1) -- each run module's final publication revalidates its destination
# ==================================================================================================

RUN_MODULES = {
    "corpus": (corpus_module, "run_corpus"),
    "ablations": (ablations_module, "run_ablations"),
    "transforms_schedule": (schedule_module, "run_transform_schedule_benchmark"),
    "shortlist": (shortlist_module, "run_shortlist"),
}


@pytest.mark.parametrize("name", sorted(RUN_MODULES))
def test_each_run_revalidates_its_report_destination_immediately_before_publishing(
    name: str, release_like: Path, tmp_path: Path
) -> None:
    module, run_name = RUN_MODULES[name]
    assert isinstance(module, ModuleType)
    # The run publishes only through _publish_report, never a direct write of its out_path.
    source = inspect.getsource(getattr(module, run_name))
    assert "_publish_report(out_path, " in source
    assert "atomic_write_json(out_path" not in source
    before = _snapshot(release_like)
    destination = release_like / "report.json"  # only the explicit guard catches this
    with pytest.raises(ValueError, match=WRITER_REFUSAL):
        module._publish_report(destination, {"report": True})
    assert _snapshot(release_like) == before
    assert not destination.exists()
    allowed = tmp_path / "reports" / "report.json"
    module._publish_report(allowed, {"report": True})
    assert json.loads(allowed.read_text("utf-8")) == {"report": True}
