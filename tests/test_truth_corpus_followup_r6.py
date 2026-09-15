"""Regressions for the round-5 review: the single corpus gateway (round 6 fix pass).

One entry function, ``truth.open_corpus``, is the front door for every corpus-touching path, and one
supported layout (``<corpus>/<set>/ground_truth.json``) is enforced everywhere.  Two-process tests
use explicit stdin/stdout barriers, never sleeps.  Nothing touches ``data/corpus/``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import id_detector.benchmark.controlled as controlled_module
import id_detector.calibrate.certify as certify_module
import id_detector.truth as truth_module
from id_detector.benchmark.controlled import render_controlled, synthesize_test_sources
from id_detector.benchmark.scorer import load_truth_directory
from id_detector.calibrate.certify import _require_frozen
from id_detector.contracts import GroundTruthRecord
from id_detector.io import read_text
from id_detector.truth import corpus_manifest_frozen, freeze_truth, seed_truth, write_draft_manifest
from id_detector.truth_review import TruthReviewSession
from tests.test_truth_corpus_followup_r3 import _junction
from tests.test_truth_review import _rows

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "truth-review"
GUIDANCE = "Pass the resolved real path instead"
LAYOUT = re.escape("<corpus>/<set>/ground_truth.json")


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


def _frozen_corpus(corpus: Path, tmp_path: Path) -> None:
    _verify(corpus / "fixture-set" / "ground_truth.json", tmp_path)
    freeze_truth(corpus, corpus_version="fx-v1", out_path=corpus / "corpus-version.json")


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


def _await(child: subprocess.Popen, line: str) -> None:
    assert child.stdout is not None
    assert child.stdout.readline().strip() == line


def _go(child: subprocess.Popen) -> tuple[str, str]:
    assert child.stdin is not None
    with contextlib.suppress(BrokenPipeError, OSError):  # a closed child pipe is not our failure
        child.stdin.write("go\n")
        child.stdin.flush()
    return child.communicate(timeout=120)


# --------------------------------------------------------------------------------------------
# Seed into a frozen corpus, and concurrent outer/nested seeds.
# --------------------------------------------------------------------------------------------


def test_seed_a_new_set_into_a_frozen_corpus_is_refused(corpus: Path, tmp_path: Path) -> None:
    _frozen_corpus(corpus, tmp_path)
    assert corpus_manifest_frozen(corpus) is True
    with pytest.raises(ValueError, match="frozen"):
        seed_truth(
            out_path=corpus / "new-set" / "ground_truth.json",
            set_id="new-set",
            duration_ms=60_000,
            media_key="a" * 64,
            tracklist=_tracklist(tmp_path),
            project_root=tmp_path / "project",
        )
    assert not (corpus / "new-set").exists()


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
truth.seed_truth(out_path=Path(sys.argv[1]), set_id=sys.argv[2], duration_ms=60000,
                 media_key="b" * 64, tracklist=Path(sys.argv[3]), project_root=Path(sys.argv[4]))
print("seeded", flush=True)
"""


