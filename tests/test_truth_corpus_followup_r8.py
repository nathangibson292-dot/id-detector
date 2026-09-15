"""Regressions for the round-7 review, fixed in the re-scoped round-8 pass.

Corpus damage and small items only; certification scope is deferred to the certification
follow-up, and until it lands every certification claim is refused.  Two-process tests use explicit
stdin/stdout barriers and test seams, never sleeps.  Nothing touches ``data/corpus/`` or ``work/``.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import id_detector.benchmark.controlled as controlled_module
import id_detector.calibrate.validate as validate_module
import id_detector.truth as truth_module
import id_detector.truth_review as review_module
from id_detector import cli
from id_detector.benchmark.controlled import (
    CONTROLLED_RENDER_MARKER,
    render_controlled,
    synthesize_test_sources,
)
from id_detector.benchmark.scorer import truth_is_frozen_verified
from id_detector.calibrate.certify import (
    CERTIFICATION_DISABLED,
    CertificationDisabled,
    run_certify,
)
from id_detector.truth import (
    EXPOSURE_LEDGER_NAME,
    exposure_path,
    freeze_truth,
    record_exposure_in_ledger,
)
from id_detector.truth_review import TruthReviewSession
from tests.test_score_corpus import _copy_fixture, _freeze, _mark_verified, _run_time
from tests.test_truth_corpus_followup_r3 import _junction
from tests.test_truth_corpus_followup_r4 import _child, _reveal_stub
from tests.test_truth_corpus_followup_r7 import (
    FIXTURE,
    GUIDANCE,
    SET_ID,
    _dummy_sources,
    _marker_payload,
    _record,
    _snapshot,
    _tracklist,
)
from tests.test_truth_review import _rows

WINDOWS = os.name == "nt"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    target = tmp_path / "root" / "corpus"
    shutil.copytree(FIXTURE, target)
    return target


def _render_to(sources: Path, out: Path, audio: Path, **extra: Any) -> Any:
    return asyncio.run(render_controlled(sources, out, seed=1, audio_dir=audio, **extra))


def _frozen(corpus: Path, tmp_path: Path, *, reveal: bool = False) -> Path:
    truth_path = corpus / "fixture-set" / "ground_truth.json"
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    if reveal:
        session.reveal_predictions()
    session.save({"rows": _rows(_record(truth_path))})
    freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    return truth_path


# ==================================================================================================
# 1 (P0) -- --audio-out goes through the same destination rules as --out
# ==================================================================================================


@pytest.fixture
def real_layout(tmp_path: Path) -> Path:
    """A copy of a real-layout corpus (<corpus>/<set>/ground_truth.json), like release-1."""

    target = tmp_path / "real" / "release-like"
    shutil.copytree(FIXTURE, target)
    return target


def test_audio_out_at_a_real_layout_corpus_is_refused_and_left_byte_identical(
    real_layout: Path, tmp_path: Path
) -> None:
    before = _snapshot(real_layout)
    with pytest.raises(ValueError, match="is a corpus, not rendered audio"):
        _render_to(_dummy_sources(tmp_path), tmp_path / "new-out", real_layout)
    assert _snapshot(real_layout) == before
    assert not (tmp_path / "new-out").exists()


def test_audio_out_through_a_link_to_a_real_layout_corpus_is_refused_and_left_byte_identical(
    real_layout: Path, tmp_path: Path
) -> None:
    link = tmp_path / "audio-link"
    _junction(link, real_layout)
    before = _snapshot(real_layout)
    with pytest.raises(ValueError, match=GUIDANCE):
        _render_to(_dummy_sources(tmp_path), tmp_path / "new-out", link)
    assert _snapshot(real_layout) == before
    assert not (tmp_path / "new-out").exists()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("inside a corpus", "is inside the corpus"),
        ("beneath the work root", "beneath the work tree"),
        ("unmarked existing directory", "no controlled-audio.json"),
        ("duplicate marker set_ids", "is not a controlled audio marker"),
        ("corpus content below", "holds corpus content"),
    ],
)
def test_audio_out_refuses_every_destination_it_could_damage(
    corpus: Path, tmp_path: Path, case: str, message: str
) -> None:
    work = tmp_path / "work"
    extra: dict[str, Any] = {}
    if case == "inside a corpus":
        audio = corpus / "rendered-audio"
    elif case == "beneath the work root":
        audio = work / "rendered-audio"
        extra["work_root"] = work
    else:
        audio = tmp_path / "owner-audio"
        audio.mkdir()
        (audio / "keep.wav").write_bytes(b"owner audio")
        if case == "duplicate marker set_ids":
            marker = {"kind": "controlled-audio", "set_ids": ["a", "a"]}
            (audio / "controlled-audio.json").write_text(json.dumps(marker), encoding="utf-8")
        elif case == "corpus content below":
            marker = {"kind": "controlled-audio", "set_ids": ["a"]}
            (audio / "controlled-audio.json").write_text(json.dumps(marker), encoding="utf-8")
            shutil.copytree(FIXTURE, audio / "nested" / "corpus")
    sources = _dummy_sources(tmp_path)
    before = _snapshot(tmp_path)
    with pytest.raises(ValueError, match=message):
        _render_to(sources, tmp_path / "new-out", audio, **extra)
    assert _snapshot(tmp_path) == before


@pytest.mark.slow
def test_audio_out_is_revalidated_immediately_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corpus that appears at the audio destination while rendering is refused, not replaced."""

    sources = tmp_path / "sources"
    synthesize_test_sources(sources, seed=17, count=3)
    audio = tmp_path / "late-audio"
    real_truth = controlled_module._truth_for_render
    planted: list[Path] = []

    async def plant_then_render(**kwargs: Any) -> Any:
        if not planted:
            shutil.copytree(FIXTURE, audio)  # the owner copies a corpus there mid-render
            planted.append(audio)
        return await real_truth(**kwargs)

    monkeypatch.setattr(controlled_module, "_truth_for_render", plant_then_render)
    with pytest.raises(ValueError, match="is a corpus, not rendered audio"):
        asyncio.run(render_controlled(sources, tmp_path / "out", seed=17, audio_dir=audio))
    assert _snapshot(audio) == _snapshot(FIXTURE)
    assert not (tmp_path / "out").exists()


