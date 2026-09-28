"""Regression tests for the session-wide owner-cache guard."""

import ast
from pathlib import Path

import pytest

from tests.conftest import _assert_work_root_unchanged, _fingerprint_work_root

_WORK_ROOT_COMMANDS = {
    ("acquire",),
    ("analyse",),
    ("backup",),
    ("cost",),
    ("gc",),
    ("hints",),
    ("rescan",),
    ("restore",),
    ("retry",),
    ("serve",),
    ("show",),
    ("benchmark", "ablations"),
    ("benchmark", "calibration-validate"),
    ("benchmark", "certify"),
    ("benchmark", "hints"),
    ("benchmark", "run"),
    ("benchmark", "shortlist"),
    ("benchmark", "transforms-schedule"),
    ("truth", "review"),
}
_CONFIG_COMMANDS = {
    ("analyse",),
    ("cost",),
    ("rescan",),
    ("serve",),
    ("benchmark", "shortlist"),
}


def _known_list_strings(
    node: ast.AST, assignments: dict[str, ast.AST], seen: frozenset[str] = frozenset()
) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Starred):
        return _known_list_strings(node.value, assignments, seen)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [
            value
            for element in node.elts
            for value in _known_list_strings(element, assignments, seen)
        ]
    if isinstance(node, ast.Name) and node.id in assignments and node.id not in seen:
        return _known_list_strings(assignments[node.id], assignments, seen | {node.id})
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _known_list_strings(node.left, assignments, seen) + _known_list_strings(
            node.right, assignments, seen
        )
    if isinstance(node, ast.IfExp):
        return _known_list_strings(node.body, assignments, seen) + _known_list_strings(
            node.orelse, assignments, seen
        )
    return []


def test_cli_tests_always_inject_work_roots_and_repository_config_paths() -> None:
    """Keep every statically visible CliRunner call isolated from checkout-local defaults."""

    failures: list[str] = []
    tests_root = Path(__file__).parent
    for path in sorted(tests_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
            assignments = {
                target.id: statement.value
                for statement in function.body
                if isinstance(statement, ast.Assign)
                for target in statement.targets
                if isinstance(target, ast.Name)
            }
            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                if (
                    not isinstance(call.func, ast.Attribute)
                    or call.func.attr != "invoke"
                    or len(call.args) < 2
                ):
                    continue
                tokens = _known_list_strings(call.args[1], assignments)
                command = next(
                    (
                        candidate
                        for candidate in _WORK_ROOT_COMMANDS
                        if tokens[: len(candidate)] == list(candidate)
                    ),
                    None,
                )
                if command is None:
                    continue
                location = f"{path.relative_to(tests_root)}:{call.lineno}"
                if "--work-root" not in tokens:
                    failures.append(f"{location} {' '.join(command)} has no --work-root")
                if command in _CONFIG_COMMANDS and "--config" not in tokens:
                    failures.append(f"{location} {' '.join(command)} has no --config")
    assert not failures, "\n".join(failures)


def test_work_cache_guard_is_injectable_catches_a_write_and_ignores_an_absent_root(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "absent"
    assert _fingerprint_work_root(absent) is None
    _assert_work_root_unchanged(absent, None)

    work_root = tmp_path / "work"
    work_root.mkdir()
    index = work_root / "index.json"
    index.write_text("{}", encoding="utf-8")
    before = _fingerprint_work_root(work_root)
    assert before is not None

    index.write_text('{"unexpected":"test write"}', encoding="utf-8")
    with pytest.raises(AssertionError, match=r"work/index\.json.*size, mtime_ns"):
        _assert_work_root_unchanged(work_root, before)