def test_concurrent_outer_and_nested_seeds_never_both_create_a_nested_layout(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outer = corpus / "outer" / "ground_truth.json"
    nested = corpus / "outer" / "inner" / "ground_truth.json"
    tracklist = _tracklist(tmp_path)
    # Round 8: every seed takes one machine-wide seed lock before its checks.  The outer seed pauses
    # after preflight holding it, so the nested seed cannot even begin validating: it times out as
    # another writer, and once released the outer seed completes alone.  The two files never both
    # exist.  (The deterministic both-past-validation barrier is in test_truth_corpus_followup_r8.)
    child = _child(
        _SEED_PAUSED_AFTER_PREFLIGHT, str(outer), "outer-set", str(tracklist), str(tmp_path / "p")
    )
    try:
        _await(child, "preflighted")
        with pytest.raises(ValueError, match="another truth writer"):
            seed_truth(
                out_path=nested,
                set_id="nested-set",
                duration_ms=60_000,
                media_key="c" * 64,
                tracklist=tracklist,
                project_root=tmp_path / "p2",
                timeout=1.0,
            )
    finally:
        out, err = _go(child)
    assert child.returncode == 0, err  # the outer seed completed alone
    assert outer.exists()
    assert not nested.exists()


# --------------------------------------------------------------------------------------------
# Draft-manifest output is fixed to the corpus root; it cannot target another corpus.
# --------------------------------------------------------------------------------------------


def test_draft_manifest_writes_only_its_own_corpus_root(tmp_path: Path) -> None:
    frozen = tmp_path / "frozen-corpus"
    shutil.copytree(FIXTURE, frozen)
    _frozen_corpus(frozen, tmp_path)
    frozen_before = (frozen / "corpus-version.json").read_bytes()

    draft = tmp_path / "draft-corpus"
    shutil.copytree(FIXTURE, draft)
    manifest = write_draft_manifest(draft, corpus_version="draft")
    assert manifest["frozen"] is False
    assert (draft / "corpus-version.json").is_file()
    # The other corpus's frozen manifest is untouched: there is no destination argument for it.
    assert (frozen / "corpus-version.json").read_bytes() == frozen_before
    with pytest.raises(TypeError):  # the arbitrary --out destination is gone
        write_draft_manifest(draft, corpus_version="draft", out_path=frozen / "corpus-version.json")


# --------------------------------------------------------------------------------------------
# Controlled render: refuse a real/frozen target; serialise against a concurrent review/freeze.
# --------------------------------------------------------------------------------------------


@pytest.fixture
def sources(tmp_path: Path) -> Path:
    directory = tmp_path / "sources"
    synthesize_test_sources(directory, seed=17, count=3)
    return directory


@pytest.mark.slow
def test_controlled_render_refuses_a_real_non_controlled_corpus(
    sources: Path, tmp_path: Path
) -> None:
    real = tmp_path / "release-like"
    (real / "some-set").mkdir(parents=True)
    (real / "some-set" / "ground_truth.json").write_text("{}", encoding="utf-8")
    before = (real / "some-set" / "ground_truth.json").read_bytes()
    with pytest.raises(ValueError, match="not a controlled corpus|release-1"):
        asyncio.run(render_controlled(sources, real, seed=17, audio_dir=tmp_path / "audio"))
    assert (real / "some-set" / "ground_truth.json").read_bytes() == before


@pytest.mark.slow
def test_controlled_render_refuses_a_frozen_target(sources: Path, tmp_path: Path) -> None:
    target = tmp_path / "controlled"
    asyncio.run(render_controlled(sources, target, seed=17, audio_dir=tmp_path / "audio1"))
    # Simulate the target having been frozen; a re-render must refuse it.
    (target / "corpus-version.json").write_text(json.dumps({"frozen": True}), encoding="utf-8")
    manifest_before = (target / "render_manifest.json").read_bytes()
    with pytest.raises(ValueError, match="frozen"):
        asyncio.run(render_controlled(sources, target, seed=17, audio_dir=tmp_path / "audio2"))
    assert (target / "render_manifest.json").read_bytes() == manifest_before


_RENDER_PAUSED = """
import asyncio, sys
from pathlib import Path
sys.path.insert(0, sys.argv[-1])
import id_detector.benchmark.controlled as controlled
real = controlled._truth_for_render
first = {"done": False}
async def paused(**kwargs):
    if not first["done"]:
        first["done"] = True
        print("rendering", flush=True)
        if sys.stdin.readline().strip() != "go":
            raise SystemExit(3)
    return await real(**kwargs)
controlled._truth_for_render = paused
asyncio.run(controlled.render_controlled(Path(sys.argv[1]), Path(sys.argv[2]), seed=17,
                                         audio_dir=Path(sys.argv[3])))
print("rendered", flush=True)
"""


@pytest.mark.slow
def test_controlled_render_serialises_against_a_concurrent_freeze(
    sources: Path, tmp_path: Path
) -> None:
    target = tmp_path / "controlled"
    asyncio.run(render_controlled(sources, target, seed=17, audio_dir=tmp_path / "audio0"))
    child = _child(_RENDER_PAUSED, str(sources), str(target), str(tmp_path / "audio1"))
    try:
        _await(child, "rendering")  # barrier: the re-render holds target's corpus lock
        with pytest.raises(ValueError, match="another truth writer"):
            freeze_truth(
                target,
                corpus_version="fx",
                out_path=target / "corpus-version.json",
                lock_timeout=1.0,
            )
    finally:
        out, err = _go(child)
    assert child.returncode == 0, err
    assert "rendered" in out


# --------------------------------------------------------------------------------------------
# Descendant junctions: a set directory and the manifest, refused before enumeration or read.
# --------------------------------------------------------------------------------------------


def test_a_set_directory_junction_is_refused_before_it_is_enumerated(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One level down: a set directly under tmp_path would make tmp_path itself a corpus, and the
    # ancestor refusal would fire before the descendant-link refusal this test targets.
    outside = tmp_path / "outside" / "outside-set"
    shutil.copytree(FIXTURE / "fixture-set", outside)
    junction = corpus / "linked-set"
    _junction(junction, outside)
    scanned: list[str] = []
    real_scandir = os.scandir
    monkeypatch.setattr(
        os, "scandir", lambda path: (scanned.append(str(path)), real_scandir(path))[1]
    )
    with pytest.raises(ValueError, match=GUIDANCE):
        load_truth_directory(corpus)
    # The junction's target was never scanned: the walk refused the link component first.
    assert not any(str(outside).lower() in call.lower() for call in scanned)
    assert not any(str(junction).lower() in call.lower() for call in scanned)


def test_a_manifest_junction_is_refused_before_it_is_read(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verify(corpus / "fixture-set" / "ground_truth.json", tmp_path)
    real_manifest = tmp_path / "real-manifest.json"
    real_manifest.write_text(json.dumps({"frozen": True}), encoding="utf-8")
    link = corpus / "corpus-version.json"
    try:
        link.symlink_to(real_manifest)
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("this platform/account cannot create symlinks")
    reads: list[str] = []
    real_read_text = certify_module.read_text
    monkeypatch.setattr(
        certify_module, "read_text", lambda path: (reads.append(str(path)), real_read_text(path))[1]
    )
    with pytest.raises(ValueError, match=GUIDANCE):
        _require_frozen(corpus, "fx-v1")
    assert not any(str(real_manifest).lower() in call.lower() for call in reads)


# --------------------------------------------------------------------------------------------
# Generic scoring rejects an unsupported nested layout.
# --------------------------------------------------------------------------------------------


def test_scoring_rejects_a_nested_truth_layout(corpus: Path) -> None:
    nested = corpus / "group" / "set"
    nested.mkdir(parents=True)
    shutil.copy(corpus / "fixture-set" / "ground_truth.json", nested / "ground_truth.json")
    with pytest.raises(ValueError, match="nested|" + LAYOUT):
        load_truth_directory(corpus)


def test_open_corpus_is_the_single_gateway_symbol() -> None:
    assert callable(truth_module.open_corpus)
    assert callable(controlled_module._refuse_controlled_overwrite)


# Round 9: these tests exercise freezing and certification underneath the owner's moratorium, so the
# single gate is opened for them by fixture (production keeps it closed).  Tests that assert the
# moratorium itself close it again explicitly.
pytestmark = pytest.mark.usefixtures("certification_gate_open")
