"""Follow-up to the truth-corpus fix: the io corpus backstop under the durable file helpers.

``bb57b70`` (money) added ``io.durable_replace`` and ``io.create_file_durably``; ``28d8fe1`` (truth)
put ``io.refuse_corpus_destination`` under ``atomic_write_bytes`` alone and recorded the two durable
helpers as a follow-up (``docs/reviews/followup-truth-corpus.md``, round 10).  Here every shared
``io`` helper that creates, replaces or renames a file at a caller-chosen destination refuses a
corpus destination first, with the one gateway exemption, and stays bounded: at most the backstop's
six probes and never a directory listing.

Nothing here sleeps, and nothing touches ``data/corpus/`` or ``work/``.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from id_detector.io import atomic_write_bytes, create_file_durably, durable_replace
from id_detector.journal import append_line
from id_detector.truth import write_corpus_file_through_gateway

ROOT = Path(__file__).resolve().parents[1]

#: Destinations the backstop must refuse, as parts under ``tmp_path`` plus the expected message:
#: a set's truth record, a manifest by name anywhere, a file in a set, a file below a set, and a
#: file in a manifest-only corpus.
DESTINATIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "set-truth": (("corpus", "set-a", "ground_truth.json"), "ground_truth.json is a corpus file"),
    "manifest-name": (
        ("elsewhere", "corpus-version.json"),
        "corpus-version.json is a corpus file",
    ),
    "inside-set": (("corpus", "set-a", "notes.jsonl"), "holds ground_truth.json"),
    "below-set": (("corpus", "set-a", "sub", "notes.jsonl"), "holds ground_truth.json"),
    "inside-manifest-corpus": (("manifested", "report.jsonl"), "holds corpus-version.json"),
}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A hand-made corpus with one set, beside a manifest-only corpus."""

    root = tmp_path / "corpus"
    (root / "set-a").mkdir(parents=True)
    (root / "set-a" / "ground_truth.json").write_text('{"set_id": "set-a"}', encoding="utf-8")
    manifested = tmp_path / "manifested"
    manifested.mkdir()
    (manifested / "corpus-version.json").write_text("{}", encoding="utf-8")
    return root


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every file (with its bytes) and directory (``None``) under ``root``."""

    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def _staged(tmp_path: Path, content: bytes = b"new bytes") -> Path:
    source = tmp_path / "staging" / "staged.bin"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(content)
    return source


# ==================================================================================================
# refusals: each newly guarded helper, every corpus destination, nothing created or moved
# ==================================================================================================


@pytest.mark.parametrize("case", sorted(DESTINATIONS))
def test_durable_replace_refuses_a_corpus_destination(
    case: str, corpus: Path, tmp_path: Path
) -> None:
    parts, message = DESTINATIONS[case]
    destination = tmp_path.joinpath(*parts)
    source = _staged(tmp_path)
    before = _tree(tmp_path)
    with pytest.raises(ValueError, match=message):
        durable_replace(source, destination)
    assert _tree(tmp_path) == before  # the source is still staged; nothing moved or appeared
    assert source.read_bytes() == b"new bytes"


@pytest.mark.parametrize("case", sorted(DESTINATIONS))
def test_create_file_durably_refuses_a_corpus_destination(
    case: str, corpus: Path, tmp_path: Path
) -> None:
    parts, message = DESTINATIONS[case]
    destination = tmp_path.joinpath(*parts)
    before = _tree(tmp_path)
    with pytest.raises(ValueError, match=message):
        create_file_durably(destination)
    # Refused before any directory is made durable: no ``sub``, no ``elsewhere``, no temporary.
    assert _tree(tmp_path) == before


def test_the_money_journal_cannot_be_started_inside_a_set(corpus: Path, tmp_path: Path) -> None:
    """End to end through a caller: ``journal.append_line`` creates its file with the helper."""

    before = _tree(tmp_path)
    with pytest.raises(ValueError, match="holds ground_truth.json"):
        append_line(corpus / "set-a" / ".attempts" / "audd.jsonl", {"event": "dispatched"})
    assert _tree(tmp_path) == before


# ==================================================================================================
# work-tree writes: unaffected, at most six probes, never a listing
# ==================================================================================================


def test_work_tree_writes_are_unaffected_bounded_and_never_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    media = work / "source" / "media"
    attempts = media / ".attempts" / "audd.jsonl"
    invocations = work / "invocations.jsonl"
    app_db = work / "app.db"
    db_sibling = work / "app.db-journal"
    bundle = media / "present" / "bundles" / ("a" * 8) / "tracklist.json"
    for path in (attempts, invocations, app_db, bundle):
        path.parent.mkdir(parents=True, exist_ok=True)
    app_db.write_bytes(b"sqlite")

    # A first pass creates everything, as a fresh run does.
    assert create_file_durably(attempts) is True
    assert create_file_durably(invocations) is True
    assert create_file_durably(db_sibling) is True
    durable_replace(_staged(tmp_path, b"first"), bundle)
    atomic_write_bytes(app_db.with_name("app.db-shm"), b"shm")
    assert bundle.read_bytes() == b"first"

    probes: list[str] = []
    listings: list[str] = []
    real_lexists = os.path.lexists
    real_scandir = os.scandir
    real_listdir = os.listdir

    def counting_lexists(path: object) -> bool:
        probes.append(os.fspath(path))  # type: ignore[arg-type]
        return real_lexists(path)  # type: ignore[arg-type]

    def counting_scandir(path: object = ".") -> object:
        listings.append(os.fspath(path))  # type: ignore[arg-type]
        return real_scandir(path)  # type: ignore[arg-type]

    def counting_listdir(path: object = ".") -> list[str]:
        listings.append(os.fspath(path))  # type: ignore[arg-type]
        return real_listdir(path)  # type: ignore[arg-type]

    # The counted pass writes into directories that already exist, as a running pipeline does.
    monkeypatch.setattr(os.path, "lexists", counting_lexists)
    monkeypatch.setattr(os, "scandir", counting_scandir)
    monkeypatch.setattr(os, "listdir", counting_listdir)
    counted: dict[str, int] = {}
    assert create_file_durably(attempts) is False
    counted["create_file_durably(existing .attempts)"] = len(probes)
    probes.clear()
    assert create_file_durably(media / ".attempts" / "shazam.jsonl") is True
    counted["create_file_durably(new .attempts)"] = len(probes)
    probes.clear()
    durable_replace(_staged(tmp_path, b"second"), bundle)
    counted["durable_replace(bundle)"] = len(probes)
    probes.clear()
    append_line(invocations, {"run_id": "r1"})
    counted["append_line(invocations.jsonl)"] = len(probes)
    monkeypatch.undo()

    assert all(count <= 6 for count in counted.values()), counted
    assert listings == []
    assert bundle.read_bytes() == b"second"
    assert (media / ".attempts" / "shazam.jsonl").read_bytes() == b""
    assert json.loads(invocations.read_bytes().splitlines()[0]) == {"run_id": "r1"}


# ==================================================================================================
# the one exemption: the corpus gateway
# ==================================================================================================


def test_the_gateway_exemption_still_works_for_every_guarded_helper(
    corpus: Path, tmp_path: Path
) -> None:
    truth = corpus / "set-a" / "ground_truth.json"
    source = _staged(tmp_path, b'{"set_id": "moved"}')
    durable_replace(source, truth, through_corpus_gateway=True)
    assert truth.read_bytes() == b'{"set_id": "moved"}'
    assert not source.exists()

    ledger = corpus / "set-a" / "notes.jsonl"
    assert create_file_durably(ledger, through_corpus_gateway=True) is True
    assert create_file_durably(ledger, through_corpus_gateway=True) is False
    assert ledger.read_bytes() == b""

    write_corpus_file_through_gateway(corpus / "set-b" / "ground_truth.json", {"set_id": "set-b"})
    assert json.loads((corpus / "set-b" / "ground_truth.json").read_text("utf-8")) == {
        "set_id": "set-b"
    }


def test_only_the_gateway_helper_passes_the_exemption() -> None:
    """No production caller of any guarded helper claims the exemption for itself."""

    def claims_exemption(source: str) -> bool:
        return any(
            keyword.arg == "through_corpus_gateway"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            for keyword in node.keywords
        )

    claimants = sorted(
        path.relative_to(ROOT).as_posix()
        for directory in (ROOT / "src", ROOT / "scripts")
        for path in directory.rglob("*.py")
        if claims_exemption(path.read_text(encoding="utf-8"))
    )
    assert claimants == ["src/id_detector/truth.py"]
