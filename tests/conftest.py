"""Shared pytest configuration.

The controlled-render and full-pipeline tests decode/render audio with ffmpeg and spawn real
subprocess trees, so they dominate wall time.  They are tagged ``slow`` here (by module, so the
test files stay free of import-order noise) and the default ``addopts`` in ``pyproject.toml``
deselects ``slow`` and ``live``.  Run everything except live with ``pytest -m "not live"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class _WorkFingerprint:
    watched_files: tuple[tuple[str, tuple[int, int] | None], ...]
    top_level_entries: tuple[tuple[str, int], ...]


_WATCHED_WORK_FILES = (
    Path("index.json"),
    Path(".idea/app.db"),
    Path(".idea/app.db-wal"),
    Path(".idea/app.db-shm"),
)
_session_work_root: Path | None = None
_session_work_fingerprint: _WorkFingerprint | None = None


def _fingerprint_work_root(work_root: Path) -> _WorkFingerprint | None:
    """Describe only the cheap, owner-data sentinels; never open or walk the work tree."""

    if not work_root.is_dir():
        return None
    watched: list[tuple[str, tuple[int, int] | None]] = []
    for relative in _WATCHED_WORK_FILES:
        try:
            stat = (work_root / relative).stat()
        except FileNotFoundError:
            value = None
        else:
            value = (stat.st_size, stat.st_mtime_ns)
        watched.append((relative.as_posix(), value))
    top_level = tuple(
        sorted((entry.name, entry.stat().st_mtime_ns) for entry in work_root.iterdir())
    )
    return _WorkFingerprint(tuple(watched), top_level)


def _work_fingerprint_changes(
    before: _WorkFingerprint, after: _WorkFingerprint | None
) -> list[str]:
    if after is None:
        return ["work/ was removed"]
    changes: list[str] = []
    before_files = dict(before.watched_files)
    after_files = dict(after.watched_files)
    for name in sorted(before_files.keys() | after_files.keys()):
        if before_files.get(name) != after_files.get(name):
            changes.append(
                f"work/{name}: {before_files.get(name)!r} -> {after_files.get(name)!r} "
                "(size, mtime_ns)"
            )
    before_entries = dict(before.top_level_entries)
    after_entries = dict(after.top_level_entries)
    for name in sorted(before_entries.keys() | after_entries.keys()):
        if name not in before_entries:
            changes.append(f"work/{name}: top-level entry added (mtime_ns={after_entries[name]})")
        elif name not in after_entries:
            changes.append(
                f"work/{name}: top-level entry removed (mtime_ns={before_entries[name]})"
            )
        elif before_entries[name] != after_entries[name]:
            changes.append(
                f"work/{name}: top-level mtime_ns {before_entries[name]} -> {after_entries[name]}"
            )
    return changes


def _assert_work_root_unchanged(work_root: Path, before: _WorkFingerprint | None) -> None:
    """Fail with exact sentinels changed; an absent-at-start work tree is deliberately ignored."""

    if before is None:
        return
    changes = _work_fingerprint_changes(before, _fingerprint_work_root(work_root))
    if changes:
        raise AssertionError(
            "pytest changed the repository's real work cache:\n" + "\n".join(changes)
        )


def pytest_sessionstart(session: pytest.Session) -> None:
    global _session_work_root, _session_work_fingerprint

    repository_root = Path(__file__).resolve().parents[1]
    work_root = repository_root / "work"
    fingerprint = _fingerprint_work_root(work_root)
    if fingerprint is not None:
        _session_work_root = work_root
        _session_work_fingerprint = fingerprint


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del exitstatus
    if _session_work_root is None or _session_work_fingerprint is None:
        return
    try:
        _assert_work_root_unchanged(_session_work_root, _session_work_fingerprint)
    except AssertionError as exc:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_sep("!", str(exc), red=True)
        session.exitstatus = int(pytest.ExitCode.TESTS_FAILED)


#: Modules whose tests render audio or drive the full multi-generation / multi-process pipeline.
SLOW_MODULES = frozenset(
    {
        "test_stage2a_controlled",
        "test_stage2b_pipeline",
        "test_stage4b_transforms_schedule",
        "test_stage4c_generations",
    }
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    slow = pytest.mark.slow
    for item in items:
        if item.module.__name__.rsplit(".", 1)[-1] in SLOW_MODULES:
            item.add_marker(slow)


def write_corpus_fixture(path: Path, value: object) -> None:
    """Write a test corpus file directly.

    Production corpus files go through the corpus gateway, and ``io``'s atomic writers refuse
    corpus file names by design, so tests that build a corpus by hand write its files here.
    """

    from id_detector.io import canonical_json_bytes

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


@pytest.fixture
def certification_gate_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open the owner's freeze/certification moratorium for one test.

    Production keeps ``truth.CERTIFICATION_ENABLED`` false.  Tests that exercise the freeze and
    certification logic underneath the gate open it here, by monkeypatch only.
    """

    import id_detector.truth as truth

    monkeypatch.setattr(truth, "CERTIFICATION_ENABLED", True)


@pytest.fixture
def certification_gate_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the freeze/certification gate closed for one test: the production state today.

    Tests that assert the closed-gate behaviour (freeze and certify refuse, nothing is ever called
    certified, every L3 block carries the disabled message) pin it here, by monkeypatch only, so
    they keep testing the closed state whatever the production default later becomes.
    """

    import id_detector.truth as truth

    monkeypatch.setattr(truth, "CERTIFICATION_ENABLED", False)
