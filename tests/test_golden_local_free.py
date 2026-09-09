"""Phase 0b-ii gate: the Local Free golden.

``scripts/make_golden.py`` generated ``tests/golden/local-free/tracklist.json`` from the 60 s
fixture with the scripted Shazam fake; this re-runs the identical offline pipeline into a fresh
work root and compares the two documents semantically — ignoring ``run_id``, timestamps,
``generated_by``, ``requested_recipe_id``, ``algorithm_version``, ``analysis_key`` and
``presentation_version`` (plan 0b-ii), so a re-run's identity never fails the gate but a change
in what the free tier lists does."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.make_golden import (
    GOLDEN,
    IGNORED_KEYS,
    SCRIPT,
    is_timestamp_key,
    run_local_free,
    semantic,
)


def test_the_golden_and_its_script_are_committed() -> None:
    assert GOLDEN.is_file(), "run `uv run python scripts/make_golden.py`"
    assert SCRIPT.is_file()
    assert {
        "run_id",
        "generated_by",
        "requested_recipe_id",
        "algorithm_version",
        "analysis_key",
        "presentation_version",
    } == IGNORED_KEYS


def test_semantic_comparison_ignores_identity_fields_and_timestamps_at_every_depth() -> None:
    document = {
        "run_id": "a",
        "generated_by": "id-detector/9",
        "started_at": "2026-01-01T00:00:00Z",
        "timestamp": 1,
        "status": "complete",
        "entries": [
            {"title": "x", "analysis_key": "k", "finished_at": "t", "nested": {"run_id": "b"}}
        ],
    }
    assert semantic(document) == {
        "status": "complete",
        "entries": [{"title": "x", "nested": {}}],
    }
    assert is_timestamp_key("generated_at") and is_timestamp_key("timestamps")
    assert not is_timestamp_key("start_ms") and not is_timestamp_key("attempts")
    # Only the listed identity fields are ignored: a result field with a similar name is compared.
    assert semantic({"requested_recipe": "free"}) == {"requested_recipe": "free"}


def test_local_free_run_matches_the_golden_semantically(tmp_path: Path) -> None:
    produced = json.loads(run_local_free(tmp_path / "work").read_text(encoding="utf-8"))
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert semantic(produced) == semantic(golden)
    # What the golden certifies about the local free tier on the fixture.
    assert produced["status"] == "complete" and produced["achieved"] == "free"
    assert [entry["title"] for entry in produced["entries"]] == ["Tone 440", "Tone 554", "Tone 659"]
    assert all(entry["kind"] == "track" for entry in produced["entries"])
    assert [entry["start_ms"] for entry in produced["entries"]] == sorted(
        entry["start_ms"] for entry in produced["entries"]
    )


def test_the_golden_is_canonical_json_with_lf_endings() -> None:
    raw = GOLDEN.read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    document = json.loads(raw.decode("utf-8"))
    expected = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    assert raw.decode("utf-8") == expected
