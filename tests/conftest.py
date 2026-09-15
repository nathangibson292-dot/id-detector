"""Shared pytest configuration.

The controlled-render and full-pipeline tests decode/render audio with ffmpeg and spawn real
subprocess trees, so they dominate wall time.  They are tagged ``slow`` here (by module, so the
test files stay free of import-order noise) and the default ``addopts`` in ``pyproject.toml``
deselects ``slow`` and ``live``.  Run everything except live with ``pytest -m "not live"``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
