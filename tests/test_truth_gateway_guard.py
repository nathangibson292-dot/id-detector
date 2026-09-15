"""Guard: corpus file names are spelled only by the corpus gateway's own modules.

Any string literal containing ``ground_truth.json``, ``corpus-version.json`` or
``review-exposure-ledger`` anywhere in ``src/`` or ``scripts/`` -- outside ``truth.py`` and
``truth_paths.py`` -- fails this test unless the tiny allowlist below lists it with a reason.
A module that needs a corpus file must take it from the gateway's handle (``open_corpus``) or the
named constants, so it cannot quietly build its own corpus paths and walk around the gateway.

Expression-statement strings (docstrings) are exempt: they document and open nothing.  f-string
fragments are string literals and are checked.  The direct entry-point tests that prove each public
path actually calls ``open_corpus`` live in ``test_truth_corpus_followup_r7.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEARCHED = (ROOT / "src", ROOT / "scripts")
EXEMPT_FILES = frozenset({"src/id_detector/truth.py", "src/id_detector/truth_paths.py"})
CORPUS_NAMES = ("ground_truth.json", "corpus-version.json", "review-exposure-ledger")

#: (repository-relative file, exact literal) -> why it is allowed.  Keep this tiny.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "src/id_detector/cli.py",
        "Truth directory or ground_truth.json.",
    ): "--truth help text for `benchmark score`; shown to the owner, opens nothing",
    (
        "src/id_detector/cli.py",
        "The <corpus> directory; sets must be <corpus>/<set>/ground_truth.json.",
    ): "--truth help text for `truth freeze`; states the supported layout, opens nothing",
    (
        "src/id_detector/cli.py",
        "Must be <corpus>/corpus-version.json, directly in the corpus directory being frozen: the "
        "only place scoring, certification and review look for it.",
    ): "--out help text for `truth freeze`; states the one manifest location, opens nothing",
}


def _documentation_strings(tree: ast.AST) -> set[int]:
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def corpus_literal_violations(source: str, relative: str) -> list[tuple[str, int, str]]:
    """Every non-documentation string literal in ``source`` naming a corpus file."""

    if relative in EXEMPT_FILES:
        return []
    tree = ast.parse(source)
    documentation = _documentation_strings(tree)
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in documentation:
            continue
        if any(name in node.value for name in CORPUS_NAMES):
            found.append((relative, node.lineno, node.value))
    return found


def _python_files() -> list[Path]:
    return sorted(path for directory in SEARCHED for path in directory.rglob("*.py"))


def test_no_corpus_file_name_literal_outside_the_gateway_modules() -> None:
    violations = [
        (relative, line, value)
        for path in _python_files()
        for relative, line, value in corpus_literal_violations(
            path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix()
        )
        if (relative, value) not in ALLOWLIST
    ]
    assert violations == [], (
        "corpus file names must come from the gateway handle or truth.py constants, not literals: "
        + "; ".join(f"{relative}:{line} {value!r}" for relative, line, value in violations)
    )


def test_every_allowlist_entry_is_still_needed() -> None:
    present = {
        (relative, value)
        for path in _python_files()
        for relative, _line, value in corpus_literal_violations(
            path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix()
        )
    }
    stale = sorted(set(ALLOWLIST) - present)
    assert stale == [], f"remove stale allowlist entries: {stale}"


def test_the_guard_catches_plain_and_formatted_literals_but_not_docstrings() -> None:
    source = '''
"""Module doc mentioning ground_truth.json is fine."""

def reader(root):
    """Docstring mentioning corpus-version.json is fine."""
    plain = root / "ground_truth.json"
    formatted = f"{root}/review-exposure-ledger.jsonl"
    return plain, formatted
'''
    found = corpus_literal_violations(source, "src/id_detector/example.py")
    assert [value for _relative, _line, value in found] == [
        "ground_truth.json",
        "/review-exposure-ledger.jsonl",
    ]
    assert corpus_literal_violations(source, "src/id_detector/truth.py") == []


# Round 10: io's corpus backstop spells the corpus file names itself (io cannot import truth).
ALLOWLIST.update(
    {
        ("src/id_detector/io.py", name): (
            "the low-level atomic-write backstop refuses this name; io cannot import truth, which "
            "imports io"
        )
        for name in ("ground_truth.json", "corpus-version.json", "review-exposure-ledger.jsonl")
    }
)

# Round 10 replaces the round-9 six-module name heuristic.  Every public writer that takes a
# user-selectable report or artifact destination refuses a corpus destination: here a new file
# directly inside a manifest-less corpus like data/corpus/release-1, which only the explicit
# refuse_generated_output guard catches (io's backstop probes just the destination's parent and
# grandparent for corpus files, and this corpus holds its sets one level down).  io's backstop is
# the repository-wide guarantee for any writer not listed here.
WRITER_REFUSAL = "refusing to write generated output"
PUBLIC_WRITERS = (
    "scorer.score_corpus",
    "scripts/score_corpus.py --out",
    "corpus.run_corpus",
    "ablations.run_ablations",
    "transforms_schedule.run_transform_schedule_benchmark",
    "shortlist.run_shortlist",
    "hints.run_hint_gate",
    "validate.run_calibration_validation out_path",
    "validate.run_calibration_validation model_out",
    "profiles.freeze_profiles",
    "idea config init --force",
    "idea benchmark links",
    "idea benchmark links-score",
    "scripts/make_controlled_predictions.py",
)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _refused(call: object) -> None:
    with pytest.raises(ValueError, match=WRITER_REFUSAL):
        call()  # type: ignore[operator]


def _cli_refused(args: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from id_detector import cli

    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code != 0, result.output
    assert WRITER_REFUSAL in result.output, result.output


@pytest.mark.parametrize("writer", PUBLIC_WRITERS)
def test_every_guarded_public_writer_refuses_a_corpus_destination(
    writer: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio
    import shutil
    import sys

    release = tmp_path / "data" / "corpus" / "release-like"
    shutil.copytree(ROOT / "tests" / "fixtures" / "truth-review", release)
    destination = release / "report.json"
    before = _tree(release)
    work = tmp_path / "work"
    if writer == "scorer.score_corpus":
        from id_detector.benchmark.scorer import score_corpus

        _refused(lambda: score_corpus(tmp_path / "t", tmp_path / "p.json", out_path=destination))
    elif writer == "scripts/score_corpus.py --out":
        from scripts.score_corpus import main
        from tests.test_score_corpus import _copy_fixture

        root = _copy_fixture(tmp_path / "mini")
        assert main(["--run-list", str(root / "run-list.json"), "--out", str(destination)]) == 1
        assert WRITER_REFUSAL in capsys.readouterr().err
    elif writer == "corpus.run_corpus":
        from id_detector.benchmark.corpus import run_corpus

        _refused(
            lambda: asyncio.run(
                run_corpus(
                    corpus_version="x",
                    profile="free",
                    out_path=destination,
                    project_root=tmp_path,
                    work_root=work,
                )
            )
        )
    elif writer == "ablations.run_ablations":
        from id_detector.benchmark.ablations import run_ablations

        _refused(
            lambda: run_ablations(
                corpus_version="x", out_path=destination, project_root=tmp_path, work_root=work
            )
        )
    elif writer == "transforms_schedule.run_transform_schedule_benchmark":
        from id_detector.benchmark.transforms_schedule import run_transform_schedule_benchmark

        _refused(
            lambda: run_transform_schedule_benchmark(
                corpus_version="x", out_path=destination, project_root=tmp_path, work_root=work
            )
        )
    elif writer == "shortlist.run_shortlist":
        from id_detector.benchmark.shortlist import run_shortlist

        _refused(
            lambda: asyncio.run(
                run_shortlist(
                    corpus_version="x",
                    out_path=destination,
                    project_root=tmp_path,
                    work_root=work,
                    app_config=None,  # type: ignore[arg-type]
                    cli_confirmation=False,
                )
            )
        )
    elif writer == "hints.run_hint_gate":
        from id_detector.benchmark.hints import run_hint_gate

        _refused(
            lambda: asyncio.run(
                run_hint_gate(
                    corpus_version="x", out_path=destination, project_root=tmp_path, work_root=work
                )
            )
        )
    elif writer.startswith("validate.run_calibration_validation"):
        from id_detector.calibrate.validate import run_calibration_validation

        option = "out_path" if writer.endswith("out_path") else "model_out"
        _refused(
            lambda: asyncio.run(
                run_calibration_validation(
                    corpus_version="x",
                    project_root=tmp_path,
                    work_root=work,
                    **{option: destination},
                )
            )
        )
    elif writer == "profiles.freeze_profiles":
        from id_detector.profiles import freeze_profiles

        _refused(
            lambda: freeze_profiles(
                ablations_path=tmp_path / "a.json",
                shortlist_path=tmp_path / "s.json",
                out_dir=release / "profiles",
            )
        )
    elif writer == "idea config init --force":
        _cli_refused(["config", "init", "--path", str(destination), "--force"], monkeypatch)
    elif writer == "idea benchmark links":
        _cli_refused(
            ["benchmark", "links", "--episodes", str(tmp_path), "--out", str(destination)],
            monkeypatch,
        )
    elif writer == "idea benchmark links-score":
        _cli_refused(
            [
                "benchmark",
                "links-score",
                "--marked",
                str(tmp_path / "marked.json"),
                "--out",
                str(destination),
            ],
            monkeypatch,
        )
    else:
        from scripts.make_controlled_predictions import main as predictions_main

        monkeypatch.setattr(
            sys, "argv", ["make_controlled_predictions.py", str(tmp_path / "t"), str(destination)]
        )
        _refused(predictions_main)
    assert _tree(release) == before
    assert not destination.exists()
