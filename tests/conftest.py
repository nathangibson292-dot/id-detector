"""Shared pytest configuration.

The controlled-render and full-pipeline tests decode/render audio with ffmpeg and spawn real
subprocess trees, so they dominate wall time.  They are tagged ``slow`` here (by module, so the
test files stay free of import-order noise) and the default ``addopts`` in ``pyproject.toml``
deselects ``slow`` and ``live``.  Run everything except live with ``pytest -m "not live"``.
"""

from __future__ import annotations

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
