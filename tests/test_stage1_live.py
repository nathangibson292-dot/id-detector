from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from id_detector.calibration import calibrate_shazam


@pytest.mark.live
def test_calibrate_shazam_live(tmp_path: Path) -> None:
    """Opt-in network smoke for the complete insertion-test matrix."""

    url = os.environ.get("ID_DETECTOR_LIVE_TRACK_URL")
    if not url:
        pytest.skip("set ID_DETECTOR_LIVE_TRACK_URL to a held commercially released track")
    result = asyncio.run(
        calibrate_shazam(
            track=url,
            positions_ms=[10_000, 40_000, 70_000, 100_000, 140_000],
            project_root=tmp_path,
        )
    )
    assert result.cases == 45
    assert result.successes > 0
    assert result.physical_attempts == 45
    assert result.config_path.name == "shazam-v1.json"
    config = json.loads(result.config_path.read_text(encoding="utf-8"))
    assert config["config"]["latency_estimator"] == "position-first-success-ecdf-v1"
    curve = config["config"]["latency_success_curve"]
    assert sum(item["n_trials"] for item in curve) == result.cases
    assert sum(item["n_successes"] for item in curve) == result.successes
    assert all(item["n_positions"] == 5 for item in curve)
