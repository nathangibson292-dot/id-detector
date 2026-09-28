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


def _golden_with_identity_of(golden: object, produced: object) -> object:
    """The committed golden, with ONLY the run identity and timestamps taken from ``produced``.

    Every other value comes from the golden itself -- including value types, which a semantic
    comparison cannot see (``True == 1 == 1.0`` in Python) -- so rendering it gives exactly the
    bytes the writer must have produced.  An identity field the golden predates is taken from the
    produced document (the semantic comparison ignores it too); any other key the golden lacks is
    left out, so a new result field fails the comparison."""

    if isinstance(golden, dict) and isinstance(produced, dict):
        expected = {}
        for key in set(golden) | set(produced):
            if key in IGNORED_KEYS or is_timestamp_key(str(key)):
                if key in produced:
                    expected[key] = produced[key]
            elif key in golden:
                expected[key] = _golden_with_identity_of(golden[key], produced.get(key))
        return expected
    if isinstance(golden, list) and isinstance(produced, list) and len(produced) == len(golden):
        return [_golden_with_identity_of(a, b) for a, b in zip(golden, produced, strict=True)]
    return golden


def _canonical_bytes(document: object) -> bytes:
    """The result writer's documented format, spelled out here independently of the writer:
    UTF-8, keys sorted, compact separators, no ASCII escaping, no trailing newline."""

    return json.dumps(
        document, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def test_local_free_run_reproduces_the_golden_bytes(tmp_path: Path) -> None:
    # The bytes the Free run actually wrote -- never parsed and re-serialised before comparing.
    produced_raw = run_local_free(tmp_path / "work").read_bytes()
    golden = json.loads(GOLDEN.read_bytes().decode("utf-8"))
    produced = json.loads(produced_raw.decode("utf-8"))
    assert b"\r" not in produced_raw  # the same bytes on every platform: no CRLF on Windows
    assert produced_raw == _canonical_bytes(_golden_with_identity_of(golden, produced))