# ==================================================================================================
# 2 (P1) -- a frozen corpus's ledger is terminal and exactly manifest-bound
# ==================================================================================================


def test_a_ledger_append_on_a_frozen_corpus_is_refused(corpus: Path, tmp_path: Path) -> None:
    truth_path = _frozen(corpus, tmp_path)
    ledger = corpus / EXPOSURE_LEDGER_NAME
    with pytest.raises(ValueError, match="frozen"):
        record_exposure_in_ledger(truth_path, set_id=SET_ID, when="2026-09-15T00:00:00Z")
    assert not ledger.exists()


def test_the_review_route_refuses_a_frozen_reveal_before_assembling_predictions(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path = corpus / "fixture-set" / "ground_truth.json"
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    session.save({"rows": _rows(_record(truth_path))})
    freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")
    before = _snapshot(corpus)

    def never(_media_dir: object) -> list[dict[str, Any]]:
        raise AssertionError("predictions were assembled for a frozen set")

    monkeypatch.setattr(review_module, "_predictions", never)
    with pytest.raises(ValueError, match="frozen"):
        session.reveal_predictions()
    assert _snapshot(corpus) == before
    assert not exposure_path(truth_path).exists()


def test_a_hand_added_ledger_line_is_caught_while_present(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reveal_stub(monkeypatch)
    truth_path = _frozen(corpus, tmp_path, reveal=True)
    ledger = corpus / EXPOSURE_LEDGER_NAME
    frozen_bytes = ledger.read_bytes()
    record = _record(truth_path)
    assert truth_is_frozen_verified(truth_path, [record]) is True  # the control
    extra = {"event": "predictions_revealed", "set_id": "someone-else", "set_directory": "x"}
    ledger.write_bytes(frozen_bytes + json.dumps(extra).encode("utf-8") + b"\n")
    with pytest.raises(ValueError, match="1 line\\(s\\) added"):
        truth_is_frozen_verified(truth_path, [record])
    ledger.write_bytes(frozen_bytes)  # deleted again: exactly the frozen ledger once more
    assert truth_is_frozen_verified(truth_path, [record]) is True


# ==================================================================================================
# 3 (P1) -- overlapping mistyped seeds serialise on one machine-wide seed lock
# ==================================================================================================

# Each child installs two seams: the create-only write pauses (prints "validated", waits for "go"),
# and the first contended truth lock prints "blocked".  The parent pauses the outer seed past its
# final validation, then starts the nested seed and waits until it has either also passed
# validation (no serialisation: releasing both would land both files) or reported itself blocked
# on the seed lock (serialised).  Deterministic both ways and never a sleep.
_SEED_WITH_SEAMS = """
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
def contended(key):
    print("blocked", flush=True)
truth._write_record_file = paused
truth._lock_contended = contended
truth.seed_truth(out_path=Path(sys.argv[1]), set_id=sys.argv[2], duration_ms=60000,
                 media_key="d" * 64, tracklist=Path(sys.argv[3]), project_root=Path(sys.argv[4]))
print("seeded", flush=True)
"""


def test_overlapping_seeds_serialise_so_only_one_write_lands(tmp_path: Path) -> None:
    root = tmp_path / "C"
    # Round 9: a seed needs an existing corpus root, so the nested seed's root C/a exists too.
    (root / "a").mkdir(parents=True)
    outer = root / "a" / "ground_truth.json"
    nested = root / "a" / "b" / "ground_truth.json"
    tracklist = _tracklist(tmp_path)
    events: queue.Queue[tuple[int, str]] = queue.Queue()
    children: list[subprocess.Popen] = []
    seen: dict[int, list[str]] = {0: [], 1: []}
    finished: set[int] = set()

    def start(out_path: Path, set_id: str) -> None:
        index = len(children)
        child = _child(_SEED_WITH_SEAMS, str(out_path), set_id, str(tracklist), str(tmp_path / "p"))
        children.append(child)

        def pump() -> None:
            assert child.stdout is not None
            for line in child.stdout:
                events.put((index, line.strip()))
            events.put((index, "<eof>"))

        threading.Thread(target=pump, daemon=True).start()

    def wait_until(condition: Callable[[], bool]) -> None:
        while not condition():
            index, line = events.get(timeout=120)
            if line == "<eof>":
                finished.add(index)
            else:
                seen[index].append(line)

    def go(index: int) -> None:
        stdin = children[index].stdin
        assert stdin is not None
        stdin.write("go\n")
        stdin.flush()

    try:
        start(outer, "outer-set")
        wait_until(lambda: "validated" in seen[0] or 0 in finished)
        assert "validated" in seen[0], "the outer seed did not reach its write"
        start(nested, "nested-set")
        wait_until(lambda: "validated" in seen[1] or "blocked" in seen[1] or 1 in finished)
        if "validated" in seen[1]:
            go(0)  # both past validation with no serialisation: release both together
            go(1)
        else:
            go(0)  # serialised: the nested seed validates only after the outer one has written
            wait_until(lambda: 0 in finished)
            wait_until(lambda: "validated" in seen[1] or 1 in finished)
            if "validated" in seen[1]:
                go(1)
        codes = [child.wait(timeout=120) for child in children]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=120)
    errors = [child.stderr.read() if child.stderr is not None else "" for child in children]
    assert codes[0] == 0, errors[0]
    assert codes[1] != 0, "the nested seed was not refused"
    assert outer.exists()
    assert not nested.exists(), "both overlapping seeds wrote"


# ==================================================================================================
# 4 (P2) -- the ledger pin identity is the canonical path_key
# ==================================================================================================


@pytest.mark.skipif(not WINDOWS, reason="Win32 namespace-prefixed spellings")
@pytest.mark.parametrize("spelling", ["ordinary", "namespace-prefixed"])
def test_a_reveal_records_the_ledger_under_either_spelling(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    truth_path = corpus / "fixture-set" / "ground_truth.json"
    spelled = truth_path if spelling == "ordinary" else Path("\\\\?\\" + str(truth_path))
    record_exposure_in_ledger(spelled, set_id=SET_ID, when="2026-09-15T00:00:00Z")
    _reveal_stub(monkeypatch)
    TruthReviewSession(spelled, work_root=tmp_path / "work").reveal_predictions()
    lines = (corpus / EXPOSURE_LEDGER_NAME).read_text("utf-8").splitlines()
    assert [json.loads(line)["set_id"] for line in lines if line.strip()] == [SET_ID]


# ==================================================================================================
# 5 (P2) -- the controlled marker rejects duplicate set_id entries
# ==================================================================================================


def test_a_controlled_marker_with_a_duplicate_set_id_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    shutil.copytree(FIXTURE, target)
    payload = _marker_payload(target)
    payload["sets"] = payload["sets"] * 2
    payload["set_count"] = 2
    (target / CONTROLLED_RENDER_MARKER).write_text(json.dumps(payload), encoding="utf-8")
    before = _snapshot(target)
    with pytest.raises(ValueError, match="lists a set_id more than once"):
        _render_to(_dummy_sources(tmp_path), target, tmp_path / "audio")
    assert _snapshot(target) == before


# ==================================================================================================
# 6 (P2) -- the calibration model and its sidecar are destination-guarded
# ==================================================================================================


def test_the_calibration_model_is_never_written_into_a_corpus_or_through_a_link(
    corpus: Path, tmp_path: Path
) -> None:
    before = _snapshot(corpus)
    with pytest.raises(ValueError, match="inside the corpus"):
        validate_module._write_calibration_model(corpus / "fixture-set" / "model.json", {"v": 1})
    assert _snapshot(corpus) == before
    real = tmp_path / "real-models"
    real.mkdir()
    _junction(tmp_path / "linked-models", real)
    with pytest.raises(ValueError, match=GUIDANCE):
        validate_module._write_calibration_model(
            tmp_path / "linked-models" / "model.json", {"v": 1}
        )
    assert list(real.iterdir()) == []
    allowed = tmp_path / "models" / "model.json"
    validate_module._write_calibration_model(allowed, {"v": 1})
    assert json.loads(allowed.read_text("utf-8")) == {"v": 1}


def test_the_calibration_model_sidecar_is_guarded_before_its_own_write(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = corpus / "fixture-set" / "model.done.json"
    monkeypatch.setattr(validate_module, "completion_sidecar_path", lambda _path: sidecar)
    with pytest.raises(ValueError, match="inside the corpus"):
        validate_module._write_calibration_model(tmp_path / "models" / "model.json", {"v": 1})
    assert not sidecar.exists()


# ==================================================================================================
# Deferred certification scope: every certification claim is refused until the follow-up lands
# ==================================================================================================


class _GatewayReached(Exception):
    pass


def test_run_certify_is_refused_before_it_opens_any_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", False)  # the moratorium
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project / "data" / "corpus" / "fx-v1")
    before = _snapshot(project)

    def reached(root: Path, mutate: bool) -> None:
        raise _GatewayReached(str(root))

    monkeypatch.setattr(truth_module, "_gateway_entered", reached)
    with pytest.raises(CertificationDisabled, match=f"^{CERTIFICATION_DISABLED}$"):
        asyncio.run(
            run_certify(
                corpus_version="fx-v1",
                profile="free",
                test_version="v1",
                project_root=project,
                work_root=tmp_path / "work",
            )
        )
    assert _snapshot(project) == before


def test_idea_benchmark_certify_refuses_with_the_disabled_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", False)  # the moratorium
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    result = CliRunner().invoke(
        cli.app,
        ["benchmark", "certify", "--corpus", "fx-v1", "--profile", "free", "--test-version", "v1"],
    )
    assert result.exit_code == 2, result.output
    assert CERTIFICATION_DISABLED in result.output


def test_the_l3_certifiable_flag_is_refused_with_the_disabled_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(truth_module, "CERTIFICATION_ENABLED", False)  # the moratorium
    root = _copy_fixture(tmp_path)
    for set_id in ("mini-a", "mini-b"):
        _mark_verified(root / set_id / "ground_truth.json")
    _freeze(root, ["mini-a", "mini-b"])
    code, document = _run_time(tmp_path / "frozen", root / "run-list.json")
    assert code == 0 and document is not None
    # Everything the old rule asked for holds -- frozen, verified, timed, independent ...
    assert document["truth_status"] == "verified"
    assert document["match_mode"] == "time"
    assert document["l3"]["independent"] is True
    # ... and it is still refused until the certification follow-up lands.
    assert document["l3"]["certifiable"] is False
    assert document["l3"]["certification"] == CERTIFICATION_DISABLED


# Round 9: these tests exercise freezing and certification underneath the owner's moratorium, so the
# single gate is opened for them by fixture (production keeps it closed).  Tests that assert the
# moratorium itself close it again explicitly.
pytestmark = pytest.mark.usefixtures("certification_gate_open")
